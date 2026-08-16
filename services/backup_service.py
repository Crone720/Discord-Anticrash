import os
import json
import uuid
import time
import datetime
import logging
import asyncio
import disnake
from typing import Dict, Tuple, Optional
from database.connection import AsyncSessionLocal
from database import crud
from services.github_service import GitHubService

logger = logging.getLogger("BackupService")

_guild_locks: Dict[int, asyncio.Lock] = {}


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _guild_locks:
        _guild_locks[guild_id] = asyncio.Lock()
    return _guild_locks[guild_id]


async def generate_guild_snapshot(guild: disnake.Guild, backup_type: str, storage: str, backup_id: str, backup_name: str) -> dict:
    """Собирает все данные сервера Discord для последующего полного восстановления."""
    
    # 1. Мета-данные и настройки сервера
    afk_chan = guild.afk_channel.id if guild.afk_channel else None
    sys_chan = guild.system_channel.id if guild.system_channel else None
    sys_flags = guild.system_channel_flags.value if guild.system_channel_flags else None
    rules_chan = guild.rules_channel.id if guild.rules_channel else None
    updates_chan = guild.public_updates_channel.id if guild.public_updates_channel else None

    guild_info = {
        "guild_id": guild.id,
        "name": guild.name,
        "description": guild.description,
        "icon": str(guild.icon.url) if guild.icon else None,
        "banner": str(guild.banner.url) if guild.banner else None,
        "splash": str(guild.splash.url) if guild.splash else None,
        "verification_level": str(guild.verification_level),
        "explicit_content_filter": str(guild.explicit_content_filter),
        "default_message_notifications": str(guild.default_notifications),
        "afk_channel_id": afk_chan,
        "afk_timeout": guild.afk_timeout,
        "system_channel_id": sys_chan,
        "system_channel_flags": sys_flags,
        "rules_channel_id": rules_chan,
        "public_updates_channel_id": updates_chan,
        "preferred_locale": str(guild.preferred_locale),
        "mfa_level": int(guild.mfa_level),
        "premium_tier": int(guild.premium_tier)
    }

    # 2. Роли
    roles_list = []
    for role in guild.roles:
        roles_list.append({
            "role_id": role.id,
            "name": role.name,
            "color": role.color.value,
            "position": role.position,
            "permissions": role.permissions.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "managed": role.managed,
            "icon": str(role.icon.url) if getattr(role, "icon", None) else None
        })

    # 3. Категории и каналы
    categories_list = []
    channels_list = []

    for channel in guild.channels:
        # Маппинг прав доступа (target_id, target_type, allow, deny)
        overwrites_data = []
        for target, overwrite in channel.overwrites.items():
            allow_val, deny_val = overwrite.pair()
            overwrites_data.append({
                "target_id": target.id,
                "target_type": "role" if isinstance(target, disnake.Role) else "member",
                "allow": allow_val.value,
                "deny": deny_val.value
            })

        if isinstance(channel, disnake.CategoryChannel):
            categories_list.append({
                "category_id": channel.id,
                "name": channel.name,
                "position": channel.position,
                "permission_overwrites": overwrites_data
            })
        else:
            chan_data = {
                "channel_id": channel.id,
                "name": channel.name,
                "type": str(channel.type),
                "category_id": channel.category_id,
                "position": channel.position,
                "permission_overwrites": overwrites_data
            }
            if hasattr(channel, "topic"):
                chan_data["topic"] = channel.topic
            if hasattr(channel, "nsfw"):
                chan_data["nsfw"] = channel.nsfw
            if hasattr(channel, "slowmode_delay"):
                chan_data["slowmode_delay"] = channel.slowmode_delay
            if hasattr(channel, "bitrate"):
                chan_data["bitrate"] = channel.bitrate
            if hasattr(channel, "user_limit"):
                chan_data["user_limit"] = channel.user_limit

            channels_list.append(chan_data)

    # 4. Участники (добавляем только если включена настройка save_members)
    async with AsyncSessionLocal() as session:
        b_setting = await crud.get_or_create_backup_settings(session, guild.id)
        save_members_enabled = b_setting.save_members

    # Собираем итоговый JSON-снапшот
    snapshot = {
        "backup_version": 1,
        "backup_id": backup_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "backup_name": backup_name,
        "backup_type": backup_type,
        "storage": storage,
        "guild": guild_info,
        "roles": roles_list,
        "categories": categories_list,
        "channels": channels_list
    }

    if save_members_enabled:
        members_dict = {}
        for member in guild.members:
            members_dict[str(member.id)] = {
                "role_ids": [r.id for r in member.roles if not r.is_default()],
                "nickname": member.nick
            }
        snapshot["members"] = members_dict

    return snapshot


async def execute_server_backup(
    guild: disnake.Guild,
    backup_type: str = "manual",
    storage_override: Optional[str] = None
) -> Tuple[bool, dict]:
    """
    Запускает бэкап сервера. Безопасно, с блокировкой.
    Возвращает Tuple[успех: bool, словарь с результатом].
    Словарь содержит: backup_name, backup_id, backup_type, storage, size_bytes, duration_ms, error.
    """
    from services.guild_operation_manager import guild_op_manager, BACKUP_RUNNING

    acquired, msg = await guild_op_manager.try_acquire(guild.id, BACKUP_RUNNING)
    if not acquired:
        return False, {
            "error": msg,
            "guild_id": guild.id
        }

    try:
        start_time = time.time()
        now = datetime.datetime.utcnow()

        # Генерируем уникальные ID и имя бэкапа
        date_str = now.strftime("%d-%m-%Y-%H-%M")
        backup_name = f"backup-{date_str}"
        backup_id = f"BK-{uuid.uuid4().hex[:12].upper()}"

        # Определяем хранилище
        async with AsyncSessionLocal() as session:
            setting = await crud.get_or_create_backup_settings(session, guild.id)
            storage = storage_override or setting.storage_type or "Database"

        try:
            # 1. Снимаем снапшот
            snapshot = await generate_guild_snapshot(
                guild=guild,
                backup_type=backup_type,
                storage=storage,
                backup_id=backup_id,
                backup_name=backup_name
            )

            # 2. Сериализуем в JSON
            json_str = json.dumps(snapshot, ensure_ascii=False, indent=2)
            size_bytes = len(json_str.encode("utf-8"))
            filename = f"{date_str}.json"

            # 3. Сохраняем в нужное хранилище
            if storage == "JSON":
                backups_dir = os.path.join(os.getcwd(), "backups")
                os.makedirs(backups_dir, exist_ok=True)
                file_path = os.path.join(backups_dir, filename)

                def write_file():
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(json_str)

                await asyncio.to_thread(write_file)
            elif storage == "GitHub":
                github_ok, github_info = await GitHubService.upload_backup_file(filename, json_str)
                if not github_ok:
                    return False, {
                        "error": f"Ошибка сохранения на GitHub: {github_info}",
                        "backup_name": backup_name,
                        "backup_id": backup_id
                    }

            # Всегда записываем запись о бэкапе в БД для быстрой индексации
            async with AsyncSessionLocal() as session:
                await crud.save_server_backup(
                    session=session,
                    backup_id=backup_id,
                    guild_id=guild.id,
                    backup_name=backup_name,
                    backup_type=backup_type,
                    storage=storage,
                    json_data=json_str,
                    size_bytes=size_bytes
                )

            # 4. Фиксируем успех в БД
            async with AsyncSessionLocal() as session:
                await crud.record_backup_success(
                    session=session,
                    guild_id=guild.id,
                    backup_name=backup_name,
                    backup_type=backup_type,
                    storage=storage
                )

            # 5. Перезапускаем таймер авто-бэкапа
            from services.backup_scheduler import backup_scheduler
            backup_scheduler.reset_timer(guild.id, guild)

            duration_ms = int((time.time() - start_time) * 1000)

            return True, {
                "backup_name": backup_name,
                "backup_id": backup_id,
                "backup_type": backup_type,
                "storage": storage,
                "size_bytes": size_bytes,
                "duration_ms": duration_ms
            }

        except Exception as e:
            logger.error(f"Ошибка создания бэкапа сервера {guild.id}: {e}", exc_info=True)
            return False, {
                "error": f"Внутренняя ошибка при создании бэкапа: {e}",
                "backup_name": backup_name,
                "backup_id": backup_id
            }
    finally:
        await guild_op_manager.release(guild.id)
