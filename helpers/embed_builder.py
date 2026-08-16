import datetime
import disnake
from typing import Optional, List, Dict
from database import crud
from database.models import GuildSettings, Whitelist, AttackLog, QuarantineUser
from helpers.time_formatter import format_relative_time


def build_settings_embed(
    settings: GuildSettings,
    whitelist_count: int,
    last_log: Optional[AttackLog] = None
) -> disnake.Embed:
    """Строит основной эмбед /anticrash настроек — без заголовка, цвет None."""
    embed = disnake.Embed(color=None)

    status_str = "ВКЛЮЧЕН" if settings.anticrash_enabled else "ВЫКЛЮЧЕН"
    mode_str = "Карантин" if settings.punishment_mode == "quarantine" else "Бан"
    q_role_str = f"<@&{settings.quarantine_role_id}>" if settings.quarantine_role_id else "Не настроена"
    notif_chan_str = f"<#{settings.notification_channel_id}>" if getattr(settings, "notification_channel_id", None) else "Не настроен"
    notif_status_str = "ВКЛЮЧЕНЫ" if getattr(settings, "notifications_enabled", True) else "ВЫКЛЮЧЕНЫ"

    if last_log:
        last_action_str = (
            f"<@{last_log.user_id}> - {last_log.action} - {format_relative_time(last_log.created_at)}"
        )
    else:
        last_action_str = "Нет зарегистрированных действий"

    description = (
        f"Состояние антикраша: {status_str}\n"
        f"Режим наказания: {mode_str}\n"
        f"Роль карантина: {q_role_str}\n"
        f"Канал уведомлений: {notif_chan_str}\n"
        f"Статус уведомлений: {notif_status_str}\n"
        f"Заблокировано атак: {settings.total_blocked_attacks}\n"
        f"Объектов в белом списке: {whitelist_count}\n\n"
        f"Последнее действие: {last_action_str}"
    )

    embed.description = description
    return embed


def build_whitelist_embed(
    entries: List[Whitelist],
    page: int,
    total_pages: int
) -> disnake.Embed:
    """Эмбед белого списка, по 10 записей на страницу."""
    embed = disnake.Embed(color=None)
    embed.title = "Белый список"

    if not entries:
        embed.description = "В белом списке пока нет ни одного пользователя или роли."
    else:
        lines = []
        for entry in entries:
            date_formatted = format_relative_time(entry.added_at)
            if getattr(entry, "is_role", False):
                lines.append(f"<@&{entry.user_id}> (Роль) - {date_formatted}")
            else:
                lines.append(f"<@{entry.user_id}> - {date_formatted}")
        
        embed.description = "\n".join(lines)
    
    embed.set_footer(text=f"Страница {page} из {total_pages}")
    return embed


def build_action_logs_embed(
    logs: List[AttackLog],
    page: int,
    total_pages: int,
    user_filter_id: Optional[int] = None,
    category_filters: Optional[List[str]] = None,
    time_label: Optional[str] = None
) -> disnake.Embed:
    """Эмбед журнала действий — по 5 записей на страницу, с шапкой активных фильтров."""
    embed = disnake.Embed(color=None)
    embed.title = "Журнал действий"

    filter_info = []
    if user_filter_id:
        filter_info.append(f"Пользователь: <@{user_filter_id}>")
    if category_filters:
        cat_names = [crud.PERMISSION_LABELS.get(c, c) for c in category_filters]
        filter_info.append(f"Категории: {', '.join(cat_names)}")
    if time_label:
        filter_info.append(f"Период: {time_label}")

    header = ""
    if filter_info:
        header = "Активные фильтры:\n" + "\n".join(f"• {f}" for f in filter_info) + "\n\n"

    if not logs:
        embed.description = header + "Записи в журнале действий по выбранным фильтрам отсутствуют."
    else:
        lines = []
        for log in logs:
            date_formatted = format_relative_time(log.created_at)
            status = "Разрешено" if log.allowed else "Заблокировано"
            lines.append(
                f"<@{log.user_id}> - {date_formatted} - {log.action} - {status} [ID: {log.log_id}]"
            )
        embed.description = header + "\n\n".join(lines)

    embed.set_footer(text=f"Страница {page} из {total_pages}")
    return embed


def build_quarantine_embed(
    quarantine_list: List[QuarantineUser],
    page: int,
    total_pages: int
) -> disnake.Embed:
    """Эмбед списка карантина, по 5 пользователей на страницу."""
    embed = disnake.Embed(color=None)
    embed.title = "Пользователи в карантине"

    if not quarantine_list:
        embed.description = "В данный момент нет пользователей в карантине."
    else:
        lines = []
        for q in quarantine_list:
            date_formatted = format_relative_time(q.quarantined_at)
            log_str = f" [ID Лога: `{q.log_id}`]" if q.log_id else ""
            lines.append(
                f"<@{q.user_id}> (ID: `{q.user_id}`){log_str} - {date_formatted}\nПричина: {q.reason}"
            )
        embed.description = "\n\n".join(lines)

    embed.set_footer(text=f"Страница {page} из {total_pages}")
    return embed


def build_user_whitelist_info_embed(
    guild: Optional[disnake.Guild] = None,
    target_id: int = 0,
    permissions: Optional[dict] = None,
    is_whitelisted: bool = False,
    is_role: bool = False
) -> disnake.Embed:
    """Показывает права пользователя/роли в белом списке. Пингов в заголовке нет."""
    if permissions is None:
        permissions = {}

    embed = disnake.Embed(color=None)
    embed.title = "Информация о правах в белом списке"

    if is_role:
        role = guild.get_role(target_id) if guild else None
        target_mention = f"Роль: <@&{target_id}>" if not role else f"Роль: <@&{target_id}> (`@{role.name}`)"
    else:
        member = guild.get_member(target_id) if guild else None
        target_mention = f"Участник: <@{target_id}>" if not member else f"Участник: <@{target_id}> (`@{member.name}`)"

    status = "В белом списке" if is_whitelisted else "Не в белом списке"

    embed.description = f"{target_mention}\nСтатус: **{status}**\n\nРазрешенные действия:\n"

    from database.crud import PERMISSION_LABELS
    rights_lines = []
    for key, label in PERMISSION_LABELS.items():
        enabled = permissions.get(key, False)
        perm_status = "Разрешено" if enabled else "Запрещено"
        rights_lines.append(f"• {label}: **{perm_status}**")

    embed.description += "\n".join(rights_lines)
    return embed


def build_backup_menu_embed(setting) -> disnake.Embed:
    """Главный эмбед меню управления бэкапами."""
    embed = disnake.Embed(color=None)
    embed.title = "Управление бэкапами сервера"

    last_time_str = format_relative_time(setting.last_backup_at) if setting.last_backup_at else "Не выполнялся"
    last_name_str = setting.last_backup_name or "—"
    
    if setting.last_backup_type == "auto":
        last_type_str = "Автоматический"
    elif setting.last_backup_type == "manual":
        last_type_str = "Принудительный"
    else:
        last_type_str = "—"

    storage_str = setting.storage_type or "Database"
    
    if setting.auto_backup_interval and setting.auto_backup_interval != "0":
        auto_status_str = f"Включен ({setting.auto_backup_interval})"
    else:
        auto_status_str = "Отключен"

    save_mem_str = "Включено" if setting.save_members else "Выключено"

    embed.description = (
        f"**Информация о бэкапах:**\n\n"
        f"• **Время последнего бэкапа:** {last_time_str}\n"
        f"• **Название последнего бэкапа:** `{last_name_str}`\n"
        f"• **Тип последнего бэкапа:** {last_type_str}\n"
        f"• **Место хранения:** {storage_str}\n"
        f"• **Статус авто-бэкапа:** {auto_status_str}\n"
        f"• **Сохранение пользователей:** {save_mem_str}"
    )
    return embed


def build_backup_result_embed(result: dict, success: bool) -> disnake.Embed:
    """Эмбед с результатом создания бэкапа (ручного или авто)."""
    embed = disnake.Embed(color=None)
    
    if success:
        embed.title = "Бэкап сервера успешно создан"
        b_type = "Автоматический" if result.get("backup_type") == "auto" else "Принудительный"
        size_bytes = result.get("size_bytes", 0)
        if size_bytes > 1024 * 1024:
            size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{size_bytes / 1024:.2f} KB"

        duration_ms = result.get("duration_ms", 0)
        duration_str = f"{duration_ms / 1000:.2f} сек." if duration_ms >= 1000 else f"{duration_ms} мс"

        embed.description = (
            f"• **Название бэкапа:** `{result.get('backup_name')}`\n"
            f"• **ID Бэкапа:** `{result.get('backup_id')}`\n"
            f"• **Дата создания:** {datetime.datetime.utcnow().strftime('%d.%m.%Y %H:%M:%S UTC')}\n"
            f"• **Тип:** {b_type}\n"
            f"• **Хранилище:** {result.get('storage')}\n"
            f"• **Размер JSON:** {size_str}\n"
            f"• **Статус:** Успешно\n"
            f"• **Время выполнения:** {duration_str}"
        )
    else:
        embed.title = "Ошибка при создании бэкапа"
        embed.description = (
            f"• **Статус:** Ошибка\n"
            f"• **Причина:** {result.get('error', 'Неизвестная ошибка')}"
        )

    return embed


def build_backup_detail_embed(meta: dict, snapshot: dict) -> disnake.Embed:
    """Детальный эмбед для просмотра конкретного снимка бэкапа."""
    embed = disnake.Embed(color=None)
    embed.title = f"Детали бэкапа: {meta.get('backup_name')}"

    size_b = meta.get("size_bytes", 0)
    if size_b > 1024 * 1024:
        size_str = f"{size_b / (1024 * 1024):.2f} MB"
    else:
        size_str = f"{size_b / 1024:.2f} KB"

    c_at = meta.get("created_at")
    if isinstance(c_at, datetime.datetime):
        date_str = c_at.strftime("%d.%m.%Y %H:%M:%S UTC")
    else:
        date_str = str(c_at)

    b_type = "Автоматический" if meta.get("backup_type") == "auto" else "Принудительный"
    has_members = "members" in snapshot and bool(snapshot["members"])
    members_count = len(snapshot["members"]) if has_members and isinstance(snapshot["members"], dict) else 0

    roles_cnt = len(snapshot.get("roles", []))
    cats_cnt = len(snapshot.get("categories", []))
    chans_cnt = len(snapshot.get("channels", []))

    embed.description = (
        f"• **Название:** `{meta.get('backup_name')}`\n"
        f"• **ID Бэкапа:** `{meta.get('backup_id')}`\n"
        f"• **Дата:** {date_str}\n"
        f"• **Тип:** {b_type}\n"
        f"• **Хранилище:** {meta.get('storage')}\n"
        f"• **Размер:** {size_str}\n"
        f"• **Сохранение пользователей:** {'Да' if has_members else 'Нет'}\n\n"
        f"**Содержимое снимка:**\n"
        f"• **Роли:** {roles_cnt}\n"
        f"• **Категории:** {cats_cnt}\n"
        f"• **Каналы:** {chans_cnt}\n"
        f"• **Сохранённых пользователей:** {members_count}"
    )
    return embed


def build_restore_stage_embed(stage: int, backup_name: str) -> disnake.Embed:
    """Живой прогресс-эмбед восстановления сервера, без эмодзи."""
    embed = disnake.Embed(color=None)
    embed.title = f"Процесс восстановления сервера: {backup_name}"

    def get_status(target_stage: int) -> str:
        if stage == target_stage:
            return "[Выполняется]"
        elif stage > target_stage:
            return "[Завершено]"
        else:
            return "[Ожидание]"

    s1 = f"{get_status(1)} Проверка бэкапа"
    s2 = f"{get_status(2)} Сверка данных"
    s3 = f"{get_status(3)} Очистка лишних объектов"
    s4 = f"{get_status(4)} Восстановление объектов"
    s5 = f"{get_status(5)} Финальная проверка"

    embed.description = f"{s1}\n{s2}\n{s3}\n{s4}\n{s5}"
    return embed


def build_restore_report_embed(
    stats: dict,
    changes_summary: Optional[Dict[str, List[str]]] = None,
    txt_filename: Optional[str] = None
) -> disnake.Embed:
    """Итоговый эмбед-отчёт по восстановлению — чистый и лаконичный."""
    embed = disnake.Embed(color=None)
    embed.title = "Результат восстановления"

    has_errors = stats.get("failed_count", 0) > 0 or not stats.get("members_matched", True) or not stats.get("structure_matched", True)

    if stats.get("cancelled"):
        status_str = "остановлено пользователем"
    elif stats.get("no_changes_needed"):
        status_str = "без изменений"
    elif has_errors:
        status_str = "завершено с ошибками"
    else:
        status_str = "завершено"

    lines = [f"**Статус:** {status_str}\n"]

    struct_str = "соответствует" if stats.get("structure_matched", True) else "есть несоответствия"
    mem_str = "соответствует" if stats.get("members_matched", True) else "есть несоответствия"

    lines.append(f"• **Структура сервера:** {struct_str}")
    lines.append(f"• **Участники:** {mem_str}\n")

    if stats.get("no_changes_needed"):
        lines.append("Состояние сервера уже соответствует backup.\n")
    else:
        lines.append("**Изменения:**")
        lines.append(f"Каналы: {stats.get('channels_created', 0)} создано, {stats.get('channels_restored', 0)} изменено, {stats.get('channels_deleted', 0)} удалено")
        lines.append(f"Категории: {stats.get('categories_created', 0)} создано, {stats.get('categories_restored', 0)} изменено, {stats.get('categories_deleted', 0)} удалено")
        lines.append(f"Роли: {stats.get('roles_created', 0)} создано, {stats.get('roles_restored', 0)} изменено, {stats.get('roles_deleted', 0)} удалено")
        lines.append(f"Permissions: {stats.get('overwrites_restored', 0)} изменено")
        lines.append(f"Участники: {stats.get('members_restored', 0)} изменено\n")

    lines.append(f"**Ошибки:** {stats.get('failed_count', 0)}\n")

    if txt_filename:
        lines.append(f"**Подробный отчёт:**\n`{txt_filename}`")

    embed.description = "\n".join(lines)
    return embed
