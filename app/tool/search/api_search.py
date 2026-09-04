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
                # Сводный ответ по запросу. Без этого поля Tavily его не
                # присылает, а он часто и есть то, что агенту нужно: короткая
                # выжимка с опорой на найденные источники.
                "include_answer": True,
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
        # Tavily умеет сразу отвечать на вопрос — но это пересказ найденных
        # страниц другой моделью, и он ошибается. На живой проверке по индексу
        # цен ЦБА этот пересказ выдал 5,4% там, где в самом отчёте ЦБА стоит
        # 12,0%: он подхватил числа из соседней колонки за прошлый год.
        # Поэтому ответ помечен как ориентир и снабжён прямым запретом
        # ссылаться на него: он годится, чтобы понять, куда смотреть, и не
        # годится как источник цифры.
        answer = payload.get("answer")
        if answer:
            items.insert(
                0,
                SearchItem(
                    title="ОРИЕНТИР (пересказ, НЕ ИСТОЧНИК)",
                    url="",
                    description=(
                        f"{answer}\n\n"
                        "[Это машинный пересказ страниц ниже, а не первоисточник. "
                        "Числа в нём регулярно оказываются неверными. "
                        "Используйте его только чтобы выбрать, какую ссылку "
                        "открыть. Ни одна цифра отсюда не должна попасть в "
                        "отчёт без проверки по самому источнику.]"
                    ),
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
