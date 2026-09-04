"""Журнал находок — общая память шагов плана.

Проблема, которую он решает, видна в логах прогона. Каждый шаг плана получает
свежего исполнителя, и память агента ограничена сотней сообщений: то, что
агент нашёл на шаге 3, к шагу 9 из неё уже вытеснено. На шаге сборки отчёта
агент честно писал: «нужно перепроверить цифры шагов 3–7, которые не
сохранились в файлах» — и шёл собирать их заново. Во второй раз источники
оказались закрыты, и цифры пропали совсем.

Журнал делает память шагов долговечной, потому что хранит её на диске, а не в
контексте модели. Каждый шаг обязан записать сюда то, что нашёл, со ссылкой на
источник и датой. Следующий шаг получает журнал в своей постановке задачи и
видит и цифры, и откуда они взяты.

Формат — обычный markdown: его читает и агент, и человек во вкладке «Файлы».
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.logger import logger


FILE_NAME = "findings.md"

# Сколько знаков журнала отдавать шагу. Журнал растёт весь прогон, а контекст
# модели — нет; при переполнении отдаём последние записи, они свежее.
MAX_DIGEST = 12_000

HEADER = """# Журнал находок

Общая память всех шагов плана. Каждый факт — со ссылкой на источник и датой
получения. Всё, чего здесь нет, для следующих шагов не существует.
"""


class Ledger:
    """Файл findings.md в папке задачи."""

    def __init__(self, folder: Path | str):
        self.path = Path(folder) / FILE_NAME

    def append(self, step_index: int, step_text: str, body: str) -> None:
        """Дописывает итог шага. Пустые итоги не пишем — они только шумят."""
        body = (body or "").strip()
        if not body:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n\n## Шаг {step_index}: {step_text}\n_записано {stamp}_\n\n{body}\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(HEADER, encoding="utf-8")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(entry)
        except OSError as error:
            logger.warning(f"Журнал находок не записан: {error}")

    def read(self, limit: int = MAX_DIGEST) -> str:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        if len(text) <= limit:
            return text
        # режем по границе записи, чтобы не оборвать факт на полуслове
        tail = text[-limit:]
        cut = tail.find("\n## ")
        return "[…начало журнала опущено…]\n" + (tail[cut:] if cut > 0 else tail)


# Итог шага агент помечает этой строкой. Разбираем её мягко: модель может
# написать по-русски, по-английски, с двоеточием или без.
OUTCOME = re.compile(
    r"(?:^|\n)\s*(?:ИТОГ\s+ШАГА|STEP\s+RESULT)\s*[:\-—]?\s*"
    r"(выполнено|частично|не\s*удалось|done|partial|blocked|failed)",
    re.IGNORECASE,
)

_DONE = {"выполнено", "done"}
_PARTIAL = {"частично", "partial"}


def outcome_of(summary: str) -> str:
    """Что шаг сам о себе сообщил: completed / partial / blocked.

    Без такой отметки шаг всегда считался выполненным — даже когда все его
    действия упёрлись в капчу и он не принёс ни одной цифры. Отчёт в конце
    выглядел собранным по полному плану, хотя треть плана не состоялась.
    По умолчанию считаем шаг выполненным: молчание модели не повод рушить
    прогон, но явное признание неудачи мы обязаны сохранить.
    """
    match = OUTCOME.search(summary or "")
    if not match:
        return "completed"
    word = re.sub(r"\s+", " ", match.group(1).strip().lower())
    if word in _DONE:
        return "completed"
    if word in _PARTIAL:
        return "partial"
    return "blocked"


def digest(records: List[dict], keep: int = 4, per_step: int = 700) -> str:
    """Короткая сводка последних шагов — для постановки задачи следующему.

    Журнал на диске полный, но он может быть длинным. Здесь — свежая выжимка,
    чтобы модель видела ближайший контекст, даже не открывая файл.
    """
    if not records:
        return ""
    lines = []
    for record in records[-keep:]:
        summary = (record.get("summary") or "").strip()
        if len(summary) > per_step:
            summary = summary[:per_step] + " […]"
        mark = {"completed": "выполнен", "partial": "частично", "blocked": "не удался"}
        lines.append(
            f"- Шаг {record['index']} ({mark.get(record.get('status'), '?')}): "
            f"{record.get('text', '')}\n  {summary}"
        )
    return "\n".join(lines)
