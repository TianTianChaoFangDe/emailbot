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


async def chat_json(system: str, user: str, model: type[T]) -> T | None:
    """调用 DeepSeek JSON mode 并用 Pydantic 校验; 失败重试一次, 仍失败返回 None。"""
    settings = get_settings()
    for attempt in range(2):
        try:
            resp = await get_client().chat.completions.create(
                model=settings.deepseek_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
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
