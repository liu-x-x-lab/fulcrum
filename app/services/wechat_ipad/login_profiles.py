"""登录设备类型的选择和配置组装，不发起协议调用。"""

from __future__ import annotations

from typing import Any

from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.constants import WECHAT_IPAD_LOGIN_PROFILE_KEYS
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


def _resolve_login_profile_key(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in WECHAT_IPAD_LOGIN_PROFILE_KEYS:
        return normalized
    return "ipad"


def _default_device_name_for_profile(profile_key: str) -> str:
    return "fulcrum iPad"


def _resolve_login_profiles(adapter_config: dict[str, Any]) -> dict[str, dict[str, str]]:
    legacy_profiles = adapter_config.get("login_profiles")
    legacy_profile_map = legacy_profiles if isinstance(legacy_profiles, dict) else {}
    legacy_ipad_profile = legacy_profile_map.get("ipad") if isinstance(legacy_profile_map.get("ipad"), dict) else {}
    legacy_car_profile = legacy_profile_map.get("car") if isinstance(legacy_profile_map.get("car"), dict) else {}
    legacy_device_name = str(
        adapter_config.get("device_name")
        or legacy_ipad_profile.get("device_name")
        or legacy_car_profile.get("device_name")
        or ""
    ).strip()
    legacy_device_id = str(
        adapter_config.get("device_id")
        or legacy_ipad_profile.get("device_id")
        or legacy_car_profile.get("device_id")
        or ""
    ).strip()
    shared_device_name = legacy_device_name or _default_device_name_for_profile("ipad")
    shared_device_id = legacy_device_id
    normalized_profiles: dict[str, dict[str, str]] = {}
    for key in WECHAT_IPAD_LOGIN_PROFILE_KEYS:
        normalized_profiles[key] = {
            "device_name": shared_device_name,
            "device_id": shared_device_id,
        }
    return normalized_profiles


def _apply_login_profile_selection(
    channel: Channel,
    adapter_config: dict[str, Any],
    runtime_info: dict[str, Any],
    *,
    requested_profile: Any = None,
) -> tuple[str, str, str]:
    login_profiles = _resolve_login_profiles(adapter_config)
    profile_key = _resolve_login_profile_key(
        requested_profile or runtime_info.get("selected_login_profile") or adapter_config.get("selected_login_profile")
    )
    shared = login_profiles.get("ipad") or login_profiles.get(profile_key) or {}
    device_name = str(shared.get("device_name") or _default_device_name_for_profile("ipad")).strip()
    device_id = str(shared.get("device_id") or "").strip()
    if not device_id:
        device_id = WechatIpadApiClient.create_device_id(seed=f"channel-{channel.id}-shared-{device_name}")
    adapter_config.pop("login_profiles", None)
    adapter_config["selected_login_profile"] = profile_key
    adapter_config["device_name"] = device_name
    adapter_config["device_id"] = device_id
    runtime_info["selected_login_profile"] = profile_key
    return profile_key, device_name, device_id


def _login_init_endpoint_for_profile(profile_key: str) -> str:
    """按已确认的登录终端选择固定 874 取码接口，不读取任何旧端点覆盖。"""

    if profile_key == "car":
        return IPAD_874_CONTRACT.endpoints.login_get_qr_car
    return IPAD_874_CONTRACT.endpoints.login_get_qr
