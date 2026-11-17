"""联系人增量同步及协议联系人字段归一化。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid, _utcnow_iso
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call


def sync_wechat_ipad_contacts(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    api_client = client or WechatIpadApiClient()
    adapter_config = _parse_json(channel.adapter_config_json)
    runtime_info = _parse_json(getattr(channel, "runtime_info_json", None))
    wxid = _resolve_wxid(channel, runtime_info, adapter_config)
    if not wxid:
        return _persist_runtime(
            db,
            channel,
            summary="联系人同步失败：缺少 wxid。",
            error="missing_wxid",
            extra={"last_contact_sync_summary": "联系人同步失败：缺少 wxid。"},
        )

    safe_limit = max(1, min(int(limit or 200), 500))
    list_result = _safe_call(
        api_client.get_contact_list,
        stage="contact_sync",
        trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_list,
        channel=channel,
        wxid=wxid,
        current_wxcontact_seq=0,
        current_chatroom_contact_seq=0,
    )
    if not list_result.get("ok"):
        summary = f"联系人同步失败：{str(list_result.get('error') or 'provider_error')}"
        return _persist_runtime(
            db,
            channel,
            summary=summary,
            error=str(list_result.get("error") or "provider_error"),
            error_context=list_result.get("error_context"),
            extra={"last_contact_sync_summary": summary},
        )

    list_data = list_result.get("data") if isinstance(list_result.get("data"), dict) else {}
    normalized_contacts = [_normalize_wechat_contact_item(item) for item in _extract_contact_items(list_data)]
    normalized_contacts = [item for item in normalized_contacts if item]
    if len(normalized_contacts) > safe_limit:
        normalized_contacts = normalized_contacts[:safe_limit]

    personal_contacts = [item for item in normalized_contacts if item.get("is_personal_contact")]
    wxids_for_detail = [str(item.get("wxid") or "").strip() for item in personal_contacts if str(item.get("wxid") or "").strip()]
    wxids_for_detail = wxids_for_detail[: min(len(wxids_for_detail), 80)]

    detail_map: dict[str, dict[str, Any]] = {}
    if wxids_for_detail:
        detail_result = _safe_call(
            api_client.get_contact_detail,
            stage="contact_detail",
            trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_detail,
            channel=channel,
            wxid=wxid,
            to_wxids=wxids_for_detail,
            chatroom_id="",
        )
        if detail_result.get("ok"):
            detail_data = detail_result.get("data") if isinstance(detail_result.get("data"), dict) else {}
            for item in _extract_contact_items(detail_data):
                normalized = _normalize_wechat_contact_item(item)
                detail_wxid = str(normalized.get("wxid") or "").strip()
                if detail_wxid:
                    detail_map[detail_wxid] = normalized

    merged_contacts: list[dict[str, Any]] = []
    for item in normalized_contacts:
        wxid_value = str(item.get("wxid") or "").strip()
        if wxid_value and wxid_value in detail_map:
            merged_contacts.append(_merge_contact_fields(item, detail_map[wxid_value]))
        else:
            merged_contacts.append(item)

    merged_personal = [item for item in merged_contacts if item.get("is_personal_contact")]
    chatroom_count = max(len(merged_contacts) - len(merged_personal), 0)
    seq = _extract_contact_seq_fields(list_data)
    synced_at = _utcnow_iso()
    summary = (
        f"联系人同步完成：个人联系人 {len(merged_personal)} 个，"
        f"群/其他联系人 {chatroom_count} 个。"
    )
    snapshot = {
        "synced_at": synced_at,
        "wxid": wxid,
        "total_count": len(merged_contacts),
        "personal_count": len(merged_personal),
        "chatroom_count": chatroom_count,
        "seq": seq,
        "contacts": merged_contacts,
    }
    return _persist_runtime(
        db,
        channel,
        summary=summary,
        error=None,
        extra={
            "last_contact_sync_at": synced_at,
            "last_contact_sync_summary": summary,
            "last_contact_sync_snapshot": snapshot,
        },
    )


def _extract_contact_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[tuple[int, int, list[Any]]] = []
    for key, value in data.items():
        if isinstance(value, list):
            score = 1 if any(token in str(key).lower() for token in ("contact", "friend", "wxcontact", "wx_friend")) else 0
            candidates.append((score, len(value), value))
            continue
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if not isinstance(nested_value, list):
                    continue
                compound = f"{key}.{nested_key}".lower()
                score = 1 if any(token in compound for token in ("contact", "friend", "wxcontact", "wx_friend")) else 0
                candidates.append((score, len(nested_value), nested_value))
    if not candidates:
        return []
    selected = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[0][2]
    return [item for item in selected if isinstance(item, dict)]


def _extract_contact_seq_fields(data: dict[str, Any]) -> dict[str, int]:
    wx_seq = _pick_int(data, ("CurrentWxcontactSeq", "WxcontactSeq", "ContactSeq", "CurrentContactSeq"))
    chatroom_seq = _pick_int(
        data,
        ("CurrentChatroomContactSeq", "ChatroomContactSeq", "CurrentChatroomSeq", "ChatroomSeq"),
    )
    return {
        "current_wxcontact_seq": wx_seq,
        "current_chatroom_contact_seq": chatroom_seq,
    }


def _pick_int(data: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    for value in data.values():
        if not isinstance(value, dict):
            continue
        for key in keys:
            raw = value.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return 0


def _normalize_wechat_contact_item(item: dict[str, Any]) -> dict[str, Any]:
    wxid = str(
        item.get("Wxid")
        or item.get("wxid")
        or item.get("UserName")
        or item.get("user_name")
        or item.get("username")
        or item.get("UserId")
        or ""
    ).strip()
    nickname = str(item.get("NickName") or item.get("nickname") or item.get("Nick") or "").strip()
    remark = str(item.get("Remark") or item.get("remark") or item.get("RemarkName") or item.get("DisplayName") or "").strip()
    alias = str(item.get("Alias") or item.get("alias") or item.get("WxNumber") or item.get("wx_number") or "").strip()
    gender_raw = item.get("Sex") if item.get("Sex") is not None else item.get("sex")
    gender = _normalize_contact_gender(gender_raw)
    province = str(item.get("Province") or item.get("province") or "").strip()
    city = str(item.get("City") or item.get("city") or "").strip()
    country = str(item.get("Country") or item.get("country") or "").strip()
    signature = str(item.get("Signature") or item.get("signature") or "").strip()
    avatar = str(
        item.get("BigHeadImgUrl")
        or item.get("HeadImgUrl")
        or item.get("avatar")
        or item.get("Avatar")
        or item.get("smallHeadImgUrl")
        or ""
    ).strip()
    is_chatroom = wxid.endswith("@chatroom")
    display_name = remark or nickname or alias or wxid or "-"
    region_parts = [part for part in (country, province, city) if part]
    return {
        "wxid": wxid,
        "display_name": display_name,
        "nickname": nickname or "-",
        "remark": remark or "-",
        "alias": alias or "-",
        "gender": gender,
        "region": " / ".join(region_parts) if region_parts else "-",
        "signature": signature or "-",
        "avatar": avatar,
        "is_chatroom": is_chatroom,
        "is_personal_contact": bool(wxid) and not is_chatroom,
    }


def _normalize_contact_gender(value: Any) -> str:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return str(value).strip() or "-"
    if normalized == 1:
        return "男"
    if normalized == 2:
        return "女"
    return "未知"


def _merge_contact_fields(primary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for field in ("display_name", "nickname", "remark", "alias", "gender", "region", "signature", "avatar"):
        detail_value = str(detail.get(field) or "").strip()
        if detail_value and detail_value != "-":
            merged[field] = detail_value
    return merged
