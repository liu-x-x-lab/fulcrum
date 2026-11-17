"""同步状态、继续标记和协议摘要的诊断规则。"""

from __future__ import annotations

from typing import Any

from app.services.wechat_ipad.common import _format_beijing_time
from app.services.wechat_ipad.diagnostics.errors import (
    _format_error_stage_label,
    _normalize_error_context,
)


def _build_sync_diagnostics(
    runtime_info: dict[str, Any],
    message_stats: dict[str, Any],
    summaries: dict[str, Any],
    *,
    last_message_sync_at: Any = None,
) -> dict[str, Any]:
    sync_key = str(runtime_info.get("sync_key") or "").strip()
    continue_flag = runtime_info.get("sync_continue_flag")
    last_error = str(runtime_info.get("last_error") or "").strip() or "-"
    last_sync_at_display = _format_beijing_time(last_message_sync_at or runtime_info.get("last_message_sync_at") or runtime_info.get("last_sync_at"))
    sync_state = _classify_sync_state(
        runtime_info=runtime_info,
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        summaries=summaries,
        last_error=last_error,
    )
    operator_summary = _build_sync_operator_summary(
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        last_error=last_error,
        state_label=sync_state["label"],
        last_sync_at_display=last_sync_at_display,
        error_context=runtime_info.get("last_error_context"),
    )
    operator_snapshot = _build_sync_operator_snapshot(
        sync_state=sync_state,
        sync_key=sync_key,
        continue_flag=continue_flag,
        message_stats=message_stats,
        last_error=last_error,
        last_sync_at_display=last_sync_at_display,
        error_context=runtime_info.get("last_error_context"),
    )
    return {
        "sync_key": sync_key or "-",
        "continue_flag": _format_continue_flag(continue_flag),
        "continue_flag_raw": continue_flag,
        "state": sync_state["state"],
        "state_label": sync_state["label"],
        "operator_hint": sync_state["hint"],
        "operator_summary": operator_summary,
        "operator_snapshot": operator_snapshot,
        "last_pull_protocol_summary": _format_protocol_summary(summaries.get("pull")),
        "last_inbound_protocol_summary": _format_protocol_summary(summaries.get("inbound")),
        "last_outbound_protocol_summary": _format_protocol_summary(summaries.get("outbound")),
        "last_sync_count": int(message_stats.get("last_sync_count") or 0),
        "last_sync_ingested": int(message_stats.get("last_sync_ingested") or 0),
        "last_sync_deduplicated": int(message_stats.get("last_sync_deduplicated") or 0),
        "receive_total": int(message_stats.get("receive_total") or 0),
        "dedupe_total": int(message_stats.get("dedupe_total") or 0),
        "last_error": last_error,
        "last_sync_at_display": last_sync_at_display,
    }


def _classify_sync_state(
    *,
    runtime_info: dict[str, Any],
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    summaries: dict[str, Any],
    last_error: str,
) -> dict[str, str]:
    last_sync_at = runtime_info.get("last_message_sync_at") or runtime_info.get("last_sync_at")
    has_protocol_summary = any(summaries.get(key) not in (None, "", {}) for key in ("pull", "inbound", "outbound"))
    has_sync_activity = bool(
        last_sync_at
        or sync_key
        or has_protocol_summary
        or int(message_stats.get("last_sync_count") or 0)
        or int(message_stats.get("last_sync_ingested") or 0)
        or int(message_stats.get("last_sync_deduplicated") or 0)
    )
    error_stage = str((_normalize_error_context(runtime_info.get("last_error_context")) or {}).get("stage") or "").strip()

    if not has_sync_activity:
        return {
            "state": "empty",
            "label": "尚未形成同步样本",
            "hint": "还没有最近同步记录；先执行状态同步或消息拉取，确认 sync_key 与协议摘要开始落库。",
        }
    if last_error != "-" and ("sync" in error_stage or error_stage == ""):
        return {
            "state": "warning",
            "label": "最近同步异常",
            "hint": f"最近一次同步相关错误为：{last_error}；建议先核对 message sync 接口回包与 sync_key 是否持续推进。",
        }
    if str(continue_flag) == "1":
        return {
            "state": "warning",
            "label": "仍有待续拉取",
            "hint": "continue_flag=1，表示服务端仍有积压消息；建议继续执行 Msg/Sync，直到 continue_flag 归零。",
        }
    if int(message_stats.get("last_sync_count") or 0) > 0 and int(message_stats.get("last_sync_ingested") or 0) <= 0:
        return {
            "state": "healthy",
            "label": "最近同步成功但无新增",
            "hint": "最近一次同步已完成，但本轮没有新增入库消息；更像是空拉取或重复消息回放，可继续观察 sync_key 是否推进。",
        }
    return {
        "state": "healthy",
        "label": "最近同步正常推进",
        "hint": "最近同步链路已有摘要，可结合 sync_key、协议摘要与计数继续排查。",
    }


def _build_sync_operator_summary(
    *,
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    last_error: str,
    state_label: str,
    last_sync_at_display: str,
    error_context: Any,
) -> str:
    parts = [f"分类={state_label}"]
    if last_sync_at_display and last_sync_at_display != "-":
        parts.append(f"最近拉取={last_sync_at_display}")
    if sync_key:
        parts.append(f"sync_key={sync_key}")
    formatted_continue_flag = _format_continue_flag(continue_flag)
    if formatted_continue_flag != "-":
        parts.append(f"continue_flag={formatted_continue_flag}")

    last_sync_count = int(message_stats.get("last_sync_count") or 0)
    last_sync_ingested = int(message_stats.get("last_sync_ingested") or 0)
    last_sync_deduplicated = int(message_stats.get("last_sync_deduplicated") or 0)
    parts.append(f"拉取/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}")

    if last_error != "-":
        stage = _format_error_stage_label(_normalize_error_context(error_context).get("stage"))
        if stage != "-":
            parts.append(f"异常阶段={stage}")
        parts.append(f"异常={last_error}")
    return "；".join(parts)


def _build_sync_operator_snapshot(
    *,
    sync_state: dict[str, str],
    sync_key: str,
    continue_flag: Any,
    message_stats: dict[str, Any],
    last_error: str,
    last_sync_at_display: str,
    error_context: Any,
) -> dict[str, str]:
    last_sync_count = int(message_stats.get("last_sync_count") or 0)
    last_sync_ingested = int(message_stats.get("last_sync_ingested") or 0)
    last_sync_deduplicated = int(message_stats.get("last_sync_deduplicated") or 0)
    context = _normalize_error_context(error_context)
    stage_label = _format_error_stage_label(context.get("stage"))
    continue_flag_display = _format_continue_flag(continue_flag)

    if sync_state["label"] == "尚未形成同步样本":
        return {
            "status_key": "no_sample",
            "status_label": "尚未形成同步样本",
            "status_tone": "muted",
            "headline": "当前同步档位：尚未形成同步样本",
            "summary": "还没有形成最近同步样本，当前更像是尚未完成首次状态同步或消息拉取。",
            "detail": "关键信息：sync_key=-；最近时间=-；当前还没有同步计数、新增计数或异常阶段可供判断。",
        }
    if sync_state["label"] == "最近同步异常":
        detail_parts = ["关键信息："]
        if sync_key:
            detail_parts.append(f"sync_key={sync_key}")
        if last_sync_at_display and last_sync_at_display != "-":
            detail_parts.append(f"最近时间={last_sync_at_display}")
        if stage_label != "-":
            detail_parts.append(f"异常阶段={stage_label}")
        detail_parts.append(f"最近同步条数={last_sync_count}")
        return {
            "status_key": "recent_error",
            "status_label": "最近同步异常",
            "status_tone": "outline",
            "headline": "当前同步档位：最近同步异常",
            "summary": "最近一次同步链路出现异常，建议先按异常阶段核对最近回包和 sync_key 推进情况。",
            "detail": "；".join(detail_parts) + (f"；最近错误={last_error}" if last_error != "-" else ""),
        }
    if str(continue_flag) == "1":
        return {
            "status_key": "pending_continue",
            "status_label": "待续拉取",
            "status_tone": "outline",
            "headline": "当前同步档位：待续拉取",
            "summary": "continue_flag=1，说明这一轮还没拉到末尾，服务端仍可能有积压消息等待继续同步。",
            "detail": (
                f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
                f"continue_flag={continue_flag_display}；最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}。"
            ),
        }
    if last_sync_count > 0 and last_sync_ingested <= 0:
        return {
            "status_key": "success_no_new",
            "status_label": "最近同步成功但无新增",
            "status_tone": "success",
            "headline": "当前同步档位：最近同步成功但无新增",
            "summary": "最近一次同步成功收口，但本轮没有新增入库，现阶段更像是空拉取或重复消息回放。",
            "detail": (
                f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
                f"最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}；"
                f"continue_flag={continue_flag_display}。"
            ),
        }
    return {
        "status_key": "progressing",
        "status_label": "最近同步正常推进",
        "status_tone": "success",
        "headline": "当前同步档位：最近同步正常推进",
        "summary": "最近同步链路已有新增或持续推进迹象，当前更像是正常收口中的运行样本。",
        "detail": (
            f"关键信息：sync_key={sync_key or '-'}；最近时间={last_sync_at_display or '-'}；"
            f"最近同步/新增/去重={last_sync_count}/{last_sync_ingested}/{last_sync_deduplicated}；"
            f"continue_flag={continue_flag_display}。"
        ),
    }


def _format_continue_flag(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if str(value) == "1":
        return "需继续拉取 (1)"
    if str(value) == "0":
        return "已到末尾 (0)"
    return str(value)


def _format_protocol_summary(value: Any) -> str:
    if isinstance(value, dict):
        ordered_parts: list[str] = []
        label_map = {
            "message_count": "消息数",
            "continue_flag": "continue_flag",
            "sync_key": "sync_key",
            "first_event": "首条事件",
            "source": "来源",
            "event_key": "event_key",
            "mapped": "已入链",
            "deduplicated": "命中去重",
            "direction": "方向",
            "session_id": "session_id",
            "receiver_id": "接收方",
            "endpoint": "endpoint",
            "external_message_id": "外部消息 ID",
            "client_msg_id": "client_msg_id",
            "new_msg_id": "new_msg_id",
        }
        for key in (
            "message_count",
            "continue_flag",
            "sync_key",
            "first_event",
            "source",
            "event_key",
            "mapped",
            "deduplicated",
            "direction",
            "receiver_id",
            "external_message_id",
            "new_msg_id",
            "endpoint",
        ):
            if value.get(key) in (None, ""):
                continue
            current = value.get(key)
            if isinstance(current, bool):
                current = "是" if current else "否"
            ordered_parts.append(f"{label_map.get(key, key)}={current}")
        return "；".join(ordered_parts) if ordered_parts else "-"
    text = str(value or "").strip()
    return text or "-"
