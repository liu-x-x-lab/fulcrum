"""媒体库页面及经过租户授权的预览、下载和清理入口。"""

from datetime import date
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.services import media_dedup, media_files, media_library, video_preview
from app.services.ops import record_audit
from app.services.saas_auth import has_tenant_role
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session
from app.web.shared.presentation import render

router = APIRouter()


def _dedup_error(message: str):
    return RedirectResponse("/media?" + urlencode({"notice": message, "tone": "warning"}), 303)


@router.post("/media/deduplicate/scan")
def scan_media(request: Request, channel_id: int = Form(0), csrf_token: str = Form("")):
    user = enforce_role(request, "admin")
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            plan = media_dedup.scan(db, user_id=user.user_id, channel_id=channel_id)
        except ValueError as error:
            return _dedup_error(str(error))
        except OSError:
            return _dedup_error("媒体存储不可用，扫描未完成，请稍后重试")
    return RedirectResponse("/media/deduplicate/" + plan["token"], 303)


@router.post("/media/deduplicate/apply")
def deduplicate_media(request: Request, token: str = Form(""), csrf_token: str = Form("")):
    user = enforce_role(request, "admin")
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            result = media_dedup.apply(db, user_id=user.user_id, token=token)
        except ValueError as error:
            return _dedup_error(str(error))
        except OSError:
            return _dedup_error("媒体存储不可用，请重新扫描后重试")
        record_audit(db, "media.deduplicate", "media", detail=json.dumps(result), actor=str(user.user_id))
    notice = f"已合并 {result['merged']} 个重复副本，释放 {result['saved_bytes']} B；来源与聊天记录保留"
    if result["skipped"]:
        notice += f"；{result['skipped']} 个文件已变化、受保护或未能处理，请重新扫描"
    return RedirectResponse("/media?" + urlencode({"notice": notice, "tone": "warning" if result["skipped"] else "success"}), 303)


@router.get("/media/deduplicate/{token}")
def review_deduplication(request: Request, token: str):
    user = enforce_role(request, "admin")
    try:
        plan = media_dedup.review(user.user_id, token)
    except ValueError as error:
        return _dedup_error(str(error))
    return render(request, "media_deduplicate.html", page_title="整理重复媒体", plan=plan)


@router.get("/media")
def media_page(request: Request, channel_id: int = 0, kind: str = "", source: str = "", query: str = "",
               start: str = "", end: str = "", page: int = 1):
    user = enforce_role(request, "readonly")
    if kind and kind not in media_library.KINDS or source and source not in media_library.SOURCES or len(query) > 100:
        raise HTTPException(400, "筛选条件无效")
    try:
        for value in (start, end):
            if value:
                date.fromisoformat(value)
        if start and end and start > end:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "日期范围无效") from None
    with get_session() as db:
        filters = dict(channel_id=channel_id, kind=kind, source=source, query=query.strip(), start=start, end=end)
        result = media_library.catalog(db, **filters, page=page)
        return render(request, "media_library.html", page_title="媒体库", library=result, filters=filters,
            kinds=media_library.KINDS, sources=media_library.SOURCES,
            previous_url="/media?" + urlencode({**filters, "page": result["page"] - 1}),
            next_url="/media?" + urlencode({**filters, "page": result["page"] + 1}),
            can_manage=has_tenant_role(user, "tenant_admin"), notice=request.query_params.get("notice", ""))


@router.get("/media/items/{reference}/cover")
def video_cover(request: Request, reference: str):
    enforce_role(request, "readonly")
    with get_session() as db:
        try:
            item = media_library.resolve(db, reference)
            if item.kind != "video":
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise HTTPException(404, "视频不存在或无权访问") from None
    # 复用原件授权；解码在数据库会话外执行，客户端不提供路径和指纹。
    try:
        path = video_preview.thumbnail(item.path)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return FileResponse(path, media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.get("/media/items/{reference}/file")
def media_file(request: Request, reference: str, download: bool = False, preview: bool = False):
    enforce_role(request, "readonly")
    with get_session() as db:
        try:
            item = media_library.resolve(db, reference)
            mime = media_files.content_type(item.path, item.mime)
        except (OSError, ValueError, TypeError):
            raise HTTPException(404, "媒体不存在或无权访问") from None
    # 授权与媒体归属已验证；本地解码不占用数据库会话。
    path = item.path
    if preview and not download and item.kind == "audio":
        try:
            path, mime = media_files.audio_preview(path, mime)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(422, "音频文件暂不可用，请刷新后重试") from None
    inline = mime.startswith(("image/", "audio/", "video/")) and mime != "image/svg+xml"
    return FileResponse(path, media_type=mime, filename=item.name,
        content_disposition_type="inline" if inline and not download else "attachment",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox"})


@router.post("/media/delete")
def delete_media(request: Request, refs: list[str] = Form(...), csrf_token: str = Form("")):
    user = enforce_role(request, "admin")
    if not 1 <= len(refs) <= 100 or any(len(reference) > 80 for reference in refs):
        raise HTTPException(400, "一次请选择 1 到 100 个文件")
    deleted = 0
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            items = [media_library.resolve(db, reference) for reference in dict.fromkeys(refs)]
            items = list({str(item.path): item for item in items}.values())
            for item in items:
                media_library.check_delete(db, item)
        except (OSError, ValueError, TypeError):
            return RedirectResponse("/media?" + urlencode({"notice": "未删除：包含不可访问、刚保存或仍在使用的文件，请刷新后重试", "tone": "danger"}), 303)
        for item in items:
            try:
                media_library.check_delete(db, item)
                media_library.delete_file(db, item)
                record_audit(db, "media.delete", "media", detail=item.ref, actor=str(user.user_id))
                deleted += 1
            except (OSError, ValueError):
                db.rollback()
        notice = f"已删除 {deleted} 个本地文件，聊天记录保留"
        if deleted != len(items):
            notice += f"；{len(items) - deleted} 个文件未能删除，请刷新后重试"
    return RedirectResponse("/media?" + urlencode({"notice": notice, "tone": "success" if deleted == len(items) else "warning"}), 303)
