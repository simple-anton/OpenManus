"""Поиск через настоящие поисковые API — с ключом, без скрейпинга.

Встроенные движки (Google, DuckDuckGo, Bing, Baidu) — это скрейперы: они
притворяются человеком у обычной поисковой страницы. В разборе логов видно,
чем это заканчивается на практике: Google отвечает «unusual traffic»,
DuckDuckGo показывает капчу, и задача встаёт намертво. Скрейпер не чинится
настройками — он ломается по решению чужой стороны.

Поисковые API решают ровно эту проблему: запрос уходит по документированному
интерфейсу с ключом, а не маскируется под браузер. Здесь их три на выбор, у
каждого есть бесплатный тариф:

* Tavily  — сделан для агентов, возвращает сразу выжимку текста страницы;
* Brave   — собственный индекс, не посредник к Google;
* Serper  — выдача Google через API.

Ключ хранится в config.toml (раздел [search], поле api_key) и вводится через
Настройки в интерфейсе. Без ключа движок честно говорит, чего ему не хватает,
и WebSearch переходит к следующему в списке.
"""

from typing import Any, Dict, List, Optional

import requests

from app.logger import logger
from app.tool.search.base import SearchItem, WebSearchEngine


TIMEOUT = 20


class ApiSearchEngine(WebSearchEngine):
    """Общая часть: ключ, запрос, разбор ответа."""

    api_key: Optional[str] = None
    label: str = "API"
    key_hint: str = ""

    def perform_search(
        self, query: str, num_results: int = 10, *args, **kwargs
    ) -> List[SearchItem]:
        if not self.api_key:
            raise RuntimeError(
                f"{self.label}: не задан ключ доступа. {self.key_hint} "
                "Вставьте его в Настройках → Поиск."
            )
        payload = self._request(query, num_results, kwargs)
        items = self._parse(payload)
        if not items:
            logger.warning(f"{self.label}: пустая выдача по запросу {query!r}")
        return items[:num_results]

    def _request(self, query: str, num_results: int, extra: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def _parse(self, payload: Any) -> List[SearchItem]:
        raise NotImplementedError


class TavilySearchEngine(ApiSearchEngine):
    """https://tavily.com — бесплатно 1000 запросов в месяц."""

    label: str = "Tavily"
    key_hint: str = "Ключ бесплатно на tavily.com, вид: tvly-…"

    def _request(self, query: str, num_results: int, extra: Dict[str, Any]) -> Any:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": max(1, min(num_results, 20)),
                "search_depth": extra.get("depth", "advanced"),
                # выжимка страницы приходит вместе с выдачей: это экономит
                # агенту отдельный заход за каждой ссылкой
                "include_raw_content": False,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _parse(self, payload: Any) -> List[SearchItem]:
        items = []
        for entry in payload.get("results", []):
            items.append(
                SearchItem(
                    title=entry.get("title") or entry.get("url", ""),
                    url=entry.get("url", ""),
                    description=entry.get("content") or None,
                )
            )
        # Tavily умеет сразу отвечать на вопрос; ответ ставим первым абзацем
        answer = payload.get("answer")
        if answer:
            items.insert(
                0,
                SearchItem(
                    title="Сводный ответ Tavily",
                    url=payload.get("results", [{}])[0].get("url", "") if payload.get("results") else "",
                    description=answer,
                ),
            )
        return items


class BraveSearchEngine(ApiSearchEngine):
    """https://brave.com/search/api — бесплатно 2000 запросов в месяц."""

    label: str = "Brave"
    key_hint: str = "Ключ бесплатно на brave.com/search/api."

    def _request(self, query: str, num_results: int, extra: Dict[str, Any]) -> Any:
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max(1, min(num_results, 20))},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _parse(self, payload: Any) -> List[SearchItem]:
        return [
            SearchItem(
                title=entry.get("title", ""),
                url=entry.get("url", ""),
                description=entry.get("description") or None,
            )
            for entry in payload.get("web", {}).get("results", [])
        ]


class SerperSearchEngine(ApiSearchEngine):
    """https://serper.dev — выдача Google, бесплатно 2500 запросов разово."""

    label: str = "Serper"
    key_hint: str = "Ключ бесплатно на serper.dev."

    def _request(self, query: str, num_results: int, extra: Dict[str, Any]) -> Any:
        response = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": max(1, min(num_results, 20))},
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _parse(self, payload: Any) -> List[SearchItem]:
        items = []
        box = payload.get("answerBox") or {}
        if box.get("snippet"):
            items.append(
                SearchItem(
                    title=box.get("title", "Быстрый ответ Google"),
                    url=box.get("link", ""),
                    description=box["snippet"],
                )
            )
        for entry in payload.get("organic", []):
            items.append(
                SearchItem(
                    title=entry.get("title", ""),
                    url=entry.get("link", ""),
                    description=entry.get("snippet") or None,
                )
            )
        return items
