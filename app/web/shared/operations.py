"""异步操作的 CSRF 校验与排队、完成响应。

任务入队只表示接受请求，是否完成必须通过任务状态接口确认。"""

from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.models.entities import Channel, TaskJob
from app.services.operation_feedback import build_operation_status
from app.web.auth_common import require_tenant_csrf


def _operation_json_requested(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("accept", "")


async def _require_operation_csrf(db: Session, request: Request) -> None:
    """同步/健康检查这类会改状态的 POST 一律校验 CSRF,不再只对 fetch 请求校验。

    令牌有两个来源:页面脚本(fetch)放在 X-CSRF-Token 请求头里;没有脚本时的普通表单提交
    放在隐藏字段 csrf_token 里。两处都没有或不匹配时由 require_tenant_csrf 拒绝。
    """

    token = _csrf_from_header(request)
    if not token:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
    require_tenant_csrf(db, request, token)


def _completed_operation_response(request: Request, message: str, redirect_url: str, *, ok: bool = True):
    if _operation_json_requested(request):
        return JSONResponse({"ok": ok, "done": True, "summary": message, "successful": int(ok), "failed": int(not ok), "jobs": [], "refresh": ok})
    separator = "&" if "?" in redirect_url else "?"
    return RedirectResponse(url=f"{redirect_url}{separator}notice={quote(message)}&tone={'success' if ok else 'danger'}", status_code=303)


def _queued_operation_response(request: Request, db: Session, channel: Channel, jobs: list[TaskJob], redirect_url: str):
    result = build_operation_status(db, channel, [int(job.id) for job in jobs])
    job_ids = ",".join(str(job.id) for job in jobs)
    result["status_url"] = f"/channels/{channel.id}/operations/status?job_ids={job_ids}"
    if _operation_json_requested(request):
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    separator = "&" if "?" in redirect_url else "?"
    return RedirectResponse(url=f"{redirect_url}{separator}operation_jobs={job_ids}&operation_channel={channel.id}", status_code=303)


def _csrf_from_header(request: Request) -> str:
    """页面脚本(fetch)提交时,CSRF 令牌放在 X-CSRF-Token 请求头里(令牌由页面 <meta name="csrf-token"> 提供)。"""

    return request.headers.get("X-CSRF-Token", "")
