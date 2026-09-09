"""Инструмент, которым агент записывает находку в журнал.

Зачем отдельный инструмент, если файлы и так умеет писать `str_replace_editor`.
Дописать строку в растущий файл им дорого: `create` затрёт файл целиком,
`insert` и `str_replace` требуют сначала прочитать содержимое и посчитать
смещения. В разобранном прогоне агент тратил на эту арифметику по шесть
действий из двадцати. Здесь дописывание — один вызов, файл читать не нужно,
затереть уже записанное невозможно.

Зачем вообще писать на диск. Память агента — сто последних сообщений, и в
длинном прогоне находки пятого шага к сорок пятому из неё вытеснены. Журнал
лежит в папке задачи, переживает и вытеснение, и перезапуск, и виден человеку
во вкладке «Файлы».
"""

from typing import Optional

from app.config import config
from app.flow.ledger import Ledger
from app.tool.base import BaseTool, ToolResult


class RecordFinding(BaseTool):
    """Дописать факт в findings.md — журнал находок этой задачи."""

    name: str = "record_finding"
    description: str = (
        "Append one finding to findings.md, the durable journal of this task. "
        "ONE CALL PER FACT, made as soon as you have the fact — not at the end.\n"
        "Record: every number, price, rate, share, date, name and conclusion you "
        "obtain, and every figure you compute. Also record what you could NOT "
        "get and why — a known gap is a finding too.\n"
        "Your conversation is a sliding window: what you learned early is dropped "
        "from it later in a long run. The journal is not — it is a file on disk, "
        "readable with str_replace_editor and visible to the person in Files. "
        "Anything you do not record here you will have to find again."
    )
    parameters: dict = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "(required) The finding itself, in the language of the task. "
                    "Start with a short first line — it becomes the heading in "
                    "the journal — then the detail: the value with its units, "
                    "what exactly it measures, and the period it covers. For a "
                    "figure you computed, give the inputs as well."
                ),
            },
            "source": {
                "type": "string",
                "description": (
                    "(required) Where it came from: the exact URL, or the name "
                    "of the file it was read from, or — for a figure you derived "
                    "— the tool and inputs you computed it with. If it is your "
                    "own unverified estimate, say so in these words."
                ),
            },
            "dated": {
                "type": "string",
                "description": (
                    "(optional) The date the SOURCE itself carries: the release "
                    "date, the reporting period, the date of the listing. Not "
                    "today's date. Data without a period cannot be compared."
                ),
            },
        },
        "required": ["fact", "source"],
    }

    # Заполняется веб-интерфейсом: у каждой задачи своя папка и свой журнал.
    directory: str = ""

    async def execute(
        self, fact: str, source: str, dated: Optional[str] = None, **kwargs
    ) -> ToolResult:
        if not (fact or "").strip():
            return ToolResult(error="Пустую находку записывать нечего.")
        ledger = Ledger(self.directory or config.workspace_root)
        total = ledger.note(fact, source or "", dated or "")
        return ToolResult(
            output=(
                f"Записано в журнал ({ledger.path}). Всего записей: {total}. "
                "Продолжайте работу."
            )
        )
