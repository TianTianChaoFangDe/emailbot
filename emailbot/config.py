"""业务配置: 直接读取 .env(与 nonebot 读同一个文件, 互不干扰, 大小写不敏感)。"""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 主号 QQ: 通知发给谁 / 只响应谁的私聊
    master_qq: int = 0

    # QQ 邮箱 IMAP
    qq_email: str = ""
    qq_email_auth_code: SecretStr = SecretStr("")
    imap_host: str = "imap.qq.com"
    imap_port: int = 993

    # DeepSeek
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    database_url: str = "sqlite+aiosqlite:///data/emailbot.db"

    # 业务参数
    mail_poll_minutes: int = 30
    ask_window_hours: int = 6
    remind_before_minutes: int = 30
    pending_expire_hours: int = 48
    first_lookback_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings()
