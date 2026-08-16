import logging
import disnake
from typing import Optional
from config import settings

logger = logging.getLogger("BaseTimeoutView")


def is_full_admin(user_id: int, guild: Optional[disnake.Guild]) -> bool:
    if guild and user_id == guild.owner_id:
        return True
    if settings.developer_discord_id and user_id == settings.developer_discord_id:
        return True
    return False


class BaseTimeoutView(disnake.ui.View):
    """
    Базовый вью с таймаутом — автоматически отключает все кнопки и селекты
    и редактирует сообщение по истечении времени. Ошибки NotFound/HTTPException проглатываются тихо.
    """
    def __init__(self, timeout: Optional[float] = 180.0):
        super().__init__(timeout=timeout)
        self.message: Optional[disnake.Message] = None

    async def on_timeout(self):
        # отключаем все интерактивные элементы
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

        # редактируем сообщение, если оно сохранено
        if self.message:
            try:
                await self.message.edit(view=self)
            except (disnake.NotFound, disnake.HTTPException, AttributeError, Exception) as e:
                logger.debug(f"Не удалось обновить сообщение при таймауте: {e}")
