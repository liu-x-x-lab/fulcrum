"""iPad 渠道公共 API；延迟加载实际职责模块，避免事件服务导入时形成环。"""

from importlib import import_module

_EXPORTS = {
    "auto_recover_wechat_ipad_login": "app.services.wechat_ipad.operations.recovery",
    "build_wechat_ipad_chatroom_list": "app.services.wechat_ipad.views.groups",
    "build_wechat_ipad_chatroom_detail": "app.services.wechat_ipad.group_details",
    "build_wechat_ipad_contact_list": "app.services.wechat_ipad.views.contacts",
    "build_wechat_ipad_group_summary": "app.services.wechat_ipad.views.groups",
    "build_wechat_ipad_overview": "app.services.wechat_ipad.views.overview",
    "heartbeat_wechat_ipad_channel": "app.services.wechat_ipad.operations.heartbeat",
    "pull_wechat_ipad_messages": "app.services.wechat_ipad.operations.sync",
    "poll_wechat_ipad_login_status": "app.services.wechat_ipad.operations.login",
    "record_wechat_ipad_receive_event": "app.services.wechat_ipad.runtime",
    "record_wechat_ipad_send_event": "app.services.wechat_ipad.runtime",
    "reconnect_wechat_ipad": "app.services.wechat_ipad.operations.login",
    "run_wechat_ipad_action": "app.services.wechat_ipad.service",
    "reset_wechat_ipad_chatroom_scope": "app.services.wechat_ipad.operations.scopes",
    "resolve_wechat_ipad_qrcode_image": "app.services.wechat_ipad.qr",
    "should_sync_wechat_ipad_chatroom_for_event": "app.services.wechat_ipad.events",
    "sync_wechat_ipad_chatrooms": "app.services.wechat_ipad.operations.sync",
    "sync_wechat_ipad_contacts": "app.services.wechat_ipad.operations.contacts",
    "start_wechat_ipad_login": "app.services.wechat_ipad.operations.login",
    "update_wechat_ipad_chatroom_scope_enabled": "app.services.wechat_ipad.operations.scopes",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    return getattr(import_module(module_name), name)
