"""租户页面共用的模板环境、时间过滤器和页面上下文。

这里只组装展示数据；表单的角色和 CSRF 校验仍由路由负责。"""

from datetime import datetime, timedelta

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.application import APP_STARTED_AT
from app.core.time import ensure_shanghai_datetime, format_shanghai_time, now_shanghai
from app.services import message_view
from app.services.channel_labels import get_channel_type_label
from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_LOGOUT_PATH, TENANT_PASSWORD_PATH
from app.web.shared.urls import build_context_href, build_module_links
from app.web.tenant_navigation import build_tenant_navigation


templates = Jinja2Templates(directory="app/templates")


def format_beijing_time(value):
    return format_shanghai_time(value)


def format_uptime(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        started_at = ensure_shanghai_datetime(value)
    else:
        return str(value)
    delta = max(now_shanghai() - started_at, timedelta())
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts[:3])


templates.env.filters["bj_time"] = format_beijing_time
templates.env.filters["uptime"] = format_uptime
templates.env.filters["channel_type_label"] = get_channel_type_label
templates.env.filters["message_display"] = lambda value, message_type="": format_message_display(value, message_type)
templates.env.globals["context_href"] = build_context_href


def render(request: Request, template: str, **context):
    notice = str(context.get("notice", "") or "").strip()
    response_status = int(context.pop("response_status", 200) or 200)
    notice_tone = context.get("notice_tone") or request.query_params.get("tone", "")
    if not notice_tone:
        notice_tone = "danger" if response_status >= 400 else infer_notice_tone(notice)
    navigation_context = build_tenant_navigation(request.url.path)
    base_context = {
        "current_user": getattr(request.state, "current_user", None),
        # 所有租户页面共用同一个可信 CSRF Cookie；状态变更表单必须把原始值放入
        # 隐藏字段，由对应路由再与数据库会话摘要进行三方校验。
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "logout_action": TENANT_LOGOUT_PATH,
        "password_action": TENANT_PASSWORD_PATH,
        "app_started_at": APP_STARTED_AT,
        "notice_tone": notice_tone,
        "module_links": build_module_links(),
    }
    base_context.update(navigation_context)
    base_context.update(context)
    response = templates.TemplateResponse(request, template, base_context)
    response.status_code = response_status
    return response


def infer_notice_tone(notice: str) -> str:
    text = str(notice or "").strip()
    if not text:
        return "default"
    error_keywords = ("错误", "失败", "异常", "不正确", "不一致", "不能为空", "不存在", "缺少", "无效", "至少", "请先", "未完成", "离线", "超时", "未执行", "不正常", "不可用", "无法")
    if any(keyword in text for keyword in error_keywords):
        return "danger"
    if any(keyword in text for keyword in ("成功", "完成", "已保存", "已更新", "正常", "已启用", "已停用", "已接入")):
        return "success"
    return "default"


def parse_message_lines(raw_text: str) -> list[str]:
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


def format_message_display(raw_text: object, message_type: object = "") -> str:
    """一行摘要(会话列表、最近消息等):文字取开头,媒体/卡片/引用用 [图片] [引用] 标题 这样的短标签。

    完整内容在消息中心用卡片展示(见 message_view 与 _message_rows.html)。
    """

    return message_view.preview(raw_text, message_type, limit=80)
