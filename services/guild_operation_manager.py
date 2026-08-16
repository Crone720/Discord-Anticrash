import asyncio
import logging
from typing import Dict, Tuple, Optional

logger = logging.getLogger("GuildOperationManager")

IDLE = "IDLE"
BACKUP_RUNNING = "BACKUP_RUNNING"
RESTORE_RUNNING = "RESTORE_RUNNING"


class GuildOperationManager:
    """
    Управляет операциями на сервере — не даёт запускать бэкап и восстановление одновременно.
    Потокобезопасен, поддерживает отмену.
    """
    def __init__(self):
        self._states: Dict[int, str] = {}
        self._locks: Dict[int, asyncio.Lock] = {}
        self._cancel_events: Dict[int, asyncio.Event] = {}
        self._global_lock = asyncio.Lock()

    async def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        async with self._global_lock:
            if guild_id not in self._locks:
                self._locks[guild_id] = asyncio.Lock()
            return self._locks[guild_id]

    def get_state(self, guild_id: int) -> str:
        return self._states.get(guild_id, IDLE)

    async def try_acquire(self, guild_id: int, target_state: str) -> Tuple[bool, str]:
        """
        Атомарно проверяет текущее состояние и занимает слот под target_state.
        Возвращает (успех: bool, сообщение для пользователя: str).
        """
        lock = await self._get_guild_lock(guild_id)
        async with lock:
            current_state = self._states.get(guild_id, IDLE)

            if current_state == BACKUP_RUNNING:
                if target_state == BACKUP_RUNNING:
                    return False, "**Бэкап уже выполняется**\n\nДождитесь окончания текущего бэкапа."
                else:
                    return False, "**Бэкап уже выполняется**\n\nНельзя начать восстановление до завершения текущего бэкапа."
            elif current_state == RESTORE_RUNNING:
                if target_state == BACKUP_RUNNING:
                    return False, "**Восстановление сервера уже выполняется**\n\nНельзя запустить бэкап до завершения восстановления."
                else:
                    return False, "**Восстановление сервера уже выполняется**\n\nПожалуйста, дождитесь завершения текущего процесса."

            # Слот свободен — занимаем
            self._states[guild_id] = target_state
            self._cancel_events[guild_id] = asyncio.Event()
            logger.info(f"Guild {guild_id} operation state changed to {target_state}")
            return True, "OK"

    async def release(self, guild_id: int):
        """Освобождает блокировку сервера, возвращает его в IDLE."""
        lock = await self._get_guild_lock(guild_id)
        async with lock:
            self._states[guild_id] = IDLE
            self._cancel_events.pop(guild_id, None)
            logger.info(f"Guild {guild_id} operation state reset to IDLE")

    def cancel_operation(self, guild_id: int):
        """Устанавливает флаг отмены для текущей операции на сервере."""
        event = self._cancel_events.get(guild_id)
        if event:
            event.set()
            logger.info(f"Cancellation requested for guild {guild_id}")

    def is_cancelled(self, guild_id: int) -> bool:
        event = self._cancel_events.get(guild_id)
        return event.is_set() if event else False


guild_op_manager = GuildOperationManager()
