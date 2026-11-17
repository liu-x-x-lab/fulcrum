"""安装向导和初始平台管理员创建入口。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.auth import validate_password_change
from app.services.installer import is_setup_required, provision_platform_admin
from app.web.auth_common import PLATFORM_LOGIN_PATH
from app.web.shared.access import get_platform_session
from app.web.shared.presentation import render


router = APIRouter()


def format_setup_database_error(error: str) -> str:
    normalized = str(error or "").strip()
    lowered = normalized.lower()
    if "unknown database" in lowered:
        return f"MariaDB 连接失败：数据库不存在，请先执行建库命令。原始错误：{normalized}"
    if "access denied" in lowered:
        return f"MariaDB 连接失败：账号或密码不正确，或当前用户没有访问该库的权限。原始错误：{normalized}"
    if "can't connect" in lowered or "connection refused" in lowered:
        return f"MariaDB 连接失败：无法连到数据库主机，请检查主机、端口和服务状态。原始错误：{normalized}"
    return f"MariaDB 连接失败：{normalized}"


@router.get("/setup/completed", response_class=HTMLResponse)
async def setup_completed_page(request: Request):
    return render(
        request,
        "setup_completed.html",
        page_title="安装完成",
        notice=request.query_params.get("notice", ""),
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = get_platform_session()
    try:
        if not is_setup_required(db):
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        return render(
            request,
            "setup.html",
            page_title="创建平台管理员",
            notice=request.query_params.get("notice", ""),
            form_data={
                "admin_username": "admin",
                "admin_display_name": "平台管理员",
            },
        )
    finally:
        db.close()


@router.post("/setup")
async def setup_action(
    request: Request,
    admin_username: str = Form(...),
    admin_display_name: str = Form("平台管理员"),
    admin_password: str = Form(...),
    admin_password_confirm: str = Form(...),
):
    form_data = {
        "admin_username": str(admin_username or "").strip(),
        "admin_display_name": str(admin_display_name or "").strip() or "平台管理员",
    }
    db = get_platform_session()
    try:
        if not is_setup_required(db):
            return RedirectResponse(url=PLATFORM_LOGIN_PATH, status_code=303)
        if not form_data["admin_username"]:
            return render(request, "setup.html", page_title="创建平台管理员", notice="管理员用户名不能为空", form_data=form_data, response_status=400)
        if len(form_data["admin_username"]) > 100:
            return render(request, "setup.html", page_title="创建平台管理员", notice="管理员用户名长度不能超过 100", form_data=form_data, response_status=400)
        if admin_password != admin_password_confirm:
            return render(request, "setup.html", page_title="创建平台管理员", notice="两次输入的管理员密码不一致", form_data=form_data, response_status=400)
        password_error = validate_password_change(admin_password)
        if password_error:
            return render(request, "setup.html", page_title="创建平台管理员", notice=password_error, form_data=form_data, response_status=400)
        provision_error = provision_platform_admin(
            db,
            admin_username=form_data["admin_username"],
            admin_password=admin_password,
            admin_display_name=form_data["admin_display_name"],
        )
        if provision_error:
            return render(request, "setup.html", page_title="创建平台管理员", notice=f"创建失败：{provision_error}", form_data=form_data, response_status=500)
    finally:
        db.close()

    return RedirectResponse(
        url=PLATFORM_LOGIN_PATH + "?notice=" + quote("平台管理员已创建，请登录后创建套餐和租户"),
        status_code=303,
    )
