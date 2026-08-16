import os
import json
import logging
import datetime
import disnake
from typing import Optional, List, Dict, Any
from config import settings
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import (
    build_backup_menu_embed,
    build_backup_result_embed,
    build_backup_detail_embed,
    build_restore_report_embed,
    build_restore_stage_embed
)
from services.backup_service import execute_server_backup
from services.backup_scheduler import backup_scheduler, parse_interval_to_seconds
from services.restore_service import restore_guild_from_backup
from services.guild_operation_manager import guild_op_manager, IDLE, BACKUP_RUNNING, RESTORE_RUNNING
from views.base_view import BaseTimeoutView, is_full_admin

logger = logging.getLogger("BackupViews")


class AutoBackupModal(disnake.ui.Modal):
    def __init__(self, backup_view: "BackupMenuView"):
        self.backup_view = backup_view
        components = [
            disnake.ui.TextInput(
                label="Интервал (напр. 30m, 1h, 2h, 1d, 0)",
                placeholder="30m (минуты), 1h (часы), 1d (дни) или 0 (отключить)",
                custom_id="interval_input",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=20,
                required=True
            )
        ]
        super().__init__(
            title="Настройка автоматического бэкапа",
            custom_id="auto_backup_modal",
            components=components
        )

    async def callback(self, interaction: disnake.ModalInteraction):
        input_val = interaction.text_values["interval_input"].strip().lower()

        if input_val != "0" and parse_interval_to_seconds(input_val) <= 0:
            embed = disnake.Embed(
                title="Ошибка формата интервала",
                description=f"Неверный формат интервала: **{input_val}**.\n\nИспользуйте комбинацию чисел и букв: `30m` (минуты), `1h` (часы), `1d` (дни) или `0` для отключения.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        interval_setting = "0" if input_val in ("0", "откл", "off") else input_val

        async with AsyncSessionLocal() as session:
            await crud.update_backup_settings(session, interaction.guild_id, auto_backup_interval=interval_setting)

        # Перезапускаем таймер планировщика для этого сервера
        backup_scheduler.reset_timer(interaction.guild_id, interaction.guild)

        await self.backup_view.update_message(interaction)


class ManualBackupStorageSelect(disnake.ui.StringSelect):
    def __init__(self, author_id: int, backup_menu_view: Optional["BackupMenuView"] = None):
        self.author_id = author_id
        self.backup_menu_view = backup_menu_view
        options = [
            disnake.SelectOption(
                label="Database (База данных)",
                value="Database",
                description="Сохранить JSON срез в MySQL базу данных"
            ),
            disnake.SelectOption(
                label="JSON (Локальный файл)",
                value="JSON",
                description="Сохранить отдельным .json файлом"
            ),
            disnake.SelectOption(
                label="GitHub (Репозиторий)",
                value="GitHub",
                description="Загрузить .json файл в GitHub репозиторий"
            )
        ]
        super().__init__(
            placeholder="Выберите тип хранилища для бэкапа",
            min_values=1,
            max_values=1,
            options=options,
            row=0
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        if interaction.author.id != self.author_id and not is_full_admin(interaction.author.id, interaction.guild):
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой операции.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Проверяем блокировку сервера перед запуском
        state = guild_op_manager.get_state(interaction.guild_id)
        if state != IDLE:
            if state == BACKUP_RUNNING:
                msg = "Бэкап уже выполняется. Дождитесь окончания текущего бэкапа."
            else:
                msg = "Восстановление сервера уже выполняется. Нельзя запустить бэкап до завершения восстановления."
            embed = disnake.Embed(title="Операция заблокирована", description=msg, color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        storage_chosen = self.values[0]
        await interaction.response.defer(ephemeral=True)

        success, result = await execute_server_backup(
            guild=interaction.guild,
            backup_type="manual",
            storage_override=storage_chosen
        )

        embed = build_backup_result_embed(result, success)
        await interaction.edit_original_response(embed=embed, view=None)

        if self.backup_menu_view:
            try:
                await self.backup_menu_view.update_message(interaction)
            except (disnake.NotFound, disnake.HTTPException) as e:
                logger.debug(f"Handled main backup menu refresh notice: {e}")


class ManualBackupPromptView(BaseTimeoutView):
    def __init__(self, author_id: int, backup_menu_view: Optional["BackupMenuView"] = None):
        super().__init__(timeout=120.0)
        self.add_item(ManualBackupStorageSelect(author_id, backup_menu_view))


class BackupStorageTypeSelect(disnake.ui.StringSelect):
    def __init__(self, current_storage: str):
        options = [
            disnake.SelectOption(
                label="Database (База данных)",
                value="Database",
                description="Хранить бэкапы в MySQL базе данных",
                default=(current_storage == "Database")
            ),
            disnake.SelectOption(
                label="JSON (Локальный файл)",
                value="JSON",
                description="Хранить бэкапы в .json файлах на сервере",
                default=(current_storage == "JSON")
            ),
            disnake.SelectOption(
                label="GitHub (Репозиторий)",
                value="GitHub",
                description="Авто-загрузка .json бэкапов на GitHub",
                default=(current_storage == "GitHub")
            )
        ]
        super().__init__(
            placeholder="Выберите по умолчанию хранилище бэкапов",
            min_values=1,
            max_values=1,
            options=options,
            row=3
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        view: BackupMenuView = self.view
        storage_chosen = self.values[0]

        async with AsyncSessionLocal() as session:
            await crud.update_backup_settings(session, interaction.guild_id, storage_type=storage_chosen)

        await view.update_message(interaction)


class RecentBackupsSelect(disnake.ui.StringSelect):
    def __init__(self, backups_list: List[Dict[str, Any]]):
        options = []
        for b in backups_list[:10]:
            c_at = b.get("created_at")
            if isinstance(c_at, datetime.datetime):
                date_str = c_at.strftime("%d.%m.%Y %H:%M")
            else:
                date_str = str(c_at)[:16]

            b_type = "Auto" if b.get("backup_type") == "auto" else "Manual"
            label = b.get("backup_name", b.get("backup_id"))
            if len(label) > 100:
                label = label[:97] + "..."

            desc = f"{date_str} | {b_type} | {b.get('storage')}"
            if len(desc) > 100:
                desc = desc[:97] + "..."

            options.append(
                disnake.SelectOption(
                    label=label,
                    value=b.get("backup_id"),
                    description=desc
                )
            )

        if not options:
            options.append(disnake.SelectOption(label="Нет бэкапов", value="none"))

        super().__init__(
            placeholder="Выберите бэкап для просмотра / отката (10 последних)",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(len(backups_list) == 0),
            row=2
        )

    async def callback(self, interaction: disnake.MessageInteraction):
        if self.values[0] == "none":
            return

        b_id = self.values[0]
        async with AsyncSessionLocal() as session:
            b_meta = await crud.get_backup_by_id(session, interaction.guild_id, b_id)

        if not b_meta or not b_meta.get("json_data"):
            embed = disnake.Embed(title="Ошибка бэкапа", description="Не удалось загрузить данные выбранного бэкапа.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            snapshot = json.loads(b_meta["json_data"])
        except Exception as e:
            embed = disnake.Embed(title="Ошибка парсинга", description=f"Ошибка чтения данных бэкапа: {e}", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = build_backup_detail_embed(b_meta, snapshot)
        view = BackupDetailView(
            guild_id=interaction.guild_id,
            author_id=interaction.author.id,
            meta=b_meta,
            snapshot=snapshot
        )
        if interaction.message:
            view.message = interaction.message
        await interaction.response.edit_message(embed=embed, view=view)


class BackupMenuView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id

        # 1. Сначала проверяем автора
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Вы не являетесь автором данной панели управления.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        # 2. Доверенным пользователям бэкапы недоступны
        if not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(
                title="Ограничение доступа",
                description="Доверенные пользователи не имеют доступа к разделу Бэкапы.",
                color=None
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False

        return True

    async def build_components(self):
        self.clear_items()
        async with AsyncSessionLocal() as session:
            setting = await crud.get_or_create_backup_settings(session, self.guild_id)
            recent_backups = await crud.get_recent_backups(session, self.guild_id, limit=10)

        guild_state = guild_op_manager.get_state(self.guild_id)
        is_busy = (guild_state != IDLE)

        # Строка 0: кнопки авто- и ручного бэкапа
        interval_label = setting.auto_backup_interval if setting.auto_backup_interval != "0" else "Откл"
        auto_text = f"Авто-бэкап: {interval_label} [Заблокировано]" if is_busy else f"Авто-бэкап: {interval_label}"
        manual_text = "Принудительный бэкап [Заблокировано]" if is_busy else "Принудительный бэкап"

        btn_auto = disnake.ui.Button(
            label=auto_text,
            style=disnake.ButtonStyle.secondary,
            disabled=is_busy,
            row=0
        )
        btn_auto.callback = self.on_auto_backup_click
        self.add_item(btn_auto)

        btn_manual = disnake.ui.Button(
            label=manual_text,
            style=disnake.ButtonStyle.secondary,
            disabled=is_busy,
            row=0
        )
        btn_manual.callback = self.on_manual_backup_click
        self.add_item(btn_manual)

        # Строка 1: переключатель сохранения участников
        if setting.save_members:
            btn_members = disnake.ui.Button(
                label="Выключить сохранение пользователей",
                style=disnake.ButtonStyle.danger,
                disabled=is_busy,
                row=1
            )
        else:
            btn_members = disnake.ui.Button(
                label="Включить сохранение пользователей",
                style=disnake.ButtonStyle.success,
                disabled=is_busy,
                row=1
            )
        btn_members.callback = self.on_toggle_save_members
        self.add_item(btn_members)

        # Строка 2: список последних бэкапов
        rec_select = RecentBackupsSelect(recent_backups)
        if is_busy:
            rec_select.disabled = True
        self.add_item(rec_select)

        # Строка 3: выбор типа хранилища
        st_select = BackupStorageTypeSelect(setting.storage_type or "Database")
        if is_busy:
            st_select.disabled = True
        self.add_item(st_select)

        # Строка 4: кнопка домой
        btn_home = disnake.ui.Button(
            label="Главное меню",
            style=disnake.ButtonStyle.secondary,
            row=4
        )
        btn_home.callback = self.on_home_click
        self.add_item(btn_home)

        return setting

    async def update_message(self, interaction: disnake.Interaction):
        if interaction.message:
            self.message = interaction.message

        setting = await self.build_components()
        embed = build_backup_menu_embed(setting)

        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(embed=embed, view=self)
                else:
                    await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except (disnake.NotFound, disnake.HTTPException) as e:
            logger.debug(f"Handled update_message exception: {e}")

    async def on_auto_backup_click(self, interaction: disnake.MessageInteraction):
        state = guild_op_manager.get_state(self.guild_id)
        if state != IDLE:
            embed = disnake.Embed(title="Операция заблокирована", description="Выполняется другая операция бэкапа или восстановления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        modal = AutoBackupModal(self)
        await interaction.response.send_modal(modal)

    async def on_manual_backup_click(self, interaction: disnake.MessageInteraction):
        state = guild_op_manager.get_state(self.guild_id)
        if state != IDLE:
            if state == BACKUP_RUNNING:
                msg = "Бэкап уже выполняется. Дождитесь окончания текущего бэкапа."
            else:
                msg = "Восстановление сервера уже выполняется. Нельзя запустить бэкап до завершения восстановления."
            embed = disnake.Embed(title="Операция заблокирована", description=msg, color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        prompt_view = ManualBackupPromptView(author_id=interaction.author.id, backup_menu_view=self)
        embed = disnake.Embed(color=None)
        embed.title = "Выбор типа хранилища для принудительного бэкапа"
        embed.description = "Выберите место хранения бэкапа в выпадающем меню ниже:"
        await interaction.response.send_message(embed=embed, view=prompt_view, ephemeral=True)

    async def on_toggle_save_members(self, interaction: disnake.MessageInteraction):
        async with AsyncSessionLocal() as session:
            setting = await crud.get_or_create_backup_settings(session, self.guild_id)
            new_state = not setting.save_members
            await crud.update_backup_settings(session, self.guild_id, save_members=new_state)

        await self.update_message(interaction)

    async def on_home_click(self, interaction: disnake.MessageInteraction):
        from views.main_view import MainAnticrashView
        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, self.guild_id)
            wl = await crud.get_whitelist(session, self.guild_id)
            last_log = await crud.get_last_attack_log(session, self.guild_id)

        from helpers.embed_builder import build_settings_embed
        embed = build_settings_embed(settings, len(wl), last_log)
        view = MainAnticrashView(self.guild_id, self.author_id, settings.anticrash_enabled)
        if interaction.message:
            view.message = interaction.message
        await interaction.response.edit_message(embed=embed, view=view)


class BackupDetailView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, meta: dict, snapshot: dict):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.meta = meta
        self.snapshot = snapshot
        self.build_buttons()

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой панели управления.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    def build_buttons(self):
        self.clear_items()
        is_busy = (guild_op_manager.get_state(self.guild_id) != IDLE)

        # 1. Кнопка восстановления
        btn_restore = disnake.ui.Button(
            label="Вернуть состояние к бэкапу",
            style=disnake.ButtonStyle.danger,
            disabled=is_busy,
            row=0
        )
        btn_restore.callback = self.on_restore_click
        self.add_item(btn_restore)

        # 2. Кнопка экспорта JSON
        btn_export = disnake.ui.Button(
            label="ExportJson",
            style=disnake.ButtonStyle.primary,
            row=0
        )
        btn_export.callback = self.on_export_click
        self.add_item(btn_export)

        # 3. Ссылка на GitHub (только если бэкап хранится там)
        storage_name = str(self.meta.get("storage", ""))
        if storage_name.lower() == "github" or "github" in str(self.meta.get("backup_id", "")).lower():
            b_name = self.meta.get("backup_name", "")
            date_part = b_name.replace("backup-", "")
            repo = settings.github_repo or ""
            branch = settings.github_branch or "main"
            git_url = f"https://github.com/{repo}/blob/{branch}/backups/{date_part}.json"

            btn_git = disnake.ui.Button(
                label="GitBackup",
                style=disnake.ButtonStyle.link,
                url=git_url,
                row=0
            )
            self.add_item(btn_git)

        # 4. Кнопка назад
        btn_back = disnake.ui.Button(
            label="Назад",
            style=disnake.ButtonStyle.secondary,
            row=1
        )
        btn_back.callback = self.on_back_click
        self.add_item(btn_back)

    async def on_restore_click(self, interaction: disnake.MessageInteraction):
        state = guild_op_manager.get_state(self.guild_id)
        if state != IDLE:
            if state == BACKUP_RUNNING:
                msg = "Бэкап уже выполняется. Нельзя начать восстановление до завершения текущего бэкапа."
            else:
                msg = "Восстановление сервера уже выполняется. Пожалуйста, дождитесь завершения текущего процесса."
            embed = disnake.Embed(title="Операция заблокирована", description=msg, color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        confirm_view = RestoreConfirmView(
            guild_id=self.guild_id,
            author_id=self.author_id,
            meta=self.meta,
            snapshot=self.snapshot
        )
        embed = disnake.Embed(color=None)
        embed.title = "Подтверждение отката состояния сервера"
        embed.description = (
            f"Вы уверены, что хотите выполнить откат сервера к бэкапу **{self.meta.get('backup_name')}**?\n\n"
            f"• Будут синхронизированы все роли, категории, каналы и права доступа.\n"
            f"• Лишние роли, категории и каналы, созданные после бэкапа, будут удалены.\n"
            f"• Недостающие роли и каналы будут созданы с восстановлением прав.\n\n"
            f"**Подтвердите действие:**"
        )
        await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)

    async def on_export_click(self, interaction: disnake.MessageInteraction):
        b_name = self.meta.get("backup_name", "backup")
        json_str = self.meta.get("json_data") or json.dumps(self.snapshot, ensure_ascii=False, indent=2)
        filename = f"{b_name}.json"

        try:
            file_bytes = json_str.encode("utf-8")
            import io
            fp = io.BytesIO(file_bytes)
            d_file = disnake.File(fp=fp, filename=filename)

            embed = disnake.Embed(title="Экспорт JSON бэкапа", description=f"Экспорт бэкапа **{b_name}**:", color=None)
            await interaction.response.send_message(
                embed=embed,
                file=d_file,
                ephemeral=True
            )
        except Exception as e:
            backups_dir = os.path.join(os.getcwd(), "backups")
            os.makedirs(backups_dir, exist_ok=True)
            local_path = os.path.join(backups_dir, filename)

            try:
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(json_str)
            except Exception:
                pass

            err_embed = disnake.Embed(
                title="Ошибка экспорта JSON в Discord",
                description=f"Не удалось отправить JSON в Discord.\n\n**Ошибка:**\n`{e}`\n\n**Файл сохранён локально:**\n`backups/{filename}`",
                color=None
            )
            await interaction.response.send_message(embed=err_embed, ephemeral=True)

    async def on_back_click(self, interaction: disnake.MessageInteraction):
        menu_view = BackupMenuView(self.guild_id, self.author_id)
        await menu_view.update_message(interaction)


class RestoreRunningView(BaseTimeoutView):
    """Вью, активная во время восстановления сервера — показывает прогресс и красную кнопку отмены."""
    def __init__(self, guild_id: int, author_id: int, meta: dict, snapshot: dict):
        super().__init__(timeout=300.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.meta = meta
        self.snapshot = snapshot

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой операции.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.danger, custom_id="btn_cancel_restore")
    async def cancel_restore(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        guild_op_manager.cancel_operation(self.guild_id)
        button.disabled = True
        button.label = "Отмена выполняется..."
        await interaction.response.edit_message(view=self)
        embed = disnake.Embed(title="Запрос отмены", description="Запрос отмены отправлен. Текущий шаг завершается, после чего будет сформирован итоговый отчёт.", color=None)
        await interaction.followup.send(embed=embed, ephemeral=True)


class RestoreConfirmView(BaseTimeoutView):
    def __init__(self, guild_id: int, author_id: int, meta: dict, snapshot: dict):
        super().__init__(timeout=120.0)
        self.guild_id = guild_id
        self.author_id = author_id
        self.meta = meta
        self.snapshot = snapshot

    async def interaction_check(self, interaction: disnake.MessageInteraction) -> bool:
        user_id = interaction.author.id
        if user_id != self.author_id and not is_full_admin(user_id, interaction.guild):
            embed = disnake.Embed(title="Ограничение доступа", description="У вас нет доступа к этой операции.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True

    @disnake.ui.button(label="Да, выполнить откат", style=disnake.ButtonStyle.danger)
    async def confirm_restore(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        state = guild_op_manager.get_state(self.guild_id)
        if state != IDLE:
            embed = disnake.Embed(title="Ошибка восстановления", description="Выполняется другая операция. Нельзя запустить восстановление.", color=None)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        running_view = RestoreRunningView(self.guild_id, self.author_id, self.meta, self.snapshot)
        init_embed = build_restore_stage_embed(1, self.meta.get("backup_name", "backup"))
        await interaction.response.edit_message(embed=init_embed, view=running_view)

        async def stage_callback(stage_num: int):
            try:
                st_embed = build_restore_stage_embed(stage_num, self.meta.get("backup_name", "backup"))
                await interaction.edit_original_response(embed=st_embed, view=running_view)
            except Exception as e:
                logger.debug(f"Progress stage edit handled: {e}")

        stats, changes_summary, txt_filepath = await restore_guild_from_backup(
            guild=interaction.guild,
            snapshot=self.snapshot,
            progress_callback=stage_callback
        )

        txt_filename = os.path.basename(txt_filepath) if txt_filepath else None
        report_embed = build_restore_report_embed(stats, changes_summary, txt_filename)

        # Отключаем кнопку отмены по завершении
        for item in running_view.children:
            if isinstance(item, disnake.ui.Button):
                item.disabled = True
                item.label = "Восстановление завершено"

        txt_file = disnake.File(fp=txt_filepath, filename=txt_filename) if txt_filepath and os.path.exists(txt_filepath) else None

        try:
            if txt_file:
                await interaction.edit_original_response(embed=report_embed, view=running_view, file=txt_file)
            else:
                await interaction.edit_original_response(embed=report_embed, view=running_view)
        except Exception as e:
            logger.debug(f"Final report edit handled: {e}")

    @disnake.ui.button(label="Отмена", style=disnake.ButtonStyle.secondary)
    async def cancel_confirm(self, button: disnake.ui.Button, interaction: disnake.MessageInteraction):
        embed = build_backup_detail_embed(self.meta, self.snapshot)
        view = BackupDetailView(self.guild_id, self.author_id, self.meta, self.snapshot)
        await interaction.response.edit_message(embed=embed, view=view)
