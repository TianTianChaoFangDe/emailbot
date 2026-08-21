"""IMAP 抓取(同步阻塞, 调用方必须 asyncio.to_thread)。

发现策略: 按 UID 水位线增量抓取(SEARCH UID watermark+1:*), 而不是 UNSEEN ——
用户在别的客户端读了邮件也不漏。PEEK 抓取, 不改变邮箱已读状态。
首次运行/UIDVALIDITY 变化时水位为 0, 用 SINCE 限定只回看最近 N 天。
"""

from dataclasses import dataclass
from datetime import date, timedelta

import imapclient


@dataclass
class RawMail:
    uid: int
    uidvalidity: int
    raw: bytes  # RFC822 原文


def fetch_new_mails(
    host: str,
    port: int,
    email: str,
    auth_code: str,
    watermark: int,
    lookback_days: int,
    mailbox: str = "INBOX",
) -> tuple[int, list[RawMail]]:
    """返回 (uidvalidity, 新邮件列表)。watermark=0 时只取最近 lookback_days 天。"""
    with imapclient.IMAPClient(host, port=port, ssl=True) as client:
        client.login(email, auth_code)
        status = client.select_folder(mailbox)
        uidvalidity = int(status[b"UIDVALIDITY"])

        criteria: list = ["UID", f"{watermark + 1}:*"]
        if watermark == 0:
            criteria += ["SINCE", date.today() - timedelta(days=lookback_days)]
        uids = [u for u in client.search(criteria) if u > watermark]
        # IMAP 怪癖: "UID n:*" 在 n 超过最大 UID 时仍返回最后一封, 上面已过滤

        mails: list[RawMail] = []
        if uids:
            for uid, data in client.fetch(uids, ["BODY.PEEK[]"]).items():
                raw = data.get(b"BODY[]")
                if raw:
                    mails.append(RawMail(uid=uid, uidvalidity=uidvalidity, raw=raw))
        return uidvalidity, mails
