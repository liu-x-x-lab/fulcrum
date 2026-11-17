"""本地联系人快照的只读列表。"""

from __future__ import annotations

from typing import Any

from app.models.entities import Channel
from app.services.wechat_ipad.common import _format_beijing_time, _parse_json


def build_wechat_ipad_contact_list(channel: Channel, *, limit: int = 100) -> dict[str, Any]:
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    snapshot = runtime_info.get("last_contact_sync_snapshot")
    if not isinstance(snapshot, dict):
        return {
            "has_data": False,
            "summary": runtime_info.get("last_contact_sync_summary") or "还没有联系人同步记录，请先执行“同步联系人”。",
            "synced_at_display": _format_beijing_time(runtime_info.get("last_contact_sync_at")),
            "personal_count": 0,
            "chatroom_count": 0,
            "total_count": 0,
            "seq": {},
            "contacts": [],
        }

    contacts = snapshot.get("contacts") if isinstance(snapshot.get("contacts"), list) else []
    personal_contacts = [item for item in contacts if isinstance(item, dict) and bool(item.get("is_personal_contact"))]
    safe_limit = max(1, min(int(limit or 100), 500))
    return {
        "has_data": True,
        "summary": runtime_info.get("last_contact_sync_summary") or "已同步联系人列表。",
        "synced_at_display": _format_beijing_time(snapshot.get("synced_at") or runtime_info.get("last_contact_sync_at")),
        "personal_count": int(snapshot.get("personal_count") or len(personal_contacts)),
        "chatroom_count": int(snapshot.get("chatroom_count") or max(len(contacts) - len(personal_contacts), 0)),
        "total_count": int(snapshot.get("total_count") or len(contacts)),
        "seq": snapshot.get("seq") if isinstance(snapshot.get("seq"), dict) else {},
        "contacts": personal_contacts[:safe_limit],
    }
