"""Manus agent instrumented for the web interface.

The console agent reports what it does through log lines. The browser needs
structured events instead - which tool is running, with which arguments, what
it returned and whether it produced a screenshot - so the UI can mirror the
agent's work the way the hosted Manus does.
"""

import json
from typing import Any, Dict, List, Optional

from app.agent.manus import Manus
from app.schema import ToolCall


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


class WebManus(Manus):
    """Manus that reports every step to a web session."""

    session: Any = None

    async def step(self) -> str:
        self.session.publish("step", index=self.current_step, total=self.max_steps)
        return await super().step()

    async def think(self) -> bool:
        should_act = await super().think()

        self.session.publish("llm", exchange=self._exchange())
        thought = self._last_assistant_content()
        if thought:
            self.session.publish("thought", text=thought)
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

        result = await super().execute_tool(command)

        image = self._current_base64_image
        self.session.publish(
            "tool_end",
            call_id=command.id,
            name=name,
            args=args,
            output=result[:MAX_OUTPUT_CHARS],
            truncated=len(result) > MAX_OUTPUT_CHARS,
            image=image,
            failed=_looks_failed(result),
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
