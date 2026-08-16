import re
import json
import asyncio
import logging
import disnake
from urllib.parse import urlparse
from typing import Optional, List
from disnake.ext import commands
from database.connection import AsyncSessionLocal
from database import crud
from helpers.telegram_notifier import send_telegram_alert
from config import settings as app_settings

logger = logging.getLogger("ProtectionEvents")

ALLOWED_DOMAINS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "tenor.com",
    "www.tenor.com",
    "giphy.com",
    "www.giphy.com",
    "gfycat.com",
    "www.gfycat.com",
    "imgur.com",
    "i.imgur.com"
}

ALLOWED_EXTENSIONS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4"}
INVITE_DOMAINS = {"discord.gg", "discord.com", "discordapp.com", "dsc.gg", "discord.me"}


def is_forbidden_url(url: str) -> bool:
    clean_url = url.lower().strip()
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        clean_url = "http://" + clean_url

    parsed = urlparse(clean_url)
    domain = parsed.netloc or parsed.path.split("/")[0]

    # 1. Проверяем — это инвайт-ссылка Discord?
    if any(inv_domain in domain for inv_domain in INVITE_DOMAINS):
        if "/invite/" in parsed.path or domain in ("discord.gg", "dsc.gg", "discord.me"):
            return True

    # 2. Разрешённый CDN или домен с гифками?
    if any(allowed in domain for allowed in ALLOWED_DOMAINS):
        return False

    # 3. Допустимое расширение файла (картинка/гифка/медиа)?
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        return False

    # Всё остальное — внешняя ссылка или инвайт -> ЗАПРЕЩЕНО!
    return True


class ProtectionEventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _handle_violation(
        self,
        guild: disnake.Guild,
        actor: disnake.User | disnake.Member,
        action_name: str,
        revert_func=None,
        extra_details: Optional[str] = None,
        before_roles: Optional[List[disnake.Role]] = None
    ):
        """Применяет наказание, откатывает действие, логирует в БД и шлёт алерт в Telegram — всё по-быстрому."""
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, guild.id)
            
            if not settings.anticrash_enabled:
                return

            # Пропускаем владельца сервера, самого бота и разработчика
            if actor.id == guild.owner_id or actor.id == self.bot.user.id or (app_settings.developer_discord_id and actor.id == app_settings.developer_discord_id):
                return

            # 1. Проверяем белый список по пользователю
            wl_entry = await crud.get_whitelist_user(session, guild.id, actor.id)
            if wl_entry and wl_entry.permissions.get(action_name, False):
                # Действие разрешено
                log_details = f"Пользователь из белого списка выполнил {action_name}"
                if extra_details:
                    log_details += f"\n{extra_details}"
                await crud.add_attack_log(
                    session=session,
                    guild_id=guild.id,
                    user_id=actor.id,
                    action=f"Разрешено: {crud.PERMISSION_LABELS.get(action_name, action_name)}",
                    allowed=True,
                    details=log_details
                )
                return

            # 2. Проверяем белый список по ролям
            roles_to_check = before_roles if (before_roles is not None) else (actor.roles if hasattr(actor, "roles") else [])
            for role in roles_to_check:
                role_wl = await crud.get_whitelist_user(session, guild.id, role.id)
                if role_wl and role_wl.permissions.get(action_name, False):
                    # Действие разрешено через роль из белого списка
                    log_details = f"Разрешено через роль в белом списке: {role.name}"
                    if extra_details:
                        log_details += f"\n{extra_details}"
                    await crud.add_attack_log(
                        session=session,
                        guild_id=guild.id,
                        user_id=actor.id,
                        action=f"Разрешено: {crud.PERMISSION_LABELS.get(action_name, action_name)}",
                        allowed=True,
                        details=log_details
                    )
                    return

            # Несанкционированное действие — реагируем!
            punishment_str = ""

            # Готовим задачи наказания для параллельного запуска
            member = guild.get_member(actor.id)
            if not member and not actor.bot:
                try:
                    member = await guild.fetch_member(actor.id)
                except Exception:
                    member = None

            quarantine_roles_target = []
            if member and settings.punishment_mode != "ban":
                punishment_str = "Карантин"
                if before_roles is not None:
                    saved_role_ids = [r.id for r in before_roles if not r.is_default()]
                else:
                    saved_role_ids = [r.id for r in member.roles if not r.is_default()]

                # Формируем список ролей карантина — убираем всё за один HTTP-запрос
                if settings.quarantine_role_id:
                    q_role = guild.get_role(settings.quarantine_role_id)
                    if q_role and q_role < guild.me.top_role:
                        quarantine_roles_target.append(q_role)
            elif settings.punishment_mode == "ban" or actor.bot:
                punishment_str = "БАН" if not actor.bot else "БАН бота"

            # Генерируем уникальный log_id для точного отслеживания в аудит-логе
            import uuid
            log_id = f"LOG-{uuid.uuid4().hex[:8].upper()}"

            # Откат и наказание запускаем параллельно через asyncio.gather — максимально быстро
            async def execute_revert():
                if revert_func:
                    try:
                        import inspect
                        if callable(revert_func):
                            sig = inspect.signature(revert_func)
                            if len(sig.parameters) > 0:
                                res = revert_func(log_id)
                            else:
                                res = revert_func()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception as e:
                        logger.error(f"Не удалось откатить действие {action_name}: {e}")

            async def execute_punishment():
                if actor.bot or settings.punishment_mode == "ban":
                    try:
                        await guild.ban(actor, reason=f"Антикраш: заблокировано действие [{action_name}] [{log_id}]")
                    except Exception as e:
                        logger.error(f"Ошибка бана {actor.id}: {e}")
                elif member:
                    try:
                        await member.edit(roles=quarantine_roles_target, reason=f"Антикраш: помещение в карантин [{log_id}]")
                    except Exception as e:
                        logger.error(f"Ошибка смены ролей карантина для {member.id}: {e}")

            # Запускаем API-вызовы одновременно
            await asyncio.gather(execute_revert(), execute_punishment())

            # Логируем в БД и шлём в Telegram
            await crud.increment_blocked_attacks(session, guild.id)

            full_log_details = f"Наказание: {punishment_str}"
            if extra_details:
                full_log_details += f"\n{extra_details}"

            log_entry = await crud.add_attack_log(
                session=session,
                guild_id=guild.id,
                user_id=actor.id,
                action=crud.PERMISSION_LABELS.get(action_name, action_name),
                allowed=False,
                details=full_log_details,
                log_id=log_id
            )

            # Сохраняем в таблицу карантина — и для карантина, и для бана (чтобы можно было разбанить)
            if not actor.bot:
                reason_prefix = "Забанен Антикрашем" if "БАН" in punishment_str else "Попытка совершения опасного действия"
                await crud.add_quarantine_user(
                    session=session,
                    guild_id=guild.id,
                    user_id=actor.id,
                    saved_roles=saved_role_ids,
                    reason=f"{reason_prefix}: {crud.PERMISSION_LABELS.get(action_name, action_name)}",
                    log_id=log_id
                )

            # Отправляем алерт в Telegram асинхронно
            asyncio.create_task(
                send_telegram_alert(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    user_id=actor.id,
                    username=str(actor),
                    action=crud.PERMISSION_LABELS.get(action_name, action_name),
                    punishment=punishment_str,
                    log_id=log_id,
                    details=full_log_details
                )
            )

            # Отправляем уведомление в лог-канал Discord, если настроен
            if (
                getattr(settings, "notifications_enabled", True) and
                getattr(settings, "notif_anticrash_trigger", True) and
                getattr(settings, "notification_channel_id", None)
            ):
                try:
                    log_ch = guild.get_channel(settings.notification_channel_id)
                    if log_ch:
                        embed = disnake.Embed(color=None)
                        embed.title = "Антикраш сработал"
                        embed.description = (
                            f"**Сервер:** {guild.name} ({guild.id})\n"
                            f"**Нарушитель:** <@{actor.id}> ({actor.name})\n"
                            f"**Действие:** {crud.PERMISSION_LABELS.get(action_name, action_name)}\n"
                            f"**Наказание:** {punishment_str}\n"
                            f"**Лог ID:** `{log_id}`"
                        )
                        if extra_details:
                            embed.description += f"\n\n**Детали:**\n{extra_details}"
                        asyncio.create_task(log_ch.send(content="@everyone", embed=embed))
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления в Discord канал: {e}")

    async def _restore_deleted_channel(self, guild: disnake.Guild, channel: disnake.abc.GuildChannel):
        """Восстанавливает конкретный канал/категорию из последнего бэкапа (или из объекта события, если бэкапа нет)."""
        try:
            from services.restore_service import restore_deleted_channel_from_backup
            await restore_deleted_channel_from_backup(guild, channel)
        except Exception as e:
            logger.error(f"Ошибка отката удаленного канала #{channel.name}: {e}")

    async def _restore_deleted_role(self, guild: disnake.Guild, role: disnake.Role):
        """Восстанавливает конкретную роль из последнего бэкапа (или из объекта события, если бэкапа нет)."""
        try:
            from services.restore_service import restore_deleted_role_from_backup
            await restore_deleted_role_from_backup(guild, role)
        except Exception as e:
            logger.error(f"Ошибка отката удаленной роли @{role.name}: {e}")

    # ------------------ Слушатели событий защиты ------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: disnake.abc.GuildChannel):
        await asyncio.sleep(0.05)
        try:
            async for entry in channel.guild.audit_logs(limit=5, action=disnake.AuditLogAction.channel_create):
                if entry.target.id == channel.id:
                    extra = f"Создан канал: #{channel.name} (ID: {channel.id})"
                    await self._handle_violation(
                        guild=channel.guild,
                        actor=entry.user,
                        action_name="channel_create",
                        revert_func=lambda log_id: channel.delete(reason=f"Антикраш откат [{log_id}]"),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_channel_create: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: disnake.abc.GuildChannel):
        await asyncio.sleep(0.05)
        try:
            async for entry in channel.guild.audit_logs(limit=5, action=disnake.AuditLogAction.channel_delete):
                extra = f"Удален канал: #{channel.name} (ID: {channel.id})"
                await self._handle_violation(
                    guild=channel.guild,
                    actor=entry.user,
                    action_name="channel_delete",
                    revert_func=lambda log_id: self._restore_deleted_channel(channel.guild, channel),
                    extra_details=extra
                )
                break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_channel_delete: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: disnake.abc.GuildChannel, after: disnake.abc.GuildChannel):
        await asyncio.sleep(0.05)
        try:
            async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.channel_update):
                if entry.target.id == after.id:
                    extra = f"Изменен канал: #{after.name} (ID: {after.id})"
                    await self._handle_violation(
                        guild=after.guild,
                        actor=entry.user,
                        action_name="channel_edit",
                        revert_func=lambda log_id: self._restore_deleted_channel(after.guild, before),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_channel_update: {e}")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: disnake.Role):
        await asyncio.sleep(0.05)
        try:
            async for entry in role.guild.audit_logs(limit=5, action=disnake.AuditLogAction.role_create):
                if entry.target.id == role.id:
                    perms = []
                    if role.permissions.administrator:
                        perms.append("АДМИНИСТРАТОР")
                    if role.permissions.manage_guild:
                        perms.append("Управление сервером")
                    if role.permissions.manage_roles:
                        perms.append("Управление ролями")
                    perm_str = f" [Права: {', '.join(perms)}]" if perms else ""
                    extra = f"Создана роль: @{role.name} (ID: {role.id}){perm_str}"
                    await self._handle_violation(
                        guild=role.guild,
                        actor=entry.user,
                        action_name="role_create",
                        revert_func=lambda log_id: role.delete(reason=f"Антикраш откат [{log_id}]"),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_role_create: {e}")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: disnake.Role):
        await asyncio.sleep(0.05)
        try:
            async for entry in role.guild.audit_logs(limit=5, action=disnake.AuditLogAction.role_delete):
                extra = f"Удалена роль: @{role.name} (ID: {role.id})"
                await self._handle_violation(
                    guild=role.guild,
                    actor=entry.user,
                    action_name="role_delete",
                    revert_func=lambda log_id: self._restore_deleted_role(role.guild, role),
                    extra_details=extra
                )
                break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_role_delete: {e}")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: disnake.Role, after: disnake.Role):
        await asyncio.sleep(0.05)
        try:
            async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.role_update):
                if entry.target.id == after.id:
                    extra = f"Изменена роль: @{after.name} (ID: {after.id})"
                    await self._handle_violation(
                        guild=after.guild,
                        actor=entry.user,
                        action_name="role_edit",
                        revert_func=lambda log_id: self._restore_deleted_role(after.guild, before),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_role_update: {e}")

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: disnake.Guild, before: list, after: list):
        await asyncio.sleep(0.05)
        try:
            async for entry in guild.audit_logs(limit=5, action=disnake.AuditLogAction.emoji_create):
                await self._handle_violation(
                    guild=guild,
                    actor=entry.user,
                    action_name="emoji_stickers",
                    extra_details="Добавлен эмодзи"
                )
                return
            async for entry in guild.audit_logs(limit=5, action=disnake.AuditLogAction.emoji_delete):
                await self._handle_violation(
                    guild=guild,
                    actor=entry.user,
                    action_name="emoji_stickers",
                    extra_details="Удален эмодзи"
                )
                return
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_emojis_update: {e}")

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: disnake.Guild, before: list, after: list):
        await asyncio.sleep(0.05)
        try:
            async for entry in guild.audit_logs(limit=5, action=disnake.AuditLogAction.sticker_create):
                await self._handle_violation(
                    guild=guild,
                    actor=entry.user,
                    action_name="emoji_stickers",
                    extra_details="Добавлен стикер"
                )
                return
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_stickers_update: {e}")

    @commands.Cog.listener()
    async def on_guild_update(self, before: disnake.Guild, after: disnake.Guild):
        await asyncio.sleep(0.05)
        try:
            async for entry in after.audit_logs(limit=5, action=disnake.AuditLogAction.guild_update):
                await self._handle_violation(
                    guild=after,
                    actor=entry.user,
                    action_name="server_edit",
                    extra_details="Изменены настройки/иконка сервера"
                )
                break
        except Exception as e:
            logger.error(f"Ошибка в listener on_guild_update: {e}")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: disnake.Guild, user: disnake.User | disnake.Member):
        await asyncio.sleep(0.05)
        try:
            async for entry in guild.audit_logs(limit=5, action=disnake.AuditLogAction.ban):
                if entry.target.id == user.id:
                    extra = f"Забанен пользователь: <@{user.id}> ({user.name})"
                    await self._handle_violation(
                        guild=guild,
                        actor=entry.user,
                        action_name="member_ban",
                        revert_func=lambda log_id: guild.unban(user, reason=f"Антикраш откат бана [{log_id}]"),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_member_ban: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: disnake.Member):
        await asyncio.sleep(0.05)
        try:
            async for entry in member.guild.audit_logs(limit=5, action=disnake.AuditLogAction.kick):
                if entry.target.id == member.id:
                    extra = f"Кикнут пользователь: <@{member.id}> ({member.name})"
                    await self._handle_violation(
                        guild=member.guild,
                        actor=entry.user,
                        action_name="member_kick",
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_member_remove (kick): {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        if member.bot:
            await asyncio.sleep(0.05)
            try:
                async for entry in member.guild.audit_logs(limit=5, action=disnake.AuditLogAction.bot_add):
                    if entry.target.id == member.id:
                        extra = f"Добавлен бот: <@{member.id}> ({member.name})"
                        await self._handle_violation(
                            guild=member.guild,
                            actor=entry.user,
                            action_name="bot_add",
                            revert_func=lambda log_id: member.ban(reason=f"Антикраш: неразрешенный бот [{log_id}]"),
                            extra_details=extra
                        )
                        break
            except Exception as e:
                logger.error(f"Ошибка в listener on_member_join (bot_add): {e}")
        else:
            # При перезаходе проверяем, не в карантине ли юзер
            try:
                async with AsyncSessionLocal() as session:
                    q_user = await crud.get_quarantine_user(session, member.guild.id, member.id)
                    if q_user:
                        settings = await crud.get_or_create_guild_settings(session, member.guild.id)
                        if settings.quarantine_role_id:
                            q_role = member.guild.get_role(settings.quarantine_role_id)
                            if q_role and q_role < member.guild.me.top_role:
                                await member.edit(roles=[q_role], reason="Антикраш: повторная выдача роли карантина при перезаходе")
            except Exception as e:
                logger.error(f"Ошибка проверки карантина при перезаходе {member.id}: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: disnake.Member, after: disnake.Member):
        # 1. Проверка таймаута
        if before.current_timeout != after.current_timeout and after.current_timeout is not None:
            await asyncio.sleep(0.05)
            try:
                async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.member_update):
                    if entry.target.id == after.id:
                        extra = f"Выдан таймаут пользователю: <@{after.id}> ({after.name})"
                        await self._handle_violation(
                            guild=after.guild,
                            actor=entry.user,
                            action_name="member_timeout",
                            revert_func=lambda log_id: after.edit(timeout=None, reason=f"Антикраш откат таймаута [{log_id}]"),
                            extra_details=extra
                        )
                        break
            except Exception as e:
                logger.error(f"Ошибка в listener on_member_update (timeout): {e}")

        # 2. Проверка изменения ролей
        if before.roles != after.roles:
            added_roles = [r for r in after.roles if r not in before.roles]
            removed_roles = [r for r in before.roles if r not in after.roles]

            changes_desc = []
            for r in added_roles:
                perms = []
                if r.permissions.administrator:
                    perms.append("АДМИНИСТРАТОР")
                if r.permissions.manage_guild:
                    perms.append("Управление сервером")
                if r.permissions.manage_roles:
                    perms.append("Управление ролями")
                if r.permissions.manage_channels:
                    perms.append("Управление каналами")
                if r.permissions.ban_members or r.permissions.kick_members:
                    perms.append("Модерация (Бан/Кик)")

                perm_str = f" [ОПАСНЫЕ ПРАВА: {', '.join(perms)}]" if perms else " [Обычная роль]"
                changes_desc.append(f"+ Выдана роль: {r.name} (ID: {r.id}){perm_str}")

            for r in removed_roles:
                perms = []
                if r.permissions.administrator:
                    perms.append("АДМИНИСТРАТОР")
                if r.permissions.manage_guild:
                    perms.append("Управление сервером")
                if r.permissions.manage_roles:
                    perms.append("Управление ролями")
                if r.permissions.manage_channels:
                    perms.append("Управление каналами")
                if r.permissions.ban_members or r.permissions.kick_members:
                    perms.append("Модерация (Бан/Кик)")

                perm_str = f" [ОПАСНЫЕ ПРАВА: {', '.join(perms)}]" if perms else " [Обычная роль]"
                changes_desc.append(f"- Забрана роль: {r.name} (ID: {r.id}){perm_str}")

            target_str = f"Целевой пользователь: <@{after.id}> ({after.display_name} @{after.name})"
            extra_details = f"{target_str}\n" + "\n".join(changes_desc)

            await asyncio.sleep(0.05)
            try:
                found = False
                async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.member_role_update):
                    if entry.target.id == after.id:
                        found = True
                        async def revert_roles(log_id):
                            valid_roles = [r for r in before.roles if not r.is_default() and r < after.guild.me.top_role]
                            await after.edit(roles=valid_roles, reason=f"Антикраш откат изменения ролей [{log_id}]")

                        actor_before_roles = before.roles if entry.user.id == after.id else None
                        await self._handle_violation(
                            guild=after.guild,
                            actor=entry.user,
                            action_name="role_give_take",
                            revert_func=revert_roles,
                            extra_details=extra_details,
                            before_roles=actor_before_roles
                        )
                        break
                
                # Быстрый фолбэк — ждём чуть дольше и повторяем
                if not found:
                    await asyncio.sleep(0.2)
                    async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.member_role_update):
                        if entry.target.id == after.id:
                            async def revert_roles(log_id):
                                valid_roles = [r for r in before.roles if not r.is_default() and r < after.guild.me.top_role]
                                await after.edit(roles=valid_roles, reason=f"Антикраш откат изменения ролей [{log_id}]")

                            actor_before_roles = before.roles if entry.user.id == after.id else None
                            await self._handle_violation(
                                guild=after.guild,
                                actor=entry.user,
                                action_name="role_give_take",
                                revert_func=revert_roles,
                                extra_details=extra_details,
                                before_roles=actor_before_roles
                            )
                            break
            except Exception as e:
                logger.error(f"Ошибка в listener on_member_update: {e}")

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: disnake.GuildScheduledEvent):
        await asyncio.sleep(0.05)
        try:
            async for entry in event.guild.audit_logs(limit=5, action=disnake.AuditLogAction.scheduled_event_create):
                if entry.target.id == event.id:
                    extra = f"Создан ивент: {event.name} (ID: {event.id})"
                    await self._handle_violation(
                        guild=event.guild,
                        actor=entry.user,
                        action_name="scheduled_events",
                        revert_func=lambda log_id: event.delete(reason=f"Антикраш откат ивента [{log_id}]"),
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_scheduled_event_create: {e}")

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: disnake.GuildScheduledEvent):
        await asyncio.sleep(0.05)
        try:
            async for entry in event.guild.audit_logs(limit=5, action=disnake.AuditLogAction.scheduled_event_delete):
                extra = f"Удален ивент: {event.name} (ID: {event.id})"
                await self._handle_violation(
                    guild=event.guild,
                    actor=entry.user,
                    action_name="scheduled_events",
                    extra_details=extra
                )
                break
        except Exception as e:
            logger.error(f"Ошибка в listener on_scheduled_event_delete: {e}")

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: disnake.GuildScheduledEvent, after: disnake.GuildScheduledEvent):
        await asyncio.sleep(0.05)
        try:
            async for entry in after.guild.audit_logs(limit=5, action=disnake.AuditLogAction.scheduled_event_update):
                if entry.target.id == after.id:
                    extra = f"Изменен ивент: {after.name} (ID: {after.id})"
                    await self._handle_violation(
                        guild=after.guild,
                        actor=entry.user,
                        action_name="scheduled_events",
                        extra_details=extra
                    )
                    break
        except Exception as e:
            logger.error(f"Ошибка в listener on_scheduled_event_update: {e}")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: disnake.abc.GuildChannel):
        await asyncio.sleep(0.05)
        try:
            for action in (disnake.AuditLogAction.webhook_create, disnake.AuditLogAction.webhook_delete, disnake.AuditLogAction.webhook_update):
                async for entry in channel.guild.audit_logs(limit=3, action=action):
                    extra = f"Действие с вебхуком ({action.name}) в канале <#{channel.id}>"
                    await self._handle_violation(
                        guild=channel.guild,
                        actor=entry.user,
                        action_name="webhooks_manage",
                        extra_details=extra
                    )
                    return
        except Exception as e:
            logger.error(f"Ошибка в listener on_webhooks_update: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        if not message.guild or message.author.bot:
            return

        # 1. Защита от пингов @everyone и @here
        if message.mention_everyone or "@everyone" in message.content or "@here" in message.content:
            channel_name = getattr(message.channel, "name", str(message.channel.id))

            attachment_urls = [att.url for att in message.attachments] if message.attachments else []
            attachment_text = ""
            if attachment_urls:
                attachment_text = "\n\nМедиа / Вложения:\n" + "\n".join(f"• {url}" for url in attachment_urls)

            raw_content = message.content or "[Сообщение без текста]"
            safe_content = raw_content.replace("```", "'''")

            extra = (
                f"Канал: <#{message.channel.id}> (#{channel_name})\n"
                f"Сообщение:\n```\n{safe_content}\n```"
                f"{attachment_text}"
            )

            await self._handle_violation(
                guild=message.guild,
                actor=message.author,
                action_name="mention_everyone",
                revert_func=lambda log_id: message.delete(),
                extra_details=extra
            )
            return

        # 2. Защита от запрещённых ссылок и инвайтов Discord (CDN и гифки — разрешены)
        if message.content:
            urls = re.findall(r"(https?://\S+|www\.\S+|discord\.gg/\S+|dsc\.gg/\S+)", message.content, re.IGNORECASE)
            forbidden_urls = [u for u in urls if is_forbidden_url(u)]

            if forbidden_urls:
                channel_name = getattr(message.channel, "name", str(message.channel.id))
                raw_content = message.content or "[Сообщение без текста]"
                safe_content = raw_content.replace("```", "'''")

                extra = (
                    f"Канал: <#{message.channel.id}> (#{channel_name})\n"
                    f"Запрещенные ссылки: {', '.join(forbidden_urls[:5])}\n"
                    f"Сообщение:\n```\n{safe_content}\n```"
                )

                await self._handle_violation(
                    guild=message.guild,
                    actor=message.author,
                    action_name="link_invite",
                    revert_func=lambda log_id: message.delete(),
                    extra_details=extra
                )


def setup(bot: commands.Bot):
    bot.add_cog(ProtectionEventsCog(bot))
