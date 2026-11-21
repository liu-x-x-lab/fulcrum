"""群内功能开关、插件配置和群作用域管理。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Chatroom
from app.services.crud import get_channel
from app.services.group_features.registry import get_feature as get_group_feature
from app.services.group_features.view import (
    save_from_form as save_group_feature_form,
    toggle_flag as toggle_group_feature_flag,
)
from app.services.group_plugin_settings import (
    group_plugin_settings_view,
    reset_group_plugin_settings,
    save_group_plugin_settings,
)
from app.services.ops import record_audit
from app.services.wechat_ipad import (
    reset_wechat_ipad_chatroom_scope,
    update_wechat_ipad_chatroom_scope_enabled,
)
from app.services.wechat_winauto_chatrooms import (
    reset_wechat_winauto_chatroom_scope,
    update_wechat_winauto_chatroom_scope_enabled,
)
from app.services.workwechat_chatrooms import (
    reset_workwechat_chatroom_scope,
    update_workwechat_chatroom_scope_enabled,
)
from app.web.auth_common import require_tenant_csrf
from app.web.channels.definitions import GROUP_SCOPE_CHANNEL_TYPES
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import _csrf_from_header
from app.web.shared.urls import preserve_return_to


router = APIRouter()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/features/{feature_key}")
async def update_chatroom_feature_action(request: Request, channel_id: int, chatroom_id: int, feature_key: str):
    """保存群功能的设置(有文本/数字/密钥输入的设置区点「保存」时提交),存进群记录的 extra_json。

    只服务页面脚本:返回 JSON,页面不刷新,脚本弹出成功提示。
    """

    user = enforce_role(request, "ops")
    form = await request.form()
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        try:
            save_group_feature_form(chatroom, feature_key, form)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        db.commit()
        record_audit(db, "channel.chatroom-feature.update", "chatroom", chatroom.id, f"{feature_key} by {user.username}")
        feature = get_group_feature(feature_key)
        return JSONResponse({"ok": True, "notice": f"已保存「{feature.name}」的设置"})
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/features/{feature_key}/toggle")
async def toggle_chatroom_feature_action(request: Request, channel_id: int, chatroom_id: int, feature_key: str):
    """点开关即保存:body 为 {"field": "enabled" | <某个开关配置项>, "value": true|false},只改这一个开关。"""

    user = enforce_role(request, "ops")
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    field = str((body or {}).get("field") or "")
    value = (body or {}).get("value")
    if not field or not isinstance(value, bool):
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        try:
            notice = toggle_group_feature_flag(chatroom, feature_key, field, value)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        db.commit()
        record_audit(db, "channel.chatroom-feature.toggle", "chatroom", chatroom.id, f"{feature_key}.{field}={value} by {user.username}")
        return JSONResponse({"ok": True, "notice": notice})
    finally:
        db.close()


def _group_plugin_settings_target(db: Session, channel_id: int, chatroom_id: int) -> tuple[Channel, Chatroom]:
    """结构化配置与恢复共用群归属校验，跨租户或跨渠道均按资源不存在处理。"""

    channel = get_channel(db, channel_id)
    room = db.get(Chatroom, chatroom_id)
    if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES or not room or room.channel_id != channel.id:
        raise HTTPException(status_code=404, detail='未找到当前群')
    return channel, room


@router.post('/channels/{channel_id}/chatrooms/{chatroom_id}/plugins/{bot_plugin_id}/settings')
async def save_chatroom_plugin_settings_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    """群详情只接收声明式表单，不重新引入已删除的 JSON 覆盖接口。"""

    user = enforce_role(request, 'ops')
    form = await request.form()
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get('X-CSRF-Token') or str(form.get('csrf_token') or ''))
        channel, room = _group_plugin_settings_target(db, channel_id, chatroom_id)
        try:
            binding = save_group_plugin_settings(db, channel, room, bot_plugin_id, form, user.user_id)
        except ValueError as exc:
            db.rollback()
            return JSONResponse({'ok':False, 'notice':str(exc)}, status_code=400)
        db.commit()
        record_audit(db, 'channel.chatroom-plugin.settings', 'group_plugin_scope', bot_plugin_id, f'chatroom={room.id}, operator={user.user_id}')
        if request.headers.get('X-Requested-With') == 'fetch':
            return JSONResponse(jsonable_encoder({'ok':True, 'notice':'本群插件设置已保存', 'settings':group_plugin_settings_view(db, channel, room, binding)}))
        return RedirectResponse(url=preserve_return_to(request, f'/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote("本群插件设置已保存")}'), status_code=303)
    finally:
        db.close()


@router.post('/channels/{channel_id}/chatrooms/{chatroom_id}/plugins/{bot_plugin_id}/settings/reset')
async def reset_chatroom_plugin_settings_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    """仅恢复参数默认值，保留群内启停，避免用户恢复指令时误开启插件。"""

    user = enforce_role(request, 'ops')
    form = await request.form()
    db = get_session()
    try:
        require_tenant_csrf(db, request, request.headers.get('X-CSRF-Token') or str(form.get('csrf_token') or ''))
        channel, room = _group_plugin_settings_target(db, channel_id, chatroom_id)
        try:
            binding = reset_group_plugin_settings(db, channel, room, bot_plugin_id, user.user_id)
        except ValueError as exc:
            db.rollback()
            return JSONResponse({'ok':False, 'notice':str(exc)}, status_code=400)
        db.commit()
        record_audit(db, 'channel.chatroom-plugin.settings.reset', 'group_plugin_scope', bot_plugin_id, f'chatroom={room.id}, operator={user.user_id}')
        if request.headers.get('X-Requested-With') == 'fetch':
            return JSONResponse(jsonable_encoder({'ok':True, 'notice':'配置已恢复默认，本群启停保持不变', 'settings':group_plugin_settings_view(db, channel, room, binding)}))
        return RedirectResponse(url=preserve_return_to(request, f'/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote("配置已恢复默认，本群启停保持不变")}'), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/enable")
async def enable_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            update_wechat_ipad_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=True,
                operator_id=user.user_id,
                audit_note=f"manual enable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为启用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.enable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=preserve_return_to(request, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}"), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/disable")
async def disable_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            update_wechat_ipad_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            update_wechat_winauto_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        else:
            update_workwechat_chatroom_scope_enabled(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                enabled=False,
                operator_id=user.user_id,
                audit_note=f"manual disable by {user.username}",
            )
        db.commit()
        message = "已将该群的插件草稿作用域设为停用"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.disable", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=preserve_return_to(request, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}"), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/reset")
async def reset_wechat_ipad_chatroom_scope_action(request: Request, channel_id: int, chatroom_id: int, bot_plugin_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail="Channel does not support group plugin scopes")
        binding = db.get(BotPlugin, bot_plugin_id)
        if not binding or binding.bot_id != channel.bot_id:
            raise HTTPException(status_code=404, detail="Bot plugin binding not found")
        if channel.channel_type == "wechat_ipad":
            reset_wechat_ipad_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        elif channel.channel_type == "wechat_winauto":
            reset_wechat_winauto_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        else:
            reset_workwechat_chatroom_scope(
                db,
                channel=channel,
                chatroom=chatroom,
                bot_plugin_id=bot_plugin_id,
                operator_id=user.user_id,
                audit_note=f"reset to inherited by {user.username}",
            )
        db.commit()
        message = "已恢复继承 bot_plugin 绑定状态"
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.reset", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}")
        return RedirectResponse(url=preserve_return_to(request, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}"), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{bot_plugin_id}/mode")
async def set_wechat_ipad_chatroom_scope_mode_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    bot_plugin_id: int,
    mode: str = Form(...),
    csrf_token: str = Form(""),
):
    """群内插件的启用/停用/恢复默认。群「插件」标签里的开关是脚本提交(返回 JSON,页面不刷新);恢复默认是整页表单提交。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        channel = db.get(Channel, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Wechat chatroom channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode == "enabled":
            if channel.channel_type == "wechat_ipad":
                update_wechat_ipad_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=True,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为启用",
                )
            message = "群插件已切换为群内启用"
        elif normalized_mode == "disabled":
            if channel.channel_type == "wechat_ipad":
                update_wechat_ipad_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            elif channel.channel_type == "wechat_winauto":
                update_wechat_winauto_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            else:
                update_workwechat_chatroom_scope_enabled(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    enabled=False,
                    operator_id=user.user_id,
                    audit_note="群级开关切换为停用",
                )
            message = "群插件已切换为群内停用"
        elif normalized_mode == "inherited":
            if channel.channel_type == "wechat_ipad":
                reset_wechat_ipad_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            elif channel.channel_type == "wechat_winauto":
                reset_wechat_winauto_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            else:
                reset_workwechat_chatroom_scope(
                    db,
                    channel=channel,
                    chatroom=chatroom,
                    bot_plugin_id=bot_plugin_id,
                    operator_id=user.user_id,
                    audit_note="群级开关恢复继承",
                )
            message = "群插件已恢复继承 bot 级状态"
        else:
            raise HTTPException(status_code=400, detail="Invalid scope mode")
        # 群权限模式是运维人员在页面上直接操作的状态，先显式提交业务变更，再记录审计，避免依赖审计函数的隐式提交。
        db.commit()
        record_audit(db, f"channel.{channel.channel_type}.chatroom-scope.mode", "group_plugin_scope", bot_plugin_id, f"chatroom={chatroom.id}, mode={normalized_mode}")
        if request.headers.get("X-Requested-With") == "fetch":
            # custom:这个群是否单独设置过(启用/停用都算自定义,恢复默认才回到继承),页面据此显示"本群已自定义"和"恢复默认"。
            return JSONResponse({"ok": True, "notice": message, "mode": normalized_mode, "custom": normalized_mode != "inherited"})
        return RedirectResponse(url=preserve_return_to(request, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote(message)}"), status_code=303)
    except ValueError as exc:
        db.rollback()
        # 服务层会在绑定和群不匹配等数据异常时抛 ValueError；这里转成页面提示，避免按钮操作直接落到 500 错误页。
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": False, "notice": f"群权限保存失败：{exc}"}, status_code=400)
        return RedirectResponse(url=preserve_return_to(request, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=plugins&notice={quote('群权限保存失败：' + str(exc))}"), status_code=303)
    finally:
        db.close()
