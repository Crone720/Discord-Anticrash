import logging
import disnake
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_settings_embed
from views.base_view import BaseTimeoutView, is_full_admin

logger = logging.getLogger("SettingsView")


async def safe_edit_message(interaction: disnake.MessageInteraction, embed: disnake.Embed, view: disnake.ui.View):
    """Редактирует ответ на взаимодействие без падения на 404, если ответ уже устарел или БД тормозит."""
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
    except disnake.errors.NotFound:
        try:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Error in safe_edit_message: {e}")
        try:
            await interaction.edit_original_response(embed=embed, view=view)
        except Exception:
            pass


class SetTelegramIdModal(disnake.ui.Modal):
    def __init__(self, guild_id: int, parent_view: "NotificationsView"):
        self.guild_id = guild_id
        self.parent_view = parent_view

        components = [
            disnake.ui.TextInput(
                label="Telegram Chat ID / User ID",
                placeholder="Например: 7775862048 или -100123456789",
                custom_id="tg_chat_id_input",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=32
            )
        ]
        super().__init__(
            title="Укажите Telegram Chat ID",
            custom_id=f"set_tg_id_modal_{guild_id}",
            components=components
        )

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_full_admin(inter.author.id, inter.guild):
            await inter.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        raw_val = inter.text_values.get("tg_chat_id_input", "").strip()
        try:
            chat_id = int(raw_val)
        except ValueError:
            await inter.response.send_message("Ошибка: Введен невалидный ID. Ожидалось целое число.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            updated_settings = await crud.update_guild_settings(session, self.guild_id, tg_notification_chat_id=chat_id)
            if self.parent_view:
                self.parent_view.settings = updated_settings

        embed = disnake.Embed(
            title="Telegram ID Установлен",
            description=f"Telegram Chat ID успешно сохранен для данного сервера: `{chat_id}`",
            color=None
        )
        await inter.response.send_message(embed=embed, ephemeral=True)
        if self.parent_view and self.parent_view.message:
            try:
                self.parent_view.build_components()
                await self.parent_view.message.edit(embed=self.parent_view.build_embed(), view=self.parent_view)
            except Exception as e:
                logger.debug(f"Parent view edit in modal handled: {e}")


class RestrictionsView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, settings):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.settings = settings
        self.build_components()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, user_id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этим настройкам.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    def build_components(self):
        self.clear_items()

        is_quarantine = (self.settings.punishment_mode == "quarantine")
        btn_label = "Режим: Карантин" if is_quarantine else "Режим: Бан"
        btn_style = disnake.ButtonStyle.success if is_quarantine else disnake.ButtonStyle.danger

        btn_toggle = disnake.ui.Button(
            label=btn_label,
            style=btn_style,
            custom_id="btn_toggle_mode",
            row=0
        )
        btn_toggle.callback = self.toggle_mode
        self.add_item(btn_toggle)

        role_select = disnake.ui.RoleSelect(
            placeholder="Выберите роль для карантина",
            min_values=1,
            max_values=1,
            custom_id="select_quarantine_role",
            row=1
        )
        role_select.callback = self.select_quarantine_role
        self.add_item(role_select)

        btn_back = disnake.ui.Button(
            label="Назад",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_back_restrictions",
            row=2
        )
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    def build_embed(self) -> disnake.Embed:
        q_role_str = f"<@&{self.settings.quarantine_role_id}>" if self.settings.quarantine_role_id else "Не настроена"
        mode_rus = "Карантин" if self.settings.punishment_mode == "quarantine" else "Бан"

        embed = disnake.Embed(color=None)
        embed.title = "Настройка ограничений"
        embed.description = (
            f"Режим наказания: **{mode_rus}**\n"
            f"Роль карантина: {q_role_str}\n\n"
            f"Выберите режим наказания и роль для помещения нарушителей в карантин:"
        )
        return embed

    async def toggle_mode(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            new_mode = "ban" if self.settings.punishment_mode == "quarantine" else "quarantine"
            self.settings = await crud.update_guild_settings(session, self.guild_id, punishment_mode=new_mode)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def select_quarantine_role(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        selected_role_id = int(interaction.values[0]) if interaction.values else None
        if selected_role_id:
            async with AsyncSessionLocal() as session:
                self.settings = await crud.update_guild_settings(session, self.guild_id, quarantine_role_id=selected_role_id)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def on_back(self, interaction: disnake.MessageInteraction):
        view = SettingsView(self.guild_id, self.author_id)
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        embed = build_settings_embed(settings, len(wl), last_log)
        await safe_edit_message(interaction, embed, view)


class NotificationsView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, settings):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.settings = settings
        self.build_components()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, user_id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этим настройкам.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    def build_components(self):
        self.clear_items()

        is_enabled = getattr(self.settings, "notifications_enabled", True)
        btn_label = "Уведомления: Включены" if is_enabled else "Уведомления: Выключены"
        btn_style = disnake.ButtonStyle.success if is_enabled else disnake.ButtonStyle.danger

        btn_toggle = disnake.ui.Button(
            label=btn_label,
            style=btn_style,
            custom_id="btn_toggle_notifications",
            row=0
        )
        btn_toggle.callback = self.toggle_notifications
        self.add_item(btn_toggle)

        btn_set_tg = disnake.ui.Button(
            label="Указать Telegram Chat ID",
            style=disnake.ButtonStyle.primary,
            custom_id="btn_open_tg_modal",
            row=0
        )
        btn_set_tg.callback = self.open_tg_modal
        self.add_item(btn_set_tg)

        chan_select = disnake.ui.ChannelSelect(
            placeholder="Выберите текстовый канал для уведомлений",
            channel_types=[disnake.ChannelType.text, disnake.ChannelType.news],
            min_values=1,
            max_values=1,
            custom_id="select_notification_channel",
            row=1
        )
        chan_select.callback = self.select_notification_channel
        self.add_item(chan_select)

        # Тоггл 1: уведомления о срабатывании антикраша
        n_trigger = getattr(self.settings, "notif_anticrash_trigger", True)
        btn_t_trigger = disnake.ui.Button(
            label=f"Алерт защиты: {'ВКЛ' if n_trigger else 'ВЫКЛ'}",
            style=disnake.ButtonStyle.success if n_trigger else disnake.ButtonStyle.secondary,
            row=2
        )
        btn_t_trigger.callback = self.toggle_trigger_notif
        self.add_item(btn_t_trigger)

        # Тоггл 2: подтверждение освобождения через Telegram (строка 2, рядом с триггером)
        n_confirm = getattr(self.settings, "notif_quarantine_confirm", True)
        btn_t_confirm = disnake.ui.Button(
            label=f"Подтверждение в ТГ: {'ВКЛ' if n_confirm else 'ВЫКЛ'}",
            style=disnake.ButtonStyle.success if n_confirm else disnake.ButtonStyle.secondary,
            row=2
        )
        btn_t_confirm.callback = self.toggle_confirm_notif
        self.add_item(btn_t_confirm)

        btn_back = disnake.ui.Button(
            label="Назад",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_back_notifications",
            row=3
        )
        btn_back.callback = self.on_back
        self.add_item(btn_back)

    def build_embed(self) -> disnake.Embed:
        from config import settings as app_settings
        db_tg_id = getattr(self.settings, "tg_notification_chat_id", None)
        active_tg_id = db_tg_id or app_settings.telegram_log_chat_id
        tg_id_str = f"`{active_tg_id}`" if active_tg_id else "Не настроен"
        notif_chan_str = f"<#{self.settings.notification_channel_id}>" if getattr(self.settings, "notification_channel_id", None) else "Не настроен"
        notif_status_str = "ВКЛЮЧЕНЫ" if getattr(self.settings, "notifications_enabled", True) else "ВЫКЛЮЧЕНЫ"

        n_trigger = getattr(self.settings, "notif_anticrash_trigger", True)
        n_confirm = getattr(self.settings, "notif_quarantine_confirm", True)

        embed = disnake.Embed(color=None)
        embed.title = "Настройка уведомлений"
        embed.description = (
            f"Общий статус уведомлений: **{notif_status_str}**\n"
            f"Канал уведомлений Discord: {notif_chan_str}\n"
            f"Telegram Chat ID: {tg_id_str}\n\n"
            f"Категории уведомлений:\n"
            f"• Алерты работы Антикраша: **{'ВКЛ' if n_trigger else 'ВЫКЛ'}**\n"
            f"• Обязательное подтверждение освобождения в ТГ: **{'ВКЛ' if n_confirm else 'ВЫКЛ'}**"
        )
        return embed

    async def open_tg_modal(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        modal = SetTelegramIdModal(self.guild_id, self)
        await interaction.response.send_modal(modal)

    async def toggle_notifications(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            new_state = not getattr(self.settings, "notifications_enabled", True)
            self.settings = await crud.update_guild_settings(session, self.guild_id, notifications_enabled=new_state)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def select_notification_channel(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        selected_chan_id = int(interaction.values[0]) if interaction.values else None
        if selected_chan_id:
            async with AsyncSessionLocal() as session:
                self.settings = await crud.update_guild_settings(session, self.guild_id, notification_channel_id=selected_chan_id)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def toggle_trigger_notif(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            new_val = not getattr(self.settings, "notif_anticrash_trigger", True)
            self.settings = await crud.update_guild_settings(session, self.guild_id, notif_anticrash_trigger=new_val)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def toggle_confirm_notif(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            await interaction.response.send_message("Доверенные пользователи не могут изменять настройки бота.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            new_val = not getattr(self.settings, "notif_quarantine_confirm", True)
            self.settings = await crud.update_guild_settings(session, self.guild_id, notif_quarantine_confirm=new_val)

        self.build_components()
        embed = self.build_embed()
        await safe_edit_message(interaction, embed, self)

    async def on_back(self, interaction: disnake.MessageInteraction):
        view = SettingsView(self.guild_id, self.author_id)
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        embed = build_settings_embed(settings, len(wl), last_log)
        await safe_edit_message(interaction, embed, view)


class SettingsView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id

        # 1. Проверяем автора
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        # 2. Проверяем, что пользователь доверенный
        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, user_id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этим настройкам.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Настройка ограничений", style=disnake.ButtonStyle.secondary, row=0)
    async def open_restrictions(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не имеют доступа к Настройкам сервера.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)

        view = RestrictionsView(self.guild_id, interaction.author.id, settings)
        embed = view.build_embed()
        await safe_edit_message(interaction, embed, view)

    @disnake.ui.button(label="Уведомления", style=disnake.ButtonStyle.secondary, row=0)
    async def open_notifications(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не имеют доступа к Настройкам сервера.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)

        view = NotificationsView(self.guild_id, interaction.author.id, settings)
        embed = view.build_embed()
        await safe_edit_message(interaction, embed, view)

    @disnake.ui.button(label="Главное меню", style=disnake.ButtonStyle.secondary, row=1)
    async def back(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        from views.main_view import MainAnticrashView
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        embed = build_settings_embed(settings, len(wl), last_log)
        view = MainAnticrashView(self.guild_id, interaction.author.id, settings.anticrash_enabled)
        view.message = interaction.message
        await safe_edit_message(interaction, embed, view)
