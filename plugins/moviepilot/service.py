"""资源投影与直接下载确认；重用 MP 默认下载规则和平台租户会话。"""

from __future__ import annotations

import json
import math
import re
import secrets
from datetime import timedelta

from sqlalchemy import select

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.moviepilot import MoviePilotDownload
from plugins.moviepilot.client import MoviePilotError, clean, digest, identity, promotion, promotion_label


def resource_projection(context, selected):
    """使用媒体身份和站点详情页指纹定位资源，签名下载地址变化不会更换资源身份。"""
    torrent = context.get("torrent_info")
    if not isinstance(torrent, dict):
        return None
    try:
        size = float(torrent.get("size") or 0)
        if not math.isfinite(size) or size < 0:
            size = 0
        seeds = max(0, int(torrent.get("seeders") or 0))
    except (TypeError, ValueError, OverflowError):
        size, seeds = 0, 0
    media = context.get("media_info")
    site, page = torrent.get("site"), torrent.get("page_url")
    # 未确认媒体归属、缺失站点或详情页时仍可展示，但不能从不完整信息推导下载权限。
    downloadable = (isinstance(media, dict) and identity(media) == identity(selected)
        and type(site) is int and site > 0 and isinstance(page, str) and 0 < len(page) <= 4096
        and isinstance(torrent.get("enclosure"), str) and bool(torrent["enclosure"]) and size > 0)
    key = digest(identity(selected), site, page, torrent.get("title"), size) if downloadable else ""
    return {"key": key, "title": clean(torrent.get("title"), 220), "site": clean(torrent.get("site_name"), 80),
            "size": size, "seeders": seeds, "promotion": promotion(torrent)}


def resource_line(resource):
    size = f"{resource['size'] / 1024**3:.1f} GB" if resource["size"] else "大小未知"
    return (f"{resource['title']}\n{resource['site'] or '站点未知'} · {size} · 做种 {resource['seeders']}"
            f"\n[{promotion_label(resource['promotion'])}]")


def remember_resources(session, selected, resources):
    """订阅上的优惠只标为最近查询快照，不能冒充 MP 自动下载时的优惠。"""
    if not resources:
        return
    row = session.find(selected)
    if row and row.status == "active":
        saved = json.loads(row.notification_json)
        saved["latest_resource"] = {"resource": resources[0], "at": now_shanghai().isoformat(timespec="seconds")}
        row.notification_json = json.dumps(saved, ensure_ascii=False, sort_keys=True)


def resource_results(session, api, selected):
    session.save_resources(selected, [])
    resources = [p for item in api.resources(selected) if (p := resource_projection(item, selected)) is not None]
    session.save_resources(selected, resources)
    remember_resources(session, selected, resources)
    if not resources:
        return "暂未找到可用资源。"
    lines = [f"《{selected['title']}》的资源："]
    for i, resource in enumerate(resources, 1):
        lines.append(f"{i}. {resource_line(resource)}" + ("" if resource["key"] else "\n此项信息不足，暂不能直接下载"))
    lines.append("发送“下载资源 序号”选择，再按提示确认下载，无需订阅。\nFree 为下载不计量，50% 为下载量按一半计；优惠以站点实际结算为准。")
    return "\n".join(lines)


def download_query(session):
    return select(MoviePilotDownload).where(MoviePilotDownload.tenant_id == session.context.tenant_id,
        MoviePilotDownload.binding_id == session.context.binding_id,
        MoviePilotDownload.connection_key == session.connection_key)


def confirmation(row, *, changed=False):
    selected, resource = json.loads(row.media_json), json.loads(row.resource_json)
    return (("资源优惠发生变化，请重新确认。\n" if changed else "")
        + f"确认直接下载《{selected['title']}》（{selected['year']}）\n{resource_line(resource)}\n"
        + f"发送“确认下载 {row.token}”，5 分钟内有效。使用 MP 默认下载设置，不创建订阅。")


def new_confirmation(row, resource):
    row.resource_json = json.dumps(resource, ensure_ascii=False, sort_keys=True)
    row.token = secrets.token_hex(6)
    row.expires_at = now_shanghai() + timedelta(minutes=5)
    row.status = "pending"


def prior_outcome(row):
    if row.status == "submitted":
        return "该资源已提交下载，不会重复提交。可在 MP 下载列表查看进度。"
    if row.status == "uncertain":
        raise MoviePilotError("该资源上次下载结果待核实，请先查看 MP 下载列表；不会重复提交。", uncertain=True)
    return None


def prepare_download(session, argument):
    selected, resource = session.resource(argument)
    if not resource["key"]:
        raise MoviePilotError("此资源的媒体或站点信息不完整，请到 MP 页面检查。")
    row = session.db.scalar(download_query(session).where(MoviePilotDownload.resource_key == resource["key"]).with_for_update())
    if row:
        outcome = prior_outcome(row)
        if outcome:
            return outcome
        if (row.status == "pending" and ensure_shanghai_datetime(row.expires_at) > now_shanghai()
                and (row.conversation_id != session.context.conversation_id or row.sender_id != session.context.sender_id)):
            raise MoviePilotError("该资源已有待确认操作，请稍后再试。")
    else:
        row = MoviePilotDownload(tenant_id=session.context.tenant_id, binding_id=session.context.binding_id,
            connection_key=session.connection_key, resource_key=resource["key"])
        session.db.add(row)
    row.conversation_id, row.sender_id = session.context.conversation_id, session.context.sender_id
    row.media_json = json.dumps(selected, ensure_ascii=False, sort_keys=True)
    new_confirmation(row, resource)
    session.db.flush()
    return confirmation(row)


def confirm_download(session, api, argument):
    if not re.fullmatch(r"[0-9a-f]{12}", argument):
        raise MoviePilotError("请使用下载确认卡中的完整确认码。")
    row = session.db.scalar(download_query(session).where(MoviePilotDownload.token == argument,
        MoviePilotDownload.conversation_id == session.context.conversation_id,
        MoviePilotDownload.sender_id == session.context.sender_id).with_for_update())
    if row is None:
        raise MoviePilotError("确认码无效或不属于你在本群的下载。")
    outcome = prior_outcome(row)
    if outcome:
        return outcome
    if row.status != "pending" or ensure_shanghai_datetime(row.expires_at) <= now_shanghai():
        raise MoviePilotError("确认已失效，请重新发送“下载资源 序号”。")
    selected, saved = json.loads(row.media_json), json.loads(row.resource_json)
    matched = []
    for item in api.resources(selected):
        projection = resource_projection(item, selected)
        if projection and projection["key"] == row.resource_key:
            matched.append((item["torrent_info"], projection))
    if len(matched) != 1:
        raise MoviePilotError("原资源已不可用或无法唯一确认，请重新找资源；本次未提交下载。")
    torrent, current = matched[0]
    if current["promotion"] != saved["promotion"]:
        new_confirmation(row, current)
        return confirmation(row, changed=True)
    # 再次搜索可能耗时较长，真正提交前重新检查确认期限。
    if ensure_shanghai_datetime(row.expires_at) <= now_shanghai():
        raise MoviePilotError("确认已过期，请重新选择资源。")
    tracking = session.find(selected)
    if tracking and tracking.status in {"uncertain", "cancelling"}:
        raise MoviePilotError("此电影的订阅操作仍待核实，请先检查 MP 状态。")
    if tracking is None or tracking.status == "cancelled":
        baseline = api.baseline()
        if ensure_shanghai_datetime(row.expires_at) <= now_shanghai():
            raise MoviePilotError("确认已过期，请重新选择资源。")
        tracking = session.register(selected, baseline)
        state = json.loads(tracking.notification_json)
        state["origin"] = "direct"
        tracking.notification_json = json.dumps(state, sort_keys=True)
    row.registration_id = tracking.id
    remember_resources(session, selected, [current])
    row.resource_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
    row.status = "uncertain"
    session.db.flush()
    try:
        row.download_id = api.download(selected, torrent)
    except MoviePilotError as exc:
        if not exc.uncertain:
            row.status = "failed"
        raise
    row.status = "submitted"
    return (f"已提交直接下载《{selected['title']}》，未创建订阅。\n{resource_line(current)}\n"
            "后续下载和整理状态将按本群通知设置推送。")
