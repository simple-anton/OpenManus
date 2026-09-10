"""Session objects backing the OpenManus web interface.

A session owns one long-lived agent, its own folder inside the workspace, the
event log its runs produce and the fan-out queues feeding the browser. Sessions
are written to disk so the interface survives a restart of the container.
"""

import asyncio
import json
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from app.config import config
from app.llm import LLM
from app.logger import logger
from app.prompt.manus import SYSTEM_PROMPT, TASK_LIST_RULES
from app.schema import Message
from app.tool import PlanningTool
from app.tool.http_fetch import CRAWL_MAX_DEPTH, CRAWL_MAX_PAGES, Crawl
from app.tool.base import BaseTool, ToolResult
from app.web import agent as web_agent
from app.web import api_tools, diagnostics
from app.web import skills as skills_store
from app.web.agent import WebManus, create_data_analysis_agent
from app.web.flow import WebPlanningFlow
from app.web.login_gate import (
    DECISION_DONE,
    DECISION_SKIP,
    RequestLogin,
    browser_alive,
    open_in_browser,
    view_url,
)


# how many events are replayed to a browser that (re)connects
MAX_HISTORY = 2000
# screenshots are heavy: keep the payload of the most recent ones only
MAX_IMAGES_KEPT = 15
# how long the agent waits for a human answer before giving up
ANSWER_TIMEOUT = 900
# вход руками занимает больше: найти пароль, получить код из СМС, пройти
# двухфакторную проверку. Полчаса — разумный запас.
LOGIN_TIMEOUT = 1800
# перед обходом сайта агент спрашивает человека; столько ждём ответа,
# потом берётся вариант по умолчанию — обычная загрузка (Уровень 1)
CRAWL_CONFIRM_TIMEOUT = 120
# a planning flow may run for a while, but not forever
FLOW_TIMEOUT = 3600
# Бюджет действий агента в режиме «Агент» — на всю задачу целиком. В «Плане»
# бюджет (self.max_steps) тратится на КАЖДЫЙ пункт по отдельности, поэтому там
# он остаётся меньше; здесь же одним числом покрывается весь путь: найти
# источники, прочитать, посчитать, свести отчёт. Двадцати не хватало на
# исследование с несколькими источниками — задача упиралась в предел раньше,
# чем агент успевал перейти к браузеру или собрать вывод.
AGENT_STEPS = 40

# conversation turns carried over when a stored session is reopened
MEMORY_KEPT = 40

CHAT_PROMPT = (
    "Ты отвечаешь в режиме обычного разговора. У тебя нет инструментов: ты не "
    "запускаешь код, не открываешь сайты, не ищешь в интернете, не читаешь и не "
    "создаёшь файлы. Отвечай текстом, на языке собеседника.\n"
    "Если вопрос требует свежих данных из сети, работы с файлами или запуска "
    "кода — скажи об этом прямо и предложи переключиться на режим «Агент» "
    "кнопкой сверху: там всё это доступно. Не пиши код, изображающий действие, "
    "которого ты не совершал."
)

ANSWER_PROMPT = (
    "\n\nПрежде чем вызвать terminate, напиши пользователю итоговый ответ "
    "обычным текстом: сам результат, а не пересказ своих действий. Если "
    "результат — данные, приведи их прямо в ответе, таблицей, если она уместна. "
    "Вывод инструментов — это черновик, ответом он не считается."
)

WORKSPACE = Path(config.workspace_root)
STORE = WORKSPACE / ".sessions"


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


# Обход сайта агент запускает не сразу: сперва спрашивает человека, зачем ему
# полный обход вместо обычной загрузки. Через веб этот вопрос показывается
# окном; без веба (в CLI) подтверждения нет и обход идёт как обычно.
class WebCrawl(Crawl):
    """`crawl`, который перед запуском спрашивает разрешения у человека."""

    session: Any = None

    async def execute(
        self,
        start_url: str,
        reason: str = "",
        max_depth=None,
        max_pages=None,
        **kwargs: Any,
    ) -> ToolResult:
        cfg = config.agent_config
        ceil_depth = min(cfg.crawl_depth, CRAWL_MAX_DEPTH)
        ceil_pages = min(cfg.crawl_pages, CRAWL_MAX_PAGES)
        depth = max(1, min(int(max_depth), ceil_depth) if max_depth else ceil_depth)
        pages = max(1, min(int(max_pages), ceil_pages) if max_pages else ceil_pages)
        allowed = await self.session.confirm_crawl(start_url, reason, depth, pages)
        if not allowed:
            return ToolResult(
                output=(
                    "Обход отклонён: человек выбрал обычную загрузку или не "
                    "ответил вовремя. Не вызывайте crawl для этого раздела "
                    "снова. Прочитайте нужные страницы обычным fetch — он "
                    "показывает ссылки каждой страницы, идите по ним сами, "
                    "по одной. Так задумано: обход применяют только там, где "
                    "человек его разрешил."
                )
            )
        return await super().execute(
            start_url=start_url, max_depth=max_depth, max_pages=max_pages
        )


class Session:
    """One conversation with a Manus agent."""

    def __init__(
        self,
        session_id: str,
        title: str = "Новая задача",
        created_at: Optional[float] = None,
        max_steps: int = 20,
        skills: Optional[List[str]] = None,
    ):
        self.id = session_id
        self.title = title
        self.created_at = created_at or time.time()
        self.max_steps = max_steps
        self.skills: List[str] = list(skills or [])
        self.agent: Optional[WebManus] = None
        self.task: Optional[asyncio.Task] = None
        self.state = "idle"
        self.mode = "agent"  # what the current run was started as
        self.last_output: Optional[Dict[str, str]] = None
        self.pending_question: Optional[str] = None

        self.history: List[Dict[str, Any]] = []
        self._image_events: deque = deque()
        self._subscribers: Set[asyncio.Queue] = set()
        self._answers: asyncio.Queue = asyncio.Queue()
        # решение человека по просьбе войти на сайт: done или skip
        self._logins: asyncio.Queue = asyncio.Queue()
        self.pending_login: Optional[Dict[str, str]] = None
        # решение человека по просьбе обойти сайт: allow или default
        self._crawl_confirms: asyncio.Queue = asyncio.Queue()
        self.pending_crawl: Optional[Dict[str, Any]] = None
        # Вкладка браузера, принадлежащая этой задаче. Браузер в контейнере
        # один на всех, и без своей вкладки задачи читают чужие страницы.
        self.browser_tab: Optional[str] = None
        self._queued: deque = deque()
        self._event_id = 0
        self._agent_lock = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._thread_id = threading.get_ident()
        self._carried_memory: List[Dict[str, str]] = []
        # Список задач режима «Агент». Живёт у сессии, а не у агента: агента
        # пересоздают при смене настроек, а список должен пережить это — он
        # виден человеку в ленте.
        self.planning_tool = PlanningTool()

        # each task gets its own folder so files from different runs do not mix
        self.workspace = WORKSPACE / f"task_{session_id}"
        self.store = STORE / session_id
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.store.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось создать папку задачи в {WORKSPACE}: {exc}. "
                "Проверьте монтирование ./workspace в docker-compose.yml."
            ) from exc
        self._save_meta()

    # ------------------------------------------------------------- persistence

    def _save_meta(self) -> None:
        try:
            (self.store / "meta.json").write_text(
                json.dumps(
                    {
                        "id": self.id,
                        "title": self.title,
                        "created_at": self.created_at,
                        "max_steps": self.max_steps,
                        "skills": self.skills,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(f"Could not save session {self.id}: {exc}")

    def _append_event(self, event: Dict[str, Any]) -> None:
        """Store the event without its screenshot, which would bloat the file."""
        record = {k: v for k, v in event.items() if k != "image"}
        if event.get("image"):
            record["image_dropped"] = True
        try:
            with (self.store / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _save_memory(self) -> None:
        if self.agent is None:
            return
        kept = [
            {"role": message.role, "content": message.content}
            for message in self.agent.memory.messages
            if message.role in {"user", "assistant"} and message.content
        ]
        try:
            (self.store / "memory.json").write_text(
                json.dumps(kept[-MEMORY_KEPT:], ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    @classmethod
    def restore_all(cls) -> Dict[str, "Session"]:
        """Rebuild sessions saved by an earlier run of the server."""
        sessions: Dict[str, Session] = {}
        if not STORE.exists():
            return sessions
        for folder in sorted(STORE.iterdir(), key=lambda p: p.name):
            meta_file = folder / "meta.json"
            if not meta_file.is_file():
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                session = cls(
                    meta["id"],
                    title=meta.get("title", "Задача"),
                    created_at=meta.get("created_at"),
                    max_steps=meta.get("max_steps", 20),
                    skills=meta.get("skills", []),
                )
                session._load_events(folder / "events.jsonl")
                memory_file = folder / "memory.json"
                if memory_file.is_file():
                    session._carried_memory = json.loads(
                        memory_file.read_text(encoding="utf-8")
                    )
                sessions[session.id] = session
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning(f"Skipping unreadable session in {folder}: {exc}")
        if sessions:
            logger.info(f"Restored {len(sessions)} saved session(s)")
        return sessions

    def _load_events(self, path: Path) -> None:
        if not path.is_file():
            return
        events = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        self.history = events[-MAX_HISTORY:]
        self._event_id = self.history[-1]["id"] if self.history else 0

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
        self._append_event(event)
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
                # Свой экземпляр модели на задачу: общий одиночка складывает
                # счётчики токенов всех задач разом и делит между ними
                # причину остановки последней генерации.
                agent.llm = LLM.isolated()
                # Модель чтения: ею пересказываются длинные ответы инструментов.
                # Своя настройка [llm.reader], если её нет — та же основная.
                agent.reader = LLM.isolated("reader")
                agent.vision = self._vision_llm()
                self._attach_web_tools(agent)
                self._carry_memory(agent)
                self.agent = agent
                self._report_mcp(agent)
            self.agent.max_steps = self._step_budget()
            # Настройки могли поменяться после создания агента — а он живёт
            # всю задачу. Подхватываем предел видимости на каждом запуске.
            self.agent.max_observe = config.agent_config.max_observe
            self.apply_prompt()
            return self.agent

    def _vision_llm(self) -> Optional[LLM]:
        """Модель зрения, если она указана в настройках.

        Слот [llm.vision] в проекте был, вкладка в настройках была, а кода,
        который бы его создавал, не существовало: настройка сохранялась и не
        работала. Модель, поставленную сюда, считаем умеющей смотреть на
        картинки — иначе ставить её в этот слот незачем.
        """
        section = config.llm.get("vision")
        if section is None or not (section.model or "").strip():
            return None
        vision = LLM.isolated("vision")
        vision.supports_images = True
        return vision

    def _report_mcp(self, agent: WebManus) -> None:
        """Say which configured plugins did not come up, instead of hiding it in logs."""
        configured = set(config.mcp_config.servers)
        missing = sorted(configured - set(agent.connected_servers))
        if missing:
            self.publish(
                "warning",
                message="Не подключились плагины: " + ", ".join(missing),
                advice="Настройки → Плагины: проверьте команду и переменные сервера. "
                "Подробности — во вкладке «Логи».",
            )

    def _step_budget(self) -> int:
        """Сколько действий даём агенту. «Агент» — весь путь одним прогоном,
        ему нужен больший запас (AGENT_STEPS); «План» ведёт бюджет на каждый
        пункт по отдельности, там остаётся настройка задачи (self.max_steps)."""
        return AGENT_STEPS if self.mode == "agent" else self.max_steps

    def apply_prompt(self) -> None:
        """Point the agent at this task's folder and its attached skills."""
        if self.agent is None:
            return
        # Список задач — только в режиме «Агент», см. _use_planning.
        solo = self.mode == "agent"
        self._use_planning(solo)
        # Итоговый ответ по журналу нужен там, где агент отвечает человеку сам.
        # Исполнителю пункта плана журнал и так приходит в постановке задачи.
        self.agent.answer_from_journal = solo
        # Напоминания отмеряются на прогон, а не на всю жизнь агента: агент
        # живёт всю задачу, а исчерпанный счётчик молчал бы и в следующих
        # запусках. apply_prompt вызывается перед каждым, здесь и сбрасываем.
        fields = getattr(type(self.agent), "model_fields", {})
        for field in ("blocked_nudges_left", "journal_nudges_left",
                      "answer_nudges_left", "eviction_told"):
            if field in fields:
                setattr(self.agent, field, fields[field].default)
        # Инструменты, умеющие писать файлы, должны писать в папку задачи, а
        # не в общий workspace: иначе скачанное не видно во вкладке «Файлы» и
        # не удаляется вместе с задачей. Журнал находок — туда же.
        for tool in self.agent.available_tools.tools:
            if "directory" in type(tool).model_fields:
                tool.directory = str(self.workspace)
        self.agent.system_prompt = (
            SYSTEM_PROMPT.format(directory=self.workspace)
            + (TASK_LIST_RULES if solo else "")
            + ANSWER_PROMPT
            + skills_store.prompt_for(self.skills)
        )

    def _use_planning(self, enabled: bool) -> None:
        """Даёт агенту инструмент списка задач — или забирает его.

        В режиме «Агент» список ведёт сам агент: плана ему никто не пишет, и
        без списка ни он, ни человек не видят, где находится работа.

        В режиме «План» список ведёт поток-планировщик, и тот же самый агент
        работает у него исполнителем одного пункта. Оставь инструмент ему там —
        он завёл бы свой план поверх настоящего, перебил бы им карточку в ленте
        и потратил бы на это действия из запаса пункта. Поэтому забираем.
        """
        if self.agent is None:
            return
        tools = [
            tool
            for tool in self.agent.available_tools.tools
            if tool.name != self.planning_tool.name
        ]
        if enabled:
            tools.append(self.planning_tool)
        self.agent.available_tools.tools = tuple(tools)
        self.agent.available_tools.tool_map = {tool.name: tool for tool in tools}

    def set_skills(self, slugs: List[str]) -> None:
        self.skills = [slug for slug in slugs if skills_store.read_skill(slug)]
        self._save_meta()
        self.apply_prompt()

    def _attach_web_tools(self, agent: WebManus) -> None:
        """Browser-based ask_human, plus the endpoints defined in the UI."""
        web_ask = WebAskHuman(session=self)
        login = RequestLogin(session=self)
        web_crawl = WebCrawl(session=self)
        replacements = {
            web_ask.name: web_ask,
            login.name: login,
            web_crawl.name: web_crawl,
        }
        tools = tuple(
            replacements.pop(tool.name, tool) for tool in agent.available_tools.tools
        )
        tools = tools + tuple(replacements.values())
        custom = tuple(
            tool
            for tool in api_tools.build_tools()
            if tool.name not in {existing.name for existing in tools}
        )
        if custom:
            logger.info(f"Attached custom API tools: {[t.name for t in custom]}")
        tools = tools + custom
        agent.available_tools.tools = tools
        agent.available_tools.tool_map = {tool.name: tool for tool in tools}

    def _carry_memory(self, agent: WebManus) -> None:
        """Give a reopened session the gist of what was said before."""
        for message in self._carried_memory:
            if message["role"] == "user":
                agent.memory.add_message(Message.user_message(message["content"]))
            else:
                agent.memory.add_message(Message.assistant_message(message["content"]))
        self._carried_memory = []

    def tools(self) -> List[Dict[str, str]]:
        if self.agent is None:
            return []
        return [
            {
                "name": tool.name,
                "description": (tool.description or "").strip()[:400],
            }
            for tool in self.agent.available_tools.tools
        ]

    async def reset_agent(self) -> None:
        """Drop the agent so the next run picks up new settings."""
        if self.agent is not None:
            self._save_memory()
            memory_file = self.store / "memory.json"
            if memory_file.is_file():
                self._carried_memory = json.loads(
                    memory_file.read_text(encoding="utf-8")
                )
            try:
                await self.agent.cleanup()
            except Exception as exc:
                logger.warning(f"Failed to clean up agent for {self.id}: {exc}")
            self.agent = None

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

    async def request_login(self, url: str, reason: str) -> str:
        """Агент просит человека войти на сайт. Работа ждёт решения."""
        while not self._logins.empty():  # снимаем решения от прошлой просьбы
            self._logins.get_nowait()

        if not await browser_alive():
            self.publish(
                "warning",
                message="Агент попросил войти на сайт, но браузер в контейнере не работает.",
                advice="Пересоберите образ и проверьте «Логи» — там строка, "
                "начинающаяся с «browser:».",
            )
            return (
                "The container browser is not running, so a sign-in is impossible. "
                "Record this source as unavailable and continue without it."
            )

        opened = await open_in_browser(url)
        self.pending_login = {"url": url, "reason": reason}
        self.publish(
            "login_request",
            url=url,
            reason=reason,
            view=view_url(),
            opened=opened,
        )
        try:
            decision = await asyncio.wait_for(
                self._logins.get(), timeout=LOGIN_TIMEOUT
            )
        except asyncio.TimeoutError:
            self.publish(
                "log",
                level="WARNING",
                message="Никто не ответил на просьбу войти — источник пропущен.",
            )
            return (
                "Nobody answered the sign-in request in time. Record this source "
                "as unavailable, say so plainly in your findings, and continue."
            )
        finally:
            self.pending_login = None

        if decision == DECISION_DONE:
            return (
                f"The person signed in. The browser now holds their session for "
                f"{url}. Open the page again with browser_exec and read the data. "
                "Take only what this task needs, and never change anything in "
                "their account."
            )
        return (
            "The person declined to sign in to this source. Do not ask again for "
            "this site. Record in your findings that the data behind it is "
            "missing and what exactly is unknown because of it, then continue."
        )

    def login_decision(self, decision: str) -> bool:
        """Ответ человека на просьбу войти: он вошёл или пропускает источник."""
        if self.pending_login is None or decision not in (DECISION_DONE, DECISION_SKIP):
            return False
        self.publish("login_decision", decision=decision)
        self._logins.put_nowait(decision)
        return True

    async def confirm_crawl(
        self, url: str, reason: str, depth: int, pages: int
    ) -> bool:
        """Агент просит обойти сайт. Ждём ответа человека CRAWL_CONFIRM_TIMEOUT
        секунд; молчание — согласие на вариант по умолчанию (обычная загрузка)."""
        while not self._crawl_confirms.empty():  # снимаем ответы прошлой просьбы
            self._crawl_confirms.get_nowait()
        self.pending_crawl = {
            "url": url,
            "reason": reason,
            "depth": depth,
            "pages": pages,
        }
        self.publish(
            "crawl_request",
            url=url,
            reason=reason,
            depth=depth,
            pages=pages,
            timeout=CRAWL_CONFIRM_TIMEOUT,
        )
        try:
            decision = await asyncio.wait_for(
                self._crawl_confirms.get(), timeout=CRAWL_CONFIRM_TIMEOUT
            )
        except asyncio.TimeoutError:
            decision = "default"
            self.publish(
                "log",
                level="INFO",
                message="Никто не ответил про обход — берём обычную загрузку.",
            )
        finally:
            self.pending_crawl = None
        self.publish("crawl_decision", decision=decision)
        return decision == "allow"

    def crawl_decision(self, decision: str) -> bool:
        """Ответ человека на просьбу обойти сайт: разрешить или обычная загрузка."""
        if self.pending_crawl is None or decision not in ("allow", "default"):
            return False
        self._crawl_confirms.put_nowait(decision)
        return True

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
            self._save_meta()
        self.task = asyncio.create_task(self._run(prompt, mode))

    async def _run(self, prompt: str, mode: str) -> None:
        self.mode = mode
        self.state = "running"
        self.publish("user", message=prompt, mode=mode)
        self.publish("status", state="running")
        try:
            with logger.contextualize(web_session=self.id):
                problem = await diagnostics.preflight()
                if problem is not None:
                    self.publish("error", **problem)
                    self.state = "idle"
                    self.publish("status", state="idle")
                    return
                if mode == "chat":
                    await self._chat(prompt)
                elif mode == "flow":
                    await self._flow(prompt)
                else:
                    agent = await self.ensure_agent()
                    agent.current_step = 0
                    result = await agent.run(prompt)
                    answer, source = self._closing_answer(agent)
                    self.publish(
                        "result", message=result, answer=answer, answer_source=source
                    )
            self.state = "idle"
            self.publish("status", state="idle")
        except asyncio.CancelledError:
            self.state = "idle"
            self.publish("status", state="stopped")
            raise
        except Exception as exc:  # surfaced in the UI instead of only in stderr
            logger.exception(f"Web session {self.id} failed: {exc}")
            try:
                self.publish("error", **diagnostics.explain(exc))
            except Exception as reporting_error:  # the report must never be silent
                logger.exception(f"Failed to report the failure: {reporting_error}")
                self.publish(
                    "error",
                    source="OpenManus",
                    title=f"{type(exc).__name__}: {exc}",
                    why="Разбор ошибки сам дал сбой, подробности — во вкладке «Логи».",
                )
            self.state = "idle"
            self.publish("status", state="idle")
        finally:
            self.pending_question = None
            self.task = None
            self._save_memory()
            self._start_queued()

    async def _chat(self, prompt: str) -> None:
        """Answer directly, without the tool loop - Manus's chat mode.

        The system prompt has to be its own: handed Manus's, the model believes
        it has a browser and a python runtime, and answers a question about the
        weather by writing code that nobody runs. Here it is told plainly that
        it has no tools, and what to say when the question needs them.
        """
        agent = await self.ensure_agent()
        agent.update_memory("user", prompt)
        answer = await agent.llm.ask(
            messages=agent.messages,
            system_msgs=[
                Message.system_message(
                    CHAT_PROMPT + skills_store.prompt_for(self.skills)
                )
            ],
            stream=False,
        )
        agent.memory.add_message(Message.assistant_message(answer))
        self.publish("chat", text=answer)

    async def _flow(self, prompt: str) -> None:
        """Plan the task first, then work through the plan - like run_flow.py."""
        manus = await self.ensure_agent()
        manus.current_step = 0
        agents = {"manus": manus}
        if config.run_flow_config.use_data_analysis_agent:
            try:
                analyst = create_data_analysis_agent(self)
            except Exception as exc:  # charting dependencies are optional
                self.publish(
                    "log",
                    level="WARNING",
                    message=f"Агент анализа данных недоступен: {exc}",
                )
            else:
                analyst.llm = LLM.isolated()
                analyst.max_steps = self.max_steps
                # the analyst has its own prompt; the task's skills apply to it too
                analyst.system_prompt += skills_store.prompt_for(self.skills)
                agents["data_analysis"] = analyst
                self.publish(
                    "log", level="INFO", message="Подключён агент анализа данных"
                )

        # the planner writes the steps, so it has to know the skills as well;
        # max_steps is the allowance for one plan step, not for the whole plan
        flow = WebPlanningFlow(
            agents,
            session=self,
            # планировщик тоже считает токены — и тоже своим счётчиком
            llm=LLM.isolated(),
            planning_context=skills_store.planning_prompt_for(self.skills),
            step_budget=self.max_steps,
            # журнал находок ложится в папку задачи: это общая память шагов
            workspace=str(self.workspace),
        )
        result = await asyncio.wait_for(flow.execute(prompt), timeout=FLOW_TIMEOUT)
        self.publish("result", message=result)

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
            self._save_memory()
            try:
                await self.agent.cleanup()
            except Exception as exc:
                logger.warning(f"Failed to clean up agent for {self.id}: {exc}")
            self.agent = None
        try:  # do not leave empty task folders behind
            self.workspace.rmdir()
        except OSError:
            pass

    def _closing_answer(self, agent: Any) -> tuple:
        """What to show as the outcome of a run.

        Some models do the work and call terminate without a word, leaving the
        answer inside a tool's output - the user then sees a finished task and
        no reply. Prefer the model's own closing words; fall back to the last
        thing a tool printed, and say which one it is.
        """
        for message in reversed(getattr(agent.memory, "messages", [])):
            if message.role == "user":
                break
            if message.role == "assistant" and (message.content or "").strip():
                return message.content.strip(), "model"
        spare = getattr(self, "last_output", None)
        if spare and spare.get("text", "").strip():
            return spare["text"].strip(), "tool"
        return "", ""

    def forget(self, remove_files: bool = False) -> None:
        """Remove what was stored on disk for this session.

        The task's own folder is kept by default: deleting a conversation
        should not silently take the work with it. `remove_files` is the
        deliberate choice made in the interface.
        """
        for name in ("meta.json", "events.jsonl", "memory.json"):
            (self.store / name).unlink(missing_ok=True)
        try:
            self.store.rmdir()
        except OSError:
            pass
        shots = web_agent.SHOTS_DIR / self.id
        if shots.is_dir():
            shutil.rmtree(shots, ignore_errors=True)
        if remove_files:
            self._remove_workspace()

    def _remove_workspace(self) -> None:
        """Delete this task's folder - and nothing that is not one."""
        folder = Path(self.workspace).resolve()
        expected = (WORKSPACE / f"task_{self.id}").resolve()
        if folder != expected or not folder.is_dir():
            # a session restored with an odd id, or a folder already gone
            logger.warning(f"Refused to delete {folder}: not this task's folder")
            return
        try:
            shutil.rmtree(folder)
            logger.info(f"Removed the task folder {folder}")
        except OSError as exc:
            logger.warning(f"Could not remove {folder}: {exc}")

    def workspace_usage(self) -> Dict[str, int]:
        """How much the task folder holds, so the interface can say it out loud."""
        files = 0
        size = 0
        folder = Path(self.workspace)
        if folder.is_dir():
            for path in folder.rglob("*"):
                if path.is_file():
                    files += 1
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
        return {"files": files, "bytes": size}

    def info(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": "running" if self.busy else self.state,
            "created_at": self.created_at,
            "pending_question": self.pending_question,
            "queued": len(self._queued),
            "workspace": str(self.workspace),
            "max_steps": self.max_steps,
            "skills": self.skills,
            "usage": self.workspace_usage(),
            "tools": self.tools(),
        }
