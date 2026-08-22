"""私聊总入口(低优先级): LLM 意图路由。

只响应主号私聊。命令已在高优先级拦截, 走到这里的都是自然语言。
修改/删除日程的目标定位: 把当前日程编号列表注入 prompt, 由 LLM 返回 target_index。
"""

import json
from datetime import timedelta

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, PrivateMessageEvent
from nonebot.rule import Rule

from .. import askq, events
from ..config import get_settings
from ..llm import chat_json
from ..models import ScheduleEvent
from ..prompts import INTENT_SYSTEM, IntentResult, intent_user
from ..timetz import fmt, now_local, parse_iso
from .commands import HELP_TEXT


async def _is_master_private(event: Event) -> bool:
    return (
        isinstance(event, PrivateMessageEvent)
        and event.user_id == get_settings().master_qq
    )


chat = on_message(rule=Rule(_is_master_private), priority=50, block=True)

PARSE_AGAIN = "没听懂时间 🤔 请再说一次, 如「明天下午3点」(或回复「取消」跳过)"
NOT_FOUND = "没找到你说的那条日程 🤔 可以发「日程」看看现有安排, 再说具体一点(如公司名+类型)"


def _pick(index: int | None, events_list: list[ScheduleEvent]) -> ScheduleEvent | None:
    """LLM 返回的 1 起始编号 -> 日程对象。"""
    if index is not None and 1 <= index <= len(events_list):
        return events_list[index - 1]
    return None


def _events_text(events_list: list[ScheduleEvent]) -> str | None:
    if not events_list:
        return None
    lines = []
    for i, ev in enumerate(events_list, 1):
        label = events.TYPE_LABELS.get(ev.event_type, "日程")
        when = fmt(ev.start_time)
        if ev.end_time:
            when += (
                f"~{ev.end_time:%H:%M}"
                if ev.end_time.date() == ev.start_time.date()
                else f"~{fmt(ev.end_time)}"
            )
        company = f" ({ev.company})" if ev.company else ""
        lines.append(f"{i}. {when} 【{label}】{ev.title}{company}")
    return "\n".join(lines)


@chat.handle()
async def _(event: PrivateMessageEvent):
    text = event.get_plaintext().strip()
    if not text:
        return

    q = await askq.active()
    q_action = "create"
    if q:
        try:
            q_action = json.loads(q.event_draft).get("action", "create")
        except json.JSONDecodeError:
            pass
    upcoming = await events.upcoming_events()

    result = await chat_json(
        INTENT_SYSTEM,
        intent_user(text, q.question_text if q else None, _events_text(upcoming)),
        IntentResult,
    )
    if result is None:
        await chat.finish("我暂时没听懂 🤯 可以发「帮助」查看用法。")

    intent = result.intent
    now = now_local()

    # 1. 回答等待中的问题(按问题的 action 分发)
    if intent == "answer_pending" and q is not None:
        if q_action == "delete":  # 确认类问题
            if result.confirm is None:
                await chat.finish("请回复「确认」或「取消」")
            done, ev = await askq.resolve_confirm(q.id, result.confirm, text)
            if not done:
                await chat.finish("该问题已失效。")
            if result.confirm:
                await chat.finish(f"🗑 已删除日程:\n{events.render_event(ev)}")
            await chat.finish("好的, 不删了 ✅")

        # create / update_time 时间问题
        when = parse_iso(result.answer_datetime)
        if when is None:
            await chat.finish(PARSE_AGAIN)
        if when < now - timedelta(minutes=5):
            await chat.finish("这个时间已经过去了诶, 说一个未来的时间吧(或「取消」跳过)")
        ev, action = await askq.resolve(q.id, when, text)
        if ev is None:
            await chat.finish("该问题已失效。")
        if action == "update_time":
            await chat.finish(f"✅ 已改期:\n{events.render_event(ev)}")
        await chat.finish(f"✅ 已写入日程:\n{events.render_event(ev)}")

    # 2. 取消等待中的问题
    if intent == "cancel_pending":
        if q:
            await askq.cancel_active()
            await chat.finish("好的, 已跳过这个问题 ✅")
        await chat.finish("当前没有等待回答的问题。")

    # 3. 添加日程
    if intent == "add_schedule" and result.event:
        ie = result.event
        start, end = parse_iso(ie.start), parse_iso(ie.end)
        if start is None:
            # 复用询问队列: 先记下来, 问用户时间
            await askq.enqueue(
                f"📝 已记下「{ie.title}」, 你想安排在什么时候?",
                {
                    "title": ie.title,
                    "event_type": ie.event_type,
                    "location_or_url": ie.location_or_url,
                    "notes": ie.notes,
                },
            )
            return  # 问题已由 promote_next 发出
        if start < now - timedelta(minutes=5):
            await chat.finish("这个时间已经过去了诶, 说一个未来的时间吧")
        if end and end < start:
            end = None
        ev = await events.add_event(
            title=ie.title,
            start_time=start,
            end_time=end,
            event_type=ie.event_type,
            location_or_url=ie.location_or_url,
            notes=ie.notes,
            source="manual",
        )
        await chat.finish(f"✅ 已写入日程:\n{events.render_event(ev)}")

    # 4. 修改日程
    if intent == "update_schedule":
        target = _pick(result.target_index, upcoming)
        if target is None:
            await chat.finish(NOT_FOUND)
        new_start = parse_iso(result.event.start) if result.event else None
        new_end = parse_iso(result.event.end) if result.event else None
        if new_start is None:
            # 用户没说改到什么时候 -> 入队询问
            await askq.enqueue(
                f"📝 想把「{target.title}」(原定 {fmt(target.start_time)})改到什么时候?",
                {"action": "update_time", "event_id": target.id},
            )
            return  # 问题已由 promote_next 发出
        if new_start < now - timedelta(minutes=5):
            await chat.finish("这个时间已经过去了诶, 说一个未来的时间吧")
        ev = await events.update_event(target.id, start_time=new_start, end_time=new_end)
        if ev is None:
            await chat.finish("该日程已失效。")
        await chat.finish(f"✅ 已改期:\n{events.render_event(ev)}")

    # 5. 删除日程(先确认)
    if intent == "delete_schedule":
        target = _pick(result.target_index, upcoming)
        if target is None:
            await chat.finish(NOT_FOUND)
        await askq.enqueue(
            "🗑 确认删除这条日程吗?\n"
            f"{events.render_event(target)}\n"
            "回复「确认」删除, 回复「取消」保留。",
            {"action": "delete", "event_id": target.id},
        )
        return  # 确认问题已由 promote_next 发出

    # 6. 查询日程
    if intent == "query_schedule":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        scope = result.query_scope or "today"
        if scope == "tomorrow":
            evs = await events.events_between(
                day_start + timedelta(days=1), day_start + timedelta(days=2)
            )
            await chat.finish(events.render_events("📅 明日日程", evs, "明天没有日程安排 🎉"))
        elif scope == "week":
            evs = await events.events_between(day_start, day_start + timedelta(days=7))
            await chat.finish(events.render_week(evs))
        else:
            evs = await events.events_between(day_start, day_start + timedelta(days=1))
            await chat.finish(events.render_today(evs))

    # 7. 其他: 不闲聊, 引导回功能
    await chat.finish(result.reply or HELP_TEXT)
