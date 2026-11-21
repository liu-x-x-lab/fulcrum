"""群设置中的最近治理记录，只读取当前租户、账号和群的台账。"""

from sqlalchemy import select

from app.core.tenant_context import require_tenant_id
from app.models.group_moderation import GroupModerationViolation

STATUS_LABELS = {
    "warning": "已记录违规", "pending": "等待移出结果", "kicked": "已移出",
    "failed": "移出失败", "cancelled": "已取消移出", "unknown": "移出结果待确认",
}


def recent_records(db, room) -> list[dict]:
    if room.tenant_id != require_tenant_id():
        raise ValueError("moderation_tenant_mismatch")
    rows = db.scalars(select(GroupModerationViolation).where(
        GroupModerationViolation.tenant_id == room.tenant_id,
        GroupModerationViolation.bot_id == room.bot_id,
        GroupModerationViolation.channel_id == room.channel_id,
        GroupModerationViolation.chatroom_id == room.id,
    ).order_by(GroupModerationViolation.id.desc()).limit(20))
    return [{"time": row.created_at, "member": row.member_external_id, "word": row.matched_word,
             "count": row.strike_count, "status": STATUS_LABELS.get(row.status, row.status),
             "note": row.result_note or ""} for row in rows]
