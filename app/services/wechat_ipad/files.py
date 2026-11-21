"""普通文件的字节来源与 874 两阶段回执，独立于已有 XML 卡片转发。"""

import base64
import hashlib
from http.client import HTTPException

from app.services.message_actions import MAX_UPLOAD_BYTES
from app.services.safe_fetch import read_public_file
from app.services.wechat_ipad.quotes import server_id


def delivery_name(name):
    if (not isinstance(name, str) or not name.strip() or not name.isprintable()
            or "/" in name or "\\" in name or name.strip() in {".", ".."}):
        raise ValueError("文件名无效")
    name = name.strip()
    if "." not in name:
        name += ".dat"
    if len(name.encode()) > 255:
        raise ValueError("文件名过长")
    return name


def prepare(payload):
    name = delivery_name(payload.get("file_name"))
    encoded = payload.get("file_base64") or payload.get("base64")
    url = payload.get("file_url") or payload.get("download_url") or payload.get("url")
    if bool(encoded) == bool(url):
        raise ValueError("请选择一个文件来源")
    if encoded:
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
            raise ValueError("文件大小或格式无效")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("文件编码无效") from None
    else:
        try:
            data = read_public_file(url, MAX_UPLOAD_BYTES)
        except (HTTPException, OSError, ValueError):
            raise ValueError("文件下载失败，请检查公网地址和文件大小") from None
        encoded = base64.b64encode(data).decode("ascii")
    if not 0 < len(data) <= MAX_UPLOAD_BYTES:
        raise ValueError("文件不能为空，且不能超过 25 MiB")
    # 总是使用明确的数据前缀；不把调用方字符串当作协议机上的路径。
    meta = {"file_name": name, "size": len(data), "md5": hashlib.md5(data, usedforsecurity=False).hexdigest()}
    return meta, "data:application/octet-stream;base64," + encoded


def receipt(reply, owner, target, meta):
    def zero(value):
        return type(value) is int and value == 0

    def obj(value):
        return value if isinstance(value, dict) else {}

    data = obj(reply.get("Data"))
    send = obj(data.get("sendResult"))
    ack = obj(send.get("Data"))
    upload = obj(data.get("uploadResp"))
    upload_ret, send_ret = data.get("uploadRetCode"), obj(ack.get("BaseResponse")).get("ret")
    if any(type(ret) is int and ret != 0 for ret in (upload_ret, send_ret)):
        return None, "provider_rejected"
    if (reply.get("http_status") == 200 and reply.get("Success") is True and zero(reply.get("Code"))
            and data.get("delivery") == "file" and data.get("isSendSuccess") is True and zero(data.get("retCode"))
            and data.get("toUserName") == target and data.get("fileMd5") == meta["md5"]
            and data.get("fileName") == meta["file_name"] and type(data.get("fileSize")) is int
            and data["fileSize"] == meta["size"] and isinstance(data.get("attachId"), str) and data["attachId"].strip()
            and upload.get("mediaId") == data["attachId"] and zero(obj(upload.get("BaseResponse")).get("ret"))
            and zero(upload_ret) and send.get("Success") is True and zero(send.get("Code")) and zero(send_ret)
            and ack.get("fromUserName") == owner and ack.get("toUserName") == target):
        mid = server_id(ack.get("newMsgId"))
        if mid:
            return mid, None
    return None, "result_unknown"
