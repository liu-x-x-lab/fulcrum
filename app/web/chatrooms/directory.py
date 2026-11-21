"""群与联系人目录、群详情和从联系人创建会话的入口。"""

from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, IpadFriend, WorkWechatContact
from app.services.account_identity import account_identity
from app.services import ai_dispatch, payment_ledger
from app.services.channel_catalog import channel_supports
from app.services.chatroom_invitations import build_invitation_graph
from app.services.chatroom_profile import build_chatroom_profile
from app.services.chatroom_registry import (
    GROUP_MODE_COLLECT_ONLY,
    GROUP_MODE_OFF,
    GROUP_MODE_OPTIONS,
    GROUP_MODE_RUN,
    is_collection_off,
)
from app.services.crud import get_channel
from app.services.directory_pages import (
    build_chatroom_directory,
    build_chatroom_plugin_directory,
    build_contact_directory,
    channel_options,
    chatroom_plugin_options,
)
from app.services.group_features.view import build_feature_cards
from app.services import ai_providers
from app.services.group_moderation.view import recent_records as moderation_records
from app.services.group_plugin_settings import attach_group_plugin_settings
from app.services.identity_directory import _avatar_from_extra
from app.services.ops import record_audit
from app.services.saas_auth import has_tenant_role
from app.services.wechat_ipad import build_wechat_ipad_chatroom_detail
from app.services.wechat_ipad.chatrooms import update_group_plugin_scope_enabled
from app.services.wechat_ipad.operations.group_rename import available as rename_available
from app.services.wechat_webridge import (
    directory_sync as webridge_directory_sync,
    invitation_sync as webridge_invitation_sync,
    wakeup as webridge_wakeup,
)
from app.services.wechat_webridge.chatrooms import build_webridge_chatroom_detail
from app.services.wechat_webridge.service import enqueue_command
from app.services.wechat_winauto_chatrooms import build_wechat_winauto_chatroom_detail
from app.services.workwechat_chatrooms import build_workwechat_chatroom_detail
from app.web.auth_common import require_tenant_csrf
from app.web.channels.definitions import GROUP_SCOPE_CHANNEL_TYPES
from app.web.plugins.routes import _current_tenant_id
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.operations import _completed_operation_response, _csrf_from_header
from app.web.shared.presentation import render
from app.web.shared.urls import build_url, safe_return_to


router = APIRouter()


@router.get("/channels/{channel_id}/chatrooms/{chatroom_id}", response_class=HTMLResponse)
async def channel_chatroom_detail_page(request: Request, channel_id: int, chatroom_id: int, tab: str = "features"):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type == "wechat_ipad":
            detail = build_wechat_ipad_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_winauto":
            detail = build_wechat_winauto_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "workwechat":
            detail = build_workwechat_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_webridge":
            detail = build_webridge_chatroom_detail(db, channel, chatroom)
        elif channel.channel_type == "wechat_gugatool":
            # 咕嘎助手的群实体由群消息驱动创建，详情页复用通用构造但隐藏同步按钮。
            from app.services.wechat_gugatool_chatrooms import build_wechat_gugatool_chatroom_detail

            detail = build_wechat_gugatool_chatroom_detail(db, channel, chatroom)
        else:
            raise HTTPException(status_code=400, detail="Chatroom detail only supported for wechat_ipad / wechat_winauto / workwechat / wechat_webridge / wechat_gugatool channels")
        # 群头像与列表共用已保存的群资料，避免渠道详情遗漏头像后退回首字占位。
        detail["avatar_url"] = _avatar_from_extra(chatroom.extra_json)
        # 群插件卡片展示真正合并后的本群配置，敏感密钥不会进入模板或保存回报。
        if tab == 'plugins':
            attach_group_plugin_settings(db, channel, chatroom, detail['bindings'])
        # 资料标签只读取本群已保存的快照；打开页面不自动向设备下发命令。
        detail["profile"] = build_chatroom_profile(channel, chatroom, detail["members"])
        detail["can_rename"] = (tab == "profile" and rename_available(channel, chatroom)
                                and has_tenant_role(require_user(request), "operator"))
        # 图谱与成员表使用同一份邀请快照；未知成员只分组展示，不推断额外邀请关系。
        detail["invitations"] = build_invitation_graph(detail["members"])
        # 读取能力提示与已保存的进度，页面 GET 绝不触发逐成员手机查询。
        detail["invitation_sync"] = webridge_invitation_sync.view(chatroom)
        detail["invitation_sync_reason"] = webridge_invitation_sync.support_reason(channel, chatroom)
        # 群消息只在消息中心里有一份:这里给出直达这个群会话的入口,不再自带一张最近 30 条的表
        group_conversation_id = db.scalar(
            select(Conversation.id).where(Conversation.channel_id == channel.id, Conversation.external_id == chatroom.external_id)
        )
        detail["message_center_href"] = "/messages?" + urlencode({"channel_id": channel.id, **({"conversation_id": int(group_conversation_id)} if group_conversation_id else {"chat_type": "group"})})
        ai_setting = ai_dispatch.get_ai_setting(db)
        return render(
            request,
            "chatroom_detail.html",
            page_title=f"群详情 · {detail['name']}",
            channel=channel,
            chatroom=chatroom,
            detail=detail,
            back_href=safe_return_to(request.query_params.get("return_to"), f"/chatrooms?channel_id={channel.id}"),
            can_sync=has_tenant_role(require_user(request), "operator"),
            # 群详情分为 功能/插件/成员/消息/资料 五个标签，非法值回落到功能。
            tab=tab if tab in ("features", "plugins", "members", "profile") else "features",
            # 没单独保存过设置的能力跟随默认值:AI 对话跟随租户「AI 助手」里的聊天开关。
            features=build_feature_cards(chatroom, channel.channel_type, defaults={"ai_chat": bool(getattr(ai_setting, "chat_enabled", False))},
                ai_catalog=ai_providers.public_catalog(db),
                default_ai_models=ai_providers.available_models(ai_setting.provider) if ai_setting and ai_setting.provider else []),
            moderation_records=moderation_records(db, chatroom) if tab == "features" else [],
            group_modes=GROUP_MODE_OPTIONS,
            # 自动抢红包、自动收款的明细与统计,按群功能 key 取(仅微桥的群有这两个功能)。
            ledgers=(
                {
                    "red_packet": payment_ledger.build_ledger(db, channel_id=channel.id, kind=payment_ledger.KIND_RED_PACKET, chat_external_id=chatroom.external_id),
                    "transfer_receive": payment_ledger.build_ledger(db, channel_id=channel.id, kind=payment_ledger.KIND_TRANSFER, chat_external_id=chatroom.external_id),
                }
                if channel.channel_type == "wechat_webridge"
                else {}
            ),
            # 当前运行模式:完全关闭看状态,其余按总开关(is_active)区分正常运行与只采集。
            group_mode=GROUP_MODE_OFF if is_collection_off(chatroom) else (GROUP_MODE_RUN if chatroom.is_active else GROUP_MODE_COLLECT_ONLY),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


def _directory_pager_base(path: str, **params: Any) -> str:
    """目录页分页链接的公共前缀:保留搜索与筛选条件,页码由模板追加。"""

    query = urlencode({key: value for key, value in params.items() if value not in ("", 0, None)})
    return f"{path}?{query}" if query else f"{path}?"


def _webridge_directory_sync_channels(db: Session) -> list[Channel]:
    return list(db.scalars(select(Channel).where(
        Channel.channel_type == "wechat_webridge", Channel.deleted_at.is_(None), Channel.is_enabled.is_(True),
    ).order_by(Channel.id)).all())


@router.post("/contacts/sync")
@router.post("/chatrooms/sync")
async def sync_webridge_directory_action(
    request: Request,
    channel_id: int = Form(0),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channels = _webridge_directory_sync_channels(db)
        if channel_id:
            channels = [channel for channel in channels if channel.id == channel_id]
            if not channels:
                raise HTTPException(status_code=404, detail="未找到已启用的微桥账号")
        if not channels:
            raise HTTPException(status_code=400, detail="请先接入并启用微桥账号")
        base = "/contacts" if request.url.path == "/contacts/sync" else "/chatrooms"
        command_name, label = ("friends_all", "好友") if base == "/contacts" else ("groups_all", "群聊")
        jobs = []
        for channel in channels:
            job = enqueue_command(db, channel, command_name=command_name, args={})
            jobs.append({"id": int(job.id), "channel_id": int(channel.id), "channel_name": channel.name})
        db.commit()
        for channel in channels:
            webridge_wakeup.notify(int(channel.id))
        job_ids = ",".join(str(job["id"]) for job in jobs)
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": True, "jobs": jobs, "status_url": f"{base}/sync/status?job_ids={job_ids}"}, headers={"Cache-Control": "no-store"})
        # 返回地址只接受当前列表，避免把同步表单变成外部跳转入口。
        try:
            target = urlsplit(next)
        except ValueError:
            target = urlsplit(base)
        query = parse_qsl(target.query, keep_blank_values=True) if target.path == base and not target.scheme and not target.netloc else []
        query = [(key, value) for key, value in query if key not in {"notice", "sync_jobs"}]
        query.append(("sync_jobs", job_ids))
        query.append(("notice", f"正在同步 {len(channels)} 个账号的{label}"))
        return RedirectResponse(url=f"{base}?{urlencode(query)}", status_code=303)
    finally:
        db.close()


@router.get("/contacts/sync/status")
@router.get("/chatrooms/sync/status")
async def webridge_directory_sync_status(request: Request, job_ids: str = ""):
    enforce_role(request, "ops")
    db = get_session()
    try:
        kind = "contacts" if request.url.path == "/contacts/sync/status" else "chatrooms"
        result = webridge_directory_sync.sync_status(db, webridge_directory_sync.parse_job_ids(job_ids), kind)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except webridge_directory_sync.DirectorySyncError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    finally:
        db.close()


@router.get("/chatrooms", response_class=HTMLResponse)
async def chatroom_directory_page(request: Request, q: str = "", channel_id: int = 0, status: str = "", page: int = 1):
    """群管理唯一入口:所有账号的群合并展示,支持搜索、账号与状态筛选。"""

    db = get_session()
    try:
        directory = build_chatroom_directory(db, keyword=q, channel_id=channel_id, status=status, page=page)
        return render(
            request,
            "chatroom_directory.html",
            page_title="群管理",
            directory=directory,
            channels=channel_options(db),
            pager_base=_directory_pager_base("/chatrooms", q=directory["keyword"], channel_id=channel_id, status=directory["status"]),
            notice=request.query_params.get("notice", ""),
            can_edit=has_tenant_role(require_user(request), "operator"),
            sync_channels=_webridge_directory_sync_channels(db),
            sync_action="/chatrooms/sync",
            sync_label="同步群聊",
        )
    finally:
        db.close()


@router.get("/chatrooms/plugin-scopes", response_class=HTMLResponse)
async def chatroom_plugin_scopes_page(request: Request, plugin_id: int = 0, q: str = "", channel_id: int = 0, scope_status: str = "", scope_source: str = "", page: int = 1):
    user = enforce_role(request, "readonly")
    db = get_session()
    try:
        directory = build_chatroom_plugin_directory(db, tenant_id=user.tenant_id, plugin_id=plugin_id, keyword=q, channel_id=channel_id, scope_status=scope_status, scope_source=scope_source, page=page)
        if plugin_id and directory["plugin"] is None:
            raise HTTPException(status_code=404, detail="插件未绑定或当前不可用")
        return render(
            request,
            "chatroom_plugin_scopes.html",
            page_title="群插件批量管理",
            directory=directory,
            tenant_id=user.tenant_id,
            pager_base=_directory_pager_base("/chatrooms/plugin-scopes", plugin_id=plugin_id, q=directory["keyword"], channel_id=channel_id, scope_status=directory["status"], scope_source=directory["source"]),
            selection_cleared=request.query_params.get("selection_cleared") == "1",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.post("/chatrooms/plugins/batch")
async def batch_chatroom_plugin_scope_action(
    request: Request,
    plugin_id: int = Form(...),
    chatroom_ids: list[int] = Form(...),
    mode: str = Form(...),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    """只调整所选群内的一个插件，不打开群总开关或机器人插件总开关。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        if mode not in {"enabled", "disabled"}:
            raise HTTPException(status_code=400, detail="请选择启用或禁用")
        room_ids = set(chatroom_ids)
        if not room_ids or len(room_ids) > 1000:
            raise HTTPException(status_code=400, detail="请选择 1 至 1000 个群聊，可跨页选择")
        option = {item["id"]: item for item in chatroom_plugin_options(db, user.tenant_id)}.get(plugin_id)
        if option is None:
            raise HTTPException(status_code=404, detail="插件未绑定或当前不可用")
        rooms = db.scalars(select(Chatroom).where(Chatroom.id.in_(room_ids))).all()
        if len(rooms) != len(room_ids):
            raise HTTPException(status_code=404, detail="所选群聊不存在或不属于当前企业")
        channels = {int(item.id): item for item in db.scalars(select(Channel).where(Channel.id.in_({room.channel_id for room in rooms}))).all()}
        bindings = {
            int(item.bot_id): item
            for item in db.scalars(select(BotPlugin).where(BotPlugin.plugin_id == plugin_id, BotPlugin.status != "unbound")).all()
        }
        # 整批验证通过后才修改，避免遇到未绑定或跨账号的群时只保存了前半批。
        for room in rooms:
            channel = channels.get(int(room.channel_id))
            binding = bindings.get(int(room.bot_id))
            if (
                not channel or channel.deleted_at is not None
                or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES
                or channel.bot_id != room.bot_id
                or room.tenant_id != user.tenant_id or channel.tenant_id != user.tenant_id
                or room.status == "left" or binding is None
                or binding.tenant_id != user.tenant_id or room.bot_id not in option["bot_ids"]
            ):
                raise HTTPException(status_code=400, detail="所选群包含未绑定该插件、已退出或不可操作的群，请重新选择")
        for room in rooms:
            update_group_plugin_scope_enabled(
                db,
                channel=channels[int(room.channel_id)],
                chatroom=room,
                bot_plugin_id=bindings[int(room.bot_id)].id,
                enabled=mode == "enabled",
                operator_id=user.user_id,
                audit_note="群管理批量启用插件" if mode == "enabled" else "群管理批量禁用插件",
                source="group_scope_batch_toggle",
            )
        db.commit()
        record_audit(db, "chatroom.plugin-scope.batch", "plugin", plugin_id, f"mode={mode}, chatrooms={','.join(str(item) for item in sorted(room_ids))}, operator={user.username}")
        verb = "启用" if mode == "enabled" else "禁用"
        message = f"已在 {len(rooms)} 个群聊中{verb}「{option['name']}」"
        if mode == "enabled":
            message += "；群聊和机器人上的插件总开关均开启后才会运行"
        target = urlsplit(str(next or ""))
        location = f"/chatrooms/plugin-scopes?{target.query}&" if target.path == "/chatrooms/plugin-scopes" and not target.netloc and not target.scheme else f"/chatrooms/plugin-scopes?plugin_id={plugin_id}&"
        redirect_url = f"{location}selection_cleared=1&notice={quote(message)}"
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": True, "notice": message, "count": len(rooms), "redirect_url": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)
    except (HTTPException, ValueError):
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/contacts", response_class=HTMLResponse)
async def contact_directory_page(request: Request, q: str = "", channel_id: int = 0, page: int = 1):
    """好友唯一入口:所有账号的好友合并展示,可直接跳到与好友的聊天。"""

    db = get_session()
    try:
        directory = build_contact_directory(db, keyword=q, channel_id=channel_id, page=page)
        return render(
            request,
            "contact_directory.html",
            page_title="好友",
            directory=directory,
            channels=channel_options(db),
            pager_base=_directory_pager_base("/contacts", q=directory["keyword"], channel_id=channel_id),
            can_edit=has_tenant_role(require_user(request), "operator"),
            sync_channels=_webridge_directory_sync_channels(db),
            sync_action="/contacts/sync",
            sync_label="同步好友",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.post("/contacts/{contact_key}/chat")
async def start_contact_chat(request: Request, contact_key: str, csrf_token: str = Form(""), return_to: str = Form("")):
    """从好友列表发起私聊:已有会话直接复用,没有就建一条再进消息中心。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        ipad = contact_key.startswith("ipad-")
        number = contact_key.removeprefix("ipad-")
        if not number.isascii() or not number.isdigit() or len(number) > 18:
            raise HTTPException(status_code=404, detail="好友不存在")
        model = IpadFriend if ipad else WorkWechatContact
        tenant = _current_tenant_id()
        # 记录键只用于定位；行锁内核验渠道的实际协议、租户及登录账号，拒绝换绑前打开的表单。
        stmt = select(model, Channel).join(Channel, Channel.id == model.channel_id).join(Bot, Bot.id == Channel.bot_id).where(
            model.id == int(number), model.tenant_id == tenant, Channel.tenant_id == tenant,
            Bot.tenant_id == tenant, Channel.deleted_at.is_(None), Bot.deleted_at.is_(None),
        )
        if ipad:
            stmt = stmt.where(Channel.channel_type == "wechat_ipad")
        else:
            stmt = stmt.where(Channel.channel_type != "wechat_ipad", model.contact_kind == "friend",
                              model.sync_status == "active", model.deleted_at.is_(None))
        pair = db.execute(stmt.with_for_update()).one_or_none()
        if pair is None:
            raise HTTPException(status_code=404, detail="好友不存在")
        contact, channel = pair
        if ipad and (not contact.owner_wxid or contact.owner_wxid != account_identity(channel)["external_id"]):
            raise HTTPException(status_code=404, detail="好友不存在")
        if not channel.is_enabled or not channel.bot.is_enabled:
            raise HTTPException(status_code=400, detail="好友所属账号已停用")
        if not channel_supports(channel.channel_type, "direct_chat"):
            raise HTTPException(status_code=400, detail="当前渠道暂不支持从好友发起聊天")
        external_id = str(contact.contact_external_id or "").strip()
        if not external_id:
            raise HTTPException(status_code=400, detail="好友缺少有效的渠道编号")
        conversation = db.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == _current_tenant_id(),
                Conversation.bot_id == channel.bot_id,
                Conversation.channel_id == channel.id,
                Conversation.external_id == external_id,
                Conversation.chat_type == "direct",
            )
            .order_by(Conversation.id.asc())
            .limit(1).with_for_update()
        )
        if conversation is None:
            title = str(contact.display_name or contact.name or contact.contact_external_id)[:255]
            conversation = Conversation(
                tenant_id=_current_tenant_id(),
                bot_id=channel.bot_id,
                channel_id=channel.id,
                channel_type=channel.channel_type,
                external_id=external_id,
                title=title,
                chat_type="direct",
            )
            db.add(conversation)
            db.flush()
            record_audit(db, "contact.start-chat", "conversation", conversation.id, f"channel={channel.id} contact={contact.id}")
            db.commit()
        return RedirectResponse(url=build_url("/messages", conversation_id=conversation.id, channel_id=channel.id, return_to=safe_return_to(return_to, "")), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/friends/{friend_id}/remark")
def edit_ipad_friend_remark(request: Request, channel_id: int, friend_id: int, expected: str = Form(""),
                          remark: str = Form(""), clear: bool = Form(False), csrf_token: str = Form(""),
                          return_to: str = Form("")):
    from app.services.wechat_ipad.operations.friend_remark import change

    user = enforce_role(request, "ops")
    with get_session() as db:
        require_tenant_csrf(db, request, _csrf_from_header(request) or csrf_token)
        # 与目录同步按相同顺序锁住渠道、好友，避免换绑和并发提交改变已验证的目标。
        channel = db.scalar(select(Channel).join(Bot).where(
            Channel.id == channel_id, Channel.tenant_id == user.tenant_id, Channel.channel_type == "wechat_ipad",
            Channel.deleted_at.is_(None), Channel.is_enabled.is_(True), Bot.tenant_id == user.tenant_id,
            Bot.deleted_at.is_(None), Bot.is_enabled.is_(True)).with_for_update())
        owner = account_identity(channel)["external_id"] if channel and channel.api_base else ""
        friend = db.scalar(select(IpadFriend).where(IpadFriend.id == friend_id, IpadFriend.channel_id == channel_id,
                           IpadFriend.tenant_id == user.tenant_id, IpadFriend.owner_wxid == owner).with_for_update()) if owner else None
        if friend is None:
            raise HTTPException(status_code=404, detail="未找到当前账号下的好友")
        ok, summary = change(channel, friend, expected=expected, remark=remark, clear=clear)
        record_audit(db, "channel.wechat-ipad.friend-remark", "ipad_friend", friend.id, summary, actor=user.username)
        return _completed_operation_response(request, summary, safe_return_to(return_to, "/contacts"), ok=ok)
