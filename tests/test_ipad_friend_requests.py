import base64
import importlib
from io import StringIO

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select

from app.core.settings import settings
from app.models.entities import Channel, IpadFriend, IpadFriendRequest as Row, Message, RawWebhookEvent
from app.services.channel_events import ingest_inbound_event
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.operations import friend_requests as inbox
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_select_layout import browser  # noqa: F401


def event(src="fmessage", dest="owner", scene=17):
    return {"MsgType": 37, "FromUserName": {"string": src}, "ToUserName": {"string": dest},
            "Content": {"string": f'<msg fromusername="peer" fromnickname="测试申请人" scene="{scene}" '
                                  'encryptusername="secret-identifier" ticket="secret-ticket"/>'}}


def ok(data):
    return {"Success": True, "Code": 0, "http_status": 200, "Data": data}


@pytest.fixture
def setup_inbox(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    monkeypatch.setattr(settings, "plugin_secret_key", base64.urlsafe_b64encode(b"k" * 32).decode())
    state = {"writes": 0, "names": [], "base": {"ret": 0}}

    def protocol(self, channel, path, *, body):
        if path.endswith("/PassVerify"):
            state["writes"] += 1
            assert body == {"Wxid": "owner", "Scene": 17, "V1": "secret-identifier", "V2": "secret-ticket"}
            return ok({"BaseResponse": {"ret": 0}, "Username": "peer"})
        if path.endswith("/Sync"):
            return ok({"AddMsgs": [event(src="chat"), event(dest="other"), event(scene=256), event(), event()]})
        if "CurrentWxcontactSeq" in body:
            return ok({"ContactUsernameList": state["names"], "BaseResponse": state["base"]})
        assert "Towxids" in body
        return ok({"ContactList": [], "BaseResponse": {"ret": 0}})

    monkeypatch.setattr(WechatIpadApiClient, "request_json", protocol)
    with ids["factory"]() as db:
        channel = db.get(Channel, cid)
        channel.runtime_info_json = '{"active_wxid":"owner"}'
        assert ingest_inbound_event(db, channel, {"Data": event()})["ignored"]
        assert not db.scalar(select(Row.id))
        db.commit()
    csrf = _login(client, "operator")
    prefix = f"/channels/{cid}/wechat-ipad"
    assert client.post(prefix + "/pull-messages", data={"csrf_token": csrf}, headers={"accept": "application/json"}).json()["ok"]
    with ids["factory"]() as db:
        row = db.scalars(select(Row)).one()
        assert row.status == "pending" and "secret-" not in row.credentials
        assert not db.scalar(select(Message.id)) and not db.scalar(select(RawWebhookEvent.id))
        assert "secret-" not in db.get(Channel, cid).runtime_info_json
        path = f"{prefix}/requests/{row.id}/accept"
    return client, ids, state, path, prefix, csrf


def test_accept_once_and_confirm_by_fresh_contact_list(setup_inbox):
    client, ids, state, path, prefix, csrf = setup_inbox
    headers = {"accept": "application/json"}
    form = {"csrf_token": csrf}
    for url in ("/contacts", prefix):
        assert "好友申请" in client.get(url).text
    html = client.get("/contacts/requests").text
    assert path in html and "secret-" not in html
    old = client.post(prefix + "/actions/accept_friend", json={"v1": "secret-identifier", "v2": "secret-ticket"})
    assert old.status_code == 400 and "secret-" not in old.text
    with ids["factory"]() as db:
        assert "secret-" not in db.get(Channel, ids["channel_id"]).runtime_info_json
    assert client.post(path, data={"csrf_token": "bad"}).status_code == 403
    assert not state["writes"]
    assert client.post(path, data=form, headers=headers).json()["ok"]
    assert not client.post(path, data=form, headers=headers).json()["ok"]
    assert client.post(prefix + "/pull-messages", data=form, headers=headers).json()["ok"]
    with ids["factory"]() as db:
        row = db.scalars(select(Row)).one()
        assert row.status == "submitted" and row.credentials is None and not db.scalar(select(IpadFriend.id))
        db.add(IpadFriend(tenant_id=row.tenant_id, channel_id=row.channel_id, owner_wxid="owner",
                          contact_external_id="peer", display_name="peer"))
        db.commit()
    for names, base, status in [(["other"], {"ret": 0}, "submitted"), (["peer"], {}, "submitted"),
                                 (["peer"], {"ret": 0}, "confirmed")]:
        state.update(names=names, base=base)
        assert client.post(prefix + "/sync-contacts", data=form, headers=headers).json()["ok"]
        with ids["factory"]() as db:
            assert db.scalar(select(Row.status)) == status
    assert state["writes"] == 1 and "已成为好友" in client.get("/contacts/requests").text


def test_boundaries_and_unknown(setup_inbox, monkeypatch):
    client, ids, state, path, prefix, csrf = setup_inbox
    with ids["factory"]() as db:
        row = db.scalars(select(Row)).one()
        channel = db.get(Channel, ids["channel_id"])
        assert inbox.accept(db, ids["beta"], channel.id, row.id) is None
        channel.is_enabled = False
        db.commit()
    assert client.post(path, data={"csrf_token": csrf}).status_code == 404
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.is_enabled = True
        channel.runtime_info_json = '{"active_wxid":"other"}'
        db.commit()
    assert "测试申请人" not in client.get("/contacts/requests").text
    assert client.post(path, data={"csrf_token": csrf}).status_code == 404
    with ids["factory"]() as db:
        db.get(Channel, ids["channel_id"]).runtime_info_json = '{"active_wxid":"owner"}'
        db.commit()
    readonly = _login(client, "readonly")
    assert path not in client.get("/contacts/requests").text
    assert client.post(path, data={"csrf_token": readonly}).status_code == 403

    def timeout(*args, **kwargs):
        state["writes"] += 1
        raise TimeoutError("secret-ticket")

    monkeypatch.setattr(WechatIpadApiClient, "accept_friend", timeout)
    csrf = _login(client, "operator")
    for _ in range(2):
        reply = client.post(path, data={"csrf_token": csrf}, headers={"accept": "application/json"}).json()
        assert not reply["ok"] and reply["summary"] == "结果待核实"
    assert state["writes"] == 1


@pytest.mark.parametrize(("data", "status"), [({}, "unknown"), ({"BaseResponse": {}}, "unknown"),
    ({"BaseResponse": {"ret": 0}, "Username": "other"}, "unknown"),
    ({"BaseResponse": {"ret": -1}}, "rejected")])
def test_incomplete_or_rejected_ack(data, status, monkeypatch):
    monkeypatch.setattr(WechatIpadApiClient, "accept_friend", lambda *a, **kw: ok(data))
    assert inbox._send(None, "owner", "peer", {}) == status


def test_mobile_shared_inbox(setup_inbox, browser):
    client, _, state, _, prefix, _ = setup_inbox
    with _application_server(client) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            for source in ("/contacts?q=peer", prefix + "?tab=contacts"):
                page.goto(base + source)
                page.get_by_role("link", name="好友申请", exact=True).click()
                page.get_by_role("button", name="接受申请").wait_for()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.get_by_role("link", name="返回好友列表").click()
                page.wait_for_url(base + source)
            page.get_by_role("link", name="好友申请", exact=True).click()
            page.get_by_role("button", name="接受申请").click()
            page.get_by_role("cell", name="已提交，待通讯录确认").wait_for()
            assert state["writes"] == 1
        finally:
            page.close()


def test_migration(monkeypatch):
    module = importlib.import_module("migrations.versions.20261118_0046_ipad_friend_requests")
    with create_engine("sqlite://").begin() as conn:
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(conn)))
        module.upgrade()
        assert conn.scalar(select(func.count()).select_from(Row)) == 0
        module.downgrade()
        assert not inspect(conn).has_table(Row.__tablename__)
    sql = StringIO()
    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(dialect_name="mariadb",
                        opts={"as_sql": True, "output_buffer": sql})))
    module.upgrade()
    assert "CREATE TABLE ipad_friend_requests" in sql.getvalue() and "uq_ipad_friend_request_ticket" in sql.getvalue()


def test_missing_key_and_untrusted_messages(provider_page_context, monkeypatch):
    _, ids = provider_page_context
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        monkeypatch.setattr(settings, "plugin_secret_key", "")
        for item in (event(src="chat"), event(dest="other"), event(scene=256), {**event(), "MsgType": 1},
                     {**event(), "Content": {"string": "<!DOCTYPE msg><msg/>"}}):
            inbox.receive(db, channel, "owner", item)
        assert not db.scalar(select(Row.id))
        inbox.receive(db, channel, "owner", event())
        db.commit()
        row = db.scalars(select(Row)).one()
        assert row.status == "unavailable" and row.credentials is None
        monkeypatch.setattr(settings, "plugin_secret_key", base64.urlsafe_b64encode(b"k" * 32).decode())
        inbox.receive(db, channel, "owner", event())
        db.commit()
        assert row.status == "pending" and row.credentials and "secret-" not in row.credentials
