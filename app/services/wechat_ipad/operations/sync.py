"""消息拉取和群档案同步，保留去重、事务和事件分发边界。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom
from app.services.channel_events import ingest_inbound_event
from app.services.wechat_ipad.chatrooms import upsert_wechat_ipad_chatroom_profile
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.events import _collect_chatroom_sync_candidates_from_results
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call, logger
from app.services.wechat_ipad.views.account import _build_pull_summary


def pull_wechat_ipad_messages(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            summary="缺少 wxid，无法调用 Msg/Sync。",
            error="missing_wxid",
            sync_at=now_shanghai(),
            extra={"reconnect_status": "required"},
        )

    response = _safe_call(
        api_client.sync_message,
        stage="message_sync",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.message_sync,
        channel=channel,
        wxid=wxid,
        scene=int(adapter_config.get("message_sync_scene") or 0),
        synckey=str(runtime_info.get("sync_key") or ""),
    )
    if not response["ok"]:
        return _persist_runtime(
            db,
            channel,
            summary="Msg/Sync 调用失败。",
            error=response["error"],
            error_context=response.get("error_context"),
            sync_at=now_shanghai(),
            extra={
                "last_message_sync_response": response.get("payload"),
                "reconnect_status": "required",
            },
        )

    payload = response["payload"]
    sync_fields = WechatIpadApiClient.extract_sync_fields(payload)
    data = sync_fields["data"]
    messages = sync_fields["list"]
    ingested = 0
    deduplicated = 0
    ingest_results: list[dict[str, Any]] = []
    for item in messages:
        result = ingest_inbound_event(db, channel, item, source="wechat_ipad_sync")
        ingest_results.append(result)
        deduplicated += 1 if result["deduplicated"] else 0
        ingested += 1 if result["mapped"] and not result["deduplicated"] else 0

    chatroom_sync_candidates = _collect_chatroom_sync_candidates_from_results(
        db,
        channel,
        ingest_results,
    )
    chatroom_sync_summary = ""
    if chatroom_sync_candidates["chatroom_ids"]:
        sync_wechat_ipad_chatrooms(
            db,
            channel,
            chatroom_ids=chatroom_sync_candidates["chatroom_ids"],
            client=api_client,
        )
        db.refresh(channel)
        runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
        chatroom_sync_summary = str(runtime_info.get("last_chatroom_sync_summary") or "")

    state = _persist_runtime(
        db,
        channel,
        summary=f"已按 Msg/Sync 拉取 {len(messages)} 条消息，新增 {ingested} 条。",
        sync_at=now_shanghai(),
        extra={
            "last_message_sync_response": payload,
            "last_message_sync_count": len(messages),
            "last_message_sync_ingested": ingested,
            "last_message_sync_deduplicated": deduplicated,
            "last_pull_protocol_summary": _build_pull_summary(messages, sync_fields),
            "sync_key": sync_fields["sync_key"] or runtime_info.get("sync_key") or "",
            "sync_continue_flag": sync_fields["continue_flag"],
            "last_message_sync_chatroom_candidates": chatroom_sync_candidates["signals"],
            "last_message_sync_chatroom_candidate_count": len(chatroom_sync_candidates["chatroom_ids"]),
            "last_message_sync_chatroom_sync_triggered": bool(chatroom_sync_candidates["chatroom_ids"]),
            "last_message_sync_chatroom_sync_summary": chatroom_sync_summary or None,
        },
        error=None,
    )
    db.commit()
    return state


def sync_wechat_ipad_chatrooms(
    db: Session,
    channel: Channel,
    *,
    chatroom_ids: list[str] | None = None,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    logger.info(
        "wechat ipad chatroom sync started",
        extra={
            "channel_id": channel.id,
            "wxid": wxid,
            "requested_chatroom_ids": chatroom_ids or [],
        },
    )
    if not wxid:
        logger.info(
            "wechat ipad chatroom sync blocked",
            extra={"channel_id": channel.id, "reason": "missing_wxid"},
        )
        return _persist_runtime(
            db,
            channel,
            summary="缺少 wxid，无法调用群资料同步接口。",
            error="missing_wxid",
            extra={"last_chatroom_sync_summary": "群资料同步被阻塞：缺少 wxid。"},
        )

    if chatroom_ids:
        targets = [str(item or "").strip() for item in chatroom_ids if str(item or "").strip()]
    else:
        targets = [
            item.external_id
            for item in db.scalars(
                select(Chatroom)
                .where(Chatroom.channel_id == channel.id)
                .order_by(Chatroom.updated_at.desc(), Chatroom.id.desc())
                .limit(20)
            ).all()
            if str(item.external_id or "").strip()
        ]

    deduped_targets: list[str] = []
    seen: set[str] = set()
    for item in targets:
        if item in seen:
            continue
        seen.add(item)
        deduped_targets.append(item)

    if not deduped_targets:
        logger.info(
            "wechat ipad chatroom sync skipped",
            extra={"channel_id": channel.id, "reason": "no_targets"},
        )
        return _persist_runtime(
            db,
            channel,
            summary="当前没有可同步的本地群样本；请先等待群消息落库或手动补样本。",
            error=None,
            extra={"last_chatroom_sync_summary": "群资料同步未执行：没有可同步的群样本。"},
        )

    # 群资料同步固定使用 874 群接口，租户配置不能再覆盖到旧协议路由。
    info_endpoint = IPAD_874_CONTRACT.endpoints.group_get_info
    member_endpoint = IPAD_874_CONTRACT.endpoints.group_get_member_detail
    synced_profiles = 0
    synced_members = 0
    failed_targets: list[dict[str, str]] = []
    sync_started_at = now_shanghai()

    for chatroom_id in deduped_targets:
        logger.info(
            "wechat ipad chatroom sync target started",
            extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "wxid": wxid},
        )
        info_result = _safe_call(
            api_client.get_chatroom_info,
            stage="chatroom_sync",
            trace_endpoint=info_endpoint,
            channel=channel,
            wxid=wxid,
            chatroom_id=chatroom_id,
        )
        if not info_result["ok"]:
            logger.warning(
                "wechat ipad chatroom profile sync failed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "error": str(info_result["error"] or "")},
            )
            failed_targets.append({"chatroom_id": chatroom_id, "stage": "profile", "error": str(info_result["error"] or "")})
            continue

        member_result = _safe_call(
            api_client.get_chatroom_member_detail,
            stage="chatroom_member_sync",
            trace_endpoint=member_endpoint,
            channel=channel,
            wxid=wxid,
            chatroom_id=chatroom_id,
        )
        upsert_wechat_ipad_chatroom_profile(
            db,
            channel,
            chatroom_external_id=chatroom_id,
            profile_payload=info_result["payload"],
            member_payload=member_result["payload"] if member_result["ok"] else None,
            synced_at=sync_started_at,
            source="wechat_ipad_chatroom_sync",
        )
        synced_profiles += 1
        synced_members += 1 if member_result["ok"] else 0
        if not member_result["ok"]:
            logger.warning(
                "wechat ipad chatroom member sync failed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "error": str(member_result["error"] or "")},
            )
            failed_targets.append({"chatroom_id": chatroom_id, "stage": "members", "error": str(member_result["error"] or "")})
        else:
            logger.info(
                "wechat ipad chatroom sync target completed",
                extra={"channel_id": channel.id, "chatroom_id": chatroom_id, "members_synced": True},
            )

    summary = (
        f"已同步 {len(deduped_targets)} 个群目标，成功补齐群资料 {synced_profiles} 个，"
        f"成功补齐成员快照 {synced_members} 个。"
    )
    if failed_targets:
        summary += f" 其中 {len(failed_targets)} 个目标仍需继续对齐。"

    state = _persist_runtime(
        db,
        channel,
        summary=summary,
        error=None,
        extra={
            "last_chatroom_sync_started_at": sync_started_at.isoformat(),
            "last_chatroom_sync_target_count": len(deduped_targets),
            "last_chatroom_sync_profile_count": synced_profiles,
            "last_chatroom_sync_member_count": synced_members,
            "last_chatroom_sync_failed_targets": failed_targets,
            "last_chatroom_sync_summary": summary,
        },
    )
    db.commit()
    logger.info(
        "wechat ipad chatroom sync completed",
        extra={
            "channel_id": channel.id,
            "target_count": len(deduped_targets),
            "synced_profiles": synced_profiles,
            "synced_members": synced_members,
            "failed_target_count": len(failed_targets),
        },
    )
    return state
