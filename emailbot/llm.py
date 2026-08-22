"""DeepSeek (OpenAI 兼容) JSON 调用封装。

注意: DeepSeek JSON mode 要求 prompt 中必须出现 "JSON" 字样,
否则模型可能持续输出空白直到 token 上限。两个 prompt 均已满足。
"""

import json
from typing import TypeVar

from nonebot.log import logger
from openai import AsyncOpenAI
from pydantic import BaseModel

from .config import get_settings

T = TypeVar("T", bound=BaseModel)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncOpenAI(
            api_key=s.deepseek_api_key.get_secret_value(),
            base_url=s.deepseek_base_url,
        )
    return _client


async def chat_text(system: str, user: str, temperature: float = 0.8) -> str | None:
    """自由文本生成(日报寄语/提醒建议等); 失败返回 None, 调用方降级。"""
    settings = get_settings()
    try:
        resp = await get_client().chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        logger.warning(f"LLM 文本生成失败: {e!r}")
        return None


def _loads_loose(text: str) -> dict | None:
    """严格解析失败时, 截取首个 { 到末个 } 再试一次。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(text[i : j + 1])
            except json.JSONDecodeError:
                return None
        return None


async def chat_json(
    system: str,
    user: str,
    model: type[T],
    history: list[dict] | None = None,
) -> T | None:
    """调用 DeepSeek JSON mode 并用 Pydantic 校验; 失败重试一次, 仍失败返回 None。

    history: 之前的对话消息([{"role": ..., "content": ...}]), 插在 system 与
    当前 user 消息之间。
    """
    settings = get_settings()
    messages = [{"role": "system", "content": system}]
    if history:
        messages += history
    messages.append({"role": "user", "content": user})
    for attempt in range(2):
        try:
            resp = await get_client().chat.completions.create(
                model=settings.deepseek_model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            text = resp.choices[0].message.content or ""
            data = _loads_loose(text)
            if data is None:
                logger.warning(f"LLM 返回非 JSON (第{attempt + 1}次): {text[:200]!r}")
                continue
            return model.model_validate(data)
        except Exception as e:
            logger.warning(f"LLM 调用/校验失败 (第{attempt + 1}次): {e!r}")
    return None
