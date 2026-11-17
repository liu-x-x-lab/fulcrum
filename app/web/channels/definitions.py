"""渠道表单和群作用域使用的公共目录定义。"""

from app.services.channel_catalog import (
    list_supported_channel_types,
    serialize_channel_type_definitions,
)
from app.services.plugin_directory import GROUP_SCOPED_CHANNEL_TYPES


BUILTIN_CHANNEL_TYPES = list_supported_channel_types()


# 支持群级插件启停、配置覆盖和纳管状态的渠道；微桥复用企微的群级作用域实现。
GROUP_SCOPE_CHANNEL_TYPES = GROUP_SCOPED_CHANNEL_TYPES
CHANNEL_TYPE_FORM_SCHEMA = serialize_channel_type_definitions()
CHANNEL_TYPE_FORM_SCHEMA_BY_CODE = {item["code"]: item for item in CHANNEL_TYPE_FORM_SCHEMA}
