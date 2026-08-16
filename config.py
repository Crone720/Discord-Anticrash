from typing import Optional

from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    discord_token: SecretStr = Field(default=SecretStr(""), validation_alias="DISCORD_TOKEN",)
    telegram_token: Optional[SecretStr] = Field(default=None,validation_alias="TELEGRAM_TOKEN",)
    telegram_log_chat_id: Optional[int] = Field(default=None,validation_alias="TELEGRAM_LOG_CHAT_ID",)

    enable_telegram_bot: bool = Field(default=True,validation_alias="ENABLE_TELEGRAM_BOT",)
    developer_discord_id: Optional[int] = Field(default=None,validation_alias="DEVELOPER_DISCORD_ID",)
    database_url: str = Field(default="sqlite+aiosqlite:///database.db",validation_alias="DATABASE_URL",)

    github_token: Optional[SecretStr] = Field(default=None,validation_alias="GITHUB_TOKEN",)
    github_repo: Optional[str] = Field(default=None,validation_alias="GITHUB_REPO",)
    github_branch: str = Field(default="main",validation_alias="GITHUB_BRANCH",)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()