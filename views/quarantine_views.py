import logging
import disnake
from typing import List, Tuple, Any
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_quarantine_embed
from views.base_view import BaseTimeoutView, is_full_admin

logger = logging.getLogger("QuarantineViews")


class QuarantineUserSelect(disnake.ui.StringSelect):
    def __init__(self, resolved_items: List[Tuple[Any, str]]):
        options = []
        for q, label in resolved_items:
            if len(label) > 100:
                label = label[:97] + "..."

            log_str = f"Лог: {q.log_id}" if q.log_id else "Лог: —"
            options.append(
                disnake.SelectOption(
                    label=label,
                    value=str(q.user_id),
                    description=f"ID: {q.user_id} | {log_str}"
                )
            )
        if not options:
            options.append(disnake.SelectOption(label="Нет пользователей", value="none"))

        super().__init__(
            placeholder="Выберите пользователя в карантине / бане",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(len(resolved_items) == 0),
            row=0
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        if self.values[0] == "none":
            return
        user_id = int(self.values[0])
        guild_id = interaction.guild_id

        async with AsyncSessionLocal() as session:
            q_user = await crud.get_quarantine_user(session, guild_id, user_id)

        if not q_user:
            embed = disnake.Embed(title="Ошибка", description="Пользователь не найден в базе данных.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        is_banned = False
        try:
            ban_entry = await interaction.guild.fetch_ban(disnake.Object(id=user_id))
            if ban_entry:
                is_banned = True
        except Exception:
            is_banned = False

        embed = disnake.Embed(color=None)
        embed.title = "Управление ограничением участника"
        status_text = "ЗАБАНЕН (Наказание Антикраша)" if is_banned else "В КАРАНТИНЕ"
        embed.description = (
            f"Пользователь: <@{user_id}> (ID: {user_id})\n"
            f"Статус: **{status_text}**\n"
            f"ID Лога: **{q_user.log_id or 'Отсутствует'}**\n"
            f"Причина: {q_user.reason}\n"
            f"Сохраненные роли: {', '.join(f'<@&{r}>' for r in q_user.saved_roles) if q_user.saved_roles else 'Нет'}"
        )

        view = QuarantineUserDetailView(
            guild_id=guild_id,
            target_user_id=user_id,
            saved_roles=q_user.saved_roles,
            author_id=interaction.author.id,
            guild=interaction.guild,
            is_banned=is_banned
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SavedRolesSelect(disnake.ui.StringSelect):
    def __init__(self, guild: disnake.Guild, saved_role_ids: List[int], parent_view: "QuarantineUserDetailView"):
        self.parent_view = parent_view
        options = []
        for rid in saved_role_ids:
            role = guild.get_role(rid)
            if role:
                label = role.name
                if len(label) > 100:
                    label = label[:97] + "..."
                options.append(
                    disnake.SelectOption(
                        label=f"@{label}",
                        value=str(role.id),
                        default=True,
                        description=f"ID: {role.id}"
                    )
                )

        if not options:
            options.append(disnake.SelectOption(label="Нет ролей для восстановления", value="none"))

        super().__init__(
            placeholder="Выберите роли для возврата пользователю",
            min_values=0,
            max_values=len(options) if (options and options[0].value != "none") else 1,
            options=options,
            disabled=(len(options) == 0 or options[0].value == "none"),
            row=0
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        if self.values and self.values[0] == "none":
            return

        selected_ids = [int(v) for v in self.values]
        self.parent_view.selected_role_ids = selected_ids

        # Обновляем список ролей в записи карантина
        async with AsyncSessionLocal() as session:
            q_user = await crud.get_quarantine_user(session, interaction.guild_id, self.parent_view.target_user_id)
            if q_user:
                q_user.saved_roles = selected_ids
                await session.commit()

        roles_mentions = ", ".join(f"<@&{r}>" for r in selected_ids) if selected_ids else "Нет выбранных ролей"
        embed = disnake.Embed(
            title="Обновлен выбор ролей",
            description=f"При освобождении пользователю <@{self.parent_view.target_user_id}> будут возвращены роли:\n{roles_mentions}",
            color=None
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class QuarantineUserDetailView(BaseTimeoutView):
    def __init__(
        self,
        guild_id: int,
        target_user_id: int,
        saved_roles: list,
        author_id: int,
        guild: disnake.Guild,
        is_banned: bool = False
    ):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.target_user_id = target_user_id
        self.saved_roles = saved_roles
        self.selected_role_ids = list(saved_roles)
        self.author_id = author_id
        self.is_banned = is_banned

        if not is_banned:
            # Дропдаун для выбора ролей, которые вернём
            self.add_item(SavedRolesSelect(guild, saved_roles, self))

            btn_release = disnake.ui.Button(
                label="Освободить (Восстановить выбранные роли)",
                style=disnake.ButtonStyle.success,
                row=1
            )
            btn_release.callback = self.release_user
            self.add_item(btn_release)

            btn_ban = disnake.ui.Button(
                label="Забанить пользователя",
                style=disnake.ButtonStyle.danger,
                row=1
            )
            btn_ban.callback = self.ban_user
            self.add_item(btn_ban)
        else:
            btn_unban = disnake.ui.Button(
                label="Разбанить (Снять бан сервера)",
                style=disnake.ButtonStyle.success,
                row=0
            )
            btn_unban.callback = self.unban_user
            self.add_item(btn_unban)

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой панели управления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    async def unban_user(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)

        try:
            await interaction.guild.unban(
                disnake.Object(id=self.target_user_id),
                reason="Разбан через панель Антикраш"
            )
        except Exception as e:
            logger.error(f"Не удалось разбанить пользователя {self.target_user_id}: {e}")

        async with AsyncSessionLocal() as session:
            await crud.remove_quarantine_user(session, self.guild_id, self.target_user_id)

        embed = disnake.Embed(
            title="Разбан выполнен",
            description=f"Пользователь <@{self.target_user_id}> разбанен на сервере и удален из списков.",
            color=None
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def release_user(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)

        from config import settings as app_settings
        async with AsyncSessionLocal() as session:
            g_settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            need_tg_confirm = (
                app_settings.enable_telegram_bot and
                app_settings.telegram_token and
                app_settings.telegram_log_chat_id and
                getattr(g_settings, "notif_quarantine_confirm", True)
            )

        if need_tg_confirm:
            from helpers.telegram_notifier import send_telegram_confirmation_request
            sent = await send_telegram_confirmation_request(
                action_type="release",
                guild_name=interaction.guild.name,
                guild_id=self.guild_id,
                user_id=self.target_user_id,
                requested_by=str(interaction.author),
                discord_inter=interaction
            )
            if sent:
                embed = disnake.Embed(
                    title="Ожидание подтверждения (60 сек)",
                    description=f"Отправлен запрос на подтверждение в Telegram-бота!\n\nПожалуйста, подтвердите освобождение пользователя <@{self.target_user_id}> из карантина в Telegram в течение 60 секунд.",
                    color=None
                )
                await interaction.edit_original_response(embed=embed, view=None)
                return

        member = interaction.guild.get_member(self.target_user_id)
        if not member:
            try:
                member = await interaction.guild.fetch_member(self.target_user_id)
            except Exception as e:
                logger.error(f"Не удалось получить объект member: {e}")
                member = None

        if member:
            roles_to_restore = []
            for r_id in self.selected_role_ids:
                r = interaction.guild.get_role(r_id)
                if r and r < interaction.guild.me.top_role and not r.is_default():
                    roles_to_restore.append(r)

            # Снимаем роль карантина, если она есть
            async with AsyncSessionLocal() as session:
                settings = await crud.get_or_create_guild_settings(session, self.guild_id)
                if settings.quarantine_role_id:
                    q_role = interaction.guild.get_role(settings.quarantine_role_id)
                    if q_role and q_role in member.roles:
                        try:
                            await member.remove_roles(q_role, reason="Освобождение из карантина")
                        except Exception as e:
                            logger.error(f"Не удалось снять роль карантина: {e}")

            if roles_to_restore:
                try:
                    await member.add_roles(*roles_to_restore, reason="Восстановление выбранных ролей после карантина")
                except Exception as e:
                    logger.error(f"Не удалось восстановить роли: {e}")

        async with AsyncSessionLocal() as session:
            await crud.remove_quarantine_user(session, self.guild_id, self.target_user_id)

        embed = disnake.Embed(
            title="Освобождение из карантина",
            description=f"Пользователь <@{self.target_user_id}> успешно освобожден из карантина: роль карантина снята, а его выбранные роли восстановлены.",
            color=None
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def ban_user(self, interaction: disnake.MessageInteraction):
        await interaction.response.defer(ephemeral=True)

        try:
            await interaction.guild.ban(
                disnake.Object(id=self.target_user_id),
                reason="Антикраш: Забанен через меню управления карантином"
            )
            async with AsyncSessionLocal() as session:
                await crud.remove_quarantine_user(session, self.guild_id, self.target_user_id)

            embed = disnake.Embed(
                title="Пользователь забанен",
                description=f"Пользователь <@{self.target_user_id}> забанен на сервере.",
                color=None
            )
            await interaction.edit_original_response(embed=embed, view=None)
        except Exception as e:
            embed = disnake.Embed(
                title="Ошибка бана",
                description=f"Не удалось забанить пользователя: {e}",
                color=None
            )
            await interaction.edit_original_response(embed=embed, view=None)


class QuarantineView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, page: int = 1):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.page = page
        self.items_per_page = 5

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
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="У вас нет прав на использование этой панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    async def get_data(self):
        async with AsyncSessionLocal() as session:
            q_users = await crud.get_quarantine_users(session, self.guild_id)

        total_items = len(q_users)
        total_pages = max(1, (total_items + self.items_per_page - 1) // self.items_per_page)

        if self.page > total_pages:
            self.page = total_pages
        if self.page < 1:
            self.page = 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_users = q_users[start_idx:end_idx]

        return q_users, page_users, total_pages

    async def build_components(self, guild: disnake.Guild, page_users: list, total_pages: int):
        self.clear_items()

        resolved_items = []
        for q in page_users:
            member = guild.get_member(q.user_id) if guild else None
            if not member and guild:
                try:
                    member = await guild.fetch_member(q.user_id)
                except Exception:
                    try:
                        member = await guild.client.fetch_user(q.user_id)
                    except Exception:
                        member = None

            if member:
                username_str = getattr(member, "name", str(q.user_id))
                disp_str = getattr(member, "display_name", username_str)
                label_str = f"{disp_str} (@{username_str})"
            else:
                label_str = f"Пользователь ({q.user_id})"

            resolved_items.append((q, label_str))

        self.add_item(QuarantineUserSelect(resolved_items))

        prev_button = disnake.ui.Button(
            label="<- Назад", style=disnake.ButtonStyle.secondary, disabled=(self.page <= 1), row=1
        )
        prev_button.callback = self.on_prev_page
        self.add_item(prev_button)

        next_button = disnake.ui.Button(
            label="Вперед ->", style=disnake.ButtonStyle.secondary, disabled=(self.page >= total_pages), row=1
        )
        next_button.callback = self.on_next_page
        self.add_item(next_button)

        home_button = disnake.ui.Button(
            label="Главное меню", style=disnake.ButtonStyle.primary, row=1
        )
        home_button.callback = self.on_home
        self.add_item(home_button)

    async def update_message(self, interaction: disnake.MessageInteraction):
        if interaction.message:
            self.message = interaction.message
        q_users, page_users, total_pages = await self.get_data()
        await self.build_components(interaction.guild, page_users, total_pages)

        embed = build_quarantine_embed(page_users, self.page, total_pages)

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
