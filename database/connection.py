import logging
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from config import settings

logger = logging.getLogger("Database")
Base = declarative_base()

_engine = None
_sessionmaker = None


def get_engine():
    global _engine
    if _engine is None:
        if "sqlite" in settings.database_url:
            _engine = create_async_engine(
                settings.database_url,
                echo=False,
                future=True,
                connect_args={"check_same_thread": False}
            )
        else:
            _engine = create_async_engine(
                settings.database_url,
                echo=False,
                future=True,
                pool_pre_ping=True,
                pool_recycle=120,
                pool_timeout=15,
                pool_use_lifo=True,
                pool_size=10,
                max_overflow=20,
                connect_args={"connect_timeout": 5}
            )
    return _engine


def AsyncSessionLocal() -> AsyncSession:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False
        )
    return _sessionmaker()


from sqlalchemy import text

async def init_db(max_retries: int = 3):
    engine = get_engine()

    alter_queries = [
        "ALTER TABLE guild_settings ADD COLUMN tg_notification_chat_id BIGINT NULL;",
        "ALTER TABLE whitelist ADD COLUMN is_role TINYINT(1) NOT NULL DEFAULT 0;",
        "ALTER TABLE whitelist ADD COLUMN is_trusted TINYINT(1) NOT NULL DEFAULT 0;",
        "ALTER TABLE quarantine_users ADD COLUMN log_id VARCHAR(32) NULL;",
        "ALTER TABLE guild_backup_settings ADD COLUMN save_members TINYINT(1) NOT NULL DEFAULT 0;",
        "ALTER TABLE guild_settings ADD COLUMN notification_channel_id BIGINT NULL;",
        "ALTER TABLE guild_settings ADD COLUMN notif_anticrash_trigger TINYINT(1) NOT NULL DEFAULT 1;",
        "ALTER TABLE guild_settings ADD COLUMN notif_quarantine_release TINYINT(1) NOT NULL DEFAULT 1;",
        "ALTER TABLE guild_settings ADD COLUMN notif_quarantine_confirm TINYINT(1) NOT NULL DEFAULT 1;",
    ]

    for attempt in range(1, max_retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                for q in alter_queries:
                    try:
                        await conn.execute(text(q))
                    except Exception:
                        pass

            logger.info("Успешная инициализация базы данных и синхронизация колонок.")
            return

        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Критическая ошибка подключения к базе данных после {max_retries} попыток: {e}")
                return
            logger.warning(f"Ошибка подключения к базе данных (попытка {attempt}/{max_retries}): {e}. Повторный запрос через 1 сек...")
            await asyncio.sleep(1)
