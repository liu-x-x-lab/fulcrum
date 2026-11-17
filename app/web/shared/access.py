"""浏览器请求的身份、角色与数据库会话边界。

租户会话必须在认证中间件恢复的可信作用域内打开；禁止从表单推导租户。"""

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core import db as database
from app.core.tenant_db import open_tenant_session
from app.services.saas_auth import TenantPrincipal, has_tenant_role


def get_session() -> Session:
    """返回绑定当前已认证租户的业务会话。"""

    return open_tenant_session(database.TenantSessionLocal)


def get_platform_session() -> Session:
    """返回显式平台会话；结构校验只在应用启动阶段执行一次。"""

    return database.SessionLocal()


def require_user(request: Request) -> TenantPrincipal:
    """返回中间件从 SaaS 数据库会话恢复的可信租户身份。"""

    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def enforce_role(request: Request, required_role: str) -> TenantPrincipal:
    """兼容旧业务路由的角色名称，并在服务端按新租户角色层级强制鉴权。"""

    user = require_user(request)
    # 现有业务路由仍使用 admin/ops/readonly 三档名称；在完成页面租户化前统一映射，
    # 避免把客户端展示角色或旧 AdminUser Cookie 当成新 SaaS 权限依据。
    tenant_role = {
        "admin": "tenant_admin",
        "ops": "operator",
        "readonly": "readonly",
    }.get(required_role, required_role)
    if not has_tenant_role(user, tenant_role):
        raise HTTPException(status_code=403, detail="Permission denied")
    return user
