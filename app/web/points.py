"""积分账户、流水与人工调整的页面入口。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.logging import get_logger
from app.services.ops import record_audit
from app.services.points_admin import (
    adjust_points,
    build_points_accounts,
    build_points_ledger,
    group_options,
)
from app.web.auth_common import require_tenant_csrf
from app.web.chatrooms.directory import _directory_pager_base
from app.web.shared.access import enforce_role, get_session
from app.web.shared.presentation import render


logger = get_logger(__name__)
router = APIRouter()


# 好友检测统一注册独立的持久化 Router，避免旧入口先匹配而跳过代次与结果保存。


@router.get("/points", response_class=HTMLResponse)
async def points_accounts_page(request: Request, q: str = "", group_id: str = "", page: int = 1, notice: str = ""):
    """积分管理:所有群的积分账户,可筛选,并可人工发放或扣减。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        groups = group_options(db)
        # 默认进入第一个群的成员列表:成员(含 0 积分的)都在这里,选到人再发放。
        if not group_id and groups:
            group_id = str(groups[0]["id"])
        directory = build_points_accounts(db, keyword=q, group_id=group_id, page=page)
        return render(
            request,
            "points_accounts.html",
            page_title="积分",
            directory=directory,
            groups=groups,
            pager_base=_directory_pager_base("/points", q=directory["keyword"], group_id=group_id),
            notice=notice,
        )
    finally:
        db.close()


@router.get("/points/ledger", response_class=HTMLResponse)
async def points_ledger_page(request: Request, q: str = "", group_id: str = "", source: str = "", page: int = 1):
    """积分账单:每一笔赚取、消费、转账和人工调整,最新在前。"""

    enforce_role(request, "readonly")
    db = get_session()
    try:
        directory = build_points_ledger(db, keyword=q, group_id=group_id, source=source, page=page)
        return render(
            request,
            "points_ledger.html",
            page_title="积分账单",
            directory=directory,
            groups=group_options(db),
            pager_base=_directory_pager_base("/points/ledger", q=directory["keyword"], group_id=group_id, source=source),
        )
    finally:
        db.close()


@router.post("/points/adjust")
async def points_adjust_action(
    request: Request,
    group_id: str = Form(""),
    user_id: str = Form(""),
    amount: int = Form(0),
    mode: str = Form("grant"),
    note: str = Form(""),
    form_token: str = Form(""),
    csrf_token: str = Form(""),
    back: str = Form("/points"),
):
    """对列表里已选中的某个成员发放或扣减积分,提交后回到原列表位置。"""

    user = enforce_role(request, "ops")
    # 只允许回到积分管理页,防止被当作开放跳转。
    back_url = back if back.startswith("/points") and "//" not in back else "/points"
    separator = "&" if "?" in back_url else "?"
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token)
        signed = abs(int(amount)) * (1 if mode == "grant" else -1)
        result = adjust_points(
            db, group_id=group_id.strip(), user_id=user_id.strip(), amount=signed, operator=user.username, note=note, request_id=form_token.strip()[:64]
        )
        if result.ok:
            # 积分已入账,审计只是附加记录:失败只记日志,不能让页面报错而误导运营重复发放。
            # 审计的 target_id 是整数列,用积分账户主键;成员 id 是 wxid 字符串,只能放进 detail。
            try:
                record_audit(
                    db,
                    "points.adjust",
                    "points_account",
                    (result.account or {}).get("id"),
                    f"group={group_id}, user={user_id}, amount={signed}, operator={user.username}",
                )
            except Exception:
                db.rollback()
                logger.exception("积分调整审计日志写入失败 | group_id=%s user_id=%s", group_id, user_id)
            after = (result.account or {}).get("total_points")
            notice = f"已{'发放' if signed > 0 else '扣减'} {abs(signed)} 积分" + (f",当前余额 {after}" if after is not None else "")
        else:
            notice = result.message
        return RedirectResponse(url=f"{back_url}{separator}notice={quote(notice)}", status_code=303)
    finally:
        db.close()
