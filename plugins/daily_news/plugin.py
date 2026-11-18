from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, PluginStateRecord
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpResponse


SCHEDULE_ACTION_KEY = "baidu_news_daily_push"
DEFAULT_USER_AGENT = "fulcrum/1.0"
CHINESE_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
STATE_NAMESPACE = "daily_news"


def _group_state_key(conversation: Conversation) -> str:
    raw = f"{conversation.channel_id}:{conversation.external_id}".encode("utf-8")
    return "group:" + hashlib.sha256(raw).hexdigest()[:32]


def _push_time_override_chatrooms(db: Session, binding: BotPlugin) -> set[int]:
    """单群改过推送时间的群不再参加绑定级全局推送，避免一天收两次。"""
    scopes = db.scalars(
        select(GroupPluginScope).where(
            GroupPluginScope.bot_plugin_id == binding.id,
            GroupPluginScope.tenant_id == binding.tenant_id,
            GroupPluginScope.enabled.is_(True),
            GroupPluginScope.config_override_json.isnot(None),
        )
    ).all()
    result: set[int] = set()
    for scope in scopes:
        if "push_time" in _parse_json_object(scope.config_override_json):
            result.add(int(scope.chatroom_id))
    return result


def _group_pushed_conversation_ids(
    db: Session,
    binding: BotPlugin,
    conversations: list[Conversation],
    today: str,
) -> set[int]:
    """一次读出这些群今天是否已经推送过，避免逐群查询。"""

    conversation_ids_by_key = {_group_state_key(item): int(item.id) for item in conversations}
    if not conversation_ids_by_key:
        return set()
    rows = db.scalars(
        select(PluginStateRecord).where(
            PluginStateRecord.tenant_id == binding.tenant_id,
            PluginStateRecord.bot_plugin_id == binding.id,
            PluginStateRecord.namespace == STATE_NAMESPACE,
            PluginStateRecord.state_key.in_(tuple(conversation_ids_by_key)),
        )
    ).all()
    return {
        conversation_ids_by_key[row.state_key]
        for row in rows
        if str(_parse_json_object(row.payload_json).get("last_push_date") or "") == today
    }


def _mark_groups_pushed(db: Session, binding: BotPlugin, state_keys: list[str], today: str) -> None:
    """只记录真正创建了出站任务的群；同一群一天只保留一条状态。"""

    if not state_keys:
        return
    rows = db.scalars(
        select(PluginStateRecord).where(
            PluginStateRecord.tenant_id == binding.tenant_id,
            PluginStateRecord.bot_plugin_id == binding.id,
            PluginStateRecord.namespace == STATE_NAMESPACE,
            PluginStateRecord.state_key.in_(tuple(state_keys)),
        )
    ).all()
    existing = {row.state_key: row for row in rows}
    payload = json.dumps({"last_push_date": today}, ensure_ascii=False, sort_keys=True)
    for key in state_keys:
        row = existing.get(key)
        if row is None:
            db.add(
                PluginStateRecord(
                    tenant_id=binding.tenant_id,
                    bot_plugin_id=binding.id,
                    namespace=STATE_NAMESPACE,
                    state_key=key,
                    payload_json=payload,
                )
            )
        else:
            row.payload_json = payload
    # 同一任务内可能连续执行/复核，立即刷新保证幂等查询读到刚写入的日期。
    db.flush()


@dataclass(frozen=True)
class DailyNewsConfig:
    """每日新闻只保留抓取、推送时刻和去重日期等当前业务配置。"""

    baidu_api_url: str
    news60s_api_url: str
    default_timeout: int
    baidu_limit: int
    push_time: str
    last_push_date: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """每日新闻不再处理聊天命令，只保留运行时重载检查。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="每日新闻定时任务已就绪。",
            output={"push_time": config.push_time},
        )
    return PluginExecutionResult(
        ok=False,
        status="unsupported",
        message="每日新闻仅支持平台定时调度。",
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在 Worker 已绑定的租户会话中执行每日新闻群推送。"""

    if request.action != SCHEDULE_ACTION_KEY:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的每日新闻动作: {request.action}",
        )
    if request.context is None:
        return PluginExecutionResult(ok=False, status="error", message="每日新闻缺少可信运行上下文。")

    binding = db.get(BotPlugin, int(request.context.binding_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(request.context.tenant_id)
        or int(binding.bot_id) != int(request.context.bot_id)
    ):
        return PluginExecutionResult(ok=False, status="error", message="每日新闻插件绑定不存在。")

    current = now_shanghai()
    config = _load_config(request.config)
    today = current.strftime("%Y-%m-%d")
    targeted_conversation_id = int(request.context.conversation_id) if request.context.conversation_id else None
    # 绑定级 last_push_date 只约束全局计划；单群任务改用每个群的推送状态。
    if not targeted_conversation_id and config.last_push_date == today:
        return PluginExecutionResult(
            ok=True,
            status="skipped",
            message="今日新闻已经推送。",
            output={"reason": "already_pushed"},
        )

    if targeted_conversation_id:
        conversations = _resolve_target_conversations(db, binding.bot_id, conversation_id=targeted_conversation_id)
        if not conversations:
            return PluginExecutionResult(
                ok=False,
                status="error",
                message="每日新闻目标群聊不可用。",
                output={"reason": "target_unavailable", "target_count": 0},
            )
        pushed_today = _group_pushed_conversation_ids(db, binding, conversations, today)
        conversations = [item for item in conversations if int(item.id) not in pushed_today]
        if not conversations:
            return PluginExecutionResult(
                ok=True,
                status="skipped",
                message="今日新闻该群已经推送。",
                output={"reason": "already_pushed", "target_count": 0},
            )
    else:
        all_conversations = _resolve_target_conversations(db, binding.bot_id)
        if not all_conversations:
            return PluginExecutionResult(
                ok=False,
                status="error",
                message="每日新闻没有可推送的启用群聊。",
                output={"reason": "no_target_conversations", "target_count": 0},
            )
        conversations = _exclude_schedule_override_chatrooms(
            db, all_conversations, _push_time_override_chatrooms(db, binding)
        )
        if not conversations:
            return PluginExecutionResult(
                ok=True,
                status="skipped",
                message="所有目标群都已改为群级定时推送。",
                output={"reason": "group_schedule_overrides", "target_count": 0},
            )
        pushed_today = _group_pushed_conversation_ids(db, binding, conversations, today)
        conversations = [item for item in conversations if int(item.id) not in pushed_today]
        if not conversations:
            return PluginExecutionResult(
                ok=True,
                status="skipped",
                message="今日新闻所有目标群已经推送。",
                output={"reason": "already_pushed", "target_count": 0},
            )

    # 先确定目标再抓取，已推送的重复任务不再白跑外部接口。
    try:
        digest = _build_daily_digest(config, current=current)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"每日新闻抓取失败: {exc}")

    state_keys_by_conversation_id = {int(item.id): _group_state_key(item) for item in conversations}
    batch_result = enqueue_outbound_jobs_by_conversation(
        db,
        conversations=conversations,
        plugin_code="daily_news",
        related_type="daily_news_binding",
        related_id=binding.id,
        priority=70,
        payload_builder=lambda conversation, channel: _build_push_payloads(conversation, digest),
        log_context={"binding_id": binding.id},
    )
    job_ids = list(batch_result["job_ids"])
    failed_targets = list(batch_result["failed_targets"])
    pushed_count = int(batch_result["pushed_conversation_count"] or 0)
    pushed_conversation_ids = [
        int(item) for item in batch_result.get("pushed_conversation_ids") or []
    ]

    if not failed_targets:
        # 只有真正入队成功的群才记为今日已推；失败任务回滚后不能留下假状态。
        _mark_groups_pushed(
            db,
            binding,
            [
                state_keys_by_conversation_id[conversation_id]
                for conversation_id in pushed_conversation_ids
                if conversation_id in state_keys_by_conversation_id
            ],
            today,
        )
        if not targeted_conversation_id and pushed_count:
            # 只更新绑定已有普通配置中的业务状态，不把目录默认值或任何敏感配置写回。
            ordinary_config = _parse_json_object(binding.config_json)
            ordinary_config["last_push_date"] = today
            binding.config_json = json.dumps(ordinary_config, ensure_ascii=False, sort_keys=True)
    if not pushed_count:
        return PluginExecutionResult(
            ok=not failed_targets,
            status="error" if failed_targets else "skipped",
            message="每日新闻没有创建推送任务。",
            output={
                "reason": "no_pushed_group",
                "target_count": len(conversations),
                "queued_conversation_count": 0,
                "outbound_job_ids": job_ids,
                "failed_targets": failed_targets,
            },
        )
    summary = f"每日新闻已向 {pushed_count} 个群聊创建推送任务。"
    if failed_targets:
        summary += f" {len(failed_targets)} 个群聊创建失败。"
    return PluginExecutionResult(
        ok=not failed_targets,
        status="completed" if not failed_targets else "partial",
        message=summary,
        output={
            "target_count": len(conversations),
            "queued_conversation_count": pushed_count,
            "outbound_job_ids": job_ids,
            "failed_targets": failed_targets,
        },
    )


def _build_daily_digest(config: DailyNewsConfig, *, current: datetime) -> dict[str, str]:
    """生成与最新来源一致的百度热榜文本和 60 秒新闻图片。"""

    text = _get_baidu_news(config, current=current)
    image_base64 = _get_60s_image_base64(config)
    return {"text": text, "image_base64": image_base64 or ""}


def _get_baidu_news(config: DailyNewsConfig, *, current: datetime) -> str:
    response = PluginHttpClient().request(
        "GET",
        config.baidu_api_url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        connect_timeout_seconds=config.default_timeout,
        total_timeout_seconds=config.default_timeout,
        max_response_bytes=2 * 1024 * 1024,
        retry_count=1,
    )
    body = _require_json_object(response)
    cards = ((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("cards") or []
    lines = [
        f"当前日期：{current.strftime('%Y年%m月%d日')}  {CHINESE_WEEKDAYS[current.weekday()]}",
        "",
    ]
    index = 1
    for card in cards:
        contents = card.get("content") if isinstance(card, dict) else []
        for block in contents or []:
            articles = block.get("content") if isinstance(block, dict) else []
            for article in articles or []:
                if not isinstance(article, dict):
                    continue
                title = str(article.get("word") or "").strip().replace(" ", "_")
                if not title:
                    continue
                lines.append(f"{index} :#{title}")
                index += 1
                if index > config.baidu_limit:
                    return "\n".join(lines).strip()
    return "\n".join(lines).strip()


def _get_60s_image_base64(config: DailyNewsConfig) -> str | None:
    # 60s 读懂世界接口需要使用者自行部署并配置地址;未配置时不生成图片,只发送文字。
    if not config.news60s_api_url:
        return None
    client = PluginHttpClient()
    response = client.request(
        "GET",
        config.news60s_api_url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        connect_timeout_seconds=config.default_timeout,
        total_timeout_seconds=config.default_timeout,
        max_response_bytes=2 * 1024 * 1024,
        retry_count=1,
    )
    body = _require_json_object(response)
    image_url = str(((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("image") or "").strip()
    if not image_url:
        return None
    image_response = client.request(
        "GET",
        image_url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        connect_timeout_seconds=config.default_timeout,
        total_timeout_seconds=config.default_timeout,
        max_response_bytes=8 * 1024 * 1024,
        retry_count=1,
    )
    _require_success(image_response)
    return base64.b64encode(image_response.body).decode("ascii") if image_response.body else None


def _resolve_target_conversations(
    db: Session,
    bot_id: int,
    *,
    conversation_id: int | None = None,
) -> list[Conversation]:
    """最新来源只向已启用群聊推送，不再把私聊作为无群时的隐式降级目标。"""

    statement = (
        select(Conversation)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(
            Conversation.bot_id == bot_id,
            Conversation.chat_type == "group",
            Channel.is_enabled.is_(True),
        )
        .order_by(Conversation.id.asc())
    )
    if conversation_id:
        statement = statement.where(Conversation.id == conversation_id)
    return list(db.scalars(statement).all())


def _exclude_schedule_override_chatrooms(
    db: Session,
    conversations: list[Conversation],
    excluded_chatroom_ids: set[int],
) -> list[Conversation]:
    """全局计划跳过在群详情单独设置过推送时间的群，由群级任务负责。"""

    if not excluded_chatroom_ids:
        return conversations
    result: list[Conversation] = []
    for conversation in conversations:
        room_id = db.scalar(
            select(Chatroom.id).where(
                Chatroom.channel_id == conversation.channel_id,
                Chatroom.external_id == conversation.external_id,
            )
        )
        if room_id is not None and int(room_id) in excluded_chatroom_ids:
            continue
        result.append(conversation)
    return result


def _build_push_payloads(conversation: Conversation, digest: dict[str, str]) -> list[dict[str, str]]:
    """输出通用图片和文本消息，由具体渠道适配器负责最终发送。"""

    payloads: list[dict[str, str]] = []
    text = str(digest.get("text") or "").strip()
    image_base64 = str(digest.get("image_base64") or "").strip()
    if text:
        payloads.append(
            {
                "external_id": conversation.external_id,
                "receiver_id": conversation.external_id,
                "chat_type": conversation.chat_type,
                "message_type": "text",
                "content": text,
            }
        )
    if image_base64:
        payloads.append(
            {
                "external_id": conversation.external_id,
                "receiver_id": conversation.external_id,
                "chat_type": conversation.chat_type,
                "message_type": "image",
                "base64": image_base64,
            }
        )
    return payloads


def _require_json_object(response: PluginHttpResponse) -> dict[str, Any]:
    _require_success(response)
    parsed = response.json()
    if not isinstance(parsed, dict):
        raise ValueError("新闻接口返回的 JSON 不是对象")
    return parsed


def _require_success(response: PluginHttpResponse) -> None:
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"新闻接口返回 HTTP {response.status_code}")


def _load_config(raw: Any) -> DailyNewsConfig:
    data = dict(raw) if isinstance(raw, dict) else {}
    return DailyNewsConfig(
        baidu_api_url=str(
            data.get("baidu_api_url")
            or "https://top.baidu.com/api/board?platform=wise&tab=realtime"
        ).strip(),
        news60s_api_url=str(data.get("news60s_api_url") or "").strip(),
        default_timeout=max(5, min(_coerce_int(data.get("default_timeout"), 15), 60)),
        baidu_limit=max(5, min(_coerce_int(data.get("baidu_limit"), 30), 50)),
        push_time=str(data.get("push_time") or "08:30").strip() or "08:30",
        last_push_date=str(data.get("last_push_date") or "").strip(),
    )


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
