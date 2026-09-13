"""待确认列表: 时间不确定/窗口过大/需用户确认时产生的问题。

列表语义(非队列): 每个问题入列后立即发给用户, 所有待确认问题平铺共存,
用户可按任意顺序回答任意一条(自然语言点名或引用回复), 互不阻塞。
status: pending(待回答) -> answered / cancelled / expired
"""

import json
import re
from datetime import datetime, timedelta

from sqlmodel import func, select

from . import events
from .config import get_settings
from .db import SessionFactory
from .models import PendingQuestion, ScheduleEvent
from .notify import notify
from .timetz import now_local

ASK_HINT = "\n回复时间即可安排(如「明天下午3点」); 回复「取消」跳过; 发「待确认」可查看全部待办。"

# 历史遗留: 旧版单 active 队列里没发出的问题状态为 queued, 一律视作待确认
_OPEN = ("pending", "queued")


async def enqueue(
    question_text: str, event_draft: dict, source_mail_id: int | None = None
) -> int:
    """入列并立即发出, 返回新问题 id。不再排队, 与其他待确认问题互不阻塞。"""
    async with SessionFactory() as s:
        q = PendingQuestion(
            status="pending",
            question_text=question_text,
            event_draft=json.dumps(event_draft, ensure_ascii=False),
            source_mail_id=source_mail_id,
            created_at=now_local(),
            asked_at=now_local(),
        )
        s.add(q)
        await s.commit()
        await s.refresh(q)
        qid = q.id
    await notify(question_text + ASK_HINT)
    return qid


async def list_open() -> list[PendingQuestion]:
    """全部待确认问题, 按入列先后排序(编号与展示一致)。"""
    async with SessionFactory() as s:
        rows = await s.exec(
            select(PendingQuestion)
            .where(PendingQuestion.status.in_(_OPEN))
            .order_by(PendingQuestion.id)
        )
        return list(rows.all())


def _draft(q: PendingQuestion) -> dict:
    try:
        return json.loads(q.event_draft)
    except json.JSONDecodeError:
        return {}


async def display_title(q: PendingQuestion) -> str:
    """问题对应的日程标题: 草稿 title -> 按 event_id 查日程 -> 问题文本「」兜底。"""
    draft = _draft(q)
    if draft.get("title"):
        return draft["title"]
    if draft.get("event_id"):
        ev = await events.get_event(draft["event_id"])
        if ev:
            return ev.title
    m = re.search(r"「(.+?)」", q.question_text)
    return m.group(1) if m else "?"


def action_of(q: PendingQuestion) -> str:
    return _draft(q).get("action", "create")


async def render_pending(qs: list[PendingQuestion]) -> str:
    """待确认列表的编号展示。同一函数用于用户可见列表与 LLM prompt,
    保证用户说的「第X个」和 pending_index 对齐。"""
    lines = []
    for i, q in enumerate(qs, 1):
        draft = _draft(q)
        action = draft.get("action", "create")
        title = await display_title(q)
        if action == "delete":
            lines.append(f"{i}. [待确认删除] 「{title}」")
        elif action == "update_time":
            lines.append(f"{i}. [待改期] 「{title}」")
        else:
            company = f"({draft['company']})" if draft.get("company") else ""
            note = draft.get("notes") or "时间待定"
            lines.append(f"{i}. 「{title}」{company} —— {note}")
    return "\n".join(lines)


async def renotify_open() -> None:
    """bot 重新连上 QQ 时, 重发待确认问题(掉线期间的通知可能已丢失)。
    多条时合并成一条消息, 避免重连刷屏。"""
    qs = await list_open()
    if not qs:
        return
    if len(qs) == 1:
        await notify(qs[0].question_text + ASK_HINT)
        return
    await notify(
        f"🔔 还有 {len(qs)} 个安排等你确认:\n"
        + await render_pending(qs)
        + "\n回复如「字节那个安排在明天下午3点」, 或「取消第X个」。"
    )


async def find_by_quote(quote: str | None) -> PendingQuestion | None:
    """引用消息命中某个待确认问题(引用的往往就是 bot 发的那条问题消息)。

    匹配依据: 草稿标题/公司名(缺失时按 event_id 查日程标题)出现在引用内容里,
    或引用内容就是问题文本本身。
    """
    if not quote:
        return None
    qs = await list_open()
    for q in qs:
        draft = _draft(q)
        title = draft.get("title") or ""
        company = draft.get("company") or ""
        if not title and draft.get("event_id"):
            ev = await events.get_event(draft["event_id"])
            title = ev.title if ev else ""
        if (title and title in quote) or (company and company in quote):
            return q
        if len(q.question_text) >= 10 and q.question_text[:15] in quote:
            return q
    return None


async def resolve(
    qid: int, when: datetime, answer_text: str
) -> tuple[ScheduleEvent | None, str]:
    """用户给出了确定时间。

    按草稿 action 分发:
    - create(默认): 新建日程
    - update_time: 把指定日程改到该时间(保留原时长平移)
    返回 (日程, action); 问题已失效返回 (None, "")。"""
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, qid)
        if not q or q.status not in _OPEN:
            return None, ""
        draft = json.loads(q.event_draft)
        source_mail_id = q.source_mail_id
        q.status = "answered"
        q.answered_at = now_local()
        q.answer_text = answer_text
        s.add(q)
        await s.commit()

    action = draft.get("action", "create")
    if action == "update_time":
        ev = await events.update_event(draft["event_id"], start_time=when)
        return ev, action

    duration = draft.pop("duration_minutes", None)
    end = when + timedelta(minutes=duration) if duration else None
    ev = await events.add_event(
        title=draft.get("title") or "(未命名日程)",
        start_time=when,
        end_time=end,
        company=draft.get("company"),
        position=draft.get("position"),
        event_type=draft.get("event_type") or "other",
        location_or_url=draft.get("location_or_url"),
        notes=draft.get("notes"),
        source="ask",
        source_mail_id=source_mail_id,
    )
    return ev, action


async def resolve_confirm(
    qid: int, confirmed: bool, answer_text: str
) -> tuple[bool, ScheduleEvent | None]:
    """处理确认类问题(目前是删除确认)。
    返回 (是否已了结, 涉及的事件)。"""
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, qid)
        if not q or q.status not in _OPEN:
            return False, None
        draft = json.loads(q.event_draft)
        q.status = "answered" if confirmed else "cancelled"
        q.answered_at = now_local() if confirmed else None
        q.answer_text = answer_text
        s.add(q)
        await s.commit()

    ev = None
    if confirmed and draft.get("action") == "delete":
        ev = await events.cancel_event(draft["event_id"])
    return True, ev


async def cancel(qid: int) -> PendingQuestion | None:
    """取消指定待确认问题。"""
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, qid)
        if not q or q.status not in _OPEN:
            return None
        q.status = "cancelled"
        s.add(q)
        await s.commit()
        return q


async def expire_old() -> None:
    """待确认超过 PENDING_EXPIRE_HOURS 未答 -> 过期并合并通知。"""
    settings = get_settings()
    cutoff = now_local() - timedelta(hours=settings.pending_expire_hours)
    async with SessionFactory() as s:
        rows = (
            await s.exec(
                select(PendingQuestion).where(
                    PendingQuestion.status.in_(_OPEN),
                    # 遗留 queued 没有 asked_at, 退用 created_at
                    func.coalesce(PendingQuestion.asked_at, PendingQuestion.created_at)
                    < cutoff,
                )
            )
        ).all()
        for q in rows:
            q.status = "expired"
            s.add(q)
        await s.commit()
        titles = [await display_title(q) for q in rows]
    if not titles:
        return
    if len(titles) == 1:
        await notify(f"⌛ 关于「{titles[0]}」的询问超过 {settings.pending_expire_hours} 小时未回复, 已跳过。")
    else:
        items = "、".join(f"「{t}」" for t in titles)
        await notify(
            f"⌛ {items} 这 {len(titles)} 个询问超过 {settings.pending_expire_hours} 小时未回复, 已一并跳过。"
        )
