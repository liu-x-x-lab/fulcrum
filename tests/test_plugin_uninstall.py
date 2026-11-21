"""平台卸载的真实权限、文件回退和租户数据保留边界，全部使用临时目录与 SQLite。"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.settings import settings
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin, PluginGrant, PluginStateRecord
from app.models.saas import PlatformAuditLog, PlatformUser, Tenant
from app.services import plugin_directory, plugin_packages
from app.services.plugin_governance import blocked_plugin_codes
from app.services.plugin_scopes import plugin_group_conversation_enabled
from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal
from app.web import platform_plugins
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


def package(version="1.0.0"):
    """只打包最小虚构插件，避免依赖工作区的公开或私有插件。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("config.toml", f'''[plugin]
code = "uninstall_demo"
display_name = "卸载测试插件"
version = "{version}"
[capability]
channel_types = ["simulator"]
chat_types = ["group", "direct"]
message_types = ["text"]
[matcher]
commands = ["演示"]
''')
        archive.writestr("plugin.py", "def execute_action(request):\n    return None\n")
    return output.getvalue()


@pytest.fixture()
def context(tmp_path, monkeypatch):
    """恢复真实平台会话，两个租户的绑定用于证明卸载不会级联删数据。"""
    root = tmp_path / "plugins"
    monkeypatch.setattr(settings, "plugins_dir", str(root))
    plugin_directory._SCAN_CACHE.clear()
    plugin_packages.install_or_upgrade(package())
    live = root / "uninstall_demo"
    (live / "resources").mkdir()
    (live / "resources" / "local-data.txt").write_text("preserve local resources")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        plugin_directory.sync_plugins_from_directory(db)
        plugin = db.scalar(select(Plugin).where(Plugin.code == "uninstall_demo"))
        admin = PlatformUser(username="uninstall-admin", display_name="测试平台管理员", role="platform_admin",
                             password_hash=hash_password("Uninstall-admin-123"), is_enabled=True,
                             auth_version=1, must_change_password=False)
        db.add(admin)
        ids = []
        for code in ["uninstall-alpha", "uninstall-beta"]:
            tenant = Tenant(code=code, name=code, status="active")
            db.add(tenant)
            db.flush()
            bot = Bot(tenant_id=tenant.id, name=code)
            db.add(bot)
            db.flush()
            binding = BotPlugin(tenant_id=tenant.id, bot_id=bot.id, plugin_id=plugin.id, status="enabled",
                                config_json='{"retained":true}', secret_config_encrypted="test-encrypted-value")
            db.add(binding)
            db.flush()
            db.add(PluginStateRecord(tenant_id=tenant.id, bot_plugin_id=binding.id,
                                    namespace="test", state_key="saved", payload_json='{"count":3}'))
            ids.append((tenant.id, bot.id, binding.id))
        db.add(PluginGrant(plugin_id=plugin.id, scope_type="tenant", scope_id=ids[0][0]))
        db.commit()
        session = create_platform_session(db, admin)
        plugin_id, actor_id = plugin.id, admin.id
    monkeypatch.setattr(platform_plugins, "SessionLocal", factory)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        with factory() as db:
            request.state.platform_principal = load_platform_principal(db, request.cookies.get(PLATFORM_SESSION_COOKIE))
        return await call_next(request)

    app.include_router(platform_plugins.router)
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, session.raw_token)
        client.cookies.set(PLATFORM_CSRF_COOKIE, session.raw_csrf_token)
        yield SimpleNamespace(client=client, csrf=session.raw_csrf_token, root=root, live=live, factory=factory,
                              plugin_id=plugin_id, actor_id=actor_id, bindings=ids)
    engine.dispose()
    plugin_directory._SCAN_CACHE.clear()
    plugin_packages._forget_loaded_modules("uninstall_demo", live)


def uninstall(ctx, **kwargs):
    return ctx.client.post("/platform/plugins/uninstall_demo/uninstall", data={"csrf_token": ctx.csrf}, **kwargs)


def assert_retained(ctx, db):
    """绑定身份、密文、状态和授权必须逐项保留，不以仍有 Plugin 行代替验证。"""
    for _tenant, _bot, binding_id in ctx.bindings:
        row = db.get(BotPlugin, binding_id)
        assert row.status == "enabled" and row.config_json == '{"retained":true}'
        assert row.secret_config_encrypted == "test-encrypted-value"
        state = db.scalar(select(PluginStateRecord).where(PluginStateRecord.bot_plugin_id == binding_id))
        assert state.payload_json == '{"count":3}'
    assert db.scalar(select(PluginGrant).where(PluginGrant.plugin_id == ctx.plugin_id)) is not None


def test_uninstall_preserves_data_hides_card_and_reinstall_requires_enable(context):
    ctx = context
    before = ctx.client.get("/platform/plugins")
    assert 'data-pp-uninstall' in before.text and 'data-confirm-mode="confirm"' in before.text
    assert '2 个租户' in before.text
    plugin_directory.load_directory_plugin("uninstall_demo")
    reply = uninstall(ctx)
    assert reply.status_code == 303 and "tone=danger" not in reply.headers["location"]
    assert not ctx.live.exists()
    backups = list((ctx.root / "_uninstalled" / "uninstall_demo").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "resources/local-data.txt").read_text() == "preserve local resources"
    assert plugin_directory.get_discovered_plugin("uninstall_demo") is None
    assert not any(key.startswith(str(ctx.live) + "/") for key in plugin_directory._MODULE_CACHE)
    with ctx.factory() as db:
        row = db.get(Plugin, ctx.plugin_id)
        assert row.platform_status == "disabled" and row.missing_at is not None
        assert_retained(ctx, db)
        assert "uninstall_demo" in blocked_plugin_codes(db, ctx.bindings[0][0])
        audit = db.scalar(select(PlatformAuditLog).where(PlatformAuditLog.action == "plugin.uninstall"))
        assert audit.target_id == ctx.plugin_id and audit.actor_user_id == ctx.actor_id
        assert "_uninstalled/uninstall_demo/" in audit.detail
    assert 'data-code="uninstall_demo"' not in ctx.client.get("/platform/plugins").text
    assert "uninstall_demo" not in ctx.client.get("/platform/plugins/usage").json()["plugins"]
    enabled = ctx.client.post("/platform/plugins/uninstall_demo/status", json={"value": True}, headers={"X-CSRF-Token": ctx.csrf})
    assert enabled.status_code == 409
    # 再次上传同编码后沿用旧身份与配置，平台停用状态不能被安装动作悄悄解除。
    uploaded = ctx.client.post("/platform/plugins/upload", data={"csrf_token": ctx.csrf}, files={"package": ("demo.zip", package("2.0.0"), "application/zip")})
    assert uploaded.status_code == 303
    assert "请手动启用" in unquote(uploaded.headers["location"])
    with ctx.factory() as db:
        row = db.get(Plugin, ctx.plugin_id)
        assert row.version == "2.0.0" and row.missing_at is None and row.platform_status == "disabled"
        assert_retained(ctx, db)
    assert ctx.client.post("/platform/plugins/uninstall_demo/status", json={"value": True}, headers={"X-CSRF-Token": ctx.csrf}).status_code == 200


def test_manual_directory_removal_is_reflected_on_next_page_load(context):
    ctx = context
    assert 'data-code="uninstall_demo"' in ctx.client.get("/platform/plugins").text
    shutil.rmtree(ctx.live)
    page = ctx.client.get("/platform/plugins")
    assert page.status_code == 200 and 'data-code="uninstall_demo"' not in page.text
    assert "未安装" not in page.text
    with ctx.factory() as db:
        assert db.get(Plugin, ctx.plugin_id).missing_at is not None
        assert "uninstall_demo" in blocked_plugin_codes(db, ctx.bindings[0][0])
        assert_retained(ctx, db)


@pytest.mark.parametrize("authenticated,csrf,expected", [(False, True, 401), (True, False, 403)])
def test_uninstall_requires_platform_session_and_csrf(context, authenticated, csrf, expected):
    ctx = context
    if not authenticated:
        ctx.client.cookies.clear()
        ctx.client.cookies.set("tenant_session", "tenant-cookie-cannot-authorize-platform")
    response = ctx.client.post("/platform/plugins/uninstall_demo/uninstall", data={"csrf_token": ctx.csrf if csrf else "forged"})
    assert response.status_code == expected and ctx.live.is_dir()
    assert not (ctx.root / "_uninstalled").exists()


def test_failed_commit_restores_files_and_keeps_original_state(context, monkeypatch):
    from app.services.plugin_uninstall import uninstall_plugin
    ctx = context
    with ctx.factory() as db:
        def fail_commit():
            raise RuntimeError("simulated commit failure")
        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            uninstall_plugin(db, db.get(Plugin, ctx.plugin_id), actor_user_id=ctx.actor_id)
    assert (ctx.live / "resources/local-data.txt").read_text() == "preserve local resources"
    assert plugin_directory.get_discovered_plugin("uninstall_demo") is not None
    with ctx.factory() as db:
        assert db.get(Plugin, ctx.plugin_id).platform_status == "active"
        assert db.get(Plugin, ctx.plugin_id).missing_at is None
        assert db.scalar(select(PlatformAuditLog).where(PlatformAuditLog.action == "plugin.uninstall")) is None
        assert_retained(ctx, db)


@pytest.mark.parametrize("bad_code", ["../outside", "_shared", "uninstall_demo/../other", "uninstall_demo\n"])
def test_uninstall_rejects_paths_and_reserved_directories(context, bad_code):
    with pytest.raises(plugin_packages.PluginPackageError):
        with plugin_packages.staged_uninstall(bad_code):
            pytest.fail("unsafe path reached transaction")
    assert context.live.is_dir()


@pytest.mark.parametrize("target", ["live", "backup"])
def test_uninstall_does_not_follow_symlinks(context, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    if target == "live":
        shutil.rmtree(context.live)
        context.live.symlink_to(outside, target_is_directory=True)
    else:
        (context.root / "_uninstalled").symlink_to(outside, target_is_directory=True)
    with pytest.raises(plugin_packages.PluginPackageError):
        with plugin_packages.staged_uninstall("uninstall_demo"):
            pytest.fail("symlink reached transaction")
    assert (outside / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("chat_type", ["group", "direct"])
def test_removed_plugin_cannot_deliver_previously_queued_messages(context, chat_type):
    ctx = context
    uninstall(ctx)
    tenant_id, bot_id, _binding_id = ctx.bindings[0]
    with ctx.factory() as db:
        channel = Channel(tenant_id=tenant_id, bot_id=bot_id, name="test", channel_type="simulator")
        db.add(channel)
        db.flush()
        conversation = Conversation(tenant_id=tenant_id, bot_id=bot_id, channel_id=channel.id,
                                    channel_type="simulator", chat_type=chat_type, external_id="test-recipient")
        db.add(conversation)
        db.flush()
        assert not plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code="uninstall_demo")
        assert plugin_group_conversation_enabled(db, channel=channel, conversation=conversation, plugin_code="ai_dispatch")


def test_failed_directory_move_does_not_disable_plugin(context, monkeypatch):
    """文件系统拒绝移动时不能提前停用数据库记录或丢失原目录。"""
    ctx = context
    rename = Path.rename
    def denied(source, destination):
        if source == ctx.live:
            raise PermissionError("simulated readonly plugin mount")
        return rename(source, destination)
    monkeypatch.setattr(Path, "rename", denied)
    response = uninstall(ctx)
    assert response.status_code == 303 and "tone=danger" in response.headers["location"]
    assert ctx.live.is_dir()
    with ctx.factory() as db:
        assert db.get(Plugin, ctx.plugin_id).platform_status == "active"
        assert db.get(Plugin, ctx.plugin_id).missing_at is None
        assert_retained(ctx, db)


def test_changed_manifest_identity_cannot_uninstall_another_plugin(context):
    """页面打开后若目录被替换为另一插件，旧操作不能把新插件一起移走。"""
    config = context.live / "config.toml"
    config.write_text(config.read_text().replace('code = "uninstall_demo"', 'code = "another_plugin"'))
    response = uninstall(context)
    assert response.status_code == 303 and "tone=danger" in response.headers["location"]
    assert context.live.is_dir()


def test_uninstall_preserves_neighbour_plugin_module_cache(context, monkeypatch):
    """相同编码前缀不能造成跨插件缓存失效。"""
    import sys
    from types import ModuleType
    module_name = "fulcrum_plugin_uninstall_demo_extra_123"
    module = ModuleType(module_name)
    monkeypatch.setitem(sys.modules, module_name, module)
    uninstall(context)
    assert sys.modules[module_name] is module
