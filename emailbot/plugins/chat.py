"""私聊总入口(低优先级): LLM 意图路由。

只响应主号私聊。命令已在高优先级拦截, 走到这里的都是自然语言。
修改/删除日程的目标定位: 当前日程编号列表注入 prompt, 由 LLM 返回 target_index。
待确认问题的定位: 待确认列表(编号)注入 prompt, 由 LLM 返回 pending_index;
多条共存、互不阻塞, 用户可任意顺序回答。
上下文: 最近 20 条对话历史注入 LLM; 用户引用回复的消息原文也会带进 prompt。
引用绑定(服务器端, 比 LLM 猜测可靠): 引用命中待确认问题 -> 视为在回答它;
引用命中某条日程 -> 锁定为修改/删除目标; 引用日程并给出时间 -> 按改期处理。
"""

from datetime import datetime, timedelta

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event, Message, PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from .. import askq, events, history
from ..config import get_settings
from ..llm import chat_json
from ..models import PendingQuestion, ScheduleEvent
from ..prompts import INTENT_SYSTEM, IntentResult, intent_user
from ..timetz import fmt, fmt_day, now_local, parse_iso
from .commands import HELP_TEXT


async def _is_master_private(event: Event) -> bool:
    return (
        isinstance(event, PrivateMessageEvent)
        and event.user_id == get_settings().master_qq
    )


chat = on_message(rule=Rule(_is_master_private), priority=50, block=True)

NOT_FOUND = "没找到你说的那条日程 🤔 可以发「日程」看看现有安排, 再说具体一点(如公司名+类型)"
QUERY_DAYS_CAP = 62  # 查询区间上限, 防止 LLM 给出离谱范围刷屏


async def _say(text: str) -> None:
    """回复用户并记入对话历史。finish 会抛异常结束处理, 之后代码不可达。"""
    await history.record_bot(text)
    await chat.finish(text)


def _flavor(result: IntentResult) -> str:
    """LLM 给动作类意图附带的情绪/提醒文案(reply 字段)。"""
    r = (result.reply or "").strip()
    return f"\n{r}" if r else ""


async def _conflict_warn(ev: ScheduleEvent) -> str:
    return events.conflict_note(
        await events.find_conflicts(ev.start_time, ev.end_time, exclude_id=ev.id)
    )


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
            text = Message(msg.get("message")).extract_plain_text().strip() or None
            logger.info(f"引用消息 id={mid} -> {text[:60]!r}" if text else f"引用消息 id={mid} 内容为空")
            return text
        except Exception as e:
            logger.warning(f"获取引用消息失败 id={mid}: {e!r}")
            return None
    return None


def _pick(index: int | None, events_list: list[ScheduleEvent]) -> ScheduleEvent | None:
    """LLM 返回的 1 起始编号 -> 日程对象。"""
    if index is not None and 1 <= index <= len(events_list):
        return events_list[index - 1]
    return None


def _pick_q(
    index: int | None,
    open_qs: list[PendingQuestion],
    quote_bind_q: PendingQuestion | None,
) -> PendingQuestion | None:
    """定位用户所指的待确认问题: 引用绑定 > LLM 编号 > 唯一待确认。"""
    if quote_bind_q is not None:
        return quote_bind_q
    if index is not None and 1 <= index <= len(open_qs):
        return open_qs[index - 1]
    if len(open_qs) == 1:
        return open_qs[0]
    return None


async def _say_which(open_qs: list[PendingQuestion]) -> None:
    """多条待确认且无法定位时, 列出清单反问。"""
    await _say(
        "你现在有好几个待确认的安排, 说的是哪一个呀?\n"
        + await askq.render_pending(open_qs)
        + "\n带上公司名或「第X个」告诉我~"
    )


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


def _render_query(start: datetime, end: datetime, evs: list[ScheduleEvent]) -> str:
    """任意日期/区间的查询结果渲染([start, end), 均为当天 0 点)。"""
    day_start = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    if end - start <= timedelta(days=1):
        if start == day_start:
            return events.render_today(evs)
        label = fmt_day(start)
        title = (
            f"📅 明日日程 ({label})"
            if start == day_start + timedelta(days=1)
            else f"📅 {label} 日程"
        )
        return events.render_events(title, evs, f"{label}没有日程安排 🎉")
    return events.render_events(
        f"📅 {fmt_day(start)} ~ {fmt_day(end - timedelta(days=1))} 日程",
        evs,
        "这段时间没有日程安排 🎉",
    )


@chat.handle()
async def _(event: PrivateMessageEvent, bot: Bot):
    text = event.get_plaintext().strip()
    if not text:
        return

    quote = await _get_quoted_text(event, bot)
    history_msgs = await history.as_messages()
    await history.record_user(f"{text}(引用:「{quote[:80]}」)" if quote else text)

    open_qs = await askq.list_open()
    upcoming = await events.upcoming_events()

    # 服务器端引用绑定
    quote_bind_q = await askq.find_by_quote(quote) if quote else None
    quote_bind_ev = (
        events.match_event_by_text(quote, upcoming)
        if quote and quote_bind_q is None
        else None
    )
    quote_for_prompt = quote
    if quote_bind_q is not None:
        quote_for_prompt += "(系统提示: 该引用对应一个等待确认的问题, 用户大概率在回答它)"
    elif quote_bind_ev is not None:
        quote_for_prompt += f"(系统提示: 该引用疑似对应日程「{quote_bind_ev.title}」)"

    result = await chat_json(
        INTENT_SYSTEM,
        intent_user(
            text,
            await askq.render_pending(open_qs) if open_qs else None,
            _events_text(upcoming),
            quote_for_prompt,
        ),
        IntentResult,
        history=history_msgs,
    )
    if result is None:
        await _say("我暂时没听懂 🤯 可以发「帮助」查看用法。")

    intent = result.intent
    now = now_local()

    # 目标绑定优先级: 引用字符串匹配 > LLM 对引用的判断 > LLM 对消息文本的判断
    bound_ev = quote_bind_ev or _pick(result.quote_target_index, upcoming)

    # 引用命中待确认问题时, 除非用户在取消/查询/添加完整新日程, 一律视为在回答它
    force_answer = (
        quote_bind_q is not None
        and intent not in ("cancel_pending", "query_schedule", "query_pending")
        and not (
            intent == "add_schedule"
            and result.event
            and parse_iso(result.event.start)
        )
    )

    # 1. 回答待确认问题(按问题的 action 分发)
    if intent == "answer_pending" or force_answer:
        target_q = _pick_q(result.pending_index, open_qs, quote_bind_q)
        if target_q is None:
            if not open_qs:
                await _say("当前没有待确认的问题 🤔")
            await _say_which(open_qs)

        if askq.action_of(target_q) == "delete":  # 确认类问题
            if result.confirm is None:
                await _say("请回复「确认」或「取消」")
            done, ev = await askq.resolve_confirm(target_q.id, result.confirm, text)
            if not done:
                await _say("该问题已失效。")
            if result.confirm:
                if ev is None:  # 重复确认(如重发后补答)目标已被删过
                    await _say("这条日程已经不在了(可能之前已删除) ✅")
                await _say(f"🗑 已删除日程:\n{events.render_event(ev)}{_flavor(result)}")
            await _say("好的, 不删了 ✅")

        # create / update_time 时间问题
        when = parse_iso(result.answer_datetime)
        if when is None:
            await _say(
                f"想把「{await askq.display_title(target_q)}」安排在什么时候?"
                " 如「明天下午3点」(或「取消」跳过)"
            )
        if when < now - timedelta(minutes=5):
            await _say("这个时间已经过去了诶, 说一个未来的时间吧(或「取消」跳过)")
        ev, action = await askq.resolve(target_q.id, when, text)
        if ev is None:
            await _say("该问题已失效。")
        warn = await _conflict_warn(ev)
        if action == "update_time":
            await _say(f"✅ 已改期:\n{events.render_event(ev)}{warn}{_flavor(result)}")
        await _say(f"✅ 已写入日程:\n{events.render_event(ev)}{warn}{_flavor(result)}")

    # 2. 取消待确认问题
    if intent == "cancel_pending":
        if not open_qs:
            await _say("当前没有等待确认的问题。")
        if "全部" in text or "所有" in text:
            for q in open_qs:
                await askq.cancel(q.id)
            await _say(f"好的, {len(open_qs)} 个待确认都跳过了 ✅")
        target_q = _pick_q(result.pending_index, open_qs, quote_bind_q)
        if target_q is None:
            await _say(
                "有好几个待确认的, 要跳过哪一个?\n"
                + await askq.render_pending(open_qs)
                + "\n回复「取消第X个」或带上公司名~"
            )
        await askq.cancel(target_q.id)
        await _say(f"好的, 已跳过「{await askq.display_title(target_q)}」✅")

    # 3. 添加日程(若引用绑定了已有日程且给了时间, 实际是想改期)
    if intent == "add_schedule" and result.event:
        ie = result.event
        start, end = parse_iso(ie.start), parse_iso(ie.end)
        if bound_ev is not None and start is not None:
            if start < now - timedelta(minutes=5):
                await _say("这个时间已经过去了诶, 说一个未来的时间吧")
            ev = await events.update_event(bound_ev.id, start_time=start, end_time=end)
            warn = await _conflict_warn(ev)
            await _say(f"✅ 已改期:\n{events.render_event(ev)}{warn}{_flavor(result)}")
        if start is None:
            # 先记下来, 问用户时间(问题会立即发出, 与其他待确认互不阻塞)
            await askq.enqueue(
                f"📝 已记下「{ie.title}」, 你想安排在什么时候?",
                {
                    "title": ie.title,
                    "event_type": ie.event_type,
                    "location_or_url": ie.location_or_url,
                    "notes": ie.notes,
                },
            )
            return  # 问题已由 enqueue 发出(notify 内会记历史)
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
        warn = await _conflict_warn(ev)
        await _say(f"✅ 已写入日程:\n{events.render_event(ev)}{warn}{_flavor(result)}")

    # 4. 修改日程
    if intent == "update_schedule":
        target = bound_ev or _pick(result.target_index, upcoming)
        if target is None:
            await _say(NOT_FOUND)
        new_start = parse_iso(result.event.start) if result.event else None
        new_end = parse_iso(result.event.end) if result.event else None
        if new_start is None:
            # 用户没说改到什么时候 -> 入列询问
            await askq.enqueue(
                f"📝 想把「{target.title}」(原定 {fmt(target.start_time)})改到什么时候?",
                {"action": "update_time", "event_id": target.id, "title": target.title},
            )
            return
        if new_start < now - timedelta(minutes=5):
            await _say("这个时间已经过去了诶, 说一个未来的时间吧")
        ev = await events.update_event(target.id, start_time=new_start, end_time=new_end)
        if ev is None:
            await _say("该日程已失效。")
        warn = await _conflict_warn(ev)
        await _say(f"✅ 已改期:\n{events.render_event(ev)}{warn}{_flavor(result)}")

    # 5. 删除日程(先确认)
    if intent == "delete_schedule":
        target = bound_ev or _pick(result.target_index, upcoming)
        if target is None:
            await _say(NOT_FOUND)
        await askq.enqueue(
            "🗑 确认删除这条日程吗?\n"
            f"{events.render_event(target)}\n"
            "回复「确认」删除, 回复「取消」保留。",
            {"action": "delete", "event_id": target.id, "title": target.title},
        )
        return

    # 6. 查询日程(任意日期/区间)
    if intent == "query_schedule":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = parse_iso(result.query_start) or day_start
        end = parse_iso(result.query_end) or (start + timedelta(days=1))
        if end <= start:
            end = start + timedelta(days=1)
        capped = end - start > timedelta(days=QUERY_DAYS_CAP)
        if capped:
            end = start + timedelta(days=QUERY_DAYS_CAP)
        evs = await events.events_between(start, end)
        text = _render_query(start, end, evs)
        if capped:
            text += f"\n(范围太大啦, 先只展示前 {QUERY_DAYS_CAP} 天)"
        await _say(text)

    # 7. 查询待确认列表
    if intent == "query_pending":
        if not open_qs:
            await _say("现在没有待确认的安排 🎉")
        await _say(
            f"📝 有 {len(open_qs)} 个安排等你确认:\n"
            + await askq.render_pending(open_qs)
            + "\n回复如「字节那个安排在明天下午3点」, 或「取消第X个」。"
        )

    # 8. 其他: 不闲聊, 引导回功能
    await _say(result.reply or HELP_TEXT)
