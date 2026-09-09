"""Пересказ длинного ответа инструмента вместо его обрезки.

Задача. Ответ инструмента обрезался по порогу видимости, и отрезанное модель
не читала уже никогда: обрезка происходит до обращения к ней. На замере восьми
источников по 20 000 знаков из 162 750 до агента доходило 60 000 — три адреса
из восьми, остальных пяти для него не существовало.

Решение. Всё, что не помещается, не отрезаем, а отдаём читающей модели: она
проходит текст кусками, целиком, и возвращает выжимку под задачу шага. В поле
зрения агента попадает пересказ всего источника, а не начало одного.

Чем это отличается от обрезки. Обрезка теряет хвост — видимо и предсказуемо.
Пересказ теряет то, что читающая модель сочла неважным, — незаметно, и может
приписать лишнее. Поэтому здесь три требования к ней: числа приводить дословно
с окружающей фразой, ничего не досочинять, и честно писать, чего в куске нет.
А оригинал остаётся на диске: любой пересказ можно проверить.

Чего здесь намеренно нет: пересказа выдач `python_execute` и файловых
инструментов. Там результат вычисления и содержимое файлов — их надо видеть
дословно, пересказ таких вещей только вредит. Список исключений — в
`toolcall.py`, у места вызова.
"""

import asyncio
import re
from typing import List, Optional, Tuple

from app.logger import logger
from app.schema import Message


# Метка, по которой видно, что это пересказ, а не сам ответ инструмента. Нужна
# и агенту (чтобы он понимал, что читает), и сворачиванию старых сообщений
# (чтобы оно не заменяло уже сжатое заглушкой).
MARK = "[СЖАТО читающей моделью]"

# Сколько знаков отдаём читающей модели за одно обращение.
CHUNK = 40_000

# Больше стольких обращений на один ответ инструмента не делаем. Размер куска
# при этом растёт, чтобы прочитан был весь текст: лучше кусок побольше, чем
# непрочитанный хвост.
MAX_CALLS = 12

# Сколько пересказов делаем одновременно. Больше — упираемся в ограничения
# провайдера по частоте запросов.
AT_ONCE = 3

SYSTEM = """\
You are the reading stage of a research agent. You are given ONE PART of a
source the agent fetched, and the task the agent is working on. You return a
digest of that part. You never talk to the user and never do the task itself.

Rules, in order of importance:

1. QUOTE FIGURES VERBATIM. Every number, rate, share, price, date, deadline and
   proper name that could matter goes into the digest exactly as written in the
   source, inside quotation marks, together with enough of its own sentence to
   say what it measures. "12,0% годовых с 4 августа 2026" — not "about 12%".
   Keep the source's units, currency and wording. Never round, convert or
   translate a figure.
2. INVENT NOTHING. If the part does not say something, it does not go in. You
   have no knowledge of your own here: everything in your answer must be
   traceable to the text in front of you.
3. SAY WHAT IS NOT THERE. End with one short line naming what the task asked
   for and this part does not contain. An honest gap tells the agent where to
   look next; silence makes it believe the source was exhausted.
4. Keep what is relevant to the task, drop navigation, menus, cookie notices,
   boilerplate and repeated headers.
5. Answer in the language of the task.

Format:
FACTS — a bullet per figure or statement worth keeping, with its quote.
ABOUT — one or two lines: what this part of the source is.
MISSING — one line: what the task needs and this part does not give.
"""

ASK = """\
TASK THE AGENT IS WORKING ON:
{task}

PART {number} OF {total} OF THE SOURCE (characters {start}-{end} of {whole}):
{text}
"""


def split(text: str, calls: int = MAX_CALLS, size: int = CHUNK) -> List[str]:
    """Режет текст на куски по границам абзацев.

    Если кусков вышло бы больше, чем мы готовы сделать обращений, увеличиваем
    размер куска. Прочитан должен быть весь текст: выбросить хвост здесь
    значило бы вернуть ту самую потерю, ради которой всё и затевалось.
    """
    if len(text) <= size:
        return [text]
    size = max(size, -(-len(text) // calls))
    pieces: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # ищем ближайший перевод строки назад, чтобы не рвать фразу
            border = text.rfind("\n", start + size // 2, end)
            if border > start:
                end = border
        pieces.append(text[start:end])
        start = end
    return pieces


async def _one(llm, text: str, task: str, number: int, total: int,
               start: int, whole: int) -> Tuple[bool, str]:
    """Пересказ одного куска. Неудача не роняет остальные, но себя называет."""
    try:
        answer = await llm.ask(
            messages=[Message.user_message(ASK.format(
                task=task or "(задача не передана — сохраните всё, что похоже на данные)",
                number=number, total=total, start=start, end=start + len(text),
                whole=whole, text=text))],
            system_msgs=[Message.system_message(SYSTEM)],
            stream=False,
        )
    except Exception as error:  # читающая модель не должна ронять шаг
        logger.warning(f"Пересказ куска {number}/{total} не удался: {error}")
        return False, (
            f"[часть {number} из {total} (знаки {start}-{start + len(text)}) "
            f"прочитать не удалось: {error}. Она есть в сохранённом файле.]"
        )
    return True, (
        f"— часть {number} из {total} (знаки {start}-{start + len(text)}) —\n"
        + answer.strip()
    )


async def condense(llm, text: str, task: str = "", budget: Optional[int] = None,
                   source: str = "") -> str:
    """Пересказывает длинный текст целиком, кусок за куском.

    `budget` — во сколько знаков желательно уложиться. Если пересказ вышел
    длиннее, он пересказывается ещё раз, уже сам себя. Если и это не помогло,
    честно обрезаем и говорим об этом: молчаливого обрыва здесь быть не должно.
    """
    pieces = split(text)
    offsets, at = [], 0
    for piece in pieces:
        offsets.append(at)
        at += len(piece)

    limit = asyncio.Semaphore(AT_ONCE)

    async def run(index: int, piece: str) -> str:
        async with limit:
            return await _one(llm, piece, task, index + 1, len(pieces),
                              offsets[index], len(text))

    answers = await asyncio.gather(
        *(run(index, piece) for index, piece in enumerate(pieces))
    )
    # Если не прочитан ни один кусок, пересказ вырождается в набор извинений.
    # Отдать его агенту вместо текста было бы хуже обрезки: обрезка сохраняет
    # хотя бы начало настоящих данных. Пусть решает вызывающий.
    if not any(ok for ok, _ in answers):
        raise RuntimeError(
            f"читающая модель не осилила ни одной из {len(pieces)} частей"
        )
    parts = [text for _, text in answers]
    head = (f"{MARK} прочитано {len(text)} знаков"
            + (f" источника {source}" if source else "")
            + f", {len(pieces)} частей. Ниже — выжимка под задачу шага; "
              "числа приведены дословно.")
    if source:
        head += f"\nПолный текст: {source}"
    body = "\n\n".join(parts)
    digest = head + "\n\n" + body

    if budget and len(digest) > budget:
        logger.info(f"Выжимка длиннее окна ({len(digest)} > {budget}) — сжимаем ещё раз")
        again, second = await _one(llm, body, task, 1, 1, 0, len(body))
        if again:
            digest = head + "\n\n" + second
    if budget and len(digest) > budget:
        digest = digest[:budget] + (
            f"\n\n[…выжимка не уместилась в {budget} знаков и обрезана здесь. "
            + (f"Полный текст источника: {source}]" if source
               else "Оригинал — в ответе инструмента, который не сохранялся.]")
        )
    return digest


ESSENCE_SYSTEM = """\
You compress the outcome of one research step into ONE line for a table of
contents. The line is what a later step reads to decide whether to open the
full entry, so it must say what was ESTABLISHED, not what was attempted.

Rules: quote figures exactly as given, with units; name the sources briefly;
if the step established nothing, say exactly that and why. No preamble, no
formatting, no more than 200 characters. Answer in the language of the step.
"""


async def essence(llm, summary: str, step: str = "") -> str:
    """Одна строка о том, что пункт выяснил. Пустая, если не вышло.

    У находок агента суть видна из заголовка и полей источника; у итога пункта
    заголовок говорит лишь, о чём пункт был. Эта строка ложится в запись
    отдельным полем и потом попадает в оглавление — уже без всякой модели.
    """
    summary = (summary or "").strip()
    if not summary:
        return ""
    try:
        answer = await llm.ask(
            messages=[Message.user_message(
                f"ПУНКТ ПЛАНА: {step}\n\nЧТО ПО НЕМУ ПОЛУЧИЛОСЬ:\n{summary[:12_000]}"
            )],
            system_msgs=[Message.system_message(ESSENCE_SYSTEM)],
            stream=False,
        )
    except Exception as error:  # без сути обойдёмся, без пункта — нет
        logger.warning(f"Суть пункта не получена: {error}")
        return ""
    return " ".join(answer.split())[:300]


def is_digest(text: str) -> bool:
    """Это уже пересказ — второй раз его сжимать незачем."""
    return text.lstrip().startswith(MARK)


# Имена файлов, которые fetch сообщает строкой «сохранено в файл: …».
SAVED = re.compile(r"^сохранено в файл: (.+)$", re.MULTILINE)


def saved_files(text: str) -> List[str]:
    """Какие файлы инструмент положил на диск — чтобы назвать их в выжимке."""
    return [match.group(1).strip() for match in SAVED.finditer(text or "")]
