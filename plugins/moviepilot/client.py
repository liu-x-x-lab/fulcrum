"""MoviePilot V3 API 边界：固定目标、严格响应、有限请求和无凭据的展示数据。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpTimeoutError


class MoviePilotError(ValueError):
    """只携带本地安全文案；上游错误正文可能含路径和站点凭据，不能直接转发。"""

    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clean(value, limit=200) -> str:
    """把非可信标题收敛为单行，防止上游内容插入伪造命令或携带敏感链接。"""
    value = re.sub(r"(?:https?://|magnet:)[^\s]+", "[链接已隐藏]", str(value or ""))
    return " ".join(value.split())[:limit]


def positive_id(value) -> int:
    raw = str(value or "")
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,18}", raw) or int(raw) <= 0:
        raise MoviePilotError("MoviePilot 返回的记录编号无效。")
    return int(value)


def movie(item: dict) -> dict | None:
    """第一版只支持 TMDB 电影，避免音乐、同名剧集或缺季信息被误订阅。"""
    if item.get("type") != "电影" or item.get("media_source") != "themoviedb":
        return None
    mid = str(item.get("media_id") or "")
    if not re.fullmatch(r"[0-9]{1,18}", mid):
        return None
    poster = str(item.get("poster_path") or item.get("poster") or "")
    parsed = urlsplit(poster)
    if parsed.scheme != "https" or parsed.netloc != "image.tmdb.org" or parsed.query or parsed.fragment:
        poster = ""
    return {"media_source": "themoviedb", "media_id": mid, "type": "电影",
            "title": clean(item.get("title") or item.get("name")), "year": clean(item.get("year"), 16),
            "overview": clean(item.get("overview") or item.get("description"), 600), "poster": poster}


def identity(item: dict) -> tuple[str, str, str]:
    return str(item.get("media_source") or ""), str(item.get("media_id") or ""), str(item.get("type") or "")


def subscription_fingerprint(item: dict) -> str:
    # 原生 ID 可能在数据库恢复后复用，取消前还要核对身份、创建时间和所属 MP 账号。
    return digest(item.get("id"), identity(item), item.get("date"), item.get("username"), item.get("season"))


def promotion(item: dict) -> dict:
    """下载计量比例为 0 才是 Free；缺失、布尔值和非法数字不能默认解释成免费。"""
    def factor(key):
        value = item.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            result = float(value)
            return result if math.isfinite(result) and 0 <= result <= 100 else None
        except (ValueError, TypeError, OverflowError):
            return None

    until = clean(item.get("freedate"), 40)
    expired = False
    if until:
        try:
            expired = ensure_shanghai_datetime(datetime.fromisoformat(until)) <= now_shanghai()
        except (ValueError, TypeError, OverflowError):
            pass
    return {"download": None if expired else factor("downloadvolumefactor"),
            "upload": factor("uploadvolumefactor"), "until": until, "expired": expired}


def promotion_label(value: dict | None) -> str:
    value = value or {}
    down, up = value.get("download"), value.get("upload")
    label = "优惠未知" if down is None else ("Free" if down == 0 else ("普通（100%）" if down == 1 else f"{down * 100:g}%"))
    if value.get("expired"):
        label = "优惠已到期，当前计量未知"
    if up is not None and up != 1:
        label += f" · 上传 {up:g} 倍"
    if value.get("until"):
        label += " · 优惠至 " + clean(value["until"], 40)
    return label


class MoviePilotClient:
    def __init__(self, config, http=None):
        base = str(config.get("server_url") or "").strip().rstrip("/")
        key = str(config.get("api_key") or "").strip()
        try:
            parsed = urlsplit(base)
            _ = parsed.port
        except ValueError:
            raise MoviePilotError("请配置有效的 MoviePilot 地址。") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or "\\" in base
                or any(ord(c) < 33 for c in base)):
            raise MoviePilotError("MoviePilot 地址应为完整服务地址，不含 API 路径或账号密码。")
        if not key or len(key) > 4096 or any(ord(c) < 33 for c in key):
            raise MoviePilotError("请在机器人插件配置中填写 MoviePilot API Key。")
        self.base = base
        self.key = key
        self.connection_key = digest(base, key)
        self.http = http or PluginHttpClient()
        # 整条插件调用共用预算，分页和多接口请求不能各自重新获得完整执行时限。
        self.deadline = time.monotonic() + 90

    def request(self, method, path, *, params=None, body=None, missing_ok=False, envelope=False):
        remaining = min(35, self.deadline - time.monotonic())
        if remaining <= 0:
            raise MoviePilotError("MoviePilot 查询时间过长，请稍后重试。")
        url = self.base + "/api/v1" + path
        if params:
            url += "?" + urlencode(params)
        writing = method != "GET"
        try:
            response = self.http.request(method, url, headers={"X-API-KEY": self.key}, json_body=body,
                connect_timeout_seconds=remaining, total_timeout_seconds=remaining,
                max_response_bytes=8 * 1024 * 1024, max_redirects=0, retry_count=0)
        except PluginHttpTimeoutError:
            raise MoviePilotError("MoviePilot 请求超时，请稍后查询状态。", uncertain=writing) from None
        except PluginHttpError:
            raise MoviePilotError("无法连接 MoviePilot，请管理员检查服务。", uncertain=writing) from None
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code in {401, 403}:
            raise MoviePilotError("MoviePilot 授权失效或权限不足，请管理员检查 API Key。")
        if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
            raise MoviePilotError("MoviePilot 请求失败，请稍后重试。", uncertain=writing)
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise MoviePilotError("MoviePilot 返回格式异常。", uncertain=writing) from None
        if not isinstance(payload, dict) or type(payload.get("success")) is not bool:
            raise MoviePilotError("MoviePilot 接口版本或响应格式不受支持。", uncertain=writing)
        if payload["success"] is not True:
            if method == "GET" and path.startswith("/search/media/") and payload.get("message") == "未搜索到任何资源":
                return []
            raise MoviePilotError("MoviePilot 未完成操作，请在 MP 页面查看原因。", uncertain=writing)
        return payload if envelope else payload.get("data")

    @staticmethod
    def rows(data) -> list[dict]:
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise MoviePilotError("MoviePilot 列表格式异常。")
        return data

    def search(self, title: str) -> list[dict]:
        data = self.rows(self.request("GET", "/media/search", params={"title": title, "type": "media",
            "media_source": "themoviedb", "count": 20}))
        return [m for x in data if (m := movie(x)) is not None][:5]

    def resources(self, selected: dict) -> list[dict]:
        return self.rows(self.request("GET", f"/search/media/{positive_id(selected['media_id'])}", params={
            "media_source": "themoviedb", "mtype": "电影", "count": 8}))[:8]

    def subscriptions(self) -> list[dict]:
        result = []
        for page in range(1, 12):
            rows = self.rows(self.request("GET", "/subscribe/", params={"page": page, "count": 100}))
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise MoviePilotError("订阅数量超过本插件查询上限，请在 MP 页面管理。")

    def create(self, selected: dict) -> tuple[int, bool]:
        payload = self.request("POST", "/subscribe/", envelope=True, body={"name": selected["title"], "year": selected["year"],
            "type": "电影", "media_source": "themoviedb", "media_id": selected["media_id"]})
        try:
            sid = positive_id(payload["data"].get("id"))
        except (AttributeError, KeyError, TypeError, MoviePilotError):
            raise MoviePilotError("订阅提交结果无法确认，请在 MP 页面核实。", uncertain=True) from None
        # V3 的 exist_ok 也会成功返回已有 ID。只认可运行源码明确的新增回执，
        # 未知文案（包括未来版本变化）仅登记关注，绝不能凭 HTTP 200 授予删除权限。
        return sid, payload.get("message") == "新增订阅成功"

    def delete(self, sid: int):
        self.request("DELETE", f"/subscribe/{positive_id(sid)}", missing_ok=True)

    def download(self, selected: dict, torrent: dict) -> str:
        """原生手动下载，不创建订阅；完整资源只在本次调用内存中交还同一个 MP。"""
        data = self.request("POST", "/download/add", body={"torrent_in": torrent,
            "media_source": "themoviedb", "media_id": str(positive_id(selected["media_id"])),
            "allow_unrecognized": False})
        did = data.get("download_id") if isinstance(data, dict) else None
        if isinstance(did, bool) or not isinstance(did, (str, int)) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(did)):
            raise MoviePilotError("下载提交结果无法确认，请在 MP 下载列表核实；不会自动重试。", uncertain=True)
        return str(did)

    def history(self, kind: str, *, page=1, count=100) -> list[dict]:
        if kind not in {"download", "transfer"}:
            raise ValueError("unsupported history kind")
        data = self.request("GET", "/history/" + kind, params={"page": page, "count": count})
        if kind == "transfer":
            if not isinstance(data, dict):
                raise MoviePilotError("MoviePilot 整理历史格式异常。")
            data = data.get("list")
        return self.rows(data)

    def baseline(self) -> dict:
        """在登记之前记录两类历史最新编号，快速完成的原生下载也不会漏掉。"""
        return {kind: max((positive_id(x.get("id")) for x in self.history(kind, count=100)), default=0)
                for kind in ("download", "transfer")}
