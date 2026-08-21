"""日程服务: 增查、提醒扫描、展示渲染。"""

from datetime import datetime, timedelta

from sqlmodel import select

from .db import SessionFactory
from .models import ScheduleEvent
from .timetz import fmt, fmt_day, now_local

TYPE_LABELS = {
    "written_test": "笔试",
    "ai_coding": "AI Coding",
    "ai_interview": "AI 面试",
    "interview": "面试",
    "assessment": "测评",
    "other": "日程",
}


async def add_event(
    *,
    title: str,
    start_time: datetime,
    end_time: datetime | None = None,
    company: str | None = None,
    position: str | None = None,
    event_type: str = "other",
    location_or_url: str | None = None,
    notes: str | None = None,
    source: str = "manual",
    source_mail_id: int | None = None,
    remind_before_minutes: int | None = None,
) -> ScheduleEvent:
    from .config import get_settings

    ev = ScheduleEvent(
        title=title,
        company=company,
        position=position,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        location_or_url=location_or_url,
        notes=notes,
        source=source,
        source_mail_id=source_mail_id,
        remind_before_minutes=remind_before_minutes
        or get_settings().remind_before_minutes,
        created_at=now_local(),
    )
    async with SessionFactory() as s:
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
    return ev


async def events_between(
    start: datetime, end: datetime, status: str = "active"
) -> list[ScheduleEvent]:
    async with SessionFactory() as s:
        rows = await s.exec(
            select(ScheduleEvent)
            .where(ScheduleEvent.status == status)
            .where(ScheduleEvent.start_time >= start)
            .where(ScheduleEvent.start_time < end)
            .order_by(ScheduleEvent.start_time)
        )
        return list(rows.all())


def render_event(ev: ScheduleEvent) -> str:
    """单条日程展示。"""
    label = TYPE_LABELS.get(ev.event_type, "日程")
    when = fmt(ev.start_time)
    if ev.end_time:
        # 同一天只显示结束时刻, 跨天显示完整日期
        when += (
            f"~{ev.end_time:%H:%M}"
            if ev.end_time.date() == ev.start_time.date()
            else f" ~ {fmt(ev.end_time)}"
        )
    head = f"· {when} 【{label}】{ev.title}"
    extra = []
    if ev.company:
        extra.append(ev.company + (f" {ev.position}" if ev.position else ""))
    elif ev.position:
        extra.append(ev.position)
    if ev.location_or_url:
        extra.append(ev.location_or_url)
    if ev.notes:
        extra.append(ev.notes)
    if extra:
        head += "\n    " + " | ".join(extra)
    return head


def render_events(title: str, events: list[ScheduleEvent], empty_text: str) -> str:
    if not events:
        return f"{title}\n{empty_text}"
    lines = [title]
    last_day = None
    for ev in events:
        day = ev.start_time.date()
        if day != last_day:
            last_day = day
            lines.append(f"—— {fmt_day(ev.start_time)} ——")
        lines.append(render_event(ev))
    return "\n".join(lines)


def render_today(events: list[ScheduleEvent]) -> str:
    n = now_local()
    return render_events(
        f"📅 今日日程 ({fmt_day(n)})", events, "今天没有日程安排 🎉"
    )


def render_week(events: list[ScheduleEvent]) -> str:
    return render_events("📅 未来 7 天日程", events, "未来 7 天没有日程安排 🎉")


async def due_reminders() -> list[ScheduleEvent]:
    """到达提醒窗口且未提醒的日程: start - remind_before <= now < start。"""
    n = now_local()
    async with SessionFactory() as s:
        rows = await s.exec(
            select(ScheduleEvent)
            .where(ScheduleEvent.status == "active")
            .where(ScheduleEvent.reminded_at.is_(None))
            .where(ScheduleEvent.start_time > n)
        )
        return [
            ev
            for ev in rows.all()
            if ev.start_time - timedelta(minutes=ev.remind_before_minutes) <= n
        ]


async def mark_reminded(event_id: int) -> None:
    async with SessionFactory() as s:
        ev = await s.get(ScheduleEvent, event_id)
        if ev:
            ev.reminded_at = now_local()
            s.add(ev)
            await s.commit()
