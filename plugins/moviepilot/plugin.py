"""MP 原生电影订阅入口：群内选片，显式订阅，用户只能取消自己创建的订阅。"""

from __future__ import annotations

import json

from app.models.moviepilot import MoviePilotSubscription
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.replies import link_reply, text_reply
from plugins.moviepilot.client import MoviePilotClient, MoviePilotError, identity, promotion_label, subscription_fingerprint
from plugins.moviepilot.service import confirm_download, prepare_download, remember_resources, resource_results
from plugins.moviepilot.state import MoviePilotSession, RESOURCE_NAMESPACE, row_identity

COMMANDS = ("MP帮助", "搜电影", "选电影", "订阅电影", "找资源", "我的电影订阅",
            "取消电影订阅", "关注电影", "取消电影关注", "下载资源", "确认下载")
HELP = """MoviePilot 电影助手
搜电影 片名 → 选电影 序号
订阅电影 序号：创建 MP 原生订阅，MP 会按设置自动下载
找资源 序号：查看资源版本、大小、做种和 Free／折扣
下载资源 序号 → 确认下载 确认码：直接下载，无需订阅
我的电影订阅：查看你在本群的订阅和关注
取消电影订阅 编号：取消你创建的原生订阅（保留已下载文件）
关注电影 序号：关注已经存在的 MP 订阅通知
取消电影关注 编号：仅停止群通知，保留 MP 订阅
搜索结果有效期 10 分钟，编号仅对你在本群有效。"""


def reply(text, *, ok=True, status="completed", extra=None):
    return PluginExecutionResult(ok=ok, status=status, message=text[:180],
        output={"replies": [text_reply(text), *(extra or [])]})


def execute_action(request):
    """重载只报告就绪，不连接 MP，不创建订阅或发送消息。"""
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="ready", message="MoviePilot 插件已就绪。")
    return PluginExecutionResult(ok=False, status="unsupported", message="请在已启用插件的群内使用电影命令。")


def execute_message_action(db, request):
    if request.action != "execute":
        return execute_action(request)
    command = str(request.payload.get("command_name") or "")
    argument = str(request.payload.get("command_args") or "").strip()
    if command not in COMMANDS:
        return reply("不支持此电影命令。", ok=False, status="unsupported")
    try:
        api = MoviePilotClient(request.config)
        session = MoviePilotSession(db, request.context, api.connection_key)
        if command == "MP帮助":
            return reply(HELP)
        session.throttle(mutation=command in {"订阅电影", "取消电影订阅", "关注电影", "取消电影关注", "下载资源", "确认下载"})
        if command == "搜电影":
            # 清空旧列表在请求之前执行；失败的新搜索不能继续使用旧作品编号。
            session.save_selection([])
            if not argument or len(argument) > 100:
                raise MoviePilotError("请输入不超过 100 字的片名，例如：搜电影 星际穿越。")
            movies = api.search(argument)
            session.save_selection(movies)
            if not movies:
                return reply("没有找到电影，请尝试补充年份或更换片名。", status="empty")
            return reply("\n".join([*[f"{i}. {m['title']}（{m['year'] or '年份未知'}）" for i, m in enumerate(movies, 1)],
                "发送“选电影 序号”查看详情，“找资源 序号”查询资源。",
                "发送“订阅电影 序号”后，MP 将按设置自动下载。"]), status="candidates")
        if command == "我的电影订阅":
            return list_subscriptions(session, api)
        if command in {"取消电影订阅", "取消电影关注"}:
            return cancel_subscription(session, api, argument, native=command == "取消电影订阅")
        if command == "下载资源":
            return reply(prepare_download(session, argument))
        if command == "确认下载":
            return reply(confirm_download(session, api, argument))
        selected = session.selection(argument)
        if command == "选电影":
            card = link_reply(url=f"https://www.themoviedb.org/movie/{selected['media_id']}",
                title=f"{selected['title']}（{selected['year']}）", description=selected["overview"],
                thumb_url=selected["poster"])
            return reply(f"{selected['title']}（{selected['year']}）\n{selected['overview'] or '暂无简介'}\n"
                f"找资源 {argument}\n订阅电影 {argument}（MP 将自动下载）", extra=[card])
        if command == "找资源":
            return reply(resource_results(session, api, selected))
        return subscribe(session, api, selected, watch_only=command == "关注电影")
    except MoviePilotError as exc:
        return reply(str(exc), ok=False, status="uncertain" if exc.uncertain else "error")


def subscribe(session, api, selected, *, watch_only):
    row = session.find(selected)
    direct_only = (row is not None and row.status == "active" and row.remote_id is None
                   and json.loads(row.notification_json).get("origin") == "direct")
    if row and row.status in {"uncertain", "cancelling"}:
        return reply(f"登记 #{row.id} 的上次操作结果待核实，请先在 MP 页面检查；不会重复提交。", ok=False, status="uncertain")
    if row and row.status == "active" and (not direct_only or (watch_only and not row.notify_enabled)):
        if watch_only and not row.notify_enabled:
            saved = json.loads(row.notification_json)
            saved.update({"floor": api.baseline(), "seen": {}})
            row.notification_json = json.dumps(saved, sort_keys=True)
            row.notify_enabled = True
            return reply(f"已恢复《{row.title}》的群通知，编号 {row.id}。")
        return reply(f"你已在本群登记《{row.title}》，编号 {row.id}。")
    existing = api.subscriptions()
    matched = [x for x in existing if identity(x) == identity(selected)]
    if matched and not watch_only:
        return reply("MP 已有这部电影的订阅。可发送“关注电影 序号”接收通知；不会取得该订阅的取消权限。")
    if watch_only and not matched:
        return reply("MP 尚无这部电影的订阅，请使用“订阅电影 序号”创建。")
    # 直接下载的通知登记不等于 MP 订阅；后续仍可创建原生订阅，且保留通知水位。
    if not direct_only:
        row = session.register(selected, api.baseline())
    cached = session.store.get(RESOURCE_NAMESPACE, session.key) or {}
    if identity(cached.get("movie") or {}) == identity(selected):
        remember_resources(session, selected, cached.get("resources", []))
    if watch_only:
        session.attach(row, matched[0])
        return reply(f"已关注《{row.title}》，编号 {row.id}。之后的下载和整理变化会通知本群。")
    # POST 超时不代表回滚；保存待核实状态，用户再次发命令也不能自动重试。
    row.status = "uncertain"
    try:
        sid, created_receipt = api.create(selected)
        row.remote_id = sid
        current = api.subscriptions()
        remote = next((x for x in current if x.get("id") == sid and identity(x) == identity(selected)), None)
        if remote is None:
            raise MoviePilotError("MP 已接受订阅，但当前状态尚无法确认，请到 MP 页面核实。", uncertain=True)
        # 快照中已存在的 ID 绝不能被视为本群创建，避免 native API 的 exist_ok 把他人订阅转交给当前成员。
        created = created_receipt and sid not in {x.get("id") for x in existing}
        session.attach(row, remote, created=created)
    except MoviePilotError as exc:
        if not exc.uncertain and row.remote_id is None:
            row.status = "active" if direct_only else "cancelled"
        raise
    if not row.can_delete:
        return reply(f"MP 已有或已接受《{row.title}》的订阅，已登记关注，编号 {row.id}。\n未取得明确新增回执，取消原生订阅请在 MP 页面操作。")
    return reply(f"已订阅《{row.title}》（{row.year}），编号 {row.id}。\nMP 会按现有设置自动搜索和下载，后续状态通知本群。")


def list_subscriptions(session, api):
    rows = session.db.scalars(session.query().where(MoviePilotSubscription.status != "cancelled")
        .order_by(MoviePilotSubscription.id.desc()).limit(50)).all()
    if not rows:
        return reply("你在本群还没有电影订阅或关注。发送“搜电影 片名”开始。")
    remote = {x.get("id"): x for x in api.subscriptions()}
    names = {"N": "等待搜索", "R": "订阅中", "P": "待定", "S": "已暂停"}
    lines = []
    for row in rows:
        item = remote.get(row.remote_id)
        saved = json.loads(row.notification_json)
        direct_only = row.remote_id is None and saved.get("origin") == "direct"
        if row.status in {"uncertain", "cancelling"}:
            status = "操作结果待核实，请查看 MP"
        elif direct_only:
            status = "直接下载记录，未创建原生订阅"
        elif item is None or identity(item) != row_identity(row):
            status = "原生订阅已不在活动列表；仍关注下载整理"
        else:
            status = names.get(item.get("state"), "状态待确认")
        lines.append(f"{row.id}. {row.title}（{row.year}）｜{status}｜{'订阅' if row.can_delete else '关注'}"
            + ("｜通知已关闭" if not row.notify_enabled else ""))
        latest = saved.get("latest_resource")
        if latest:
            resource = latest["resource"]
            lines.append(f"最近查看资源：{promotion_label(resource['promotion'])}（{latest['at']}）\n{resource['title']}")
        else:
            lines.append("资源优惠未知，可先“搜电影”再“找资源”查看。")
    return reply("\n".join(lines))


def cancel_subscription(session, api, argument, *, native):
    row = session.own(argument)
    if not native:
        row.notify_enabled = False
        return reply(f"已停止《{row.title}》的群通知，MP 订阅保持原样。")
    if row.status == "cancelled":
        return reply("这条登记已取消；如需管理原生订阅，请到 MP 页面操作。")
    if not row.can_delete or row.status != "active":
        raise MoviePilotError("只能取消你通过插件创建且结果已确认的订阅；关注的订阅请在 MP 页面管理。")
    remote = next((x for x in api.subscriptions() if x.get("id") == row.remote_id), None)
    if remote is None:
        row.status = "cancelled"
        return reply("原生订阅已不存在，已停止本地关注。")
    if subscription_fingerprint(remote) != row.remote_fingerprint:
        raise MoviePilotError("原生订阅身份已变化，请到 MP 页面核实，插件不会删除。")
    row.status = "cancelling"
    try:
        api.delete(row.remote_id)
    except MoviePilotError as exc:
        if not exc.uncertain:
            row.status = "active"
        raise
    row.status = "cancelled"
    return reply(f"已取消《{row.title}》的原生订阅和本地关注。已存在的下载任务和文件保留。")


def execute_scheduled_action(db, request):
    # 保持目录入口可重载，具体轮询职责独立，避免命令入口混入平台发送实现。
    from plugins.moviepilot.notifications import poll_notifications

    if request.action != "poll_notifications":
        return execute_action(request)
    try:
        return poll_notifications(db, request)
    except MoviePilotError as exc:
        return PluginExecutionResult(ok=False, status="error", message=str(exc))
