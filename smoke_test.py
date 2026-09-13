"""冒烟测试: 临时 DB 上验证待确认列表语义与区间查询渲染。

运行: .venv/Scripts/python.exe smoke_test.py (跑完自动删除临时 DB)
notify 在无 bot 环境下会静默失败(设计如此), 不影响断言。
"""

import asyncio
import os
from datetime import timedelta
from pathlib import Path

DB = "data/smoke_test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"

import nonebot  # noqa: E402

nonebot.init()  # plugins/chat.py 注册 matcher 需要 driver

from emailbot import askq, events  # noqa: E402
from emailbot.db import SessionFactory, init_db  # noqa: E402
from emailbot.models import PendingQuestion  # noqa: E402
from emailbot.plugins.chat import _render_query  # noqa: E402
from emailbot.timetz import now_local  # noqa: E402


async def main() -> None:
    await init_db()

    # 1. 三个问题入列, 全部立即 pending(不再有 queued 阻塞)
    q1 = await askq.enqueue(
        "❓ 收到「字节跳动后端岗笔试」(字节跳动), 邮件没有给出明确时间。\n你想把它安排在什么时候?",
        {
            "title": "字节跳动后端岗笔试",
            "company": "字节跳动",
            "event_type": "written_test",
            "notes": "邮件没有给出明确时间",
            "duration_minutes": 90,
        },
    )
    q2 = await askq.enqueue(
        "❓ 收到「腾讯前端岗测评」(腾讯), 邮件没有给出明确时间。\n你想把它安排在什么时候?",
        {"title": "腾讯前端岗测评", "company": "腾讯", "notes": "邮件没有给出明确时间"},
    )
    ev_del = await events.add_event(title="旧笔试", start_time=now_local() + timedelta(days=1))
    q3 = await askq.enqueue(
        "🗑 确认删除这条日程吗?\n· 旧笔试", {"action": "delete", "event_id": ev_del.id}
    )
    # 遗留 queued 行(旧版队列残留, 无 asked_at/title 字段)应视为待确认
    ev_upd = await events.add_event(title="阿里实习面试", start_time=now_local() + timedelta(days=2))
    async with SessionFactory() as s:
        s.add(
            PendingQuestion(
                status="queued",
                question_text="📝 想把「阿里实习面试」(原定 09-15 10:00)改到什么时候?",
                event_draft=f'{{"action": "update_time", "event_id": {ev_upd.id}}}',
                created_at=now_local() - timedelta(hours=3),
            )
        )
        await s.commit()

    qs = await askq.list_open()
    assert len(qs) == 4, f"应为 4 条待确认, 实际 {len(qs)}"
    assert all(q.status in ("pending", "queued") for q in qs)
    rendered = await askq.render_pending(qs)
    print(rendered)
    assert "字节跳动后端岗笔试" in rendered and "待确认删除" in rendered and "待改期" in rendered
    # delete/update 草稿无 title 时按 event_id 查到真标题, 不会显示「确认」
    assert "「旧笔试」" in rendered and "「阿里实习面试」" in rendered

    # 2. 乱序回答: 先答第 2 个, 再答第 1 个(带时长 -> 自动生成 end)
    when = (now_local() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    ev2, action = await askq.resolve(q2, when, "明天下午2点")
    assert action == "create" and ev2.title == "腾讯前端岗测评" and ev2.end_time is None
    ev1, _ = await askq.resolve(q1, when, "明天下午2点")
    assert ev1.end_time == when + timedelta(minutes=90), "duration_minutes 应生效"
    assert len(await askq.list_open()) == 2

    # 3. 删除确认
    done, deleted = await askq.resolve_confirm(q3, True, "确认")
    assert done and deleted and deleted.status == "cancelled"
    # 重复回答已了结的问题 -> 失效
    done2, _ = await askq.resolve_confirm(q3, True, "确认")
    assert not done2

    # 4. 引用命中遗留 queued(问题文本「」取标题 + update_time 改期)
    hit = await askq.find_by_quote("📝 想把「阿里实习面试」(原定 09-15 10:00)改到什么时候?")
    assert hit is not None and askq.action_of(hit) == "update_time"
    assert await askq.display_title(hit) == "阿里实习面试"
    ev3, action = await askq.resolve(hit.id, when, "明天下午2点")
    assert action == "update_time" and ev3.start_time == when
    assert not await askq.list_open()

    # 5. 取消 + 过期
    q4 = await askq.enqueue("❓ 收到「美团测开笔试」。", {"title": "美团测开笔试"})
    c = await askq.cancel(q4)
    assert c and c.status == "cancelled"
    q5 = await askq.enqueue("❓ 收到「网易游戏策划测评」。", {"title": "网易游戏策划测评"})
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, q5)
        q.asked_at = now_local() - timedelta(hours=100)
        s.add(q)
        await s.commit()
    await askq.expire_old()
    assert not await askq.list_open(), "过期后应无待确认"

    # 6. 区间查询渲染(单日/明天/多天)
    day0 = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    evs = await events.events_between(day0, day0 + timedelta(days=7))
    single = _render_query(day0 + timedelta(days=1), day0 + timedelta(days=2), evs)
    assert single.startswith("📅 明日日程"), single
    multi = _render_query(day0 + timedelta(days=1), day0 + timedelta(days=4), evs)
    assert "~" in multi.splitlines()[0] and "—— " in multi, multi
    empty = _render_query(day0 + timedelta(days=5), day0 + timedelta(days=6), [])
    assert "没有日程安排" in empty
    print("SMOKE_OK")


asyncio.run(main())


async def _cleanup() -> None:
    from emailbot.db import engine

    await engine.dispose()  # Windows 下不先关连接会占用文件
    Path(DB).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(DB + suffix).unlink(missing_ok=True)


asyncio.run(_cleanup())
