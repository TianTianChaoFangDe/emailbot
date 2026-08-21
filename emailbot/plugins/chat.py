"""私聊总入口(低优先级): LLM 意图路由。

只响应主号私聊。命令已在高优先级拦截, 走到这里的都是自然语言。
"""

from datetime import timedelta

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Event, PrivateMessageEvent
from nonebot.rule import Rule

from .. import askq, events
from ..config import get_settings
from ..llm import chat_json
from ..prompts import INTENT_SYSTEM, IntentResult, intent_user
from ..timetz import now_local, parse_iso
from .commands import HELP_TEXT


async def _is_master_private(event: Event) -> bool:
    return (
        isinstance(event, PrivateMessageEvent)
        and event.user_id == get_settings().master_qq
    )


chat = on_message(rule=Rule(_is_master_private), priority=50, block=True)

PARSE_AGAIN = "没听懂时间 🤔 请再说一次, 如「明天下午3点」(或回复「取消」跳过)"


@chat.handle()
async def _(event: PrivateMessageEvent):
    text = event.get_plaintext().strip()
    if not text:
        return

    q = await askq.active()
    result = await chat_json(
        INTENT_SYSTEM, intent_user(text, q.question_text if q else None), IntentResult
    )
    if result is None:
        await chat.finish("我暂时没听懂 🤯 可以发「帮助」查看用法。")

    intent = result.intent
    now = now_local()

    # 1. 回答等待中的问题
    if intent == "answer_pending" and q is not None:
        when = parse_iso(result.answer_datetime)
        if when is None:
            await chat.finish(PARSE_AGAIN)
        if when < now - timedelta(minutes=5):
            await chat.finish("这个时间已经过去了诶, 说一个未来的时间吧(或「取消」跳过)")
        ev = await askq.resolve(q.id, when, text)
        if ev:
            await chat.finish(f"✅ 已写入日程:\n{events.render_event(ev)}")
        await chat.finish("该问题已失效。")

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

    # 4. 查询日程
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

    # 5. 其他: 不闲聊, 引导回功能
    await chat.finish(result.reply or HELP_TEXT)
