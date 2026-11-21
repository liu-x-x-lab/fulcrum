"""渠道通用入站接口：平台会话验签后恢复可信租户作用域处理事件。"""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel, ErrorLog
from app.services.channel_events import ingest_inbound_event
from app.services.crud import get_channel
from app.services.ops import create_alert, record_audit, record_health_check
from app.services.security import (
    build_telegram_webhook_secret,
    decrypt_secret,
    verify_telegram_webhook_secret,
    verify_webhook_signature,
)
from app.services.wechat_ipad import record_wechat_ipad_receive_event


router = APIRouter()


@router.post("/webhooks/{channel_id}/inbound")
async def inbound_webhook(channel_id: int, request: Request):
    body = await request.body()
    # URL 中的 channel_id 只能用于定位待校验凭证。平台会话完成签名预检后只保留
    # 可信 tenant_id 和渠道主键，后续事件、日志、告警及审计全部在租户会话中处理。
    with platform_session(SessionLocal) as auth_db:
        channel = get_channel(auth_db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type == "wechat_webridge":
            # 微桥必须走带 Token 和设备身份校验的专用入口，不能绕过账号确认。
            raise HTTPException(status_code=403, detail="请使用微桥专属长轮询地址")

        secret = decrypt_secret(channel.bot_token_encrypted)
        if channel.channel_type == "telegram":
            expected_secret = build_telegram_webhook_secret(secret, channel.id)
            provided_signature = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            signature_valid = verify_telegram_webhook_secret(expected_secret, provided_signature)
        else:
            provided_signature = request.headers.get("X-Signature") or request.headers.get("X-Abot-Signature")
            timestamp = request.headers.get("X-Timestamp") or request.headers.get("X-Abot-Timestamp") or ""
            signature_valid = verify_webhook_signature(secret, body, provided_signature, timestamp=timestamp)
        credential = (int(channel.tenant_id), int(channel.id))

    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        if not signature_valid:
            db.add(ErrorLog(source="webhook", level="ERROR", message=f"渠道 #{channel.id} Webhook 签名校验失败"))
            db.commit()
            create_alert(
                db,
                "webhook",
                f"渠道 {channel.name} Webhook 签名失败",
                "收到的请求签名与共享密钥不匹配",
                level="critical",
                related_type="channel",
                related_id=channel.id,
            )
            record_health_check(
                db,
                "webhook-signature",
                "channel",
                "failed",
                "Webhook 签名校验失败",
                target_id=channel.id,
                details={"header": provided_signature or "-", "channel_type": channel.channel_type},
            )
            record_audit(db, "webhook.reject", "channel", channel.id, "签名校验失败")
            return JSONResponse(status_code=401, content={"ok": False, "error": "invalid_signature"})

        payload = json.loads(body.decode("utf-8") or "{}")
        result = ingest_inbound_event(
            db,
            channel,
            payload,
            headers={key: value for key, value in request.headers.items()},
            source=channel.channel_type,
        )
        if result.get("reason") == "friend_request_requires_sync":
            return {"ok": True, "mapped": False, "ignored": True}
        normalized = result["normalized"]
        event_key = str(normalized.get("event_key") or "")
        if result["deduplicated"]:
            if channel.channel_type == "wechat_ipad":
                record_wechat_ipad_receive_event(
                    db,
                    channel,
                    event_key=event_key or None,
                    mapped=bool(result["mapped"]),
                    deduplicated=True,
                    source=channel.channel_type,
                )
            record_audit(db, "webhook.deduplicated", "raw_webhook_event", result["raw_event_id"], f"event_key={event_key}")
            return {"ok": True, "deduplicated": True, "event_id": result["raw_event_id"]}

        db.commit()
        if channel.channel_type == "wechat_ipad":
            record_wechat_ipad_receive_event(
                db,
                channel,
                event_key=event_key or None,
                mapped=bool(result["mapped"]),
                deduplicated=False,
                source=channel.channel_type,
            )
        record_health_check(
            db,
            "webhook-inbound",
            "channel",
            "ok",
            "入站 Webhook 已接收并处理",
            target_id=channel.id,
            details={
                "event_key": event_key or "-",
                "mapped": result["mapped"],
                "adapter_type": normalized.get("adapter_type"),
                "event_type": normalized.get("event_type"),
                "adapter_details": normalized.get("details"),
                "dispatch": result.get("dispatch"),
            },
        )
        record_audit(db, "webhook.accept", "raw_webhook_event", result["raw_event_id"], f"mapped={result['mapped']}")
        return {
            "ok": True,
            "event_id": result["raw_event_id"],
            "mapped": result["mapped"],
            "event_type": normalized.get("event_type"),
            "dispatch": result.get("dispatch"),
        }
