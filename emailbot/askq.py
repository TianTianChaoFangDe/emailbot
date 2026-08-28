"""询问队列状态机(单 active): 时间不确定/窗口过大时向用户发问。

流转: enqueue -> queued -(无 active 时 promote)-> pending(已发出)
       pending -> answered / cancelled / expired -> promote_next
任意时刻最多一条 pending, 用户回复永远只匹配当前这条, 避免多问混淆。
"""

import json
from datetime import datetime, timedelta

from sqlmodel import select

from . import events
from .config import get_settings
from .db import SessionFactory
from .models import PendingQuestion, ScheduleEvent
from .notify import notify
from .timetz import now_local

ASK_HINT = "\n回复如「明天下午3点」; 回复「取消」跳过。"


async def enqueue(
    question_text: str, event_draft: dict, source_mail_id: int | None = None
) -> int:
    """入队并尝试立即发出, 返回新问题 id。

    调用方可对比 active().id 判断问题是否被前面的 pending 阻塞(排队中),
    以便给用户"先回答/取消前一个问题"的提示。
    """
    async with SessionFactory() as s:
        q = PendingQuestion(
            question_text=question_text,
            event_draft=json.dumps(event_draft, ensure_ascii=False),
            source_mail_id=source_mail_id,
            created_at=now_local(),
        )
        s.add(q)
        await s.commit()
        await s.refresh(q)
        qid = q.id
    await promote_next()
    return qid


async def active() -> PendingQuestion | None:
    async with SessionFactory() as s:
        rows = await s.exec(
            select(PendingQuestion)
            .where(PendingQuestion.status == "pending")
            .order_by(PendingQuestion.id)
        )
        return rows.first()


async def promote_next() -> None:
    """无 active 时把最早的 queued 提为 pending 并发给用户。"""
    async with SessionFactory() as s:
        has = (
            await s.exec(
                select(PendingQuestion.id).where(PendingQuestion.status == "pending")
            )
        ).first()
        if has:
            return
        nxt = (
            await s.exec(
                select(PendingQuestion)
                .where(PendingQuestion.status == "queued")
                .order_by(PendingQuestion.id)
            )
        ).first()
        if not nxt:
            return
        nxt.status = "pending"
        nxt.asked_at = now_local()
        s.add(nxt)
        await s.commit()
        text = nxt.question_text
    await notify(text + ASK_HINT)


async def renotify_active() -> None:
    """bot 重新连上 QQ 时, 重发当前 pending 问题(掉线期间的通知可能已丢失)。"""
    q = await active()
    if q:
        await notify(q.question_text + ASK_HINT)


async def find_by_quote(quote: str | None) -> PendingQuestion | None:
    """引用消息命中某个待答/排队问题(引用的往往就是 bot 发的那条问题消息)。

    匹配依据: 草稿标题/公司名出现在引用内容里, 或引用内容就是问题文本本身。
    优先 active(pending), 再按入队顺序看 queued。
    """
    if not quote:
        return None
    async with SessionFactory() as s:
        rows = (
            await s.exec(
                select(PendingQuestion)
                .where(PendingQuestion.status.in_(["pending", "queued"]))
                .order_by(PendingQuestion.id)
            )
        ).all()
    for status in ("pending", "queued"):
        for q in [r for r in rows if r.status == status]:
            try:
                draft = json.loads(q.event_draft)
            except json.JSONDecodeError:
                continue
            title = draft.get("title") or ""
            company = draft.get("company") or ""
            if (title and title in quote) or (company and company in quote):
                return q
            if len(q.question_text) >= 10 and q.question_text[:15] in quote:
                return q
    return None


async def resolve(
    qid: int, when: datetime, answer_text: str
) -> tuple[ScheduleEvent | None, str]:
    """用户给出了确定时间。pending 和 queued 都允许回答(queued 被引用点名回答)。

    按草稿 action 分发:
    - create(默认): 新建日程
    - update_time: 把指定日程改到该时间(保留原时长平移)
    返回 (日程, action); 问题已失效返回 (None, "")。"""
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, qid)
        if not q or q.status not in ("pending", "queued"):
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
        await promote_next()
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
    await promote_next()
    return ev, action


async def resolve_confirm(
    qid: int, confirmed: bool, answer_text: str
) -> tuple[bool, ScheduleEvent | None]:
    """处理确认类问题(目前是删除确认)。pending/queued 均可回答。
    返回 (是否已了结, 涉及的事件)。"""
    async with SessionFactory() as s:
        q = await s.get(PendingQuestion, qid)
        if not q or q.status not in ("pending", "queued"):
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
    await promote_next()
    return True, ev


async def cancel_active() -> PendingQuestion | None:
    async with SessionFactory() as s:
        q = (
            await s.exec(
                select(PendingQuestion)
                .where(PendingQuestion.status == "pending")
                .order_by(PendingQuestion.id)
            )
        ).first()
        if not q:
            return None
        q.status = "cancelled"
        s.add(q)
        await s.commit()
    await promote_next()
    return q


async def expire_old() -> None:
    """pending 超过 PENDING_EXPIRE_HOURS 未答 -> 过期并通知, 自动问下一个。"""
    settings = get_settings()
    cutoff = now_local() - timedelta(hours=settings.pending_expire_hours)
    async with SessionFactory() as s:
        rows = (
            await s.exec(
                select(PendingQuestion).where(
                    PendingQuestion.status == "pending",
                    PendingQuestion.asked_at < cutoff,
                )
            )
        ).all()
        for q in rows:
            q.status = "expired"
            s.add(q)
        await s.commit()
        titles = [json.loads(q.event_draft).get("title", "?") for q in rows]
    for t in titles:
        await notify(f"⌛ 关于「{t}」的询问超过 {settings.pending_expire_hours} 小时未回复, 已跳过。")
    # 自愈: 正常流转各出口都会 promote, 走到这还有 "无 pending 但有 queued"
    # 说明中间环节出过异常(如过期通知发送失败), 补一次 promote 防队列卡死
    await promote_next()
