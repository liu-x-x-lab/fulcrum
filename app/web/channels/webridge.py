"""微桥设备面板、实时摘要和设备命令入口。"""

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.services.crud import get_channel
from app.services.account_dashboard import build_account_dashboard
from app.services.wechat_webridge.dashboard import (
    build_webridge_device_panel,
    build_webridge_live_summary,
)
from app.services.wechat_webridge.service import enqueue_command, reap_stale_commands
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import (
    _completed_operation_response,
    _csrf_from_header,
    _queued_operation_response,
)
from app.web.shared.presentation import render


router = APIRouter()


@router.get("/channels/{channel_id}/wechat-webridge", response_class=HTMLResponse)
async def wechat_webridge_dashboard_page(request: Request, channel_id: int, tab: str = "contacts", keyword: str = "", page: int = 1, conversation_id: int = 0, sender: str = ""):
    """展示 WeBridge 渠道内已落库的好友、群组和消息信息。"""

    # 后台页面必须先恢复当前租户身份，避免匿名请求或错误租户上下文读取渠道数据。
    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        # 手机离线时不会有 poll，打开后台页面时也回收一次超时命令，避免页面一直显示进行中。
        reap_stale_commands(db, channel)
        return render(
            request,
            "wechat_webridge_dashboard.html",
            page_title=f"微桥后台 · {channel.name}",
            channel=channel,
            can_edit=True,
            dashboard=build_account_dashboard(db, channel, tab=tab, keyword=keyword, page=page, conversation_id=conversation_id, sender=sender),
            device=build_webridge_device_panel(db, channel),
            live=build_webridge_live_summary(db, channel),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get("/channels/{channel_id}/wechat-webridge/live")
async def wechat_webridge_live_summary(request: Request, channel_id: int):
    """顶部摘要的实时数据（只读 JSON），页面每 10 秒轮询一次。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        return JSONResponse(build_webridge_live_summary(db, channel))
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-webridge/command")
async def wechat_webridge_dashboard_command_action(
    request: Request,
    channel_id: int,
    command_name: str = Form(""),
    args_json: str = Form("{}"),
    csrf_token: str = Form(""),
):
    """从后台直接发起好友、公众号、群组和成员同步命令。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="WeBridge channel not found")
        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError:
            return _completed_operation_response(request, "参数 JSON 无效", f"/channels/{channel_id}/wechat-webridge", ok=False)
        if not isinstance(args, dict):
            return _completed_operation_response(request, "参数必须是 JSON 对象", f"/channels/{channel_id}/wechat-webridge", ok=False)
        # 无筛选的公众号同步走完整分页入口，通用命令桥仍保留用户指定的查询范围。
        if command_name == "official_accounts" and not args:
            from app.services.wechat_webridge import official_accounts

            job = official_accounts.request_sync(db, channel, force=True)
            if job is None:
                raise ValueError("请先完成账号接入并启用渠道")
        else:
            job = enqueue_command(db, channel, command_name=command_name, args=args)
        db.commit()
        return _queued_operation_response(request, db, channel, [job], f"/channels/{channel_id}/wechat-webridge")
    except ValueError as exc:
        return _completed_operation_response(request, str(exc), f"/channels/{channel_id}/wechat-webridge", ok=False)
    finally:
        db.close()
