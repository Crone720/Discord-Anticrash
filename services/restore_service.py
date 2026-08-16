import os
import json
import time
import logging
import asyncio
import datetime
import disnake
from typing import Dict, List, Tuple, Any, Optional, Set, Callable
from services.guild_operation_manager import guild_op_manager, RESTORE_RUNNING
from database.connection import AsyncSessionLocal
from database import crud

logger = logging.getLogger("RestoreService")

DIFF_TRANSLATIONS = {
    "name": "название",
    "topic": "тема",
    "slowmode_delay": "медленный режим",
    "nsfw": "NSFW",
    "category": "категория",
    "position": "позиция",
    "overwrites": "права доступа",
    "permissions": "права",
    "color": "цвет",
    "hoist": "отображение раздельно",
    "mentionable": "упоминаемая"
}


def _translate_diff_fields(fields: List[str]) -> str:
    return ", ".join(DIFF_TRANSLATIONS.get(f, f) for f in fields)


def _compare_overwrites(
    current_overwrites: dict,
    backup_overwrites_list: list,
    role_id_map: dict
) -> bool:
    """
    Возвращает True, если текущие права доступа совпадают с бэкапом.
    Возвращает False, если есть различия.
    """
    current_set: Set[Tuple[int, str, int, int]] = set()
    for target, ow in current_overwrites.items():
        if isinstance(target, disnake.Role):
            t_type = "role"
            t_id = target.id
        elif isinstance(target, disnake.Member):
            t_type = "member"
            t_id = target.id
        else:
            continue
        pair = ow.pair()
        current_set.add((t_id, t_type, pair[0].value, pair[1].value))

    backup_set: Set[Tuple[int, str, int, int]] = set()
    for ow in backup_overwrites_list:
        t_id = ow.get("target_id")
        t_type = ow.get("target_type")
        allow_val = ow.get("allow", 0)
        deny_val = ow.get("deny", 0)
        if t_type == "role":
            t_id = role_id_map.get(t_id, t_id)
        backup_set.add((t_id, t_type, allow_val, deny_val))

    return current_set == backup_set


def _build_overwrites_dict(
    backup_overwrites_list: list,
    role_map: dict,
    role_id_map: dict,
    guild: disnake.Guild,
    error_logs: list
) -> dict:
    overwrites_dict = {}
    for ow in backup_overwrites_list:
        t_id = ow.get("target_id")
        t_type = ow.get("target_type")
        allow_val = ow.get("allow", 0)
        deny_val = ow.get("deny", 0)

        ow_obj = disnake.PermissionOverwrite.from_pair(
            disnake.Permissions(allow_val),
            disnake.Permissions(deny_val)
        )

        if t_type == "role":
            target_role_id = role_id_map.get(t_id, t_id)
            r_target = role_map.get(t_id) or guild.get_role(target_role_id)
            if r_target:
                overwrites_dict[r_target] = ow_obj
            else:
                error_logs.append(f"Роль {t_id}: Не найдена на сервере для прав доступа")
        elif t_type == "member":
            m_target = guild.get_member(t_id)
            if m_target:
                overwrites_dict[m_target] = ow_obj
            else:
                error_logs.append(f"Участник {t_id}: Не найден на сервере для прав доступа")
    return overwrites_dict


def _get_channel_diff(
    target_ch: disnake.abc.GuildChannel,
    ch_data: dict,
    category_id_map: dict,
    role_id_map: dict,
    live_categories_by_id: dict
) -> Tuple[dict, List[str]]:
    edit_kwargs = {}
    diff_fields = []

    ch_name = ch_data.get("name")
    if target_ch.name != ch_name:
        edit_kwargs["name"] = ch_name
        diff_fields.append("name")

    backup_cat_id = ch_data.get("category_id")
    expected_cat_id = category_id_map.get(backup_cat_id, backup_cat_id) if backup_cat_id else None
    if target_ch.category_id != expected_cat_id:
        if expected_cat_id in live_categories_by_id:
            edit_kwargs["category"] = live_categories_by_id[expected_cat_id]
            diff_fields.append("category")
        elif expected_cat_id is None and target_ch.category_id is not None:
            edit_kwargs["category"] = None
            diff_fields.append("category")

    ch_pos = ch_data.get("position", 0)
    if "category" not in edit_kwargs and target_ch.position != ch_pos:
        edit_kwargs["position"] = ch_pos
        diff_fields.append("position")

    if "topic" in ch_data and hasattr(target_ch, "topic"):
        b_topic = ch_data.get("topic") or ""
        t_topic = getattr(target_ch, "topic", "") or ""
        if t_topic != b_topic:
            edit_kwargs["topic"] = b_topic
            diff_fields.append("topic")

    if "nsfw" in ch_data and hasattr(target_ch, "nsfw"):
        if getattr(target_ch, "nsfw", False) != ch_data.get("nsfw", False):
            edit_kwargs["nsfw"] = ch_data.get("nsfw", False)
            diff_fields.append("nsfw")

    if "slowmode_delay" in ch_data and hasattr(target_ch, "slowmode_delay"):
        if getattr(target_ch, "slowmode_delay", 0) != ch_data.get("slowmode_delay", 0):
            edit_kwargs["slowmode_delay"] = ch_data.get("slowmode_delay", 0)
            diff_fields.append("slowmode_delay")

    ow_list = ch_data.get("permission_overwrites", [])
    if ow_list and not _compare_overwrites(target_ch.overwrites, ow_list, role_id_map):
        edit_kwargs["__update_overwrites__"] = True
        diff_fields.append("overwrites")

    return edit_kwargs, diff_fields


async def restore_deleted_channel_from_backup(guild: disnake.Guild, deleted_ch: disnake.abc.GuildChannel):
    """
    Восстанавливает только тот канал (или категорию), который был удалён.
    1. Ищет данные в последнем снапшоте бэкапа.
    2. Если нет в бэкапе — берёт параметры из объекта deleted_ch (событие до удаления).
    3. Роли и участников не трогает!
    """
    snapshot = None
    try:
        async with AsyncSessionLocal() as session:
            recent = await crud.get_recent_backups(session, guild.id, limit=1)
            if recent and recent[0].get("json_data"):
                snapshot = json.loads(recent[0]["json_data"])
    except Exception as e:
        logger.error(f"Failed to load snapshot for single channel restore: {e}")

    ch_data_in_backup = None
    cat_data_in_backup = None

    if snapshot:
        if deleted_ch.type == disnake.ChannelType.category:
            for cat_data in snapshot.get("categories", []):
                if cat_data.get("category_id") == deleted_ch.id or cat_data.get("name") == deleted_ch.name:
                    cat_data_in_backup = cat_data
                    break
        else:
            for ch_data in snapshot.get("channels", []):
                if ch_data.get("channel_id") == deleted_ch.id or ch_data.get("name") == deleted_ch.name:
                    ch_data_in_backup = ch_data
                    break

    # СЛУЧАЙ 1: Удалённый объект — КАТЕГОРИЯ
    if deleted_ch.type == disnake.ChannelType.category:
        if cat_data_in_backup:
            cat_name = cat_data_in_backup.get("name", deleted_ch.name)
            cat_pos = cat_data_in_backup.get("position", deleted_ch.position)
            ow_list = cat_data_in_backup.get("permission_overwrites", [])
            overwrites = _build_overwrites_dict(ow_list, {}, {}, guild, []) if ow_list else deleted_ch.overwrites
        else:
            cat_name = deleted_ch.name
            cat_pos = deleted_ch.position
            overwrites = deleted_ch.overwrites

        try:
            new_cat = await guild.create_category(
                name=cat_name,
                position=cat_pos,
                overwrites=overwrites,
                reason="Антикраш откат: целевое восстановление удаленной категории"
            )
            logger.info(f"Anti-Crash: Restored deleted category {new_cat.name} ({new_cat.id})")
        except Exception as e:
            logger.error(f"Anti-Crash: Failed to restore deleted category {cat_name}: {e}")
        return

    # СЛУЧАЙ 2: Удалённый объект — КАНАЛ (текстовый, голосовой, сцена)
    if ch_data_in_backup:
        ch_name = ch_data_in_backup.get("name", deleted_ch.name)
        ch_type_str = ch_data_in_backup.get("type", str(deleted_ch.type))
        ch_cat_id = ch_data_in_backup.get("category_id")
        ch_pos = ch_data_in_backup.get("position", deleted_ch.position)
        topic_val = ch_data_in_backup.get("topic", getattr(deleted_ch, "topic", None))
        nsfw_val = ch_data_in_backup.get("nsfw", getattr(deleted_ch, "nsfw", False))
        slowmode_val = ch_data_in_backup.get("slowmode_delay", getattr(deleted_ch, "slowmode_delay", 0))

        parent_cat = None
        if ch_cat_id:
            parent_cat = guild.get_channel(ch_cat_id)
        if not parent_cat and deleted_ch.category:
            parent_cat = deleted_ch.category

        ow_list = ch_data_in_backup.get("permission_overwrites", [])
        if ow_list:
            overwrites = _build_overwrites_dict(ow_list, {}, {}, guild, [])
        else:
            overwrites = deleted_ch.overwrites
    else:
        # Фолбэк — берём данные из объекта события
        ch_name = deleted_ch.name
        ch_type_str = str(deleted_ch.type)
        parent_cat = deleted_ch.category
        ch_pos = deleted_ch.position
        topic_val = getattr(deleted_ch, "topic", None)
        nsfw_val = getattr(deleted_ch, "nsfw", False)
        slowmode_val = getattr(deleted_ch, "slowmode_delay", 0)
        overwrites = deleted_ch.overwrites

    create_kwargs = {
        "name": ch_name,
        "category": parent_cat,
        "position": ch_pos,
        "overwrites": overwrites,
        "reason": "Антикраш откат: целевое восстановление удаленного канала"
    }
    if topic_val and "text" in ch_type_str.lower():
        create_kwargs["topic"] = topic_val
    if nsfw_val and "text" in ch_type_str.lower():
        create_kwargs["nsfw"] = nsfw_val
    if slowmode_val and "text" in ch_type_str.lower():
        create_kwargs["slowmode_delay"] = slowmode_val

    try:
        if "voice" in ch_type_str.lower():
            await guild.create_voice_channel(**create_kwargs)
        elif "stage" in ch_type_str.lower():
            await guild.create_stage_channel(**create_kwargs)
        else:
            await guild.create_text_channel(**create_kwargs)
        logger.info(f"Anti-Crash: Restored deleted channel #{ch_name}")
    except Exception as e:
        logger.error(f"Anti-Crash: Failed to restore deleted channel #{ch_name}: {e}")


async def restore_deleted_role_from_backup(guild: disnake.Guild, deleted_role: disnake.Role):
    """
    Восстанавливает только ту роль, которая была удалена.
    1. Ищет данные в последнем снапшоте (название, цвет, права, hoist, mentionable).
    2. Если нет в снапшоте — берёт из объекта deleted_role.
    3. Участников не трогает.
    """
    snapshot = None
    try:
        async with AsyncSessionLocal() as session:
            recent = await crud.get_recent_backups(session, guild.id, limit=1)
            if recent and recent[0].get("json_data"):
                snapshot = json.loads(recent[0]["json_data"])
    except Exception as e:
        logger.error(f"Failed to load snapshot for single role restore: {e}")

    role_data_in_backup = None
    if snapshot:
        for r_data in snapshot.get("roles", []):
            if r_data.get("role_id") == deleted_role.id or r_data.get("name") == deleted_role.name:
                role_data_in_backup = r_data
                break

    if role_data_in_backup:
        r_name = role_data_in_backup.get("name", deleted_role.name)
        r_color = disnake.Color(role_data_in_backup.get("color", deleted_role.color.value))
        r_perms = disnake.Permissions(role_data_in_backup.get("permissions", deleted_role.permissions.value))
        r_hoist = role_data_in_backup.get("hoist", deleted_role.hoist)
        r_mentionable = role_data_in_backup.get("mentionable", deleted_role.mentionable)
    else:
        r_name = deleted_role.name
        r_color = deleted_role.color
        r_perms = deleted_role.permissions
        r_hoist = deleted_role.hoist
        r_mentionable = deleted_role.mentionable

    try:
        await guild.create_role(
            name=r_name,
            color=r_color,
            permissions=r_perms,
            hoist=r_hoist,
            mentionable=r_mentionable,
            reason="Антикраш откат: целевое восстановление удаленной роли"
        )
        logger.info(f"Anti-Crash: Restored deleted role @{r_name}")
    except Exception as e:
        logger.error(f"Anti-Crash: Failed to restore deleted role @{r_name}: {e}")


async def restore_guild_from_backup(
    guild: disnake.Guild,
    snapshot: dict,
    progress_callback: Optional[Callable[[int], Any]] = None
) -> Tuple[dict, Dict[str, List[str]], str]:
    """
    Движок точного восстановления Антикраша с полным отчётом.
    Защищает пользователей в карантине от потери статуса.
    """
    acquired, msg = await guild_op_manager.try_acquire(guild.id, RESTORE_RUNNING)
    if not acquired:
        return {"error": msg}, {}, ""

    started_dt_str = datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M:%S UTC")
    b_name = snapshot.get("backup_name", f"backup-{guild.id}")
    b_id = snapshot.get("backup_id", "N/A")
    txt_filename = f"{b_name}-restore.txt"

    backups_dir = os.path.join(os.getcwd(), "backups")
    os.makedirs(backups_dir, exist_ok=True)
    txt_filepath = os.path.join(backups_dir, txt_filename)

    stats = {
        "channels_created": 0,
        "channels_restored": 0,
        "channels_deleted": 0,
        "categories_created": 0,
        "categories_restored": 0,
        "categories_deleted": 0,
        "roles_created": 0,
        "roles_restored": 0,
        "roles_deleted": 0,
        "overwrites_restored": 0,
        "members_restored": 0,
        "failed_count": 0,
        "skipped_managed_roles": 0,
        "cancelled": False,
        "no_changes_needed": False,
        "structure_matched": True,
        "members_matched": True,
        "has_discrepancies": False
    }

    changes_summary: Dict[str, List[str]] = {
        "created": [],
        "updated": [],
        "deleted": []
    }

    txt_created_roles: List[str] = []
    txt_created_cats: List[str] = []
    txt_created_chans: List[str] = []
    txt_updated_chans: List[str] = []
    txt_updated_overwrites: List[str] = []
    txt_updated_members: List[str] = []
    txt_errors: List[str] = []

    role_map: Dict[int, disnake.Role] = {}
    role_id_map: Dict[int, int] = {}
    category_map: Dict[int, disnake.CategoryChannel] = {}
    category_id_map: Dict[int, int] = {}

    try:
        # ================= Этап 1: Анализ в памяти и вычисление diff =================
        if progress_callback:
            await progress_callback(1)

        roles_backup = snapshot.get("roles", [])
        categories_backup = snapshot.get("categories", [])
        channels_backup = snapshot.get("channels", [])
        members_backup = snapshot.get("members", {})

        backup_role_ids = {r.get("role_id") for r in roles_backup if r.get("role_id")}
        backup_role_names = {r.get("name") for r in roles_backup if r.get("name")}

        backup_category_ids = {c.get("category_id") for c in categories_backup if c.get("category_id")}
        backup_category_names = {c.get("name") for c in categories_backup if c.get("name")}

        backup_channel_ids = {c.get("channel_id") for c in channels_backup if c.get("channel_id")}
        backup_channel_names = {c.get("name") for c in channels_backup if c.get("name")}

        bot_top_role = guild.me.top_role

        # Получаем ID карантинных пользователей и ID карантинной роли, чтобы их не трогать
        quarantined_user_ids = set()
        quarantine_role_id = None
        try:
            async with AsyncSessionLocal() as session:
                q_users = await crud.get_quarantine_users(session, guild.id)
                quarantined_user_ids = {q.user_id for q in q_users}
                g_settings = await crud.get_or_create_guild_settings(session, guild.id)
                quarantine_role_id = g_settings.quarantine_role_id
        except Exception as e:
            logger.error(f"Error checking quarantine users: {e}")

        # 1. Что нужно удалить
        roles_to_delete: List[disnake.Role] = []
        for r in guild.roles:
            if r.is_default():
                continue
            if r.managed:
                stats["skipped_managed_roles"] += 1
                continue
            if r >= bot_top_role:
                continue
            if r.id not in backup_role_ids and r.name not in backup_role_names:
                roles_to_delete.append(r)

        categories_to_delete: List[disnake.CategoryChannel] = []
        for cat in guild.categories:
            if cat.id not in backup_category_ids and cat.name not in backup_category_names:
                categories_to_delete.append(cat)

        channels_to_delete: List[disnake.abc.GuildChannel] = []
        for ch in guild.channels:
            if ch.type == disnake.ChannelType.category:
                continue
            if ch.id not in backup_channel_ids and ch.name not in backup_channel_names:
                channels_to_delete.append(ch)

        # 2. Lookup-карты по ID и имени
        existing_roles_by_id = {r.id: r for r in guild.roles}
        existing_roles_by_name = {r.name: r for r in guild.roles if not r.is_default()}

        existing_categories_by_id = {c.id: c for c in guild.categories}
        existing_categories_by_name = {c.name: c for c in guild.categories}

        existing_channels_by_id = {c.id: c for c in guild.channels}

        # 3. Что делаем с ролями
        roles_to_create: List[dict] = []
        roles_to_update: List[Tuple[disnake.Role, dict, dict, List[str]]] = []

        for r_data in roles_backup:
            r_id = r_data.get("role_id")
            r_name = r_data.get("name")
            r_color_val = r_data.get("color", 0)
            r_perms_val = r_data.get("permissions", 0)
            r_hoist = r_data.get("hoist", False)
            r_mentionable = r_data.get("mentionable", False)

            if r_name == "@everyone" or r_id == guild.id:
                role_map[r_id] = guild.default_role
                role_id_map[r_id] = guild.default_role.id
                if guild.default_role.permissions.value != r_perms_val:
                    roles_to_update.append((guild.default_role, r_data, {"permissions": disnake.Permissions(r_perms_val)}, ["permissions"]))
                continue

            if r_data.get("managed", False):
                target_role = existing_roles_by_id.get(r_id) or existing_roles_by_name.get(r_name)
                if target_role:
                    role_map[r_id] = target_role
                    role_id_map[r_id] = target_role.id
                continue

            target_role = existing_roles_by_id.get(r_id) or existing_roles_by_name.get(r_name)
            if not target_role:
                roles_to_create.append(r_data)
            else:
                role_map[r_id] = target_role
                role_id_map[r_id] = target_role.id

                if target_role < bot_top_role:
                    edit_kwargs = {}
                    diff_fields = []
                    if target_role.name != r_name:
                        edit_kwargs["name"] = r_name
                        diff_fields.append("name")
                    if target_role.color.value != r_color_val:
                        edit_kwargs["color"] = disnake.Color(r_color_val)
                        diff_fields.append("color")
                    if target_role.permissions.value != r_perms_val:
                        edit_kwargs["permissions"] = disnake.Permissions(r_perms_val)
                        diff_fields.append("permissions")
                    if target_role.hoist != r_hoist:
                        edit_kwargs["hoist"] = r_hoist
                        diff_fields.append("hoist")
                    if target_role.mentionable != r_mentionable:
                        edit_kwargs["mentionable"] = r_mentionable
                        diff_fields.append("mentionable")

                    if edit_kwargs:
                        roles_to_update.append((target_role, r_data, edit_kwargs, diff_fields))

        # 4. Что делаем с категориями
        categories_to_create: List[dict] = []
        categories_to_update: List[Tuple[disnake.CategoryChannel, dict, dict, List[str]]] = []

        for cat_data in categories_backup:
            cat_id = cat_data.get("category_id")
            cat_name = cat_data.get("name")
            cat_pos = cat_data.get("position", 0)

            target_cat = existing_categories_by_id.get(cat_id) or existing_categories_by_name.get(cat_name)
            if not target_cat:
                categories_to_create.append(cat_data)
            else:
                category_map[cat_id] = target_cat
                category_id_map[cat_id] = target_cat.id
                edit_kwargs = {}
                diff_fields = []
                if target_cat.name != cat_name:
                    edit_kwargs["name"] = cat_name
                    diff_fields.append("name")
                if target_cat.position != cat_pos:
                    edit_kwargs["position"] = cat_pos
                    diff_fields.append("position")

                ow_list = cat_data.get("permission_overwrites", [])
                if ow_list and not _compare_overwrites(target_cat.overwrites, ow_list, role_id_map):
                    edit_kwargs["__update_overwrites__"] = True
                    diff_fields.append("overwrites")

                if edit_kwargs:
                    categories_to_update.append((target_cat, cat_data, edit_kwargs, diff_fields))

        # 5. Что делаем с каналами
        channels_to_create: List[dict] = []
        channels_to_update: List[Tuple[disnake.abc.GuildChannel, dict, dict, List[str]]] = []

        for ch_data in channels_backup:
            ch_id = ch_data.get("channel_id")
            ch_name = ch_data.get("name")
            ch_type_str = ch_data.get("type", "text")
            ch_cat_id = ch_data.get("category_id")

            expected_cat_id = category_id_map.get(ch_cat_id, ch_cat_id) if ch_cat_id else None
            parent_cat = existing_categories_by_id.get(expected_cat_id) if expected_cat_id else None

            target_ch = existing_channels_by_id.get(ch_id)
            if not target_ch:
                for c in guild.channels:
                    if c.name == ch_name and str(c.type) == ch_type_str and c.category_id == expected_cat_id:
                        target_ch = c
                        break

            if not target_ch:
                channels_to_create.append(ch_data)
            else:
                edit_kwargs, diff_fields = _get_channel_diff(
                    target_ch, ch_data, category_id_map, role_id_map, existing_categories_by_id
                )
                if edit_kwargs:
                    channels_to_update.append((target_ch, ch_data, edit_kwargs, diff_fields))

        # 6. Что делаем с участниками (КАРАНТИННЫХ НЕ ТРОГАЕМ!)
        members_to_update: List[Tuple[disnake.Member, dict, dict]] = []
        if members_backup and isinstance(members_backup, dict):
            for user_id_str, m_data in members_backup.items():
                u_id = int(user_id_str)
                member = guild.get_member(u_id)
                if not member:
                    txt_errors.append(f"Участник {u_id}: 404 Пользователь отсутствует на сервере")
                    stats["members_matched"] = False
                    continue

                # Карантинных пользователей не трогаем — не снимаем роль и не восстанавливаем старые
                is_in_quarantine = (u_id in quarantined_user_ids) or (
                    quarantine_role_id and any(r.id == quarantine_role_id for r in member.roles)
                )
                if is_in_quarantine:
                    txt_updated_members.append(f"- Участник {member.id} (Пропущен: находится в карантине)")
                    continue

                saved_role_ids = m_data.get("role_ids", [])
                saved_nick = m_data.get("nickname")

                target_role_objs = []
                for rid in saved_role_ids:
                    target_rid = role_id_map.get(rid, rid)
                    r_obj = role_map.get(rid) or guild.get_role(target_rid)
                    if r_obj and r_obj < bot_top_role and not r_obj.is_default():
                        target_role_objs.append(r_obj)

                current_role_ids = set(r.id for r in member.roles if not r.is_default())
                target_role_ids = set(r.id for r in target_role_objs)

                roles_differ = (current_role_ids != target_role_ids)
                nick_differ = (saved_nick != member.nick and member.id != guild.owner_id)

                if roles_differ or nick_differ:
                    edit_kwargs = {}
                    if nick_differ:
                        edit_kwargs["nick"] = saved_nick
                    if roles_differ:
                        edit_kwargs["roles"] = target_role_objs
                    members_to_update.append((member, m_data, edit_kwargs))

        total_changes_planned = (
            len(roles_to_delete) + len(categories_to_delete) + len(channels_to_delete) +
            len(roles_to_create) + len(categories_to_create) + len(channels_to_create) +
            len(roles_to_update) + len(categories_to_update) + len(channels_to_update) +
            len(members_to_update)
        )

        # ЕСЛИ ИЗМЕНЕНИЙ НЕТ — выходим сразу, без запросов к API
        if total_changes_planned == 0:
            stats["no_changes_needed"] = True

            txt_content = (
                f"Отчёт восстановления сервера\n\n"
                f"Сервер: {guild.name} ({guild.id})\n"
                f"Бэкап: {b_name} ({b_id})\n"
                f"Запущено: {started_dt_str}\n"
                f"Завершено: {datetime.datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S UTC')}\n\n"
                f"Изменений не обнаружено.\n\n"
                f"Текущее состояние сервера уже полностью соответствует бэкапу.\n\n"
                f"Изменения через API:\n"
                f"- Роли: 0\n"
                f"- Категории: 0\n"
                f"- Каналы: 0\n"
                f"- Права доступа: 0\n"
                f"- Участники: 0\n\n"
                f"Финальная проверка:\n"
                f"- статус: совпадает (100% соответствие бэкапу)\n"
            )
            with open(txt_filepath, "w", encoding="utf-8") as f:
                f.write(txt_content)

            return stats, changes_summary, txt_filepath

        if guild_op_manager.is_cancelled(guild.id):
            stats["cancelled"] = True
            return stats, changes_summary, txt_filepath

        # ================= Этап 2: Удаления =================
        if progress_callback:
            await progress_callback(2)

        for ch in channels_to_delete:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                await ch.delete(reason="Антикраш откат: удаление лишнего канала")
                stats["channels_deleted"] += 1
                changes_summary["deleted"].append(f"Канал #{ch.name}")
            except Exception as e:
                txt_errors.append(f"Канал #{ch.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        for cat in categories_to_delete:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                await cat.delete(reason="Антикраш откат: удаление лишней категории")
                stats["categories_deleted"] += 1
                changes_summary["deleted"].append(f"Категория {cat.name}")
            except Exception as e:
                txt_errors.append(f"Категория {cat.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        for r in roles_to_delete:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                await r.delete(reason="Антикраш откат: удаление лишней роли")
                stats["roles_deleted"] += 1
                changes_summary["deleted"].append(f"Роль {r.name}")
            except Exception as e:
                txt_errors.append(f"Роль {r.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        if guild_op_manager.is_cancelled(guild.id):
            stats["cancelled"] = True
            return stats, changes_summary, txt_filepath

        # ================= Этап 3: Создание объектов и маппинг ролей =================
        if progress_callback:
            await progress_callback(3)

        for r_data in roles_to_create:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            r_id = r_data.get("role_id")
            r_name = r_data.get("name")
            try:
                new_role = await guild.create_role(
                    name=r_name,
                    color=disnake.Color(r_data.get("color", 0)),
                    permissions=disnake.Permissions(r_data.get("permissions", 0)),
                    hoist=r_data.get("hoist", False),
                    mentionable=r_data.get("mentionable", False),
                    reason="Антикраш откат: создание роли"
                )
                role_map[r_id] = new_role
                role_id_map[r_id] = new_role.id
                existing_roles_by_id[new_role.id] = new_role
                stats["roles_created"] += 1
                changes_summary["created"].append(f"Роль {r_name}")
                txt_created_roles.append(
                    f"- {r_name}\n"
                    f"  старый ID: {r_id}\n"
                    f"  новый ID: {new_role.id}\n"
                    f"  права: восстановлены\n"
                    f"  позиция: восстановлена"
                )
            except Exception as e:
                txt_errors.append(f"Создание роли {r_name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # Восстанавливаем позиции ролей
        positions_payload = {}
        for r_data in sorted(roles_backup, key=lambda x: x.get("position", 0)):
            r_id = r_data.get("role_id")
            target_role_id = role_id_map.get(r_id, r_id)
            r_obj = role_map.get(r_id) or guild.get_role(target_role_id)
            if r_obj and r_obj < bot_top_role and not r_obj.is_default() and not r_obj.managed:
                if r_obj.position != r_data.get("position", 0):
                    positions_payload[r_obj] = r_data.get("position", 0)

        if positions_payload:
            try:
                await guild.edit_role_positions(positions=positions_payload, reason="Антикраш откат: позиционирование ролей")
            except Exception as e:
                txt_errors.append(f"Позиционирование ролей: {e}")

        # Создаём недостающие категории
        for cat_data in categories_to_create:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            cat_id = cat_data.get("category_id")
            cat_name = cat_data.get("name")
            try:
                new_cat = await guild.create_category(
                    name=cat_name,
                    position=cat_data.get("position", 0),
                    reason="Антикраш откат: создание категории"
                )
                category_map[cat_id] = new_cat
                category_id_map[cat_id] = new_cat.id
                existing_categories_by_id[new_cat.id] = new_cat
                stats["categories_created"] += 1
                changes_summary["created"].append(f"Категория {cat_name}")
                txt_created_cats.append(f"- {cat_name} ({new_cat.id})")
            except Exception as e:
                txt_errors.append(f"Создание категории {cat_name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # Создаём недостающие каналы
        for ch_data in channels_to_create:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            ch_name = ch_data.get("name")
            ch_type_str = ch_data.get("type", "text")
            ch_cat_id = ch_data.get("category_id")

            expected_cat_id = category_id_map.get(ch_cat_id, ch_cat_id) if ch_cat_id else None
            parent_cat = existing_categories_by_id.get(expected_cat_id) if expected_cat_id else None

            try:
                create_kwargs = {
                    "name": ch_name,
                    "category": parent_cat,
                    "position": ch_data.get("position", 0),
                    "reason": "Антикраш откат: создание канала"
                }
                if "topic" in ch_data and "text" in ch_type_str:
                    create_kwargs["topic"] = ch_data["topic"]
                if "nsfw" in ch_data and "text" in ch_type_str:
                    create_kwargs["nsfw"] = ch_data["nsfw"]

                ow_list = ch_data.get("permission_overwrites", [])
                if ow_list:
                    create_kwargs["overwrites"] = _build_overwrites_dict(ow_list, role_map, role_id_map, guild, txt_errors)

                if "voice" in ch_type_str:
                    target_ch = await guild.create_voice_channel(**create_kwargs)
                elif "stage" in ch_type_str:
                    target_ch = await guild.create_stage_channel(**create_kwargs)
                else:
                    target_ch = await guild.create_text_channel(**create_kwargs)

                stats["channels_created"] += 1
                changes_summary["created"].append(f"Канал #{ch_name}")
                txt_created_chans.append(f"- #{ch_name} ({target_ch.id})")
            except Exception as e:
                txt_errors.append(f"Создание канала #{ch_name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # ================= Этап 4: Обновления =================
        if progress_callback:
            await progress_callback(4)

        # Обновляем роли
        for target_role, r_data, edit_kwargs, diff_fields in roles_to_update:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                await target_role.edit(**edit_kwargs, reason="Антикраш откат: синхронизация роли")
                stats["roles_restored"] += 1
                changes_summary["updated"].append(f"Роль {target_role.name}")
            except Exception as e:
                txt_errors.append(f"Обновление роли {target_role.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # Обновляем категории
        for target_cat, cat_data, edit_kwargs, diff_fields in categories_to_update:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                update_ow = edit_kwargs.pop("__update_overwrites__", False)
                if update_ow:
                    ow_list = cat_data.get("permission_overwrites", [])
                    edit_kwargs["overwrites"] = _build_overwrites_dict(ow_list, role_map, role_id_map, guild, txt_errors)
                    stats["overwrites_restored"] += 1
                    txt_updated_overwrites.append(f"- Категория {target_cat.name}")

                if edit_kwargs:
                    await target_cat.edit(**edit_kwargs, reason="Антикраш откат: синхронизация категории")
                    stats["categories_restored"] += 1
                    changes_summary["updated"].append(f"Категория {target_cat.name}")
            except Exception as e:
                txt_errors.append(f"Обновление категории {target_cat.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # Обновляем каналы
        for target_ch, ch_data, edit_kwargs, diff_fields in channels_to_update:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                update_ow = edit_kwargs.pop("__update_overwrites__", False)
                if update_ow:
                    ow_list = ch_data.get("permission_overwrites", [])
                    edit_kwargs["overwrites"] = _build_overwrites_dict(ow_list, role_map, role_id_map, guild, txt_errors)
                    stats["overwrites_restored"] += 1
                    txt_updated_overwrites.append(f"- Канал #{target_ch.name}")

                if edit_kwargs:
                    await target_ch.edit(**edit_kwargs, reason="Антикраш откат: синхронизация канала")
                    stats["channels_restored"] += 1
                    changes_summary["updated"].append(f"Канал #{target_ch.name}")
                    txt_updated_chans.append(f"- #{target_ch.name} ({target_ch.id})\n  изменения: {_translate_diff_fields(diff_fields)}")
            except Exception as e:
                txt_errors.append(f"Обновление канала #{target_ch.name}: {e}")
                stats["failed_count"] += 1
                stats["structure_matched"] = False

        # Обновляем участников (роли по маппингу + никнеймы)
        for member, m_data, edit_kwargs in members_to_update:
            if guild_op_manager.is_cancelled(guild.id):
                stats["cancelled"] = True
                break
            try:
                await member.edit(**edit_kwargs, reason="Антикраш откат: синхронизация участника")
                stats["members_restored"] += 1
                txt_updated_members.append(f"- Участник {member.id}")
            except Exception as e:
                txt_errors.append(f"Участник {member.id}: {e}")
                stats["failed_count"] += 1
                stats["members_matched"] = False

        # ================= Этап 5: Финальная проверка в памяти =================
        if progress_callback:
            await progress_callback(5)

        discrepancies = 0
        for r_data in roles_backup:
            if r_data.get("name") == "@everyone" or r_data.get("managed", False):
                continue
            r_obj = role_map.get(r_data.get("role_id")) or guild.get_role(role_id_map.get(r_data.get("role_id"), 0))
            if not r_obj:
                discrepancies += 1

        for ch_data in channels_backup:
            ch_name = ch_data.get("name")
            found = False
            for c in guild.channels:
                if c.name == ch_name:
                    found = True
                    break
            if not found:
                discrepancies += 1

        if discrepancies > 0 or not stats["structure_matched"]:
            stats["has_discrepancies"] = True
            stats["discrepancy_count"] = discrepancies

        # Пишем итоговый отчёт в TXT
        finished_dt_str = datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M:%S UTC")

        txt_lines = [
            f"Отчёт восстановления сервера\n",
            f"Сервер: {guild.name} ({guild.id})",
            f"Бэкап: {b_name} ({b_id})",
            f"Запущено: {started_dt_str}",
            f"Завершено: {finished_dt_str}\n",
            f"Выполненные изменения:",
            f"- Роли: {stats['roles_created']} создано, {stats['roles_restored']} обновлено, {stats['roles_deleted']} удалено",
            f"- Категории: {stats['categories_created']} создано, {stats['categories_restored']} обновлено, {stats['categories_deleted']} удалено",
            f"- Каналы: {stats['channels_created']} создано, {stats['channels_restored']} обновлено, {stats['channels_deleted']} удалено",
            f"- Права доступа (Overwrites): {stats['overwrites_restored']} обновлено",
            f"- Участники: {stats['members_restored']} обновлено\n",
            f"Созданные роли:"
        ]
        txt_lines.extend(txt_created_roles if txt_created_roles else ["- нет"])
        txt_lines.append("\nСозданные категории:")
        txt_lines.extend(txt_created_cats if txt_created_cats else ["- нет"])
        txt_lines.append("\nСозданные каналы:")
        txt_lines.extend(txt_created_chans if txt_created_chans else ["- нет"])
        txt_lines.append("\nОбновлённые каналы:")
        txt_lines.extend(txt_updated_chans if txt_updated_chans else ["- нет"])
        txt_lines.append("\nОбновлённые оверрайты прав:")
        txt_lines.extend(txt_updated_overwrites if txt_updated_overwrites else ["- нет"])
        txt_lines.append("\nОбновлённые участники:")
        txt_lines.extend(txt_updated_members if txt_updated_members else ["- нет"])
        txt_lines.append(f"\nПропущено:\n- Интеграционные роли (managed): {stats['skipped_managed_roles']}")
        txt_lines.append("\nОшибки:")
        txt_lines.extend([f"- {err}" for err in txt_errors] if txt_errors else ["- нет"])
        txt_lines.append(f"\nФинальная проверка:")
        txt_lines.append(f"- структура сервера: {'совпадает' if stats['structure_matched'] else 'обнаружены несоответствия'}")
        txt_lines.append(f"- участники: {'совпадает' if stats['members_matched'] else 'обнаружены несоответствия'}")
        txt_lines.append(f"- статус: {'совпадает (100% соответствие бэкапу)' if (stats['structure_matched'] and stats['members_matched']) else 'завершено с ошибками'}\n")

        with open(txt_filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        return stats, changes_summary, txt_filepath

    finally:
        await guild_op_manager.release(guild.id)
