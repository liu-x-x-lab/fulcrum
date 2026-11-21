"""群内会话、订阅归属和本地水位，所有数据库写入由平台调用方统一提交。"""

from __future__ import annotations

import json

from sqlalchemy import select, func

from app.core.tenant_context import require_tenant_id
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope
from app.models.moviepilot import MoviePilotSubscription
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.state_store import PluginStateStore
from app.services.plugin_scopes import plugin_group_conversation_enabled
from plugins.moviepilot.client import MoviePilotError, digest, identity, subscription_fingerprint

CODE = "moviepilot"
SELECTION_NAMESPACE = "moviepilot_selection"
RESOURCE_NAMESPACE = "moviepilot_resources"


def binding_for(db, context):
    """从可信上下文复核绑定和机器人；业务 payload 从不参与授权。"""
    if context is None or context.tenant_id != require_tenant_id():
        raise MoviePilotError("缺少有效的租户上下文。")
    binding = db.get(BotPlugin, context.binding_id)
    if (binding is None or binding.tenant_id != context.tenant_id or binding.bot_id != context.bot_id
            or binding.status != "enabled" or not binding.bot or not binding.bot.is_enabled
            or binding.bot.deleted_at is not None or not binding.plugin or binding.plugin.code != CODE
            or binding.plugin.platform_status != "active" or binding.plugin.missing_at is not None):
        raise MoviePilotError("MoviePilot 插件或机器人当前不可用。")
    return binding


def group_for(db, binding, conversation_id):
    """调度与命令使用同一归属检查，退群、停用渠道或关闭群插件立即停止操作。"""
    conversation = db.get(Conversation, conversation_id) if conversation_id else None
    if (conversation is None or conversation.tenant_id != binding.tenant_id
            or conversation.bot_id != binding.bot_id or conversation.chat_type != "group"):
        raise MoviePilotError("此操作需要当前机器人的有效群会话。")
    channel = db.get(Channel, conversation.channel_id)
    if (channel is None or channel.tenant_id != binding.tenant_id or channel.bot_id != binding.bot_id
            or not channel.is_enabled or channel.deleted_at is not None):
        raise MoviePilotError("群所属渠道已停用。")
    room = db.scalar(select(Chatroom).where(Chatroom.tenant_id == binding.tenant_id,
        Chatroom.bot_id == binding.bot_id, Chatroom.channel_id == channel.id,
        Chatroom.external_id == conversation.external_id))
    if (room is None or not room.is_active or room.status == "left" or room.management_status != "enabled"
            or not plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code=CODE)):
        raise MoviePilotError("当前群未启用 MoviePilot 插件。")
    scope = db.scalar(select(GroupPluginScope).where(GroupPluginScope.tenant_id == binding.tenant_id,
        GroupPluginScope.bot_plugin_id == binding.id, GroupPluginScope.chatroom_id == room.id,
        GroupPluginScope.channel_id == channel.id))
    override = json.loads(scope.config_override_json or "{}") if scope else {}
    return conversation, channel, override


class MoviePilotSession:
    def __init__(self, db, context, connection_key):
        self.db, self.context, self.connection_key = db, context, connection_key
        self.binding = binding_for(db, context)
        self.conversation, self.channel, _ = group_for(db, self.binding, context.conversation_id)
        if context.channel_id != self.channel.id or not context.sender_id or len(context.sender_id) > 128:
            raise MoviePilotError("缺少有效的群成员身份。")
        self.store = PluginStateStore(db, context)
        self.key = digest(connection_key, context.conversation_id, context.sender_id)

    def save_selection(self, movies):
        self.store.delete(RESOURCE_NAMESPACE, self.key)
        self.store.cleanup_expired(limit=100)
        self.store.put(SELECTION_NAMESPACE, self.key, {"movies": movies}, ttl_seconds=600)

    def selection(self, argument):
        if not argument.isascii() or not argument.isdigit() or len(argument) > 2 or not 1 <= int(argument) <= 5:
            raise MoviePilotError("请带上搜索结果序号，例如：选电影 1。")
        movies = (self.store.get(SELECTION_NAMESPACE, self.key) or {}).get("movies", [])
        if int(argument) > len(movies):
            raise MoviePilotError("搜索结果不存在或已过期，请重新发送“搜电影 片名”。")
        return movies[int(argument) - 1]

    def save_resources(self, selected, resources):
        # 只接受经过投影的资源，种子地址、站点 Cookie 和上游原始响应不进状态表。
        self.store.put(RESOURCE_NAMESPACE, self.key, {"movie": selected, "resources": resources}, ttl_seconds=600)

    def resource(self, argument):
        if not argument.isascii() or not argument.isdigit() or len(argument) > 2 or not 1 <= int(argument) <= 8:
            raise MoviePilotError("请带上资源序号，例如：下载资源 1。")
        state = self.store.get(RESOURCE_NAMESPACE, self.key) or {}
        resources = state.get("resources", [])
        if int(argument) > len(resources):
            raise MoviePilotError("资源列表不存在或已过期，请先发送“找资源 电影序号”。")
        return state["movie"], resources[int(argument) - 1]

    def throttle(self, *, mutation=False):
        # 数据库限流行使用行锁；变更在绑定范围串行，避免多人争抢同一远端订阅。
        decision = PluginRateLimiter(self.db, self.context).consume(
            "binding" if mutation else "user", identity=self.context.sender_id,
            limit=1 if mutation else 6,
            window_seconds=3 if mutation else 60)
        if not decision.allowed:
            raise MoviePilotError(f"操作较频繁，请 {max(1, decision.retry_after_seconds)} 秒后重试。")

    def query(self):
        return select(MoviePilotSubscription).where(
            MoviePilotSubscription.tenant_id == self.context.tenant_id,
            MoviePilotSubscription.binding_id == self.context.binding_id,
            MoviePilotSubscription.connection_key == self.connection_key,
            MoviePilotSubscription.conversation_id == self.context.conversation_id,
            MoviePilotSubscription.sender_id == self.context.sender_id)

    def find(self, selected):
        key = digest(self.key, identity(selected))
        return self.db.scalar(self.query().where(MoviePilotSubscription.registration_key == key))

    def own(self, argument):
        if not argument.isascii() or not argument.isdigit() or len(argument) > 12:
            raise MoviePilotError("请使用“我的电影订阅”里显示的本地编号。")
        row = self.db.scalar(self.query().where(MoviePilotSubscription.id == int(argument)))
        if row is None:
            raise MoviePilotError("这条登记不属于你在本群的订阅。")
        return row

    def register(self, selected, baseline):
        row = self.find(selected)
        if row is None:
            count = self.db.scalar(select(func.count()).select_from(MoviePilotSubscription).where(
                MoviePilotSubscription.tenant_id == self.context.tenant_id,
                MoviePilotSubscription.binding_id == self.context.binding_id,
                MoviePilotSubscription.status != "cancelled"))
            if count >= 200:
                raise MoviePilotError("本机器人最多登记 200 条电影订阅，请先取消不需要的关注。")
            row = MoviePilotSubscription(tenant_id=self.context.tenant_id, binding_id=self.context.binding_id,
                conversation_id=self.context.conversation_id, sender_id=self.context.sender_id,
                registration_key=digest(self.key, identity(selected)), connection_key=self.connection_key,
                media_source=selected["media_source"], media_id=selected["media_id"],
                title=selected["title"], year=selected["year"])
            self.db.add(row)
        row.status, row.can_delete, row.remote_id, row.remote_fingerprint = "active", False, None, None
        row.notify_enabled = True
        row.notification_json = json.dumps({"floor": baseline, "seen": {}}, sort_keys=True)
        self.db.flush()
        return row

    @staticmethod
    def attach(row, remote, *, created=False):
        from plugins.moviepilot.client import positive_id

        row.remote_id = positive_id(remote.get("id"))
        row.remote_fingerprint = subscription_fingerprint(remote)
        row.can_delete = created
        row.status = "active"


def row_identity(row):
    return row.media_source, row.media_id, "电影"
