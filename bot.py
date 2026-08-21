"""emailbot 入口: python bot.py

启动顺序要求: 先启动本程序(监听 8080), 再在 NapCat 中启用 WebSocket 客户端连接
ws://127.0.0.1:8080/onebot/v11/ws
"""

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

from emailbot.db import init_db  # noqa: E402  (须在 nonebot.init 之后)

driver.on_startup(init_db)

nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
