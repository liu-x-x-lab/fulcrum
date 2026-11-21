"""读取完整 874 通讯录；好友行与同步摘要在同一事务落库。"""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, IpadFriend, IpadFriendRequest
from app.services.conversation_kinds import is_official_account
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call


def _text(value):
    if isinstance(value, dict):
        value = value.get("string")
    return value.strip() if isinstance(value, str) else ""


def _valid_response(result):
    data = result["data"]
    base = data.get("BaseResponse") or {}
    return result["ok"] and isinstance(base, dict) and base.get("ret", 0) == 0


def contact_pages(api, channel, owner, *, require_flag=False):
    """共享双游标遍历；调用方仅在迭代正常结束后写库，并区分有完整回执的页。"""
    cursor = (0, 0)
    seen = set()
    while cursor not in seen:
        seen.add(cursor)
        response = _safe_call(api.get_contact_list, stage="contact_sync",
                              trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_list,
                              channel=channel, wxid=owner, current_wxcontact_seq=cursor[0],
                              current_chatroom_contact_seq=cursor[1])
        data = response["data"]
        payload = response["payload"]
        base = data.get("BaseResponse")
        verified = (payload.get("http_status", 200) == 200 and payload.get("Success") is True
                    and type(payload.get("Code")) is int and payload["Code"] == 0
                    and isinstance(base, dict) and type(base.get("ret")) is int and base["ret"] == 0)
        if not _valid_response(response) or not any(k in data for k in ("ContactUsernameList", "CountinueFlag", "BaseResponse")):
            raise ValueError("读取通讯录失败")
        names = data.get("ContactUsernameList", [])
        if not isinstance(names, list):
            raise ValueError("通讯录格式错误")
        ids = [_text(raw) for raw in names]
        if any(not ident or len(ident) > 128 for ident in ids):
            raise ValueError("联系人 ID 无效")
        flag = data.get("CountinueFlag", 0)
        if (require_flag and "CountinueFlag" not in data) or type(flag) is not int or flag not in (0, 1):
            raise ValueError("通讯录分页标记异常")
        yield ids, verified
        if flag == 0:
            return
        cursor = (data.get("CurrentWxcontactSeq", 0), data.get("CurrentChatRoomContactSeq", 0))
        if not all(type(n) is int and 0 <= n < 2**31 for n in cursor) or len(seen) >= 10000:
            raise ValueError("通讯录分页异常")
    raise ValueError("通讯录分页游标重复")


def sync_wechat_ipad_contacts(db: Session, channel: Channel, *, client: WechatIpadApiClient | None = None) -> dict:
    api = client or WechatIpadApiClient()
    started = now_shanghai()
    started_at = started.timestamp()
    owner = _resolve_wxid(channel, _parse_json(channel.runtime_info_json), _parse_json(channel.adapter_config_json))

    def fail(reason):
        db.refresh(channel)
        summary = f"好友同步未完成：{reason}"
        return _persist_runtime(db, channel, summary=summary, error=reason, extra={"last_contact_sync_summary": summary})

    if not owner or len(owner) > 128:
        return fail("缺少有效的账号 wxid")
    ids = {}
    verified_pages = True
    try:
        for page, verified in contact_pages(api, channel, owner):
            verified_pages &= verified
            for ident in page:
                if ident != owner and not ident.endswith("@chatroom") and not is_official_account(ident):
                    ids[ident] = None
    except ValueError as exc:
        return fail(str(exc))

    profiles = {}
    ordered = list(ids)
    for offset in range(0, len(ordered), 80):
        batch = ordered[offset:offset + 80]
        response = _safe_call(api.get_contact_detail, stage="contact_detail",
                              trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_detail,
                              channel=channel, wxid=owner, to_wxids=batch, chatroom_id="")
        items = response["data"].get("ContactList")
        if not _valid_response(response) or not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and (ident := _text(item.get("UserName"))) in batch:
                # 不跨批持有协议返回的大图、二进制缓冲或无关资料。
                profiles[ident] = {key: item[key] for key in (
                    "NickName", "Alias", "Remark", "BigHeadImgUrl", "SmallHeadImgUrl",
                    "Signature", "Country", "Province", "City", "Sex",
                ) if key in item}

    # 网络阶段不锁行；写入时重新核对账号，避免并行登录/同步覆盖新身份。
    db.refresh(channel, with_for_update=True)
    if owner != _resolve_wxid(channel, _parse_json(channel.runtime_info_json), _parse_json(channel.adapter_config_json)):
        return fail("同步期间登录账号已变更")
    existing = {row.contact_external_id: row for row in db.scalars(select(IpadFriend).where(
        IpadFriend.tenant_id == channel.tenant_id, IpadFriend.channel_id == channel.id, IpadFriend.owner_wxid == owner
    ).with_for_update())}
    now = now_shanghai()
    for ident in ids:
        row = existing.get(ident)
        if row is None:
            row = IpadFriend(tenant_id=channel.tenant_id, channel_id=channel.id, owner_wxid=owner, contact_external_id=ident)
            db.add(row)
        profile = profiles.get(ident, {})
        row.name = _text(profile.get("NickName"))[:255] or row.name
        row.alias = _text(profile.get("Alias"))[:128] or row.alias
        extra = _parse_json(row.profile_json)
        # 同步读取资料期间可能有人保存了新备注；旧批次仍可补充头像、昵称。
        if "Remark" in profile and started_at > extra.get("remark_verified_at", 0):
            row.remark = _text(profile["Remark"])[:255] or None
        row.avatar_url = _text(profile.get("BigHeadImgUrl")) or _text(profile.get("SmallHeadImgUrl")) or row.avatar_url
        row.display_name = row.remark or row.name or row.alias or ident
        for key in ("Signature", "Country", "Province", "City", "Sex"):
            if key in profile and isinstance(profile[key], (str, int)):
                extra[key.lower()] = profile[key]
        row.profile_json = json.dumps(extra, ensure_ascii=False)
        row.last_synced_at = now
    candidates = select(IpadFriendRequest).where(
        IpadFriendRequest.tenant_id == channel.tenant_id, IpadFriendRequest.channel_id == channel.id,
        IpadFriendRequest.owner_wxid == owner, IpadFriendRequest.status.in_(("submitted", "unknown")),
        IpadFriendRequest.claimed_at < started).with_for_update()
    for request in db.scalars(candidates) if verified_pages and ids else ():
        if request.owner_wxid == owner and request.applicant in ids:
            request.status = "confirmed"
    summary = f"已同步 {len(ids)} 位好友"
    if len(profiles) < len(ids):
        summary += f"，其中 {len(ids) - len(profiles)} 位本次未取到详细资料"
    return _persist_runtime(db, channel, summary=summary,
                            extra={"last_contact_sync_at": now.isoformat(), "last_contact_sync_summary": summary})
