"""对话历史: 记录与主号的往来消息(含 bot 主动推送), 供意图路由注入上下文。

注入最近 HISTORY_LIMIT 条; 表里最多保留 KEEP_MAX 条, 超出即修剪。
"""

from sqlalchemy import delete
from sqlmodel import select

from .db import SessionFactory
from .models import ChatMessage
from .timetz import now_local

HISTORY_LIMIT = 20   # 注入 LLM 的条数
KEEP_MAX = 100       # 表里最多保留的条数


async def record(role: str, text: str) -> None:
    if not text:
        return
    async with SessionFactory() as s:
        s.add(ChatMessage(role=role, text=text, created_at=now_local()))
        await s.commit()
        # 修剪: 只保留最新 KEEP_MAX 条
        keep = select(ChatMessage.id).order_by(ChatMessage.id.desc()).limit(KEEP_MAX)
        await s.execute(delete(ChatMessage).where(ChatMessage.id.not_in(keep)))
        await s.commit()


async def record_user(text: str) -> None:
    await record("user", text)


async def record_bot(text: str) -> None:
    await record("assistant", text)


async def recent(limit: int = HISTORY_LIMIT) -> list[ChatMessage]:
    """按时间正序返回最近 limit 条。"""
    async with SessionFactory() as s:
        rows = (
            await s.exec(select(ChatMessage).order_by(ChatMessage.id.desc()).limit(limit))
        ).all()
        return list(reversed(rows))


async def as_messages(limit: int = HISTORY_LIMIT) -> list[dict]:
    """转成 OpenAI messages 格式, 直接拼进 chat.completions。"""
    return [{"role": m.role, "content": m.text} for m in await recent(limit)]
