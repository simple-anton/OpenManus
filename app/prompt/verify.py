"""Промпты проверочного прохода.

B — сверка итогового отчёта с журналом находок: каждое существенное
утверждение отчёта должно опираться на запись в журнале (факт + источник +
дата). Проверяющий НЕ ходит в сеть — он сверяет отчёт с тем, что записано.

C — глубокая проверка одного спорного утверждения: страницу-источник
загружают заново, и проверяющий смотрит, правда ли она это утверждение
подтверждает. Это единственный способ поймать выдуманный или неверно
прочитанный источник.
"""

# --- B: отчёт против журнала -------------------------------------------------

VERIFY_PROMPT = """You check a finished analytical report against the task's
journal of findings. The journal is the only record of what was actually found,
with its sources; the report was written afterwards and may contain claims that
are NOT backed by any journal entry — that is exactly what you must catch.

Go through the report and pull out its MATERIAL claims: concrete facts, numbers,
prices, dates, named statements a decision could rest on. Ignore filler,
framing, and generic advice — only checkable factual claims.

For each claim, find the journal entry that supports it and decide a status:
- "supported"  — a journal entry states this, with a source.
- "no_source"  — the claim is in the report but no journal entry backs it
                 (the report invented or embellished it).
- "mismatch"   — a journal entry is about this, but says something different.
- "outdated"   — supported, but the source's date is old enough to doubt it.

Answer with STRICT JSON and nothing else — no prose, no markdown fences:

{"rows": [
  {"claim": "<the claim, short, in the report's language>",
   "status": "supported|no_source|mismatch|outdated",
   "source": "<the journal source that backs it, or empty>",
   "url": "<source URL if the journal has one, else empty>",
   "note": "<one short phrase in the report's language, why this status>"}
]}

Rules:
- Keep claim and note in the SAME language as the report (usually Russian).
- Do not invent sources or URLs: copy them from the journal or leave empty.
- If the report has no checkable claims, return {"rows": []}.
- Output the JSON object only.
"""

VERIFY_INPUT = """ОТЧЁТ ДЛЯ ПРОВЕРКИ:
{report}

--- ЖУРНАЛ НАХОДОК (единственный источник истины): ---
{journal}
"""


# --- C: одно утверждение против заново загруженной страницы -------------------

DEEP_VERIFY_PROMPT = """You verify ONE claim against the current text of its
source page, freshly downloaded just now. Decide whether the page actually
supports the claim:
- "supported" — the page clearly states this (or the number/fact is on it).
- "mismatch"  — the page is about this but says something different.
- "unclear"   — the page does not contain enough to confirm or deny it
                (page changed, moved, blocked, or simply does not cover this).

Answer with STRICT JSON and nothing else:

{"verdict": "supported|mismatch|unclear",
 "note": "<one short phrase in the claim's language, what the page actually says>"}
"""

DEEP_VERIFY_INPUT = """УТВЕРЖДЕНИЕ:
{claim}

--- ТЕКСТ СТРАНИЦЫ-ИСТОЧНИКА ({url}): ---
{page}
"""
