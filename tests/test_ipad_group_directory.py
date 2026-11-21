import json

import pytest
from sqlalchemy import select

from app.models.entities import Channel, Chatroom, ChatroomMember, IpadFriend, TaskJob
from app.services.wechat_ipad.client import WechatIpadApiClient
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture
def directory(provider_page_context):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    with ids["factory"]() as db:
        channel = db.get(Channel, cid)
        channel.runtime_info_json = '{"active_wxid":"owner","last_chatroom_sync_failed_targets":["stale"]}'
        old = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=cid, external_id="old@chatroom",
                       name="原群名", remark="手动备注", is_active=False, management_status="disabled",
                       member_count=3, extra_json='{"config":"preserved"}')
        db.add(old)
        db.commit()
    base = f"/channels/{cid}/wechat-ipad"
    token = _login(client, "operator")

    def sync():
        return client.post(base + "/sync-chatrooms", data={"csrf_token": token},
                           headers={"accept": "application/json"}).json()

    return client, ids, base, sync


def ack(data):
    return {"Code": 0, "Success": True, "Data": {"BaseResponse": {"ret": 0}, **data}}


def test_sync_discovers_every_group_without_touching_local_configuration(directory, monkeypatch):
    client, ids, base, sync = directory
    calls = []
    rows = [f"g{n}@chatroom" for n in range(82)]
    empty = False

    def protocol(self, channel, endpoint, *, body):
        assert channel.id == ids["channel_id"] and body["Wxid"] == "owner"
        calls.append(endpoint)
        if endpoint.endswith("/GetContractList"):
            if empty:
                return ack({"CountinueFlag": 0})
            if body["CurrentWxcontactSeq"] == 0:
                return ack({"ContactUsernameList": ["friend", "gh_test", "old@chatroom", *rows[:60]], "CountinueFlag": 1,
                            "CurrentWxcontactSeq": 21, "CurrentChatRoomContactSeq": 9})
            assert (body["CurrentWxcontactSeq"], body["CurrentChatRoomContactSeq"]) == (21, 9)
            return ack({"ContactUsernameList": rows[59:], "CountinueFlag": 0})
        assert endpoint.endswith("/GetContractDetail")
        targets = body["Towxids"].split(",")
        assert len(targets) <= 80 and all(t.endswith("@chatroom") for t in targets)
        return ack({"ContactList": [{"UserName": {"string": t}, "NickName": {"string": "群名" + t},
                                     "Remark": {"string": "微信备注"}} for t in reversed(targets)],
                    "Ret": [-1 if t == "g80@chatroom" else 0 for t in reversed(targets)]})

    monkeypatch.setattr(WechatIpadApiClient, "request_json", protocol)
    assert client.post(base + "/sync-chatrooms", data={"csrf_token": "invalid"}).status_code == 403 and not calls
    first = sync()
    assert first["ok"] and "83 个群，新增 82 个" in first["summary"] and "1 个本次未取得群名" in first["summary"]
    assert len(calls) == 4
    assert "新增 0 个" in sync()["summary"]
    page = client.get(base, params={"tab": "groups", "keyword": "g81@"})
    assert page.status_code == 200 and "群名g81@chatroom" in page.text and len(calls) == 8
    empty = True
    assert "0 个群" in sync()["summary"]
    with ids["factory"]() as db:
        rooms = {r.external_id: r for r in db.scalars(select(Chatroom))}
        assert len(rooms) == 83 and rooms["g81@chatroom"].is_active and not rooms["g80@chatroom"].name
        old = rooms["old@chatroom"]
        assert (old.remark, old.is_active, old.management_status, old.member_count) == ("手动备注", False, "disabled", 3)
        assert json.loads(old.extra_json)["config"] == "preserved" and old.last_synced_at is None
        assert not any(db.scalar(select(model.id)) for model in (IpadFriend, ChatroomMember, TaskJob))


@pytest.mark.parametrize("failure", ["cursor", "inner_error", "no_end_marker", "changed_owner"])
def test_incomplete_reads_do_not_publish_partial_directories(directory, monkeypatch, failure):
    _, ids, _, sync = directory

    def protocol(self, channel, endpoint, *, body):
        if body.get("CurrentWxcontactSeq") == 0:
            return ack({"ContactUsernameList": ["new@chatroom"], "CountinueFlag": 1,
                        "CurrentWxcontactSeq": 1, "CurrentChatRoomContactSeq": 2})
        if failure == "cursor":
            return ack({"CountinueFlag": 1, "CurrentWxcontactSeq": 1, "CurrentChatRoomContactSeq": 2})
        if failure == "inner_error":
            return ack({"CountinueFlag": 0, "BaseResponse": {"ret": -8}})
        if failure == "no_end_marker":
            return ack({})
        if endpoint.endswith("/GetContractDetail"):
            with ids["factory"]() as db:
                db.get(Channel, channel.id).runtime_info_json = '{"active_wxid":"replacement"}'
                db.commit()
            return ack({"ContactList": []})
        return ack({"CountinueFlag": 0})

    monkeypatch.setattr(WechatIpadApiClient, "request_json", protocol)
    assert not sync()["ok"]
    with ids["factory"]() as db:
        assert list(db.scalars(select(Chatroom.external_id))) == ["old@chatroom"]
        if failure == "changed_owner":
            assert json.loads(db.get(Channel, ids["channel_id"]).runtime_info_json) == {"active_wxid": "replacement"}
