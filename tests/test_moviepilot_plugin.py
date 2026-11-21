"""电影插件的真实数据库隔离、原生订阅权限、上游写入不确定性与通知幂等。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select, delete
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin, PluginStateRecord, TaskJob
from app.models.moviepilot import MoviePilotDownload, MoviePilotSubscription
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_manifest import parse_manifest, read_config_fields
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse, PluginHttpTimeoutError
from app.services.plugin_runtime.state_store import PluginStateStore
from plugins.moviepilot import client, notifications, plugin
from plugins.moviepilot.state import MoviePilotSession, SELECTION_NAMESPACE

CONFIG = {"server_url": "https://mp.example.test", "api_key": "fictional-api-key", "notifications_enabled": True}
MOVIE = {"media_source": "themoviedb", "media_id": "123", "type": "电影", "title": "示例电影", "year": "2025",
         "overview": "示例简介", "poster_path": "https://image.tmdb.org/t/p/w500/example.jpg"}


class FakeHTTP:
    def __init__(self):
        self.calls, self.subs = [], []
        self.histories = {"download": [], "transfer": []}
        self.search_rows = [MOVIE]
        self.override = None
        self.create_timeout = False
        self.delete_timeout = False
        self.create_message = "新增订阅成功"
        self.download_timeout = False
        self.download_result = {"download_id": "hash123"}
        self.resources_rows = [{"media_info": MOVIE.copy(), "torrent_info": {
            "title": "Example.1080p", "size": 1024**3, "seeders": 12, "site": 7, "site_name": "Example PT",
            "page_url": "https://tracker.example.test/details?id=1", "downloadvolumefactor": 0.5,
            "uploadvolumefactor": 1, "site_cookie": "private-cookie",
            "enclosure": "https://private.test/?passkey=hidden"}}]

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        path, params = parsed.path.removeprefix("/api/v1"), parse_qs(parsed.query)
        self.calls.append((method, path, params, kwargs))
        if self.override is not None:
            return self.override
        data = None
        if path == "/media/search":
            data = self.search_rows
        elif path.startswith("/search/media/"):
            data = self.resources_rows
        elif path == "/download/add" and method == "POST":
            if self.download_timeout:
                raise PluginHttpTimeoutError("sensitive download error")
            data = self.download_result
        elif path == "/subscribe/" and method == "GET":
            data = self.subs
        elif path == "/subscribe/" and method == "POST":
            self.subs.append({**kwargs["json_body"], "id": 19, "date": "2026-01-01 12:00:00", "username": "service", "state": "N"})
            if self.create_timeout:
                raise PluginHttpTimeoutError("sensitive upstream debug")
            data = {"id": 19}
        elif path.startswith("/subscribe/") and method == "DELETE":
            self.subs = []
            if self.delete_timeout:
                raise PluginHttpTimeoutError("sensitive upstream debug")
            data = {"status": "deleted"}
        elif path.startswith("/history/"):
            kind = path.split("/")[-1]
            count, page = int(params["count"][0]), int(params["page"][0])
            rows = sorted(self.histories[kind], key=lambda x: x["id"], reverse=True)
            rows = rows[(page - 1) * count:page * count]
            data = rows if kind == "download" else {"list": rows, "total": len(self.histories[kind])}
        else:
            raise AssertionError((method, path))
        return PluginHttpResponse(200, {"content-type": "application/json"},
            json.dumps({"success": True, "data": data,
                "message": self.create_message if method == "POST" else ""}).encode(), url)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with platform_session(factory) as db:
        db.add_all([Tenant(id=1, code="one", name="One"), Tenant(id=2, code="two", name="Two"),
                    Plugin(id=1, code="moviepilot", display_name="MoviePilot")])
        db.commit()
    with tenant_scope(1), tenant_session(factory) as db:
        db.add(Bot(id=1, tenant_id=1, name="bot"))
        db.add(Channel(id=1, tenant_id=1, bot_id=1, name="channel", channel_type="simulator", is_enabled=True))
        db.add(BotPlugin(id=1, tenant_id=1, bot_id=1, plugin_id=1, status="enabled"))
        for n in (1, 2):
            db.add(Conversation(id=n, tenant_id=1, bot_id=1, channel_id=1, channel_type="simulator", external_id=f"group{n}", chat_type="group"))
            db.add(Chatroom(id=n, tenant_id=1, bot_id=1, channel_id=1, external_id=f"group{n}",
                is_active=True, management_status="enabled"))
        db.commit()
        fake = FakeHTTP()
        monkeypatch.setattr(client, "PluginHttpClient", lambda: fake)
        context = PluginExecutionContext(tenant_id=1, binding_id=1, bot_id=1, channel_id=1,
            conversation_id=1, sender_id="alice", chat_type="group", channel_type="simulator")
        yield db, context, fake, factory
    engine.dispose()


def say(env, command, arg="", *, context=None, config=None):
    db, ctx, _, _ = env
    return plugin.execute_message_action(db, PluginExecutionRequest(action="execute", config=config or CONFIG,
        context=context or ctx, payload={"command_name": command, "command_args": arg}))


def text(result):
    return "\n".join(r.get("content", "") for r in result.output.get("replies", []))


def clear_limits(db):
    db.execute(delete(PluginStateRecord).where(PluginStateRecord.namespace == "rate_limit"))
    db.flush()


def register(env, *, context=None, baseline=None):
    db, ctx, _, _ = env
    api = client.MoviePilotClient(CONFIG)
    session = MoviePilotSession(db, context or ctx, api.connection_key)
    return session.register(client.movie(MOVIE), baseline or {"download": 0, "transfer": 0})


def poll(env, *, context=None, config=None):
    db, ctx, _, _ = env
    return plugin.execute_scheduled_action(db, PluginExecutionRequest(action="poll_notifications", config=config or CONFIG,
        context=context or replace(ctx, conversation_id=None, channel_id=None, sender_id=None)))


def test_manifest_and_reload_are_installable_and_side_effect_free(env):
    manifest = parse_manifest(Path("plugins/moviepilot/config.toml").read_text())
    fields, errors = read_config_fields(manifest)
    assert not errors
    assert {x.key for x in fields if x.kind == "secret"} == {"server_url", "api_key"}
    assert get_discovered_plugin("moviepilot", force=True).is_valid
    assert load_directory_plugin("moviepilot", force=True).instance is not None
    assert plugin.execute_action(PluginExecutionRequest(action="reload", config={})).ok
    assert not env[2].calls


def test_movie_search_selection_resource_projection_and_automatic_download_notice(env):
    result = say(env, "搜电影", "示例电影")
    assert result.ok and "自动下载" in text(result)
    detail = say(env, "选电影", "1")
    assert detail.ok and detail.output["replies"][1]["thumb_url"].startswith("https://image.tmdb.org/")
    resources = say(env, "找资源", "1")
    assert "1.0 GB" in text(resources) and "做种 12" in text(resources)
    assert "private-cookie" not in str(resources) and "passkey" not in str(resources)
    assert all(call[3]["max_redirects"] == 0 and call[3]["retry_count"] == 0 for call in env[2].calls)
    assert all(call[0] == "GET" for call in env[2].calls)


def test_selection_isolated_by_sender_group_connection_and_empty_search(env):
    say(env, "搜电影", "示例")
    for ctx in (replace(env[1], sender_id="bob"), replace(env[1], conversation_id=2)):
        assert not say(env, "订阅电影", "1", context=ctx).ok
        clear_limits(env[0])
    assert not say(env, "选电影", "1", config={**CONFIG, "api_key": "changed"}).ok
    env[2].search_rows = []
    say(env, "搜电影", "不存在")
    assert not say(env, "选电影", "1").ok
    assert not any(x[0] == "POST" for x in env[2].calls)


def test_create_list_and_owner_only_cancellation(env):
    say(env, "搜电影", "示例")
    result = say(env, "订阅电影", "1")
    assert result.ok and "自动" in text(result)
    db, ctx, fake, _ = env
    row = db.scalar(select(MoviePilotSubscription))
    assert row.can_delete and row.remote_id == 19
    assert "示例电影" in text(say(env, "我的电影订阅"))
    for other in (replace(ctx, sender_id="bob"), replace(ctx, conversation_id=2)):
        clear_limits(db)
        assert not say(env, "取消电影订阅", str(row.id), context=other).ok
    assert not any(x[0] == "DELETE" for x in fake.calls)
    clear_limits(db)
    assert say(env, "取消电影订阅", str(row.id)).ok
    assert row.status == "cancelled" and len([x for x in fake.calls if x[0] == "DELETE"]) == 1


def test_existing_subscription_can_only_be_watched(env):
    env[2].subs = [{**MOVIE, "id": 8, "date": "old", "username": "other"}]
    say(env, "搜电影", "示例")
    result = say(env, "订阅电影", "1")
    assert "已有" in text(result)
    clear_limits(env[0])
    assert say(env, "关注电影", "1").ok
    row = env[0].scalar(select(MoviePilotSubscription))
    assert not row.can_delete
    clear_limits(env[0])
    assert not say(env, "取消电影订阅", str(row.id)).ok
    assert not any(x[0] in {"POST", "DELETE"} for x in env[2].calls)


def test_uncertain_creation_is_not_automatically_retried(env):
    env[2].create_timeout = True
    say(env, "搜电影", "示例")
    result = say(env, "订阅电影", "1")
    assert result.status == "uncertain"
    row = env[0].scalar(select(MoviePilotSubscription))
    assert row.status == "uncertain" and not row.can_delete
    clear_limits(env[0])
    assert say(env, "订阅电影", "1").status == "uncertain"
    assert len([x for x in env[2].calls if x[0] == "POST"]) == 1


def test_cancel_checks_remote_identity_and_handles_timeout(env):
    say(env, "搜电影", "示例")
    say(env, "订阅电影", "1")
    row = env[0].scalar(select(MoviePilotSubscription))
    env[2].subs[0]["date"] = "changed"
    clear_limits(env[0])
    assert not say(env, "取消电影订阅", str(row.id)).ok
    assert not any(x[0] == "DELETE" for x in env[2].calls)
    env[2].subs[0]["date"] = "2026-01-01 12:00:00"
    env[2].delete_timeout = True
    clear_limits(env[0])
    assert say(env, "取消电影订阅", str(row.id)).status == "uncertain"
    assert row.status == "cancelling"


def test_native_race_or_unknown_create_receipt_does_not_grant_delete(env):
    env[2].create_message = "订阅已存在"
    say(env, "搜电影", "示例")
    assert say(env, "订阅电影", "1").ok
    row = env[0].scalar(select(MoviePilotSubscription))
    assert row.remote_id == 19 and not row.can_delete
    clear_limits(env[0])
    assert not say(env, "取消电影订阅", str(row.id)).ok


def test_muting_notifications_preserves_native_cancellation_permission(env):
    say(env, "搜电影", "示例")
    say(env, "订阅电影", "1")
    row = env[0].scalar(select(MoviePilotSubscription))
    clear_limits(env[0])
    assert say(env, "取消电影关注", str(row.id)).ok
    assert row.status == "active" and row.can_delete and not row.notify_enabled
    assert poll(env).status == "skipped"
    clear_limits(env[0])
    assert say(env, "取消电影订阅", str(row.id)).ok


def test_notification_baseline_scope_dedup_and_transfer_retry(env):
    db, ctx, fake, _ = env
    register(env, baseline={"download": 10, "transfer": 20})
    register(env, context=replace(ctx, sender_id="bob"), baseline={"download": 10, "transfer": 20})
    fake.histories["download"] = [{**MOVIE, "id": 10}, {**MOVIE, "id": 11, "torrent_name": "Example.1080p"},
                                   {**MOVIE, "id": 12, "media_id": "999"}]
    fake.histories["transfer"] = [{**MOVIE, "id": 21, "status": False, "errmsg": "secret-path"}]
    assert poll(env).ok
    jobs = db.scalars(select(TaskJob)).all()
    assert len(jobs) == 2
    assert "secret-path" not in str([x.payload_json for x in jobs])
    assert poll(env).ok and len(db.scalars(select(TaskJob)).all()) == 2
    fake.histories["transfer"][0]["status"] = True
    assert poll(env).ok and len(db.scalars(select(TaskJob)).all()) == 3
    assert all(json.loads(x.payload_json)["conversation_id"] == 1 for x in jobs)


def test_disabled_group_and_group_override_skip_without_upstream_calls(env):
    db, ctx, fake, _ = env
    row = register(env)
    db.add(GroupPluginScope(tenant_id=1, bot_plugin_id=1, channel_id=1, chatroom_id=1,
        enabled=True, config_override_json='{"notifications_enabled":true}'))
    db.flush()
    assert poll(env).status == "skipped" and not fake.calls
    assert poll(env, context=replace(ctx, sender_id=None)).ok
    fake.calls.clear()
    db.get(Chatroom, 1).management_status = "archived"
    db.flush()
    assert poll(env).status == "skipped" and not fake.calls
    assert not db.scalars(select(TaskJob)).all()


def test_tenant_isolation_and_forged_context(env):
    register(env)
    env[0].commit()
    with tenant_scope(2), tenant_session(env[3]) as db:
        assert not db.scalars(select(MoviePilotSubscription)).all()
        result = plugin.execute_message_action(db, PluginExecutionRequest(action="execute", config=CONFIG,
            context=replace(env[1], tenant_id=2), payload={"command_name": "我的电影订阅", "tenant_id": 1}))
        assert not result.ok
    assert not say(env, "搜电影", "示例", context=replace(env[1], channel_id=99)).ok


@pytest.mark.parametrize("code,payload", [(401, {}), (302, {}), (200, []), (200, {"success": "true"}),
    (200, {"success": False, "message": "password=secret"})])
def test_client_fails_closed_and_never_echoes_remote_errors(code, payload):
    fake = FakeHTTP()
    fake.override = PluginHttpResponse(code, {}, json.dumps(payload).encode(), "https://mp.example.test")
    api = client.MoviePilotClient(CONFIG, fake)
    with pytest.raises(client.MoviePilotError) as caught:
        api.search("示例")
    assert "secret" not in str(caught.value)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://name:password@mp.example.test", "https://mp.example.test/api/v1",
    "https://mp.example.test?token=abc", "https://mp.example.test\\@other.test", "http://mp.example.test:invalid"])
def test_connection_rejects_ambiguous_or_credential_urls(url):
    with pytest.raises(client.MoviePilotError):
        client.MoviePilotClient({**CONFIG, "server_url": url})


def test_backlog_limit_preserves_watermark_and_no_queue(env):
    row = register(env)
    env[2].histories["download"] = [{**MOVIE, "id": n} for n in range(1, 502)]
    before = row.notification_json
    assert not poll(env).ok
    assert row.notification_json == before
    assert not env[0].scalars(select(TaskJob)).all()


def test_mutation_rate_limit_prevents_repeated_external_write(env):
    say(env, "搜电影", "示例")
    assert say(env, "订阅电影", "1").ok
    assert not say(env, "订阅电影", "1").ok
    assert len([x for x in env[2].calls if x[0] == "POST"]) == 1


def test_expired_selection_and_new_connection_do_not_reuse_registrations(env):
    from datetime import timedelta
    from app.core.time import now_shanghai

    say(env, "搜电影", "示例")
    record = env[0].scalar(select(PluginStateRecord).where(PluginStateRecord.namespace == SELECTION_NAMESPACE))
    record.expires_at = now_shanghai() - timedelta(seconds=1)
    env[0].flush()
    assert not say(env, "订阅电影", "1").ok
    row = register(env)
    env[0].flush()
    assert poll(env, config={**CONFIG, "api_key": "rotated"}).status == "skipped"


def test_notification_resume_uses_new_baseline_and_retains_ownership(env):
    say(env, "搜电影", "示例")
    say(env, "订阅电影", "1")
    row = env[0].scalar(select(MoviePilotSubscription))
    clear_limits(env[0])
    say(env, "取消电影关注", str(row.id))
    env[2].histories["download"] = [{**MOVIE, "id": 21}]
    clear_limits(env[0])
    assert say(env, "关注电影", "1").ok
    assert row.notify_enabled and row.can_delete
    assert poll(env).ok
    assert not env[0].scalars(select(TaskJob)).all()


def test_notification_queue_failure_rolls_back_watermark(env, monkeypatch):
    row = register(env)
    env[0].commit()
    env[2].histories["download"] = [{**MOVIE, "id": 1}]
    before = row.notification_json
    monkeypatch.setattr(notifications, "enqueue_outbound_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    with pytest.raises(RuntimeError):
        poll(env)
    env[0].rollback()
    assert env[0].get(MoviePilotSubscription, row.id).notification_json == before


def test_schema_migration_matches_model_and_supports_downgrade(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    spec = importlib.util.spec_from_file_location("mp_migration", "migrations/versions/20261118_0043_moviepilot.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert {x['name'] for x in inspect(conn).get_columns('moviepilot_subscriptions')} == set(MoviePilotSubscription.__table__.columns.keys())
            migration.downgrade()
            assert 'moviepilot_subscriptions' not in inspect(conn).get_table_names()
    engine.dispose()
    # 生成 MySQL/MariaDB 方言 DDL，避免把 SQLite 专属语法带到生产迁移。
    import io
    output = io.StringIO()
    with Operations.context(MigrationContext.configure(dialect_name="mysql", opts={"as_sql": True, "output_buffer": output})):
        migration.upgrade()
    assert "CREATE TABLE moviepilot_subscriptions" in output.getvalue()


def prepare_direct(env):
    assert say(env, "搜电影", "示例").ok
    assert say(env, "找资源", "1").ok
    clear_limits(env[0])
    result = say(env, "下载资源", "1")
    assert result.ok
    return env[0].scalar(select(MoviePilotDownload))


def test_direct_download_requires_confirmation_and_never_creates_subscription(env):
    row = prepare_direct(env)
    assert row.status == "pending" and not any(c[0] == "POST" for c in env[2].calls)
    assert "折扣" in text(say(env, "MP帮助"))
    clear_limits(env[0])
    result = say(env, "确认下载", row.token)
    assert result.ok and "未创建订阅" in text(result)
    assert row.status == "submitted" and row.download_id == "hash123"
    posts = [c for c in env[2].calls if c[0] == "POST"]
    assert [c[1] for c in posts] == ["/download/add"]
    body = posts[0][3]["json_body"]
    assert body["media_id"] == MOVIE["media_id"] and body["allow_unrecognized"] is False
    assert "downloader" not in body and "save_path" not in body and "filter" not in body
    tracking = env[0].get(MoviePilotSubscription, row.registration_id)
    assert tracking.remote_id is None and not tracking.can_delete
    clear_limits(env[0])
    assert "不会重复" in text(say(env, "确认下载", row.token))
    clear_limits(env[0])
    assert "不会重复" in text(say(env, "下载资源", "1"))
    assert len([c for c in env[2].calls if c[0] == "POST"]) == 1


def test_download_confirmation_isolated_by_sender_group_connection_and_tenant(env):
    row = prepare_direct(env)
    for context in (replace(env[1], sender_id="bob"), replace(env[1], conversation_id=2)):
        clear_limits(env[0])
        assert not say(env, "确认下载", row.token, context=context).ok
    clear_limits(env[0])
    assert not say(env, "确认下载", row.token, config={**CONFIG, "api_key": "rotated"}).ok
    env[0].commit()
    with tenant_scope(2), tenant_session(env[3]) as db:
        assert not db.scalars(select(MoviePilotDownload)).all()
    assert not any(c[0] == "POST" for c in env[2].calls)


def test_expired_confirmation_and_missing_resource_do_not_download(env):
    from datetime import timedelta
    from app.core.time import now_shanghai

    row = prepare_direct(env)
    row.expires_at = now_shanghai() - timedelta(seconds=1)
    clear_limits(env[0])
    assert not say(env, "确认下载", row.token).ok
    clear_limits(env[0])
    assert say(env, "下载资源", "1").ok
    env[2].resources_rows = []
    clear_limits(env[0])
    assert not say(env, "确认下载", row.token).ok
    assert not any(c[0] == "POST" for c in env[2].calls)


def test_changed_promotion_rotates_confirmation_and_uses_fresh_torrent(env):
    row = prepare_direct(env)
    old = row.token
    torrent = env[2].resources_rows[0]["torrent_info"]
    torrent["downloadvolumefactor"] = 0
    torrent["enclosure"] = "https://private.test/?passkey=new-signature"
    clear_limits(env[0])
    result = say(env, "确认下载", old)
    assert result.ok and "发生变化" in text(result) and "Free" in text(result)
    assert row.token != old and row.status == "pending"
    assert not any(c[0] == "POST" for c in env[2].calls)
    clear_limits(env[0])
    assert not say(env, "确认下载", old).ok
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).ok
    assert [c for c in env[2].calls if c[0] == "POST"][0][3]["json_body"]["torrent_in"]["enclosure"].endswith("new-signature")


@pytest.mark.parametrize("bad_result", [None, {}, {"download_id": True}, {"download_id": "https://secret.test/token"}])
def test_malformed_download_receipt_stays_uncertain_without_retry(env, bad_result):
    row = prepare_direct(env)
    env[2].download_result = bad_result
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).status == "uncertain"
    assert row.status == "uncertain"
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).status == "uncertain"
    assert len([c for c in env[2].calls if c[0] == "POST"]) == 1


def test_timed_out_download_survives_new_session_and_does_not_repeat(env):
    row = prepare_direct(env)
    env[2].download_timeout = True
    token, row_id = row.token, row.id
    clear_limits(env[0])
    result = say(env, "确认下载", token)
    assert result.status == "uncertain" and "sensitive" not in text(result)
    env[0].commit()
    with tenant_session(env[3]) as db:
        clear_limits(db)
        new_env = (db, env[1], env[2], env[3])
        assert db.get(MoviePilotDownload, row_id).status == "uncertain"
        assert say(new_env, "确认下载", token).status == "uncertain"
    assert len([c for c in env[2].calls if c[0] == "POST"]) == 1


def test_resources_and_ledger_never_store_torrent_credentials(env):
    row = prepare_direct(env)
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).ok
    stored = row.media_json + row.resource_json
    stored += ''.join(env[0].scalars(select(PluginStateRecord.payload_json)).all())
    stored += ''.join(env[0].scalars(select(MoviePilotSubscription.notification_json)).all())
    assert not any(secret in stored for secret in ("private-cookie", "passkey", "enclosure", "private.test"))


def test_direct_notifications_use_exact_download_snapshot_not_movie_guess(env):
    row = prepare_direct(env)
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).ok
    env[2].histories['download'] = [{**MOVIE, 'id': 1, 'download_hash': 'hash123'},
                                   {**MOVIE, 'id': 2, 'download_hash': 'other-hash'}]
    assert poll(env).ok
    jobs = env[0].scalars(select(TaskJob).order_by(TaskJob.id)).all()
    assert len(jobs) == 2
    assert "提交时优惠：50%" in jobs[0].payload_json
    assert "优惠未知" in jobs[1].payload_json


def test_direct_download_can_later_be_natively_subscribed_and_snapshot_is_labeled(env):
    row = prepare_direct(env)
    clear_limits(env[0])
    assert say(env, "确认下载", row.token).ok
    clear_limits(env[0])
    before = say(env, "我的电影订阅")
    assert "未创建原生订阅" in text(before) and "最近查看资源：50%" in text(before)
    clear_limits(env[0])
    assert say(env, "取消电影关注", str(row.registration_id)).ok
    clear_limits(env[0])
    assert say(env, "关注电影", "1").ok
    clear_limits(env[0])
    assert say(env, "订阅电影", "1").ok
    tracking = env[0].get(MoviePilotSubscription, row.registration_id)
    assert tracking.can_delete and tracking.remote_id == 19
    post = next(c for c in env[2].calls if c[0] == "POST" and c[1] == "/subscribe/")
    assert not any(k in post[3]["json_body"] for k in ('filter', 'filter_groups', 'sites', 'downloadvolumefactor'))


def test_other_member_cannot_claim_pending_resource_or_read_its_confirmation(env):
    row = prepare_direct(env)
    old_token = row.token
    other = replace(env[1], sender_id='bob')
    assert say(env, '搜电影', '示例', context=other).ok
    assert say(env, '找资源', '1', context=other).ok
    clear_limits(env[0])
    result = say(env, '下载资源', '1', context=other)
    assert not result.ok and old_token not in text(result)
    assert row.sender_id == 'alice' and row.token == old_token


def test_direct_download_preserves_existing_native_ownership_and_mute(env):
    say(env, '搜电影', '示例')
    assert say(env, '订阅电影', '1').ok
    tracking = env[0].scalar(select(MoviePilotSubscription))
    clear_limits(env[0])
    assert say(env, '取消电影关注', str(tracking.id)).ok
    clear_limits(env[0])
    row = prepare_direct(env)
    clear_limits(env[0])
    assert say(env, '确认下载', row.token).ok
    assert tracking.can_delete and tracking.remote_id == 19 and not tracking.notify_enabled
    assert poll(env).status == 'skipped'


def test_wrong_movie_or_changed_resource_identity_cannot_be_submitted(env):
    row = prepare_direct(env)
    env[2].resources_rows[0]['media_info']['media_id'] = '999'
    clear_limits(env[0])
    assert not say(env, '确认下载', row.token).ok
    assert not any(c[0] == 'POST' for c in env[2].calls)


def test_shared_group_notification_can_use_exact_direct_download_promotion(env):
    register(env, context=replace(env[1], sender_id='bob'))
    row = prepare_direct(env)
    clear_limits(env[0])
    assert say(env, '确认下载', row.token).ok
    env[2].histories['download'] = [{**MOVIE, 'id': 1, 'download_hash': 'hash123'}]
    assert poll(env).ok
    jobs = env[0].scalars(select(TaskJob)).all()
    assert len(jobs) == 1 and '提交时优惠：50%' in jobs[0].payload_json


@pytest.mark.parametrize('factor,label', [(0, 'Free'), (0.5, '50%'), (0.3, '30%'), (1, '普通'),
    (None, '未知'), (True, '未知'), ('NaN', '未知'), (-1, '未知')])
def test_promotion_factor_semantics(factor, label):
    assert label in client.promotion_label(client.promotion({'downloadvolumefactor': factor}))
    assert '上传 2 倍' in client.promotion_label(client.promotion({'downloadvolumefactor': 0, 'uploadvolumefactor': 2}))


def test_expired_promotion_is_not_advertised_as_current_free():
    value = client.promotion({'downloadvolumefactor': 0, 'freedate': '2000-01-01 00:00:00'})
    assert value['download'] is None and '已到期' in client.promotion_label(value)


def test_direct_download_migration_sqlite_and_mysql(tmp_path):
    import importlib.util
    import io
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    spec = importlib.util.spec_from_file_location('mp_download_migration', 'migrations/versions/20261118_0044_moviepilot_download.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'direct.db'}")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()
        assert {c['name'] for c in inspect(conn).get_columns('moviepilot_downloads')} == set(MoviePilotDownload.__table__.columns.keys())
        migration.downgrade()
        assert 'moviepilot_downloads' not in inspect(conn).get_table_names()
    engine.dispose()
    output = io.StringIO()
    with Operations.context(MigrationContext.configure(dialect_name='mysql', opts={'as_sql': True, 'output_buffer': output})):
        migration.upgrade()
    assert 'uq_mp_download_resource' in output.getvalue()
