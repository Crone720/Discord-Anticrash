import uuid
import datetime
from typing import List, Optional, Dict, Any, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, desc
from database.models import GuildSettings, Whitelist, QuarantineUser, AttackLog, GuildBackupSettings, ServerBackup


# Словарь опасных прав по умолчанию — всё выключено
DEFAULT_PERMISSIONS = {
    "channel_create": False,
    "channel_delete": False,
    "channel_edit": False,
    "role_create": False,
    "role_delete": False,
    "role_edit": False,
    "emoji_stickers": False,
    "soundboard": False,
    "server_edit": False,
    "bot_add": False,
    "role_give_take": False,
    "mention_everyone": False,
    "link_invite": False,
    "member_ban": False,
    "member_kick": False,
    "member_timeout": False,
    "scheduled_events": False,
    "webhooks_manage": False,
}

PERMISSION_LABELS = {
    "channel_create": "Создание каналов",
    "channel_delete": "Удаление каналов",
    "channel_edit": "Изменение каналов",
    "role_create": "Создание ролей",
    "role_delete": "Удаление ролей",
    "role_edit": "Изменение ролей",
    "emoji_stickers": "Изменение эмодзи и стикеров",
    "soundboard": "Изменение саундборда",
    "server_edit": "Изменение параметров сервера",
    "bot_add": "Добавление ботов",
    "role_give_take": "Выдача / забирание ролей",
    "mention_everyone": "Упоминание @everyone / @here",
    "link_invite": "Отправка ссылок и приглашений",
    "member_ban": "Бан участников",
    "member_kick": "Кик участников",
    "member_timeout": "Выдача таймаута участникам",
    "scheduled_events": "Создание и управление ивентами",
    "webhooks_manage": "Управление вебхуками",
}


from sqlalchemy.exc import OperationalError, DBAPIError


async def get_or_create_guild_settings(session: AsyncSession, guild_id: int) -> GuildSettings:
    stmt = select(GuildSettings).where(GuildSettings.guild_id == guild_id)
    try:
        result = await session.execute(stmt)
    except (OperationalError, DBAPIError):
        result = await session.execute(stmt)

    settings = result.scalar_one_or_none()
    if not settings:
        settings = GuildSettings(guild_id=guild_id)
        session.add(settings)
        try:
            await session.commit()
            await session.refresh(settings)
        except (OperationalError, DBAPIError):
            await session.commit()
            await session.refresh(settings)
    return settings


async def update_guild_settings(session: AsyncSession, guild_id: int, **kwargs) -> GuildSettings:
    settings = await get_or_create_guild_settings(session, guild_id)
    for key, value in kwargs.items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    await session.commit()
    await session.refresh(settings)
    return settings


async def increment_blocked_attacks(session: AsyncSession, guild_id: int):
    settings = await get_or_create_guild_settings(session, guild_id)
    settings.total_blocked_attacks += 1
    await session.commit()


async def get_whitelist(session: AsyncSession, guild_id: int) -> List[Whitelist]:
    stmt = select(Whitelist).where(Whitelist.guild_id == guild_id).order_by(desc(Whitelist.added_at))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_whitelist_user(session: AsyncSession, guild_id: int, user_id: int) -> Optional[Whitelist]:
    stmt = select(Whitelist).where(Whitelist.guild_id == guild_id, Whitelist.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def add_to_whitelist(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    permissions: Optional[Dict[str, bool]] = None,
    is_role: bool = False
) -> Whitelist:
    if permissions is None:
        permissions = DEFAULT_PERMISSIONS.copy()
    
    wl_entry = await get_whitelist_user(session, guild_id, user_id)
    if wl_entry:
        wl_entry.permissions = permissions
        wl_entry.is_role = is_role
    else:
        wl_entry = Whitelist(
            guild_id=guild_id,
            user_id=user_id,
            is_role=is_role,
            permissions=permissions,
            added_at=datetime.datetime.utcnow()
        )
        session.add(wl_entry)
    
    await session.commit()
    await session.refresh(wl_entry)
    return wl_entry


async def remove_from_whitelist(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    stmt = delete(Whitelist).where(Whitelist.guild_id == guild_id, Whitelist.user_id == user_id)
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


from config import settings


async def is_user_trusted(session: AsyncSession, guild_id: int, user_id: int, owner_id: int) -> bool:
    if user_id == owner_id or (settings.developer_discord_id and user_id == settings.developer_discord_id):
        return True
    entry = await get_whitelist_user(session, guild_id, user_id)
    if entry and getattr(entry, "is_trusted", False):
        return True
    return False


async def toggle_whitelist_trusted(session: AsyncSession, guild_id: int, user_id: int) -> Tuple[bool, bool]:
    entry = await get_whitelist_user(session, guild_id, user_id)
    if not entry:
        return False, False
    entry.is_trusted = not getattr(entry, "is_trusted", False)
    await session.commit()
    await session.refresh(entry)
    return True, entry.is_trusted


async def add_quarantine_user(
    session: AsyncSession, guild_id: int, user_id: int, saved_roles: List[int], reason: str, log_id: Optional[str] = None
) -> QuarantineUser:
    stmt = select(QuarantineUser).where(QuarantineUser.guild_id == guild_id, QuarantineUser.user_id == user_id)
    result = await session.execute(stmt)
    entry = result.scalar_one_or_none()
    
    if entry:
        entry.saved_roles = saved_roles
        entry.reason = reason
        if log_id:
            entry.log_id = log_id
        entry.quarantined_at = datetime.datetime.utcnow()
    else:
        entry = QuarantineUser(
            guild_id=guild_id,
            user_id=user_id,
            saved_roles=saved_roles,
            reason=reason,
            log_id=log_id,
            quarantined_at=datetime.datetime.utcnow()
        )
        session.add(entry)
        
    await session.commit()
    await session.refresh(entry)
    return entry


async def get_quarantine_users(session: AsyncSession, guild_id: int) -> List[QuarantineUser]:
    stmt = select(QuarantineUser).where(QuarantineUser.guild_id == guild_id).order_by(desc(QuarantineUser.quarantined_at))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_quarantine_user(session: AsyncSession, guild_id: int, user_id: int) -> Optional[QuarantineUser]:
    stmt = select(QuarantineUser).where(QuarantineUser.guild_id == guild_id, QuarantineUser.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def remove_quarantine_user(session: AsyncSession, guild_id: int, user_id: int) -> bool:
    stmt = delete(QuarantineUser).where(QuarantineUser.guild_id == guild_id, QuarantineUser.user_id == user_id)
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount > 0


async def add_attack_log(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    action: str,
    allowed: bool = False,
    details: Optional[str] = None,
    log_id: Optional[str] = None
) -> AttackLog:
    unique_id = log_id or f"LOG-{uuid.uuid4().hex[:8].upper()}"
    log = AttackLog(
        log_id=unique_id,
        guild_id=guild_id,
        user_id=user_id,
        action=action,
        allowed=allowed,
        details=details,
        created_at=datetime.datetime.utcnow()
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


from sqlalchemy import select, update, delete, func, desc, or_


async def get_attack_logs(
    session: AsyncSession,
    guild_id: int,
    user_id: Optional[int] = None,
    categories: Optional[List[str]] = None,
    since_datetime: Optional[datetime.datetime] = None,
    limit: int = 300,
    offset: int = 0
) -> List[AttackLog]:
    stmt = select(AttackLog).where(AttackLog.guild_id == guild_id)
    if user_id:
        stmt = stmt.where(AttackLog.user_id == user_id)

    if categories:
        conditions = []
        for cat_key in categories:
            lbl = PERMISSION_LABELS.get(cat_key, cat_key)
            conditions.append(AttackLog.action.like(f"%{lbl}%"))
            conditions.append(AttackLog.action == cat_key)
        if conditions:
            stmt = stmt.where(or_(*conditions))

    if since_datetime:
        stmt = stmt.where(AttackLog.created_at >= since_datetime)

    stmt = stmt.order_by(desc(AttackLog.created_at)).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_attack_log_by_id(session: AsyncSession, guild_id: int, log_id: str) -> Optional[AttackLog]:
    clean_id = log_id.strip().upper()
    if not clean_id.startswith("LOG-"):
        clean_id = f"LOG-{clean_id}"

    stmt = select(AttackLog).where(AttackLog.guild_id == guild_id, AttackLog.log_id == clean_id)
    result = await session.execute(stmt)
    log = result.scalar_one_or_none()

    if not log:
        raw_query = log_id.strip()
        stmt = select(AttackLog).where(
            AttackLog.guild_id == guild_id,
            AttackLog.log_id.like(f"%{raw_query}%")
        )
        result = await session.execute(stmt)
        log = result.scalar_one_or_none()

    return log


async def get_last_attack_log(session: AsyncSession, guild_id: int) -> Optional[AttackLog]:
    stmt = select(AttackLog).where(AttackLog.guild_id == guild_id).order_by(desc(AttackLog.created_at)).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_or_create_backup_settings(session: AsyncSession, guild_id: int) -> GuildBackupSettings:
    stmt = select(GuildBackupSettings).where(GuildBackupSettings.guild_id == guild_id)
    result = await session.execute(stmt)
    setting = result.scalar_one_or_none()
    if not setting:
        setting = GuildBackupSettings(
            guild_id=guild_id,
            auto_backup_interval="0",
            storage_type="Database"
        )
        session.add(setting)
        await session.commit()
        await session.refresh(setting)
    return setting


async def update_backup_settings(
    session: AsyncSession,
    guild_id: int,
    auto_backup_interval: Optional[str] = None,
    storage_type: Optional[str] = None,
    save_members: Optional[bool] = None
) -> GuildBackupSettings:
    setting = await get_or_create_backup_settings(session, guild_id)
    if auto_backup_interval is not None:
        setting.auto_backup_interval = auto_backup_interval
    if storage_type is not None:
        setting.storage_type = storage_type
    if save_members is not None:
        setting.save_members = save_members
    await session.commit()
    await session.refresh(setting)
    return setting


async def record_backup_success(
    session: AsyncSession,
    guild_id: int,
    backup_name: str,
    backup_type: str,
    storage: str
) -> GuildBackupSettings:
    setting = await get_or_create_backup_settings(session, guild_id)
    setting.last_backup_at = datetime.datetime.utcnow()
    setting.last_backup_name = backup_name
    setting.last_backup_type = backup_type
    setting.last_backup_storage = storage
    await session.commit()
    await session.refresh(setting)
    return setting


async def save_server_backup(
    session: AsyncSession,
    backup_id: str,
    guild_id: int,
    backup_name: str,
    backup_type: str,
    storage: str,
    json_data: str,
    size_bytes: int
) -> ServerBackup:
    backup = ServerBackup(
        backup_id=backup_id,
        guild_id=guild_id,
        backup_name=backup_name,
        backup_type=backup_type,
        storage=storage,
        json_data=json_data,
        size_bytes=size_bytes,
        created_at=datetime.datetime.utcnow()
    )
    session.add(backup)
    await session.commit()
    await session.refresh(backup)
    return backup


async def get_all_active_auto_backups(session: AsyncSession) -> List[GuildBackupSettings]:
    stmt = select(GuildBackupSettings).where(GuildBackupSettings.auto_backup_interval != "0")
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_recent_backups(session: AsyncSession, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Возвращает последние бэкапы сервера (до limit штук), от новых к старым.
    Смотрит и в БД, и в локальную папку backups/ — объединяет всё в один список.
    """
    import os
    import json
    backups_dict = {}

    # 1. Тянем бэкапы из БД
    stmt = select(ServerBackup).where(ServerBackup.guild_id == guild_id).order_by(desc(ServerBackup.created_at)).limit(limit * 2)
    result = await session.execute(stmt)
    for b in result.scalars().all():
        backups_dict[b.backup_id] = {
            "backup_id": b.backup_id,
            "backup_name": b.backup_name,
            "backup_type": b.backup_type,
            "storage": b.storage,
            "size_bytes": b.size_bytes,
            "created_at": b.created_at,
            "json_data": b.json_data
        }

    # 2. Смотрим локальные JSON-файлы в папке backups/
    backups_dir = os.path.join(os.getcwd(), "backups")
    if os.path.exists(backups_dir):
        try:
            for fname in os.listdir(backups_dir):
                if fname.endswith(".json"):
                    fpath = os.path.join(backups_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            if isinstance(data, dict) and data.get("guild", {}).get("guild_id") == guild_id:
                                b_id = data.get("backup_id") or f"FILE-{fname}"
                                if b_id not in backups_dict:
                                    c_at_str = data.get("created_at")
                                    try:
                                        c_at = datetime.datetime.fromisoformat(c_at_str)
                                    except Exception:
                                        c_at = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))

                                    size_b = os.path.getsize(fpath)
                                    backups_dict[b_id] = {
                                        "backup_id": b_id,
                                        "backup_name": data.get("backup_name", fname.replace(".json", "")),
                                        "backup_type": data.get("backup_type", "manual"),
                                        "storage": data.get("storage", "JSON"),
                                        "size_bytes": size_b,
                                        "created_at": c_at,
                                        "json_data": json.dumps(data, ensure_ascii=False)
                                    }
                    except Exception:
                        pass
        except Exception:
            pass

    sorted_list = sorted(backups_dict.values(), key=lambda x: x["created_at"], reverse=True)
    return sorted_list[:limit]


async def get_backup_by_id(session: AsyncSession, guild_id: int, backup_id: str) -> Optional[Dict[str, Any]]:
    recent = await get_recent_backups(session, guild_id, limit=50)
    for b in recent:
        if b["backup_id"] == backup_id or b["backup_name"] == backup_id:
            return b
    return None
