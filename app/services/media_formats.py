"""媒体原件的保守格式识别；不解码、不联网，不把未知字节误标为图片。"""

from pathlib import Path


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/bmp": ".bmp", "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mp4": ".m4a",
    "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/amr": ".amr",
    "audio/amr-wb": ".amr", "audio/silk": ".silk", "audio/speex": ".spx",
    "application/pdf": ".pdf", "application/octet-stream": ".bin",
}


def identify(content: bytes, declared: str = "", filename: str = "") -> tuple[str, str]:
    """文件头优先于手机声明；只有已知格式才回退 MIME/扩展名，原文件字节保持不动。"""

    head = content[:64]
    signatures = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
                  (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"ID3", "audio/mpeg"),
                  (b"#!AMR-WB\n", "audio/amr-wb"), (b"#!AMR\n", "audio/amr"),
                  (b"#!SILK_V3", "audio/silk"), (b"\x02#!SILK_V3", "audio/silk"),
                  (b"fLaC", "audio/flac"), (b"OggS", "audio/ogg"), (b"%PDF-", "application/pdf"))
    mime = next((kind for signature, kind in signatures if head.startswith(signature)), "")
    if head[:4] == b"RIFF":
        mime = {b"WAVE": "audio/wav", b"WEBP": "image/webp"}.get(head[8:12], mime)
    if head[4:8] == b"ftyp":
        mime = "audio/mp4" if head[8:12] in {b"M4A ", b"M4B "} else "video/mp4"
    if head[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        mime = "audio/mpeg"
    normalized = str(declared or "").split(";", 1)[0].strip().lower()
    if not mime and normalized in MIME_EXTENSIONS and normalized != "application/octet-stream":
        mime = normalized
    if not mime:
        suffix = Path(str(filename)).suffix.lower()
        mime = next((kind for kind, extension in MIME_EXTENSIONS.items() if extension == suffix), "application/octet-stream")
    return mime, MIME_EXTENSIONS[mime]


def media_kind(mime: str, attachment_type: str = "", filename: str = "") -> str:
    """voice 和作为普通文件发送的音频在媒体库统一归入 audio。"""

    normalized, _ = identify(b"", mime, filename)
    family = normalized.partition("/")[0]
    if family in {"image", "video", "audio"}:
        return family
    return {"image": "image", "emoji": "image", "video": "video", "voice": "audio", "audio": "audio"}.get(attachment_type, "file")
