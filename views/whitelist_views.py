import logging
import disnake
from typing import List, Optional, Tuple, Any
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_whitelist_embed, build_user_whitelist_info_embed
from views.base_view import BaseTimeoutView, is_full_admin

logger = logging.getLogger("WhitelistViews")


class WhitelistRightsSelect(disnake.ui.StringSelect):
    def __init__(
        self,
        target_id: int,
        current_permissions: dict,
        author_id: int,
        is_role: bool = False,
        parent_view: Optional["WhitelistUserDetailView"] = None,
        parent_message: Optional[disnake.Message] = None
    ):
        self.target_id = target_id
        self.author_id = author_id
        self.is_role = is_role
        self.parent_view = parent_view
        self.parent_message = parent_message
        options = []
        for key, label in crud.PERMISSION_LABELS.items():
            is_default = current_permissions.get(key, False)
            options.append(
                disnake.SelectOption(
                    label=label,
                    value=key,
                    description=f"Право: {label}",
                    default=is_default
                )
            )
        super().__init__(
            placeholder="Выберите разрешенные опасные права",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        # 1. Проверяем, что взаимодействует именно автор
        if interaction.author.id != self.author_id and not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 2. Только полный админ может менять права — доверенным нельзя
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут изменять права в белом списке.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        selected_keys = set(self.values or [])
        new_permissions = {}
        for key in crud.PERMISSION_LABELS.keys():
            new_permissions[key] = key in selected_keys

        if self.parent_view:
            self.parent_view.selected_permissions = new_permissions
            if "is_trusted" in self.parent_view.permissions:
                self.parent_view.selected_permissions["is_trusted"] = self.parent_view.permissions["is_trusted"]

        embed = disnake.Embed(
            title="Выбор прав обновлен",
            description="Список выбранных прав изменен в памяти. Нажмите кнопку «Сохранить изменения» или «Добавить в белый список» для применения.",
            color=None
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class WhitelistUserDetailView(BaseTimeoutView):
    def __init__(
        self,
        guild_id: int,
        target_id: int,
        permissions: dict,
        is_whitelisted: bool,
        author_id: int,
        is_role: bool = False,
        parent_message: Optional[disnake.Message] = None
    ):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.target_id = target_id
        self.permissions = dict(permissions)
        self.selected_permissions = dict(permissions)
        self.is_whitelisted = is_whitelisted
        self.author_id = author_id
        self.is_role = is_role
        self.parent_message = parent_message

        # Добавляем дропдаун выбора прав
        self.add_item(WhitelistRightsSelect(target_id, permissions, author_id, is_role, self, parent_message))

        if is_whitelisted:
            btn_save = disnake.ui.Button(
                label="Сохранить изменения", style=disnake.ButtonStyle.success, row=1
            )
            btn_save.callback = self.on_save_permissions
            self.add_item(btn_save)

            btn_action = disnake.ui.Button(
                label="Удалить из белого списка", style=disnake.ButtonStyle.danger, row=1
            )
            btn_action.callback = self.on_remove_target
            self.add_item(btn_action)
        else:
            btn_action = disnake.ui.Button(
                label="Добавить в белый список", style=disnake.ButtonStyle.success, row=1
            )
            btn_action.callback = self.on_add_target
            self.add_item(btn_action)

        if not is_role and is_whitelisted:
            is_trusted_val = permissions.get("is_trusted", False)
            btn_trusted_label = "Убрать из доверенных" if is_trusted_val else "Добавить в доверенные"
            btn_trusted_style = disnake.ButtonStyle.danger if is_trusted_val else disnake.ButtonStyle.primary
            btn_trusted = disnake.ui.Button(
                label=btn_trusted_label, style=btn_trusted_style, row=1
            )
            btn_trusted.callback = self.on_toggle_trusted
            self.add_item(btn_trusted)

        btn_back = disnake.ui.Button(
            label="Назад", style=disnake.ButtonStyle.secondary, row=1
        )
        btn_back.callback = self.on_back
        self.add_item(btn_back)

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

    async def _refresh_parent(self, guild: disnake.Guild):
        if self.parent_message:
            try:
                view = WhitelistView(self.guild_id, self.author_id, page=1)
                async with AsyncSessionLocal() as session:
                    all_entries = await crud.get_whitelist(session, self.guild_id)
                page_entries = all_entries[:10]
                total_pages = max(1, (len(all_entries) + 9) // 10)
                await view.build_view_components(guild, page_entries, self.parent_message)
                embed = build_whitelist_embed(page_entries, 1, total_pages)
                await self.parent_message.edit(embed=embed, view=view)
            except Exception as e:
                logger.error(f"Ошибка обновления главного родительского сообщения: {e}")

    async def on_save_permissions(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут изменять права в белом списке.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            await crud.add_to_whitelist(
                session=session,
                guild_id=self.guild_id,
                user_id=self.target_id,
                permissions=self.selected_permissions,
                is_role=self.is_role
            )

        target_str = f"Роли <@&{self.target_id}>" if self.is_role else f"Пользователю <@{self.target_id}>"
        embed = disnake.Embed(
            title="Права сохранены",
            description=f"Новые права для {target_str} успешно сохранены в базе данных.",
            color=None
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._refresh_parent(interaction.guild)

    async def on_toggle_trusted(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут изменять статус участников.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            success, is_now_trusted = await crud.toggle_whitelist_trusted(session, self.guild_id, self.target_id)

        if is_now_trusted:
            msg = (
                f"Участник <@{self.target_id}> успешно добавлен в список доверенных!\n\n"
                f"После добавления он сможет открывать `/anticrash settings`, просматривать панели и подтверждать освобождение участников из карантина."
            )
        else:
            msg = f"Участник <@{self.target_id}> убран из списка доверенных."

        embed = disnake.Embed(title="Статус доверенного обновлен", description=msg, color=None)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._refresh_parent(interaction.guild)

    async def on_add_target(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут редактировать белый список.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            await crud.add_to_whitelist(
                session=session,
                guild_id=self.guild_id,
                user_id=self.target_id,
                permissions=self.selected_permissions,
                is_role=self.is_role
            )

        target_str = f"Роль <@&{self.target_id}>" if self.is_role else f"Пользователь <@{self.target_id}>"
        embed = disnake.Embed(
            title="Добавлено в белый список",
            description=f"{target_str} успешно добавлен(а) в белый список с выбранными правами.",
            color=None
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._refresh_parent(interaction.guild)

    async def on_remove_target(self, interaction: disnake.MessageInteraction):
        if not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не могут редактировать белый список.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            removed = await crud.remove_from_whitelist(session, self.guild_id, self.target_id)

        target_str = f"Роль <@&{self.target_id}>" if self.is_role else f"Пользователь <@{self.target_id}>"
        if removed:
            msg = f"{target_str} удален(а) из белого списка."
        else:
            msg = f"{target_str} не найден(а) в белом списке."

        embed = disnake.Embed(title="Удаление из белого списка", description=msg, color=None)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self._refresh_parent(interaction.guild)

    async def on_back(self, interaction: disnake.MessageInteraction):
        embed = disnake.Embed(description="Возврат в список...", color=None)
        await interaction.response.edit_message(embed=embed, view=None)


class WhitelistUserSelect(disnake.ui.UserSelect):
    def __init__(self, author_id: int, parent_message: Optional[disnake.Message] = None):
        self.author_id = author_id
        self.parent_message = parent_message
        super().__init__(
            placeholder="Выберите участника сервера для управления / добавления",
            min_values=1,
            max_values=1,
            row=0
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        # 1. Проверяем автора
        if interaction.author.id != self.author_id and not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, interaction.author.id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой панели управления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not self.values:
            return
        target = self.values[0]
        target_id = target.id if hasattr(target, "id") else int(target)

        async with AsyncSessionLocal() as session:
            entry = await crud.get_whitelist_user(session, interaction.guild_id, target_id)

        if entry:
            permissions = dict(entry.permissions)
            if getattr(entry, "is_trusted", False):
                permissions["is_trusted"] = True
            is_whitelisted = True
        else:
            permissions = crud.DEFAULT_PERMISSIONS.copy()
            is_whitelisted = False

        embed = build_user_whitelist_info_embed(interaction.guild, target_id, permissions, is_whitelisted, is_role=False)
        view = WhitelistUserDetailView(
            guild_id=interaction.guild_id,
            target_id=target_id,
            permissions=permissions,
            is_whitelisted=is_whitelisted,
            author_id=interaction.author.id,
            is_role=False,
            parent_message=self.parent_message
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class WhitelistRoleSelect(disnake.ui.RoleSelect):
    def __init__(self, author_id: int, parent_message: Optional[disnake.Message] = None):
        self.author_id = author_id
        self.parent_message = parent_message
        super().__init__(
            placeholder="Выберите роль сервера для управления / добавления",
            min_values=1,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        # 1. Проверяем автора
        if interaction.author.id != self.author_id and not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, interaction.author.id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой панели управления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not self.values:
            return
        target = self.values[0]
        target_id = target.id if hasattr(target, "id") else int(target)

        async with AsyncSessionLocal() as session:
            entry = await crud.get_whitelist_user(session, interaction.guild_id, target_id)

        if entry:
            permissions = dict(entry.permissions)
            is_whitelisted = True
        else:
            permissions = crud.DEFAULT_PERMISSIONS.copy()
            is_whitelisted = False

        embed = build_user_whitelist_info_embed(interaction.guild, target_id, permissions, is_whitelisted, is_role=True)
        view = WhitelistUserDetailView(
            guild_id=interaction.guild_id,
            target_id=target_id,
            permissions=permissions,
            is_whitelisted=is_whitelisted,
            author_id=interaction.author.id,
            is_role=True,
            parent_message=self.parent_message
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class WhitelistUserPageSelect(disnake.ui.StringSelect):
    def __init__(self, resolved_items: List[Tuple[Any, str]], author_id: int, parent_message: Optional[disnake.Message] = None):
        self.author_id = author_id
        self.parent_message = parent_message
        options = []
        for entry, label in resolved_items:
            if len(label) > 100:
                label = label[:97] + "..."

            options.append(
                disnake.SelectOption(
                    label=label,
                    value=str(entry.user_id),
                    description=f"ID: {entry.user_id}"
                )
            )
        if not options:
            options.append(disnake.SelectOption(label="Нет объектов", value="none"))

        super().__init__(
            placeholder="Выберите объект со страницы для управления",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(len(resolved_items) == 0),
            row=2
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        # 1. Проверяем автора
        if interaction.author.id != self.author_id and not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, interaction.guild_id, interaction.author.id, interaction.guild.owner_id)

        if not is_trusted:
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой панели управления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.values[0] == "none":
            return
        target_id = int(self.values[0])

        async with AsyncSessionLocal() as session:
            entry = await crud.get_whitelist_user(session, interaction.guild_id, target_id)

        is_role = False
        if entry:
            is_role = entry.is_role
            permissions = dict(entry.permissions)
            if getattr(entry, "is_trusted", False):
                permissions["is_trusted"] = True
            is_whitelisted = True
        else:
            permissions = crud.DEFAULT_PERMISSIONS.copy()
            is_whitelisted = False

        embed = build_user_whitelist_info_embed(interaction.guild, target_id, permissions, is_whitelisted, is_role)
        view = WhitelistUserDetailView(
            guild_id=interaction.guild_id,
            target_id=target_id,
            permissions=permissions,
            is_whitelisted=is_whitelisted,
            author_id=interaction.author.id,
            is_role=is_role,
            parent_message=self.parent_message
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class WhitelistView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, page: int = 1):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.page = page
        self.items_per_page = 10

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
            all_entries = await crud.get_whitelist(session, self.guild_id)

        total_items = len(all_entries)
        total_pages = max(1, (total_items + self.items_per_page - 1) // self.items_per_page)

        if self.page > total_pages:
            self.page = total_pages
        if self.page < 1:
            self.page = 1

        start_idx = (self.page - 1) * self.items_per_page
        end_idx = start_idx + self.items_per_page
        page_entries = all_entries[start_idx:end_idx]

        return all_entries, page_entries, total_pages

    async def build_view_components(self, guild: disnake.Guild, page_entries: list, parent_message: Optional[disnake.Message] = None):
        self.clear_items()

        # Строка 0: выбор участника сервера
        self.add_item(WhitelistUserSelect(self.author_id, parent_message))

        # Строка 1: выбор роли сервера
        self.add_item(WhitelistRoleSelect(self.author_id, parent_message))

        resolved_items = []
        for entry in page_entries:
            if entry.is_role:
                role = guild.get_role(entry.user_id) if guild else None
                label = f"Роль: @{role.name}" if role else f"Роль (ID: {entry.user_id})"
            else:
                member = guild.get_member(entry.user_id) if guild else None
                if not member and guild:
                    try:
                        member = await guild.fetch_member(entry.user_id)
                    except Exception:
                        member = None
                if member:
                    username_str = getattr(member, "name", str(entry.user_id))
                    disp_str = getattr(member, "display_name", username_str)
                    label = f"Участник: {disp_str} (@{username_str})"
                else:
                    label = f"Участник (ID: {entry.user_id})"

            resolved_items.append((entry, label))

        # Строка 2: выбор объекта с текущей страницы
        self.add_item(WhitelistUserPageSelect(resolved_items, self.author_id, parent_message))

        # Строка 3: кнопки навигации
        prev_button = disnake.ui.Button(
            label="<- Назад", style=disnake.ButtonStyle.secondary, disabled=(self.page <= 1), row=3
        )
        prev_button.callback = self.on_prev_page
        self.add_item(prev_button)

        next_button = disnake.ui.Button(
            label="Вперед ->", style=disnake.ButtonStyle.secondary, disabled=(self.page >= (max(1, (len(resolved_items) + 9) // 10))), row=3
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
        all_entries, page_entries, total_pages = await self.get_data()
        await self.build_view_components(interaction.guild, page_entries, interaction.message)

        embed = build_whitelist_embed(page_entries, self.page, total_pages)

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
