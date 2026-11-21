"""渠道账号列表和详情页；连接、登录及状态操作由同目录模块负责。"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.tenant_context import require_tenant_id
from app.models.entities import Bot, Channel, Conversation, Message
from app.models.saas import BotBinding
from app.services.account_identity import account_identity
from app.services.account_presence import build_account_presence
from app.services.bot_entitlements import BotEntitlementError, get_bot_quota
from app.services.crud import get_channel
from app.services.provider_nodes import list_provider_nodes
from app.services.saas_auth import has_tenant_role
from app.services.wechat_ipad import build_wechat_ipad_overview
from app.services.account_dashboard import build_account_dashboard
from app.services.wechat_winauto_chatrooms import build_wechat_winauto_chatroom_list
from app.services.workwechat_chatrooms import build_workwechat_chatroom_list
from app.services.workwechat_cloud_poll import load_cloud_poll_config
from app.services.workwechat_contacts import build_workwechat_contact_summary
from app.web.channels.connections import (
    _account_connection_error_message,
    _channel_connection_state,
)
from app.web.channels.definitions import BUILTIN_CHANNEL_TYPES, CHANNEL_TYPE_FORM_SCHEMA_BY_CODE
from app.web.channels.views import build_channel_chatroom_summary, build_channel_health_timeline
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.presentation import render
from app.web.shared.queries import build_bot_name_map


router = APIRouter()


@router.get("/channels/{channel_id}/wechat-ipad", response_class=HTMLResponse)
def ipad_account_dashboard(request: Request, channel_id: int, tab: str = "contacts", keyword: str = "",
                           page: int = 1, conversation_id: int = 0, sender: str = ""):
    user = enforce_role(request, "readonly")
    with get_session() as db:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_ipad" or channel.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="Channel not found")
        return render(request, "wechat_ipad_dashboard.html", page_title=f"iPad 后台 · {channel.name}", channel=channel,
                      overview=build_wechat_ipad_overview(channel), can_edit=has_tenant_role(user, "operator"),
                      dashboard=build_account_dashboard(db, channel, tab=tab, keyword=keyword, page=page,
                                                        conversation_id=conversation_id, sender=sender),
                      notice=request.query_params.get("notice", ""))


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    db = get_session()
    try:
        tenant_id = require_tenant_id()
        # “账号接入”列表仍以真实 Channel 为运行对象，但默认只展示业务状态，
        # 不再把 API Base、Webhook、适配 JSON 和执行端主键平铺给租户。
        channels = db.scalars(
            select(Channel)
            .where(Channel.channel_type.in_(BUILTIN_CHANNEL_TYPES), Channel.deleted_at.is_(None))
            .options(selectinload(Channel.provider_node), selectinload(Channel.provider_account))
            .order_by(Channel.id.desc())
        ).all()
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        bindings = db.scalars(
            select(BotBinding)
            .where(BotBinding.tenant_id == tenant_id)
            .order_by(BotBinding.id.desc())
        ).all()
        binding_by_channel_id = {
            int(binding.channel_id): binding
            for binding in bindings
            if binding.channel_id is not None
        }
        account_rows = [
            {
                "channel": channel,
                "bot_name": bot_name_map.get(channel.bot_id, "机器人"),
                "binding": binding_by_channel_id.get(channel.id),
                "connection": _channel_connection_state(channel),
                "identity": account_identity(channel),
                "presence": build_account_presence(channel),
            }
            for channel in channels
        ]
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        return render(
            request,
            "channels.html",
            page_title="账号接入",
            account_rows=account_rows,
            quota=quota,
            quota_error=quota_error,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get("/channels/{channel_id}", response_class=HTMLResponse)
async def channel_detail_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in BUILTIN_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        provider_nodes = list_provider_nodes(db)
        bot_name_map = build_bot_name_map(bots)
        binding = db.scalar(select(BotBinding).where(BotBinding.channel_id == channel.id))
        connection = _channel_connection_state(channel)
        try:
            quota = get_bot_quota(db)
            quota_error = ""
        except BotEntitlementError as exc:
            quota = None
            quota_error = _account_connection_error_message(str(exc))
        # 消息列表只在消息中心里有一份:账号详情只显示总数和入口,不再自带分页表。
        message_total = int(
            db.scalar(
                select(func.count(Message.id))
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.channel_id == channel.id)
            )
            or 0
        )
        # workwechat 云端 WS/拉取模式由本地 provider 主动注册和领取任务，不需要主服务直连 provider 的 API Base。
        workwechat_cloud_poll_config = load_cloud_poll_config(channel) if channel.channel_type == "workwechat" else None
        workwechat_cloud_poll_enabled = bool(
            channel.channel_type == "workwechat"
            and workwechat_cloud_poll_config
            and str(workwechat_cloud_poll_config.get("workwechat_delivery_mode") or "").strip() == "cloud_poll"
        )
        health_checks = {
            "api_base": True if workwechat_cloud_poll_enabled else bool(channel.api_base),
            "api_base_not_required": workwechat_cloud_poll_enabled,
            "webhook_path": bool(channel.webhook_path),
            "webhook_not_required": workwechat_cloud_poll_enabled,
            "bot_token": bool(channel.bot_token_encrypted),
            "adapter_config": bool(channel.adapter_config_json),
        }
        channel_chatroom_summary = None
        if channel.channel_type == "wechat_winauto":
            # 渠道详情页只读取本地轻量群视图；主动刷新仍由页面按钮触发，避免打开详情页时阻塞。
            channel_chatroom_summary = build_channel_chatroom_summary(build_wechat_winauto_chatroom_list(db, channel))
        elif channel.channel_type == "workwechat":
            # workwechat 群摘要来自独立群服务，包含 Hook 群列表和消息观测落库后的本地样本。
            channel_chatroom_summary = build_channel_chatroom_summary(build_workwechat_chatroom_list(db, channel))
        # WeBridge 的群组信息统一放在微桥后台，账号详情页不再重复展示群聊概览卡片。
        workwechat_contact_summary = build_workwechat_contact_summary(db, channel) if channel.channel_type == "workwechat" else None
        # 企微云端拉取配置只在渠道详情页展示摘要；授权码明文不落库，生成后只通过一次性提示展示。
        return render(
            request,
            "channel_detail.html",
            page_title=f"账号详情 · {channel.name}",
            account=account_identity(channel),
            channel=channel,
            binding=binding,
            connection=connection,
            quota=quota,
            quota_error=quota_error,
            presence=build_account_presence(channel),
            bots=bots,
            bot_name_map=bot_name_map,
            builtin_channel_types=BUILTIN_CHANNEL_TYPES,
            channel_definition=CHANNEL_TYPE_FORM_SCHEMA_BY_CODE[channel.channel_type],
            channel_config_field_map={
                item["name"]: item
                for item in CHANNEL_TYPE_FORM_SCHEMA_BY_CODE[channel.channel_type]["config_fields"]
            },
            provider_nodes=provider_nodes,
            message_total=message_total,
            token_preview=channel.bot_token_masked or "-",
            health_checks=health_checks,
            channel_health_timeline=build_channel_health_timeline(db, channel.id),
            wechat_ipad_overview=build_wechat_ipad_overview(channel) if channel.channel_type == "wechat_ipad" else None,
            channel_chatroom_summary=channel_chatroom_summary,
            workwechat_contact_summary=workwechat_contact_summary,
            workwechat_cloud_poll_config=workwechat_cloud_poll_config,
            can_manage_channel=has_tenant_role(require_user(request), "tenant_admin"),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()
