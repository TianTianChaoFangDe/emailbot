"""RFC822 邮件原文 -> 结构化内容(主题/发件人/日期/纯文本正文)。"""

from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .timetz import now_local, to_naive_sh

MAX_TEXT_LEN = 4000  # 截断正文, 控制 LLM token 成本


@dataclass
class ParsedMail:
    message_id: str | None
    subject: str
    sender: str
    received_at: datetime  # naive 上海时间
    text: str


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    try:
        for chunk, charset in decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        return "".join(parts).strip()
    except Exception:
        return str(value).strip()


def _extract_text(msg) -> str:
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        content = BeautifulSoup(content, "lxml").get_text("\n")
    # 收敛空行, 控制长度
    lines = [ln.strip() for ln in content.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:MAX_TEXT_LEN]


def parse_mail(raw: bytes) -> ParsedMail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    received = now_local()
    try:
        if msg.get("Date"):
            received = to_naive_sh(parsedate_to_datetime(str(msg["Date"])))
    except Exception:
        pass
    return ParsedMail(
        message_id=(str(msg.get("Message-ID") or "").strip() or None),
        subject=_decode_header_value(str(msg.get("Subject", ""))),
        sender=_decode_header_value(str(msg.get("From", ""))),
        received_at=received,
        text=_extract_text(msg),
    )
