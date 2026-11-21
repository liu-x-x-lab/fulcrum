"""874 原生视频投递：只接受有界媒体字节，不把缩略图的上传结果当成视频回执。"""

import base64
import re
from io import BytesIO

from PIL import Image

from app.services.message_actions import MAX_UPLOAD_BYTES
from app.services.wechat_ipad.quotes import server_id

MAX_COVER_BYTES = 2 * 1024 * 1024


def has_mp4_header(data):
    size = int.from_bytes(data[:4], "big")
    if data[4:8] != b"ftyp" or not 16 <= size <= min(len(data) - 8, 4096) or size % 4:
        return False
    brands = [data[8:12], *(data[i:i + 4] for i in range(16, size, 4))]
    return any(b in {b"isom", b"mp41", b"mp42", b"avc1", b"M4V "} or b.startswith(b"iso") for b in brands)


def _decode(value, limit):
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3) + 64:
        raise ValueError("视频或封面为空或过大")
    if value.startswith("data:"):
        value = value.partition(",")[2]
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("视频或封面编码无效") from None
    if not 0 < len(data) <= limit:
        raise ValueError("视频或封面为空或过大")
    return data


def prepare(payload):
    video = _decode(payload.get("video_base64") or payload.get("base64"), MAX_UPLOAD_BYTES)
    if not has_mp4_header(video):
        raise ValueError("请选择 MP4 视频")
    cover = _decode(payload.get("image_base64") or payload.get("cover_base64") or payload.get("thumb_base64"), MAX_COVER_BYTES)
    try:
        with Image.open(BytesIO(cover), formats=("JPEG", "PNG")) as image:
            if image.width * image.height > 4096**2:
                raise ValueError("视频封面分辨率过大")
            image.thumbnail((640, 640))
            jpeg = BytesIO()
            image.convert("RGB").save(jpeg, format="JPEG", quality=82)
    except (OSError, Image.DecompressionBombError):
        raise ValueError("视频封面不是有效的 JPEG 或 PNG") from None
    duration = payload.get("play_length", payload.get("duration", payload.get("duration_seconds", 1)))
    if type(duration) is not int or not 0 <= duration <= 86400:
        raise ValueError("视频时长无效")
    return {"size": len(video), "duration": duration}, {
        "Base64": "data:video/mp4;base64," + base64.b64encode(video).decode("ascii"),
        "ImageBase64": "data:image/jpeg;base64," + base64.b64encode(jpeg.getvalue()).decode("ascii"),
        "PlayLength": duration,
    }


def receipt(reply, owner, target, meta):
    data = reply.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("BaseResponse"), dict):
        return None, "result_unknown"
    ret = data["BaseResponse"].get("ret")
    if type(ret) is int and ret != 0:
        return None, "provider_rejected"
    # 874 的 clientMsgId 使用 wxid_秒；缩略图阶段也可能返回编号，必须另验视频进度。
    if (type(ret) is int and ret == 0 and reply.get("http_status") == 200 and reply.get("Success") is True
            and type(reply.get("Code")) is int and reply["Code"] == 0 and data.get("toUserName", target) == target
            and data.get("fromUserName", owner) == owner
            and re.fullmatch(re.escape(owner) + r"_[0-9]+", str(data.get("clientMsgId") or ""))
            and type(data.get("videoStartPos")) is int and data["videoStartPos"] == meta["size"]):
        if mid := server_id(data.get("newMsgId")):
            return mid, None
    return None, "result_unknown"
