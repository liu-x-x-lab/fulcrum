from __future__ import annotations

import json
import html
import socket
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.services.plugin_directory import load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services import safe_fetch
from app.services.safe_fetch import PublicResource, UnsafeUrlError
from plugins.article_summary import plugin
from plugins.article_summary.parsers import extract_article, message_url
from tests.test_plugin_common_services import common_service_fixture


ARTICLE_URL = "https://mp.weixin.qq.com/s/demo"
BODY = "志愿者在周末记录了河边的鸟类，并解释了如何核对观察数据。" * 6


def page(markup, url=ARTICLE_URL, charset="utf-8"):
    return PublicResource(markup.encode(charset), url, charset)


def ai_setting(tenant_id):
    provider = SimpleNamespace(tenant_id=tenant_id, enabled=True, api_base="https://example.test/v1", api_key_encrypted="fixture-cipher")
    return SimpleNamespace(enabled=True, model="test", tenant_id=tenant_id, provider=provider)


def test_discovery_and_group_matching():
    instance = load_directory_plugin("article_summary", force=True).instance
    event = SimpleNamespace(chat_type="group", message_type="link", content="文章分享", normalized_event={
        "details": {"media": {"app_msg_url": ARTICLE_URL, "app_thumb_url": "https://example.test/cover"}}})
    assert instance.matches_event(event)
    event.chat_type = "direct"
    assert not instance.matches_event(event)
    assert instance.execute_action(PluginExecutionRequest("reload", {})).ok
    assert not instance.execute_action(PluginExecutionRequest("other", {})).ok


@pytest.mark.parametrize("content,expected", [
    ("先看 https://v.douyin.com/share；再看 " + ARTICLE_URL + "。", ARTICLE_URL),
    ("https://example.test/x.mp4", ""), ("https://name:password@example.test/page", ""),
    ("看（" + ARTICLE_URL + "#comment）", ARTICLE_URL),
])
def test_text_share(content, expected):
    assert message_url("text", content, {}) == expected


def test_card_selects_article_not_cover_or_quoted_url():
    xml = '<msg><appmsg><type>5</type><thumburl>https://example.test/cover</thumburl><mmreader><category><item>'
    xml += '<url><![CDATA[' + ARTICLE_URL + '?a=1&b=2]]></url></item></category></mmreader></appmsg></msg>'
    assert message_url("app", xml, {}) == ARTICLE_URL + "?a=1&b=2"
    assert message_url("app", "[链接/文件]", {"details": {"content_xml": html.escape(xml)}}) == ARTICLE_URL + "?a=1&b=2"
    assert message_url("file", xml, {}) == ARTICLE_URL + "?a=1&b=2"
    assert not message_url("file", xml.replace("<type>5", "<type>6"), {})
    assert not message_url("file", ARTICLE_URL, {})
    assert not message_url("app", xml.replace("<type>5", "<type>57"), {})
    assert not message_url("app", "", {"details": {"content_xml": "<!DOCTYPE msg>" + xml}})


def test_extract_body_only_and_respect_encoding():
    doc = f'<title>鸟类观察</title><nav>导航</nav><div id="js_content"><section>{BODY}</section><script>机密脚本</script></div><p>推荐</p>'
    assert extract_article(page(doc, charset="gbk")) == ("鸟类观察", BODY)
    assert extract_article(page(f'<article><h1>观察</h1><p>{BODY}</p></article>', 'https://example.test/a')) == ("观察", BODY)
    assert extract_article(page('<title>安全验证</title><p>' + BODY + '</p>')) is None
    assert extract_article(page('<title>登录</title><main>' + BODY + '<input type="password"></main>', 'https://example.test/login')) is None
    assert extract_article(page('<title>只有标题</title>')) is None


@pytest.mark.parametrize('response_kind', ['large_article', 'over_limit', 'verification'])
def test_shared_card_fetches_large_article_without_cookies(common_service_fixture, monkeypatch, response_kind):
    factory, ids = common_service_fixture
    context = PluginExecutionContext(ids['alpha_tenant_id'], ids['alpha_binding_id'], ids['alpha_bot_id'],
                                     conversation_id=12, chat_type='group', message_type='file')
    xml = f'<msg><appmsg><type>5</type><title>鸟类观察</title><url>{ARTICLE_URL}</url></appmsg></msg>'
    request = PluginExecutionRequest('execute', {}, {'content': '[链接/文件]', 'normalized_event': {'details': {'content_xml': xml}}}, context)
    markup = '<title></title><script>' + ' ' * (3 * 1024 * 1024) + '</script>'
    markup += f'<h1 id="activity-name">鸟类观察</h1><div id="js_content"><section>{BODY}</section></div><p>推荐文章</p>'
    data = markup.encode('utf-8') if response_kind == 'large_article' else '<title>环境异常</title><p>请完成验证</p>'.encode('utf-8')
    length = len(data) if response_kind != 'over_limit' else 8 * 1024 * 1024 + 1
    wire = b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=UTF-8\r\nContent-Type: text/html; charset=UTF-8\r\n'
    wire += f'Content-Length: {length}\r\n\r\n'.encode('ascii') + data
    sock = Mock()
    sock.makefile.return_value = BytesIO(wire)
    tls = Mock()
    tls.wrap_socket.return_value = sock
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(socket, 'socket', Mock(return_value=sock))
    monkeypatch.setattr(safe_fetch.ssl, 'create_default_context', lambda: tls)
    monkeypatch.setattr(plugin, 'get_ai_setting', lambda _: ai_setting(context.tenant_id))
    monkeypatch.setattr(plugin, 'snapshot_setting', lambda _: SimpleNamespace(api_base='https://example.test/v1', model='test', api_key='test'))
    model = Mock()
    model.return_value.generate.return_value.text = '志愿者介绍了鸟类观察方法。'
    monkeypatch.setattr(plugin, 'PluginTextGenerationClient', model)
    with tenant_scope(context.tenant_id), tenant_session(factory) as db:
        result = plugin.execute_message_action(db, request)
        if response_kind == 'large_article':
            assert result.status == 'success'
            assert result.output['reply_text'] == '鸟类观察\n志愿者介绍了鸟类观察方法。'
            assert json.loads(model.return_value.generate.call_args.kwargs['user_prompt']) == {'标题': '鸟类观察', '正文': BODY}
        else:
            assert not result.output
            model.assert_not_called()
    request_bytes = sock.sendall.call_args.args[0]
    assert b'User-Agent: Mozilla/5.0' in request_bytes
    assert b'Accept-Language: zh-CN' in request_bytes
    assert b'Cookie:' not in request_bytes and b'Authorization:' not in request_bytes
    sock.connect.assert_called_once_with(('93.184.216.34', 443))
    tls.wrap_socket.assert_called_once_with(sock, server_hostname='mp.weixin.qq.com')
    assert sock.close.called


def test_group_reply_dedupe_scope_and_failures(common_service_fixture, monkeypatch):
    factory, ids = common_service_fixture
    context = PluginExecutionContext(ids['alpha_tenant_id'], ids['alpha_binding_id'], ids['alpha_bot_id'],
                                     conversation_id=12, chat_type="group", message_type="text")
    request = PluginExecutionRequest("execute", {}, {"content": ARTICLE_URL, "conversation_id": 999}, context)
    fetch = Mock(return_value=page('<title>鸟类观察</title><div id="js_content">' + BODY + '</div>'))
    model = Mock()
    model.return_value.generate.return_value.text = '志愿者分享了观察记录。\n• 核对观察数据。'
    monkeypatch.setattr(plugin, 'read_public_html', fetch)
    monkeypatch.setattr(plugin, 'PluginTextGenerationClient', model)
    monkeypatch.setattr(plugin, 'snapshot_setting', lambda _: SimpleNamespace(api_base='https://example.test/v1', model='test', api_key='test'))
    with tenant_scope(context.tenant_id), tenant_session(factory) as db:
        assert plugin.execute_message_action(db, request).status == 'not_configured'
        fetch.assert_not_called()
        monkeypatch.setattr(plugin, 'get_ai_setting', lambda _: ai_setting(context.tenant_id))
        assert plugin.execute_message_action(db, replace(request, config={'summary_model': ['invalid']})).status == 'invalid_config'
        assert plugin.execute_message_action(db, replace(request, config={'model_timeout_seconds': 61})).status == 'invalid_config'
        output = plugin.execute_message_action(db, replace(request, config={'summary_model': 'article-only', 'model_timeout_seconds': 60})).output
        assert model.return_value.generate.call_args.kwargs['timeout_seconds'] == 60
        model.assert_called_once_with(api_base='https://example.test/v1', model='article-only', api_key='test')
        assert output['reply_text'] == '鸟类观察\n志愿者分享了观察记录。\n• 核对观察数据。'
        assert json.loads(model.return_value.generate.call_args.kwargs['user_prompt']) == {'标题': '鸟类观察', '正文': BODY}
        assert not plugin.execute_message_action(db, request).output
        fetch.assert_called_once_with(ARTICLE_URL, limit=8 * 1024 * 1024)
        other = replace(request, context=replace(context, conversation_id=13))
        assert plugin.execute_message_action(db, other).output
        model.assert_called_with(api_base='https://example.test/v1', model='test', api_key='test')
        assert model.return_value.generate.call_args.kwargs['timeout_seconds'] == 30
        fetch.side_effect = UnsafeUrlError('blocked')
        failure = replace(request, payload={'content': 'https://example.test/other'})
        assert not plugin.execute_message_action(db, failure).output
        assert model.return_value.generate.call_count == 2
        assert not plugin.execute_message_action(db, replace(request, context=None)).output
        fetch.side_effect = None
        fetch.return_value = page('<title>文章已删除</title>')
        for index in range(3):
            assert not plugin.execute_message_action(db, replace(request, payload={'content': f'https://example.test/removed/{index}'})).output
        count = fetch.call_count
        assert not plugin.execute_message_action(db, replace(request, payload={'content': 'https://example.test/new'})).output
        assert fetch.call_count == count and model.return_value.generate.call_count == 2
