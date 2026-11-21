"""完整通讯录、本地目录与账号边界；所有协议响应均为构造数据。"""

import importlib
import json

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text

from app.models.entities import Channel, IpadFriend, TaskJob
from app.services.wechat_ipad.client import WechatIpadApiClient
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def test_paged_sync_stores_all_friends_and_get_is_read_only(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    base = f"/channels/{cid}/wechat-ipad"
    with ids["factory"]() as db:
        db.get(Channel, cid).adapter_config_json = '{"wxid":"owner"}'
        other = Channel(tenant_id=ids["beta"], bot_id=ids["bot"], name="other", channel_type="wechat_ipad")
        wrong = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="wrong", channel_type="wechat_webridge")
        db.add_all([other, wrong])
        db.commit()
        other_id, wrong_id = other.id, wrong.id
    calls = []
    incomplete = broken = False

    def protocol(self, target, endpoint, *, body):
        assert target.id == cid and body["Wxid"] == "owner"
        calls.append(endpoint)
        if "CurrentWxcontactSeq" in body:
            if body["CurrentWxcontactSeq"] == 0:
                data = {"ContactUsernameList": [f"peer{i}" for i in range(510)] + ["owner", "group@chatroom", "gh_demo"],
                        "CountinueFlag": 1, "CurrentWxcontactSeq": 7, "CurrentChatRoomContactSeq": 9}
            else:
                assert body["CurrentChatRoomContactSeq"] == 9
                data = {"ContactUsernameList": ["peer0", "tail"], "CountinueFlag": int(broken),
                        "CurrentWxcontactSeq": 7, "CurrentChatRoomContactSeq": 9}
        else:
            names = body["Towxids"].split(",")
            assert len(names) <= 80
            data = {"ContactList": [] if incomplete else [{"UserName": {"string": n},
                    "NickName": {"string": "昵称" + n}, "Remark": {"string": "备注" + n},
                    "Alias": "alias_" + n, "SmallHeadImgUrl": "/test.png"} for n in names]}
        return {"Success": True, "Data": data}

    monkeypatch.setattr(WechatIpadApiClient, "request_json", protocol)
    token = _login(client, "operator")
    assert client.post(base + "/sync-contacts", data={"csrf_token": "wrong"}).status_code == 403
    assert client.get(f"/channels/{cid}").status_code == 200
    assert base in client.get(f"/channels/{cid}").text
    assert client.get(f"/channels/{other_id}/wechat-ipad").status_code == 404
    assert client.get(f"/channels/{wrong_id}/wechat-ipad").status_code == 404
    assert not calls

    def sync():
        return client.post(base + "/sync-contacts", data={"csrf_token": token}, headers={"accept": "application/json"}).json()

    assert sync()["ok"]
    incomplete = True
    assert "511 位本次未取到详细资料" in sync()["summary"]
    broken = True
    assert not sync()["ok"]
    with ids["factory"]() as db:
        assert db.scalar(select(func.count(IpadFriend.id))) == 511
        row = db.scalar(select(IpadFriend).where(IpadFriend.contact_external_id == "tail"))
        assert (row.owner_wxid, row.name, row.remark, row.avatar_url) == ("owner", "昵称tail", "备注tail", "/test.png")
        assert "tail" not in db.get(Channel, cid).runtime_info_json
    calls.clear()
    token = _login(client, "readonly")
    for tab in ("contacts", "groups", "members", "messages"):
        page = client.get(base, params={"tab": tab, "keyword": "alias_tail"})
        assert page.status_code == 200 and 'action="' + base + '/sync-' not in page.text
        if tab == "contacts":
            assert "备注tail" in page.text and "好友 511" in page.text
    for action in ("sync-contacts", "sync-chatrooms"):
        assert client.post(base + "/" + action, data={"csrf_token": token}).status_code == 403
    with ids["factory"]() as db:
        assert db.scalar(select(func.count(TaskJob.id))) == 0
        channel = db.get(Channel, cid)
        channel.runtime_info_json = '{"active_wxid":"replacement"}'
        db.commit()
    page = client.get(base)
    assert "好友 0" in page.text and "备注tail" not in page.text and not calls


def test_upgrade_imports_legacy_friends_once_under_snapshot_owner(monkeypatch):
    migration = importlib.import_module("migrations.versions.20261118_0045_ipad_friends")
    engine = create_engine("sqlite://")
    snapshot = {"wxid": "previous", "synced_at": "2026-10-01T12:00:00+08:00", "contacts": [
        {"wxid": "peer", "is_personal_contact": True, "nickname": "测试", "remark": "-"}] * 2 + [
        {"wxid": "room@chatroom", "is_personal_contact": False}]}
    with engine.begin() as conn:
        conn.execute(text("create table channels (id integer primary key, tenant_id integer, channel_type text, runtime_info_json text)"))
        conn.execute(text("insert into channels values (1,2,'wechat_ipad',:info)"),
                     {"info": json.dumps({"active_wxid": "new", "last_contact_sync_snapshot": snapshot})})
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.execute(select(IpadFriend.owner_wxid, IpadFriend.display_name, IpadFriend.tenant_id)).all() == [
            ("previous", "测试", 2)]
        assert json.loads(conn.scalar(text("select runtime_info_json from channels"))) == {"active_wxid": "new"}
