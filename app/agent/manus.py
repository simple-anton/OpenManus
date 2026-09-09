import os
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import Field

from app.agent.toolcall import ToolCallAgent
from app.config import config
from app.flow.ledger import Ledger
from app.logger import logger
from app.prompt.manus import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import Message
from app.tool import Terminate, ToolCollection
from app.tool.ask_human import AskHuman
from app.tool.http_fetch import Fetch
from app.tool.journal import RecordFinding
from app.tool.mcp import MCPClients, MCPClientTool
from app.tool.python_execute import PythonExecute
from app.tool.str_replace_editor import StrReplaceEditor
from app.tool.web_search import WebSearch


_BROWSER_USE_SERVER_ID = "browser_use"
_BROWSER_USE_COMMAND = "uvx"
_BROWSER_USE_ARGS = ["browser-use", "--cli-mcp"]
_BROWSER_USE_ENV_VARS = (
    "BROWSER_USE_API_KEY",
    "BROWSER_USE_CLOUD_API_URL",
    "BU_BROWSER_ID",
    "BU_CDP_URL",
    "BU_CDP_WS",
    "BU_NAME",
)
_BROWSER_USE_TRANSPORT_INSTRUCTIONS = """\
Browser Use CLI 3.0 is exposed here as MCP tools. When the Browser Use skill
shows `browser-use <<'PY'`, pass the Python body to `browser_exec` instead.
Use `browser_screenshot` when visual inspection is needed. Both tools use the
same persistent browser-harness session as CLI 3.0.
"""


def _browser_use_env() -> Dict[str, str]:
    return {name: value for name in _BROWSER_USE_ENV_VARS if (value := os.getenv(name))}


class Manus(ToolCallAgent):
    """A versatile general-purpose agent with support for both local and MCP tools."""

    name: str = "Manus"
    description: str = "A versatile agent that can solve various tasks using multiple tools including MCP-based tools"

    system_prompt: str = SYSTEM_PROMPT.format(directory=config.workspace_root)
    next_step_prompt: str = NEXT_STEP_PROMPT

    # Сколько знаков ответа инструмента агент видит. Берём из настроек: предел
    # зависит от окна модели, а оно у разных моделей отличается в сотни раз.
    # Обрезка происходит ДО обращения к модели — отрезанное она не увидит уже
    # никогда, поэтому душить это значение без нужды нельзя.
    max_observe: int = Field(default_factory=lambda: config.agent_config.max_observe)
    max_steps: int = 20

    # MCP clients for remote tool access
    mcp_clients: MCPClients = Field(default_factory=MCPClients)

    # Add general-purpose tools to the tool collection
    available_tools: ToolCollection = Field(
        default_factory=lambda: ToolCollection(
            PythonExecute(),
            # Поиск и прямая загрузка страниц. Без них единственным окном
            # наружу остаётся браузер: один адрес — один шаг агента, и любой
            # антибот останавливает работу целиком.
            WebSearch(),
            Fetch(),
            StrReplaceEditor(),
            # Журнал находок: единственная память задачи, переживающая и
            # вытеснение старых сообщений, и перезапуск контейнера.
            RecordFinding(),
            AskHuman(),
            Terminate(),
        )
    )

    special_tool_names: list[str] = Field(default_factory=lambda: [Terminate().name])

    # Как часто возвращать в разговор хвост журнала, когда окно памяти уже
    # вытесняет старые сообщения, — в шагах агента. Реже, чем раз в двадцать
    # шагов, находки успевают потеряться; чаще — журнал начинает занимать в
    # разговоре больше места, чем сама работа.
    JOURNAL_REFRESH_EVERY: ClassVar[int] = 20
    JOURNAL_TAIL: ClassVar[int] = 4_000

    # На каком шаге журнал возвращали в разговор в прошлый раз.
    journal_refreshed_at: int = -1

    # Сказали ли агенту, что окно памяти начало вытеснять начало разговора.
    eviction_told: bool = False

    # Сколько раз за шаг мы возвращаем агента к брошенному источнику. Одного
    # раза достаточно: если он и после напоминания решит не открывать браузер,
    # это уже осознанный выбор, а не недосмотр. Больше — риск зациклиться.
    blocked_nudges_left: int = 1

    # Сколько раз за шаг возвращаем агента, закончившего с пустым журналом.
    journal_nudges_left: int = 1

    # Писать ли итоговый ответ по журналу. Включается только там, где агент
    # отвечает человеку сам, — в режиме «Агент». Исполнителю пункта плана это
    # не нужно: журнал и так лежит в его постановке задачи, а лишняя выдача
    # съела бы действие из запаса пункта.
    answer_from_journal: bool = False
    answer_nudges_left: int = 1

    # Инструменты, которыми агент добывает данные снаружи. Если он не тронул ни
    # один — записывать ему нечего, и напоминание про журнал будет придиркой.
    # `python_execute` сюда намеренно не входит: сам по себе он ничего не
    # добывает, а задачу вида «посчитай вот это» напоминание только задержало
    # бы. Когда агент что-то посчитал по добытым данным, сработает та вещь,
    # которой он их добыл.
    GATHERING: ClassVar[tuple] = ("web_search", "fetch", "browser", "crawl")

    # Track connected MCP servers
    connected_servers: Dict[str, str] = Field(
        default_factory=dict
    )  # server_id -> url/command
    mcp_instruction_servers: set[str] = Field(default_factory=set, exclude=True)
    _initialized: bool = False

    def _abandoned_sources(self) -> list[str]:
        """Источники, которые fetch пометил закрытыми, а браузер не открывал."""
        fetch = self.available_tools.tool_map.get("fetch")
        blocked = list(getattr(fetch, "blocked_urls", []) or [])
        if not blocked:
            return []
        # Что уже отдавали браузеру — ищем по тексту его вызовов в разговоре.
        tried = "".join(
            call.function.arguments or ""
            for message in self.memory.messages
            if message.tool_calls
            for call in message.tool_calls
            if call.function.name.startswith("browser")
        )
        return [url for url in blocked if url not in tried]

    def _called(self, *prefixes: str) -> int:
        """Сколько раз за этот шаг агент вызывал такие инструменты."""
        return sum(
            1
            for message in self.memory.messages
            if message.tool_calls
            for call in message.tool_calls
            if call.function.name.startswith(prefixes)
        )

    def _journal(self) -> Optional[Ledger]:
        """Журнал этой задачи — там же, где его видит инструмент записи."""
        tool = self.available_tools.tool_map.get("record_finding")
        if tool is None:
            return None
        return Ledger(getattr(tool, "directory", "") or config.workspace_root)

    def _nothing_recorded(self) -> bool:
        """Агент добывал данные, но не записал за шаг ни одной находки.

        Правило «записывай по ходу» живёт в подсказке, а подсказка — не
        механизм: модель, увлёкшаяся чтением страниц, проходит десяток
        действий без единой записи, и всё прочитанное живёт только в
        разговоре, откуда его вытесняет окно памяти.
        """
        return self._called(*self.GATHERING) > 0 and self._called("record_finding") == 0

    async def _handle_special_tool(self, name: str, result: Any, **kwargs):
        """Три проверки перед тем, как дать шагу закрыться.

        Все три — про одно: правило в подсказке не есть механизм. Агент,
        которому сказано «пробуй браузер», «записывай находки», «отвечай по
        журналу», в длинном прогоне делает это через раз, и потерю замечает
        только человек, читая отчёт без половины данных.

        1. Брошенный источник — вернуть и попросить открыть браузером.
        2. Пустой журнал — вернуть и попросить записать добытое.
        3. Итоговый ответ — отдать журнал и попросить писать по нему.

        Каждая срабатывает не более раза за шаг: если агент и после
        напоминания решит иначе, это уже осознанный выбор, а не недосмотр.
        """
        if not self._is_special_tool(name):
            await super()._handle_special_tool(name=name, result=result, **kwargs)
            return

        if self._nudge_blocked_sources():
            return  # состояние FINISHED не выставляем, шаг продолжается
        if self._nudge_empty_journal():
            return
        if self._nudge_answer_from_journal():
            return
        await super()._handle_special_tool(name=name, result=result, **kwargs)

    def _nudge_blocked_sources(self) -> bool:
        """Не даём закрыть шаг, бросив источник непопробованным.

        В разобранном прогоне fetch четыре раза сказал «идите через
        browser_exec», list.am отдал 403 — и агент ни разу не открыл браузер,
        подменив главную доску объявлений страны пересказом из поисковой
        выдачи. Совет в тексте ответа оказался слишком слабым средством.
        """
        if self.blocked_nudges_left > 0:
            abandoned = self._abandoned_sources()
            if abandoned:
                self.blocked_nudges_left -= 1
                listed = "\n".join(f"  - {url}" for url in abandoned[:5])
                self.memory.add_message(
                    Message.user_message(
                        "Шаг ещё не закончен. Эти источники прямым запросом не "
                        f"открылись, и браузер к ним не применялся:\n{listed}\n"
                        "Откройте их через browser_exec — new_tab(адрес), "
                        "wait_for_load(), затем js(\"document.body.innerText\"). "
                        "Если браузер тоже не справится, запишите это в находки "
                        "и тогда завершайте шаг. Данные из первоисточника "
                        "весомее пересказа поисковой выдачи."
                    )
                )
                logger.info(
                    f"Завершение шага отложено: {len(abandoned)} источников "
                    "закрыты и не проверены браузером"
                )
                return True
        return False

    def _nudge_empty_journal(self) -> bool:
        """Не даём закрыть шаг, в котором ничего не записано.

        Агент читал страницы, считал, делал выводы — и не оставил ни строчки.
        Его разговор скоро вытеснится или будет очищен перед следующим
        пунктом, и всё добытое исчезнет вместе с ним.
        """
        if self.journal_nudges_left <= 0 or not self._nothing_recorded():
            return False
        self.journal_nudges_left -= 1
        self.memory.add_message(
            Message.user_message(
                "Шаг ещё не закончен: вы добывали данные, но не записали ни "
                "одной находки. Всё, что вы узнали, живёт сейчас только в этом "
                "разговоре — а он короче задачи.\n"
                "Вызовите record_finding на каждый добытый факт: число, цену, "
                "ставку, дату, вывод, посчитанную величину — с источником и "
                "датой источника. Если добыть ничего не удалось, запишите "
                "именно это: какой источник не открылся и что из-за него "
                "осталось неизвестным. Пустая неудача тоже находка. Потом "
                "завершайте шаг."
            )
        )
        logger.info("Завершение шага отложено: за шаг не записано ни одной находки")
        return True

    def _nudge_answer_from_journal(self) -> bool:
        """Итоговый ответ пишется по журналу, а не по памяти.

        В режиме «Агент» финальный ответ рождается в том же разговоре, где
        дословно живы лишь последние обмены: находки начала работы из него уже
        вытеснены. Отдаём журнал перед завершением — тогда ответ опирается на
        всё собранное, а не на то, что случайно уцелело.
        """
        if not self.answer_from_journal or self.answer_nudges_left <= 0:
            return False
        ledger = self._journal()
        body = ledger.read().strip() if ledger else ""
        if not body:
            return False
        self.answer_nudges_left -= 1
        self.memory.add_message(
            Message.user_message(
                "Прежде чем закончить — вот всё, что вы записали за эту "
                f"работу (файл {ledger.path}). Ранние находки из разговора уже "
                "вытеснены, так что отвечайте по этому журналу, а не по "
                "памяти.\n\n" + body + "\n\nТеперь напишите человеку итоговый "
                "ответ обычным текстом: сам результат — числа со ссылками и "
                "датами источников, выводы, и отдельно то, чего добыть не "
                "удалось. После этого вызовите terminate."
            )
        )
        logger.info(f"Завершение отложено: журнал ({len(body)} знаков) отдан для итога")
        return True

    @classmethod
    async def create(cls, **kwargs) -> "Manus":
        """Factory method to create and properly initialize a Manus instance."""
        instance = cls(**kwargs)
        await instance.initialize_mcp_servers()
        instance._initialized = True
        return instance

    async def initialize_mcp_servers(self) -> None:
        """Initialize connections to configured MCP servers."""
        if _BROWSER_USE_SERVER_ID not in config.mcp_config.servers and os.getenv(
            "OPENMANUS_DISABLE_BROWSER_USE", ""
        ).lower() not in {"1", "true", "yes"}:
            try:
                await self.connect_mcp_server(
                    _BROWSER_USE_COMMAND,
                    _BROWSER_USE_SERVER_ID,
                    use_stdio=True,
                    stdio_args=_BROWSER_USE_ARGS,
                    tool_name_prefix=False,
                    stdio_env=_browser_use_env(),
                )
                logger.info("Connected to Browser Use CLI 3.0 through MCP")
            except Exception as e:
                logger.error(f"Failed to connect to Browser Use CLI 3.0: {e}")

        for server_id, server_config in config.mcp_config.servers.items():
            try:
                if server_config.type == "sse":
                    if server_config.url:
                        await self.connect_mcp_server(server_config.url, server_id)
                        logger.info(
                            f"Connected to MCP server {server_id} at {server_config.url}"
                        )
                elif server_config.type == "stdio":
                    if server_config.command:
                        await self.connect_mcp_server(
                            server_config.command,
                            server_id,
                            use_stdio=True,
                            stdio_args=server_config.args,
                            tool_name_prefix=server_id != _BROWSER_USE_SERVER_ID,
                            stdio_env=(
                                _browser_use_env()
                                if server_id == _BROWSER_USE_SERVER_ID
                                else (server_config.env or None)
                            ),
                        )
                        logger.info(
                            f"Connected to MCP server {server_id} using command {server_config.command}"
                        )
            except Exception as e:
                logger.error(f"Failed to connect to MCP server {server_id}: {e}")

    async def connect_mcp_server(
        self,
        server_url: str,
        server_id: str = "",
        use_stdio: bool = False,
        stdio_args: Optional[List[str]] = None,
        tool_name_prefix: bool = True,
        stdio_env: Optional[Dict[str, str]] = None,
    ) -> None:
        """Connect to an MCP server and add its tools."""
        if use_stdio:
            await self.mcp_clients.connect_stdio(
                server_url,
                stdio_args or [],
                server_id,
                tool_name_prefix=tool_name_prefix,
                env=stdio_env,
            )
            self.connected_servers[server_id or server_url] = server_url
        else:
            await self.mcp_clients.connect_sse(server_url, server_id)
            self.connected_servers[server_id or server_url] = server_url

        # Update available tools with only the new tools from this server
        new_tools = [
            tool for tool in self.mcp_clients.tools if tool.server_id == server_id
        ]
        self.available_tools.add_tools(*new_tools)

        resolved_server_id = server_id or server_url
        instructions = self.mcp_clients.server_instructions.get(resolved_server_id)
        if instructions and resolved_server_id not in self.mcp_instruction_servers:
            transport_instructions = (
                f"{_BROWSER_USE_TRANSPORT_INSTRUCTIONS}\n"
                if resolved_server_id == _BROWSER_USE_SERVER_ID
                else ""
            )
            self.memory.add_message(
                Message.system_message(
                    f"{transport_instructions}MCP server instructions:\n{instructions}"
                )
            )
            self.mcp_instruction_servers.add(resolved_server_id)

    async def disconnect_mcp_server(self, server_id: str = "") -> None:
        """Disconnect from an MCP server and remove its tools."""
        await self.mcp_clients.disconnect(server_id)
        if server_id:
            self.connected_servers.pop(server_id, None)
        else:
            self.connected_servers.clear()

        # Rebuild available tools without the disconnected server's tools
        base_tools = [
            tool
            for tool in self.available_tools.tools
            if not isinstance(tool, MCPClientTool)
        ]
        self.available_tools = ToolCollection(*base_tools)
        self.available_tools.add_tools(*self.mcp_clients.tools)

    async def cleanup(self):
        """Clean up Manus agent resources."""
        # Disconnect from all MCP servers only if we were initialized
        if self._initialized:
            await self.disconnect_mcp_server()
            self._initialized = False

    def _notice_eviction(self) -> None:
        """Говорит агенту, что начало разговора вытеснено окном памяти.

        Это единственное место во всей цепочке, которое выбрасывало молча.
        Вернуть вытесненное нельзя — оно не сохранялось нигде, кроме разговора.
        Но молчание хуже потери: агент продолжает считать, что помнит всё, и
        опирается на то, чего уже нет. Говорим один раз за прогон, дальше о
        журнале ему напоминает возврат журнала.
        """
        if self.eviction_told or not self.memory.dropped:
            return
        self.eviction_told = True
        self.memory.add_message(
            Message.user_message(
                f"Разговор перерос окно памяти: {self.memory.dropped} ранних "
                "сообщений из него удалено, и вернуть их нельзя. Не считайте, "
                "что помните всё с начала работы.\n"
                "Цело то, что вы записали: находки — в findings.md, скачанные "
                "страницы и таблицы — файлами в рабочей папке. Если для "
                "дальнейшего нужно что-то из начала, возьмите это оттуда, а не "
                "из памяти."
            )
        )
        logger.info(
            f"Агент предупреждён о вытеснении: {self.memory.dropped} сообщений"
        )

    def _refresh_journal(self) -> None:
        """Возвращает в разговор хвост журнала, когда память уже вытесняет.

        Память агента — сто последних сообщений. В длинном прогоне находки
        пятого шага к сорок пятому из неё вытеснены молча: агент не знает, что
        забыл, и идёт добывать то же самое заново — а источник за это время мог
        закрыться. Пока окно не переполнено, вмешиваться незачем: всё найденное
        и так в разговоре.
        """
        if len(self.memory.messages) < self.memory.max_messages:
            return
        since = self.current_step - self.journal_refreshed_at
        if self.journal_refreshed_at >= 0 and since < self.JOURNAL_REFRESH_EVERY:
            return
        tool = self.available_tools.tool_map.get("record_finding")
        if tool is None:
            return
        ledger = Ledger(getattr(tool, "directory", "") or config.workspace_root)
        body = ledger.read(self.JOURNAL_TAIL).strip()
        if not body:
            return
        self.journal_refreshed_at = self.current_step
        self.memory.add_message(
            Message.user_message(
                "Разговор стал длинным, и ранние сообщения из него уже "
                "вытеснены — того, что вы нашли в начале работы, в нём больше "
                "нет. Не собирайте это заново: вот хвост вашего журнала "
                f"находок (файл {ledger.path}), полный файл открывается через "
                f"str_replace_editor.\n\n{body}"
            )
        )
        logger.info(
            f"Журнал находок возвращён в разговор на шаге {self.current_step} "
            f"({len(body)} знаков)"
        )

    async def think(self) -> bool:
        """Process current state and decide next actions with appropriate context."""
        if not self._initialized:
            await self.initialize_mcp_servers()
            self._initialized = True

        self._notice_eviction()
        self._refresh_journal()
        return await super().think()
