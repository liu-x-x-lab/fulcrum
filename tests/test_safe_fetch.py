"""出站媒体下载的 SSRF 防护:只允许公网地址,重定向的每一跳也要校验。"""

from __future__ import annotations

import socket
import threading
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock

import pytest

from app.services import safe_fetch
from app.services.safe_fetch import UnsafeUrlError, ensure_public_http_url, open_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x", "http://localhost/x", "http://127.1/x", "http://[::1]/x",
        "http://10.0.0.5/x", "http://192.168.50.10:8280/x", "http://172.16.3.4/x",
        "http://169.254.169.254/latest/meta-data/",          # 云元数据
        "http://0.0.0.0/x", "http://[::ffff:127.0.0.1]/x",   # IPv4 映射的回环
        "http://[fe80::1]/x", "http://224.0.0.1/x",
    ],
)
def test_internal_and_reserved_addresses_are_refused(url) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "http:///x", "", "http://user:pw@example.com/x"])
def test_other_schemes_and_embedded_credentials_are_refused(url) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url(url)


def test_a_hostname_that_resolves_to_an_internal_address_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))])
    with pytest.raises(UnsafeUrlError, match="内网"):
        ensure_public_http_url("http://innocent-looking.example.com/x")
    # 只要有一个解析结果是内网就拒绝(攻击者可以同时返回公网和内网地址)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url("http://mixed.example.com/x")


def test_a_public_address_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    assert ensure_public_http_url("https://cdn.example.com/a.png") == "https://cdn.example.com/a.png"


@pytest.mark.parametrize("wire,expected", [
    (b"200 OK\r\nContent-Length: 3\r\n\r\nabc", b"abc"),
    (b"200 OK\r\nContent-Length: 4\r\n\r\nabc", ValueError),
    (b"200 OK\r\nContent-Length: 5\r\n\r\nabcde", ValueError),
    (b"200 OK\r\n\r\nabcde", ValueError),
    (b"302 Found\r\nLocation: http://internal.test/other\r\n\r\n", UnsafeUrlError),
])
@pytest.mark.parametrize("html_only", [False, True])
def test_file_fetch_pins_connection_and_limits_response(monkeypatch, wire, expected, html_only):
    resolutions = []

    def resolve(host, port, **_):
        resolutions.append(host)
        addr = "93.184.216.34" if host == "cdn.test" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]

    sock = Mock()
    if html_only:
        wire = wire.replace(b"\r\n", b"\r\nContent-Type: text/html; charset=utf-8\r\n", 1)
    sock.makefile.return_value = BytesIO(b"HTTP/1.1 " + wire)
    tls = Mock()
    tls.wrap_socket.return_value = sock
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", Mock(return_value=sock))
    monkeypatch.setattr(safe_fetch.ssl, "create_default_context", lambda: tls)
    read = safe_fetch.read_public_html if html_only else safe_fetch.read_public_file
    if expected == b"abc":
        result = read("https://cdn.test/x?a=b", 4)
        assert (result.data if html_only else result) == expected
        if html_only:
            assert result.charset == "utf-8"
    else:
        with pytest.raises(expected):
            read("https://cdn.test/x?a=b", 4)
    assert resolutions == (["cdn.test", "internal.test"] if expected is UnsafeUrlError else ["cdn.test"])
    sock.connect.assert_called_once_with(("93.184.216.34", 443))
    tls.wrap_socket.assert_called_once_with(sock, server_hostname="cdn.test")
    raw = sock.sendall.call_args.args[0]
    assert b"GET /x?a=b HTTP/1.1" in raw and b"Host: cdn.test\r\n" in raw
    if html_only:
        assert b"User-Agent: Mozilla/5.0" in raw and b"Accept-Language: zh-CN" in raw
    else:
        assert b"User-Agent:" not in raw and b"Accept-Language:" not in raw
    assert b"Cookie:" not in raw and b"Authorization:" not in raw
    assert sock.close.called


@pytest.mark.parametrize("headers", [b"Content-Type: video/mp4", b"Content-Type: application/pdf",
                                      b"Content-Type: text/html\r\nContent-Encoding: gzip", b"X-Missing-Type: 1"])
def test_html_reader_rejects_unsupported_content(monkeypatch, headers):
    sock = Mock()
    sock.makefile.return_value = BytesIO(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\nnot an article")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))])
    monkeypatch.setattr(socket, "socket", Mock(return_value=sock))
    with pytest.raises(ValueError, match="HTML"):
        safe_fetch.read_public_html("http://cdn.test/file")


def test_a_redirect_to_an_internal_address_is_blocked_on_that_hop(monkeypatch) -> None:
    """公网地址 302 到内网:第一跳通过,但重定向目标必须被拦下。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *args):  # 静默
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    real = safe_fetch.ensure_public_http_url
    # 测试服务器在回环上:仅对第一跳放行,重定向目标仍走真实校验
    first = {"done": False}

    def fake(url):
        if not first["done"] and url.startswith(f"http://127.0.0.1:{port}"):
            first["done"] = True
            return url
        return real(url)

    monkeypatch.setattr(safe_fetch, "ensure_public_http_url", fake)
    try:
        with pytest.raises(Exception, match="重定向目标不安全"):
            open_public_url(f"http://127.0.0.1:{port}/start")
    finally:
        server.shutdown()


def test_the_gugatool_adapter_refuses_internal_media_urls() -> None:
    from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter

    adapter = WechatGugatoolAdapter()
    for url in ("http://192.168.50.10:8280/secret", "http://127.0.0.1:9064/", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(RuntimeError, match="地址不允许"):
            adapter._load_media_bytes({"image_url": url}, url_keys=("image_url",), b64_keys=("base64",))
