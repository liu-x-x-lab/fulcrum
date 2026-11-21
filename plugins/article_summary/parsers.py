from __future__ import annotations

import codecs
import html
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

from app.services.safe_fetch import PublicResource


URL = re.compile(r'''https?://[^\s<>"'，。；、！？【】（）《》]+''', re.I)
VIDEO_HOSTS = ("douyin.com", "iesdouyin.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "v.qq.com", "tiktok.com", "channels.weixin.qq.com")
MEDIA_PATH = re.compile(r"\.(?:mp[34]|webm|m3u8|mov|avi|pdf|zip|jpe?g|png|gif|webp|svg)$", re.I)
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
SKIP = frozenset("script style template noscript nav aside footer form button svg iframe".split())
BLOCK = frozenset("p br div section li h1 h2 h3 h4 blockquote pre".split())


def normalize_url(raw: str) -> str:
    if not raw or len(raw) > 4096 or any(char.isspace() or ord(char) < 32 or char == '\\' for char in raw):
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443 if parts.scheme == "https" else 80)
                or any(host == domain or host.endswith("." + domain) for domain in VIDEO_HOSTS) or MEDIA_PATH.search(parts.path)):
            return ""
        return urlunsplit((parts.scheme, f"[{host}]" if ":" in host else host, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="%/:?@!$&'()*+,;=-._~"), ""))
    except (ValueError, UnicodeError):
        return ""


def message_url(kind: str, content: str, event: Mapping) -> str:
    if kind == "text":
        for match in URL.finditer(content[:32768]):
            raw = match.group().rstrip(".,;!?")
            if raw.endswith(")") and raw.count(")") > raw.count("("):
                raw = raw[:-1]
            if url := normalize_url(raw):
                return url
        return ""
    if kind not in {"app", "link", "file"}:
        return ""
    details = event.get("details") if isinstance(event, Mapping) else None
    details = details if isinstance(details, Mapping) else {}
    xml = str(details.get("content_xml") or content).strip()
    if len(xml) > 128000:
        return ""
    for _ in range(2):
        if not xml.startswith(("&lt;", "&amp;lt;")):
            break
        xml = html.unescape(xml)
    if xml.startswith("<"):
        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            return ""
        try:
            root = ElementTree.fromstring(xml)
        except (ValueError, ElementTree.ParseError):
            return ""
        card = root if root.tag == "appmsg" else root.find("appmsg")
        if card is None or card.findtext("type", "").strip() != "5":
            return ""
        for path in ("url", "mmreader/category/item/url"):
            if url := normalize_url(html.unescape(card.findtext(path) or "").strip()):
                return url
    if kind == "file":
        return ""
    media = details.get("media")
    media = media if isinstance(media, Mapping) else {}
    return normalize_url(str(media.get("app_msg_url") or (content if kind == "link" else "")).strip())


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parts = {key: [] for key in ("title", "h1", "wechat", "article", "main", "p")}
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and (attrs.get("type") or "").lower() == "password":
            self.blocked = True
        if tag == "meta" and attrs.get("property") == "og:type" and (attrs.get("content") or "").startswith("video"):
            self.blocked = True
        target = self.stack[-1][1] if self.stack else None
        if tag in SKIP or "hidden" in attrs or attrs.get("aria-hidden") == "true" or target == "skip":
            target = "skip"
        elif attrs.get("id") == "js_content":
            target = "wechat"
        elif tag == "h1" or attrs.get("id") == "activity-name":
            target = "h1"
        elif attrs.get("role") == "main" and target not in {"wechat", "article"}:
            target = "main"
        elif target not in {"wechat", "article"}:
            if tag in {"title", "article", "main", "h1"} or (tag == "p" and not target):
                target = tag
        if tag in BLOCK and target in self.parts:
            self.parts[target].append("\n")
        if tag not in VOID:
            if len(self.stack) >= 256:
                raise ValueError("网页层级过深")
            self.stack.append((tag, target))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] in self.parts:
            self.parts[self.stack[-1][1]].append(data)


def extract_article(page: PublicResource) -> tuple[str, str] | None:
    if not normalize_url(page.url):
        return None
    meta = re.search(rb'''<meta\b[^>]{0,256}?charset\s*=\s*["']?([a-z0-9._-]+)''', page.data[:4096], re.I)
    charset = (page.charset or (meta[1].decode("ascii") if meta else "utf-8")).lower()
    encoding = codecs.lookup("gb18030" if charset in {"gb2312", "gbk"} else charset).name
    if encoding not in {"utf-8", "utf-8-sig", "ascii", "gb18030", "big5", "iso8859-1", "cp1252", "shift_jis"}:
        return None
    parser = ArticleParser()
    try:
        parser.feed(page.data.decode(encoding, errors="replace"))
        parser.close()
    except AssertionError:
        return None
    if parser.blocked:
        return None

    def text(key):
        return re.sub(r"\s+", " ", "".join(parser.parts[key])).strip()

    title = (text("title") or text("h1"))[:160]
    wechat = urlsplit(page.url).hostname == "mp.weixin.qq.com"
    body = text("wechat") if wechat else next((value for key in ("wechat", "article", "main", "p") if (value := text(key))), "")
    if len(body) < 80 or not title or any(term in title.lower() for term in ("验证码", "安全验证", "环境异常", "just a moment", "captcha")):
        return None
    return title, body[:12000]
