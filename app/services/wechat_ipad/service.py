"""通用 iPad 协议动作的编排入口。

登录、心跳和同步由 operations 负责；本模块只分派动作和汇总结果。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.wechat_ipad.action_results import (
    _build_wechat_ipad_action_preview,
    _build_wechat_ipad_action_summary,
    _detect_wechat_ipad_action_missing_field,
)
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid, _utcnow_iso
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call


def run_wechat_ipad_action(
    db: Session,
    channel: Channel,
    *,
    action: str,
    params: dict[str, Any] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    payload = params or {}
    action_key = str(action or "").strip().lower()

    action_specs: dict[str, dict[str, Any]] = {
        "get_contact_list": {
            "func": api_client.get_contact_list,
            "kwargs": {
                "wxid": wxid,
                "current_wxcontact_seq": int(payload.get("current_wxcontact_seq") or 0),
                "current_chatroom_contact_seq": int(payload.get("current_chatroom_contact_seq") or 0),
            },
            "stage": "contact_list",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_get_list,
            "requires_wxid": True,
        },
        "get_contact_detail": {
            "func": api_client.get_contact_detail,
            "kwargs": {
                "wxid": wxid,
                "to_wxids": payload.get("to_wxids") or payload.get("wxids") or [],
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "contact_detail",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_get_detail,
            "requires_wxid": True,
        },
        "get_profile": {
            "func": api_client.get_profile,
            "kwargs": {
                "wxid": wxid,
                "target_wxid": str(payload.get("target_wxid") or payload.get("wxid") or wxid),
            },
            "stage": "user_profile",
            "endpoint": IPAD_874_CONTRACT.endpoints.user_get_profile,
            "requires_wxid": True,
        },
        "get_my_qrcode": {
            "func": api_client.get_my_qrcode,
            "kwargs": {
                "wxid": wxid,
                "style": int(payload.get("style") or 0),
            },
            "stage": "my_qrcode",
            "endpoint": IPAD_874_CONTRACT.endpoints.user_get_qrcode,
            "requires_wxid": True,
        },
        "get_chatroom_qrcode": {
            "func": api_client.get_chatroom_qrcode,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "chatroom_qrcode",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_get_qrcode,
            "requires_wxid": True,
        },
        "get_chatroom_announce": {
            "func": api_client.get_chatroom_announce,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
            },
            "stage": "chatroom_announce",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_get_info_detail,
            "requires_wxid": True,
        },
        "invite_chatroom_member": {
            "func": api_client.invite_chatroom_member,
            "kwargs": {
                "wxid": wxid,
                "chatroom_id": str(payload.get("chatroom_id") or ""),
                "to_wxids": payload.get("to_wxids") or payload.get("wxids") or [],
            },
            "stage": "chatroom_invite",
            "endpoint": IPAD_874_CONTRACT.endpoints.group_invite_member,
            "requires_wxid": True,
        },
        "accept_friend": {
            "func": api_client.accept_friend,
            "kwargs": {
                "wxid": wxid,
                "scene": int(payload.get("scene") or 0),
                "v1": str(payload.get("v1") or ""),
                "v2": str(payload.get("v2") or ""),
            },
            "stage": "accept_friend",
            "endpoint": IPAD_874_CONTRACT.endpoints.friend_pass_verify,
            "requires_wxid": True,
        },
        "download_image": {
            "func": api_client.download_image,
            "kwargs": {
                "wxid": wxid,
                "file_aes_key": str(payload.get("file_aes_key") or payload.get("aeskey") or ""),
                "file_no": str(payload.get("file_no") or payload.get("cdnmidimgurl") or ""),
            },
            "stage": "download_image",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_image,
            "requires_wxid": True,
        },
        "download_voice": {
            "func": api_client.download_voice,
            "kwargs": {
                "wxid": wxid,
                "msg_id": str(payload.get("msg_id") or ""),
                "from_user_name": str(payload.get("from_user_name") or ""),
                "bufid": str(payload.get("bufid") or ""),
                "length": int(payload.get("length") or 0),
            },
            "stage": "download_voice",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_voice,
            "requires_wxid": True,
        },
        "download_video": {
            "func": api_client.download_video,
            "kwargs": {
                "wxid": wxid,
                "msg_id": str(payload.get("msg_id") or ""),
                "to_wxid": str(payload.get("to_wxid") or ""),
                "data_len": int(payload.get("data_len") or 0),
                "start_pos": int(payload.get("start_pos") or 0),
                "section_length": int(payload.get("section_length") or payload.get("data_len") or 0),
                "compress_type": int(payload.get("compress_type") or 0),
            },
            "stage": "download_video",
            "endpoint": IPAD_874_CONTRACT.endpoints.tools_download_video,
            "requires_wxid": True,
        },
    }

    spec = action_specs.get(action_key)
    if not spec:
        raise ValueError(f"unsupported wechat_ipad action: {action}")
    if spec["requires_wxid"] and not wxid:
        return _persist_runtime(
            db,
            channel,
            summary=f"wechat_ipad 动作 {action_key} 被阻塞：缺少 wxid。",
            error="missing_wxid",
            extra={
                "last_action_name": action_key,
                "last_action_ok": False,
                "last_action_params": payload,
            },
        )

    missing_field = _detect_wechat_ipad_action_missing_field(action_key, spec["kwargs"])
    if missing_field:
        raise ValueError(f"{action_key} missing required field: {missing_field}")

    result = _safe_call(
        spec["func"],
        stage=spec["stage"],
        trace_endpoint=str(spec["endpoint"]),
        channel=channel,
        **spec["kwargs"],
    )
    action_summary = _build_wechat_ipad_action_summary(action_key, result)
    extra = {
        "last_action_name": action_key,
        "last_action_at": _utcnow_iso(),
        "last_action_ok": bool(result["ok"]),
        "last_action_params": payload,
        "last_action_response": result.get("payload"),
        "last_action_summary": action_summary,
        "last_action_result_preview": _build_wechat_ipad_action_preview(result.get("payload")),
    }
    return _persist_runtime(
        db,
        channel,
        summary=action_summary,
        error=None if result["ok"] else result["error"],
        error_context=None if result["ok"] else result.get("error_context"),
        extra=extra,
    )
