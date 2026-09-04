"""Agents instrumented for the web interface.

The console agents report what they do through log lines. The browser needs
structured events instead - which tool is running, with which arguments, what
it returned and whether it produced a screenshot - so the UI can mirror the
agent's work the way the hosted Manus does.
"""

import json
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from app.agent.manus import Manus
from app.schema import ToolCall
from app.web import diagnostics


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
        should_act = await super().think()

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

        self.session.publish(
            "tool_end",
            call_id=command.id,
            name=name,
            args=args,
            output=result[:MAX_OUTPUT_CHARS],
            truncated=len(result) > MAX_OUTPUT_CHARS,
            image=self._current_base64_image,
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
