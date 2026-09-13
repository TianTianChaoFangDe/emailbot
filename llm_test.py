"""LLM 意图路由联调: 真实调 DeepSeek 验证新 prompt(多待确认 + 区间查询)。

运行: .venv/Scripts/python.exe llm_test.py
"""

import asyncio

from emailbot.llm import chat_json
from emailbot.prompts import INTENT_SYSTEM, IntentResult, intent_user

PENDING = """1. 「字节跳动后端岗笔试」(字节跳动) —— 邮件没有给出明确时间
2. 「腾讯前端岗测评」(腾讯) —— 邮件给的时间窗口是 09-15 10:00 ~ 09-17 22:00
3. [待改期] 「阿里实习面试」"""

EVENTS = """1. 09-14 10:00 【笔试】美团测开笔试 (美团)
2. 09-15 14:00 【面试】Shopee 后端一面 (Shopee)"""

CASES = [
    ("字节那个安排在明天下午3点", "answer_pending", lambda r: r.pending_index == 1 and r.answer_datetime),
    ("第二个不用安排了", "cancel_pending", lambda r: r.pending_index == 2),
    ("9月20号有什么安排", "query_schedule", lambda r: r.query_start and "09-20" in r.query_start and r.query_end and "09-21" in r.query_end),
    ("下周有什么", "query_schedule", lambda r: r.query_start and r.query_end),
    ("9月15到20号有哪些事", "query_schedule", lambda r: r.query_start and "09-15" in r.query_start and r.query_end and "09-21" in r.query_end),
    ("有什么待确认的", "query_pending", lambda r: True),
    ("明天下午3点", "answer_pending", lambda r: r.answer_datetime is not None),  # 多条时 pending_index 可为空, 由 bot 反问
    ("后天上午10点华为OD机试", "add_schedule", lambda r: r.event and r.event.start),
]


async def main() -> None:
    fails = 0
    for text, want_intent, check in CASES:
        r = await chat_json(INTENT_SYSTEM, intent_user(text, PENDING, EVENTS, None), IntentResult)
        ok = r is not None and r.intent == want_intent and check(r)
        fails += not ok
        tag = "OK " if ok else "FAIL"
        print(f"[{tag}] {text!r} -> intent={r and r.intent} pi={r and r.pending_index} "
              f"qs={r and r.query_start} qe={r and r.query_end} ad={r and r.answer_datetime} "
              f"ev={r and r.event and r.event.start}")
    print("LLM_TEST_DONE", "ALL_PASS" if fails == 0 else f"{fails} FAIL")


asyncio.run(main())
