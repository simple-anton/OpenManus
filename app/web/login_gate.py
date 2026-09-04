"""Вход под учётной записью человека посреди исследования.

Задача. Агент упирается в сайт, который отдаёт данные только своим: банк,
платная подписка, кабинет регулятора. Пароля у агента нет и быть не должно.
Но у человека доступ есть — и он сидит перед интерфейсом.

Решение. Агент вызывает `request_login`, работа встаёт на паузу, а в
интерфейсе открывается окно с живым видом на тот самый браузер, который
работает внутри контейнера. Человек входит руками — как обычно, своими
пальцами, — и нажимает «я вошёл». Агент продолжает уже в открытой сессии.
Либо человек нажимает «пропустить», и агент честно записывает источник как
недоступный и идёт дальше.

Чего здесь намеренно нет:

* Пароли нигде не хранятся и не проходят через агента. Он видит только факт
  «человек закончил» или «человек отказался».
* Автоматической регистрации нет. Завести учётную запись может только
  человек и только сам.
* Щит от автоматики (Cloudflare) этим не лечится: он срабатывает до формы
  входа. Инструмент для другого случая — для двери с замком, а не для
  вышибалы на входе.

Что при этом остаётся на диске: профиль браузера с cookies уже открытых
сессий. Он лежит в отдельном томе Docker и переживает перезапуск — иначе
входить пришлось бы каждый раз заново. Стереть его можно кнопкой в
Настройках.
"""

import os
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.logger import logger
from app.tool.base import BaseTool, ToolResult


CDP = f"http://127.0.0.1:{os.getenv('CHROME_CDP_PORT', '9222')}"

# Адрес живого вида на браузер контейнера. Пробрасывается наружу только на
# 127.0.0.1, см. docker-compose.yml.
VIEW_PORT = os.getenv("BROWSER_VIEW_PORT", "6080")

DECISION_DONE = "done"
DECISION_SKIP = "skip"


def view_url() -> str:
    """Адрес окна с живым видом. Открывается в рамке внутри интерфейса."""
    return f"http://127.0.0.1:{VIEW_PORT}/vnc.html?autoconnect=1&resize=scale"


async def open_in_browser(url: str) -> bool:
    """Открывает адрес во вкладке того браузера, которым работает агент.

    Человек должен увидеть нужную страницу сразу, а не искать её сам.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        # У разных сборок Chromium /json/new отвечает то на PUT, то на GET
        for method in ("PUT", "GET"):
            try:
                response = await client.request(method, f"{CDP}/json/new?{url}")
                if response.status_code < 400:
                    return True
            except httpx.HTTPError as error:
                logger.warning(f"Не удалось открыть {url} в браузере: {error}")
                return False
    logger.warning(f"Браузер не открыл {url}: /json/new отказал и на PUT, и на GET")
    return False


async def browser_alive() -> bool:
    """Есть ли вообще браузер, в который человеку смотреть."""
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            response = await client.get(f"{CDP}/json/version")
            return response.status_code < 400
        except httpx.HTTPError:
            return False


class RequestLogin(BaseTool):
    """Просьба к человеку войти на сайт своими руками."""

    name: str = "request_login"
    description: str = (
        "Ask the person running this task to sign in to a website by hand, in "
        "the browser you are using, and then continue with their session.\n"
        "USE THIS when a source is behind a login or paywall that the person "
        "plausibly has access to: a bank, a broker, a paid data subscription, "
        "a regulator's personal cabinet, a company account.\n"
        "DO NOT use this for an anti-bot shield (Cloudflare 'Just a moment', a "
        "captcha) — that check happens before any login form, so an account "
        "changes nothing there. Do not use it to have anyone register a new "
        "account on your behalf.\n"
        "The task pauses while they decide. They either sign in and you "
        "continue on the same page with browser_exec, or they decline and you "
        "must record the source as unavailable and carry on without it."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "(required) The exact page to open for them — the login "
                    "page, or the page you were blocked on."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "(required) In one or two sentences, in the language the "
                    "person is using: what data you need from this source and "
                    "why the research needs it. They decide whether it is "
                    "worth signing in, so tell them what they are deciding."
                ),
            },
        },
        "required": ["url", "reason"],
    }

    session: Any = None

    async def execute(self, url: str, reason: str, **kwargs: Any) -> ToolResult:
        if self.session is None:
            return ToolResult(
                error="Просьба войти работает только через веб-интерфейс."
            )
        if not urlparse(url).scheme:
            url = "https://" + url
        return ToolResult(output=await self.session.request_login(url, reason))
