"""存量媒体先扫描后确认；计划仅绑定当前租户和管理员，不接受客户端文件路径。"""

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from app.core.tenant_context import require_tenant_id
from app.services import media_library, media_storage

MAX_FILES = 1000
MAX_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 10
PLAN_SECONDS = 30 * 60
PLAN_BYTES = 1024 * 1024


def _plan_path(user_id: int) -> Path:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("整理用户无效")
    root = media_storage.storage_root(require_tenant_id())
    return media_storage.checked_path(root / "plans" / f"{user_id}.json", root)


def _save(user_id: int, plan: dict) -> None:
    """每个管理员只保留最后一次计划，防止反复扫描积累无界的临时清单。"""

    path = _plan_path(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".plan-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(plan, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def review(user_id: int, token: str) -> dict:
    """编号只用于匹配服务端计划；跨用户、跨租户、过期及已执行计划一律拒绝。"""

    try:
        if re.fullmatch(r"[0-9a-f]{32}", token) is None:
            raise ValueError
        with _plan_path(user_id).open("rb") as source:
            content = source.read(PLAN_BYTES + 1)
        if len(content) > PLAN_BYTES:
            raise ValueError
        plan = json.loads(content)
        if (plan["tenant_id"] != require_tenant_id() or plan["user_id"] != user_id
                or not secrets.compare_digest(plan["token"], token) or plan["state"] != "ready"
                or not time.time() < plan["expires_at"] <= time.time() + PLAN_SECONDS):
            raise ValueError
        groups = plan["groups"]
        if not isinstance(groups, list) or len(groups) > MAX_FILES:
            raise ValueError
        entries = []
        for group in groups:
            if re.fullmatch(r"[0-9a-f]{64}", group["digest"]) is None or not isinstance(group["items"], list):
                raise ValueError
            for entry in group["items"]:
                snapshot = entry["signature"]
                if (not isinstance(snapshot, list) or len(snapshot) != 4
                        or any(type(value) is not int or value < 0 for value in snapshot)
                        or not isinstance(entry["ref"], str) or len(entry["ref"]) > 80):
                    raise ValueError
                entries.append(entry)
        if (len(entries) > MAX_FILES or len({entry["ref"] for entry in entries}) != len(entries)
                or sum(entry["signature"][2] for entry in entries) > MAX_BYTES):
            raise ValueError
        return plan
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("扫描结果不存在、已过期或已使用，请重新扫描") from None


def scan(db, *, user_id: int, channel_id: int = 0) -> dict:
    """只读已授权原件，先按大小筛选；到达任一上限就报告部分结果。"""

    tenant_id = require_tenant_id()
    with media_storage.locked(tenant_id, "dedup", timeout=1):
        deadline = time.monotonic() + MAX_SECONDS
        catalog = media_library.collect(db, channel_id=channel_id, limit=MAX_FILES, deadline=deadline)
        if channel_id and channel_id not in {channel.id for channel in catalog["channels"]}:
            raise ValueError("账号不存在或无权访问")
        by_size = defaultdict(list)
        for item in catalog["items"]:
            if item.size:
                by_size[item.size].append(item)
        hashed, skipped, inspected = 0, 0, 0
        limited = catalog["limited"]
        by_digest = defaultdict(list)
        for candidates in by_size.values():
            if len(candidates) < 2:
                continue
            for item in candidates:
                if time.monotonic() >= deadline or hashed + item.size > MAX_BYTES:
                    limited = True
                    skipped += 1
                    continue
                try:
                    media_library.check_delete(db, item)
                    hashed += item.size
                    digest, snapshot = media_storage.file_digest(item.path, max_bytes=item.size, deadline=deadline)
                    by_digest[digest].append({"ref": item.ref, "name": item.name, "channel_name": item.channel_name,
                        "signature": list(snapshot), "links": item.path.stat().st_nlink})
                    inspected += 1
                except (OSError, ValueError, TypeError):
                    skipped += 1
        groups = []
        for digest, entries in by_digest.items():
            inodes = Counter(tuple(entry["signature"][:2]) for entry in entries)
            if len(inodes) < 2:
                continue
            canonical = tuple(entries[0]["signature"][:2])
            try:
                blob = media_storage.blob_path(tenant_id, digest)
                if blob.exists():
                    canonical = media_storage.signature(blob)[:2]
            except (OSError, ValueError):
                skipped += len(entries)
                continue
            counted, saved = set(), 0
            for entry in entries:
                inode = tuple(entry["signature"][:2])
                if inode != canonical and inode not in counted and entry["links"] == inodes[inode]:
                    saved += entry["signature"][2]
                counted.add(inode)
            if saved:
                groups.append({"digest": digest, "items": entries, "saved_bytes": saved})
        plan = {"tenant_id": tenant_id, "user_id": user_id, "token": secrets.token_hex(16), "state": "ready",
            "expires_at": time.time() + PLAN_SECONDS, "channel_id": channel_id, "groups": groups,
            "saved_bytes": sum(group["saved_bytes"] for group in groups), "scanned": len(catalog["items"]),
            "hashed": inspected, "skipped": skipped, "limited": limited or time.monotonic() >= deadline}
        _save(user_id, plan)
        return plan


def apply(db, *, user_id: int, token: str) -> dict:
    """一次性领取计划后逐项复验；中断或部分失败必须重新扫描，不能重放整批操作。"""

    tenant_id = require_tenant_id()
    with media_storage.locked(tenant_id, "dedup", timeout=1):
        plan = review(user_id, token)
        if not plan["groups"]:
            raise ValueError("没有可整理的重复文件")
        plan["state"] = "used"
        _save(user_id, plan)
        deadline, consumed = time.monotonic() + MAX_SECONDS, 0
        result = {"merged": 0, "skipped": 0, "saved_bytes": 0}
        for group in plan["groups"]:
            for entry in group["items"]:
                size = entry["signature"][2]
                if time.monotonic() >= deadline or consumed + size > MAX_BYTES:
                    result["skipped"] += 1
                    continue
                try:
                    item = media_library.resolve(db, entry["ref"])
                    media_library.check_delete(db, item)
                    consumed += size
                    outcome = media_storage.adopt(item.path, tenant_id=tenant_id, boundary=item.path.parent,
                        expected=entry["signature"], expected_digest=group["digest"], max_bytes=size, deadline=deadline)
                    if media_storage.signature(item.path)[:2] != tuple(entry["signature"][:2]):
                        result["merged"] += 1
                    result["saved_bytes"] += outcome["saved_bytes"]
                except (OSError, ValueError, TypeError):
                    result["skipped"] += 1
        return result
