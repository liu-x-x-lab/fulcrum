"""机器人插件在没有单群设置时的默认开放范围。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin, TaskJob


def binding_group_default_enabled(binding: BotPlugin) -> bool:
    # 尚未 flush 的旧构造方式可能还没有默认值，按旧绑定的继承语义处理。
    default_enabled = getattr(binding, "group_default_enabled", None)
    return binding.status == "enabled" and (default_enabled is None or bool(default_enabled))


def group_plugin_scope_enabled(binding: BotPlugin, scope: GroupPluginScope | None) -> bool:
    """已保存的单群选择优先，缺少记录时使用首次绑定的选择。"""

    return bool(scope.enabled) if scope is not None else binding_group_default_enabled(binding)


def plugin_group_conversation_enabled(db: Session, *, channel: Channel, conversation: Conversation, plugin_code: str) -> bool:
    """定时和异步出站也必须遵守群插件设置，不能绕过消息入口。"""

    # 视频与超限卡片异步领取，停用或卸载解析插件后不能补发到群聊或私聊。
    if plugin_code == "douyin_parser":
        plugin = db.scalar(select(Plugin).where(Plugin.code == plugin_code))
        if plugin is None or plugin.platform_status != "active" or plugin.missing_at is not None:
            return False
    if plugin_code == "group_feature:moderation":
        from app.services.group_features.store import is_enabled

        # 治理提示同样服从当前群开关；关闭后不能继续发送队列里的警告或处罚通知。
        if (not channel.is_enabled or conversation.tenant_id != channel.tenant_id
                or conversation.bot_id != channel.bot_id or conversation.channel_id != channel.id):
            return False
        room = db.scalar(select(Chatroom).where(
            Chatroom.tenant_id == channel.tenant_id, Chatroom.bot_id == channel.bot_id,
            Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id,
        ))
        return bool(room and room.status != "left" and room.management_status != "archived" and is_enabled(room, "moderation"))
    # AI 核心和内置群功能有各自的设置入口，不属于可绑定的目录插件。
    if not plugin_code or plugin_code == "ai_dispatch" or plugin_code.startswith("group_feature:"):
        return True
    # 卸载/停用后已经排队的群聊和私聊也必须停止，不能只依赖入队时的授权。
    plugin = db.scalar(select(Plugin).where(Plugin.code == plugin_code))
    if plugin is None or plugin.platform_status != "active" or plugin.missing_at is not None:
        return False
    if conversation.chat_type != "group":
        return True
    binding = db.scalar(
        select(BotPlugin).join(Plugin, Plugin.id == BotPlugin.plugin_id).where(
            BotPlugin.bot_id == channel.bot_id,
            Plugin.code == plugin_code,
        )
    )
    if binding is None or binding.status != "enabled":
        return False
    chatroom = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id))
    if plugin_code == "maibot_adapter" and chatroom is not None and (chatroom.status == "left" or not chatroom.is_active or chatroom.management_status != "enabled"):
        return False
    scope = None
    if chatroom is not None:
        scope = db.scalar(select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
            GroupPluginScope.bot_plugin_id == binding.id,
        ))
    return group_plugin_scope_enabled(binding, scope)


def cancel_disallowed_plugin_outbound_job(db: Session, *, channel: Channel, job: TaskJob, payload: dict) -> bool:
    """设备领取前复核已入队的插件消息；手动消息和控制命令不受群插件设置影响。"""

    plugin_code = str(payload.get("plugin_code") or "").strip()
    if not plugin_code:
        return False
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    binding = db.scalar(select(BotPlugin).join(Plugin, Plugin.id == BotPlugin.plugin_id).where(
        BotPlugin.bot_id == channel.bot_id, Plugin.code == plugin_code,
    ))
    if (
        conversation is not None
        and conversation.channel_id == channel.id and conversation.bot_id == channel.bot_id
        and not (binding is not None and binding.status == "unbound")
        and plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code=plugin_code)
        and (plugin_code != "maibot_adapter" or (channel.is_enabled and channel.deleted_at is None and _maibot_outbound_allowed(binding, conversation, payload)))
    ):
        return False
    # 关闭权限后旧任务不能留在 pending，避免重新启用时又发出过期消息。
    job.status = "cancelled"
    job.finished_at = now_shanghai()
    job.error_message = "插件消息已无有效会话或授权，取消待发送消息"
    return True


def _maibot_outbound_allowed(binding, conversation, payload) -> bool:
    from app.services.maibot_config import maibot_outbound_allowed

    return maibot_outbound_allowed(binding, conversation, payload)


def maibot_previous_part_pending(db: Session, job: TaskJob, payload: dict) -> bool:
    """Do not let a later part overtake an earlier one via another phone transport."""
    from app.services.task_queue import OUTBOUND_DISPATCH_JOB, parse_job_payload

    previous_id = payload.get("maibot_previous_dispatch_id")
    if payload.get("plugin_code") != "maibot_adapter" or type(previous_id) is not int:
        return False
    previous = db.get(TaskJob, previous_id)
    if previous is None or previous.tenant_id != job.tenant_id or previous.job_type != OUTBOUND_DISPATCH_JOB:
        return False
    previous_payload = parse_job_payload(previous)
    if any(previous_payload.get(key) != payload.get(key) for key in (
        "plugin_code", "channel_id", "conversation_id", "maibot_binding_id", "maibot_connection_id", "maibot_connection_revision",
    )):
        return False
    if previous.status in {"pending", "running"}:
        return True
    from app.services.maibot_config import parse_config

    result = parse_config(previous.result_json)
    command_id = (result.get("adapter_result") or {}).get("job_id")
    command = db.get(TaskJob, command_id) if type(command_id) is int else None
    return bool(command is not None and command.tenant_id == job.tenant_id and command.job_type == job.job_type
                and command.status in {"pending", "running"})
