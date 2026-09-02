"""Session objects backing the OpenManus web interface.

A session owns one long-lived agent, the event log its runs produce and the
fan-out queues feeding the browser's event stream.
"""

import asyncio
import threading
import time
from collections import deque
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from app.logger import logger
from app.schema import Message
from app.tool.base import BaseTool
from app.web.agent import WebManus


# how many events are replayed to a browser that (re)connects
MAX_HISTORY = 2000
# screenshots are heavy: keep the payload of the most recent ones only
MAX_IMAGES_KEPT = 15
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

    def __init__(self, session_id: str, title: str = "Новая задача"):
        self.id = session_id
        self.title = title
        self.created_at = time.time()
        self.agent: Optional[WebManus] = None
        self.task: Optional[asyncio.Task] = None
        self.state = "idle"
        self.pending_question: Optional[str] = None

        self.history: List[Dict[str, Any]] = []
        self._image_events: deque = deque()
        self._subscribers: Set[asyncio.Queue] = set()
        self._answers: asyncio.Queue = asyncio.Queue()
        self._queued: deque = deque()
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
        if event.get("image"):
            self._image_events.append(event)
            while len(self._image_events) > MAX_IMAGES_KEPT:
                stale = self._image_events.popleft()
                stale["image"] = None
                stale["image_dropped"] = True
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
        queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
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

    async def ensure_agent(self) -> WebManus:
        async with self._agent_lock:
            if self.agent is None:
                agent = await WebManus.create(session=self)
                self._attach_web_tools(agent)
                self.agent = agent
            return self.agent

    def _attach_web_tools(self, agent: WebManus) -> None:
        """Swap the stdin-based ask_human tool for the browser-based one."""
        web_ask = WebAskHuman(session=self)
        tools = tuple(
            web_ask if tool.name == web_ask.name else tool
            for tool in agent.available_tools.tools
        )
        agent.available_tools.tools = tools
        agent.available_tools.tool_map = {tool.name: tool for tool in tools}

    def tools(self) -> List[Dict[str, str]]:
        if self.agent is None:
            return []
        return [
            {"name": tool.name, "description": (tool.description or "").strip()}
            for tool in self.agent.available_tools.tools
        ]

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
            self.publish("log", level="WARNING", message="Пользователь не ответил.")
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

    def submit(self, prompt: str, mode: str = "agent") -> str:
        """Answer a pending question, queue a follow-up, or start a run."""
        if self.pending_question is not None:
            self.answer(prompt)
            return "answered"
        if self.busy:
            self._queued.append((prompt, mode))
            self.publish("queued", message=prompt)
            return "queued"
        self.start(prompt, mode)
        return "started"

    def start(self, prompt: str, mode: str = "agent") -> None:
        if self.title == "Новая задача":
            self.title = prompt[:60]
        self.task = asyncio.create_task(self._run(prompt, mode))

    async def _run(self, prompt: str, mode: str) -> None:
        self.state = "running"
        self.publish("user", message=prompt, mode=mode)
        self.publish("status", state="running")
        try:
            with logger.contextualize(web_session=self.id):
                if mode == "chat":
                    await self._chat(prompt)
                else:
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
            self.publish("error", message=f"{type(exc).__name__}: {exc}")
            self.state = "idle"
            self.publish("status", state="idle")
        finally:
            self.pending_question = None
            self.task = None
            self._start_queued()

    async def _chat(self, prompt: str) -> None:
        """Answer directly, without the tool loop - Manus's chat mode."""
        agent = await self.ensure_agent()
        agent.update_memory("user", prompt)
        answer = await agent.llm.ask(
            messages=agent.messages,
            system_msgs=[Message.system_message(agent.system_prompt)],
            stream=False,
        )
        agent.memory.add_message(Message.assistant_message(answer))
        self.publish("chat", text=answer)

    def _start_queued(self) -> None:
        if not self._queued:
            return
        prompt, mode = self._queued.popleft()
        self.start(prompt, mode)

    async def stop(self) -> bool:
        self._queued.clear()
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
            "queued": len(self._queued),
            "tools": self.tools(),
        }
