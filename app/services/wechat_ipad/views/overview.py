"""渠道概览与动作面板的展示组装，依赖纯诊断结果而不执行协议操作。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _format_beijing_time, _parse_json
from app.services.wechat_ipad.constants import (
    WECHAT_IPAD_LOGIN_PROFILE_KEYS,
    WECHAT_IPAD_LOGIN_PROFILE_LABELS,
)
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.diagnostics.errors import (
    _build_error_context_view,
    _build_error_diagnostics,
    _build_error_operator_snapshot,
    _build_error_operator_summary,
)
from app.services.wechat_ipad.diagnostics.messages import _build_recent_message_flow_summary
from app.services.wechat_ipad.diagnostics.protocol import (
    _build_recent_call_view,
    _build_recent_protocol_operator_snapshot,
)
from app.services.wechat_ipad.diagnostics.sync import _build_sync_diagnostics
from app.services.wechat_ipad.login_profiles import (
    _login_init_endpoint_for_profile,
    _resolve_login_profile_key,
    _resolve_login_profiles,
)
from app.services.wechat_ipad.qr import (
    _build_qrcode_expiry_view,
    _derive_display_login_state,
    _derive_display_qrcode_status,
    _is_polling_active,
    _resolve_qrcode_image,
    _resolve_qrcode_link_url,
)
from app.services.wechat_ipad.views.account import _build_account_snapshot, _build_user_profile_view
from app.services.wechat_ipad.views.group_governance import (
    _build_group_allow_deny_model_snapshot,
    _build_group_enablement_model_snapshot,
    _build_group_plugin_governance_snapshot,
)
from app.services.wechat_ipad.views.group_scopes import (
    _build_chatroom_plugin_scope_brief,
    _build_chatroom_plugin_scope_overview,
)
from app.services.wechat_ipad.views.operations import (
    _build_current_ops_snapshot,
    _build_login_operator_snapshot,
)
from app.services.wechat_ipad.views.priority import _build_top_priority_snapshot


def build_wechat_ipad_overview(
    channel: Channel,
    *,
    now: datetime | None = None,
    group_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    adapter_config = _parse_json(channel.adapter_config_json)
    account_snapshot = _build_account_snapshot(runtime_info, adapter_config)
    reference_time = now or now_shanghai()
    expiry = _build_qrcode_expiry_view(runtime_info, now=reference_time)
    last_check_qr_payload = runtime_info.get("last_check_qr_response") if isinstance(runtime_info.get("last_check_qr_response"), dict) else {}
    detailed_last_error = (
        WechatIpadApiClient.extract_detail_message(last_check_qr_payload)
        or WechatIpadApiClient.extract_message(last_check_qr_payload)
        or runtime_info.get("last_error")
    )
    login_profiles = _resolve_login_profiles(adapter_config)
    selected_login_profile = _resolve_login_profile_key(
        runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    selected_profile_config = login_profiles.get(selected_login_profile, {})
    qrcode_status = getattr(channel, "qrcode_status", "idle") or "idle"
    login_state = getattr(channel, "login_state", "unknown") or "unknown"
    qrcode_status_display = _derive_display_qrcode_status(qrcode_status, expiry["is_expired"])
    login_state_display = _derive_display_login_state(login_state)
    error_context = _build_error_context_view(runtime_info.get("last_error_context"), now=reference_time)
    message_stats = {
        "last_sync_count": runtime_info.get("last_message_sync_count", 0),
        "last_sync_ingested": runtime_info.get("last_message_sync_ingested", 0),
        "last_sync_deduplicated": runtime_info.get("last_message_sync_deduplicated", 0),
        "receive_total": runtime_info.get("receive_total", 0),
        "dedupe_total": runtime_info.get("dedupe_total", 0),
    }
    protocol_state = {
        "qr_uuid": runtime_info.get("login_qr_uuid"),
        "qr_message": runtime_info.get("login_qr_message"),
        "last_qrcode_at": runtime_info.get("last_qrcode_at"),
        "last_qrcode_at_display": _format_beijing_time(runtime_info.get("last_qrcode_at")),
        "last_get_qr_result": _build_recent_call_view(
            runtime_info.get("last_login_init_response"),
            fallback_message=runtime_info.get("login_qr_message"),
            called_at=runtime_info.get("last_qrcode_at"),
            now=reference_time,
        ),
        "last_check_qr_at": runtime_info.get("last_check_qr_at"),
        "last_check_qr_at_display": _format_beijing_time(runtime_info.get("last_check_qr_at")),
        "last_heartbeat_status": runtime_info.get("heartbeat_status", "unknown"),
        "last_heartbeat_long_status": runtime_info.get("heartbeat_long_status", "unknown"),
        "last_reconnect_method": runtime_info.get("reconnect_method"),
        "sync_key": runtime_info.get("sync_key") or "",
        "last_check_qr_result": _build_recent_call_view(
            runtime_info.get("last_check_qr_response"),
            fallback_message=runtime_info.get("login_qr_message"),
            called_at=runtime_info.get("last_check_qr_at"),
            now=reference_time,
        ),
        "last_heartbeat_result": _build_recent_call_view(
            runtime_info.get("last_heartbeat_response"),
            called_at=getattr(channel, "last_heartbeat_at", None),
            now=reference_time,
        ),
        "last_heartbeat_long_result": _build_recent_call_view(
            runtime_info.get("last_heartbeat_long_response"),
            called_at=runtime_info.get("last_heartbeat_long_at"),
            now=reference_time,
        ),
        "last_user_profile_result": _build_user_profile_view(
            runtime_info.get("last_user_profile_response"),
            runtime_info.get("user_profile_summary"),
            called_at=runtime_info.get("last_user_profile_at"),
            now=reference_time,
        ),
    }
    protocol_state["operator_snapshot"] = _build_recent_protocol_operator_snapshot(protocol_state)
    login_operator_snapshot = _build_login_operator_snapshot(
        login_state=login_state,
        qrcode_status=qrcode_status,
        qrcode_status_display=qrcode_status_display,
        qrcode_is_expired=expiry["is_expired"],
        qrcode_countdown_label=expiry["countdown_label"],
        qrcode_expires_at_display=expiry["expires_at_display"],
        last_login_at=getattr(channel, "last_login_at", None),
        qrcode_refresh_count=runtime_info.get("qrcode_refresh_count", 0),
        qrcode_message=runtime_info.get("login_qr_message"),
        qrcode_image_available=bool(_resolve_qrcode_image(runtime_info)),
        check_qr_result=protocol_state["last_check_qr_result"],
    )
    summaries = {
        "pull": runtime_info.get("last_pull_protocol_summary"),
        "inbound": runtime_info.get("last_inbound_protocol_summary"),
        "outbound": runtime_info.get("last_outbound_protocol_summary"),
    }
    sync_diagnostics = _build_sync_diagnostics(
        runtime_info,
        message_stats,
        summaries,
        last_message_sync_at=getattr(channel, "last_message_sync_at", None),
    )
    recent_message_flow = _build_recent_message_flow_summary(runtime_info, now=reference_time)
    error_diagnostics = _build_error_diagnostics(detailed_last_error, runtime_info.get("last_error_context"))
    error_operator_summary = _build_error_operator_summary(
        detailed_last_error,
        runtime_info.get("last_error_context"),
        now=reference_time,
    )
    error_operator_snapshot = _build_error_operator_snapshot(
        detailed_last_error,
        runtime_info.get("last_error_context"),
        error_diagnostics=error_diagnostics,
        operator_summary=error_operator_summary,
        now=reference_time,
    )
    plugin_scope_snapshot = (group_summary or {}).get("plugin_scope_snapshot") if isinstance(group_summary, dict) else None
    plugin_scope_brief = (
        ((group_summary or {}).get("plugin_scope_brief") if isinstance(group_summary, dict) else None)
        or _build_chatroom_plugin_scope_brief(plugin_scope_snapshot=plugin_scope_snapshot)
    )
    plugin_scope_overview = (
        ((group_summary or {}).get("plugin_scope_overview") if isinstance(group_summary, dict) else None)
        or _build_chatroom_plugin_scope_overview(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    group_plugin_governance_snapshot = (
        ((group_summary or {}).get("group_plugin_governance_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_plugin_governance_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
            plugin_scope_overview=plugin_scope_overview,
        )
    )
    group_enablement_model_snapshot = (
        ((group_summary or {}).get("group_enablement_model_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_enablement_model_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    group_allow_deny_model_snapshot = (
        ((group_summary or {}).get("group_allow_deny_model_snapshot") if isinstance(group_summary, dict) else None)
        or _build_group_allow_deny_model_snapshot(
            plugin_scope_snapshot=plugin_scope_snapshot,
            plugin_scope_brief=plugin_scope_brief,
        )
    )
    current_ops_snapshot = _build_current_ops_snapshot(
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_state["operator_snapshot"],
        sync_operator_snapshot=sync_diagnostics["operator_snapshot"],
        message_operator_snapshot=recent_message_flow["operator_snapshot"],
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=(group_summary or {}).get("stage_snapshot") if isinstance(group_summary, dict) else None,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    top_priority_snapshot = _build_top_priority_snapshot(
        current_ops_snapshot=current_ops_snapshot,
        login_operator_snapshot=login_operator_snapshot,
        protocol_operator_snapshot=protocol_state["operator_snapshot"],
        sync_operator_snapshot=sync_diagnostics["operator_snapshot"],
        message_operator_snapshot=recent_message_flow["operator_snapshot"],
        error_operator_snapshot=error_operator_snapshot,
        error_context=error_context,
        group_stage_snapshot=(group_summary or {}).get("stage_snapshot") if isinstance(group_summary, dict) else None,
        group_plugin_governance_snapshot=group_plugin_governance_snapshot,
    )
    action_console = _build_wechat_ipad_action_console(channel, runtime_info)
    return {
        "login_state": login_state,
        "login_state_display": login_state_display,
        "qrcode_status": qrcode_status,
        "qrcode_status_display": qrcode_status_display,
        "last_login_at": getattr(channel, "last_login_at", None),
        "last_login_at_display": _format_beijing_time(getattr(channel, "last_login_at", None)),
        "last_heartbeat_at": getattr(channel, "last_heartbeat_at", None),
        "last_heartbeat_at_display": _format_beijing_time(getattr(channel, "last_heartbeat_at", None)),
        "last_message_sync_at": getattr(channel, "last_message_sync_at", None),
        "last_message_sync_at_display": _format_beijing_time(getattr(channel, "last_message_sync_at", None)),
        "last_receive_at": runtime_info.get("last_receive_at"),
        "last_receive_at_display": _format_beijing_time(runtime_info.get("last_receive_at")),
        "last_send_at": runtime_info.get("last_send_at"),
        "last_send_at_display": _format_beijing_time(runtime_info.get("last_send_at")),
        "wxid": account_snapshot["wxid"],
        "device_id": str(adapter_config.get("device_id") or "").strip() or "-",
        "device_name": account_snapshot["device_name"],
        "selected_login_profile": selected_login_profile,
        "selected_login_profile_display": WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(selected_login_profile, selected_login_profile),
        "available_login_profiles": [
            {
                "key": key,
                "label": WECHAT_IPAD_LOGIN_PROFILE_LABELS.get(key, key),
                "device_name": str((login_profiles.get(key) or {}).get("device_name") or "").strip(),
                "device_id": str((login_profiles.get(key) or {}).get("device_id") or "").strip(),
                "selected": key == selected_login_profile,
            }
            for key in WECHAT_IPAD_LOGIN_PROFILE_KEYS
        ],
        "selected_profile_device_name": str(selected_profile_config.get("device_name") or adapter_config.get("device_name") or "").strip() or "-",
        "selected_profile_device_id": str(selected_profile_config.get("device_id") or adapter_config.get("device_id") or "").strip() or "-",
        "nickname": account_snapshot["nickname"],
        "alias": account_snapshot["alias"],
        "bind_mobile": account_snapshot["bind_mobile"],
        "qrcode_url": runtime_info.get("qrcode_url"),
        "qrcode_link_url": _resolve_qrcode_link_url(runtime_info),
        "qrcode_image_url": f"/channels/{channel.id}/wechat-ipad/qrcode-image" if _resolve_qrcode_image(runtime_info) else None,
        "qrcode_visible": login_state != "online" and qrcode_status != "success",
        "qrcode_uuid": runtime_info.get("login_qr_uuid"),
        "qrcode_message": runtime_info.get("login_qr_message"),
        "login_init_endpoint": str(
            runtime_info.get("last_login_init_endpoint")
            or _login_init_endpoint_for_profile(selected_login_profile)
        ),
        "qrcode_expires_at": expiry["expires_at"],
        "qrcode_expires_at_display": expiry["expires_at_display"],
        "qrcode_remaining_seconds": expiry["remaining_seconds"],
        "qrcode_countdown_label": expiry["countdown_label"],
        "qrcode_is_expired": expiry["is_expired"],
        "qrcode_refresh_count": runtime_info.get("qrcode_refresh_count", 0),
        "last_error": detailed_last_error,
        "last_error_context": error_context,
        "last_error_stage": error_context["stage"],
        "last_error_endpoint": error_context["endpoint"],
        "last_error_at": error_context["at"],
        "last_error_at_display": error_context["at_display"],
        "last_error_operator_summary": error_operator_summary,
        "error_diagnostics": error_diagnostics,
        "error_operator_snapshot": error_operator_snapshot,
        "plugin_scope_brief": plugin_scope_brief,
        "plugin_scope_overview": plugin_scope_overview,
        "group_plugin_governance_snapshot": group_plugin_governance_snapshot,
        "group_enablement_model_snapshot": group_enablement_model_snapshot,
        "group_allow_deny_model_snapshot": group_allow_deny_model_snapshot,
        "current_ops_snapshot": current_ops_snapshot,
        "top_priority_snapshot": top_priority_snapshot,
        "action_console": action_console,
        "last_status_summary": runtime_info.get("last_status_summary"),
        "last_sync_summary": runtime_info.get("last_sync_summary"),
        "last_chatroom_sync_summary": runtime_info.get("last_chatroom_sync_summary"),
        "last_receive_summary": runtime_info.get("last_receive_summary"),
        "last_send_summary": runtime_info.get("last_send_summary"),
        "last_receive_event_key": runtime_info.get("last_receive_event_key"),
        "last_dedupe_event_key": runtime_info.get("last_dedupe_event_key"),
        "heartbeat_status": runtime_info.get("heartbeat_status", "unknown"),
        "reconnect_status": runtime_info.get("reconnect_status", "idle"),
        "login_flow_polling_active": _is_polling_active(
            login_state=getattr(channel, "login_state", "unknown") or "unknown",
            qrcode_status=qrcode_status,
            is_expired=expiry["is_expired"],
        ),
        "message_stats": message_stats,
        "protocol_state": protocol_state,
        "login_operator_snapshot": login_operator_snapshot,
        "protocol_summaries": summaries,
        "sync_diagnostics": sync_diagnostics,
        "recent_message_flow": recent_message_flow,
        "entrypoints": {
            "message_pull": f"/channels/{channel.id}/wechat-ipad/pull-messages",
            "inbound_receive": f"/webhooks/{channel.id}/inbound",
            "outbound_send": f"/channels/{channel.id}/outbound",
        },
        "hooks": {
            # 运行概览只展示固定 874 契约，旧配置中的端点键不会再影响真实调用。
            "login_init_endpoint": IPAD_874_CONTRACT.endpoints.login_get_qr,
            "login_init_car_endpoint": IPAD_874_CONTRACT.endpoints.login_get_qr_car,
            "check_qr_endpoint": IPAD_874_CONTRACT.endpoints.login_check_qr,
            "heartbeat_endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat,
            "heartbeat_long_endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat_long,
            "user_profile_endpoint": IPAD_874_CONTRACT.endpoints.user_get_profile,
            "awaken_endpoint": IPAD_874_CONTRACT.endpoints.login_awaken,
            "twice_auto_auth_endpoint": IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
            "message_sync_endpoint": IPAD_874_CONTRACT.endpoints.message_sync,
        },
        "runtime_info": runtime_info,
        "adapter_config": adapter_config,
    }


def _build_wechat_ipad_action_console(channel: Channel, runtime_info: dict[str, Any]) -> dict[str, Any]:
    action_endpoint_template = f"/channels/{channel.id}/wechat-ipad/actions/__ACTION__"
    return {
        "endpoint_template": action_endpoint_template,
        "supported_actions": [
            {
                "key": "get_contact_list",
                "label": "同步联系人列表",
                "description": "用于拉取联系人与群联系人游标，适合先确认通讯录同步是否正常。",
                "sample_payload": {
                    "current_wxcontact_seq": 0,
                    "current_chatroom_contact_seq": 0,
                },
            },
            {
                "key": "get_contact_detail",
                "label": "查看联系人详情",
                "description": "按 wxid 批量查询联系人详情，也支持附带 chatroom_id 做群成员上下文查询。",
                "sample_payload": {
                    "to_wxids": ["wxid_demo_contact_001"],
                    "chatroom_id": "",
                },
            },
            {
                "key": "get_profile",
                "label": "查看个人资料",
                "description": "默认查询当前登录账号，也可改成目标 wxid。",
                "sample_payload": {
                    "target_wxid": "",
                },
            },
            {
                "key": "get_my_qrcode",
                "label": "获取个人二维码",
                "description": "验证账号侧二维码接口是否可用。",
                "sample_payload": {
                    "style": 0,
                },
            },
            {
                "key": "get_chatroom_qrcode",
                "label": "获取群二维码",
                "description": "用于核对群二维码接口与 chatroom_id 是否匹配。",
                "sample_payload": {
                    "chatroom_id": "123456@chatroom",
                },
            },
            {
                "key": "invite_chatroom_member",
                "label": "邀请群成员",
                "description": "运维侧邀请成员入群，需显式提供群 ID 和 wxid 列表。",
                "sample_payload": {
                    "chatroom_id": "123456@chatroom",
                    "to_wxids": ["wxid_demo_member_001"],
                },
            },
            {
                "key": "download_image",
                "label": "下载图片附件",
                "description": "按消息内 aeskey 与 file_no 拉取图片。",
                "sample_payload": {
                    "file_aes_key": "aes-key",
                    "file_no": "cdnmidimgurl",
                },
            },
            {
                "key": "download_voice",
                "label": "下载语音附件",
                "description": "按消息 ID、发送方、缓冲区标识和语音长度下载语音。",
                "sample_payload": {
                    "msg_id": "1234567890",
                    "from_user_name": "room-001@chatroom",
                    "bufid": "99887766",
                    "length": 3200,
                },
            },
            {
                "key": "download_video",
                "label": "下载视频附件",
                "description": "按消息 ID、发送方和分片范围下载视频。",
                "sample_payload": {
                    "msg_id": "1234567890",
                    "to_wxid": "wxid_sender",
                    "data_len": 65536,
                    "start_pos": 0,
                    "section_length": 32768,
                    "compress_type": 0,
                },
            },
        ],
        "last_action_name": runtime_info.get("last_action_name"),
        "last_action_at": runtime_info.get("last_action_at"),
        "last_action_ok": runtime_info.get("last_action_ok"),
        "last_action_summary": runtime_info.get("last_action_summary"),
        "last_action_params": runtime_info.get("last_action_params") or {},
        "last_action_result_preview": runtime_info.get("last_action_result_preview") or {},
    }
