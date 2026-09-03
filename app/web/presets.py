"""Quick tasks - the chips under the input.

A preset is a starting point: a prompt template, the mode it should run in, and
the skills it needs. Clicking one fills the composer instead of sending, so the
text can be adjusted before the agent starts.
"""

import json
from typing import Any, Dict, List

from app.config import PROJECT_ROOT
from app.logger import logger


PRESETS_PATH = PROJECT_ROOT / "config" / "presets.json"

# Written for what this build can actually do: run python, edit files, browse.
DEFAULT_PRESETS = [
    {
        "id": "analyse-file",
        "title": "Разобрать файл",
        "prompt": "Разбери файл из папки задачи: опиши, что в нём, найди главное "
        "и собери выводы в отдельный markdown-файл.",
        "mode": "agent",
        "skills": [],
    },
    {
        "id": "collect-web",
        "title": "Собрать данные из сети",
        "prompt": "Найди в интернете актуальные данные по теме: <тема>. "
        "Проверь по нескольким источникам, собери таблицей в файл и укажи ссылки.",
        "mode": "agent",
        "skills": [],
    },
    {
        "id": "write-script",
        "title": "Написать скрипт",
        "prompt": "Напиши и проверь на запуске python-скрипт, который: <что должен делать>. "
        "Сохрани его в папку задачи и покажи пример работы.",
        "mode": "agent",
        "skills": [],
    },
    {
        "id": "make-page",
        "title": "Сделать страницу",
        "prompt": "Собери одностраничный сайт: <о чём>. Один HTML-файл со встроенными "
        "стилями, аккуратная типографика, работает без интернета. Сохрани в папку задачи.",
        "mode": "agent",
        "skills": [],
    },
    {
        "id": "report",
        "title": "Подготовить отчёт",
        "prompt": "Подготовь отчёт по теме: <тема>. Сначала распиши план, потом собери "
        "материал и сведи в один документ с выводами и источниками.",
        "mode": "flow",
        "skills": [],
    },
    {
        "id": "ask",
        "title": "Просто спросить",
        "prompt": "",
        "mode": "chat",
        "skills": [],
    },
]


def read_presets() -> List[Dict[str, Any]]:
    if not PRESETS_PATH.is_file():
        return [dict(item) for item in DEFAULT_PRESETS]
    try:
        stored = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read the presets: {exc}")
        return [dict(item) for item in DEFAULT_PRESETS]
    return stored.get("presets", []) if isinstance(stored, dict) else stored


def write_presets(presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []
    for index, item in enumerate(presets):
        title = (item.get("title") or "").strip()
        if not title:
            continue  # a chip without a name is a draft the user abandoned
        cleaned.append(
            {
                "id": (item.get("id") or f"preset-{index}").strip(),
                "title": title[:40],
                "prompt": (item.get("prompt") or "")[:2000],
                "mode": (
                    item.get("mode")
                    if item.get("mode") in {"agent", "flow", "chat"}
                    else "agent"
                ),
                "skills": [str(slug) for slug in item.get("skills", [])][:10],
            }
        )
    PRESETS_PATH.write_text(
        json.dumps({"presets": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cleaned


def reset_presets() -> List[Dict[str, Any]]:
    return write_presets([dict(item) for item in DEFAULT_PRESETS])
