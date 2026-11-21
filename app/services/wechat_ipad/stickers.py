"""按原始字节投递表情；不重编码动图，也不向协议传入本地文件路径。"""

import base64
import hashlib
from io import BytesIO

from PIL import Image

from app.services.wechat_ipad.quotes import server_id

MAX_BYTES = 8 * 1024 * 1024


def media_type(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("表情不能为空，且不能超过 8 MB")
    try:
        with Image.open(BytesIO(data), formats=("GIF", "PNG", "WEBP")) as image:
            if image.width * image.height > 4096**2:
                raise ValueError("表情分辨率过大")
            mime = image.get_format_mimetype()
            image.verify()
            return mime
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("请选择有效的 GIF、PNG 或 WebP 表情") from None


def prepare(payload):
    value = payload.get("emoji_base64")
    if not isinstance(value, str) or len(value) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("表情编码无效或超过 8 MB")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("表情编码无效") from None
    mime = media_type(data)
    return {"size": len(data), "md5": hashlib.md5(data, usedforsecurity=False).hexdigest()}, {
        "Base64": f"data:{mime};base64,{value}"}


def receipt(reply, owner, target, meta):
    data = reply.get("Data")
    resp = data.get("resp") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        return None, "result_unknown"
    base = resp.get("BaseResponse")
    items = resp.get("emojiItem")
    item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else {}
    codes = [data.get("retCode"), base.get("ret") if isinstance(base, dict) else None, item.get("ret")]
    if any(type(code) is int and code != 0 for code in codes):
        return None, "provider_rejected"
    # 外层成功只证明上传请求结束；核对最后一项，不能把分片回执当成消息。
    if (reply.get("http_status") == 200 and reply.get("Success") is True
            and type(reply.get("Code")) is int and reply["Code"] == 0
            and all(type(code) is int and code == 0 for code in codes)
            and data.get("isSendSuccess") is True and type(resp.get("emojiItemCount")) is int
            and resp["emojiItemCount"] == 1 and data.get("emojiMd5") == item.get("md5") == meta["md5"]
            and data.get("emojiSize") == item.get("totalLen") == item.get("startPos") == meta["size"]
            and (mid := server_id(item.get("newMsgId")))):
        return mid, None
    return None, "result_unknown"
