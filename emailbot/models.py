"""数据库模型(SQLModel)。

时间字段一律为 naive 的 Asia/Shanghai 本地时间, 见 timetz.py。
"""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ProcessedMail(SQLModel, table=True):
    """已处理邮件账本: 无论是否求职相关都写入, 用于去重。

    IMAP UID 仅在 (mailbox, uidvalidity) 内唯一且递增; 文件夹重建后
    uidvalidity 变化, 旧 UID 全部失效, 此时靠 message_id 二次去重。
    """

    __table_args__ = (UniqueConstraint("mailbox", "uidvalidity", "uid"),)

    id: int | None = Field(default=None, primary_key=True)
    mailbox: str = "INBOX"
    uidvalidity: int = 0
    uid: int
    message_id: str | None = Field(default=None, index=True)
    subject: str = ""
    sender: str = ""
    received_at: datetime | None = None
    processed_at: datetime
    is_job_related: bool = False
    llm_ok: bool = True          # LLM 解析失败为 False, 便于人工复查
    notified: bool = False
    retry_count: int = 0         # 预留: LLM 失败重试上限防毒循环


class ScheduleEvent(SQLModel, table=True):
    """日程事件。"""

    id: int | None = Field(default=None, primary_key=True)
    title: str
    company: str | None = None
    position: str | None = None
    event_type: str = "other"    # written_test/ai_coding/ai_interview/interview/assessment/other
    start_time: datetime = Field(index=True)
    end_time: datetime | None = None
    location_or_url: str | None = None
    notes: str | None = None
    source: str = "email"        # email / ask / manual
    source_mail_id: int | None = Field(default=None, foreign_key="processedmail.id")
    remind_before_minutes: int = 30
    reminded_at: datetime | None = None   # 幂等标记: 非空 = 已提醒
    status: str = "active"       # active / done / cancelled
    created_at: datetime


class PendingQuestion(SQLModel, table=True):
    """待回答询问(单 active 队列): 时间不确定/窗口过大时向用户发问。

    status 流转: queued -> pending(已发出) -> answered / expired / cancelled
    """

    id: int | None = Field(default=None, primary_key=True)
    status: str = Field(default="queued", index=True)
    question_text: str
    event_draft: str             # JSON: 缺确定时间的 ScheduleEvent 草稿字段
    source_mail_id: int | None = Field(default=None, foreign_key="processedmail.id")
    created_at: datetime
    asked_at: datetime | None = None
    answered_at: datetime | None = None
    answer_text: str | None = None
