import logging
import asyncio
import time
import disnake
from typing import Optional, Dict, Any
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message as TelegramMessage
from config import settings
from database.connection import AsyncSessionLocal
from database import crud

logger = logging.getLogger("TelegramNotifier")

_tg_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None
_disnake_bot = None
router = Router()


@router.message(Command("id"))
async def cmd_telegram_id(message: TelegramMessage):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id

    response_text = (
        f"Информация об ID в Telegram:\n\n"
        f"• Ваш Личный User ID (ЛС): `{user_id}`\n"
        f"• ID текущего чата: `{chat_id}`\n\n"
    )
    await message.reply(text=response_text, parse_mode="Markdown")

_pending_requests: Dict[str, Dict[str, Any]] = {}


def get_telegram_bot() -> Optional[Bot]:
    global _tg_bot
    if _tg_bot is None and settings.telegram_token and settings.telegram_token.get_secret_value():
        try:
            _tg_bot = Bot(token=settings.telegram_token.get_secret_value())
        except Exception as e:
            logger.error(f"Ошибка инициализации Telegram бота: {e}")
            _tg_bot = None
    return _tg_bot


async def _timeout_watcher(req_id: str, timeout: int = 60):
    await asyncio.sleep(timeout)
    req = _pending_requests.pop(req_id, None)
    if not req:
        return

    tg_msg = req.get("tg_message")
    discord_inter = req.get("discord_inter")

    if tg_msg:
        try:
            await tg_msg.edit_text(
                text="[ИСТЕКЛО] Срок действия запроса на подтверждение (60 секунд) истёк.",
                reply_markup=None
            )
        except Exception as e:
            logger.error(f"Ошибка обновления сообщения Telegram при таймауте: {e}")

    if discord_inter:
        try:
            embed = disnake.Embed(
                title="Подтверждение не получено",
                description="Срок действия запроса на подтверждение в Telegram (60 секунд) истёк. Действие отменено.",
                color=None
            )
            await discord_inter.edit_original_response(embed=embed, view=None)
        except Exception as e:
            logger.error(f"Ошибка обновления сообщения Discord при таймауте: {e}")


async def send_telegram_alert(
    guild_name: str,
    guild_id: int,
    user_id: int,
    username: str,
    action: str,
    punishment: str,
    log_id: str,
    details: Optional[str] = None
):
    if not settings.enable_telegram_bot:
        return

    bot = get_telegram_bot()
    if not bot:
        return

    target_chat_id = settings.telegram_log_chat_id

    # Смотрим настройки уведомлений сервера в БД
    try:
        async with AsyncSessionLocal() as session:
            g_settings = await crud.get_or_create_guild_settings(session, guild_id)
            if not getattr(g_settings, "notifications_enabled", True):
                return
            if not getattr(g_settings, "notif_anticrash_trigger", True):
                return
            if getattr(g_settings, "tg_notification_chat_id", None):
                target_chat_id = g_settings.tg_notification_chat_id
    except Exception as e:
        logger.debug(f"Guild notification settings check error: {e}")

    if not target_chat_id:
        return

    text = (
        f"[АНТИКРАШ СРАБОТАЛ]\n"
        f"Сервер: {guild_name} (ID: {guild_id})\n"
        f"Нарушитель: {username} (ID: {user_id})\n"
        f"Действие: {action}\n"
        f"Наказание: {punishment}\n"
        f"ID Лога: {log_id}"
    )

    if details:
        text += f"\n\nПодробности:\n{details}"

    try:
        logger.info(f"Отправка алерта Антикраша в Telegram (Chat ID: {target_chat_id})")
        await asyncio.wait_for(bot.send_message(chat_id=target_chat_id, text=text), timeout=5.0)
    except Exception as e:
        logger.error(f"[Ошибка отправки Telegram алерта] Chat ID: {target_chat_id}, Причина: {e}")


async def send_telegram_confirmation_request(
    action_type: str,
    guild_name: str,
    guild_id: int,
    user_id: int,
    requested_by: str,
    discord_inter: Optional[disnake.MessageInteraction] = None,
    details: Optional[str] = None
) -> bool:
    bot = get_telegram_bot()
    chat_id = settings.telegram_log_chat_id

    if not bot or not chat_id or not settings.enable_telegram_bot:
        return False

    req_id = f"{guild_id}_{user_id}_{int(time.time())}"

    if action_type == "release":
        text = (
            f"[ЗАПРОС ПОДТВЕРЖДЕНИЯ - 60 СЕК]\n"
            f"Сервер: {guild_name} (ID: {guild_id})\n"
            f"Освобождение пользователя: (ID: {user_id})\n"
            f"Запросил в Discord: {requested_by}\n\n"
            f"Вы действительно хотите освободить этого пользователя из карантина и вернуть ему роли?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, подтверждаю",
                    callback_data=f"conf_rel:{req_id}"
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"cancel_act:{req_id}"
                )
            ]
        ])
    else:
        text = (
            f"[ЗАПРОС ПОДТВЕРЖДЕНИЯ - 60 СЕК]\n"
            f"Сервер: {guild_name} (ID: {guild_id})\n"
            f"Действие: Изменение статуса работы Антикраша\n"
            f"Запросил в Discord: {requested_by}\n\n"
            f"Вы действительно хотите переключить состояние работы Антикраша на сервере?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, подтверждаю",
                    callback_data=f"conf_tog:{req_id}"
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"cancel_act:{req_id}"
                )
            ]
        ])

    try:
        tg_msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        timer_task = asyncio.create_task(_timeout_watcher(req_id, timeout=60))

        _pending_requests[req_id] = {
            "guild_id": guild_id,
            "user_id": user_id,
            "action_type": action_type,
            "tg_message": tg_msg,
            "discord_inter": discord_inter,
            "timer_task": timer_task
        }
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить запрос подтверждения в Telegram: {e}")
        return False


# ================= Обработчики колбэков Telegram =================

@router.callback_query(F.data.startswith("ask_rel:"))
async def on_ask_release(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка формата данных.", show_alert=True)
        return
    guild_id = int(parts[1])
    user_id = int(parts[2])

    req_id = f"{guild_id}_{user_id}_{int(time.time())}"

    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Да, подтверждаю",
                callback_data=f"conf_rel:{req_id}"
            ),
            InlineKeyboardButton(
                text="Отмена",
                callback_data=f"cancel_act:{req_id}"
            )
        ]
    ])

    try:
        await callback.message.edit_text(
            text=(
                f"[ЗАПРОС ПОДТВЕРЖДЕНИЯ - 60 СЕК]\n\n"
                f"Вы действительно хотите освободить пользователя <{user_id}> из карантина на сервере ID: {guild_id}?\n"
                f"Роль карантина будет снята, а его прежние роли восстановлены."
            ),
            reply_markup=confirm_keyboard
        )
        timer_task = asyncio.create_task(_timeout_watcher(req_id, timeout=60))
        _pending_requests[req_id] = {
            "guild_id": guild_id,
            "user_id": user_id,
            "action_type": "release",
            "tg_message": callback.message,
            "discord_inter": None,
            "timer_task": timer_task
        }
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при ответе в Telegram: {e}")


@router.callback_query(F.data.startswith("conf_rel:"))
async def on_confirm_release(callback: CallbackQuery):
    req_id = callback.data.split(":", 1)[1]
    req = _pending_requests.pop(req_id, None)

    if not req:
        await callback.message.edit_text(text="[ИСТЕКЛО] Запрос более недействителен (таймаут 60 сек вышел или отменен).", reply_markup=None)
        await callback.answer("Срок действия запроса истек", show_alert=True)
        return

    req["timer_task"].cancel()
    guild_id = req["guild_id"]
    user_id = req["user_id"]
    discord_inter = req.get("discord_inter")

    global _disnake_bot
    if not _disnake_bot:
        await callback.answer("Discord бот недоступен.", show_alert=True)
        return

    guild = _disnake_bot.get_guild(guild_id)
    if not guild:
        try:
            guild = await _disnake_bot.fetch_guild(guild_id)
        except Exception:
            guild = None

    if not guild:
        await callback.message.edit_text(text=f"[ОШИБКА] Сервер ID: {guild_id} не найден.", reply_markup=None)
        return

    member = guild.get_member(user_id)
    if not member:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            member = None

    async with AsyncSessionLocal() as session:
        q_user = await crud.get_quarantine_user(session, guild_id, user_id)
        saved_roles = q_user.saved_roles if q_user else []
        g_settings = await crud.get_or_create_guild_settings(session, guild_id)

    if member:
        if g_settings.quarantine_role_id:
            q_role = guild.get_role(g_settings.quarantine_role_id)
            if q_role and q_role in member.roles:
                try:
                    await member.remove_roles(q_role, reason="Освобождение из карантина через Telegram")
                except Exception as e:
                    logger.error(f"Ошибка снятия роли карантина: {e}")

        if saved_roles:
            roles_to_restore = []
            for r_id in saved_roles:
                r = guild.get_role(r_id)
                if r and r < guild.me.top_role and not r.is_default():
                    roles_to_restore.append(r)
            if roles_to_restore:
                try:
                    await member.add_roles(*roles_to_restore, reason="Восстановление ролей из карантина")
                except Exception as e:
                    logger.error(f"Ошибка восстановления ролей: {e}")

    async with AsyncSessionLocal() as session:
        await crud.remove_quarantine_user(session, guild_id, user_id)

    await callback.message.edit_text(
        text=f"[ПОДТВЕРЖДЕНО]\nПользователь (ID: {user_id}) успешно освобожден из карантина на сервере {guild.name}.",
        reply_markup=None
    )
    await callback.answer("Подтверждено!")

    if discord_inter:
        try:
            embed = disnake.Embed(
                title="Действие подтверждено",
                description=f"Освобождение пользователя <@{user_id}> из карантина успешно **ПОДТВЕРЖДЕНО** в Telegram!",
                color=None
            )
            await discord_inter.edit_original_response(embed=embed, view=None)
        except Exception as e:
            logger.error(f"Ошибка обновления сообщения Discord: {e}")


@router.callback_query(F.data.startswith("conf_tog:"))
async def on_confirm_toggle(callback: CallbackQuery):
    req_id = callback.data.split(":", 1)[1]
    req = _pending_requests.pop(req_id, None)

    if not req:
        await callback.message.edit_text(text="[ИСТЕКЛО] Запрос более недействителен (таймаут 60 сек вышел или отменен).", reply_markup=None)
        await callback.answer("Срок действия запроса истек", show_alert=True)
        return

    req["timer_task"].cancel()
    guild_id = req["guild_id"]
    discord_inter = req.get("discord_inter")

    async with AsyncSessionLocal() as session:
        g_settings = await crud.get_or_create_guild_settings(session, guild_id)
        new_state = not g_settings.anticrash_enabled
        updated_settings = await crud.update_guild_settings(session, guild_id, anticrash_enabled=new_state)

    status_str = "ВКЛЮЧЕН" if updated_settings.anticrash_enabled else "ВЫКЛЮЧЕН"

    await callback.message.edit_text(
        text=f"[ПОДТВЕРЖДЕНО]\nСостояние работы Антикраша изменено на: **{status_str}**.",
        reply_markup=None
    )
    await callback.answer("Подтверждено!")

    if discord_inter:
        try:
            embed = disnake.Embed(
                title="Действие подтверждено",
                description=f"Изменение состояния работы Антикраша (новый статус: **{status_str}**) успешно **ПОДТВЕРЖДЕНО** в Telegram!",
                color=None
            )
            await discord_inter.edit_original_response(embed=embed, view=None)
        except Exception as e:
            logger.error(f"Ошибка обновления сообщения Discord: {e}")


@router.callback_query(F.data.startswith("cancel_act:"))
async def on_cancel_action(callback: CallbackQuery):
    req_id = callback.data.split(":", 1)[1]
    req = _pending_requests.pop(req_id, None)

    if req:
        req["timer_task"].cancel()
        discord_inter = req.get("discord_inter")
        if discord_inter:
            try:
                embed = disnake.Embed(
                    title="Действие отменено",
                    description="Запрос на подтверждение был отменён в Telegram-боте.",
                    color=None
                )
                await discord_inter.edit_original_response(embed=embed, view=None)
            except Exception as e:
                logger.error(f"Ошибка обновления сообщения Discord при отмене: {e}")

    await callback.message.edit_text(
        text="[ОТМЕНЕНО] Действие отменено пользователем.",
        reply_markup=None
    )
    await callback.answer("Отменено")


# ================= Запуск бота =================

async def start_telegram_polling(disnake_bot):
    global _disnake_bot, _dp
    _disnake_bot = disnake_bot

    if not settings.enable_telegram_bot:
        logger.info("Telegram бот выключен через ENABLE_TELEGRAM_BOT=false в .env. Пропуск запуска Telegram polling.")
        return

    bot = get_telegram_bot()
    if not bot:
        logger.info("Telegram бот не настроен (отсутствует TELEGRAM_TOKEN). Пропуск запуска Telegram polling.")
        return

    _dp = Dispatcher()
    _dp.include_router(router)

    logger.info("Запуск Telegram polling для обработки подтверждений кнопок...")
    try:
        await _dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка при работе Telegram polling: {e}")
