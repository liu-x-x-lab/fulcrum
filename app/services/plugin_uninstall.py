"""平台插件卸载：协调目录恢复、平台可用状态与审计，保留所有租户数据。"""

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Plugin
from app.services.plugin_governance import STATUS_DISABLED, record_plugin_audit
from app.services.plugin_packages import staged_uninstall


def uninstall_plugin(db: Session, plugin: Plugin, *, actor_user_id: int) -> None:
    """专用卸载事务由本函数提交，调用方不能在会话中夹带其他待提交修改。

    不删除 Plugin/绑定/配置/密钥/状态/历史记录；目录不再可发现且平台保持停用，
    重新安装同编码后也必须手动启用。数据库失败时文件上下文恢复原目录。
    已进入执行中的 Python 调用无法安全强制终止，后续调度走现有可用性检查。
    """

    try:
        with staged_uninstall(plugin.code) as backup:
            plugin.platform_status = STATUS_DISABLED
            plugin.missing_at = now_shanghai()
            plugin.status_note = "插件已卸载，重新安装后需手动启用"
            record_plugin_audit(db, actor_user_id, "plugin.uninstall", plugin,
                                f"code={plugin.code};backup={backup};tenant_data=preserved")
            db.commit()
    except BaseException:
        db.rollback()
        raise
