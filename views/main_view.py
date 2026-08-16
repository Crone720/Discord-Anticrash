import disnake
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_settings_embed
from views.base_view import BaseTimeoutView, is_full_admin


class MainAnticrashView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, anticrash_enabled: bool):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.anticrash_enabled = anticrash_enabled
        self.update_buttons()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id

        # сначала проверяем: автор, владелец или разраб
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        # проверяем, есть ли у пользователя доверенный статус
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

    def update_buttons(self):
        self.clear_items()

        # белый список
        btn_whitelist = disnake.ui.Button(
            label="Белый список",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_whitelist",
            row=0
        )
        btn_whitelist.callback = self.on_whitelist
        self.add_item(btn_whitelist)

        # журнал действий
        btn_actions = disnake.ui.Button(
            label="Действия",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_actions",
            row=0
        )
        btn_actions.callback = self.on_actions
        self.add_item(btn_actions)

        # бэкап
        btn_backup = disnake.ui.Button(
            label="Бэкап",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_backup",
            row=0
        )
        btn_backup.callback = self.on_backup
        self.add_item(btn_backup)

        # настройки
        btn_settings = disnake.ui.Button(
            label="Настройки",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_settings",
            row=1
        )
        btn_settings.callback = self.on_settings
        self.add_item(btn_settings)

        # кнопка включения/выключения: зелёная — выключен, красная — включён
        if self.anticrash_enabled:
            btn_toggle = disnake.ui.Button(
                label="Выключить",
                style=disnake.ButtonStyle.danger,
                custom_id="btn_toggle_anticrash",
                row=1
            )
        else:
            btn_toggle = disnake.ui.Button(
                label="Включить",
                style=disnake.ButtonStyle.success,
                custom_id="btn_toggle_anticrash",
                row=1
            )
        btn_toggle.callback = self.on_toggle_anticrash
        self.add_item(btn_toggle)

        # карантин
        btn_quarantine = disnake.ui.Button(
            label="Карантин",
            style=disnake.ButtonStyle.secondary,
            custom_id="btn_quarantine",
            row=1
        )
        btn_quarantine.callback = self.on_quarantine
        self.add_item(btn_quarantine)

    async def on_whitelist(self, interaction: disnake.MessageInteraction):
        from views.whitelist_views import WhitelistView
        view = WhitelistView(self.guild_id, interaction.author.id, page=1)
        await view.update_message(interaction)

    async def on_actions(self, interaction: disnake.MessageInteraction):
        from views.action_views import ActionLogsView
        view = ActionLogsView(self.guild_id, interaction.author.id, page=1)
        await view.update_message(interaction)

    async def on_backup(self, interaction: disnake.MessageInteraction):
        # доверенным пользователям бэкап недоступен
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не имеют доступа к разделу Бэкапы.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        from views.backup_views import BackupMenuView
        view = BackupMenuView(self.guild_id, interaction.author.id)
        await view.update_message(interaction)

    async def on_settings(self, interaction: disnake.MessageInteraction):
        # доверенным пользователям настройки недоступны
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не имеют доступа к Настройкам сервера.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        from views.settings_view import SettingsView
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        embed = build_settings_embed(settings, len(wl), last_log)
        view = SettingsView(self.guild_id, interaction.author.id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_toggle_anticrash(self, interaction: disnake.MessageInteraction):
        if interaction.message:
            self.message = interaction.message

        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут включать или выключать Антикраш.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        from config import settings as app_settings
        if app_settings.enable_telegram_bot and app_settings.telegram_token and app_settings.telegram_log_chat_id:
            from helpers.telegram_notifier import send_telegram_confirmation_request
            sent = await send_telegram_confirmation_request(
                action_type="toggle_anticrash",
                guild_name=interaction.guild.name,
                guild_id=self.guild_id,
                user_id=0,
                requested_by=str(interaction.author),
                discord_inter=interaction
            )
            if sent:
                embed = disnake.Embed(
                    title="Ожидание подтверждения (60 сек)",
                    description="Отправлен запрос на подтверждение в Telegram!\n\nПожалуйста, подтвердите изменение состояния работы Антикраша в Telegram-боте в течение 60 секунд.",
                    color=None
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        new_state = not self.anticrash_enabled
        async with AsyncSessionLocal() as session:
            settings = await crud.update_guild_settings(
                session, self.guild_id, anticrash_enabled=new_state
            )
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        self.anticrash_enabled = settings.anticrash_enabled
        self.update_buttons()
        embed = build_settings_embed(settings, len(wl), last_log)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_quarantine(self, interaction: disnake.MessageInteraction):
        from views.quarantine_views import QuarantineView
        view = QuarantineView(self.guild_id, interaction.author.id, page=1)
        await view.update_message(interaction)
