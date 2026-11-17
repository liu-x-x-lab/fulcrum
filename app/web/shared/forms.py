"""页面输入解析与配置合并，不执行数据库写入。"""

from datetime import date, datetime
from typing import Any
import json

from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime
from app.services.plugin_schemas import (
    deep_merge_config,
    get_plugin_config_schema,
    parse_config_json,
)


def parse_json_object(raw_text: str) -> tuple[dict[str, object], str]:
    normalized = (raw_text or "").strip()
    if not normalized:
        return {}, ""
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        return {}, f"JSON 解析失败: {exc}"
    if not isinstance(parsed, dict):
        return {}, "JSON 必须是对象。"
    return parsed, ""


def build_group_scope_effective_config(plugin_code: str | None, binding_config_json: str | None, scope_config_json: str | None) -> dict[str, Any]:
    base: dict[str, Any] = {}
    schema = get_plugin_config_schema(plugin_code)
    if schema:
        for field in schema.fields:
            if field.default not in (None, "", []):
                base[field.name] = field.default
    binding_config = parse_config_json(binding_config_json)
    scope_config = parse_config_json(scope_config_json)
    return deep_merge_config(deep_merge_config(base, binding_config), scope_config)


def merge_channel_adapter_config(
    *,
    channel_type: str,
    adapter_config_json: str,
) -> tuple[str, str]:
    """规范化渠道运行配置，并直接删除微信 iPad 已废弃的长期代理节点。"""

    parsed, error = parse_json_object(adapter_config_json)
    if error:
        return "", error
    if str(channel_type or "").strip() == "wechat_ipad":
        # 当前架构只允许 LoginProxySession 保存单次扫码代理，编辑历史账号时同步清理旧数据。
        parsed.pop("proxy", None)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True) if parsed else "", ""


def parse_date_input(value: str) -> date | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def parse_log_timestamp(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            continue
    return None


def ensure_utc_datetime(value: datetime | None) -> datetime | None:
    if not value:
        return None
    return ensure_shanghai_datetime(value)


def level_rank(level: str) -> int:
    mapping = {"INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    return mapping.get(level.upper(), 0)
