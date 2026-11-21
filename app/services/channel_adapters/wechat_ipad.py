from __future__ import annotations

import base64
import html
import json
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from xml.sax.saxutils import escape
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.models.entities import Channel, Conversation
from app.services.account_identity import account_identity
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
from app.services.wechat_ipad import files, stickers, videos
from app.services.wechat_ipad.operations.invitations import send_invitation
from app.services.wechat_ipad.quotes import server_id

from .base import BaseChannelAdapter


DEFAULT_TIMEOUT_SECONDS = 12


class WechatIpadAdapter(BaseChannelAdapter):
    adapter_type = "wechat_ipad"

    MSG_TYPE_MAP = {
        1: "text",
        3: "image",
        34: "voice",
        43: "video",
        47: "emoji",
        49: "file",
        10000: "system",
    }

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        adapter_config = self._load_adapter_config(channel)
        wxid = str(adapter_config.get("wxid") or "").strip()
        to_wxid = str(payload.get("receiver_id") or payload.get("external_id") or conversation.external_id or "").strip()
        native_file = message_type == "file" and not payload.get("xml")
        native_video = False
        if message_type == "video":
            cdn_xml = self._video_xml(payload)
            if cdn_xml and (payload.get("base64") or payload.get("video_base64")):
                return self._error_result(conversation, message_type, "invalid_payload", "视频不能同时指定媒体和转发 XML")
            native_video = not cdn_xml
        if native_file or message_type in {"video", "emoji"}:
            wxid = account_identity(channel)["external_id"]
            if (conversation.channel_id != channel.id or conversation.tenant_id != channel.tenant_id
                    or conversation.bot_id != channel.bot_id or channel.deleted_at or not channel.is_enabled
                    or any(payload.get(k) not in (None, "", conversation.external_id) for k in ("receiver_id", "external_id"))):
                return self._error_result(conversation, message_type, "invalid_target", "附件目标不是当前账号会话")

        if not channel.api_base:
            return self._error_result(conversation, message_type, "missing_api_base", "渠道未配置 API Base")
        if not wxid:
            return self._error_result(conversation, message_type, "missing_wxid", "adapter_config_json 缺少 wxid")
        if not to_wxid:
            return self._error_result(conversation, message_type, "missing_target", "缺少接收方 ID")

        if native_file or native_video or message_type == "emoji":
            return self._send_attachment(channel, conversation, wxid, to_wxid, message_type, payload)

        try:
            request_spec = self._build_request(message_type, wxid, to_wxid, payload)
        except (ValueError, OSError, RuntimeError) as exc:
            return self._error_result(conversation, message_type, "invalid_payload", f"消息文件无法发送：{exc}")
        if not request_spec:
            return self._error_result(
                conversation,
                message_type,
                "invalid_payload",
                self._build_payload_error_message(message_type, payload),
            )
        endpoint = str(request_spec["endpoint"])
        body = request_spec["body"]

        base_url = channel.api_base.rstrip("/")
        target_url = f"{base_url}{endpoint}"
        raw_request = {
            "url": target_url,
            "method": "POST",
            "json": body,
        }

        try:
            raw_response = self._post_json(target_url, body)
            response_status = int(raw_response.get("http_status") or 200)
            response_ok = response_status < 400 and self._response_indicates_success(raw_response)
            if message_type == "link" and response_ok:
                # SendApp 解包成功不代表卡片获微信确认；账号、接收方或服务端编号缺失均不报已发。
                data = WechatIpadApiClient.extract_data(raw_response)
                base = data.get("BaseResponse")
                ret = base.get("ret") if isinstance(base, dict) else None
                if type(ret) is int and ret != 0:
                    return self._error_result(conversation, message_type, "provider_rejected", "微信拒绝发送链接卡片")
                if (type(ret) is not int or not server_id(data.get("newMsgId"))
                        or data.get("fromUserName") != wxid or data.get("toUserName") != to_wxid
                        or raw_response.get("Success") is not True or raw_response.get("Code") != 0):
                    return self._error_result(conversation, message_type, "result_unknown", "卡片回执不完整或目标不符，请核对会话")
            if response_ok and endpoint == IPAD_874_CONTRACT.endpoints.message_quote:
                # 874 的 Success 只表示 protobuf 已解包，真正的发送结果在 BaseResponse。
                data = WechatIpadApiClient.extract_data(raw_response)
                base = data.get("BaseResponse")
                ret = base.get("ret") if isinstance(base, dict) else None
                if type(ret) is not int or ret != 0 or not server_id(data.get("newMsgId")) or data.get("toUserName", to_wxid) != to_wxid:
                    denied = type(ret) is int and ret != 0
                    return self._error_result(conversation, message_type, "provider_rejected" if denied else "result_unknown",
                                              "微信拒绝引用回复" if denied else "引用回复结果不明，请核对会话后再操作")
            if not response_ok:
                return self.build_result(
                    ok=False,
                    conversation=conversation,
                    message_type=message_type,
                    transport_status="error",
                    status="failed",
                    raw_request=raw_request,
                    raw_response=raw_response,
                    error_code=self._extract_error_code(raw_response),
                    error_message=self._extract_error_message(raw_response),
                    extra={"endpoint": endpoint},
                )

            return self.build_result(
                ok=True,
                conversation=conversation,
                message_type=message_type,
                transport_status="sent",
                status="sent",
                external_status=self._pick_response_status(raw_response),
                external_message_id=self._extract_external_message_id(raw_response),
                raw_request=raw_request,
                raw_response=raw_response,
                extra=self._build_send_metadata(raw_response, endpoint, conversation.external_id),
            )
        except Exception as exc:
            if message_type == "link":
                return self._error_result(conversation, message_type, "result_unknown", "卡片未取得有效回执，请核对会话")
            if endpoint == IPAD_874_CONTRACT.endpoints.message_quote:
                return self._error_result(conversation, message_type, "result_unknown",
                                          "引用回复未取得有效回执，请核对会话后再操作")
            return self.build_result(
                ok=False,
                conversation=conversation,
                message_type=message_type,
                transport_status="error",
                status="failed",
                raw_request=raw_request,
                error_code="request_failed",
                error_message=str(exc),
            )

    def _video_xml(self, payload):
        xml = self._first_non_empty(payload, "xml", "cdn_xml", "content_xml", "content")
        return xml if xml and xml.startswith("<") else ""

    def _send_attachment(self, channel, conversation, wxid, target, kind, payload):
        endpoints = IPAD_874_CONTRACT.endpoints
        try:
            if kind == "file":
                meta, encoded = files.prepare(payload)
                fields = {"FileName": meta["file_name"], "FileData": encoded}
                summary = {"file_name": meta["file_name"], "file_size": meta["size"]}
                endpoint, receipt, label = endpoints.message_send_file, files.receipt, "文件"
            elif kind == "emoji":
                meta, fields = stickers.prepare(payload)
                summary = {"emoji_size": meta["size"]}
                endpoint, receipt, label = endpoints.message_upload_emoji, stickers.receipt, "表情"
            else:
                meta, fields = videos.prepare(payload)
                summary = {"video_size": meta["size"], "play_length": meta["duration"]}
                endpoint, receipt, label = endpoints.message_send_video, videos.receipt, "视频"
        except ValueError as exc:
            return self._error_result(conversation, kind, "invalid_payload", str(exc))
        try:
            reply = self._post_json(channel.api_base.rstrip("/") + endpoint, {
                "Wxid": wxid, "ToWxid": target, **fields})
            mid, error = receipt(reply, wxid, target, meta)
        except Exception:
            mid, error = None, "result_unknown"
        if error:
            hint = f"微信拒绝发送{label}" if error == "provider_rejected" else f"{label}发送结果待核实，请先查看目标会话"
            result = self._error_result(conversation, kind, error, hint)
            if error == "result_unknown":
                result["external_status"] = "unknown"
            return result
        return self.build_result(ok=True, conversation=conversation, message_type=kind, transport_status="sent",
            status="sent", external_message_id=mid, raw_request=summary,
            raw_response={"newMsgId": mid}, extra={"endpoint": endpoint})

    def invite_group_members(
        self,
        channel: Channel,
        *,
        chatroom_external_id: str,
        member_external_ids: list[str],
    ) -> dict[str, Any]:
        """通过 874 协议执行群邀请，并返回统一渠道动作结果。"""

        return send_invitation(WechatIpadApiClient(), channel, account_identity(channel)["external_id"],
                               chatroom_external_id, member_external_ids)

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        envelope = self._resolve_event_envelope(payload)
        msg_type_value = self._coerce_int(self._pick_value(envelope, "MsgType", "msg_type", "Type"))
        message_type = self.MSG_TYPE_MAP.get(msg_type_value, "text" if self._extract_text_content(envelope) else "event")
        from_user = self._clean_string(
            self._pick_value(
                envelope,
                "FromUserName",
                path=("FromUserName",),
            )
        )
        to_user = self._clean_string(
            self._pick_value(
                envelope,
                "ToUserName",
                path=("ToUserName",),
            )
        )
        configured_self = self._clean_string(self._load_adapter_config(channel).get("wxid"))
        self_wxid = configured_self or self._clean_string(
            self._pick_value(
                payload,
                "Wxid",
                "wxid",
                path=("Data", "Wxid"),
                path2=("data", "Wxid"),
            )
        )
        raw_content = self._extract_text_content(envelope)
        quote_payload = self._parse_quote_payload(raw_content)
        if quote_payload:
            message_type = "text"
        image_info = self._extract_image_info(raw_content) if message_type == "image" else None
        voice_info = self._extract_voice_info(envelope) if message_type == "voice" else None
        video_info = self._extract_video_info(envelope) if message_type == "video" else None
        sender_id = self._clean_string(
            self._pick_value(
                envelope,
                "ActualUserName",
                "SenderWxid",
                "sender_id",
            )
        )
        sender_name = self._clean_string(self._pick_value(envelope, "ActualNickName", "sender_name", "PushContent"))

        is_group = bool(
            payload.get("IsGroup")
            or payload.get("is_group")
            or (from_user and from_user.endswith("@chatroom"))
            or (to_user and to_user.endswith("@chatroom"))
        )
        parsed_sender_id, parsed_content = self._split_group_sender(raw_content, sender_id, is_group)
        if parsed_sender_id:
            sender_id = parsed_sender_id
        direction = self._resolve_direction(from_user, to_user, self_wxid)
        if is_group and self_wxid and sender_id == self_wxid:
            direction = "out"
        # 只读取结构化提及，昵称和通知正文可能重名，也可能由发言人自由编写。
        mentions = self._mention_ids(self._pick_value(envelope, "MsgSource", "MessageSource", "msg_source")) if is_group else []
        conversation_external_id = self._resolve_conversation_key(from_user, to_user, self_wxid)
        receiver_id = self._resolve_receiver_id(direction, from_user, to_user, self_wxid, conversation_external_id)

        event_key = self._clean_string(
            self._pick_value(
                envelope,
                "EventId",
                "event_id",
                "ClientMsgId",
                "MsgId",
                "NewMsgId",
            )
        )
        event_type = self._clean_string(
            self._pick_value(payload, "Event", "event", "TypeName", "type_name", "Action", "action")
        ) or "message"
        content = parsed_content if parsed_content else raw_content
        if quote_payload:
            content = str(quote_payload.get("title") or content or "").strip()
        timestamp = self._coerce_int(self._pick_value(envelope, "CreateTime", "CreateTimestamp"))

        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": event_type,
            "should_persist_message": bool(content),
            "conversation": {
                "external_id": conversation_external_id or self_wxid or "manual",
                "title": self._clean_string(
                    self._pick_value(payload, "RoomName", "room_name", "conversation_title", "ConversationTitle")
                ),
                "chat_type": "group" if is_group else "direct",
            },
            "message": {
                "content": content,
                "sender_id": sender_id or from_user,
                "sender_name": sender_name,
                "message_type": message_type,
                "external_message_id": event_key,
            },
            "details": {
                "headers_used": sorted((headers or {}).keys()),
                "msg_type": msg_type_value,
                "self_wxid": self_wxid,
                "is_group": is_group,
                "from_user_name": from_user,
                "to_user_name": to_user,
                "receiver_id": receiver_id,
                "conversation_key": conversation_external_id,
                "direction": direction,
                "server_message_id": server_id(envelope.get("NewMsgId")),
                "mention_user_list": mentions,
                "mentioned_self": bool(configured_self and direction == "in" and configured_self in mentions),
                "timestamp": timestamp,
                "quote_payload": quote_payload,
                "raw_content": raw_content,
                "mod_contacts_chatroom_ids": self.parse_mod_contacts_msg(payload),
                "image_info": image_info,
                "voice_info": voice_info,
                "video_info": video_info,
            },
        }

    def health_check(self, channel: Channel) -> dict[str, Any]:
        adapter_config = self._load_adapter_config(channel)
        wxid = str(adapter_config.get("wxid") or "").strip()
        device_id = str(adapter_config.get("device_id") or "").strip()
        if not channel.api_base:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": "缺少 API Base",
                "details": {"reason": "missing_api_base"},
            }
        if not wxid:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": "缺少 wxid",
                "details": {"reason": "missing_wxid"},
            }

        probes = [
            {
                "name": "heartbeat",
                "endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat,
                "query": {"wxid": wxid},
                "required": True,
            },
            {
                "name": "cache_info",
                "endpoint": IPAD_874_CONTRACT.endpoints.login_get_cache_info,
                "query": {"wxid": wxid},
                "required": False,
            },
        ]
        probe_results: list[dict[str, Any]] = []
        required_failed = False
        optional_failed = False
        for probe in probes:
            result = self._run_health_probe(channel, probe["endpoint"], query=probe["query"])
            result["name"] = probe["name"]
            probe_results.append(result)
            if result["ok"]:
                continue
            if probe["required"]:
                required_failed = True
            else:
                optional_failed = True

        if required_failed:
            failed_probe = next((item for item in probe_results if not item["ok"]), {})
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "degraded",
                "summary": f"微信渠道关键探针失败：{failed_probe.get('name') or 'heartbeat'}",
                "details": {
                    "wxid": wxid,
                    "device_id": device_id or None,
                    "probes": probe_results,
                },
            }

        summary = "微信渠道心跳与缓存探针通过" if not optional_failed else "微信渠道心跳通过，缓存探针异常"
        return {
            "ok": not optional_failed,
            "adapter_type": self.adapter_type,
            "status": "online" if not optional_failed else "degraded",
            "summary": summary,
            "details": {
                "wxid": wxid,
                "device_id": device_id or None,
                "probes": probe_results,
            },
        }

    def _load_adapter_config(self, channel: Channel) -> dict[str, Any]:
        raw = getattr(channel, "adapter_config_json", None)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _error_result(
        self,
        conversation: Conversation,
        message_type: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return self.build_result(
            ok=False,
            conversation=conversation,
            message_type=message_type,
            transport_status="error",
            status="failed",
            error_code=error_code,
            error_message=error_message,
        )

    def parse_mod_contacts_msg(self, msg_content: dict[str, Any]) -> list[str]:
        group_ids: set[str] = set()
        mod_contacts = msg_content.get("ModContacts", [])
        if not isinstance(mod_contacts, list) or not mod_contacts:
            return []

        for contact in mod_contacts:
            if not isinstance(contact, dict):
                continue
            user_name_obj = contact.get("UserName", {})
            user_name = ""
            if isinstance(user_name_obj, dict):
                user_name = str(user_name_obj.get("string", "") or "").strip()
            elif isinstance(user_name_obj, str):
                user_name = user_name_obj.strip()

            if user_name.endswith("@chatroom"):
                group_ids.add(user_name)

        return sorted(group_ids)

    def _build_request(self, message_type: str, wxid: str, to_wxid: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if message_type == "text":
            content = str(payload.get("content") or "").strip()
            if not content:
                return None
            if "reply_to_id" in payload or "quote_message_id" in payload:
                source = payload.get("_ipad_quote")
                if not isinstance(source, dict) or payload.get("at"):
                    raise ValueError("缺少有效引用原消息，或同时请求了引用与 @")
                return {"endpoint": IPAD_874_CONTRACT.endpoints.message_quote,
                        "body": {**source, "Wxid": wxid, "ToWxid": to_wxid, "Title": content}}
            return {
                "endpoint": IPAD_874_CONTRACT.endpoints.message_send_text,
                "body": {
                    "Wxid": wxid,
                    "ToWxid": to_wxid,
                    "Type": 1,
                    "Content": content,
                    "At": str(payload.get("at") or ""),
                },
            }
        if message_type == "image":
            base64_data = self._first_non_empty(payload, "base64", "image_base64")
            if base64_data:
                return {
                    "endpoint": IPAD_874_CONTRACT.endpoints.message_upload_image,
                    "body": {
                        "Wxid": wxid,
                        "ToWxid": to_wxid,
                        "Base64": base64_data,
                    },
                }
            cdn_xml = self._first_non_empty(payload, "content", "xml", "cdn_xml", "content_xml")
            if cdn_xml:
                return {
                    "endpoint": IPAD_874_CONTRACT.endpoints.message_send_cdn_image,
                    "body": {
                        "Wxid": wxid,
                        "ToWxid": to_wxid,
                        "Content": cdn_xml,
                    },
                }
            return None
        if message_type == "voice":
            prepared_voice = self._prepare_voice_payload(payload)
            if not prepared_voice:
                return None
            return {
                "endpoint": IPAD_874_CONTRACT.endpoints.message_send_voice,
                "body": {
                    "Wxid": wxid,
                    "ToWxid": to_wxid,
                    "Base64": prepared_voice["base64"],
                    "VoiceTime": prepared_voice["voice_time"],
                    "Type": prepared_voice["voice_type"],
                },
            }
        if message_type == "video":
            cdn_xml = self._video_xml(payload)
            if cdn_xml:
                return {
                    "endpoint": IPAD_874_CONTRACT.endpoints.message_send_cdn_video,
                    "body": {
                        "Wxid": wxid,
                        "ToWxid": to_wxid,
                        "Content": cdn_xml,
                    },
                }
            return None
        if message_type == "file":
            xml = self._first_non_empty(payload, "xml")
            app_type = self._resolve_send_app_type(payload, xml)
            if xml:
                if app_type is None:
                    return None
                return {
                    "endpoint": IPAD_874_CONTRACT.endpoints.message_send_app,
                    "body": {
                        "Wxid": wxid,
                        "ToWxid": to_wxid,
                        "Xml": xml,
                        "Type": app_type,
                    },
                }
            return None
        if message_type == "link":
            generated_xml = self._build_link_app_xml(payload)
            if not generated_xml:
                return None
            return {
                "endpoint": IPAD_874_CONTRACT.endpoints.message_send_app,
                "body": {
                    "Wxid": wxid,
                    "ToWxid": to_wxid,
                    "Xml": generated_xml,
                    "Type": 5,
                },
            }
        return None

    def _run_health_probe(
        self,
        channel: Channel,
        endpoint: str,
        *,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        """通过统一 874 客户端执行探针，确保健康检查与业务调用共享查询参数编码。"""

        try:
            parsed = WechatIpadApiClient().request_json(channel, endpoint, query=query)
            response_status = int(parsed.get("http_status") or 200)
            ok = response_status < 400 and self._response_indicates_success(parsed)
            return {
                "ok": ok,
                "endpoint": endpoint,
                "http_status": response_status,
                "response_status": self._pick_response_status(parsed),
                "error_message": None if ok else self._extract_error_message(parsed),
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "error_message": str(exc),
                "reason": type(exc).__name__,
            }

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
                return self._parse_json_like(raw, getattr(response, "status", 200))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            return self._parse_json_like(body_text, exc.code)
        except URLError:
            raise

    def _parse_json_like(self, raw: str, status: int) -> dict[str, Any]:
        try:
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                parsed["http_status"] = status
                return parsed
            return {"http_status": status, "data": parsed}
        except json.JSONDecodeError:
            return {"http_status": status, "raw": raw}

    def _extract_external_message_id(self, response: dict[str, Any]) -> str | None:
        from app.services.wechat_ipad.recall import text_receipt

        text = text_receipt(response)
        if text:
            return str(text["new_msg_id"])
        ack = WechatIpadApiClient.extract_send_ack_fields(response)
        return ack["new_msg_id"] or ack["client_msg_id"]

    def _response_indicates_success(self, response: dict[str, Any]) -> bool:
        return WechatIpadApiClient.is_success(response)

    def _extract_error_code(self, response: dict[str, Any]) -> str:
        for key in ("ErrCode", "errcode"):
            value = response.get(key)
            if value not in (None, "", 0, "0", 1, "1", 200, "200"):
                return f"provider_{value}"
        provider_code = WechatIpadApiClient.extract_code(response)
        if provider_code not in (None, "", "0", "1", "200"):
            return f"provider_{provider_code}"
        return "provider_error"

    def _extract_error_message(self, response: dict[str, Any]) -> str:
        extracted = WechatIpadApiClient.extract_message(response)
        if extracted not in (None, ""):
            return str(extracted)
        for key in ("ErrMsg", "errmsg"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return "wechat_ipad provider returned an error"

    def _pick_response_status(self, response: dict[str, Any]) -> str:
        for key in ("Ret", "ret", "Code", "code", "Status", "status"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
        return "sent"

    def _build_send_metadata(self, response: dict[str, Any], endpoint: str, session_id: str) -> dict[str, Any]:
        ack = WechatIpadApiClient.extract_send_ack_fields(response)
        return {
            "endpoint": endpoint,
            "direction": "out",
            "session_id": session_id,
            "client_msg_id": ack["client_msg_id"],
            "create_time": ack["create_time"],
            "new_msg_id": ack["new_msg_id"],
        }

    def _build_payload_error_message(self, message_type: str, payload: dict[str, Any]) -> str:
        if message_type == "image":
            return "image 消息缺少可用载荷，需提供 base64/image_base64 或 content/xml/cdn_xml/content_xml"
        if message_type == "voice":
            return "voice 消息缺少可用载荷，需提供 base64/voice_base64/audio_base64，可选 voice_time 和 voice_type"
        if message_type == "video":
            return "video 消息缺少可用载荷，需提供 xml/cdn_xml 或 video_base64 + image_base64/cover_base64，可选 play_length"
        if message_type == "file":
            return "file 消息缺少可用载荷，需提供 xml 或 file_name + file_url/download_url；SendApp 需带真实 Type 或可从 xml.type 推导"
        if message_type == "link":
            return "link 消息缺少可用载荷，需提供 url/link_url，可选 title、desc 和 thumb_url"
        return f"暂不支持的消息类型: {message_type}"

    def _build_link_app_xml(self, payload: dict[str, Any]) -> str | None:
        """把平台通用链接卡片转换为 874 SendApp 所需的 type=5 XML。"""

        link_url = self._first_non_empty(payload, "url", "link_url")
        if not link_url:
            return None
        title = self._first_non_empty(payload, "title", "link_title") or link_url
        description = self._first_non_empty(payload, "desc", "description", "link_description") or ""
        thumb_url = self._first_non_empty(payload, "thumb_url", "image_url") or ""
        # 所有租户可编辑字段都先做 XML 转义，避免标题或 URL 中的特殊字符破坏消息结构。
        return (
            "<appmsg appid='' sdkver='0'>"
            f"<title>{escape(title)}</title>"
            f"<des>{escape(description)}</des>"
            "<type>5</type>"
            f"<url>{escape(link_url)}</url>"
            f"<thumburl>{escape(thumb_url)}</thumburl>"
            "</appmsg>"
        )

    def _resolve_send_app_type(self, payload: dict[str, Any], xml: str | None) -> int | None:
        explicit_type = self._coerce_int(payload.get("app_type") or payload.get("type"))
        if explicit_type is not None:
            return explicit_type
        if xml:
            xml_type = self._extract_app_msg_type(xml)
            if xml_type is not None:
                return xml_type
            return None
        return 6

    def _extract_app_msg_type(self, xml: str) -> int | None:
        marker_start = "<type>"
        marker_end = "</type>"
        start = xml.find(marker_start)
        end = xml.find(marker_end, start + len(marker_start))
        if start == -1 or end == -1:
            return None
        return self._coerce_int(xml[start + len(marker_start):end].strip())

    def _resolve_event_envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("Data", "data", "msg", "message"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload

    def _resolve_direction(self, from_user: str | None, to_user: str | None, self_wxid: str | None) -> str:
        if self_wxid and from_user == self_wxid:
            return "out"
        if self_wxid and to_user == self_wxid:
            return "in"
        return "in"

    def _resolve_conversation_key(
        self,
        from_user: str | None,
        to_user: str | None,
        self_wxid: str | None,
    ) -> str | None:
        for candidate in (from_user, to_user):
            if candidate and candidate.endswith("@chatroom"):
                return candidate
        if self_wxid and from_user == self_wxid:
            return to_user or from_user
        if self_wxid and to_user == self_wxid:
            return from_user or to_user
        return from_user or to_user

    def _resolve_receiver_id(
        self,
        direction: str,
        from_user: str | None,
        to_user: str | None,
        self_wxid: str | None,
        conversation_key: str | None,
    ) -> str | None:
        if conversation_key and conversation_key.endswith("@chatroom"):
            return conversation_key
        if direction == "out":
            return to_user or conversation_key
        if self_wxid:
            return self_wxid
        return to_user or conversation_key

    def _extract_text_content(self, payload: dict[str, Any]) -> str:
        value = self._pick_value(payload, "Content", "content", "Text", "text", "PushContent")
        if value is None:
            return ""
        return str(value).strip()

    def _parse_quote_payload(self, content: str) -> dict[str, Any] | None:
        xml_content = str(content or "").strip()
        if not xml_content.startswith("<"):
            return None
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return None

        appmsg = root.find(".//appmsg")
        if appmsg is None or appmsg.findtext("type", "").strip() != "57":
            return None

        title = html.unescape((appmsg.findtext("title", "") or "").strip())
        refer = appmsg.find("refermsg")
        if refer is None:
            return None

        ref_type = self._coerce_int(refer.findtext("type", "0")) or 0
        ref_content = html.unescape((refer.findtext("content", "") or "").strip())
        payload = {
            "title": title,
            "ref_type": ref_type,
            "ref_content": ref_content,
        }
        external_id = (refer.findtext("svrid") or "").strip()
        if ref_type in {3, 43} and external_id and external_id != "0":
            payload["ref_external_id"] = external_id
        image_info = self._extract_image_info(ref_content)
        if image_info:
            payload["image_info"] = image_info
        return payload

    def _extract_image_info(self, content: str) -> dict[str, str] | None:
        if not content:
            return None
        aeskey_match = re.search(r'aeskey="([^"]+)"', content)
        if not aeskey_match:
            return None
        url_match = re.search(r'cdnmidimgurl="([^"]+)"', content)
        if not url_match:
            url_match = re.search(r'cdnbigimgurl="([^"]+)"', content)
        if not url_match:
            url_match = re.search(r'cdnthumburl="([^"]+)"', content)
        if not url_match:
            return None
        md5_match = re.search(r'md5="([^"]+)"', content)
        return {
            "aeskey": aeskey_match.group(1),
            "file_no": url_match.group(1),
            "url": url_match.group(1),
            "md5": md5_match.group(1) if md5_match else "",
        }

    def _extract_voice_info(self, payload: dict[str, Any]) -> dict[str, str] | None:
        """从 874 同步消息的语音 XML 生成下载接口需要的四项元数据。"""

        content = self._extract_text_content(payload)
        msg_id = self._clean_string(self._pick_value(payload, "MsgId", "msg_id"))
        from_user_name = self._clean_string(self._pick_value(payload, "FromUserName", "from_user_name"))
        bufid = self._extract_xml_attribute(content, "bufid")
        # DownloadVoice 的 Length 表示需要下载的字节数，不能使用 voicelength 播放时长。
        length = self._extract_xml_attribute(content, "length")
        if not msg_id or not from_user_name or not bufid or not length:
            return None
        return {
            "msg_id": msg_id,
            "from_user_name": from_user_name,
            "bufid": bufid,
            "length": length,
        }

    def _extract_video_info(self, payload: dict[str, Any]) -> dict[str, str] | None:
        """从 874 视频 XML 读取总字节数，并初始化首个下载分片。"""

        content = self._extract_text_content(payload)
        msg_id = self._clean_string(self._pick_value(payload, "MsgId", "msg_id"))
        to_wxid = self._clean_string(self._pick_value(payload, "FromUserName", "from_user_name"))
        data_len = self._extract_xml_attribute(content, "length")
        if not msg_id or not to_wxid or not data_len:
            return None
        return {
            "msg_id": msg_id,
            "to_wxid": to_wxid,
            "data_len": data_len,
            "start_pos": "0",
            "section_length": data_len,
            "compress_type": "0",
        }

    def _extract_xml_attribute(self, content: str, attribute: str) -> str | None:
        """从 874 消息 XML 中提取媒体下载字段，属性名按协议原样匹配。"""

        if not content:
            return None
        matched = re.search(rf'\b{re.escape(attribute)}=["\']([^"\']+)["\']', content, flags=re.IGNORECASE)
        return self._clean_string(matched.group(1)) if matched else None

    def _pick_value(self, payload: dict[str, Any], *keys: str, **named_paths: tuple[str, ...]) -> Any:
        for key in keys:
            value = self._unwrap_value(payload.get(key))
            if value not in (None, ""):
                return value
        for path in named_paths.values():
            value = self._unwrap_path(payload, path)
            if value not in (None, ""):
                return value
        return None

    def _unwrap_path(self, payload: Any, path: tuple[str, ...]) -> Any:
        current = payload
        for part in path:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return self._unwrap_value(current)

    def _unwrap_value(self, value: Any) -> Any:
        current = value
        while isinstance(current, dict):
            if "string" in current:
                current = current["string"]
                continue
            if "String" in current:
                current = current["String"]
                continue
            if "str" in current:
                current = current["str"]
                continue
            if "Str" in current:
                current = current["Str"]
                continue
            if "value" in current:
                current = current["value"]
                continue
            break
        return current

    def _split_group_sender(self, content: str, sender_id: str | None, is_group: bool) -> tuple[str | None, str]:
        if not is_group:
            return sender_id, content
        if "\n" not in content:
            return sender_id, content
        prefix, remainder = content.split("\n", 1)
        if not prefix.endswith(":") or not re.fullmatch(r"[\w.@-]+:", prefix):
            return sender_id, content
        candidate = prefix[:-1]
        if not sender_id and candidate:
            sender_id = candidate
        return sender_id, remainder.strip()

    @staticmethod
    def _mention_ids(source: Any) -> list[str]:
        if not isinstance(source, str) or len(source) > 65536 or "<!DOCTYPE" in source:
            return []
        try:
            root = ET.fromstring(source)
        except ET.ParseError:
            return []
        text = root.findtext("atuserlist") if root.tag == "msgsource" else None
        return list(dict.fromkeys(part.strip() for part in (text or "").split(",") if part.strip()))

    def _coerce_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _first_non_empty(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                normalized = str(value).strip()
                if normalized:
                    return normalized
        return None

    def _clean_string(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip()
        return normalized or None

    def _guess_file_ext(self, file_name: str) -> str:
        if "." not in file_name:
            return ""
        return file_name.rsplit(".", 1)[-1]

    def _ensure_base64_data_uri(self, value: str, *, prefix: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return normalized
        if normalized.startswith("data:"):
            return normalized
        return f"{prefix}{normalized}"

    def _prepare_voice_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw_base64 = self._first_non_empty(payload, "base64", "voice_base64", "audio_base64")
        voice_path = self._first_non_empty(payload, "voice_path", "audio_path", "path")
        if not raw_base64 and not voice_path:
            return None

        voice_format = (
            self._first_non_empty(payload, "format", "voice_format", "audio_format")
            or ("silk" if self._coerce_int(payload.get("voice_type") or payload.get("format_type") or payload.get("type")) == 4 else "")
        ).strip().lower()
        if not voice_format and voice_path:
            voice_format = self._guess_file_ext(voice_path).lower()
        if not voice_format:
            voice_format = "silk"

        voice_bytes = self._load_binary_payload(raw_base64=raw_base64, file_path=voice_path)
        if not voice_bytes:
            return None

        if voice_format in {"silk", "slik"}:
            voice_base64 = base64.b64encode(voice_bytes).decode("ascii")
            voice_type = 4
        elif voice_format == "amr":
            voice_base64 = base64.b64encode(voice_bytes).decode("ascii")
            voice_type = 0
        elif voice_format in {"wav", "mp3"}:
            encoded = self._encode_audio_to_silk(voice_bytes, audio_format=voice_format)
            voice_base64 = base64.b64encode(encoded["bytes"]).decode("ascii")
            voice_type = 4
        else:
            raise ValueError(f"unsupported voice format: {voice_format}")

        voice_time = self._coerce_int(
            payload.get("voice_time")
            or payload.get("duration_ms")
            or payload.get("duration")
            or payload.get("play_length")
        )
        if voice_time is None:
            if voice_format in {"wav", "mp3"}:
                voice_time = int(encoded["duration_ms"])
            else:
                voice_time = 0

        return {
            "base64": voice_base64,
            "voice_time": voice_time,
            "voice_type": voice_type,
        }

    def _load_binary_payload(self, *, raw_base64: str | None, file_path: str | None) -> bytes:
        if raw_base64:
            normalized = raw_base64.strip()
            if normalized.startswith("data:") and "," in normalized:
                normalized = normalized.split(",", 1)[1]
            return base64.b64decode(normalized)
        if file_path:
            return open(file_path, "rb").read()
        return b""

    def _encode_audio_to_silk(self, voice_bytes: bytes, *, audio_format: str) -> dict[str, Any]:
        try:
            from pydub import AudioSegment
        except ImportError as exc:
            raise ValueError("pydub is required to transcode voice into silk") from exc
        try:
            import pysilk
        except ImportError as exc:
            raise ValueError("silk-python is required to transcode voice into silk") from exc

        audio = AudioSegment.from_file(BytesIO(voice_bytes), format=audio_format).set_channels(1)
        sample_rate = self._get_closest_frame_rate(audio.frame_rate)
        audio = audio.set_frame_rate(sample_rate).set_channels(1).set_sample_width(2)
        # 与内置试听共用 silk-python；流式 API 明确使用微信容器，不混装同名旧包。
        encoded = BytesIO()
        pysilk.encode(BytesIO(audio.raw_data), encoded, sample_rate, 24000, tencent=True)
        return {"bytes": encoded.getvalue(), "duration_ms": len(audio), "sample_rate": sample_rate}

    def _get_closest_frame_rate(self, frame_rate: int) -> int:
        supported = [8000, 12000, 16000, 24000]
        return min(supported, key=lambda candidate: abs(candidate - frame_rate))
