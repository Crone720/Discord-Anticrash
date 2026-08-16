import base64
import logging
import aiohttp
from typing import Tuple, Optional
from config import settings

logger = logging.getLogger("GitHubService")


class GitHubService:
    @staticmethod
    async def upload_backup_file(file_name: str, json_content: str) -> Tuple[bool, str]:
        """
        Загружает JSON-строку бэкапа как файл в настроенный GitHub-репозиторий.
        Возвращает (успех: bool, ссылка или сообщение об ошибке: str).
        """
        token = settings.github_token.get_secret_value() if settings.github_token else ""
        repo = settings.github_repo or ""
        branch = settings.github_branch or "main"

        if not token or not repo:
            return False, "Не настроены переменные GITHUB_TOKEN или GITHUB_REPO в файле .env."

        # Проверяем формат репо: должно быть owner/repo
        if "/" not in repo:
            return False, f"Неверный формат GITHUB_REPO: '{repo}'. Ожидается формат 'owner/repo'."

        url = f"https://api.github.com/repos/{repo}/contents/backups/{file_name}"
        
        # Кодируем содержимое в base64 для GitHub API
        content_b64 = base64.b64encode(json_content.encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Anticrash-Bot-Backup"
        }

        payload = {
            "message": f"Auto-backup: {file_name}",
            "content": content_b64,
            "branch": branch
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, headers=headers, json=payload, timeout=30) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        html_url = data.get("content", {}).get("html_url", f"backups/{file_name}")
                        return True, html_url
                    else:
                        error_data = await resp.json()
                        err_msg = error_data.get("message", f"HTTP Status {resp.status}")
                        logger.error(f"GitHub Upload Error ({resp.status}): {err_msg}")
                        return False, f"Ошибка GitHub API ({resp.status}): {err_msg}"
        except Exception as e:
            logger.error(f"Исключение при вызове GitHub API: {e}")
            return False, f"Сетевая ошибка при загрузке на GitHub: {e}"
