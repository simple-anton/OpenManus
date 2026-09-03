"""Turning failures into something a person can act on.

Two jobs. `explain` takes an exception from a run and says which part broke -
the model, a tool, the configuration or the environment - why, and what to do
about it. `health_checks` answers the same question before anything breaks:
is the config in place, does the model answer, did the MCP servers connect.
"""

import asyncio
import os
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.config import config
from app.web import settings as settings_store


MAX_TECHNICAL_CHARS = 2500


def unwrap(exc: BaseException) -> BaseException:
    """Retries wrap the real error; dig it out."""
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # tenacity keeps the original attempt
        last_attempt = getattr(current, "last_attempt", None)
        if last_attempt is not None:
            try:
                inner = last_attempt.exception()
            except Exception:  # pragma: no cover - defensive
                inner = None
            if inner is not None:
                current = inner
                continue
        if current.__cause__ is not None:
            current = current.__cause__
            continue
        return current
    return exc


def _model_hint(status: Optional[int], text: str) -> Optional[Dict[str, str]]:
    """Known shapes of an answer from an LLM server."""
    lowered = text.lower()
    if (
        status == 401
        or "authenticationerror" in lowered
        or "invalid api key" in lowered
    ):
        return {
            "title": "Модель отвергла ключ доступа",
            "why": "Сервер модели ответил «401 не авторизован»: ключ неверный или не подходит этому адресу.",
            "advice": "Настройки → Модель: проверьте «Ключ API» и «Адрес API», затем нажмите «Проверить связь». "
            "Для Ollama ключ может быть любым словом, а адрес должен заканчиваться на /v1.",
        }
    if status == 404 or "model not found" in lowered or "does not exist" in lowered:
        return {
            "title": "Сервер не знает такой модели",
            "why": "Имя модели в настройках не совпадает с тем, что установлено на сервере.",
            "advice": "Настройки → Модель: выберите имя из выпадающего списка — он берётся с самого сервера "
            "(для Ollama это то, что показывает ollama list).",
        }
    if status == 429 or "rate limit" in lowered:
        return {
            "title": "Сервер модели просит подождать",
            "why": "Слишком много запросов подряд или закончилась квота.",
            "advice": "Подождите минуту и повторите. Для облачных моделей проверьте лимиты тарифа.",
        }
    if status and status >= 500:
        return {
            "title": f"Сервер модели ответил ошибкой {status}",
            "why": "Проблема на стороне сервера модели, а не в OpenManus.",
            "advice": "Проверьте, жива ли Ollama (окно с ollama serve) и хватает ли памяти для этой модели.",
        }
    return None


def explain(exc: BaseException) -> Dict[str, Any]:
    """Describe a failure: whose it is, what happened, what to do."""
    original = unwrap(exc)
    name = type(original).__name__
    module = type(original).__module__.split(".")[0]
    text = str(original)
    technical = "".join(
        traceback.format_exception(type(original), original, original.__traceback__)
    )[-MAX_TECHNICAL_CHARS:]

    status = getattr(getattr(original, "response", None), "status_code", None)
    if status is None:
        status = getattr(original, "status_code", None)

    result: Dict[str, Any] = {
        "source": "OpenManus",
        "title": f"{name}: {text[:200]}",
        "why": "",
        "advice": "",
        "technical": technical,
        "exception": name,
    }

    if isinstance(original, asyncio.TimeoutError):
        result.update(
            source="OpenManus",
            title="Задача не уложилась в отведённое время",
            why="Выполнение шло дольше лимита и было прервано.",
            advice="Разбейте задачу на части или увеличьте «Максимум шагов» в настройках задачи.",
        )
        return result

    if name == "TokenLimitExceeded":
        result.update(
            source="Модель",
            title="Упёрлись в лимит входных токенов",
            why=text
            or "Суммарный объём отправленного в модель превысил заданный лимит.",
            advice="Настройки → Модель: поднимите «Лимит входных токенов» или оставьте поле пустым. "
            "Ещё помогает начать новую задачу — история очистится.",
        )
        return result

    if module in {"openai", "httpx", "httpcore"} or "APIConnectionError" in name:
        hint = _model_hint(status, f"{name} {text}")
        if hint:
            result.update(source="Модель", **hint)
            return result
        if "connect" in name.lower() or "connect" in text.lower():
            llm = config.llm.get("default")
            address = llm.base_url if llm else "адрес не задан"
            result.update(
                source="Модель",
                title="Не удалось соединиться с сервером модели",
                why=f"OpenManus не достучался до {address}.",
                advice="Проверьте, запущена ли Ollama и слушает ли она снаружи "
                "(в терминале: lsof -nP -iTCP:11434 -sTCP:LISTEN — должно быть *:11434). "
                "Из Docker адрес хоста — host.docker.internal, а не localhost.",
            )
            return result
        result.update(
            source="Модель",
            title=f"Ошибка при обращении к модели: {name}",
            why=text[:400],
            advice="Откройте вкладку «Модель» в правой панели — там видно, что уходило в модель последним.",
        )
        return result

    if module == "pydantic" or name == "ValidationError":
        result.update(
            source="Настройки",
            title="Настройки не проходят проверку",
            why=text[:400],
            advice="Настройки: поправьте поле, на которое ругается сообщение, и сохраните заново.",
        )
        return result

    if module in {"mcp", "anyio"} or "mcp" in text.lower():
        result.update(
            source="Плагин (MCP)",
            title="Сбой при работе с MCP-сервером",
            why=text[:400],
            advice="Настройки → Плагины: выключите подозрительный сервер и запустите задачу снова. "
            "Серверам на npx нужен Node.js в образе, серверам с токенами — заполненные переменные.",
        )
        return result

    result.update(
        why=text[:400] or "Внутренняя ошибка OpenManus.",
        advice="Подробности — во вкладке «Логи». Если ошибка повторяется, покажите этот блок разработчику.",
    )
    return result


def tool_failure(name: str, output: str) -> Dict[str, Any]:
    """A tool returned an error instead of a result.

    Some of these are the tool's fault, some are the model's: a small model
    often produces arguments that are not valid JSON, or invents a tool that
    does not exist. The interface should not blame the wrong side.
    """
    head = output.strip()[:400]
    lowered = head.lower()

    if "invalid json" in lowered or "parsing arguments" in lowered:
        return {
            "source": "Модель",
            "title": "Модель прислала неразборчивые аргументы инструмента",
            "why": f"Вызов {name} не разобрался: то, что модель выдала вместо JSON, прочитать не удалось. "
            "Так обычно ведут себя небольшие модели.",
            "advice": "Понизьте «Температуру» до 0 в настройках модели или возьмите модель покрупнее. "
            "Что именно она прислала — видно во вкладке «Модель».",
        }
    if "unknown tool" in lowered:
        return {
            "source": "Модель",
            "title": "Модель попросила несуществующий инструмент",
            "why": head,
            "advice": "Модель придумала инструмент, которого нет. Список доступных — во вкладке «Инструменты». "
            "Помогает более чёткая формулировка задачи или модель посильнее.",
        }
    if "timeout" in lowered or "timed out" in lowered:
        return {
            "source": f"Инструмент {name}",
            "title": f"Инструмент {name} не уложился во время",
            "why": head,
            "advice": "Операция шла слишком долго и была прервана. Для кода помогает разбить его на части.",
        }
    advice = "Посмотрите шаг в правой панели: там видно, с какими аргументами инструмент вызывали."
    if name == "python_execute":
        advice = "Код агента упал. Терминал в правой панели показывает сам код и текст ошибки."
    elif name == "str_replace_editor":
        advice = "Файловая операция не прошла: обычно это неверный путь. Агент работает в папке задачи."
    elif name.startswith("browser"):
        advice = "Браузерный инструмент не справился со страницей. Снимок последнего состояния — во вкладке «Браузер»."
    return {
        "source": f"Инструмент {name}",
        "title": f"Инструмент {name} вернул ошибку",
        "why": head,
        "advice": advice,
    }


async def preflight() -> Optional[Dict[str, Any]]:
    """Cheap check before a run starts.

    Without it a wrong key or a stopped Ollama sends the agent into six retries
    with growing pauses - minutes of silence for something that will never
    succeed. This asks the server for its model list, which costs nothing and
    does not load the model.
    """
    llm = config.llm.get("default")
    if llm is None or not llm.model:
        return {
            "source": "Настройки",
            "title": "Модель не настроена",
            "why": "В настройках не указано, какой моделью пользоваться.",
            "advice": "Откройте «Настройки → Модель», заполните имя модели и адрес API.",
        }

    url = llm.base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {llm.api_key}"}
            )
    except httpx.HTTPError as exc:
        return {
            "source": "Модель",
            "title": "Сервер модели недоступен",
            "why": f"OpenManus не достучался до {llm.base_url}: {exc}",
            "advice": "Проверьте, запущена ли Ollama и виден ли её адрес из контейнера. "
            "Вкладка «Проверка» в правой панели покажет то же самое подробнее.",
            "technical": f"GET {url}\n{type(exc).__name__}: {exc}",
        }

    if response.status_code in {401, 403}:
        hint = _model_hint(401, "") or {}
        return {"source": "Модель", "technical": response.text[:500], **hint}

    return None


# ------------------------------------------------------------------- health


async def health_checks() -> List[Dict[str, Any]]:
    """What the interface can verify about the setup right now."""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: Optional[bool], detail: str, advice: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "advice": advice})

    # configuration
    exists = settings_store.CONFIG_PATH.exists()
    add(
        "Файл настроек",
        exists,
        (
            str(settings_store.CONFIG_PATH)
            if exists
            else "своего файла нет, взяты значения из примера"
        ),
        (
            ""
            if exists
            else "Откройте настройки и нажмите «Сохранить», чтобы создать свой файл."
        ),
    )

    llm = config.llm.get("default")
    add(
        "Модель в настройках",
        bool(llm and llm.model),
        f"{llm.model} → {llm.base_url}" if llm else "не задана",
    )

    # can we reach the model server, and does it know this model
    if llm:
        base = llm.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    base + "/chat/completions",
                    json={
                        "model": llm.model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 4,
                    },
                    headers={"Authorization": f"Bearer {llm.api_key}"},
                )
            if response.status_code == 200:
                add("Ответ модели", True, "модель отвечает на пробный запрос")
            else:
                hint = _model_hint(response.status_code, response.text) or {}
                add(
                    "Ответ модели",
                    False,
                    f"HTTP {response.status_code}: {response.text[:160]}",
                    hint.get("advice", ""),
                )
        except httpx.HTTPError as exc:
            add(
                "Ответ модели",
                False,
                f"нет соединения: {exc}",
                "Проверьте, запущена ли Ollama и виден ли её адрес из контейнера.",
            )

    # tools environment
    add(
        "Python-серверы MCP (uvx)",
        shutil.which("uvx") is not None,
        "uvx найден" if shutil.which("uvx") else "uvx не найден",
        "" if shutil.which("uvx") else "Без uvx питоновские плагины не запустятся.",
    )
    node = settings_store.node_available()
    add(
        "npm-серверы MCP (npx)",
        node,
        "npx найден" if node else "npx нет в контейнере",
        (
            ""
            if node
            else "Плагины на npm не запустятся. Node.js добавляется в Dockerfile и требует пересборки образа."
        ),
    )

    stored = settings_store.read_mcp()
    configured = list(stored.get("mcpServers", {}))
    add(
        "Плагины MCP",
        None if not configured else True,
        ", ".join(configured) if configured else "не подключены",
    )

    browser_off = os.getenv("OPENMANUS_DISABLE_BROWSER_USE", "").lower() in {
        "1",
        "true",
        "yes",
    }
    add(
        "Браузер (browser-use)",
        None if browser_off else True,
        (
            "выключен переменной OPENMANUS_DISABLE_BROWSER_USE"
            if browser_off
            else "подключается при запуске задачи, качается из сети при первом запуске"
        ),
    )

    # workspace
    root = Path(config.workspace_root)
    writable = os.access(root, os.W_OK) if root.exists() else False
    usage = shutil.disk_usage(root) if root.exists() else None
    add(
        "Папка workspace",
        writable,
        f"{root} — свободно {usage.free // (1024 ** 3)} ГБ" if usage else str(root),
        (
            ""
            if writable
            else "Папка недоступна на запись: проверьте монтирование тома в docker-compose.yml."
        ),
    )

    return checks
