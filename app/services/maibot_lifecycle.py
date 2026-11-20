"""Restore configured MaiBot connections after a process or binding starts."""

from app.core.logging import get_logger
from app.services.plugin_runtime import execute_binding_action

logger = get_logger(__name__)


def start_maibot_binding(binding) -> None:
    if (binding.status != "enabled" or binding.plugin is None
            or binding.plugin.code != "maibot_adapter" or binding.plugin.missing_at is not None
            or binding.bot is None or not binding.bot.is_enabled or binding.bot.deleted_at is not None):
        return
    try:
        execute_binding_action(binding, "start_connections")
    except Exception:
        # Connection and secret-loader exceptions may contain sensitive data.
        logger.warning("unable to start MaiBot binding", extra={"tenant_id": binding.tenant_id, "binding_id": binding.id})
