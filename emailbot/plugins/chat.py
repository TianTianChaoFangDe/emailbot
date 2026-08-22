"""私聊总入口(低优先级): LLM 意图路由。

只响应主号私聊。命令已在高优先级拦截, 走到这里的都是自然语言。
修改/删除日程的目标定位: 把当前日程编号列表注入 prompt, 由 LLM 返回 target_index。
上下文: 最近 20 条对话历史注入 LLM; 用户引用回复的消息原文也会带进 prompt。
"""

import json
from datetime import timedelta

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, Message, PrivateMessageEvent
from nonebot.rule import Rule

from .. import askq, events, history
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


async def _say(text: str) -> None:
    """回复用户并记入对话历史。finish 会抛异常结束处理, 之后代码不可达。"""
    await history.record_bot(text)
    await chat.finish(text)


async def _get_quoted_text(event: PrivateMessageEvent, bot: Bot) -> str | None:
    """用户引用回复了历史消息时, 取被引用消息的纯文本内容。"""
    for seg in event.message:
        if seg.type != "reply":
            continue
        mid = seg.data.get("id")
        if mid is None:
            return None
        try:
            msg = await bot.get_msg(message_id=int(mid))
            return Message(msg.get("message")).extract_plain_text().strip() or None
        except Exception:
            return None
    return None


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
async def _(event: PrivateMessageEvent, bot: Bot):
    text = event.get_plaintext().strip()
    if not text:
        return

    quote = await _get_quoted_text(event, bot)
    history_msgs = await history.as_messages()
    await history.record_user(f"{text}(引用:「{quote[:80]}」)" if quote else text)

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
        intent_user(text, q.question_text if q else None, _events_text(upcoming), quote),
        IntentResult,
        history=history_msgs,
    )
    if result is None:
        await _say("我暂时没听懂 🤯 可以发「帮助」查看用法。")

    intent = result.intent
    now = now_local()

    # 1. 回答等待中的问题(按问题的 action 分发)
    if intent == "answer_pending" and q is not None:
        if q_action == "delete":  # 确认类问题
            if result.confirm is None:
                await _say("请回复「确认」或「取消」")
            done, ev = await askq.resolve_confirm(q.id, result.confirm, text)
            if not done:
                await _say("该问题已失效。")
            if result.confirm:
                await _say(f"🗑 已删除日程:\n{events.render_event(ev)}")
            await _say("好的, 不删了 ✅")

        # create / update_time 时间问题
        when = parse_iso(result.answer_datetime)
        if when is None:
            await _say(PARSE_AGAIN)
        if when < now - timedelta(minutes=5):
            await _say("这个时间已经过去了诶, 说一个未来的时间吧(或「取消」跳过)")
        ev, action = await askq.resolve(q.id, when, text)
        if ev is None:
            await _say("该问题已失效。")
        if action == "update_time":
            await _say(f"✅ 已改期:\n{events.render_event(ev)}")
        await _say(f"✅ 已写入日程:\n{events.render_event(ev)}")

    # 2. 取消等待中的问题
    if intent == "cancel_pending":
        if q:
            await askq.cancel_active()
            await _say("好的, 已跳过这个问题 ✅")
        await _say("当前没有等待回答的问题。")

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
            return  # 问题已由 promote_next 发出(notify 内会记历史)
        if start < now - timedelta(minutes=5):
            await _say("这个时间已经过去了诶, 说一个未来的时间吧")
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
        await _say(f"✅ 已写入日程:\n{events.render_event(ev)}")

    # 4. 修改日程
    if intent == "update_schedule":
        target = _pick(result.target_index, upcoming)
        if target is None:
            await _say(NOT_FOUND)
        new_start = parse_iso(result.event.start) if result.event else None
        new_end = parse_iso(result.event.end) if result.event else None
        if new_start is None:
            # 用户没说改到什么时候 -> 入队询问
            await askq.enqueue(
                f"📝 想把「{target.title}」(原定 {fmt(target.start_time)})改到什么时候?",
                {"action": "update_time", "event_id": target.id},
            )
            return
        if new_start < now - timedelta(minutes=5):
            await _say("这个时间已经过去了诶, 说一个未来的时间吧")
        ev = await events.update_event(target.id, start_time=new_start, end_time=new_end)
        if ev is None:
            await _say("该日程已失效。")
        await _say(f"✅ 已改期:\n{events.render_event(ev)}")

    # 5. 删除日程(先确认)
    if intent == "delete_schedule":
        target = _pick(result.target_index, upcoming)
        if target is None:
            await _say(NOT_FOUND)
        await askq.enqueue(
            "🗑 确认删除这条日程吗?\n"
            f"{events.render_event(target)}\n"
            "回复「确认」删除, 回复「取消」保留。",
            {"action": "delete", "event_id": target.id},
        )
        return

    # 6. 查询日程
    if intent == "query_schedule":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        scope = result.query_scope or "today"
        if scope == "tomorrow":
            evs = await events.events_between(
                day_start + timedelta(days=1), day_start + timedelta(days=2)
            )
            await _say(events.render_events("📅 明日日程", evs, "明天没有日程安排 🎉"))
        elif scope == "week":
            evs = await events.events_between(day_start, day_start + timedelta(days=7))
            await _say(events.render_week(evs))
        else:
            evs = await events.events_between(day_start, day_start + timedelta(days=1))
            await _say(events.render_today(evs))

    # 7. 其他: 不闲聊, 引导回功能
    await _say(result.reply or HELP_TEXT)
