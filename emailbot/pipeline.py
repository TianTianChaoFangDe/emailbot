"""邮件轮询主流水线: 抓取 -> 去重 -> LLM 分类 -> 记账 -> 通知 -> 建程/入队询问。"""

import asyncio
from datetime import timedelta

from nonebot.log import logger
from sqlmodel import func, select

from . import askq, events, mailfetch, mailparse
from .config import get_settings
from .db import SessionFactory
from .llm import chat_json
from .models import ProcessedMail
from .notify import notify
from .prompts import (
    CATEGORY_LABELS,
    CLASSIFY_SYSTEM,
    ClassifyEvent,
    ClassifyResult,
    classify_user,
)
from .timetz import fmt, now_local, parse_iso

MAILBOX = "INBOX"


async def _stored_watermark() -> tuple[int, int]:
    """库中记录的 (最新 uidvalidity, 其下最大 uid)。无记录 -> (0, 0)。"""
    async with SessionFactory() as s:
        uv = (
            await s.exec(
                select(func.max(ProcessedMail.uidvalidity)).where(
                    ProcessedMail.mailbox == MAILBOX
                )
            )
        ).one()
        if uv is None:
            return 0, 0
        w = (
            await s.exec(
                select(func.max(ProcessedMail.uid)).where(
                    ProcessedMail.mailbox == MAILBOX,
                    ProcessedMail.uidvalidity == uv,
                )
            )
        ).one()
        return uv, w or 0


async def _find_duplicate(
    uid: int, uidvalidity: int, message_id: str | None
) -> ProcessedMail | None:
    async with SessionFactory() as s:
        exact = (
            await s.exec(
                select(ProcessedMail).where(
                    ProcessedMail.mailbox == MAILBOX,
                    ProcessedMail.uidvalidity == uidvalidity,
                    ProcessedMail.uid == uid,
                )
            )
        ).first()
        if exact:
            return exact
        if message_id:
            return (
                await s.exec(
                    select(ProcessedMail).where(ProcessedMail.message_id == message_id)
                )
            ).first()
    return None


def _needs_ask(e: ClassifyEvent, start, end) -> bool:
    """服务端判定(不盲信 LLM 标签): 无明确开始时间, 或窗口超过阈值 -> 询问。"""
    if start is None:
        return True
    if end and (end - start) > timedelta(hours=get_settings().ask_window_hours):
        return True
    return False


def _build_notification(
    result: ClassifyResult, added: list, asked: list[str]
) -> str:
    label = CATEGORY_LABELS.get(result.category, "求职相关")
    lines = [f"📧 求职邮件 | {label}"]
    if result.company:
        lines.append(
            f"🏢 {result.company}" + (f" · {result.position}" if result.position else "")
        )
    if result.summary:
        lines.append(f"💬 {result.summary}")
    if result.action_required:
        lines.append(f"📌 {result.action_required}")
    for ev in added:
        lines.append(f"🗓 已写入日程: {ev.title} @ {fmt(ev.start_time)}")
    for t in asked:
        lines.append(f"❓ 时间待定: {t}(稍后会问你安排)")
    return "\n".join(lines)


async def _handle_events(result: ClassifyResult, mail_id: int) -> tuple[list, list[str]]:
    """返回 (已建日程列表, 已入队询问的标题列表)。"""
    settings = get_settings()
    now = now_local()
    added, asked = [], []
    for e in result.events:
        start, end = parse_iso(e.start), parse_iso(e.end)
        if start and end and end < start:
            end = None
        if start and start < now - timedelta(hours=1):
            logger.info(f"跳过已过期事件: {e.title} @ {start}")
            continue
        if _needs_ask(e, start, end):
            window = (
                f"邮件给的时间窗口是 {fmt(start)} ~ {fmt(end)}"
                if start and end
                else "邮件没有给出明确时间"
            )
            draft = {
                "title": e.title,
                "event_type": e.type,
                "company": result.company,
                "position": result.position,
                "location_or_url": e.location_or_url,
                "duration_minutes": e.duration_minutes,
                "notes": window,
            }
            question = (
                f"❓ 收到「{e.title}」"
                f"({result.company or '未知公司'}), {window}。\n"
                f"你想把它安排在什么时候?"
            )
            await askq.enqueue(question, draft, source_mail_id=mail_id)
            asked.append(e.title)
        else:
            ev = await events.add_event(
                title=e.title,
                start_time=start,
                end_time=end,
                company=result.company,
                position=result.position,
                event_type=e.type,
                location_or_url=e.location_or_url,
                notes=f"时长约{e.duration_minutes}分钟" if e.duration_minutes else None,
                source="email",
                source_mail_id=mail_id,
            )
            added.append(ev)
    return added, asked


async def _process_mail(rm: mailfetch.RawMail) -> None:
    parsed = mailparse.parse_mail(rm.raw)
    dup = await _find_duplicate(rm.uid, rm.uidvalidity, parsed.message_id)
    if dup and dup.uid == rm.uid and dup.uidvalidity == rm.uidvalidity:
        return  # 同 UID 已记账, 跳过
    if dup:
        # message_id 命中(UIDVALIDITY 重置后的重复): 记账推进水位, 不再通知
        async with SessionFactory() as s:
            s.add(
                ProcessedMail(
                    uidvalidity=rm.uidvalidity,
                    uid=rm.uid,
                    message_id=parsed.message_id,
                    subject=parsed.subject,
                    sender=parsed.sender,
                    received_at=parsed.received_at,
                    processed_at=now_local(),
                    is_job_related=dup.is_job_related,
                    llm_ok=True,
                    notified=True,
                )
            )
            await s.commit()
        return

    result = await chat_json(
        CLASSIFY_SYSTEM,
        classify_user(parsed.sender, parsed.subject, str(parsed.received_at), parsed.text),
        ClassifyResult,
    )
    llm_ok = result is not None
    if not llm_ok:
        logger.warning(f"邮件分类失败, 按无关处理: uid={rm.uid} subject={parsed.subject!r}")
    related = bool(result and result.is_job_related)

    async with SessionFactory() as s:
        rec = ProcessedMail(
            uidvalidity=rm.uidvalidity,
            uid=rm.uid,
            message_id=parsed.message_id,
            subject=parsed.subject,
            sender=parsed.sender,
            received_at=parsed.received_at,
            processed_at=now_local(),
            is_job_related=related,
            llm_ok=llm_ok,
        )
        s.add(rec)
        await s.commit()
        await s.refresh(rec)
        rec_id = rec.id

    if not related or result is None:
        return

    added, asked = await _handle_events(result, rec_id)
    ok = await notify(_build_notification(result, added, asked))
    if ok:
        async with SessionFactory() as s:
            rec = await s.get(ProcessedMail, rec_id)
            if rec:
                rec.notified = True
                s.add(rec)
                await s.commit()


async def poll_once() -> None:
    settings = get_settings()
    if not settings.qq_email or not settings.qq_email_auth_code.get_secret_value():
        logger.warning("未配置 QQ_EMAIL/QQ_EMAIL_AUTH_CODE, 跳过本轮邮件轮询")
        return
    try:
        uv_stored, watermark = await _stored_watermark()
        uv, mails = await asyncio.to_thread(
            mailfetch.fetch_new_mails,
            settings.imap_host,
            settings.imap_port,
            settings.qq_email,
            settings.qq_email_auth_code.get_secret_value(),
            watermark,
            settings.first_lookback_days,
        )
        if uv_stored and uv != uv_stored:
            # 文件夹被重建, 旧 UID 全部失效: 重置水位重抓(message_id 兜底防重复通知)
            logger.warning(f"UIDVALIDITY 变化 {uv_stored} -> {uv}, 重置水位")
            uv, mails = await asyncio.to_thread(
                mailfetch.fetch_new_mails,
                settings.imap_host,
                settings.imap_port,
                settings.qq_email,
                settings.qq_email_auth_code.get_secret_value(),
                0,
                settings.first_lookback_days,
            )
    except Exception as e:
        logger.warning(f"IMAP 抓取失败: {e!r}")
        return

    if not mails:
        logger.info("本轮无新邮件")
        return
    logger.info(f"发现 {len(mails)} 封新邮件, 开始处理")
    for rm in mails:
        try:
            await _process_mail(rm)
        except Exception as e:
            logger.warning(f"处理邮件失败 uid={rm.uid}: {e!r}")  # 单封失败不影响批次
