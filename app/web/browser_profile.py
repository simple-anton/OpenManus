"""Профиль браузера: где живут открытые сессии и как их стереть.

Когда человек входит на сайт через окно живого вида, вход остаётся в профиле
Chromium — там cookies и хранилище сессии. Профиль лежит в отдельном томе
Docker и переживает перезапуск контейнера: иначе входить пришлось бы заново
после каждой остановки, и смысл затеи терялся бы.

Оборотная сторона: на диске оказываются ключи от чужих кабинетов. Поэтому
здесь есть кнопка «забыть все входы» — она стирает профиль целиком.
"""

import asyncio
import os
import shutil
from pathlib import Path

import httpx

from app.logger import logger


PROFILE = Path(os.getenv("CHROME_PROFILE", "/root/.config/chromium"))
CDP = f"http://127.0.0.1:{os.getenv('CHROME_CDP_PORT', '9222')}"

# Что именно в профиле хранит вход. Стираем только это: остальное — кэш и
# настройки, из-за них незачем заставлять браузер переучиваться с нуля.
SESSION_PARTS = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "Service Worker",
    "Sessions",
    "Network",
)


async def forget() -> int:
    """Стирает всё, чем браузер помнит входы. Возвращает число удалённого.

    Сначала просим браузер закрыть вкладки: открытый Chromium держит файлы
    профиля и дописывает их при выходе, так что удалять из-под работающего
    процесса — верный способ получить обратно то же самое.
    """
    await _close_tabs()
    # даём браузеру дописать буферы на диск, иначе он вернёт cookies обратно
    await asyncio.sleep(0.5)

    removed = 0
    default = PROFILE / "Default"
    for folder in (default, PROFILE):
        if not folder.is_dir():
            continue
        for name in SESSION_PARTS:
            target = folder / name
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                    removed += 1
                elif target.exists():
                    target.unlink()
                    removed += 1
            except OSError as error:
                logger.warning(f"Не удалось стереть {target}: {error}")
    logger.info(f"Профиль браузера очищен: удалено объектов {removed}")
    return removed


async def _close_tabs() -> None:
    """Закрывает вкладки, чтобы сайты не держали открытые сессии."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            tabs = (await client.get(f"{CDP}/json/list")).json()
        except (httpx.HTTPError, ValueError):
            return
        # На этом порту может оказаться не браузер, а прокси или заглушка —
        # тогда придёт что угодно. Форму ответа проверяем, а не предполагаем:
        # уронить очистку профиля из-за чужого ответа недопустимо.
        if not isinstance(tabs, list):
            logger.warning("Браузер вернул неожиданный ответ на /json/list")
            return
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            target = tab.get("id")
            if target and tab.get("type") == "page":
                try:
                    await client.get(f"{CDP}/json/close/{target}")
                except httpx.HTTPError:
                    pass
