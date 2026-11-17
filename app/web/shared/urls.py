"""站内链接和返回地址处理，集中约束跳转边界。"""

from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Request
from starlette.datastructures import URL


def build_url(path: str, **params: object) -> str:
    filtered = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, int) and value == 0:
            continue
        filtered[key] = value
    if not filtered:
        return path
    return f"{path}?{urlencode(filtered)}"


def current_path_with_query(request: Request) -> str:
    return f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path


def safe_return_to(value: str | None, fallback: str) -> str:
    """返回链接只接受站内路径，供主页面和独立路由共同使用。"""

    candidate = (value or "").strip()
    if not candidate:
        return fallback
    # 浏览器会把反斜杠当作斜杠，不能让返回链接绕过站内地址校验。
    if "\\" in candidate or any(ord(char) < 32 for char in candidate):
        return fallback
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return fallback
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def preserve_return_to(request: Request, target: str) -> str:
    """详情表单跳转后继续携带来源；只从同源的当前详情页补取返回地址。"""

    return_to = safe_return_to(request.query_params.get("return_to"), "")
    if not return_to:
        try:
            referer = URL(request.headers.get("referer", ""))
            if (referer.scheme, referer.netloc, referer.path) == (request.url.scheme, request.url.netloc, URL(target).path):
                return_to = safe_return_to(dict(parse_qsl(referer.query)).get("return_to"), "")
        except ValueError:
            pass
    return str(URL(target).include_query_params(return_to=return_to)) if return_to else target


def build_context_href(path: str, return_to: str = "") -> str:
    """生成站内链接；机器人环境已删除，只保留必要的返回地址。"""

    return build_url(path, return_to=return_to)


def build_module_links() -> dict[str, str]:
    """集中提供不携带旧环境参数的模块入口。"""

    return {
        "dashboard": "/dashboard",
        "bots": "/bots",
        "channels": "/channels",
        "plugins": "/plugins",
        "plugin_runtime": "/plugins/runtime",
        "messages": "/messages",
    }


def detail_fallback(path: str) -> str:
    return path
