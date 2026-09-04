"""Agents instrumented for the web interface.

The console agents report what they do through log lines. The browser needs
structured events instead - which tool is running, with which arguments, what
it returned and whether it produced a screenshot - so the UI can mirror the
agent's work the way the hosted Manus does.
"""

import ast
import base64
import json
import os
import re
import shutil
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent.manus import Manus
from app.logger import logger
from app.schema import ToolCall
from app.web import diagnostics


# Screenshots live inside the container, not in the mounted workspace: they are
# working material, and the user asked for them to go away with the container.
# Kept as files so the transcript still shows them after a page reload, once the
# in-memory copy has been evicted.
SHOTS_DIR = Path(os.getenv("OPENMANUS_SHOTS", "/tmp/openmanus-screenshots"))
MAX_SHOTS_PER_TASK = 200

# tool output shown in the UI; the agent itself still sees max_observe chars
MAX_OUTPUT_CHARS = 20000
# how much of the conversation with the model the Model tab shows
EXCHANGE_MESSAGES = 8
MAX_EXCHANGE_CHARS = 4000


def _parse_args(command: ToolCall) -> Dict[str, Any]:
    try:
        return json.loads(command.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"_raw": command.function.arguments or ""}


def _looks_failed(result: str) -> bool:
    """Tool errors come back wrapped in the observation text, not as exceptions."""
    head = result.lstrip()[:400]
    return head.startswith("Error") or "\nError:" in head or "Error: " in head


def _readable(output: str) -> str:
    """Tool output without the machinery around it.

    A tool answers with `Observed output of cmd ... executed:` and then the
    repr of a dict. Fine for the raw terminal panel, but this text may end up
    standing in for the model's answer, and there it should read as what the
    tool printed.
    """
    text = re.sub(r"^Observed output of cmd `[^`]+` executed:\s*", "", output.strip())
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    if isinstance(parsed, dict):
        for key in ("observation", "output", "result", "content"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def clear_screenshots() -> int:
    """Wipe every screenshot at start-up.

    Screenshots are the shortest-lived thing here: material for looking at
    while the work happens. Tying their removal to how the user stops the
    container would be fragile - a laptop lid closes without any stop script -
    so each start of the interface begins with an empty folder.
    """
    if not SHOTS_DIR.exists():
        return 0
    count = sum(1 for _ in SHOTS_DIR.rglob("*.png"))
    try:
        shutil.rmtree(SHOTS_DIR)
    except OSError as exc:
        logger.warning(f"Could not clear old screenshots: {exc}")
        return 0
    if count:
        logger.info(f"Removed {count} screenshot(s) from the previous session")
    return count


def save_screenshot(session_id: str, data: str) -> Optional[str]:
    """Put a screenshot on the container's own disk and return its address.

    The event stream keeps the picture itself only for the newest steps, and
    the stored history never keeps it at all - a path costs nothing and lets an
    old step still show what the agent saw.
    """
    folder = SHOTS_DIR / str(session_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # хвост из случайных символов: два снимка в одну миллисекунду бывают
        name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}.png"
        (folder / name).write_bytes(base64.b64decode(data))
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not save the screenshot: {exc}")
        return None
    _trim_shots(folder)
    return f"/api/screenshots/{session_id}/{name}"


def _trim_shots(folder: Path) -> None:
    """A long browsing run must not fill the container's disk."""
    try:
        shots = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in shots[:-MAX_SHOTS_PER_TASK]:
        try:
            path.unlink()
        except OSError:
            pass


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование числа со словом: 1 действие, 2 действия, 5 действий."""
    tens, ones = count % 100, count % 10
    if 11 <= tens <= 14:
        return many
    if ones == 1:
        return one
    if 2 <= ones <= 4:
        return few
    return many


class WebAgentMixin:
    """Reports every step of a ToolCallAgent to a web session."""

    session: Any = None

    async def run(self, request: Optional[str] = None) -> str:
        """Run as usual, but say out loud when the step limit cut the work off.

        Upstream signals this by appending a line to the result, which is easy
        to miss in a long transcript - and in plan mode the flow moves on to the
        next step as if nothing happened, leaving the current one half done.
        """
        result = await super().run(request)
        if "Reached max steps" in result:
            per_step = getattr(self.session, "mode", "agent") == "flow"
            self.session.publish(
                "warning",
                message=(
                    f"Достигнут предел в {self.max_steps} "
                    f"{_plural(self.max_steps, 'действие', 'действия', 'действий')} — "
                    + (
                        "текущий пункт плана прерван и мог остаться недоделанным."
                        if per_step
                        else "работа остановлена, задача могла остаться недоделанной."
                    )
                ),
                advice="Поднимите «Максимум шагов» в Настройках → Текущая задача "
                "или разбейте задачу на части.",
            )
        return result

    async def step(self) -> str:
        started = time.monotonic()
        before_in = self.llm.total_input_tokens
        before_out = self.llm.total_completion_tokens
        self.session.publish(
            "step", index=self.current_step, total=self.max_steps, agent=self.name
        )
        try:
            return await super().step()
        finally:
            # what this step cost, so the run is legible afterwards
            self.session.publish(
                "step_end",
                index=self.current_step,
                seconds=round(time.monotonic() - started, 1),
                tokens_in=self.llm.total_input_tokens - before_in,
                tokens_out=self.llm.total_completion_tokens - before_out,
            )

    async def think(self) -> bool:
        before = self.truncation_retries
        should_act = await super().think()

        # Ответ модели упёрся в предел длины и не содержал вызова инструмента.
        # Само по себе это уже обработано (шаг не тратится, модели объяснили,
        # что делать), но человеку об этом надо сказать: чаще всего это значит,
        # что «Токенов в ответе» выставлено слишком мало для его модели.
        if self.truncation_retries > before:
            self.session.publish(
                "warning",
                message=(
                    f"Модель упёрлась в предел длины ответа "
                    f"({self.llm.max_tokens} токенов) и не успела вызвать "
                    "инструмент. Шаг не потрачен, работа продолжается."
                ),
                advice="Если это повторяется — поднимите «Токенов в ответе» в "
                "Настройках → Модель. Ставьте столько, сколько допускает ваша "
                "модель: у облачных это обычно 16000–64000, у локальных бывает "
                "меньше. Слишком большое значение модель отвергнет с ошибкой.",
            )

        self.session.publish("llm", exchange=self._exchange())
        thought = self._last_assistant_content()
        if thought:
            self.session.publish("thought", text=thought, agent=self.name)
        if self.tool_calls:
            self.session.publish(
                "plan",
                tools=[
                    {"id": call.id, "name": call.function.name}
                    for call in self.tool_calls
                ],
            )
        return should_act

    async def execute_tool(self, command: ToolCall) -> str:
        name = command.function.name
        args = _parse_args(command)
        self.session.publish("tool_start", call_id=command.id, name=name, args=args)

        started = time.monotonic()
        result = await super().execute_tool(command)
        failed = _looks_failed(result)
        if not failed and name != "terminate" and result.strip():
            # запасной итог: если модель завершит работу молча, показать это
            self.session.last_output = {"name": name, "text": _readable(result)}

        shot = (
            save_screenshot(self.session.id, self._current_base64_image)
            if self._current_base64_image
            else None
        )
        self.session.publish(
            "tool_end",
            call_id=command.id,
            name=name,
            args=args,
            output=result[:MAX_OUTPUT_CHARS],
            truncated=len(result) > MAX_OUTPUT_CHARS,
            image=self._current_base64_image,
            shot=shot,
            failed=failed,
            seconds=round(time.monotonic() - started, 1),
            diagnosis=diagnostics.tool_failure(name, result) if failed else None,
        )
        return result

    def _exchange(self) -> List[Dict[str, Any]]:
        """The tail of what the model just saw and answered, for the Model tab."""
        exchange = []
        for message in self.memory.messages[-EXCHANGE_MESSAGES:]:
            entry = {
                "role": message.role,
                "content": (message.content or "")[:MAX_EXCHANGE_CHARS],
            }
            if message.tool_calls:
                entry["tools"] = [call.function.name for call in message.tool_calls]
            exchange.append(entry)
        return exchange

    def _last_assistant_content(self) -> Optional[str]:
        for message in reversed(self.memory.messages):
            if message.role == "assistant":
                return message.content
            if message.role == "user":
                break
        return None


class WebManus(WebAgentMixin, Manus):
    """Manus that reports every step to a web session."""


@lru_cache(maxsize=1)
def _data_analysis_class():
    """Imported late: charting pulls in heavy, optional dependencies."""
    from app.agent.data_analysis import DataAnalysis

    return type("WebDataAnalysis", (WebAgentMixin, DataAnalysis), {})


def create_data_analysis_agent(session: Any):
    """The charting agent the planning flow uses when it is switched on."""
    agent = _data_analysis_class()(session=session)
    point_at_workspace(agent, session.workspace)
    return agent


def point_at_workspace(agent: Any, workspace: Any) -> None:
    """Send the analyst's files to the task's folder, not the shared workspace.

    Its prompt and its tools are written around one directory, and by default
    that is workspace/ for every task at once. The task folder is what the
    interface shows in "Files", so charts and reports have to land there.
    """
    from app.prompt.visualization import SYSTEM_PROMPT as VISUALIZATION_PROMPT

    folder = str(workspace)
    agent.system_prompt = VISUALIZATION_PROMPT.format(directory=folder)
    for tool in agent.available_tools.tools:
        if "directory" in type(tool).model_fields:
            tool.directory = folder
