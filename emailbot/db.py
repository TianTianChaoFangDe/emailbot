"""异步数据库引擎与初始化。"""

from pathlib import Path

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, echo=False)


@sa_event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    # WAL: 定时任务与消息 handler 并发读写不互斥
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.close()


SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    # sqlite 文件父目录(如 data/)可能不存在
    prefix = "sqlite+aiosqlite:///"
    if _settings.database_url.startswith(prefix):
        Path(_settings.database_url[len(prefix):]).parent.mkdir(parents=True, exist_ok=True)
    from . import models  # noqa: F401  确保模型已注册到 metadata

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
