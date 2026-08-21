"""时区助手: 全库统一存 naive 的 Asia/Shanghai 本地时间(中国无夏令时, 安全)。

入库前一律过 to_naive_sh(); 展示直接 strftime; 给 LLM 的"当前时间"用 now_prompt()。
"""

from datetime import datetime, timedelta, timezone

SH = timezone(timedelta(hours=8))

_WEEKDAYS = "一二三四五六日"


def now_local() -> datetime:
    """当前 Asia/Shanghai 时间(naive, 全库统一约定)。"""
    return datetime.now(SH).replace(tzinfo=None)


def to_naive_sh(dt: datetime) -> datetime:
    """aware -> 转上海时区并去 tzinfo; naive -> 视为已是上海本地时间, 原样返回。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(SH).replace(tzinfo=None)


def parse_iso(s: str | None) -> datetime | None:
    """解析 LLM 返回的 ISO 8601 字符串为 naive 上海时间; 失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_naive_sh(dt)


def now_prompt() -> str:
    """注入 prompt 的当前时间描述, 如 '2026-08-22 15:30 星期六 (Asia/Shanghai)'。"""
    n = datetime.now(SH)
    return f"{n:%Y-%m-%d %H:%M} 星期{_WEEKDAYS[n.weekday()]} (Asia/Shanghai)"


def fmt(dt: datetime | None) -> str:
    """展示用: '08-25 14:00'。"""
    return dt.strftime("%m-%d %H:%M") if dt else "?"


def fmt_day(dt: datetime) -> str:
    return f"{dt.month}月{dt.day}日 周{_WEEKDAYS[dt.weekday()]}"
