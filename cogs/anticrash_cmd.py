import disnake
from disnake.ext import commands
from database.connection import AsyncSessionLocal
from database import crud
from helpers.embed_builder import build_settings_embed
from views.main_view import MainAnticrashView


class AnticrashCommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(name="anticrash", description="Управление модулем безопасности Антикраш")
    async def anticrash(self, inter: disnake.ApplicationCommandInteraction):
        pass

    @commands.slash_command(
        name="id",
        description="Получить ID указанного пользователя или свой ID"
    )
    async def id_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(default=None, description="Пользователь, чей ID вы хотите получить")
    ):
        target_user = user or inter.author
        embed = disnake.Embed(color=None)
        embed.title = "Информация об ID"
        embed.description = f"Пользователь: {target_user.mention} (`@{target_user.name}`)\nID: `{target_user.id}`"
        await inter.response.send_message(embed=embed, ephemeral=True)

    @anticrash.sub_command(
        name="settings",
        description="Панель управления настройками антикраша"
    )
    async def settings_cmd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        ephemeral: bool = commands.Param(
            default=False,
            name="ephemeral",
            description="Отобразить сообщение скрыто (True/False)"
        )
    ):
        """Показывает главную панель управления антикрашем."""
        if not inter.guild:
            await inter.response.send_message("Эта команда доступна только на серверах.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            is_trusted = await crud.is_user_trusted(session, inter.guild_id, inter.author.id, inter.guild.owner_id)

        if not is_trusted:
            await inter.response.send_message(
                "Доступ запрещен. Управление антикрашем доступно только владельцу сервера и доверенным лицам.",
                ephemeral=True
            )
            return

        async with AsyncSessionLocal() as session:
            settings = await crud.get_or_create_guild_settings(session, inter.guild_id)
            whitelist_users = await crud.get_whitelist(session, inter.guild_id)
            last_log = await crud.get_last_attack_log(session, inter.guild_id)

        embed = build_settings_embed(
            settings=settings,
            whitelist_count=len(whitelist_users),
            last_log=last_log
        )

        view = MainAnticrashView(
            guild_id=inter.guild_id,
            author_id=inter.author.id,
            anticrash_enabled=settings.anticrash_enabled
        )

        await inter.response.send_message(embed=embed, view=view, ephemeral=ephemeral)


def setup(bot: commands.Bot):
    bot.add_cog(AnticrashCommandCog(bot))
