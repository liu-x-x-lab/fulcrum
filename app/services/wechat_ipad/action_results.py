"""通用协议动作的缺失字段判断和结果摘要。"""

from __future__ import annotations

from typing import Any

from app.services.wechat_ipad.client import WechatIpadApiClient


def _detect_wechat_ipad_action_missing_field(action: str, kwargs: dict[str, Any]) -> str | None:
    required_fields = {
        "get_contact_detail": ("to_wxids",),
        "get_chatroom_qrcode": ("chatroom_id",),
        "get_chatroom_announce": ("chatroom_id",),
        "invite_chatroom_member": ("chatroom_id", "to_wxids"),
        "accept_friend": ("scene", "v1", "v2"),
        "download_image": ("file_aes_key", "file_no"),
        "download_voice": ("msg_id", "from_user_name", "bufid", "length"),
        "download_video": ("msg_id", "to_wxid", "data_len", "section_length"),
    }
    for field in required_fields.get(action, ()):
        value = kwargs.get(field)
        if isinstance(value, list):
            if value:
                continue
        elif value not in (None, "", 0):
            continue
        return field
    return None


def _build_wechat_ipad_action_summary(action: str, result: dict[str, Any]) -> str:
    if result.get("ok"):
        return f"wechat_ipad 动作 {action} 调用成功。"
    message = str(result.get("error") or result.get("message") or "provider_error")
    return f"wechat_ipad 动作 {action} 调用失败：{message}"


def _build_wechat_ipad_action_preview(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    preview: dict[str, Any] = {}
    for key in ("Success", "Code", "Message", "Msg", "Ret", "http_status"):
        if key in payload:
            preview[key] = payload.get(key)
    data = WechatIpadApiClient.extract_data(payload)
    if data:
        preview["data_keys"] = sorted(data.keys())[:10]
    return preview
