"""定时任务: 邮件轮询 / 每日 9:00 日报 / 每分钟提醒扫描(+询问过期)。

提醒用每分钟扫描 + reminded_at 幂等标记实现: 重启不漏, 不重发。
NapCat 每次连上时触发一轮邮件轮询并重发未送达的询问 —— 不用干等 30 分钟,
也避免 QQ 通道还没建立时通知/询问丢失。
"""

from datetime import datetime, timedelta

from nonebot import get_driver, require

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .. import askq, events, pipeline  # noqa: E402
from ..config import get_settings  # noqa: E402
from ..llm import chat_text  # noqa: E402
from ..notify import notify  # noqa: E402
from ..prompts import DIGEST_SYSTEM, REMIND_SYSTEM, digest_user, remind_user  # noqa: E402
from ..timetz import now_local  # noqa: E402

_settings = get_settings()

# 提醒附 AI 建议的日程类型
_TIP_TYPES = {"written_test", "ai_coding", "ai_interview", "interview", "assessment"}


@scheduler.scheduled_job(
    "interval",
    minutes=_settings.mail_poll_minutes,
    id="mail_poll",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)
async def job_mail_poll():
    await pipeline.poll_once()


@scheduler.scheduled_job("cron", hour=9, minute=0, id="daily_digest")
async def job_daily_digest():
    n = now_local()
    day_start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    evs = await events.events_between(day_start, day_start + timedelta(days=1))
    text = events.render_today(evs)
    if evs:
        # AI 晨间寄语(失败则降级为纯日程列表)
        evs_text = "\n".join(events.render_event(ev) for ev in evs)
        note = await chat_text(DIGEST_SYSTEM, digest_user(evs_text))
        if note:
            text += f"\n\n💡 {note}"
    await notify(text)


@scheduler.scheduled_job("interval", minutes=1, id="reminder_scan", max_instances=1, coalesce=True)
async def job_reminder_scan():
    for ev in await events.due_reminders():
        text = f"⏰ {ev.remind_before_minutes} 分钟后开始:\n{events.render_event(ev)}"
        if ev.event_type in _TIP_TYPES:
            tip = await chat_text(REMIND_SYSTEM, remind_user(events.render_event(ev)))
            if tip:
                text += f"\n💡 {tip}"
        if await notify(text):
            await events.mark_reminded(ev.id)
    await askq.expire_old()


@get_driver().on_bot_connect
async def _on_bot_connect(bot):
    # NapCat 连上 10 秒后跑一轮邮件轮询; 重发可能在掉线期间丢失的询问
    scheduler.add_job(
        pipeline.poll_once,
        "date",
        run_date=datetime.now() + timedelta(seconds=10),
        id="mail_poll_on_connect",
        replace_existing=True,
    )
    await askq.renotify_active()
