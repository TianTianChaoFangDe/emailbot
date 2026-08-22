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


async def upcoming_events(days: int = 60, limit: int = 20) -> list[ScheduleEvent]:
    """今天起的进行中日程, 供 LLM 定位修改/删除目标(编号与列表顺序一致)。"""
    n = now_local()
    day_start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionFactory() as s:
        rows = await s.exec(
            select(ScheduleEvent)
            .where(ScheduleEvent.status == "active")
            .where(ScheduleEvent.start_time >= day_start)
            .where(ScheduleEvent.start_time < day_start + timedelta(days=days))
            .order_by(ScheduleEvent.start_time)
            .limit(limit)
        )
        return list(rows.all())


async def update_event(
    event_id: int,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    title: str | None = None,
    location_or_url: str | None = None,
    notes: str | None = None,
    keep_duration: bool = True,
) -> ScheduleEvent | None:
    """修改日程。只改 start_time 且原来有 end_time 时, 按原时长平移结束时间。
    开始时间变化会重置提醒标记(reminded_at), 让新时间重新进入提醒窗口。"""
    async with SessionFactory() as s:
        ev = await s.get(ScheduleEvent, event_id)
        if not ev or ev.status != "active":
            return None
        if start_time is not None:
            if keep_duration and end_time is None and ev.end_time is not None:
                end_time = ev.end_time + (start_time - ev.start_time)
            ev.start_time = start_time
            ev.reminded_at = None
        if end_time is not None:
            ev.end_time = end_time
        if title:
            ev.title = title
        if location_or_url:
            ev.location_or_url = location_or_url
        if notes:
            ev.notes = notes
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
        return ev


async def cancel_event(event_id: int) -> ScheduleEvent | None:
    """软删除日程(status=cancelled, 留痕且不再提醒/展示)。"""
    async with SessionFactory() as s:
        ev = await s.get(ScheduleEvent, event_id)
        if not ev or ev.status != "active":
            return None
        ev.status = "cancelled"
        s.add(ev)
        await s.commit()
        await s.refresh(ev)
        return ev


def match_event_by_text(
    text: str, events_list: list[ScheduleEvent]
) -> ScheduleEvent | None:
    """文本里出现日程标题/公司名即命中(标题优先, 公司次之)。
    用于把用户引用的消息内容绑定到具体日程。"""
    for ev in events_list:
        if ev.title and ev.title in text:
            return ev
    for ev in events_list:
        if ev.company and ev.company in text:
            return ev
    return None


async def find_conflicts(
    start: datetime, end: datetime | None, exclude_id: int | None = None
) -> list[ScheduleEvent]:
    """找出与 [start, end) 时间重叠的进行中日程。end 为空按 1 小时估算。"""
    e = end or start + timedelta(hours=1)
    async with SessionFactory() as s:
        rows = await s.exec(
            select(ScheduleEvent)
            .where(ScheduleEvent.status == "active")
            .where(ScheduleEvent.start_time >= start - timedelta(days=1))
            .where(ScheduleEvent.start_time <= e + timedelta(days=1))
        )
        out = []
        for ev in rows.all():
            if exclude_id is not None and ev.id == exclude_id:
                continue
            ev_end = ev.end_time or ev.start_time + timedelta(hours=1)
            if ev.start_time < e and start < ev_end:
                out.append(ev)
        return out


def conflict_note(conflicts: list[ScheduleEvent]) -> str:
    """冲突提醒文案; 无冲突返回空串。"""
    if not conflicts:
        return ""
    items = "、".join(f"「{c.title}」({fmt(c.start_time)})" for c in conflicts)
    return f"\n⚠️ 时间冲突: 和 {items} 撞了"


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
