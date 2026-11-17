"""站内链接和返回地址处理，集中约束跳转边界。"""

from urllib.parse import urlencode, urlsplit

from fastapi import Request


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
    candidate = (value or "").strip()
    if not candidate:
        return fallback
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


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
