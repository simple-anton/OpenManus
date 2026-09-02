"""Session objects backing the OpenManus web interface.

A session owns one long-lived :class:`~app.agent.manus.Manus` instance, the
event log produced by its runs and the fan-out queues used by the SSE stream.
"""

import asyncio
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from app.agent.manus import Manus
from app.logger import logger
from app.tool.base import BaseTool


# how many events are replayed to a browser that (re)connects
MAX_HISTORY = 1000
# how long the agent waits for a human answer before giving up
ANSWER_TIMEOUT = 900


class WebAskHuman(BaseTool):
    """`ask_human` variant that asks through the browser instead of stdin."""

    name: str = "ask_human"
    description: str = "Use this tool to ask human for help."
    parameters: dict = {
        "type": "object",
        "properties": {
            "inquire": {
                "type": "string",
                "description": "The question you want to ask human.",
            }
        },
        "required": ["inquire"],
    }
    session: Any = None

    async def execute(self, inquire: str) -> str:
        return await self.session.ask_human(inquire)


class Session:
    """One conversation with a Manus agent."""

    def __init__(self, session_id: str, title: str = "New session"):
        self.id = session_id
        self.title = title
        self.created_at = time.time()
        self.agent: Optional[Manus] = None
        self.task: Optional[asyncio.Task] = None
        self.state = "idle"
        self.pending_question: Optional[str] = None

        self.history: List[Dict[str, Any]] = []
        self._subscribers: Set[asyncio.Queue] = set()
        self._answers: asyncio.Queue = asyncio.Queue()
        self._event_id = 0
        self._agent_lock = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._thread_id = threading.get_ident()

    # ------------------------------------------------------------------ events

    def publish(self, event_type: str, **data: Any) -> None:
        """Record an event and push it to every connected browser.

        Safe to call from worker threads: tools may log from outside the loop.
        """
        event = {"type": event_type, "ts": time.time(), **data}
        if threading.get_ident() == self._thread_id:
            self._deliver(event)
        else:
            self._loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: Dict[str, Any]) -> None:
        self._event_id += 1
        event["id"] = self._event_id
        self.history.append(event)
        if len(self.history) > MAX_HISTORY:
            del self.history[: len(self.history) - MAX_HISTORY]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # a slow browser must never stall the agent
                self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[Optional[Dict[str, Any]]]:
        """Yield past then live events; ``None`` marks a keep-alive tick."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(queue)
        try:
            backlog = list(self.history)
            last_id = backlog[-1]["id"] if backlog else 0
            for event in backlog:
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield None
                    continue
                if event["id"] > last_id:
                    last_id = event["id"]
                    yield event
        finally:
            self._subscribers.discard(queue)

    # ------------------------------------------------------------------- agent

    async def ensure_agent(self) -> Manus:
        async with self._agent_lock:
            if self.agent is None:
                agent = await Manus.create()
                self._attach_web_tools(agent)
                self.agent = agent
            return self.agent

    def _attach_web_tools(self, agent: Manus) -> None:
        """Swap the stdin-based ask_human tool for the browser-based one."""
        web_ask = WebAskHuman(session=self)
        tools = tuple(
            web_ask if tool.name == web_ask.name else tool
            for tool in agent.available_tools.tools
        )
        agent.available_tools.tools = tools
        agent.available_tools.tool_map = {tool.name: tool for tool in tools}

    async def ask_human(self, inquire: str) -> str:
        """Called by :class:`WebAskHuman` from inside a running agent."""
        while not self._answers.empty():  # drop stale answers
            self._answers.get_nowait()
        self.pending_question = inquire
        self.publish("question", message=inquire)
        try:
            answer = await asyncio.wait_for(self._answers.get(), timeout=ANSWER_TIMEOUT)
        except asyncio.TimeoutError:
            self.pending_question = None
            self.publish("log", level="WARNING", message="No answer from the user.")
            return "The user did not answer in time. Continue on your own."
        finally:
            self.pending_question = None
        return answer

    def answer(self, text: str) -> bool:
        if self.pending_question is None:
            return False
        self.publish("answer", message=text)
        self._answers.put_nowait(text)
        return True

    # --------------------------------------------------------------------- run

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, prompt: str) -> None:
        if self.title == "New session":
            self.title = prompt[:60]
        self.task = asyncio.create_task(self._run(prompt))

    async def _run(self, prompt: str) -> None:
        self.state = "running"
        self.publish("user", message=prompt)
        self.publish("status", state="running")
        try:
            with logger.contextualize(web_session=self.id):
                agent = await self.ensure_agent()
                agent.current_step = 0
                result = await agent.run(prompt)
            self.publish("result", message=result)
            self.state = "idle"
            self.publish("status", state="idle")
        except asyncio.CancelledError:
            self.state = "idle"
            self.publish("status", state="stopped")
            raise
        except Exception as exc:  # surfaced in the UI instead of only in stderr
            logger.exception(f"Web session {self.id} failed: {exc}")
            self.state = "error"
            self.publish("error", message=f"{type(exc).__name__}: {exc}")
            self.publish("status", state="idle")
            self.state = "idle"
        finally:
            self.pending_question = None
            self.task = None

    async def stop(self) -> bool:
        if not self.busy:
            return False
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    async def cleanup(self) -> None:
        await self.stop()
        if self.agent is not None:
            try:
                await self.agent.cleanup()
            except Exception as exc:
                logger.warning(f"Failed to clean up agent for {self.id}: {exc}")
            self.agent = None

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": "running" if self.busy else self.state,
            "created_at": self.created_at,
            "pending_question": self.pending_question,
        }
