"""One inbox for both friend directories."""

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy import func, select, tuple_

from app.models.entities import Channel, IpadFriendRequest as Row
from app.services.account_identity import account_identity
from app.services.directory_pages import PAGE_SIZE, _page
from app.services.ops import record_audit
from app.services.saas_auth import has_tenant_role
from app.services.wechat_ipad.operations.friend_requests import LABELS, accept, active_channels
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import _completed_operation_response
from app.web.shared.presentation import render
from app.web.shared.urls import safe_return_to


router = APIRouter()


@router.get("/contacts/requests")
def inbox(request: Request, channel_id: int = 0, page: int = 1, return_to: str = ""):
    user = enforce_role(request, "readonly")
    with get_session() as db:
        channels = list(db.scalars(active_channels(user.tenant_id).order_by(Channel.id)))
        owners = {c.id: account_identity(c)["external_id"] for c in channels if not channel_id or c.id == channel_id}
        # Only presentation fields enter the template, never credential material.
        stmt = select(Row.id, Row.channel_id, Row.applicant, Row.nickname, Row.status, Row.received_at).where(
            Row.tenant_id == user.tenant_id, tuple_(Row.channel_id, Row.owner_wxid).in_(
                [(cid, owner) for cid, owner in owners.items() if owner]))
        paging = _page(db.scalar(select(func.count()).select_from(stmt.subquery())), page)
        rows = db.execute(stmt.order_by((Row.status == "pending").desc(), Row.id.desc())
                          .limit(PAGE_SIZE).offset(paging["offset"])).all()
        return render(request, "friend_requests.html", page_title="好友申请", rows=rows, paging=paging,
                      labels=LABELS, channels=channels, channel_id=channel_id, names={c.id: c.name for c in channels},
                      enabled={c.id for c in channels if c.is_enabled and c.bot.is_enabled and c.api_base},
                      can_edit=has_tenant_role(user, "operator"), back_href=safe_return_to(return_to, "/contacts"),
                      notice=request.query_params.get("notice", ""))


@router.post("/channels/{channel_id}/wechat-ipad/requests/{request_id}/accept")
def accept_request(request: Request, channel_id: int, request_id: int, csrf_token: str = Form(""),
                   return_to: str = Form("")):
    user = enforce_role(request, "ops")
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        result = accept(db, user.tenant_id, channel_id, request_id)
        if result is None:
            raise HTTPException(404, "当前账号下未找到可操作的申请")
        ok, summary = result
        record_audit(db, "channel.wechat-ipad.friend-request", "ipad_friend_request", request_id, summary, actor=user.username)
        return _completed_operation_response(request, summary, safe_return_to(return_to, "/contacts/requests"), ok=ok)
