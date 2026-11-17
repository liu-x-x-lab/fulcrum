"""渠道配置、健康检查、同步及主动发送的操作入口。"""

from urllib.parse import quote
import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select

from app.core.tenant_context import require_tenant_id
from app.models.entities import Conversation
from app.models.saas import BotBinding
from app.schemas.forms import ChannelUpdate
from app.services.channel_adapters import get_channel_adapter
from app.services.channel_catalog import (
    ChannelConfigurationError,
    get_channel_type_definition,
    require_supported_channel_type,
    validate_enabled_channel_configuration,
)
from app.services.channel_deletion import ChannelDeletionError, delete_channel_account
from app.services.crud import get_channel, toggle_channel, update_channel
from app.services.operation_feedback import OperationFeedbackError, build_operation_status
from app.services.ops import create_alert, record_audit, record_health_check
from app.services.provider_nodes import ProviderNodeError, validate_channel_executor_binding
from app.services.runtime import run_channel_health_check
from app.services.saas_auth import has_tenant_role
from app.services.task_queue import enqueue_job
from app.services.wechat_ipad import (
    pull_wechat_ipad_messages,
    run_wechat_ipad_action,
    sync_wechat_ipad_chatrooms,
    sync_wechat_ipad_contacts,
)
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type
from app.web.auth_common import require_tenant_csrf
from app.web.channels.definitions import BUILTIN_CHANNEL_TYPES
from app.web.shared.access import enforce_role, get_session
from app.web.shared.forms import merge_channel_adapter_config
from app.web.shared.operations import _completed_operation_response, _require_operation_csrf


router = APIRouter()


@router.get("/channels/{channel_id}/operations/status")
async def channel_operation_status(request: Request, channel_id: int, job_ids: str = ""):
    enforce_role(request, "ops")
    parts = job_ids.split(",")
    if len(parts) > 200 or any(not part.isascii() or not part.isdigit() or len(part) > 18 or int(part) < 1 for part in parts):
        raise HTTPException(status_code=400, detail="操作任务编号无效")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="未找到账号")
        return JSONResponse(build_operation_status(db, channel, list(dict.fromkeys(int(part) for part in parts))), headers={"Cache-Control": "no-store"})
    except OperationFeedbackError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    finally:
        db.close()


@router.post("/channels/{channel_id}/edit")
async def edit_channel_action(
    request: Request,
    channel_id: int,
    name: str = Form(...),
    channel_type: str = Form(...),
    api_base: str = Form(""),
    bot_token: str = Form(""),
    webhook_path: str | None = Form(None),
    dm_policy: str = Form("allowlist"),
    allow_from: str = Form(""),
    adapter_config_json: str | None = Form(None),
    csrf_token: str = Form(""),
):
    principal = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in BUILTIN_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            normalized_channel_type = require_supported_channel_type(channel_type)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not has_tenant_role(principal, "tenant_admin"):
            raise HTTPException(status_code=403, detail="Permission denied")
        require_tenant_csrf(db, request, csrf_token)
        if normalized_channel_type != channel.channel_type:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote('渠道类型不可修改，请新建渠道后停用旧渠道')}&tone=danger",
                status_code=303,
            )
        channel_definition = get_channel_type_definition(channel.channel_type)
        # 平台托管渠道的地址只能由平台后台同步。租户即使手工构造表单字段，保存时也必须保留服务端现值。
        resolved_api_base = channel.api_base if channel_definition.platform_managed_endpoint or channel.channel_type == "wechat_webridge" else api_base
        # 普通租户页面不回显原始适配 JSON；没有提交的高级字段保留服务端现值，
        # 但 merge_channel_adapter_config 会明确删除 iPad 已废弃的长期代理节点。
        adapter_config_source = channel.adapter_config_json if adapter_config_json is None else adapter_config_json
        resolved_webhook_path = channel.webhook_path if webhook_path is None else webhook_path
        merged_adapter_config_json, adapter_error = merge_channel_adapter_config(
            channel_type=channel.channel_type,
            adapter_config_json=adapter_config_source or "",
        )
        if adapter_error:
            return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(adapter_error)}", status_code=303)
        try:
            # 机器人、外部账号和执行器共同构成商业绑定身份。高级设置只允许
            # 调整运行配置，任何额外表单字段都不能把渠道改挂到另一账号或机器人。
            provider_node, provider_account = validate_channel_executor_binding(
                db,
                channel_type=channel.channel_type,
                provider_node_id=channel.provider_node_id,
                provider_account_id=channel.provider_account_id,
                channel_id=channel.id,
            )
        except ProviderNodeError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote(f'执行端绑定失败：{exc}')}&tone=danger",
                status_code=303,
            )
        try:
            validate_enabled_channel_configuration(
                channel.channel_type,
                is_enabled=channel.is_enabled,
                field_values={
                    "api_base": resolved_api_base,
                    "bot_token": bot_token,
                    "webhook_path": resolved_webhook_path,
                    "dm_policy": dm_policy,
                    "allow_from": allow_from,
                    "adapter_config_json": merged_adapter_config_json,
                    "provider_node_id": provider_node.id if provider_node else None,
                    "provider_account_id": provider_account.id if provider_account else None,
                },
                configured_secret_fields={"bot_token"} if channel.bot_token_encrypted else (),
            )
        except ChannelConfigurationError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}?notice={quote(str(exc))}&tone=danger",
                status_code=303,
            )
        payload = ChannelUpdate(
            bot_id=channel.bot_id,
            name=name,
            channel_type=channel.channel_type,
            api_base=resolved_api_base,
            bot_token=bot_token,
            webhook_path=resolved_webhook_path,
            dm_policy=dm_policy,
            allow_from=allow_from,
            adapter_config_json=merged_adapter_config_json,
            provider_node_id=provider_node.id if provider_node else None,
            provider_account_id=provider_account.id if provider_account else None,
            is_enabled=channel.is_enabled,
        )
        update_channel(db, channel, payload)
        record_audit(db, "channel.update", "channel", channel.id, f"更新渠道 {channel.name}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/toggle")
async def toggle_channel_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if not channel.is_enabled:
            # 启用动作必须关联当前租户的有效商业绑定；仅有完整协议配置的草稿
            # 仍不能进入调度，避免绕过可信身份验证、设备限制和机器人额度占用。
            binding = db.scalar(
                select(BotBinding).where(
                    BotBinding.tenant_id == require_tenant_id(),
                    BotBinding.bot_id == channel.bot_id,
                    BotBinding.channel_id == channel.id,
                    BotBinding.adapter_type == channel.channel_type,
                    BotBinding.status.in_(("active", "disabled")),
                )
            )
            if binding is None:
                if channel.channel_type == "wechat_webridge":
                    return RedirectResponse(url=f"/channels/connect?channel_id={channel_id}", status_code=303)
                return RedirectResponse(
                    url=f"/channels/{channel_id}?notice={quote('请先把系统生成的地址粘贴到手机微桥插件，完成首次注册')}&tone=danger",
                    status_code=303,
                )
            try:
                validate_channel_executor_binding(
                    db,
                    channel_type=channel.channel_type,
                    provider_node_id=channel.provider_node_id,
                    provider_account_id=channel.provider_account_id,
                    channel_id=channel.id,
                )
                validate_enabled_channel_configuration(
                    channel.channel_type,
                    is_enabled=True,
                    field_values={
                        "api_base": channel.api_base,
                        "bot_token": None,
                        "webhook_path": channel.webhook_path,
                        "dm_policy": channel.dm_policy,
                        "allow_from": channel.allow_from,
                        "adapter_config_json": channel.adapter_config_json,
                        "provider_node_id": channel.provider_node_id,
                        "provider_account_id": channel.provider_account_id,
                    },
                    configured_secret_fields={"bot_token"} if channel.bot_token_encrypted else (),
                )
            except (ChannelConfigurationError, ProviderNodeError) as exc:
                return RedirectResponse(
                    url=f"/channels/{channel_id}?notice={quote(str(exc))}&tone=danger",
                    status_code=303,
                )
        toggle_channel(db, channel)
        record_audit(db, "channel.toggle", "channel", channel.id, f"enabled={channel.is_enabled}")
        return RedirectResponse(url=f"/channels/{channel_id}", status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/delete")
async def delete_channel_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    """删除未启用的账号(逻辑删除):从清单隐藏并释放额度,聊天数据全部保留。"""

    principal = enforce_role(request, "ops")
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        name = channel.name
        try:
            released = delete_channel_account(db, channel)
        except ChannelDeletionError as exc:
            return RedirectResponse(url=f"/channels?notice={quote(str(exc))}&tone=danger", status_code=303)
        db.commit()
        record_audit(db, "channel.delete", "channel", channel_id, f"逻辑删除账号 {name},释放额度 {released} 个")
        return RedirectResponse(url=f"/channels?notice={quote('账号已删除，占用的额度已释放')}", status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/health-check")
async def check_channel_health_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        adapter = get_channel_adapter(channel)
        adapter_result = adapter.health_check(channel)
        adapter_status = str(adapter_result.get("status") or "").strip().lower()
        if adapter_result.get("ok"):
            status = adapter_status or "online"
            message = str(adapter_result.get("summary") or "渠道健康检查通过")
            channel.status = status
            db.commit()
        elif adapter_status:
            status = adapter_status
            message = str(adapter_result.get("summary") or "渠道健康检查失败")
            channel.status = status
            db.commit()
        else:
            status, fallback_message = run_channel_health_check(db, channel)
            message = str(adapter_result.get("summary") or fallback_message)
        record_health_check(
            db,
            "health-check",
            "channel",
            status,
            message,
            target_id=channel.id,
            details={
                "channel": channel.name,
                "type": channel.channel_type,
                "adapter_type": adapter_result.get("adapter_type"),
                "adapter_details": adapter_result.get("details"),
            },
        )
        if status != "online":
            create_alert(db, "channel-health", f"渠道 {channel.name} 健康异常", message, related_type="channel", related_id=channel.id)
        record_audit(db, "channel.health-check", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/channels/{channel_id}", ok=status == "online")
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/pull-messages")
async def pull_wechat_ipad_messages_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = pull_wechat_ipad_messages(db, channel)
        message = str(overview.get("last_sync_summary") or "已执行微信 iPad 消息拉取")
        record_audit(db, "channel.wechat-ipad.pull-messages", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/channels/{channel_id}", ok=not bool(overview.get("runtime_info", {}).get("last_error")))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/sync-chatrooms")
async def sync_wechat_ipad_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = sync_wechat_ipad_chatrooms(db, channel)
        message = str(overview.get("last_status_summary") or "已触发群资料同步")
        record_audit(db, "channel.wechat-ipad.sync-chatrooms", "channel", channel.id, message)
        runtime = overview.get("runtime_info", {})
        return _completed_operation_response(request, message, f"/channels/{channel_id}", ok=not bool(runtime.get("last_error") or runtime.get("last_chatroom_sync_failed_targets")))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/sync-contacts")
async def sync_wechat_ipad_contacts_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        overview = sync_wechat_ipad_contacts(db, channel)
        message = str(overview.get("last_contact_sync_summary") or overview.get("last_status_summary") or "已执行联系人同步")
        record_audit(db, "channel.wechat-ipad.sync-contacts", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/channels/{channel_id}", ok=not bool(overview.get("runtime_info", {}).get("last_error")))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-gugatool/sync-friends")
async def sync_wechat_gugatool_friends_action(request: Request, channel_id: int, csrf_token: str = Form("")):
    """下发好友同步命令,由手机领取后回传通讯录。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_gugatool":
            raise HTTPException(status_code=404, detail="GugaTool channel not found")
        from app.services.guga_agent.service import enqueue_command

        enqueue_command(
            db,
            channel,
            command_name="get_friend_list",
            params={},
            priority=45,
            purpose="friend_sync",
        )
        db.commit()
        message = "已下发好友同步命令，手机在线时会自动回传。"
        record_audit(db, "channel.wechat-gugatool.sync-friends", "channel", channel.id, message)
        return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-ipad/actions/{action}")
async def run_wechat_ipad_action_api(request: Request, channel_id: int, action: str):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_ipad":
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        overview = run_wechat_ipad_action(db, channel, action=action, params=payload)
        db.refresh(channel)
        runtime_info = json.loads(channel.runtime_info_json or "{}")
        record_audit(
            db,
            f"channel.wechat-ipad.action.{action}",
            "channel",
            channel.id,
            str(runtime_info.get("last_action_summary") or overview.get("last_status_summary") or action),
        )
        return JSONResponse(
            {
                "ok": True,
                "action": action,
                "summary": runtime_info.get("last_action_summary") or overview.get("last_status_summary"),
                "preview": runtime_info.get("last_action_result_preview") or {},
                "runtime_info": {
                    "last_action_name": runtime_info.get("last_action_name"),
                    "last_action_at": runtime_info.get("last_action_at"),
                    "last_action_ok": runtime_info.get("last_action_ok"),
                },
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        db.close()


@router.post("/channels/{channel_id}/outbound")
async def outbound_message_action(channel_id: int, request: Request):
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        payload = await request.json()
        conversation = None
        conversation_id = payload.get("conversation_id")
        if conversation_id:
            conversation = db.get(Conversation, int(conversation_id))
            if conversation and (conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id):
                raise HTTPException(status_code=400, detail="conversation does not belong to channel")

        if not conversation:
            external_id = str(payload.get("external_id") or payload.get("receiver_id") or f"manual-{channel.id}")
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.bot_id == channel.bot_id,
                    Conversation.channel_id == channel.id,
                    Conversation.external_id == external_id,
                )
            )
            if not conversation:
                conversation = Conversation(
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    channel_type=channel.channel_type,
                    external_id=external_id,
                    title=str(payload.get("title") or payload.get("receiver_name") or external_id),
                    chat_type=str(payload.get("chat_type") or "direct"),
                )
                db.add(conversation)
                db.flush()

        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": payload,
            },
            priority=90,
            related_type="conversation",
            related_id=conversation.id,
        )
        notify_cloud_job_enqueued(channel, job)
        db.commit()
        record_audit(db, "message.outbound-queued", "task_job", job.id, f"channel={channel.id}, conversation={conversation.id}")
        return {
            "ok": True,
            "queued": True,
            "job_id": job.id,
            "conversation_id": conversation.id,
            "adapter": {"provider": "queued", "transport_status": "queued", "status": "pending"},
        }
    finally:
        db.close()
