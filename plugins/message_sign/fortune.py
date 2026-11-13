"""Daily fortunes and cards adapted from MIT-licensed astrbot_plugin_jrys.

Upstream: fiatlux2333/astrbot_plugin_jrys, see LICENSE.upstream and README.md.
Business records and the points ledger remain owned by the existing plugin.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import json
import logging
import os
import random
import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.safe_fetch import open_public_url


ASSETS = Path(__file__).resolve().parent / "assets"
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000
MAX_BACKGROUND_BYTES = 5 * 1024 * 1024
MAX_BACKGROUND_PIXELS = 12_000_000
logger = logging.getLogger(__name__)
QUOTES = (
    "愿你今天的每一份认真，都能遇见小小的惊喜。",
    "不必赶路，也可以感受路。",
    "把平凡的日子，过成自己喜欢的样子。",
    "今天也要留一点时间，给喜欢的人和事。",
    "好运藏在行动里，快乐藏在日常里。",
)


@lru_cache(maxsize=1)
def fortune_tables() -> dict[str, Any]:
    return json.loads((ASSETS / "fortune_data.json").read_text(encoding="utf-8"))


def daily_fortune(tenant_id: int, group_id: str, user_id: str, current: datetime) -> dict[str, Any]:
    """Use an explicit business date and local RNG; retries and restarts cannot reroll."""
    day = current.date().isoformat()
    seed = json.dumps([tenant_id, group_id, user_id, day], ensure_ascii=False)
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    luck = rng.randrange(100)
    tables = fortune_tables()
    description = tables["fortunes"][0]["desc"]
    for item in tables["fortunes"]:
        if luck >= item["luck"]:
            description = item["desc"]
    events = rng.sample(tables["events"], 4)
    return {
        "date": day,
        "luck": luck,
        "description": description,
        "good_events": [{"name": item["name"], "text": item["good"]} for item in events[:2]],
        "bad_events": [{"name": item["name"], "text": item["bad"]} for item in events[2:]],
        "hitokoto": rng.choice(QUOTES),
    }


def experience_reward(fortune: Mapping[str, Any], low: int, high: int, identity: str) -> int:
    """Keep upstream's luck-weighted experience, independently of existing points."""
    seed = json.dumps([identity, fortune["date"], "experience"])
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    value = rng.gauss(int(fortune["luck"]) / 100, 0.12)
    if value > 1:
        value = 2 - value
    elif value < 0:
        value = -value
    return round(max(0, min(1, value)) * (high - low) + low)


def level_info(total_exp: int) -> dict[str, Any]:
    levels = fortune_tables()["levels"]
    level = levels[0]
    next_exp = None
    for index, item in enumerate(levels):
        if total_exp < item["levelExp"]:
            break
        level = item
        next_exp = levels[index + 1]["levelExp"] if index + 1 < len(levels) else None
    progress = 100 if next_exp is None else (total_exp - level["levelExp"]) * 100 / (next_exp - level["levelExp"])
    return {
        "level": level["level"], "name": level["levelName"],
        "color": level["levelColor"], "next_exp": next_exp,
        "progress": max(0, min(100, progress)),
    }


@lru_cache(maxsize=3)
def _asset_uri(filename: str) -> str:
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".woff2": "font/woff2"}[Path(filename).suffix]
    encoded = base64.b64encode((ASSETS / filename).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class _PublicImageOpener:
    def open(self, request, *, timeout):
        # Reuse the platform's public-address checks, including every redirect.
        return open_public_url(request.full_url, timeout=timeout)


def _avatar_data_uri(url: str) -> str:
    """Fetch a synced member avatar after commit; normalize pixels before embedding."""
    if not url:
        return _asset_uri("default_avatar.png")
    try:
        client = PluginHttpClient(opener_factory=lambda _: _PublicImageOpener())
        response = client.request(
            "GET", url, connect_timeout_seconds=2, total_timeout_seconds=2,
            max_response_bytes=MAX_AVATAR_BYTES, retry_count=0,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError("Avatar download failed")
        with Image.open(io.BytesIO(response.body)) as picture:
            if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("Unsupported avatar format")
            if picture.width * picture.height > MAX_AVATAR_PIXELS:
                raise ValueError("Avatar exceeds pixel limit")
            picture.seek(0)
            picture.thumbnail((168, 168))
            normalized = picture.convert("RGBA")
            normalized.info.clear()
            output = io.BytesIO()
            normalized.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:
        logger.warning("签到头像加载失败，已使用默认头像", extra={"error_type": type(exc).__name__})
        return _asset_uri("default_avatar.png")


def _background_data_uri(url: str) -> str:
    """Normalize a configured public image; failures never prevent a sign-in."""
    if not url:
        return _asset_uri("default_background.jpg")
    try:
        client = PluginHttpClient(opener_factory=lambda _: _PublicImageOpener())
        response = client.request(
            "GET", url, connect_timeout_seconds=3, total_timeout_seconds=3,
            max_response_bytes=MAX_BACKGROUND_BYTES, retry_count=0,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError("Background download failed")
        with Image.open(io.BytesIO(response.body)) as picture:
            if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("Unsupported background format")
            if picture.width * picture.height > MAX_BACKGROUND_PIXELS:
                raise ValueError("Background exceeds pixel limit")
            picture.seek(0)
            picture.thumbnail((1200, 800))
            # Flatten transparent images onto the card's white background.
            rgba = picture.convert("RGBA")
            normalized = Image.new("RGB", rgba.size, "white")
            normalized.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            normalized.save(output, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception as exc:
        logger.warning("签到背景加载失败，已使用内置背景", extra={"error_type": type(exc).__name__})
        return _asset_uri("default_background.jpg")


def build_card_html(view: Mapping[str, Any]) -> str:
    """Embed all assets and escape user text; rendering never needs network access."""
    esc = lambda value: html.escape(str(value), quote=True)
    fortune, level = view["fortune"], view["level"]
    avatar_uri = str(view.get("avatar_data_uri") or _asset_uri("default_avatar.png"))
    background_uri = str(view.get("background_data_uri") or _asset_uri("default_background.jpg"))
    good = "".join(f"<p>{esc(e['name'])}<span>{esc(e['text'])}</span></p>" for e in fortune["good_events"])
    bad = "".join(f"<p>{esc(e['name'])}<span>{esc(e['text'])}</span></p>" for e in fortune["bad_events"])
    success = view["provider_status"] == "signed"
    status = "签到成功！" if success else "今天已经签过到了"
    reward = f"积分 +{int(view['points_awarded'])} · 经验 +{int(view['exp_awarded'])}" if success else "今日奖励已领取"
    exp_text = f"{int(view['total_exp'])} / {level['next_exp']} EXP" if level["next_exp"] is not None else f"{int(view['total_exp'])} EXP · 已满级"
    rank = f" · 今日第 {int(view['todays_rank'])} 位" if view.get("todays_rank") else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'">
<style>
@font-face {{font-family:card;src:url('{_asset_uri('osans4.subset.woff2')}') format('woff2')}}
* {{box-sizing:border-box}}
html,body {{margin:0;padding:0;width:600px;background:#fff}}
body {{font-family:'Microsoft YaHei','Noto Sans CJK SC',card,sans-serif;color:#242933}}
.card {{width:600px;overflow:hidden;background:#fff}}
.hero {{width:600px;height:310px;display:block;object-fit:cover}}
.header {{position:relative;margin:-44px 26px 0;display:flex;gap:16px;align-items:center}}
.avatar {{width:84px;height:84px;border:4px solid #fff;border-radius:18px;object-fit:cover}}
.identity {{background:rgba(255,255,255,.95);border-radius:14px;padding:12px 18px;flex:1;min-width:0}}
.name {{font-size:25px;font-weight:bold;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.date {{font-size:15px;color:#707987;margin-top:3px}}
.content {{padding:18px 30px 8px}}
.status {{font-size:23px;font-weight:bold}}
.reward {{font-size:18px;color:#8b6541;margin-top:5px}}
.stats {{font-size:16px;color:#737c89;margin:12px 0;line-height:1.7}}
.level {{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
.level strong {{font-size:23px;color:{esc(level['color'])}}}
.level small {{font-size:15px;color:#8a929d}}
.track {{margin-top:9px;background:#edf0f4;height:12px;border-radius:6px;overflow:hidden}}
.progress {{height:100%;width:{float(level['progress']):.3f}%;background:#8a9bba}}
.fortune {{display:flex;gap:18px;align-items:center;margin:18px 0 12px}}
.luck {{font-size:49px;font-weight:bold;color:#47624f;line-height:1.1}}
.fortune small {{font-size:14px;color:#8a929d}}
.description {{font-size:20px;line-height:1.45;flex:1;min-width:0;overflow-wrap:anywhere}}
.events {{border-top:1px solid #e9ebef;padding-top:12px}}
.row {{display:flex;gap:14px;align-items:center;margin-bottom:14px}}
.badge {{width:42px;height:42px;flex-shrink:0;border-radius:50%;color:#fff;font-size:26px;display:flex;align-items:center;justify-content:center}}
.good {{background:#b75a4c}} .bad {{background:#566477}}
.items {{flex:1;min-width:0}} p {{margin:5px 0;font-size:17px;line-height:1.5;overflow-wrap:anywhere}}
p span {{color:#7b828c;font-size:15px;margin-left:10px}}
.quote {{text-align:center;font-size:15px;color:#8b929c;line-height:1.6;padding:0 30px 12px}}
.credit {{font-size:12px;color:#a0a6af;text-align:center;padding:0 15px 16px}}
</style></head><body><main class="card">
<img class="hero" src="{esc(background_uri)}" alt="">
<div class="header"><img class="avatar" src="{esc(avatar_uri)}" alt="">
<div class="identity"><div class="name">{esc(view['username'])}</div><div class="date">{esc(fortune['date'])} · 今日运势签到</div></div></div>
<div class="content"><div class="status">{status}</div><div class="reward">{reward}{rank}</div>
<div class="stats">连续 {int(view['signin_streak'])} 天 · 累计 {int(view['total_sign_count'])} 次 · 当前积分 {int(view['points'])}</div>
<div class="level"><strong>Lv.{int(level['level'])} {esc(level['name'])}</strong><small>{esc(exp_text)}</small></div>
<div class="track"><div class="progress"></div></div>
<div class="fortune"><div><div class="luck">{int(fortune['luck'])}</div><small>今日幸运值</small></div><div class="description">{esc(fortune['description'])}</div></div>
<div class="events"><div class="row"><div class="badge good">宜</div><div class="items">{good}</div></div>
<div class="row"><div class="badge bad">忌</div><div class="items">{bad}</div></div></div></div>
<div class="quote">{esc(fortune['hitokoto'])}</div><footer class="credit">随机生成，仅供娱乐 · Fulcrum</footer>
</main></body></html>"""


def _browser_path() -> str | None:
    if os.name == "nt":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if not root:
                continue
            for suffix in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                path = Path(root) / suffix
                if path.is_file():
                    return str(path)
    return shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


async def _render_png(html_content: str) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            headless=True, executable_path=_browser_path(), timeout=4500,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = await browser.new_page(viewport={"width": 600, "height": 1200}, device_scale_factor=1)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html_content, wait_until="load", timeout=2500)
            await page.evaluate("async () => { await document.fonts.ready; await Promise.all(Array.from(document.images, i => i.decode())); }")
            return await page.locator(".card").screenshot(type="png", animations="disabled", timeout=2000)
        finally:
            await browser.close()


def render_card(view: Mapping[str, Any]) -> str:
    async def render() -> bytes:
        # Fetch in parallel so the 3-second image budget plus 7-second browser
        # budget fits inside the plugin's 12-second execution timeout.
        avatar_uri, background_uri = await asyncio.gather(
            asyncio.to_thread(_avatar_data_uri, str(view.get("avatar_url") or "")),
            asyncio.to_thread(_background_data_uri, str(view.get("background_url") or "")),
        )
        prepared = {**view, "avatar_data_uri": avatar_uri, "background_data_uri": background_uri}
        return await asyncio.wait_for(_render_png(build_card_html(prepared)), timeout=7)

    return base64.b64encode(asyncio.run(render())).decode("ascii")
