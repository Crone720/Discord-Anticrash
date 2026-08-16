import datetime
from typing import Dict, Any, List
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, String, JSON, Text
from database.connection import Base


class GuildSettings(Base):
    __tablename__ = "guild_settings"

    guild_id = Column(BigInteger, primary_key=True, index=True)
    anticrash_enabled = Column(Boolean, default=True, nullable=False)
    punishment_mode = Column(String(32), default="quarantine", nullable=False)  # 'quarantine' или 'ban'
    quarantine_role_id = Column(BigInteger, nullable=True)
    notification_channel_id = Column(BigInteger, nullable=True)
    tg_notification_chat_id = Column(BigInteger, nullable=True)
    notifications_enabled = Column(Boolean, default=True, nullable=False)
    notif_anticrash_trigger = Column(Boolean, default=True, nullable=False)
    notif_quarantine_release = Column(Boolean, default=True, nullable=False)
    notif_quarantine_confirm = Column(Boolean, default=False, nullable=False)
    total_blocked_attacks = Column(Integer, default=0, nullable=False)


class Whitelist(Base):
    __tablename__ = "whitelist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, index=True, nullable=False)
    user_id = Column(BigInteger, index=True, nullable=False)  # тут хранится user_id или role_id
    is_role = Column(Boolean, default=False, nullable=False)
    is_trusted = Column(Boolean, default=False, nullable=False)
    added_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    permissions = Column(JSON, default=dict, nullable=False)


class QuarantineUser(Base):
    __tablename__ = "quarantine_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(BigInteger, index=True, nullable=False)
    user_id = Column(BigInteger, index=True, nullable=False)
    saved_roles = Column(JSON, default=list, nullable=False)
    log_id = Column(String(32), nullable=True)
    quarantined_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    reason = Column(String(255), default="Нарушение правил безопасности сервера", nullable=False)


class AttackLog(Base):
    __tablename__ = "attack_logs"

    log_id = Column(String(32), primary_key=True, index=True)
    guild_id = Column(BigInteger, index=True, nullable=False)
    user_id = Column(BigInteger, index=True, nullable=False)
    action = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    allowed = Column(Boolean, default=False, nullable=False)
    details = Column(Text, nullable=True)


class GuildBackupSettings(Base):
    __tablename__ = "guild_backup_settings"

    guild_id = Column(BigInteger, primary_key=True, index=True)
    auto_backup_interval = Column(String(32), default="0", nullable=False)
    storage_type = Column(String(32), default="Database", nullable=False)
    save_members = Column(Boolean, default=False, nullable=False)
    last_backup_at = Column(DateTime, nullable=True)
    last_backup_name = Column(String(100), nullable=True)
    last_backup_type = Column(String(32), nullable=True)
    last_backup_storage = Column(String(32), nullable=True)


class ServerBackup(Base):
    __tablename__ = "server_backups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    backup_id = Column(String(64), index=True, nullable=False)
    guild_id = Column(BigInteger, index=True, nullable=False)
    backup_name = Column(String(100), nullable=False)
    backup_type = Column(String(32), default="manual", nullable=False)
    storage = Column(String(32), default="Database", nullable=False)
    json_data = Column(Text, nullable=False)
    size_bytes = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
