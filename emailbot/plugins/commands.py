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
· 时间不明确或窗口过长 -> 我会问你打算安排在什么时候
· 每天 9:00 推送当天日程; 日程开始前 30 分钟提醒

命令:
· 今日 / 今天 —— 查看今天的日程
· 日程 / 安排 —— 查看未来 7 天日程
· 取消 —— 跳过当前正在询问你的问题
· 帮助 —— 显示本说明

也可以直接和我说话, 如:
「明天下午3点字节跳动后端岗面试」 —— 加日程
「把字节的面试改到后天下午3点」 —— 改日程
「删除明天的笔试」 —— 删日程(会先让你确认)"""


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


@cancel_cmd.handle()
async def _():
    q = await askq.cancel_active()
    if q:
        await _say(cancel_cmd, "好的, 已跳过这个问题 ✅")
    else:
        await _say(cancel_cmd, "当前没有等待回答的问题。")
