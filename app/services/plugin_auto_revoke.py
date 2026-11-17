"""插件回复的公共撤回策略与内存调度：每个进程只有一个等待线程。

倒计时不写 TaskJob、消息快照或缓存服务，重启直接丢弃。字典只保存消息身份和
几个数字，最小堆只检查最近到期项；多个插件同时发图也不会持有图片或创建 Timer。
到期后仍复用消息中心的撤回接口，手机确认成功后才更新原消息状态。
"""

from __future__ import annotations

import heapq
import itertools
import json
import random
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Mapping

from sqlalchemy import event, select
from sqlalchemy.orm import Session, object_session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Message
from app.services.message_actions import REVOKE_SECONDS, capabilities, object_json, request_revoke


logger = get_logger(__name__)
CONFIG_KEY = "reply_auto_revoke"
DEFAULT_POLICY = {"enabled": False, "min_seconds": 60, "max_seconds": 80}
# 微信撤回窗口为 120 秒，预留十秒给调度、命令领取和手机执行。
MAX_DELAY_SECONDS = 110
FORM_FIELDS = {
    "enabled": "delivery__revoke_enabled",
    "min_seconds": "delivery__revoke_min_seconds",
    "max_seconds": "delivery__revoke_max_seconds",
}
MAX_PENDING = 10000
RECEIPT_TIMEOUT_SECONDS = 600
BATCH_SIZE = 32


def auto_revoke_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    """统一校验公共策略，拒绝倒置区间、小数、越界值和伪造的开关字符串。"""

    raw = config.get(CONFIG_KEY, {})
    if not isinstance(raw, Mapping) or set(raw) - DEFAULT_POLICY.keys():
        raise ValueError("自动撤回配置格式无效")
    policy = {**DEFAULT_POLICY, **raw}
    enabled = policy["enabled"]
    if isinstance(enabled, str) and enabled.lower() in {"true", "false"}:
        enabled = enabled.lower() == "true"
    if not isinstance(enabled, bool):
        raise ValueError("自动撤回开关值无效")
    policy["enabled"] = enabled
    for key in ("min_seconds", "max_seconds"):
        value = policy[key]
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
            raise ValueError("撤回时间需要填写整数秒")
        seconds = int(value)
        if not 1 <= seconds <= MAX_DELAY_SECONDS:
            raise ValueError(f"撤回时间需要在 1–{MAX_DELAY_SECONDS} 秒之间")
        policy[key] = seconds
    if policy["min_seconds"] > policy["max_seconds"]:
        raise ValueError("最短撤回时间不能大于最长撤回时间")
    return policy


def merge_auto_revoke_form(config: Mapping[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
    """插件默认和本群覆盖使用同一表单；未提交公共字段时保留现有策略。"""

    merged = dict(config)
    if any(name in form for name in FORM_FIELDS.values()):
        values = {**auto_revoke_policy(config), **{
            key: form[name] for key, name in FORM_FIELDS.items() if name in form
        }}
        merged[CONFIG_KEY] = auto_revoke_policy({CONFIG_KEY: values})
    elif CONFIG_KEY in merged:
        merged[CONFIG_KEY] = auto_revoke_policy(merged)
    return merged


def binding_auto_revoke_policy(db: Session, binding: BotPlugin, channel: Channel,
                               conversation: Conversation) -> dict[str, Any]:
    """排队回复在实际发送时读取当前有效策略，不给出站任务再保存撤回计划。

    群差异只从本租户、本渠道、本机器人的群记录读取，客户端或插件输出中的同名
    参数不能更改平台策略。消息/手动/定时等入口只要经过公共发送链即可使用。
    """

    if (binding.tenant_id != channel.tenant_id or conversation.tenant_id != channel.tenant_id
            or binding.bot_id != channel.bot_id or conversation.bot_id != channel.bot_id
            or conversation.channel_id != channel.id):
        raise ValueError("自动撤回的插件与目标会话归属不一致")
    config = object_json(binding.config_json)
    if conversation.chat_type == "group":
        override = db.scalar(select(GroupPluginScope.config_override_json).join(
            Chatroom, Chatroom.id == GroupPluginScope.chatroom_id,
        ).where(
            GroupPluginScope.tenant_id == channel.tenant_id,
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.bot_plugin_id == binding.id,
            Chatroom.tenant_id == channel.tenant_id, Chatroom.bot_id == channel.bot_id,
            Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id,
        ))
        patch = object_json(override).get(CONFIG_KEY, {})
        if patch:
            config[CONFIG_KEY] = {**config.get(CONFIG_KEY, {}), **patch}
    return auto_revoke_policy(config)


@dataclass(frozen=True, slots=True)
class PendingRevoke:
    """不持有 ORM/Session 和消息正文；token 让旧堆项失效而无需线性删除。"""

    tenant_id: int
    message_id: int
    channel_id: int
    conversation_id: int
    min_seconds: int
    max_seconds: int
    token: int
    due_at: float
    confirmed: bool = False
    expires_at: float = 0

    @property
    def key(self) -> tuple[int, int]:
        return self.tenant_id, self.message_id


class AutoRevokeQueue:
    """有界最小堆，登记/确认为 O(log n)，空闲时 Condition 阻塞，不定频轮询。

    收到发送回执后替换对应字典项，旧的回执超时堆项惰性丢弃；当无效堆项超过
    阈值才整体压缩，避免长期运行积累，也避免每条消息触发一次全表扫描。
    """

    def __init__(self, capacity: int = MAX_PENDING):
        self.capacity = capacity
        self.condition = threading.Condition()
        self._items: dict[tuple[int, int], PendingRevoke] = {}
        self._heap: list[tuple[float, int, tuple[int, int]]] = []
        self._tokens = itertools.count()

    def __len__(self) -> int:
        with self.condition:
            return len(self._items)

    def contains(self, tenant_id: int, message_id: int) -> bool:
        with self.condition:
            return (tenant_id, message_id) in self._items

    def remember(self, *, tenant_id: int, message_id: int, channel_id: int,
                 conversation_id: int, policy: Mapping[str, Any], now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        key = (tenant_id, message_id)
        with self.condition:
            if key in self._items:
                return True
            if len(self._items) >= self.capacity:
                return False
            item = PendingRevoke(tenant_id, message_id, channel_id, conversation_id,
                int(policy["min_seconds"]), int(policy["max_seconds"]), next(self._tokens),
                current + RECEIPT_TIMEOUT_SECONDS)
            self._put(item)
            return True

    def confirm(self, tenant_id: int, message_id: int, *, ok: bool, has_native_id: bool,
                now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self.condition:
            key = (tenant_id, message_id)
            item = self._items.get(key)
            # 重复回执不重新随机或延长倒计时；过期意图也不能被迟到回执重新创建。
            if item is None or item.confirmed:
                return
            if not ok or not has_native_id or current >= item.due_at:
                self._items.pop(key, None)
                self._compact()
                self.condition.notify()
                return
            delay = random.randint(item.min_seconds, item.max_seconds)
            self._put(replace(item, token=next(self._tokens), due_at=current + delay,
                              confirmed=True, expires_at=current + REVOKE_SECONDS))

    def discard(self, tenant_id: int, message_id: int) -> None:
        with self.condition:
            self._items.pop((tenant_id, message_id), None)
            self._compact()
            self.condition.notify()

    def clear(self) -> None:
        """停止时直接丢弃内存任务，不序列化、不扫描数据库重建。"""

        with self.condition:
            self._items.clear()
            self._heap.clear()
            self.condition.notify_all()

    def _put(self, item: PendingRevoke) -> None:
        previous_deadline = self._heap[0][0] if self._heap else None
        self._items[item.key] = item
        heapq.heappush(self._heap, (item.due_at, item.token, item.key))
        self._compact()
        # 只有下一到期时间提前才唤醒，批量登记更晚的任务不反复打断等待线程，
        # 从而避免大量同时计时带来无意义的线程切换。
        if previous_deadline is None or item.due_at < previous_deadline:
            self.condition.notify()

    def _compact(self) -> None:
        if len(self._heap) > 2 * len(self._items) + 128:
            self._heap = [(item.due_at, item.token, item.key) for item in self._items.values()]
            heapq.heapify(self._heap)

    def take_due(self, *, now: float | None = None, limit: int = BATCH_SIZE) -> list[PendingRevoke]:
        current = time.monotonic() if now is None else now
        due: list[PendingRevoke] = []
        with self.condition:
            while self._heap and len(due) < limit:
                at, token, key = self._heap[0]
                item = self._items.get(key)
                if item is None or item.token != token:
                    heapq.heappop(self._heap)
                    continue
                if at > current:
                    break
                heapq.heappop(self._heap)
                self._items.pop(key)
                if item.confirmed and current < item.expires_at:
                    due.append(item)
                # 未收到回执或已经超过撤回期限的项在内存直接淘汰，不产生数据库读写。
            return due

    def wait_due(self, stop: threading.Event) -> list[PendingRevoke]:
        with self.condition:
            while not stop.is_set():
                due = self.take_due()
                if due:
                    return due
                timeout = max(0, self._heap[0][0] - time.monotonic()) if self._heap else None
                self.condition.wait(timeout)
            return []


queue = AutoRevokeQueue()
_stop = threading.Event()
_thread: threading.Thread | None = None
_lifecycle_lock = threading.Lock()


def remember_outbound(channel: Channel, conversation: Conversation, message: Message,
                      policy: Mapping[str, Any] | None) -> None:
    """仅给开启且渠道支持的成功入队消息登记，必须在发送事务提交前调用。"""

    if not policy or not policy.get("enabled") or message.status == "failed":
        return
    identity = {"tenant_id": int(channel.tenant_id), "channel_id": int(channel.id),
                "conversation_id": int(conversation.id), "message_id": int(message.id)}
    if not capabilities(channel, conversation)["revoke"]:
        logger.info("plugin auto revoke skipped: channel unsupported", extra=identity)
        return
    if not queue.remember(**identity, policy=policy):
        logger.warning("plugin auto revoke skipped: memory queue full", extra=identity)
    else:
        db = object_session(message)
        if db is not None:
            # 发送链还有统计等同事务写入，任何一步回滚都应撤销意图，不能只处理
            # commit 抛错的情况，更不能等十分钟 TTL 才释放这些失败登记。
            db.info.setdefault("plugin_auto_revoke_registrations", set()).add((identity["tenant_id"], identity["message_id"]))


def confirm_outbound(message: Message, *, ok: bool) -> None:
    """异步手机回执和同步渠道共用；无原生编号不能凭正文猜测撤回目标。"""

    if ok:
        # created_at 是入队时间，不代表手机真正发出的时间。只保存发送确认时间，
        # 手动/自动撤回共用同一期限；撤回策略、随机值和倒计时均不写入消息记录。
        raw = object_json(message.raw_request_json)
        if not raw.get("sent_at"):
            raw["sent_at"] = now_shanghai().isoformat()
            message.raw_request_json = json.dumps(raw, ensure_ascii=False)
    tenant_id, message_id = int(message.tenant_id), int(message.id)
    if queue.contains(tenant_id, message_id):
        if ok and not message.external_message_id:
            logger.info("plugin auto revoke skipped: native message id missing", extra={
                "tenant_id": tenant_id, "message_id": message_id})
        db = object_session(message)
        if db is not None:
            # 回执事务提交前不让调度线程读取尚未可见的新状态；只保存标量，避免
            # after_commit 持有 ORM/连接。事务失败时仍等待下一次真实回执。
            confirmations = db.info.setdefault("plugin_auto_revoke_confirmations", {})
            confirmations[(tenant_id, message_id)] = (ok, bool(message.external_message_id), time.monotonic())


@event.listens_for(Session, "after_commit")
def _confirm_committed_receipts(db: Session) -> None:
    db.info.pop("plugin_auto_revoke_registrations", None)
    for (tenant_id, message_id), (ok, has_id, confirmed_at) in db.info.pop("plugin_auto_revoke_confirmations", {}).items():
        queue.confirm(tenant_id, message_id, ok=ok, has_native_id=has_id, now=confirmed_at)


@event.listens_for(Session, "after_rollback")
def _discard_uncommitted_receipts(db: Session) -> None:
    db.info.pop("plugin_auto_revoke_confirmations", None)
    for tenant_id, message_id in db.info.pop("plugin_auto_revoke_registrations", set()):
        queue.discard(tenant_id, message_id)


def execute_due_revoke(db: Session, item: PendingRevoke) -> dict[str, Any]:
    """到期仅查这条消息，完整核对租户/渠道/会话，复用已有幂等撤回路径。"""

    channel = db.get(Channel, item.channel_id)
    conversation = db.get(Conversation, item.conversation_id)
    message = db.get(Message, item.message_id)
    if (channel is None or conversation is None or message is None
            or any(int(row.tenant_id) != item.tenant_id for row in (channel, conversation, message))
            or conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id
            or message.conversation_id != conversation.id or message.direction != "out"):
        return {"skipped": True, "reason": "message_scope_mismatch"}
    if message.status == "revoked":
        return {"skipped": True, "reason": "already_revoked"}
    try:
        return request_revoke(db, channel, conversation, message)
    except ValueError as exc:
        # 期限、渠道能力及手动撤回状态以真正执行时为准，不能重复或无限重试撤回。
        return {"skipped": True, "reason": str(exc)}


def _worker_loop() -> None:
    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session

    while not _stop.is_set():
        items = queue.wait_due(_stop)
        # 每批最多 32 条，按租户复用一次连接；每条独立提交/回滚，避免一条故障拖掉整批。
        by_tenant: dict[int, list[PendingRevoke]] = {}
        for item in items:
            by_tenant.setdefault(item.tenant_id, []).append(item)
        for tenant_id, batch in by_tenant.items():
            try:
                with tenant_scope(tenant_id), tenant_session() as db:
                    for item in batch:
                        if _stop.is_set():
                            return
                        try:
                            result = execute_due_revoke(db, item)
                            db.commit()
                            logger.info("plugin auto revoke dispatched", extra={
                                "tenant_id": tenant_id, "message_id": item.message_id, **result})
                        except Exception:
                            db.rollback()
                            logger.exception("plugin auto revoke failed", extra={
                                "tenant_id": tenant_id, "message_id": item.message_id})
            except Exception:
                logger.exception("plugin auto revoke tenant session failed", extra={"tenant_id": tenant_id})


def start_auto_revoke_worker() -> None:
    """全应用只启动一个线程，与插件数量、消息数量无关。"""

    global _thread
    with _lifecycle_lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_worker_loop, name="plugin-auto-revoke", daemon=True)
        _thread.start()


def stop_auto_revoke_worker() -> None:
    with _lifecycle_lock:
        _stop.set()
        queue.clear()
        if _thread and _thread.is_alive():
            _thread.join(timeout=5)
