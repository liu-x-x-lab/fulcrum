"""iPad 展示与群详情准备的事务边界，使用最小租户数据，不启动协议客户端。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import SHANGHAI_TZ
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, GroupPluginScope, Plugin
from app.models.saas import Tenant
from app.services.wechat_ipad import (
    build_wechat_ipad_chatroom_detail,
    build_wechat_ipad_chatroom_list,
    build_wechat_ipad_group_summary,
    build_wechat_ipad_overview,
)


@pytest.mark.parametrize("login_state,qrcode_status", [("offline", "expired"), ("online", "confirmed"), ("waiting_scan", "waiting_scan")])
def test_overview_does_not_mutate_persisted_login_state(login_state, qrcode_status):
    """诊断页面可以推导显示状态，但不能把推导结果写回渠道或改动原始运行信息。"""

    runtime = json.dumps({"receive_total": 7, "dedupe_total": 2, "last_message_sync_count": 3})
    channel = Channel(id=1, tenant_id=1, bot_id=1, name="View test", channel_type="wechat_ipad",
                      login_state=login_state, qrcode_status=qrcode_status,
                      runtime_info_json=runtime, adapter_config_json="{}")
    result = build_wechat_ipad_overview(channel, now=datetime(2026, 1, 1, tzinfo=SHANGHAI_TZ))
    assert result["login_state"] == login_state
    assert result["qrcode_status"] == qrcode_status
    assert result["message_stats"]["receive_total"] == 7
    assert result["message_stats"]["dedupe_total"] == 2
    assert (channel.login_state, channel.qrcode_status, channel.runtime_info_json) == (login_state, qrcode_status, runtime)


def test_group_detail_materializes_scopes_only_inside_callers_transaction(tmp_path):
    """列表不写作用域；详情按原规则补建继承行，但调用方回滚后记录必须消失。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'ipad-views.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        with Session(engine) as db:
            tenant = Tenant(code="ipad-views", name="View tenant", status="active")
            plugin = Plugin(code="view_test", display_name="View test")
            db.add_all([tenant, plugin])
            db.flush()
            bot = Bot(tenant_id=tenant.id, name="View bot")
            db.add(bot)
            db.flush()
            channel = Channel(tenant_id=tenant.id, bot_id=bot.id, name="View account", channel_type="wechat_ipad")
            binding = BotPlugin(tenant_id=tenant.id, bot_id=bot.id, plugin_id=plugin.id,
                                status="enabled", group_default_enabled=False, config_json="{}")
            db.add_all([channel, binding])
            db.flush()
            room = Chatroom(tenant_id=tenant.id, bot_id=bot.id, channel_id=channel.id,
                            external_id="view-room@chatroom", name="View group")
            db.add(room)
            db.commit()
            tenant_id, channel_id, room_id = tenant.id, channel.id, room.id

        with tenant_scope(tenant_id), tenant_session(factory) as db:
            channel, room = db.get(Channel, channel_id), db.get(Chatroom, room_id)
            assert build_wechat_ipad_chatroom_list(db, channel)["total"] == 1
            build_wechat_ipad_group_summary(db, channel)
            assert db.scalar(select(func.count(GroupPluginScope.id))) == 0
            detail = build_wechat_ipad_chatroom_detail(db, channel, room)
            scope = db.scalar(select(GroupPluginScope))
            assert scope is not None and scope.tenant_id == tenant_id
            assert scope.scope_status == "inherited" and scope.enabled is False
            assert len(detail["bindings"]) == 1
            # 重复构建不能再生成重复作用域，也不能代替调用方提交事务。
            build_wechat_ipad_chatroom_detail(db, channel, room)
            assert db.scalar(select(func.count(GroupPluginScope.id))) == 1
            db.rollback()
            assert db.scalar(select(func.count(GroupPluginScope.id))) == 0
    finally:
        engine.dispose()
