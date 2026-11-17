"""浏览器页面共用的查询入口，沿用调用方租户会话。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Bot, BotPlugin, Channel


def filtered_bots_query(db: Session):
    return db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()


def build_bot_name_map(bots: list[Bot]) -> dict[int, str]:
    return {int(bot.id): str(bot.name or "").strip() or f"Bot #{bot.id}" for bot in bots}


def filtered_channels_query(db: Session):
    return db.scalars(select(Channel).where(Channel.deleted_at.is_(None)).order_by(Channel.id.desc())).all()


def filtered_bindings_query(db: Session):
    return db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound").order_by(BotPlugin.id.desc())).all()
