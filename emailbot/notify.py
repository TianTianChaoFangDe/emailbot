"""QQ 私聊通知封装: 所有定时任务/流水线发消息统一走这里, 掉线不炸任务。"""

from nonebot import get_bot
from nonebot.log import logger

from . import history
from .config import get_settings


async def notify(text: str) -> bool:
    """给主号发私聊消息; 失败(如 NapCat 掉线)记日志并返回 False。
    发送成功的消息会记入对话历史。"""
    try:
        bot = get_bot()
        await bot.send_private_msg(user_id=get_settings().master_qq, message=text)
    except Exception as e:
        logger.warning(f"发送 QQ 私聊失败: {e!r}")
        return False
    await history.record_bot(text)
    return True
