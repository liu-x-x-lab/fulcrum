"""自动重连策略：检查当前登录流程与冷却时间后执行恢复。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import (
    _parse_datetime_like,
    _parse_json,
    _resolve_wxid,
    _utcnow_iso,
)
from app.services.wechat_ipad.constants import WECHAT_IPAD_AUTO_RECOVER_COOLDOWN_SECONDS
from app.services.wechat_ipad.operations.login import reconnect_wechat_ipad
from app.services.wechat_ipad.transport import logger


def auto_recover_wechat_ipad_login(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    now = now_shanghai()
    last_attempt_at = _parse_datetime_like(runtime_info.get("last_auto_recover_at"))
    qrcode_status = str(getattr(channel, "qrcode_status", "") or runtime_info.get("qrcode_status") or "").strip()
    login_state = str(getattr(channel, "login_state", "") or "").strip()

    if not wxid:
        return {"ok": False, "skipped": True, "reason": "missing_wxid"}
    if not force and qrcode_status in {"waiting_scan", "scanned", "confirmed"}:
        return {"ok": False, "skipped": True, "reason": "qr_flow_active"}
    if not force and login_state in {"online", "reconnecting"}:
        return {"ok": True, "skipped": True, "reason": "state_not_recoverable"}
    if last_attempt_at and not force and (now - last_attempt_at).total_seconds() < WECHAT_IPAD_AUTO_RECOVER_COOLDOWN_SECONDS:
        return {"ok": False, "skipped": True, "reason": "cooldown"}

    logger.info(
        "wechat ipad auto recover started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "login_state": login_state,
            "qrcode_status": qrcode_status,
        },
    )
    runtime_info["last_auto_recover_at"] = _utcnow_iso()
    state = reconnect_wechat_ipad(db, channel, client=api_client)
    logger.info(
        "wechat ipad auto recover completed",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "reconnect_status": state.get("reconnect_status") if isinstance(state, dict) else "",
            "login_state": state.get("login_state") if isinstance(state, dict) else "",
        },
    )
    return {"ok": True, "skipped": False, "reason": "reconnect_triggered", "state": state}
