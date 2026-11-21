"""好友备注的单次写入、回读确认、租户与账号边界。"""

from types import SimpleNamespace

import pytest

from app.models.entities import Channel, IpadFriend
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.operations.friend_remark import change
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def _protocol(monkeypatch, case, initial="旧备注"):
    writes = []

    def request(self, channel, endpoint, *, body):
        assert body["Wxid"] == "owner"
        if endpoint.endswith("/SetRemarks"):
            assert body["ToWxid"] == "peer"
            writes.append(body["Remarks"])
            if case == "timeout":
                raise TimeoutError
            data = {} if case == "no_ack" else {"ret": -2 if case == "reject" else 0}
        else:
            assert endpoint.endswith("/GetContractDetail") and body["Towxids"] == "peer"
            value = writes[-1] if writes else initial
            row = {"UserName": {"string": "other" if case == "wrong" and writes else "peer"}}
            if value is not None and not (case == "missing_clear" and writes):
                row["Remark"] = {"string": value}
            data = {"BaseResponse": {"ret": 0}, "ContactList": [row]}
        return {"http_status": 200, "Success": True, "Code": 0, "Data": data}

    monkeypatch.setattr(WechatIpadApiClient, "request_json", request)
    return writes


@pytest.mark.parametrize("case", ["first", "changed", "clear", "stale", "no_ack", "reject", "timeout", "wrong", "missing_clear"])
def test_write_and_confirm(case, monkeypatch):
    old = "" if case == "first" else "旧备注"
    writes = _protocol(monkeypatch, case, None if case == "first" else old)
    friend = SimpleNamespace(remark=old or None, display_name=old or "昵称", name="昵称", alias=None,
                             profile_json=None, owner_wxid="owner", contact_external_id="peer")
    clear = case in {"clear", "missing_clear"}
    ok, summary = change(SimpleNamespace(), friend, expected="其他" if case == "stale" else old,
                         remark="新备注", clear=clear)
    assert writes == ([] if case == "stale" else ["" if clear else "新备注"])
    assert ok == (case in {"first", "changed", "clear"})
    assert friend.remark == ((None if clear else "新备注") if ok else old)
    assert friend.display_name == (friend.remark or "昵称")
    if case in {"no_ack", "timeout", "wrong", "missing_clear"}:
        assert "待核实" in summary


def test_edit_from_directories_requires_live_binding_and_operator(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    cid = ids["channel_id"]
    with ids["factory"]() as db:
        db.get(Channel, cid).adapter_config_json = '{"wxid":"owner"}'
        friend = IpadFriend(tenant_id=ids["alpha"], channel_id=cid, owner_wxid="owner", contact_external_id="peer",
                            name="昵称", remark="旧备注", display_name="旧备注")
        foreign = IpadFriend(tenant_id=ids["beta"], channel_id=cid, owner_wxid="owner", contact_external_id="other",
                             display_name="其他")
        db.add_all([friend, foreign])
        db.commit()
        fid, foreign_id = friend.id, foreign.id
    writes = _protocol(monkeypatch, "changed")
    prefix = f"/channels/{cid}/wechat-ipad"
    path = f"{prefix}/friends/{fid}/remark"
    csrf = _login(client, "operator")
    form = {"expected": "旧备注", "remark": "新备注", "csrf_token": csrf}
    for page in ("/contacts?q=peer", prefix + "?keyword=peer"):
        assert f'action="{path}"' in client.get(page).text
    assert client.post(path, data={**form, "csrf_token": "bad"}).status_code == 403
    assert client.post(f"{prefix}/friends/{foreign_id}/remark", data=form).status_code == 404
    assert not writes
    result = client.post(path, data=form, headers={"accept": "application/json"})
    assert result.json()["ok"] and writes == ["新备注"]
    for page in ("/contacts?q=peer", prefix + "?keyword=peer"):
        assert 'name="expected" value="新备注"' in client.get(page).text
    assert not client.post(path, data=form, headers={"accept": "application/json"}).json()["ok"]
    with ids["factory"]() as db:
        db.get(Channel, cid).runtime_info_json = '{"active_wxid":"replacement"}'
        db.commit()
    assert client.post(path, data=form).status_code == 404
    readonly = _login(client, "readonly")
    assert client.post(path, data={**form, "csrf_token": readonly}).status_code == 403
    assert writes == ["新备注"]
