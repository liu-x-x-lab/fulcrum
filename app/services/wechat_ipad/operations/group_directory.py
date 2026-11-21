"""从已登录账号的完整通讯录发现群；不把目录缺席当成退群或成员变动。"""

import json

from app.core.time import now_shanghai
from app.services.chatroom_directory import get_or_create_chatroom
from app.services.chatroom_registry import mark_chatroom_discovered
from app.services.wechat_ipad.chatrooms import _normalize_chatroom_profile
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.common import _parse_json, _resolve_wxid
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad.operations.contacts import contact_pages
from app.services.wechat_ipad.runtime import _persist_runtime
from app.services.wechat_ipad.transport import _safe_call


def sync_group_directory(db, channel):
    api = WechatIpadApiClient()
    started = now_shanghai()

    def identity():
        return (channel.tenant_id, channel.bot_id,
                _resolve_wxid(channel, _parse_json(channel.runtime_info_json), _parse_json(channel.adapter_config_json)))

    initial = identity()
    owner = initial[-1]

    def fail(reason):
        db.refresh(channel, with_for_update=True)
        summary = f"群目录同步未完成：{reason}"
        if initial != identity() or channel.deleted_at or not channel.is_enabled or channel.channel_type != "wechat_ipad":
            # 旧请求可以向当前操作者报错，但不能污染新登录账号的诊断记录。
            return {"last_status_summary": summary, "runtime_info": {"last_error": reason}}
        return _persist_runtime(db, channel, summary=summary, error=reason,
                                extra={"last_group_directory_summary": summary})

    if not channel.is_enabled or channel.deleted_at or not owner or len(owner) > 128:
        return fail("请先启用渠道并登录账号")
    rooms = {}
    try:
        for page, verified in contact_pages(api, channel, owner, require_flag=True):
            if not verified:
                raise ValueError("通讯录分页回执不完整")
            for ident in page:
                if ident.endswith("@chatroom"):
                    if ident == "@chatroom" or "," in ident or not ident.isprintable() or any(c.isspace() for c in ident):
                        raise ValueError("通讯录中的群 ID 无效")
                    rooms[ident] = None
    except ValueError as exc:
        return fail(str(exc))

    ordered = list(rooms)
    for offset in range(0, len(ordered), 80):
        batch = ordered[offset:offset + 80]
        result = _safe_call(api.get_contact_detail, channel=channel, wxid=owner, to_wxids=batch,
                            stage="group_directory_names", trace_endpoint=IPAD_874_CONTRACT.endpoints.friend_get_detail)
        if result["ok"] and result["payload"].get("http_status", 200) == 200:
            for ident in batch:
                rooms[ident] = _normalize_chatroom_profile(result["payload"], ident).get("name")

    # 网络请求阶段不持写锁。落库前重读账号，防止旧账号的回执写入已切换的渠道。
    db.refresh(channel, with_for_update=True)
    if initial != identity() or channel.deleted_at or not channel.is_enabled or channel.channel_type != "wechat_ipad":
        return fail("同步期间账号已变更或停用")
    created_count = 0
    for ident, name in rooms.items():
        room, created = get_or_create_chatroom(db, channel, ident, status="pending_sync")
        if created:
            created_count += 1
            mark_chatroom_discovered(db, channel=channel, chatroom=room, seen_at=started, enable_management=True)
        if name:
            extra = _parse_json(room.extra_json)
            # 这个接口只同步名称，不能推进完整群资料或成员快照的版本。
            if started.timestamp() > max(extra.get("ipad_directory_name_at", -1), extra.get("ipad_profile_at", -1)):
                room.name = name[:255]
                room.extra_json = json.dumps({**extra, "ipad_directory_name_at": started.timestamp()}, ensure_ascii=False)
    missing = sum(not name for name in rooms.values())
    summary = f"已同步群目录 {len(rooms)} 个群，新增 {created_count} 个。"
    if missing:
        summary += f"其中 {missing} 个本次未取得群名。"
    return _persist_runtime(db, channel, summary=summary,
                            extra={"last_group_directory_summary": summary, "last_group_directory_count": len(rooms)})
