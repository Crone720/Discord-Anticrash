import re
import asyncio
import logging
import disnake
from typing import Dict, Optional
from database.connection import AsyncSessionLocal
from database import crud

logger = logging.getLogger("BackupScheduler")


def parse_interval_to_seconds(interval_str: str) -> int:
    interval_str = interval_str.strip().lower()
    if not interval_str or interval_str == "0" or interval_str in ("откл", "off", "disabled"):
        return 0

    match = re.match(r"^(\d+)\s*([a-zа-я]+)?$", interval_str)
    if not match:
        return 0

    val = int(match.group(1))
    unit = match.group(2) or "m"

    if unit in ("m", "min", "мин", "минута", "минуты", "минут"):
        return val * 60
    elif unit in ("h", "hr", "ч", "час", "часа", "часов"):
        return val * 3600
    elif unit in ("d", "д", "день", "дня", "дней"):
        return val * 86400
    elif unit in ("w", "н", "нед", "неделя", "недели"):
        return val * 7 * 86400
    return 0


class BackupScheduler:
    def __init__(self):
        self.bot = None
        self._tasks: Dict[int, asyncio.Task] = {}

    def init_bot(self, bot):
        self.bot = bot

    async def start_all(self):
        """Запускает циклы авто-бэкапа для всех серверов, у которых это настроено в БД."""
        if not self.bot:
            return

        async with AsyncSessionLocal() as session:
            settings_list = await crud.get_all_active_auto_backups(session)

        for setting in settings_list:
            if setting.auto_backup_interval != "0":
                guild = self.bot.get_guild(setting.guild_id)
                if guild:
                    self.reset_timer(setting.guild_id, guild)

    def cancel_timer(self, guild_id: int):
        task = self._tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    def reset_timer(self, guild_id: int, guild: Optional[disnake.Guild] = None):
        """Отменяет текущий таймер для сервера и запускает новый отсчёт."""
        self.cancel_timer(guild_id)

        if not self.bot:
            return

        target_guild = guild or self.bot.get_guild(guild_id)
        if not target_guild:
            return

        async def _timer_task():
            try:
                while True:
                    async with AsyncSessionLocal() as session:
                        setting = await crud.get_or_create_backup_settings(session, guild_id)
                        interval_str = setting.auto_backup_interval

                    seconds = parse_interval_to_seconds(interval_str)
                    if seconds <= 0:
                        break

                    logger.info(f"Таймер авто-бэкапа для сервера {guild_id} установлен на {seconds} сек. ({interval_str})")
                    await asyncio.sleep(seconds)

                    from services.guild_operation_manager import guild_op_manager, IDLE
                    if guild_op_manager.get_state(guild_id) != IDLE:
                        logger.info(f"Сервер {guild_id} занят (state={guild_op_manager.get_state(guild_id)}). Авто-бэкап пропущен.")
                        continue

                    # Импорт внутри задачи — чтобы избежать циклических зависимостей
                    from services.backup_service import execute_server_backup
                    logger.info(f"Запуск автоматического бэкапа по таймеру для сервера {guild_id}...")
                    success, res = await execute_server_backup(target_guild, backup_type="auto")
                    if success:
                        logger.info(f"Авто-бэкап сервера {guild_id} успешно выполнен: {res.get('backup_name')}")
                    else:
                        logger.error(f"Ошибка авто-бэкапа сервера {guild_id}: {res.get('error')}")

            except asyncio.CancelledError:
                logger.info(f"Таймер авто-бэкапа для сервера {guild_id} отменен.")
            except Exception as e:
                logger.error(f"Исключение в цикле авто-бэкапа сервера {guild_id}: {e}", exc_info=True)

        task = asyncio.create_task(_timer_task())
        self._tasks[guild_id] = task


backup_scheduler = BackupScheduler()
