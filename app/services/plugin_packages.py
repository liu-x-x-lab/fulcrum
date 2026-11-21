"""插件包的安装、升级、回滚与可恢复卸载：平台管理员操作，不重启服务即可生效。

安全边界:插件在服务进程内运行,拥有服务端代码执行权限,所以只有平台管理员能走这条路径(路由层校验平台会话与 CSRF),
租户永远不能上传。这里再做包本身的防护:路径穿越、符号链接、加密包、文件数与体积上限、清单必须有效、入口必须能通过语法检查。
校验全部在暂存目录完成,通过后才替换正式目录;替换失败会原样还原,不会留下半个插件。

目录布局(都在 plugins_dir 下,以下划线开头所以不会被当成插件扫描):
- _staging/<随机名>/   上传后的解压与校验临时区,结束即删除;
- _versions/<编码>/    每次升级或回滚前自动把当前版本打成 zip 存档(每个插件保留最近 MAX_KEPT_VERSIONS 个),回滚就是装回其中一个。
- _uninstalled/<编码>/<唯一标识>/ 卸载移入的完整目录，保留本地资源，不参与发现或自动清理。
"""

from __future__ import annotations

import io
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.core.settings import settings
from app.services import plugin_directory
from app.services.plugin_manifest import ManifestParseError, parse_manifest

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_FILES = 500
MAX_KEPT_VERSIONS = 10
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_IGNORED_PARTS = {"__pycache__", "__MACOSX"}
_ARCHIVE_NAME = re.compile(r"^(\d{14})-v([A-Za-z0-9._+-]{1,40})\.zip$")

# 同一进程内串行化:安装、升级、回滚会整体替换目录,不能交叉进行
_LOCK = threading.Lock()


class PluginPackageError(ValueError):
    """插件包不合法或操作无法完成;消息可以直接展示给平台管理员。"""


@dataclass(frozen=True)
class PackageResult:
    action: str  # install / upgrade / rollback
    code: str
    display_name: str
    from_version: str | None
    to_version: str


@dataclass(frozen=True)
class StoredVersion:
    filename: str
    version: str
    stored_at: str  # 14 位时间戳 YYYYmmddHHMMSS


def _base_dir() -> Path:
    return Path(settings.plugins_dir).resolve()


def _versions_dir(code: str) -> Path:
    return _base_dir() / "_versions" / code


def _clean_member_path(name: str) -> tuple[str, ...]:
    """压缩包里的路径拆成片段;绝对路径、反斜杠、盘符、.. 一律拒绝。"""

    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise PluginPackageError(f"压缩包里有不安全的路径:{name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") or (":" in part and index == 0) for index, part in enumerate(parts)):
        raise PluginPackageError(f"压缩包里有不安全的路径:{name!r}")
    return parts


def _collect_members(archive: zipfile.ZipFile) -> tuple[str, list[tuple[zipfile.ZipInfo, tuple[str, ...]]]]:
    """检查压缩包并确定插件根目录前缀,返回 (前缀, [(条目, 去掉前缀后的路径片段)])。"""

    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise PluginPackageError(f"压缩包文件数超过上限 {MAX_FILES}")
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info in infos:
        parts = _clean_member_path(info.filename)
        if any(part in _IGNORED_PARTS for part in parts) or parts[-1] == ".DS_Store":
            continue
        if info.flag_bits & 0x1:
            raise PluginPackageError("不支持加密的压缩包")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise PluginPackageError(f"压缩包里不允许符号链接:{info.filename}")
        members.append((info, parts))
    config_depths = {len(parts) for info, parts in members if parts[-1] == "config.toml" and not info.is_dir() and len(parts) <= 2}
    if config_depths == {1}:
        prefix = ""
    elif config_depths == {2}:
        prefix = next(parts[0] for info, parts in members if len(parts) == 2 and parts[1] == "config.toml")
    else:
        raise PluginPackageError("压缩包里必须有且只有一个插件目录,目录根部要有 config.toml")
    kept: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info, parts in members:
        if prefix:
            if parts[0] != prefix:
                raise PluginPackageError(f"压缩包里除了插件目录 {prefix}/ 之外还有其他内容:{info.filename}")
            parts = parts[1:]
        if parts:
            kept.append((info, parts))
    return prefix, kept


def _extract(archive: zipfile.ZipFile, members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]], dest: Path) -> None:
    """解压到暂存目录。体积按实际读到的字节数限制,不信任压缩包头里声明的大小。"""

    total = 0
    for info, parts in members:
        target = dest.joinpath(*parts)
        if not target.resolve().is_relative_to(dest.resolve()):
            raise PluginPackageError(f"压缩包里有不安全的路径:{info.filename!r}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, open(target, "wb") as out:
            while chunk := source.read(65536):
                total += len(chunk)
                if total > MAX_UNPACKED_BYTES:
                    raise PluginPackageError("解压后体积超过上限")
                out.write(chunk)


def _validate_staged(staged: Path, parent: Path):
    """校验暂存目录里的插件:编码合法、清单有效、入口存在且语法正确。返回 (编码, 已发现的插件, 放在 parent/编码 下的目录)。"""

    config_path = staged / "config.toml"
    try:
        parsed = parse_manifest(config_path.read_text(encoding="utf-8"))
    except (ManifestParseError, UnicodeDecodeError) as exc:
        raise PluginPackageError(f"config.toml 解析失败:{exc}") from exc
    code = str((parsed.get("plugin") or {}).get("code") or "").strip()
    if not CODE_PATTERN.match(code):
        raise PluginPackageError("plugin.code 必须是小写字母开头、只含小写字母/数字/下划线的 2-64 位编码")
    entrypoint = plugin_directory._resolve_entrypoint(staged)
    if not entrypoint:
        raise PluginPackageError("缺少入口文件(plugin.py)")
    named = parent / code
    staged.rename(named)
    config_path, entrypoint = named / "config.toml", named / entrypoint.name
    discovered = plugin_directory._build_discovered_plugin(named, config_path, entrypoint, parsed)
    if not discovered.is_valid:
        raise PluginPackageError("插件清单无效:" + ";".join(discovered.validation_errors))
    # 只做语法检查,不执行任何插件代码
    for source_file in named.rglob("*.py"):
        try:
            compile(source_file.read_bytes(), str(source_file), "exec")
        except (SyntaxError, ValueError) as exc:
            raise PluginPackageError(f"{source_file.relative_to(named)} 语法错误:{exc}") from exc
    return code, discovered, named


def _archive_current(live: Path, code: str, version: str) -> None:
    """把当前正在运行的版本打成 zip 存进 _versions,回滚就靠它。"""

    target_dir = _versions_dir(code)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_version = re.sub(r"[^A-Za-z0-9._+-]", "_", version or "unknown")[:40] or "unknown"
    stamp = time.strftime("%Y%m%d%H%M%S")
    path = target_dir / f"{stamp}-v{safe_version}.zip"
    counter = 1
    while path.exists():  # 同一秒内连续操作:不覆盖已有存档
        path = target_dir / f"{stamp}-v{safe_version}-{counter}.zip"
        counter += 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for file in sorted(live.rglob("*")):
            relative = file.relative_to(live)
            if file.is_symlink() or not file.is_file() or _IGNORED_PARTS & set(relative.parts) or file.suffix == ".pyc":
                continue
            out.write(file, relative.as_posix())
    for old in sorted(target_dir.glob("*.zip"))[:-MAX_KEPT_VERSIONS]:
        old.unlink(missing_ok=True)


def _forget_loaded_modules(code: str, live: Path) -> None:
    """让下一次调用重新加载新代码:清掉该插件的模块缓存和扫描缓存。"""

    import sys

    # 必须以目录分隔符结尾，避免卸载 demo 时误清 demo_extra 的模块缓存。
    prefix = str(live) + "/"
    for key in [key for key in plugin_directory._MODULE_CACHE if key.startswith(prefix)]:
        plugin_directory._MODULE_CACHE.pop(key, None)
    module_prefix = f"fulcrum_plugin_{code}_"
    for name in [name for name in sys.modules if name.startswith(module_prefix) and name[len(module_prefix):].isdigit()]:
        sys.modules.pop(name, None)
    plugin_directory._SCAN_CACHE.clear()


@contextmanager
def staged_uninstall(code: str) -> Iterator[str]:
    """移走完整插件目录，并把数据库提交放在可恢复的文件操作边界内。

    调用方必须在 with 内完成状态、审计及 commit；异常时原路径恢复。
    成功后保留完整目录（含本地资源），不受版本 ZIP 的体积和保留数量限制。
    与安装、升级、回滚共用进程锁，防止本进程交叉覆盖同一目录。
    """

    if not CODE_PATTERN.fullmatch(code or ""):
        raise PluginPackageError("插件编码不合法，不能卸载公共目录或目录之外的文件")
    with _LOCK:
        base = _base_dir()
        live = base / code
        if live.is_symlink() or live.resolve().parent != base:
            raise PluginPackageError("插件目录不能是符号链接或指向插件根目录之外")
        if not live.is_dir():
            raise PluginPackageError("插件目录不存在，请刷新列表")
        # 扫描器会把声明编码归一为目录名，因此必须读取原始声明才能识别目录被替换。
        # 损坏或缺失清单仍允许卸载；存在明确的其他插件身份时拒绝旧页面操作。
        try:
            parsed = parse_manifest((live / "config.toml").read_text(encoding="utf-8"))
        except (FileNotFoundError, ManifestParseError, UnicodeError):
            parsed = {}
        except OSError as exc:
            raise PluginPackageError("无法读取插件清单，请检查目录权限") from exc
        plugin_block = parsed.get("plugin", {})
        declared_code = str(plugin_block.get("code") or "").strip() if isinstance(plugin_block, dict) else ""
        if declared_code and declared_code != code:
            raise PluginPackageError("插件编码与目录不一致，不能卸载")
        # 不允许恢复副本目录通过符号链接写到别处；父目录仅由本服务创建。
        recovery = base / "_uninstalled"
        for folder in (recovery, recovery / code):
            if folder.is_symlink() or folder.resolve().parent not in (base, recovery):
                raise PluginPackageError("卸载备份目录不安全")
            try:
                folder.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise PluginPackageError("无法建立卸载备份，请检查目录权限") from exc
        backup = recovery / code / (time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex)
        try:
            live.rename(backup)
        except OSError as exc:
            raise PluginPackageError("无法移走插件目录，原插件未卸载") from exc
        try:
            _forget_loaded_modules(code, live)
            yield backup.relative_to(base).as_posix()
        except BaseException:
            # 外部操作若重新创建了同名路径，不覆盖它，也不删除仍可找回的副本。
            if live.exists() or live.is_symlink():
                raise PluginPackageError("卸载未完成，原路径已被其他操作占用；恢复副本仍保留")
            try:
                backup.rename(live)
            except OSError as exc:
                raise PluginPackageError("卸载未完成且目录恢复失败，恢复副本仍保留，请检查目录权限") from exc
            _forget_loaded_modules(code, live)
            raise


def _install_zip(data: bytes, *, expect_code: str | None, action_hint: str) -> PackageResult:
    if not data:
        raise PluginPackageError("上传的文件是空的")
    if len(data) > MAX_PACKAGE_BYTES:
        raise PluginPackageError(f"插件包超过 {MAX_PACKAGE_BYTES // 1024 // 1024}MB 上限")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise PluginPackageError("不是有效的 zip 文件")
    base = _base_dir()
    staging_root = base / "_staging" / uuid.uuid4().hex
    with _LOCK:
        try:
            staging_root.mkdir(parents=True)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                _prefix, members = _collect_members(archive)
                _extract(archive, members, staging_root / "pkg")
            code, discovered, staged_dir = _validate_staged(staging_root / "pkg", staging_root)
            if expect_code and code != expect_code:
                raise PluginPackageError(f"插件包的编码是 {code},与要回滚的插件 {expect_code} 不一致")
            live = base / code
            previous = None
            if live.exists():
                previous = plugin_directory.get_discovered_plugin(code, force=True)
                _archive_current(live, code, previous.manifest.version if previous else "unknown")
            trash = staging_root / "old"
            if live.exists():
                live.rename(trash)
            try:
                staged_dir.rename(live)
            except Exception:
                if trash.exists():
                    trash.rename(live)  # 替换失败:原样还原
                raise
            _forget_loaded_modules(code, live)
            return PackageResult(
                action="install" if previous is None else action_hint,
                code=code,
                display_name=discovered.manifest.display_name,
                from_version=previous.manifest.version if previous else None,
                to_version=discovered.manifest.version,
            )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)


def install_or_upgrade(data: bytes) -> PackageResult:
    """上传一个插件包:编码不存在就是安装,已存在就是升级(旧版本自动存档,可回滚)。"""

    return _install_zip(data, expect_code=None, action_hint="upgrade")


def list_versions(code: str) -> list[StoredVersion]:
    """某个插件已存档的历史版本,新的在前。"""

    if not CODE_PATTERN.match(code or ""):
        return []
    found = []
    for path in _versions_dir(code).glob("*.zip"):
        match = _ARCHIVE_NAME.match(path.name)
        if match:
            found.append(StoredVersion(filename=path.name, version=match.group(2), stored_at=match.group(1)))
    return sorted(found, key=lambda item: item.filename, reverse=True)


def rollback(code: str, filename: str) -> PackageResult:
    """回滚到某个存档版本。回滚前当前版本同样会存档,所以回滚本身也能再撤销。"""

    stored = {item.filename for item in list_versions(code)}
    if filename not in stored:  # 只接受清单里的文件名,不拼接用户输入的路径
        raise PluginPackageError("找不到这个历史版本")
    data = (_versions_dir(code) / filename).read_bytes()
    result = _install_zip(data, expect_code=code, action_hint="rollback")
    return PackageResult("rollback", result.code, result.display_name, result.from_version, result.to_version)
