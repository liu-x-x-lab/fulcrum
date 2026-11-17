"""朋友圈页面、动态刷新和授权媒体读取入口。"""

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, MomentsMedia, MomentsPost
from app.services import moments_media
from app.services.crud import get_channel
from app.services.ops import record_audit
from app.services.saas_auth import has_tenant_role
from app.services.wechat_webridge import moments as webridge_moments
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.operations import _csrf_from_header
from app.web.shared.presentation import render, templates


router = APIRouter()


def _moments_channels(db: Session) -> list[Channel]:
    """可查看朋友圈的账号:个人微信(微桥)账号,不含已删除的。"""

    return list(db.scalars(select(Channel).where(Channel.channel_type == "wechat_webridge", Channel.deleted_at.is_(None)).order_by(Channel.id.asc())).all())


def _moments_channel(db: Session, channel_id: int) -> Channel:
    channel = get_channel(db, channel_id) if channel_id else None
    if channel is None or channel.channel_type != "wechat_webridge":
        raise HTTPException(status_code=404, detail="WeBridge channel not found")
    return channel


def _moments_post(db: Session, post_id: int) -> MomentsPost:
    post = db.get(MomentsPost, int(post_id)) if post_id else None
    if post is None:
        raise HTTPException(status_code=404, detail="Moments post not found")
    return post


@router.post("/moments/media/load")
async def load_moments_media(request: Request, post_id: int = Form(...), mid: str = Form(...)):
    """点击朋友圈里的图片/视频占位卡片:让手机下载并解密,再传回服务器保存(已有文件或已在获取中则直接返回当前状态)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        try:
            outcome = moments_media.request(db, _moments_post(db, post_id), mid)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc), "state": "failed", "url": "", "error": str(exc)}, status_code=400)
        db.commit()
        return JSONResponse({"ok": outcome["state"] != "failed", **outcome, "notice": outcome["error"]})
    finally:
        db.close()


@router.get("/moments/media/status")
async def moments_media_status(request: Request, post_id: int, mid: str):
    """朋友圈媒体获取进度(页面轮询):none / loading / ready(带地址)/ failed(带原因)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        post = _moments_post(db, post_id)
        outcome = moments_media.status(db, moments_media.find(db, post.id, mid))
        db.commit()  # 状态检查会把失败/超时落实到记录上
        return JSONResponse({"ok": True, **outcome})
    finally:
        db.close()


@router.get("/moments/media/file/{media_id}")
async def moments_media_file(request: Request, media_id: int):
    """朋友圈媒体文件(手机传回并保存的副本)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        row = db.get(MomentsMedia, int(media_id))
        if row is None or row.status != "downloaded" or not row.storage_path or not Path(row.storage_path).is_file():
            raise HTTPException(status_code=404, detail="Media not found")
        return FileResponse(row.storage_path, media_type=row.mime_type or "application/octet-stream", headers={"Cache-Control": "private, max-age=3600"})
    finally:
        db.close()


@router.get("/moments", response_class=HTMLResponse)
async def moments_page(request: Request, channel_id: int = 0):
    """朋友圈:查看从个人微信账号取回并缓存的朋友圈动态;「刷新」「加载更早」向手机发起取回。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channels = _moments_channels(db)
        channel = next((item for item in channels if int(item.id) == int(channel_id)), channels[0] if channels else None)
        feed = webridge_moments.build_feed(db, channel) if channel else None
        return render(
            request,
            "moments.html",
            page_title="朋友圈",
            channels=channels,
            channel=channel,
            feed=feed,
            summary=webridge_moments.summary(db, channel) if channel else None,
            can_refresh=has_tenant_role(require_user(request), "operator"),
        )
    finally:
        db.close()


@router.get("/moments/feed")
async def moments_feed(request: Request, channel_id: int, before_id: int = 0):
    """朋友圈的增量加载:返回渲染好的动态卡片 HTML,页面直接追加(或整体替换)。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channel = _moments_channel(db, channel_id)
        feed = webridge_moments.build_feed(db, channel, before_id=max(before_id, 0))
        macro = templates.env.get_template("_moment_posts.html").module.moment_posts
        html = str(macro(feed["posts"], feed["identities"])) if feed["posts"] else ""
        return JSONResponse({"html": html, "has_more": feed["has_more"], "last_id": feed["posts"][-1]["id"] if feed["posts"] else 0, "count": len(feed["posts"])})
    finally:
        db.close()


@router.get("/moments/status")
async def moments_status(request: Request, channel_id: int):
    """刷新进度(页面刷新后轮询):idle / running / done / failed,以及新增条数、缓存总数。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        channel = _moments_channel(db, channel_id)
        info = webridge_moments.summary(db, channel)
        refreshed = info["refreshed_at"]
        return JSONResponse({**info, "ok": True, "refreshed_at": refreshed.strftime("%Y-%m-%d %H:%M:%S") if refreshed else ""})
    finally:
        db.close()


@router.post("/moments/refresh")
async def moments_refresh(request: Request, channel_id: int = Form(...), direction: str = Form("head")):
    """向手机发起一次朋友圈取回(head 刷新最新 / tail 取更早的)。脚本提交,返回 JSON;结果由 /moments/status 查询。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        channel = _moments_channel(db, channel_id)
        try:
            outcome = webridge_moments.request_refresh(db, channel, direction)
        except ValueError as exc:
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        db.commit()
        if outcome["started"]:
            record_audit(db, "moments.refresh", "channel", channel.id, f"direction={direction}")
            return JSONResponse({"ok": True, "started": True, "notice": "正在从手机取回朋友圈…"})
        return JSONResponse({"ok": True, "started": False, "notice": outcome["reason"]})
    finally:
        db.close()
