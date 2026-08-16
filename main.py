import logging
import sys
import asyncio
import disnake
from disnake.ext import commands
from config import settings
from database.connection import init_db
from helpers.logger import setup_logger

setup_logger()
logger = logging.getLogger("Main")

intents = disnake.Intents.default()
intents.members = True
intents.guilds = True
intents.moderation = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    test_guilds=[1198697363028574368]
)


from services.backup_scheduler import backup_scheduler


@bot.event
async def on_ready():
    logger.info(f"Бот Антикраш успешно запущен как {bot.user} (ID: {bot.user.id})")
    logger.info(f"Подключен к {len(bot.guilds)} серверам.")

    async def async_init():
        logger.info("Подключение и проверка базы данных MySQL...")
        try:
            await asyncio.wait_for(init_db(), timeout=10.0)
            logger.info("База данных MySQL успешно инициализирована.")
        except asyncio.TimeoutError:
            logger.error("Превышено время ожидания ответа MySQL (10 сек). Проверьте сетевую доступность 144.31.12.108:3306.")
        except Exception as e:
            logger.error(f"Ошибка при инициализации базы данных MySQL: {e}")

        try:
            backup_scheduler.init_bot(bot)
            await backup_scheduler.start_all()
            logger.info("Планировщик авто-бэкапов серверов запущен.")
        except Exception as e:
            logger.error(f"Ошибка запуска планировщика бэкапов: {e}")

        from helpers.telegram_notifier import start_telegram_polling
        asyncio.create_task(start_telegram_polling(bot))

    asyncio.create_task(async_init())


def main():
    token = settings.discord_token.get_secret_value()
    if not token or token == "your_discord_bot_token_here":
        logger.error("ОШИБКА: Пожалуйста, укажите валидный DISCORD_TOKEN в файле .env")
        sys.exit(1)

    # Загружаем коги до запуска бота
    bot.load_extension("cogs.anticrash_cmd")
    bot.load_extension("cogs.protection_events")
    logger.info("Все коги (cogs) успешно загружены.")

    bot.run(token)


if __name__ == "__main__":
    main()
