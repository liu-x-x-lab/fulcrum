"""账号资料、缓存和拉取结果的只读摘要。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _map_recent_call_state_tone
from app.services.wechat_ipad.diagnostics.protocol import _build_recent_call_view
from app.services.wechat_ipad.qr import _build_qr_status_summary


def _build_account_snapshot(runtime_info: dict[str, Any], adapter_config: dict[str, Any]) -> dict[str, str]:
    last_cache_info_response = runtime_info.get("last_cache_info_response")
    cache_payload = last_cache_info_response if isinstance(last_cache_info_response, dict) else {}
    cache_data = WechatIpadApiClient.extract_data(cache_payload) if cache_payload else {}
    if not isinstance(cache_data, dict):
        cache_data = {}
    last_user_profile_response = runtime_info.get("last_user_profile_response")
    profile_payload = last_user_profile_response if isinstance(last_user_profile_response, dict) else {}
    profile_data = WechatIpadApiClient.extract_data(profile_payload) if profile_payload else {}
    if not isinstance(profile_data, dict):
        profile_data = {}
    profile_fields = WechatIpadApiClient.extract_account_fields(profile_payload) if profile_payload else {}
    wxid = str(
        runtime_info.get("active_wxid")
        or profile_fields.get("wxid")
        or profile_data.get("Wxid")
        or profile_data.get("UserName")
        or cache_data.get("Wxid")
        or cache_data.get("UserName")
        or adapter_config.get("wxid")
        or ""
    ).strip() or "-"
    nickname = str(
        runtime_info.get("active_nickname")
        or profile_fields.get("nickname")
        or profile_data.get("NickName")
        or profile_data.get("nickname")
        or cache_data.get("NickName")
        or cache_data.get("nickname")
        or ""
    ).strip() or "-"
    alias = str(
        runtime_info.get("active_alias")
        or profile_fields.get("alias")
        or profile_data.get("Alias")
        or profile_data.get("alias")
        or cache_data.get("Alias")
        or cache_data.get("alias")
        or cache_data.get("Wechat")
        or cache_data.get("WeChat")
        or ""
    ).strip() or "-"
    bind_mobile = str(
        runtime_info.get("active_bind_mobile")
        or profile_fields.get("bind_mobile")
        or profile_data.get("BindMobile")
        or profile_data.get("bind_mobile")
        or cache_data.get("BindMobile")
        or cache_data.get("bind_mobile")
        or cache_data.get("Mobile")
        or cache_data.get("mobile")
        or ""
    ).strip() or "-"
    device_name = str(
        adapter_config.get("device_name")
        or cache_data.get("DeviceName")
        or cache_data.get("device_name")
        or ""
    ).strip() or "-"
    return {
        "wxid": wxid,
        "nickname": nickname,
        "alias": alias,
        "bind_mobile": bind_mobile,
        "device_name": device_name,
    }


def _build_pull_summary(messages: list[dict[str, Any]], sync_fields: dict[str, Any]) -> dict[str, Any]:
    first = messages[0] if messages else {}
    return {
        "message_count": len(messages),
        "continue_flag": sync_fields.get("continue_flag"),
        "sync_key": sync_fields.get("sync_key"),
        "first_event": first.get("Event") if isinstance(first, dict) else None,
    }


def _build_cache_info_view(value: Any, summary: Any, *, user_profile: Any = None, called_at: Any = None, now: datetime | None = None) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    data = WechatIpadApiClient.extract_data(payload)
    cached = summary if isinstance(summary, dict) else {}
    user_profile_summary = user_profile if isinstance(user_profile, dict) else {}
    meta = _build_recent_call_view(payload, called_at=called_at, now=now)
    wxid = str(cached.get("wxid") or data.get("Wxid") or data.get("UserName") or user_profile_summary.get("wxid") or "-")
    nickname = str(cached.get("nickname") or data.get("NickName") or data.get("nickname") or user_profile_summary.get("nickname") or "-")
    device_id = str(cached.get("device_id") or data.get("DeviceID") or "-")
    has_account_summary = any(item != "-" for item in (wxid, nickname, device_id))
    if meta.get("state") == "blank" and has_account_summary:
        meta["state"] = "healthy"
        meta["state_label"] = "调用成功"
        meta["state_tone"] = _map_recent_call_state_tone("healthy")
        meta["operator_hint"] = "最近一次 CacheInfo 已返回账号缓存摘要，可直接核对 wxid、昵称和设备信息。"
    elif meta.get("state") == "healthy" and not has_account_summary:
        meta["state"] = "blank"
        meta["state_label"] = "返回空摘要"
        meta["state_tone"] = _map_recent_call_state_tone("blank")
        meta["operator_hint"] = "最近一次 CacheInfo 没有硬错误，但账号缓存摘要为空；建议关注缓存字段是否缺失或接口回包漂移。"
    meta["operator_summary"] = (
        f"分档={meta.get('state_label', '尚未调用')}；时效={meta.get('freshness_label', '尚无时间样本')}；"
        f"最近调用={meta.get('called_at_display', '-')}；消息={meta.get('message', '-')}；"
        f"wxid={wxid}；昵称={nickname}；device_id={device_id}"
    )
    return {
        "state": meta.get("state", "empty"),
        "state_label": meta.get("state_label", "尚未调用"),
        "state_tone": meta.get("state_tone", _map_recent_call_state_tone("empty")),
        "operator_hint": meta.get("operator_hint", "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"),
        "operator_summary": meta.get("operator_summary", "-"),
        "called_at_display": meta.get("called_at_display", "-"),
        "message": meta.get("message", "-"),
        "code": meta.get("code", "-"),
        "http_status": meta.get("http_status", "-"),
        "wxid": wxid,
        "nickname": nickname,
        "device_id": device_id,
        "freshness_key": meta.get("freshness_key", "no_sample"),
        "freshness_label": meta.get("freshness_label", "尚无时间样本"),
        "freshness_tone": meta.get("freshness_tone", "muted"),
        "payload_json": meta.get("payload_json", "{}"),
        "has_payload": meta.get("has_payload", "false"),
    }


def _extract_user_profile_summary(profile_data: dict[str, Any], profile_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "wxid": profile_fields.get("wxid") or profile_data.get("Wxid") or profile_data.get("UserName"),
        "nickname": profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname"),
        "alias": profile_fields.get("alias") or profile_data.get("Alias") or profile_data.get("alias"),
        "bind_mobile": profile_fields.get("bind_mobile") or profile_data.get("BindMobile") or profile_data.get("bind_mobile"),
    }


def _build_user_profile_view(value: Any, summary: Any, *, called_at: Any = None, now: datetime | None = None) -> dict[str, str]:
    payload = value if isinstance(value, dict) else {}
    data = WechatIpadApiClient.extract_data(payload)
    profile_fields = WechatIpadApiClient.extract_account_fields(payload) if payload else {}
    cached = summary if isinstance(summary, dict) else {}
    meta = _build_recent_call_view(payload, called_at=called_at, now=now)
    wxid = str(cached.get("wxid") or profile_fields.get("wxid") or data.get("Wxid") or data.get("UserName") or "-")
    nickname = str(cached.get("nickname") or profile_fields.get("nickname") or data.get("NickName") or data.get("nickname") or "-")
    alias = str(cached.get("alias") or profile_fields.get("alias") or data.get("Alias") or data.get("alias") or "-")
    bind_mobile = str(cached.get("bind_mobile") or profile_fields.get("bind_mobile") or data.get("BindMobile") or data.get("bind_mobile") or "-")
    meta["operator_summary"] = (
        f"分档={meta.get('state_label', '尚未调用')}；时效={meta.get('freshness_label', '尚无时间样本')}；"
        f"最近调用={meta.get('called_at_display', '-')}；消息={meta.get('message', '-')}；"
        f"wxid={wxid}；昵称={nickname}；微信号={alias}；手机号={bind_mobile}"
    )
    return {
        "state": meta.get("state", "empty"),
        "state_label": meta.get("state_label", "尚未调用"),
        "state_tone": meta.get("state_tone", _map_recent_call_state_tone("empty")),
        "operator_hint": meta.get("operator_hint", "还没有最近调用记录；执行对应动作后，这里会显示结构化结果。"),
        "operator_summary": meta.get("operator_summary", "-"),
        "called_at_display": meta.get("called_at_display", "-"),
        "message": meta.get("message", "-"),
        "code": meta.get("code", "-"),
        "http_status": meta.get("http_status", "-"),
        "wxid": wxid,
        "nickname": nickname,
        "alias": alias,
        "bind_mobile": bind_mobile,
        "freshness_key": meta.get("freshness_key", "no_sample"),
        "freshness_label": meta.get("freshness_label", "尚无时间样本"),
        "freshness_tone": meta.get("freshness_tone", "muted"),
        "payload_json": meta.get("payload_json", "{}"),
        "has_payload": meta.get("has_payload", "false"),
    }


def _build_status_summary(
    qr_result: dict[str, Any] | None,
    heartbeat: dict[str, Any],
    user_profile: dict[str, Any],
) -> str:
    parts = []
    if qr_result:
        parts.append(_build_qr_status_summary(qr_result))
    parts.append("HeartBeat 成功" if heartbeat["ok"] else f"HeartBeat 失败：{heartbeat['error']}")
    if user_profile["ok"]:
        profile_data = user_profile["data"] if isinstance(user_profile.get("data"), dict) else {}
        profile_fields = WechatIpadApiClient.extract_account_fields(user_profile.get("payload") or {})
        nickname = profile_fields.get("nickname") or profile_data.get("NickName") or profile_data.get("nickname")
        parts.append(f"GetContractProfile 成功{f'，昵称 {nickname}' if nickname else ''}")
    elif user_profile["payload"]:
        parts.append(f"GetContractProfile 失败：{user_profile['error']}")
    return "；".join(parts)
