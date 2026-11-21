"""微桥后台的分页、筛选与群详情来源链接。"""

import re
import pytest
from html import unescape
from urllib.parse import parse_qs, urlsplit

from app.models.entities import Channel, Chatroom, Conversation
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.mark.parametrize("protocol,label", [("wechat_webridge", "微桥"), ("wechat_ipad", "iPad")])
def test_group_paging_links_and_details_keep_source_page(provider_page_context, protocol, label):
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="分页账号", channel_type=protocol, is_enabled=True)
        db.add(channel)
        db.flush()
        db.add_all(Chatroom(
            tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
            external_id=f"paging-{number}@chatroom", name=f"分页群 {number:03d}",
        ) for number in range(101))
        db.add(Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
            channel_type=channel.channel_type, external_id="paging-80@chatroom", title="分页群 080", chat_type="group"))
        db.commit()
        channel_id = channel.id
    _login(client, "admin")
    path = f"/channels/{channel_id}/{protocol.replace('_', '-')}"
    response = client.get(path, params={"tab": "groups", "keyword": "分页群", "page": 5})
    assert response.status_code == 200
    pager = response.text.split(f'aria-label="{label}群组分页"', 1)[1].split("</nav>", 1)[0]
    assert "第 5 / 6 页" in pager and "page=4" in pager and "page=6" in pager
    assert 'name="keyword" value="分页群"' in pager
    assert 'name="tab" value="groups"' in pager
    assert 'name="page"' in pager and "跳转" in pager
    detail_href = re.search(r'href="([^"]+/chatrooms/[^"]+)">群详情</a>', response.text).group(1)
    source = parse_qs(urlsplit(unescape(detail_href)).query)["return_to"][0]
    assert urlsplit(source).path == path
    assert parse_qs(urlsplit(source).query) == {"tab": ["groups"], "keyword": ["分页群"], "page": ["5"]}
    chat_href = re.search(r'href="(/messages\?[^"]+)"', response.text).group(1)
    assert parse_qs(urlsplit(unescape(chat_href)).query)["return_to"] == [source]
