"""Reading and writing OpenManus configuration from the browser.

Everything the console user edits by hand in `config/config.toml` and
`config/mcp.json` is exposed here, so the web interface can change models,
browser, search and sandbox options without a terminal.
"""

import json
import shutil
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List

from app.config import (
    PROJECT_ROOT,
    BrowserSettings,
    LLMSettings,
    RunflowSettings,
    SandboxSettings,
    SearchSettings,
    config,
)
from app.llm import LLM
from app.logger import logger


CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
EXAMPLE_PATH = PROJECT_ROOT / "config" / "config.example.toml"
MCP_PATH = PROJECT_ROOT / "config" / "mcp.json"
LOG_DIR = PROJECT_ROOT / "logs"

# sections the interface is allowed to write back
EDITABLE_SECTIONS = (
    "llm",
    "browser",
    "search",
    "sandbox",
    "runflow",
    "daytona",
    "store",
)


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format(item) for item in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dumps(data: Dict[str, Any]) -> str:
    """Serialise the config subset we manage back to TOML."""
    lines: List[str] = []

    def emit(table: Dict[str, Any], prefix: str) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in table.items() if isinstance(v, dict)}
        if scalars or not tables:
            lines.append(f"[{prefix}]")
            for key, value in scalars.items():
                if value is not None and value != "":
                    lines.append(f"{key} = {_format(value)}")
            lines.append("")
        for name, sub in tables.items():
            emit(sub, f"{prefix}.{name}")

    for section, table in data.items():
        if isinstance(table, dict) and table:
            emit(table, section)
    return "\n".join(lines).rstrip() + "\n"


def read_raw() -> Dict[str, Any]:
    """The current config file, or the shipped example when there is none."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_PATH
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def read_settings() -> Dict[str, Any]:
    raw = read_raw()
    return {
        "sections": {name: raw.get(name, {}) for name in EDITABLE_SECTIONS},
        "path": str(CONFIG_PATH),
        "exists": CONFIG_PATH.exists(),
        "defaults": {
            "browser": BrowserSettings().model_dump(exclude={"proxy"}),
            "search": SearchSettings().model_dump(),
            "sandbox": SandboxSettings().model_dump(),
            "runflow": RunflowSettings().model_dump(),
        },
    }


def validate(sections: Dict[str, Any]) -> None:
    """Reject a broken config before it reaches disk."""
    llm = sections.get("llm", {})
    base = {k: v for k, v in llm.items() if not isinstance(v, dict)}
    LLMSettings(
        model=base.get("model", ""),
        base_url=base.get("base_url", ""),
        api_key=base.get("api_key", ""),
        max_tokens=base.get("max_tokens", 4096),
        max_input_tokens=base.get("max_input_tokens"),
        temperature=base.get("temperature", 0.0),
        api_type=base.get("api_type", ""),
        api_version=base.get("api_version", ""),
    )
    if sections.get("browser"):
        BrowserSettings(
            **{k: v for k, v in sections["browser"].items() if k != "proxy"}
        )
    if sections.get("search"):
        SearchSettings(**sections["search"])
    if sections.get("sandbox"):
        SandboxSettings(**sections["sandbox"])
    if sections.get("runflow"):
        RunflowSettings(**sections["runflow"])


def write_settings(sections: Dict[str, Any]) -> None:
    """Write the config file and make the running process pick it up."""
    validate(sections)
    raw = read_raw()
    for name in EDITABLE_SECTIONS:
        if name in sections:
            value = sections[name]
            if value:
                raw[name] = value
            else:
                raw.pop(name, None)
    raw.setdefault("mcp", {"server_reference": "app.mcp.server"})

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(dumps(raw), encoding="utf-8")
    reload_config()


def reload_config() -> None:
    """Re-read config.toml into the singleton and drop cached LLM clients."""
    config._load_initial_config()
    LLM._instances.clear()
    logger.info("Configuration reloaded from disk")


# ------------------------------------------------------------------- MCP servers


# ready-made servers offered in the interface; "node" ones need Node.js
MCP_CATALOGUE = [
    {
        "id": "fetch",
        "title": "Загрузка страниц",
        "description": "Скачивает веб-страницу и отдаёт её текстом. Python, ставится сам.",
        "server": {"type": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]},
    },
    {
        "id": "time",
        "title": "Время и часовые пояса",
        "description": "Текущее время, перевод между часовыми поясами. Python.",
        "server": {"type": "stdio", "command": "uvx", "args": ["mcp-server-time"]},
    },
    {
        "id": "git",
        "title": "Git-репозитории",
        "description": "Чтение истории и файлов локального репозитория. Python.",
        "server": {
            "type": "stdio",
            "command": "uvx",
            "args": ["mcp-server-git", "--repository", "/app/OpenManus/workspace"],
        },
    },
    {
        "id": "sqlite",
        "title": "База SQLite",
        "description": "Запросы к файлу базы данных внутри workspace. Python.",
        "server": {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "mcp-server-sqlite",
                "--db-path",
                "/app/OpenManus/workspace/data.db",
            ],
        },
    },
    {
        "id": "filesystem",
        "title": "Файловая система",
        "description": "Чтение и запись файлов в указанной папке. Требует Node.js.",
        "needs_node": True,
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "/app/OpenManus/workspace",
            ],
        },
    },
    {
        "id": "memory",
        "title": "Долгая память",
        "description": "Граф знаний, который агент наполняет между задачами. Требует Node.js.",
        "needs_node": True,
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
        },
    },
    {
        "id": "sequential-thinking",
        "title": "Пошаговое рассуждение",
        "description": "Помогает модели разбивать задачу на шаги. Требует Node.js.",
        "needs_node": True,
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        },
    },
    {
        "id": "github",
        "title": "GitHub",
        "description": "Issues, pull requests, код. Нужен токен GITHUB_PERSONAL_ACCESS_TOKEN и Node.js.",
        "needs_node": True,
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
        },
    },
    {
        "id": "slack",
        "title": "Slack",
        "description": "Чтение и отправка сообщений. Нужны SLACK_BOT_TOKEN и SLACK_TEAM_ID, Node.js.",
        "needs_node": True,
        "server": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-slack"],
            "env": {"SLACK_BOT_TOKEN": "", "SLACK_TEAM_ID": ""},
        },
    },
]


def node_available() -> bool:
    return shutil.which("npx") is not None


def read_mcp() -> Dict[str, Any]:
    if not MCP_PATH.exists():
        return {"mcpServers": {}, "disabledServers": {}}
    try:
        data = json.loads(MCP_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mcpServers": {}, "disabledServers": {}}
    data.setdefault("mcpServers", {})
    data.setdefault("disabledServers", {})
    return data


def _check_server(server_id: str, server: Dict[str, Any]) -> None:
    if server.get("type") not in {"sse", "stdio"}:
        raise ValueError(f"{server_id}: тип должен быть sse или stdio")
    if server["type"] == "sse" and not server.get("url"):
        raise ValueError(f"{server_id}: для sse нужен адрес")
    if server["type"] == "stdio" and not server.get("command"):
        raise ValueError(f"{server_id}: для stdio нужна команда")


def write_mcp(servers: Dict[str, Any], disabled: Dict[str, Any] = None) -> None:
    """Enabled servers go to mcpServers; the rest are kept, but not loaded."""
    for server_id, server in servers.items():
        _check_server(server_id, server)
    for server_id, server in (disabled or {}).items():
        _check_server(server_id, server)

    MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_PATH.write_text(
        json.dumps(
            {"mcpServers": servers, "disabledServers": disabled or {}},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reload_config()


# ------------------------------------------------------------------------- logs


def list_logs(limit: int = 30) -> List[Dict[str, Any]]:
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "modified": path.stat().st_mtime,
        }
        for path in files[:limit]
    ]


# Every start of the interface opens a log file of its own and nothing ever
# removes them, so they pile up for as long as the installation lives. Loguru's
# own `retention` does not help: it runs on rotation, and a file named after the
# start time never rotates. So the old ones are swept at start-up instead.
LOG_MAX_AGE_DAYS = 14
LOG_ALWAYS_KEEP = 20  # a rarely used installation keeps its history anyway


def prune_logs(
    max_age_days: int = LOG_MAX_AGE_DAYS, always_keep: int = LOG_ALWAYS_KEEP
) -> List[str]:
    """Delete log files older than the limit, keeping the newest few regardless."""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = time.time() - max_age_days * 86400
    removed = []
    for path in files[always_keep:]:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path.name)
        except OSError as exc:  # a locked or vanished file must not stop start-up
            logger.warning(f"Could not remove the old log {path.name}: {exc}")
    if removed:
        logger.info(
            f"Removed {len(removed)} log file(s) older than {max_age_days} days"
        )
    return removed


def read_log(name: str, tail: int = 400) -> str:
    path = (LOG_DIR / name).resolve()
    if LOG_DIR.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError(name)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])
