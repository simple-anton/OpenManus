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

Пишут в него с двух сторон. Поток-планировщик кладёт сюда итог каждого пункта
сам, как только пункт закончился. Сам агент — по ходу работы, инструментом
`record_finding`; в режиме «Агент», где плана и его пунктов нет, это
единственный путь, и без него находки теряются, как только сотня последних
сообщений вытеснит их из памяти агента.

Формат — обычный markdown: его читает и агент, и человек во вкладке «Файлы».
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.config import config
from app.logger import logger


FILE_NAME = "findings.md"

# Метка начала записи. Заголовок для этого не годится: агент вписывает в
# находки выдержки из источников, а на сайтах банков и статведомств строки
# «## Раздел 3. Комиссии» — обычное дело. Такая строка становилась поддельной
# записью: попадала в оглавление как отдельный факт, врала счётчику и служила
# границей обрезки, из-за чего «полный текст записи» мог начаться с середины
# чужой находки. Метка — наша, в markdown невидима, и из тела вычищается при
# записи, так что подделать её нечем.
MARKER = "<!-- om-запись -->"
ENTRY_START = re.compile(r"(?m)^" + re.escape(MARKER) + r"$")

# Журналы, записанные до появления метки, разбираем по-старому.
LEGACY_START = re.compile(r"(?m)^## ")

# Сколько заголовков опущенных записей перечислять. Это оглавление, а не
# содержание: его дело — подсказать, что искать в файле, и самому не разрастись.
MAX_OMITTED_LISTED = 60

# Сколько знаков первой строки находки берём в заголовок записи.
NOTE_HEADING = 90

HEADER = """# Журнал находок

Общая память задачи. Каждый факт — со ссылкой на источник и датой получения.
Всё, чего здесь нет, для дальнейшей работы не существует: разговор агента
короче задачи, а этот файл живёт до её конца.
"""


def _clean(text: str) -> str:
    """Убирает из текста агента нашу служебную метку.

    Агент может прочитать журнал и вставить кусок в новую находку — тогда
    метка попала бы в тело и снова разъехались бы границы записей. Вычищаем
    при записи: инвариант «метка = начало записи» должен держаться всегда.
    """
    return (text or "").replace(MARKER, "").strip()


def _starts(text: str) -> List[int]:
    """Смещения начал записей. По метке, а для старых журналов — по заголовку."""
    found = [match.start() for match in ENTRY_START.finditer(text)]
    return found or [match.start() for match in LEGACY_START.finditer(text)]


# Поля, которые пишет сюда наш же код: их можно доставать разбором, без модели.
FIELD = {
    "source": re.compile(r"(?m)^Источник:\s*(.+)$"),
    "dated": re.compile(r"(?m)^Дата источника:\s*(.+)$"),
    "essence": re.compile(r"(?m)^Суть:\s*(.+)$"),
}
HEADING = re.compile(r"(?m)^## (.+)$")

# Сколько знаков источника показывать в строке оглавления.
SOURCE_IN_LINE = 46


def _line(entry: str) -> str:
    """Строка оглавления: заголовок записи плюс то, чем её можно опознать.

    Всё берётся из записи дословно. Оглавление — карта, по которой агент
    решает, лезть ли в запись; искажение в ней он не заметит, потому что
    проверять не пойдёт. Поэтому здесь нет пересказа: только копирование.
    """
    heading = HEADING.search(entry)
    text = heading.group(1).strip() if heading else "(без заголовка)"
    essence = FIELD["essence"].search(entry)
    if essence:
        text += " — " + essence.group(1).strip()
    tail = []
    source = FIELD["source"].search(entry)
    if source:
        value = source.group(1).strip()
        value = re.sub(r"^https?://(www\.)?", "", value)
        if len(value) > SOURCE_IN_LINE:
            value = value[:SOURCE_IN_LINE].rstrip("/") + "…"
        tail.append(value)
    dated = FIELD["dated"].search(entry)
    if dated:
        tail.append(dated.group(1).strip())
    if tail:
        text += " — " + ", ".join(tail)
    return text


class Ledger:
    """Файл findings.md в папке задачи."""

    def __init__(self, folder: Path | str):
        self.path = Path(folder) / FILE_NAME

    def append(self, step_index: int, step_text: str, body: str,
               essence: str = "") -> None:
        """Дописывает итог шага. Пустые итоги не пишем — они только шумят.

        `essence` — одна строка о том, что пункт выяснил. У записей агента суть
        видна из заголовка (это первая строка находки) и из полей источника; у
        итога пункта заголовок говорит лишь, о чём пункт был. Поэтому здесь
        суть приходит отдельно, от читающей модели.
        """
        body = _clean(body)
        if not body:
            return
        entry = f"## Шаг {step_index}: {step_text}\n{self._stamp()}\n\n{body}\n"
        if essence.strip():
            entry += f"\nСуть: {_clean(essence)}\n"
        self._write(entry)

    def note(self, fact: str, source: str = "", dated: str = "") -> int:
        """Дописывает отдельную находку и возвращает, сколько их стало.

        Этим пишет сам агент, по ходу работы. Заголовок берём из первой строки
        находки: журнал читает и человек, и «## Находка» двести раз подряд ему
        ничего не скажет.
        """
        fact = _clean(fact)
        if not fact:
            return self.count()
        head = fact.splitlines()[0].strip()
        if len(head) > NOTE_HEADING:
            head = head[:NOTE_HEADING].rstrip() + "…"
        entry = f"## {head}\n{self._stamp()}\n\n{fact}\n"
        if source.strip():
            entry += f"\nИсточник: {_clean(source)}\n"
        if dated.strip():
            entry += f"Дата источника: {_clean(dated)}\n"
        self._write(entry)
        return self.count()

    def count(self) -> int:
        """Сколько записей уже в журнале."""
        try:
            return len(_starts(self.path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            return 0

    @staticmethod
    def _stamp() -> str:
        return "_записано " + datetime.now().strftime("%Y-%m-%d %H:%M") + "_"

    def _write(self, entry: str) -> None:
        """Дописывает готовую запись в файл, заводя его при первой записи."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(HEADER, encoding="utf-8")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("\n\n" + MARKER + "\n" + entry)
        except OSError as error:
            logger.warning(f"Журнал находок не записан: {error}")

    def read(self, limit: Optional[int] = None) -> str:
        """Журнал для постановки шага: целиком, если помещается.

        Если не помещается — последние записи полностью, а вместо ранних
        оглавление их заголовков. Прежде здесь был молчаливый обрыв: агент
        видел хвост и не знал ни что раньше что-то было, ни как это достать.
        Оглавление стоит копейки, а превращает потерю в отсылку к файлу.
        """
        limit = limit or config.agent_config.journal_chars
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        if len(text) <= limit:
            return text

        starts = _starts(text)
        if not starts:  # файл без записей — резать по границам нечего
            return text[-limit:]

        # берём с конца столько целых записей, сколько помещается
        cut = starts[-1]
        for start in reversed(starts):
            if len(text) - start > limit:
                break
            cut = start

        tail = text[cut:]
        if len(tail) > limit:
            # одна запись длиннее всего окна: отдаём её конец, но честно
            tail = "[…начало записи опущено…]\n" + tail[-limit:]
        omitted = [start for start in starts if start < cut]
        if not omitted:
            return tail
        return self._contents(text, omitted, len(starts)) + "\n\n" + tail

    def _contents(self, text: str, omitted: List[int], total: int) -> str:
        """Оглавление записей, которые в окно не поместились."""
        listed = omitted[-MAX_OMITTED_LISTED:]
        earlier = len(omitted) - len(listed)
        bounds = _starts(text) + [len(text)]
        lines = [
            f"[В журнале {total} записей, целиком они сюда не помещаются. "
            f"Ниже — оглавление {len(omitted)} ранних записей, а под ним полный "
            "текст последних. Ни одна запись не потеряна: любую из оглавления "
            f"прочитайте в файле {self.path} через str_replace_editor.]",
            "",
            "ОГЛАВЛЕНИЕ РАННИХ ЗАПИСЕЙ:",
        ]
        if earlier:
            lines.append(f"- […и ещё {earlier} записей до перечисленных]")
        for start in listed:
            after = next((edge for edge in bounds if edge > start), len(text))
            lines.append("- " + _line(text[start:after]))
        return "\n".join(lines)


# Сколько знаков итога вообще имеет смысл класть в журнал за один шаг.
# В разобранном прогоне журнал разросся до 920 000 знаков, и агент тратил по
# шесть действий из двадцати на то, чтобы найти в нём нужное место регулярками
# и срезами по смещениям. Причина была здесь: сюда попадала вся стенограмма
# шага — «Step 1: Observed output …» для каждого из двадцати действий.
MAX_ENTRY = 4_000

# Агент возвращает работу шага склейкой вида «Step 1: …\nStep 2: …». Ценна в
# ней последняя часть: там модель подводит итог. Остальное — сырые выдачи
# инструментов, которые агент и так выписал своими словами.
STEP_LINE = re.compile(r"(?m)^Step \d+: ")

# Кусок стенограммы, целиком состоящий из ответа инструмента.
TOOL_OUTPUT = re.compile(r"^(Observed output of cmd|Cmd `)")

# Признак того, что шаг оборвался на пределе действий, а не закончился.
OUT_OF_STEPS = "Reached max steps"


def summarise(result: str) -> str:
    """Оставляет от работы шага то, что стоит помнить дальше.

    Берём последние куски и идём к началу, пока не наберём лимит. Одного
    последнего куска мало: модель часто подводит итог, а следующим действием
    вызывает завершение — и от него в стенограмме остаётся строчка вроде
    «завершено», за которой весь смысл шага и потерялся бы.
    """
    text = (result or "").strip()
    if not text:
        return ""
    parts = [chunk.strip() for chunk in STEP_LINE.split(text) if chunk.strip()]
    # Сначала пробуем оставить только реплики модели: её выводы ценнее сырых
    # выдач инструментов, которые она и так пересказала своими словами.
    spoken = [chunk for chunk in parts if not TOOL_OUTPUT.match(chunk)]
    # Но у шага, оборванного на пределе действий, реплик и нет: он не успел
    # подвести итог, и весь его результат — одни выдачи инструментов. Отбросив
    # их, мы записывали в журнал пустоту под предупреждением «всё, что ниже» —
    # и следующий шаг не получал ни единого факта. Пусто хуже, чем сыро.
    parts = spoken or parts
    if not parts:
        return ""
    picked: List[str] = []
    size = 0
    for chunk in reversed(parts):
        if picked and size + len(chunk) > MAX_ENTRY:
            break
        picked.append(chunk if len(chunk) <= MAX_ENTRY
                      else "[…начало опущено…]\n" + chunk[-MAX_ENTRY:])
        size += len(picked[-1])
    return "\n\n".join(reversed(picked))


def spoken_parts(agent: object) -> List[str]:
    """Все реплики модели за этот шаг, по порядку и без среза."""
    memory = getattr(agent, "memory", None)
    messages = getattr(memory, "messages", None) or []
    return [
        (message.content or "").strip()
        for message in messages
        if getattr(message, "role", "") == "assistant" and (message.content or "").strip()
    ]


def spoken_all(agent: object) -> str:
    """Всё сказанное моделью за шаг целиком — вход для читающей модели."""
    return "\n\n".join(spoken_parts(agent))


def spoken(agent: object) -> str:
    """Слова самой модели за этот шаг — то, что она поняла и сказала.

    Почему не берём то, что вернул шаг. Строка, которую агент возвращает из
    run(), собрана из выдач инструментов и только из них: реплики модели уходят
    в её память отдельным сообщением и в эту строку не попадают вовсе
    (`ToolCallAgent.act`: `return "\n\n".join(results)`, где results — ответы
    инструментов). Поэтому в журнал вместо выводов шага попадало вот такое:

        Observed output of cmd `record_finding` executed: Записано в журнал…
        Observed output of cmd `terminate` executed: … status: success

    Ноль смысла — и эти же строки потом занимали место в журнале, который
    возвращается следующему шагу. Берём то, что модель написала сама.

    Память исполнителя перед пунктом очищается, так что все реплики в ней —
    этого шага. Набираем с конца назад: там выводы, а не планы на будущее.
    """
    parts = spoken_parts(agent)
    picked: List[str] = []
    size = 0
    for chunk in reversed(parts):
        if picked and size + len(chunk) > MAX_ENTRY:
            break
        picked.append(chunk if len(chunk) <= MAX_ENTRY
                      else "[…начало опущено…]\n" + chunk[-MAX_ENTRY:])
        size += len(picked[-1])
    return "\n\n".join(reversed(picked))


def ran_out_of_steps(result: str) -> bool:
    """Шаг не закончился, а упёрся в предел действий."""
    return OUT_OF_STEPS in (result or "")


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
    # Шаг, у которого просто кончились действия, не выполнен — он оборван.
    # В логе такой пункт обрывался посреди чтения банковского PDF и всё равно
    # получал зелёную галочку, а его пробелы нигде не фиксировались.
    if ran_out_of_steps(summary):
        return "partial"
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
