"""高优先级命令: 今日 / 日程 / 帮助 / 取消。直接查库渲染, 不消耗 LLM。"""

from datetime import timedelta

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event, PrivateMessageEvent
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from .. import askq, events, history
from ..config import get_settings
from ..timetz import now_local

HELP_TEXT = """📮 求职邮件/日程小助手

我会每 30 分钟检查一次你的 QQ 邮箱:
· 求职/招聘相关邮件 -> 提取要点推送给你
· 笔试/测评/AI面试/面试等带时间的 -> 自动写入日程
· 时间不明确或窗口过长 -> 我会问你打算安排在什么时候(可稍后再答, 互不阻塞)
· 每天 9:00 推送当天日程; 日程开始前 30 分钟提醒

命令:
· 今日 / 今天 —— 查看今天的日程
· 日程 / 安排 —— 查看未来 7 天日程
· 待确认 —— 查看所有待你安排/确认的事
· 取消 —— 跳过某个待确认问题
· 帮助 —— 显示本说明

也可以直接和我说话, 如:
「9月20号有什么安排」「下周有哪些事」 —— 按日期/区间查日程
「明天下午3点字节跳动后端岗面试」 —— 加日程
「把字节的面试改到后天下午3点」 —— 改日程
「删除明天的笔试」 —— 删日程(会先让你确认)
「字节那个安排在周五下午2点」 —— 回答待确认"""


async def _is_master(event: Event) -> bool:
    return (
        isinstance(event, PrivateMessageEvent)
        and event.user_id == get_settings().master_qq
    )


async def _is_exact_cancel(event: Event) -> bool:
    # 「取消明天的笔试」应走删除意图, 只有单独的「取消」才取消待答问题
    return (
        isinstance(event, PrivateMessageEvent)
        and event.get_plaintext().strip() == "取消"
    )


MASTER = Rule(_is_master)

help_cmd = on_command("帮助", aliases={"help", "菜单"}, rule=MASTER, priority=1, block=True)
today_cmd = on_command("今日", aliases={"今天"}, rule=MASTER, priority=1, block=True)
week_cmd = on_command("日程", aliases={"安排", "本周"}, rule=MASTER, priority=1, block=True)
pending_cmd = on_command("待确认", aliases={"待定", "待安排"}, rule=MASTER, priority=1, block=True)
cancel_cmd = on_command("取消", rule=Rule(_is_master, _is_exact_cancel), priority=1, block=True)


async def _say(matcher: Matcher, text: str) -> None:
    """回复并记入对话历史。"""
    await history.record_bot(text)
    await matcher.finish(text)


@help_cmd.handle()
async def _():
    await _say(help_cmd, HELP_TEXT)


@today_cmd.handle()
async def _():
    n = now_local()
    day_start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    evs = await events.events_between(day_start, day_start + timedelta(days=1))
    await _say(today_cmd, events.render_today(evs))


@week_cmd.handle()
async def _():
    n = now_local()
    day_start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    evs = await events.events_between(day_start, day_start + timedelta(days=7))
    await _say(week_cmd, events.render_week(evs))


@pending_cmd.handle()
async def _():
    qs = await askq.list_open()
    if not qs:
        await _say(pending_cmd, "现在没有待确认的安排 🎉")
    await _say(
        pending_cmd,
        f"📝 有 {len(qs)} 个安排等你确认:\n"
        + await askq.render_pending(qs)
        + "\n回复如「字节那个安排在明天下午3点」, 或「取消第X个」。",
    )


@cancel_cmd.handle()
async def _():
    qs = await askq.list_open()
    if not qs:
        await _say(cancel_cmd, "当前没有等待确认的问题。")
    if len(qs) > 1:
        await _say(
            cancel_cmd,
            "有好几个待确认的, 要跳过哪一个?\n"
            + await askq.render_pending(qs)
            + "\n回复「取消第X个」或带上公司名~",
        )
    await askq.cancel(qs[0].id)
    await _say(cancel_cmd, f"好的, 已跳过「{await askq.display_title(qs[0])}」✅")
