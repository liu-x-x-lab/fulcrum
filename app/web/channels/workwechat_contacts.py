"""企业微信联系人管理和云端授权入口。"""

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.models.entities import WorkWechatContact
from app.services.crud import get_channel
from app.services.ops import record_audit
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.workwechat_cloud_poll import (
    is_cloud_poll_enabled,
    issue_channel_authorization_code,
    notify_cloud_job_enqueued,
)
from app.services.workwechat_contacts import (
    build_workwechat_contact_detail,
    build_workwechat_contact_list,
    mark_workwechat_contact_deleted,
    restore_workwechat_contact,
    sync_workwechat_contacts,
    upsert_workwechat_contact,
)
from app.web.shared.access import enforce_role, get_session
from app.web.shared.operations import (
    _completed_operation_response,
    _queued_operation_response,
    _require_operation_csrf,
)
from app.web.shared.presentation import render


router = APIRouter()


def _build_workwechat_contact_form_payload(**values: str) -> dict[str, Any]:
    relation = {
        "owner_userid": str(values.pop("owner_userid", "") or "").strip(),
        "owner_name": str(values.pop("owner_name", "") or "").strip(),
        "remark": str(values.pop("remark", "") or "").strip(),
        "relationship_status": "active",
    }
    payload = {key: str(value or "").strip() for key, value in values.items()}
    payload["source"] = "manual"
    payload["sync_status"] = "active"
    if relation["owner_userid"] or relation["owner_name"] or relation["remark"]:
        payload["follow_user"] = [relation]
    return payload


@router.get("/channels/{channel_id}/workwechat/contacts", response_class=HTMLResponse)
async def workwechat_contact_list_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        list_view = build_workwechat_contact_list(
            db,
            channel,
            query=request.query_params.get("q", ""),
            sync_status=request.query_params.get("sync_status", ""),
            contact_kind=request.query_params.get("contact_kind", ""),
        )
        return render(
            request,
            "workwechat_contact_list.html",
            page_title=f"企微联系人 · {channel.name}",
            channel=channel,
            list_view=list_view,
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get("/channels/{channel_id}/workwechat/contacts/new", response_class=HTMLResponse)
async def workwechat_contact_create_page(request: Request, channel_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        return render(
            request,
            "workwechat_contact_detail.html",
            page_title=f"新增企微联系人 · {channel.name}",
            channel=channel,
            contact=None,
            detail=None,
            mode="create",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get("/channels/{channel_id}/workwechat/contacts/{contact_id}", response_class=HTMLResponse)
async def workwechat_contact_detail_page(request: Request, channel_id: int, contact_id: int):
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        detail = build_workwechat_contact_detail(db, channel, contact)
        return render(
            request,
            "workwechat_contact_detail.html",
            page_title=f"企微联系人 · {detail['name']}",
            channel=channel,
            contact=contact,
            detail=detail,
            mode="edit",
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/contacts")
async def create_workwechat_contact_action(
    request: Request,
    channel_id: int,
    contact_kind: str = Form("external_contact"),
    contact_external_id: str = Form(""),
    conversation_external_id: str = Form(""),
    external_userid: str = Form(""),
    userid: str = Form(""),
    name: str = Form(""),
    display_name: str = Form(""),
    alias: str = Form(""),
    mobile: str = Form(""),
    email: str = Form(""),
    corp_name: str = Form(""),
    corp_full_name: str = Form(""),
    corp_id: str = Form(""),
    position: str = Form(""),
    avatar_url: str = Form(""),
    gender: str = Form("unknown"),
    contact_type: str = Form("unknown"),
    remark: str = Form(""),
    owner_userid: str = Form(""),
    owner_name: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        payload = _build_workwechat_contact_form_payload(
            contact_kind=contact_kind,
            contact_external_id=contact_external_id,
            conversation_external_id=conversation_external_id,
            external_userid=external_userid,
            userid=userid,
            name=name,
            display_name=display_name,
            alias=alias,
            mobile=mobile,
            email=email,
            corp_name=corp_name,
            corp_full_name=corp_full_name,
            corp_id=corp_id,
            position=position,
            avatar_url=avatar_url,
            gender=gender,
            contact_type=contact_type,
            remark=remark,
            owner_userid=owner_userid,
            owner_name=owner_name,
        )
        try:
            contact = upsert_workwechat_contact(db, channel, payload)
        except ValueError as exc:
            return RedirectResponse(
                url=f"/channels/{channel_id}/workwechat/contacts/new?notice={quote(str(exc))}",
                status_code=303,
            )
        db.commit()
        record_audit(db, "channel.workwechat.contact.create", "workwechat_contact", contact.id, f"contact={contact.contact_external_id}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact.id}?notice={quote('联系人已保存')}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/edit")
async def update_workwechat_contact_action(
    request: Request,
    channel_id: int,
    contact_id: int,
    contact_kind: str = Form("external_contact"),
    contact_external_id: str = Form(""),
    conversation_external_id: str = Form(""),
    external_userid: str = Form(""),
    userid: str = Form(""),
    name: str = Form(""),
    display_name: str = Form(""),
    alias: str = Form(""),
    mobile: str = Form(""),
    email: str = Form(""),
    corp_name: str = Form(""),
    corp_full_name: str = Form(""),
    corp_id: str = Form(""),
    position: str = Form(""),
    avatar_url: str = Form(""),
    gender: str = Form("unknown"),
    contact_type: str = Form("unknown"),
    remark: str = Form(""),
    owner_userid: str = Form(""),
    owner_name: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        payload = _build_workwechat_contact_form_payload(
            contact_kind=contact_kind,
            contact_external_id=contact.contact_external_id,
            conversation_external_id=conversation_external_id,
            external_userid=external_userid,
            userid=userid,
            name=name,
            display_name=display_name,
            alias=alias,
            mobile=mobile,
            email=email,
            corp_name=corp_name,
            corp_full_name=corp_full_name,
            corp_id=corp_id,
            position=position,
            avatar_url=avatar_url,
            gender=gender,
            contact_type=contact_type,
            remark=remark,
            owner_userid=owner_userid,
            owner_name=owner_name,
        )
        saved = upsert_workwechat_contact(db, channel, payload)
        db.commit()
        record_audit(db, "channel.workwechat.contact.edit", "workwechat_contact", saved.id, f"contact={saved.contact_external_id}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{saved.id}?notice={quote('联系人已更新')}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/mark-deleted")
async def mark_workwechat_contact_deleted_action(
    request: Request,
    channel_id: int,
    contact_id: int,
    delete_reason: str = Form("deleted_by_contact"),
    delete_event_key: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        mark_workwechat_contact_deleted(db, contact, reason=delete_reason, event_key=delete_event_key)
        db.commit()
        record_audit(db, "channel.workwechat.contact.mark-deleted", "workwechat_contact", contact.id, f"reason={delete_reason}, operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact_id}?notice={quote('联系人已标记为删除')}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/contacts/{contact_id}/restore")
async def restore_workwechat_contact_action(request: Request, channel_id: int, contact_id: int):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type != "workwechat":
            raise HTTPException(status_code=404, detail="WorkWechat channel not found")
        contact = db.get(WorkWechatContact, contact_id)
        if not contact or contact.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Contact not found")
        restore_workwechat_contact(db, contact)
        db.commit()
        record_audit(db, "channel.workwechat.contact.restore", "workwechat_contact", contact.id, f"operator={user.username}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/workwechat/contacts/{contact_id}?notice={quote('联系人已恢复为有效')}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/sync-contacts")
async def sync_workwechat_contacts_action(request: Request, channel_id: int, contact_kind: str = Form("all")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "workwechat":
            raise HTTPException(status_code=400, detail="Channel is not workwechat")
        if is_cloud_poll_enabled(channel):
            # 云端模式下联系人列表必须由客服电脑本地 provider 调 Hook 11036/11037 后回传；
            # 服务端只负责下发控制任务，避免要求配置内网 API Base。
            job = enqueue_job(
                db,
                job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                payload={
                    "channel_id": channel.id,
                    "action": "sync_contacts",
                    "payload": {"contact_kind": contact_kind, "page_size": 100, "max_pages": 50},
                },
                priority=95,
                related_type="channel",
                related_id=channel.id,
                max_attempts=3,
            )
            db.commit()
            notify_cloud_job_enqueued(channel, job)
            message = "企微联系人同步已排队，正在等待客户端执行。"
            record_audit(db, "channel.workwechat.sync-contacts", "channel", channel.id, message)
            return _queued_operation_response(request, db, channel, [job], f"/channels/{channel_id}/workwechat/contacts")
        overview = sync_workwechat_contacts(db, channel, contact_kind=contact_kind)
        db.commit()
        message = str(overview.get("last_status_summary") or "已同步企微联系人")
        record_audit(db, "channel.workwechat.sync-contacts", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/channels/{channel_id}/workwechat/contacts", ok=bool(overview.get("ok")))
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/cloud-poll/authorization-code")
async def issue_workwechat_cloud_poll_authorization_code_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        try:
            channel, code = issue_channel_authorization_code(db, channel_id)
        except ValueError as exc:
            error_map = {
                "channel_not_found": "渠道不存在",
                "workwechat_channel_required": "只有企业微信渠道可以生成云端授权码",
            }
            message = error_map.get(str(exc), f"生成企微云端授权码失败：{exc}")
            return RedirectResponse(url=f"/channels/{channel_id}?notice={quote(message)}", status_code=303)
        # 授权码是一次性明文，只在本次操作提示里展示；数据库仅保存哈希和过期时间。
        message = f"企微云端授权码：{code}（24小时内使用一次）"
        record_audit(db, "channel.workwechat.cloud-poll.authorization-code", "channel", channel.id, "生成企微云端授权码")
        return RedirectResponse(url=f"/channels/{channel.id}?notice={quote(message)}", status_code=303)
    finally:
        db.close()
