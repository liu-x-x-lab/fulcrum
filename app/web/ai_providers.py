"""AI 供应商管理；所有变更均受租户管理员、CSRF 和订阅边界约束。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from app.services import ai_providers
from app.services.saas_auth import has_tenant_role
from app.services.tenant_admin import require_tenant_mutation_allowed
from app.web.auth_common import require_tenant_csrf
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.presentation import render

router = APIRouter(prefix="/tenant/ai-providers", tags=["tenant-ai-providers"])


def _redirect(message: str, *, error: bool = False):
    return RedirectResponse(f"/tenant/ai-providers?notice={quote(message)}&tone={'danger' if error else 'success'}", status_code=303)


def _page(request, db, *, form=None, notice="", error=False):
    providers = ai_providers.list_providers(db)
    rows = [{"id": item.id, "name": item.name, "api_base": item.api_base, "enabled": item.enabled,
             "key_configured": bool(item.api_key_encrypted), "models": ai_providers.available_models(item)} for item in providers]
    return render(request, "tenant_ai_providers.html", page_title="AI 供应商", providers=rows, form=form,
                  can_manage=has_tenant_role(require_user(request), "tenant_admin"),
                  notice=notice or request.query_params.get("notice", ""), notice_tone="danger" if error else request.query_params.get("tone", ""))


@router.get("")
def providers_page(request: Request):
    require_user(request)
    with get_session() as db:
        return _page(request, db)


@router.get("/new")
def new_provider_page(request: Request):
    enforce_role(request, "tenant_admin")
    with get_session() as db:
        return _page(request, db, form={"id": "", "name": "", "api_base": "", "enabled": True, "manual_models": ""})


@router.get("/{provider_id}")
def edit_provider_page(provider_id: int, request: Request):
    enforce_role(request, "tenant_admin")
    with get_session() as db:
        try:
            provider = ai_providers.get_provider(db, provider_id)
        except ValueError as exc:
            return _redirect(str(exc), error=True)
        return _page(request, db, form={"id": provider.id, "name": provider.name, "api_base": provider.api_base,
            "enabled": provider.enabled, "manual_models": "\n".join(ai_providers.stored_models(provider.manual_models_json)),
            "key_configured": bool(provider.api_key_encrypted)})


@router.post("/save")
def save_provider_action(request: Request, provider_id: str = Form(""), name: str = Form(""), api_base: str = Form(""),
                         api_key: str = Form(""), enabled: str = Form(""), manual_models: str = Form(""),
                         clear_key: str = Form(""), csrf_token: str = Form("")):
    principal = enforce_role(request, "tenant_admin")
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        require_tenant_mutation_allowed(principal)
        try:
            ai_providers.save_provider(db, provider_id=int(provider_id) if provider_id else None, name=name,
                api_base=api_base, api_key=api_key, enabled=enabled == "on", manual_models=manual_models, clear_key=clear_key == "on")
            db.commit()
        except (ValueError, IntegrityError) as exc:
            db.rollback()
            return _page(request, db, form={"id": provider_id, "name": name, "api_base": api_base, "enabled": enabled == "on",
                "manual_models": manual_models}, notice=str(exc) if isinstance(exc, ValueError) else "供应商名称已存在，请刷新后重试", error=True)
    return _redirect("AI 供应商已保存")


@router.post("/{provider_id}/refresh")
def refresh_provider_models(provider_id: int, request: Request, csrf_token: str = Form("")):
    principal = enforce_role(request, "tenant_admin")
    try:
        with get_session() as db:
            require_tenant_csrf(db, request, csrf_token)
            require_tenant_mutation_allowed(principal)
            provider = ai_providers.get_provider(db, provider_id)
            config = ai_providers.snapshot(provider, principal.tenant_id, "models-catalog")
            if config is None:
                raise ValueError("请先启用供应商并保存密钥")
            revision = provider.revision
        models = ai_providers.fetch_models(config)
        # 请求在数据库会话关闭后执行，回来必须确认地址和密钥未被另一个管理员改变。
        with get_session() as db:
            ai_providers.save_models(db, provider_id, revision, models)
            db.commit()
    except ValueError as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"已读取 {len(models)} 个模型；目录可见不代表支持识图或绘图")


@router.post("/{provider_id}/delete")
def delete_provider_action(provider_id: int, request: Request, csrf_token: str = Form("")):
    principal = enforce_role(request, "tenant_admin")
    with get_session() as db:
        require_tenant_csrf(db, request, csrf_token)
        require_tenant_mutation_allowed(principal)
        try:
            ai_providers.delete_provider(db, ai_providers.get_provider(db, provider_id))
            db.commit()
        except ValueError as exc:
            db.rollback()
            return _redirect(str(exc), error=True)
    return _redirect("AI 供应商已删除")
