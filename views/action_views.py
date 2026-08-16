import re
import logging
import datetime
import disnake
from typing import Optional, List
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_action_logs_embed
from helpers.time_formatter import format_relative_time
from views.base_view import BaseTimeoutView, is_full_admin

logger = logging.getLogger("ActionViews")


def parse_time_string(time_str: str) -> Optional[datetime.timedelta]:
    time_str = time_str.strip().lower()
    if not time_str:
        return None

    match = re.match(r"^(\d+)\s*([a-zа-я]+)?$", time_str)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2) or "m"

    if unit in ("s", "sec", "сек", "секунд", "секунды"):
        return datetime.timedelta(seconds=value)
    elif unit in ("m", "min", "мин", "минут", "минуты"):
        return datetime.timedelta(minutes=value)
    elif unit in ("h", "hr", "ч", "час", "часа", "часов"):
        return datetime.timedelta(hours=value)
    elif unit in ("d", "д", "день", "дня", "дней"):
        return datetime.timedelta(days=value)
    elif unit in ("w", "н", "нед", "неделя", "недели", "недель"):
        return datetime.timedelta(weeks=value)
    elif unit in ("mo", "mon", "мес", "месяц", "месяца", "месяцев"):
        return datetime.timedelta(days=value * 30)
    else:
        if unit.startswith("m"):
            return datetime.timedelta(minutes=value)
        return None


class SearchLogModal(disnake.ui.Modal):
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        components = [
            disnake.ui.TextInput(
                label="Введите ID лога (например, LOG-A1B2C3D4)",
                placeholder="LOG-XXXXXXXX",
                custom_id="log_id_input",
                style=disnake.TextInputStyle.short,
                min_length=5,
                max_length=32,
                required=True
            )
        ]
        super().__init__(
            title="Поиск записи в журнале по ID",
            custom_id="search_log_modal",
            components=components
        )

    async def callback(self, interaction: disnake.ModalInteraction):
        input_val = interaction.text_values["log_id_input"].strip()

        # если ввели числовой ID пользователя
        if input_val.isdigit():
            target_user_id = int(input_val)
            async with AsyncSessionLocal() as session:
                user_logs = await crud.get_attack_logs(session, self.guild_id, user_id=target_user_id, limit=20)

            if not user_logs:
                await interaction.response.send_message(
                    f"Записи в журнале для пользователя с ID **{target_user_id}** не найдены.",
                    ephemeral=True
                )
                return

            embed = disnake.Embed(color=None)
            embed.title = f"Записи пользователя (ID: {target_user_id})"
            lines = []
            for log in user_logs[:10]:
                date_formatted = format_relative_time(log.created_at)
                status = "Разрешено" if log.allowed else "Заблокировано"
                lines.append(f"`{log.log_id}` - {date_formatted} - {log.action} - {status}")

            embed.description = f"Пользователь: <@{target_user_id}>\nНайдено записей: {len(user_logs)}\n\n" + "\n".join(lines)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        log_id_val = input_val.upper()
        async with AsyncSessionLocal() as session:
            log_entry = await crud.get_attack_log_by_id(session, self.guild_id, log_id_val)

        if not log_entry:
            await interaction.response.send_message(
                f"Запись с ID **{log_id_val}** не найдена в базе данных.",
                ephemeral=True
            )
            return

        embed = disnake.Embed(color=None)
        embed.title = f"Детали записи {log_entry.log_id}"
        status = "Разрешено" if log_entry.allowed else "Заблокировано / Применено наказание"
        embed.description = (
            f"Пользователь: <@{log_entry.user_id}> (ID: {log_entry.user_id})\n"
            f"Действие: {log_entry.action}\n"
            f"Дата: {format_relative_time(log_entry.created_at)}\n"
            f"Статус: {status}\n\n"
            f"{log_entry.details or 'Подробности отсутствуют'}"
        )

        details_text = log_entry.details or ""
        img_match = re.search(r"(https?://\S+\.(?:png|jpg|jpeg|gif|webp)(?:\?\S+)?)", details_text, re.IGNORECASE)
        if not img_match:
            img_match = re.search(r"(https?://media\.discordapp\.net/\S+)", details_text, re.IGNORECASE)
            if not img_match:
                img_match = re.search(r"(https?://cdn\.discordapp\.com/attachments/\S+)", details_text, re.IGNORECASE)

        if img_match:
            embed.set_image(url=img_match.group(1))

        await interaction.response.send_message(embed=embed, ephemeral=True)


class TimeFilterModal(disnake.ui.Modal):
    def __init__(self, action_logs_view: "ActionLogsView"):
        self.action_logs_view = action_logs_view
        components = [
            disnake.ui.TextInput(
                label="Введите период (напр. 30m, 2h, 1w, 1mo)",
                placeholder="Примеры: 30m (минуты), 2h (часы), 1d (дни), 1w (недели)",
                custom_id="time_input",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=20,
                required=True
            )
        ]
        super().__init__(
            title="Фильтр логов по времени",
            custom_id="time_filter_modal",
            components=components
        )

    async def callback(self, interaction: disnake.ModalInteraction):
        time_str = interaction.text_values["time_input"].strip()
        delta = parse_time_string(time_str)

        if not delta:
            await interaction.response.send_message(
                f"Неверный формат времени: **{time_str}**.\nИспользуйте форматы: `30m` (минуты), `2h` (часы), `1d` (дни), `1w` (недели), `1mo` (месяцы).",
                ephemeral=True
            )
            return

        # тихий defer, чтобы обработать ответ модалки без лишнего шума
        await interaction.response.defer(ephemeral=True)

        now = datetime.datetime.utcnow()
        self.action_logs_view.since_datetime = now - delta
        self.action_logs_view.time_label = f"последние {time_str}"
        self.action_logs_view.page = 1

        logs, page_logs, total_pages = await self.action_logs_view.get_logs_data()
        await self.action_logs_view.build_view_components(total_pages)

        embed = build_action_logs_embed(
            logs=page_logs,
            page=self.action_logs_view.page,
            total_pages=total_pages,
            user_filter_id=self.action_logs_view.user_filter_id,
            category_filters=self.action_logs_view.category_filters,
            time_label=self.action_logs_view.time_label
        )

        target_msg = self.action_logs_view.main_message or interaction.message
        if target_msg:
            try:
                await target_msg.edit(embed=embed, view=self.action_logs_view)
            except Exception:
                pass

        # удаляем пустой эфемерный ответ
        try:
            await interaction.delete_original_response()
        except Exception:
            pass


class ActionLogsCategorySelect(disnake.ui.StringSelect):
    def __init__(self, active_categories: List[str]):
        options = []
        for key, label in crud.PERMISSION_LABELS.items():
            is_def = key in active_categories
            options.append(
                disnake.SelectOption(
                    label=label,
                    value=key,
                    description=f"Категория: {label}",
                    default=is_def
                )
            )

        super().__init__(
            placeholder="Фильтр по категориям (выберите категории)",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        view: ActionLogsView = self.view
        view.category_filters = self.values or []
        view.page = 1
        await view.update_message(interaction)


class ActionLogsView(BaseTimeoutView):
    def __init__(
        self,
        guild_id: int,
        author_id: int,
        page: int = 1,
        user_filter_id: Optional[int] = None,
        category_filters: Optional[List[str]] = None,
        since_datetime: Optional[datetime.datetime] = None,
        time_label: Optional[str] = None
    ):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.page = page
        self.user_filter_id = user_filter_id
        self.category_filters = category_filters or []
        self.since_datetime = since_datetime
        self.time_label = time_label
        self.items_per_page = 5
        self.main_message: Optional[disnake.Message] = None

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id

        # проверяем: автор, владелец или разраб
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        # проверяем доверенный статус
        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, user_id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="У вас нет прав на использование этой панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    async def get_logs_data(self):
        async with AsyncSessionLocal() as session:
            logs = await crud.get_attack_logs(
                session=session,
                guild_id=self.guild_id,
                user_id=self.user_filter_id,
                categories=self.category_filters,
                since_datetime=self.since_datetime,
                limit=300
            )

        total_items = len(logs)
        total_pages = max(1, (total_items + self.items_per_page - 1) // self.items_per_page)

        if self.page > total_pages:
            self.page = total_pages
        if self.page < 1:
            self.page = 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_logs = logs[start_idx:end_idx]

        return logs, page_logs, total_pages

    async def build_view_components(self, total_pages: int):
        self.clear_items()

        # строка 0: фильтр по пользователю
        user_select = disnake.ui.UserSelect(
            placeholder="Фильтр логов по пользователю",
            min_values=1,
            max_values=1,
            row=0
        )
        user_select.callback = self.on_user_filter_selected
        self.add_item(user_select)

        # строка 1: выбор категорий
        self.add_item(ActionLogsCategorySelect(self.category_filters))

        # строка 2: кнопки действий и фильтров
        time_button = disnake.ui.Button(
            label="По времени", style=disnake.ButtonStyle.primary, row=2
        )
        time_button.callback = self.on_time_filter
        self.add_item(time_button)

        search_button = disnake.ui.Button(
            label="Поиск по ID лога", style=disnake.ButtonStyle.secondary, row=2
        )
        search_button.callback = self.on_search_by_id
        self.add_item(search_button)

        has_active_filters = bool(self.user_filter_id or self.category_filters or self.since_datetime)
        if has_active_filters:
            reset_button = disnake.ui.Button(
                label="Очистить фильтры", style=disnake.ButtonStyle.danger, row=2
            )
            reset_button.callback = self.on_reset_filter
            self.add_item(reset_button)

        # строка 3: пагинация и навигация
        prev_button = disnake.ui.Button(
            label="<- Назад", style=disnake.ButtonStyle.secondary, disabled=(self.page <= 1), row=3
        )
        prev_button.callback = self.on_prev_page
        self.add_item(prev_button)

        next_button = disnake.ui.Button(
            label="Вперед ->", style=disnake.ButtonStyle.secondary, disabled=(self.page >= total_pages), row=3
        )
        next_button.callback = self.on_next_page
        self.add_item(next_button)

        home_button = disnake.ui.Button(
            label="Главное меню", style=disnake.ButtonStyle.primary, row=3
        )
        home_button.callback = self.on_home
        self.add_item(home_button)

    async def update_message(self, interaction: disnake.MessageInteraction):
        if interaction.message:
            self.message = interaction.message
            self.main_message = interaction.message

        logs, page_logs, total_pages = await self.get_logs_data()
        await self.build_view_components(total_pages)

        embed = build_action_logs_embed(
            logs=page_logs,
            page=self.page,
            total_pages=total_pages,
            user_filter_id=self.user_filter_id,
            category_filters=self.category_filters,
            time_label=self.time_label
        )

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_prev_page(self, interaction: disnake.MessageInteraction):
        self.page -= 1
        await self.update_message(interaction)

    async def on_next_page(self, interaction: disnake.MessageInteraction):
        self.page += 1
        await self.update_message(interaction)

    async def on_user_filter_selected(self, interaction: disnake.MessageInteraction):
        selected_user_id = int(interaction.data.values[0])
        self.user_filter_id = selected_user_id
        self.page = 1
        await self.update_message(interaction)

    async def on_reset_filter(self, interaction: disnake.MessageInteraction):
        self.user_filter_id = None
        self.category_filters = []
        self.since_datetime = None
        self.time_label = None
        self.page = 1
        await self.update_message(interaction)

    async def on_time_filter(self, interaction: disnake.MessageInteraction):
        if interaction.message:
            self.main_message = interaction.message
        modal = TimeFilterModal(self)
        await interaction.response.send_modal(modal)

    async def on_search_by_id(self, interaction: disnake.MessageInteraction):
        if interaction.message:
            self.main_message = interaction.message
        modal = SearchLogModal(self.guild_id)
        await interaction.response.send_modal(modal)

    async def on_home(self, interaction: disnake.MessageInteraction):
        from views.main_view import MainAnticrashView
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        from helpers.embed_builder import build_settings_embed
        embed = build_settings_embed(settings, len(wl), last_log)
        view = MainAnticrashView(self.guild_id, interaction.author.id, settings.anticrash_enabled)
        view.message = interaction.message
        await interaction.response.edit_message(embed=embed, view=view)
