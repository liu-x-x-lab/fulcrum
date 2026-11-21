"""群治理的声明式配置；执行由入站前置阶段负责，不注册普通群功能处理器。"""

import re
import unicodedata

from app.services.config_fields import ConfigField
from app.services.group_features.registry import GroupFeature, register


def normalized_text(value: str) -> str:
    """只统一大小写和全半角，不做模糊推断或跨字拼接，避免扩大误伤范围。"""
    return unicodedata.normalize("NFKC", value).casefold()


def words(config: dict) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in str(config.get("words") or "").splitlines() if line.strip()))


def validate(config: dict) -> None:
    """保存和执行共用校验，词库限额与现有表单容量保持一致。"""
    if len(str(config.get("words") or "")) > 2000 or len(words(config)) > 200:
        raise ValueError("敏感词最多 200 个，总长度不能超过 2000 字")
    if any(len(word) > 128 for word in words(config)):
        raise ValueError("单个敏感词不能超过 128 字")
    if len(str(config.get("exempt_members") or "")) > 2000:
        raise ValueError("豁免成员列表不能超过 2000 字")
    for key in ("warning_text", "kick_text"):
        template = str(config.get(key) or "")
        if len(template) > 2000:
            raise ValueError("提示语不能超过 2000 字")
        unknown = set(re.findall(r"\{([^{}]+)\}", template)) - {"member", "count", "limit", "group"}
        if unknown:
            raise ValueError("提示语仅支持 {member}、{count}、{limit}、{group}")


FEATURE = register(GroupFeature(
    key="moderation", name="敏感词治理",
    description="命中后警告或累计踢出，并停止该消息的其他自动处理。",
    validate_config=validate,
    fields=(
        ConfigField("words", "敏感词", "textarea", "", hint="每行一个，忽略英文大小写和全半角。", group="匹配"),
        ConfigField("exempt_members", "豁免成员 ID", "textarea", "", hint="每行一个。群主、管理员和机器人自身始终豁免。", group="匹配"),
        ConfigField("mode", "处理方式", "select", "warn", options=(("warn", "仅警告"), ("kick", "累计踢出（微桥、iPad）")), group="处理"),
        ConfigField("threshold", "踢出次数", "number", 3, minimum=1, maximum=1000, group="处理", show_when=("mode", "kick")),
        ConfigField("window_hours", "累计周期（小时）", "number", 24, minimum=0, maximum=8760, hint="0 为长期累计；其他值按最近指定小时统计。", group="处理"),
        ConfigField("warning_text", "警告提示", "textarea", "{member}，请遵守群规。当前已违规 {count} 次。", hint="支持 {member}、{count}、{limit}、{group}，留空不发送。", group="提示语"),
        ConfigField("kick_text", "踢出成功提示", "textarea", "{member} 累计违规 {count} 次，已移出群聊。", hint="收到手机成功回报后发送；变量同上，留空不发送。", group="提示语", show_when=("mode", "kick")),
    ),
))
