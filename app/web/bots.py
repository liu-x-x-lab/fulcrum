"""机器人列表、详情及管理操作，复用统一角色与租户会话边界。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.entities import Bot, BotPlugin, Channel, Conversation
from app.schemas.forms import BotUpdate
from app.services.account_identity import bot_identity
from app.services.crud import delete_bot, get_bot, toggle_bot, update_bot
from app.services.ops import create_alert, record_audit, record_health_check
from app.services.runtime import run_bot_status_check
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import _completed_operation_response, _require_operation_csrf
from app.web.shared.presentation import render
from app.web.shared.urls import detail_fallback, safe_return_to


router = APIRouter()


@router.get("/bots", response_class=HTMLResponse)
async def bots_page(request: Request):
    db = get_session()
    try:
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        # 列表需要稳定的计数键，模板不再触碰已删除的分组、模板、标签和环境关系。
        channels_by_bot: dict[int, list[Channel]] = {}
        for channel in db.scalars(select(Channel).where(Channel.deleted_at.is_(None)).order_by(Channel.id.asc())).all():
            channels_by_bot.setdefault(int(channel.bot_id), []).append(channel)
        bot_rows = [
            {
                "bot": bot,
                "identity": bot_identity(bot.name, channels_by_bot.get(int(bot.id), [])),
                "channel_count": db.scalar(select(func.count(Channel.id)).where(Channel.bot_id == bot.id, Channel.deleted_at.is_(None))) or 0,
                "binding_count": db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot.id)) or 0,
            }
            for bot in bots
        ]
        return render(
            request,
            "bots.html",
            page_title="机器人管理",
            bot_rows=bot_rows,
            bot_count=len(bot_rows),
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
        )
    finally:
        db.close()


@router.get("/bots/{bot_id}", response_class=HTMLResponse)
async def bot_detail_page(request: Request, bot_id: int, return_to: str = ""):
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        channels = db.scalars(select(Channel).where(Channel.bot_id == bot_id, Channel.deleted_at.is_(None)).order_by(Channel.id.desc())).all()
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.status != "unbound").order_by(BotPlugin.id.desc())).all()
        conversation_count = db.scalar(select(func.count(Conversation.id)).where(Conversation.bot_id == bot_id)) or 0
        return render(
            request,
            "bot_detail.html",
            page_title=f"机器人详情 · {bot.name}",
            bot=bot,
            bot_ident=bot_identity(bot.name, list(reversed(channels))),
            channels=channels,
            bindings=bindings,
            channel_count=len(channels),
            binding_count=len(bindings),
            conversation_count=conversation_count,
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            back_href=safe_return_to(return_to, detail_fallback("/bots")),
        )
    finally:
        db.close()


@router.post("/bots/batch-toggle")
async def batch_toggle_bots_action(request: Request, action: str = Form(...)):
    enforce_role(request, "ops")
    db = get_session()
    try:
        form = await request.form()
        bot_ids = [int(value) for value in form.getlist("bot_ids") if str(value).isdigit()]
        if not bot_ids:
            return RedirectResponse(url="/bots?notice=" + quote("请先选择至少一个机器人"), status_code=303)

        bots = db.scalars(select(Bot).where(Bot.id.in_(bot_ids), Bot.deleted_at.is_(None))).all()
        enabled = action == "enable"
        for bot in bots:
            bot.is_enabled = enabled
            bot.status = "running" if enabled else "stopped"
        db.commit()
        record_audit(db, "bot.batch-toggle", "bot", detail=f"action={action}, count={len(bots)}")
        message = f"已批量{'启用' if enabled else '停用'} {len(bots)} 个机器人"
        return RedirectResponse(url="/bots?notice=" + quote(message), status_code=303)
    finally:
        db.close()


@router.post("/bots/{bot_id}/edit")
async def edit_bot_action(
    request: Request,
    bot_id: int,
    name: str = Form(...),
    description: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        cleaned_name = name.strip()
        if not cleaned_name:
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称不能为空") + "&tone=danger",
                status_code=303,
            )
        duplicate_id = db.scalar(select(Bot.id).where(Bot.name == cleaned_name, Bot.id != bot_id))
        if duplicate_id is not None:
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        try:
            update_bot(db, bot, BotUpdate(name=cleaned_name, description=description))
        except IntegrityError:
            db.rollback()
            return RedirectResponse(
                url=f"/bots/{bot_id}?notice=" + quote("机器人名称已存在，请换一个名称") + "&tone=danger",
                status_code=303,
            )
        record_audit(db, "bot.update", "bot", bot.id, f"更新机器人 {bot.name}")
        return RedirectResponse(url=f"/bots/{bot_id}?notice=" + quote("机器人资料已更新"), status_code=303)
    finally:
        db.close()


@router.post("/bots/{bot_id}/toggle")
async def toggle_bot_action(request: Request, bot_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        toggle_bot(db, bot)
        record_audit(db, "bot.toggle", "bot", bot.id, f"enabled={bot.is_enabled}")
        return RedirectResponse(url=f"/bots/{bot_id}", status_code=303)
    finally:
        db.close()


@router.post("/bots/{bot_id}/status-check")
async def check_bot_status_action(request: Request, bot_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        status, message = run_bot_status_check(db, bot)
        record_health_check(
            db,
            "status-check",
            "bot",
            status,
            message,
            target_id=bot.id,
            details={"bot": bot.name},
        )
        if status != "running":
            create_alert(db, "bot-health", f"机器人 {bot.name} 状态异常", message, related_type="bot", related_id=bot.id)
        record_audit(db, "bot.status-check", "bot", bot.id, message)
        return _completed_operation_response(request, message, f"/bots/{bot_id}", ok=status == "running")
    finally:
        db.close()


@router.post("/bots/{bot_id}/delete")
async def delete_bot_action(request: Request, bot_id: int, confirmation_name: str = Form("")):
    enforce_role(request, "admin")
    db = get_session()
    try:
        bot = get_bot(db, bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail="Bot not found")
        expected_name = (bot.name or "").strip()
        submitted_name = (confirmation_name or "").strip()
        if not expected_name or submitted_name != expected_name:
            message = quote(f"删除失败：请输入机器人名称 {expected_name} 进行确认")
            return RedirectResponse(url=f"/bots/{bot_id}?notice={message}&tone=danger", status_code=303)
        name = bot.name
        delete_bot(db, bot)
        record_audit(db, "bot.delete", "bot", bot_id, f"删除机器人 {name}")
        return RedirectResponse(url="/bots?notice=机器人已删除", status_code=303)
    finally:
        db.close()
