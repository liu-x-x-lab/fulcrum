"""向外部地址下载媒体时的 SSRF 防护:只允许访问公网地址。

出站消息的图片、文件、视频地址来自插件回复或手动发送,是不可信输入。如果直接 urlopen,租户可以让服务器去访问
`http://127.0.0.1:...`、`http://192.168.x.x/...`、`http://169.254.169.254/`(云元数据)等内网地址,
再把抓到的内容当成图片/文件发出去。这里在**每一次请求(包括重定向的每一跳)**之前解析主机名,
只要解析结果里有任何一个回环、私网、链路本地、保留、组播或未指定地址就拒绝。

read_public_file 直接连接审核过的 IP。旧 open_public_url 调用方仍使用 urllib 二次解析域名。
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from http.client import HTTPConnection
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_REDIRECTS = 3
HTML_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class UnsafeUrlError(ValueError):
    """地址不是允许访问的公网 http(s) 地址。"""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def ensure_public_http_url(url: str) -> str:
    """校验地址:只允许 http/https、无内嵌凭证、主机名解析出的所有地址都是公网地址。"""

    return _public_target(url)[0].geturl()


def _public_target(url: str):
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只支持 http/https 地址")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("地址不能内嵌账号密码")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("端口不合法") from exc
    if port == 0:
        raise UnsafeUrlError("端口不合法")
    try:
        infos = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError("域名无法解析") from exc
    addresses = {info[4][0].split("%", 1)[0] for info in infos}
    if not addresses or not all(_is_public(address) and ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeUrlError("不允许访问内网或保留地址")
    return parsed, infos


def read_public_file(url: str, limit: int) -> bytes:
    """单群/私聊附件下载：连接已检查的地址，TLS 仍校验原域名，每跳重新解析。"""
    return _read_resource(url, limit).data


@dataclass(frozen=True)
class PublicResource:
    data: bytes
    url: str
    charset: str | None


def read_public_html(url: str, limit: int = 2 * 1024 * 1024) -> PublicResource:
    """匿名读取公开网页，以浏览器请求头获取正文；保留逐跳校验和调用方下载限额。"""

    return _read_resource(url, limit, html_only=True)


def _read_resource(url: str, limit: int, *, html_only: bool = False) -> PublicResource:
    deadline = time.monotonic() + 12 if html_only else None

    def timeout() -> float:
        if deadline is None:
            return 20
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("网页读取超时")
        return remaining

    for _ in range(MAX_REDIRECTS + 1):
        parsed, infos = _public_target(url)
        if html_only and parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            raise UnsafeUrlError("网页地址只支持标准端口")
        family, kind, proto, _, address = infos[0]
        conn = HTTPConnection(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=timeout())
        try:
            conn.auto_open = False
            conn.sock = socket.socket(family, kind, proto)
            conn.sock.settimeout(timeout())
            conn.sock.connect(address)
            if parsed.scheme == "https":
                conn.sock = ssl.create_default_context().wrap_socket(conn.sock, server_hostname=parsed.hostname)
            path = parsed.path or "/"
            headers = {"Host": parsed.netloc}
            if html_only:
                headers.update({
                    "Accept": "text/html, application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
                    "User-Agent": HTML_USER_AGENT,
                })
            conn.request("GET", path + ("?" + parsed.query if parsed.query else ""), headers=headers)
            transport = conn.sock
            with conn.getresponse() as response:
                if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                    url = urljoin(url, response.getheader("Location"))
                    continue
                if response.status != 200:
                    raise ValueError("文件下载失败")
                if html_only and (not response.getheader("Content-Type") or response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}
                                  or response.getheader("Content-Encoding", "identity").lower() != "identity"):
                    raise ValueError("不是可读取的 HTML 网页")
                length = response.getheader("Content-Length")
                if length is not None and not 0 < int(length) <= limit:
                    raise ValueError("文件大小无效")
                if html_only:
                    buffer = bytearray()
                    while len(buffer) <= limit:
                        transport.settimeout(timeout())
                        chunk = response.read1(min(65536, limit + 1 - len(buffer)))
                        if not chunk:
                            break
                        buffer.extend(chunk)
                    data = bytes(buffer)
                else:
                    data = response.read(limit + 1)
                if not 0 < len(data) <= limit or (length is not None and len(data) != int(length)):
                    raise ValueError("文件大小无效或下载不完整")
                return PublicResource(data, parsed.geturl(), response.headers.get_content_charset() if html_only else None)
        finally:
            conn.close()
    raise UnsafeUrlError("重定向次数超过上限")


class _PublicOnlyRedirects(HTTPRedirectHandler):
    """每一跳重定向都重新校验目标地址,并限制跳数。"""

    def __init__(self) -> None:
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise URLError("重定向次数超过上限")
        try:
            ensure_public_http_url(newurl)
        except UnsafeUrlError as exc:
            raise URLError(f"重定向目标不安全: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public_url(url: str, *, timeout: float = 20.0):
    """校验后打开地址;返回可读的响应对象,调用方负责限制读取长度。"""

    safe = ensure_public_http_url(url)
    request = Request(safe, method="GET")
    request.add_header("Expect", "")
    return build_opener(_PublicOnlyRedirects()).open(request, timeout=timeout)
