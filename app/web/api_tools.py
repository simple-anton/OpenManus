"""User-defined HTTP tools.

An entry describes one endpoint - address, method, headers and parameters - and
becomes an ordinary tool in the agent's toolbox. This is how a data source like
CoinGecko or an internal service is wired in without writing code.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx

from app.config import PROJECT_ROOT
from app.logger import logger
from app.tool.base import BaseTool


TOOLS_PATH = PROJECT_ROOT / "config" / "api_tools.json"
MAX_RESPONSE_CHARS = 20000
REQUEST_TIMEOUT = 60


def read_specs() -> List[Dict[str, Any]]:
    if not TOOLS_PATH.is_file():
        return []
    try:
        data = json.loads(TOOLS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data.get("tools", [])


def write_specs(specs: List[Dict[str, Any]]) -> None:
    for spec in specs:
        validate(spec)
    TOOLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOLS_PATH.write_text(
        json.dumps({"tools": specs}, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def validate(spec: Dict[str, Any]) -> None:
    name = spec.get("name", "")
    if not name or not name.replace("_", "").isalnum():
        raise ValueError(f"Имя «{name}»: только латиница, цифры и подчёркивание")
    if not spec.get("url", "").startswith(("http://", "https://")):
        raise ValueError(f"{name}: адрес должен начинаться с http:// или https://")
    if spec.get("method", "GET").upper() not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError(f"{name}: метод должен быть GET, POST, PUT или DELETE")
    for param in spec.get("params", []):
        if not param.get("name"):
            raise ValueError(f"{name}: у параметра нет имени")
        if param.get("in", "query") not in {"query", "path", "body", "header"}:
            raise ValueError(f"{name}: параметр {param['name']} - неизвестное место")


class CustomApiTool(BaseTool):
    """One endpoint described in the web interface."""

    spec: Dict[str, Any] = {}

    @classmethod
    def from_spec(cls, spec: Dict[str, Any]) -> "CustomApiTool":
        properties = {}
        required = []
        for param in spec.get("params", []):
            properties[param["name"]] = {
                "type": param.get("type", "string"),
                "description": param.get("description", ""),
            }
            if param.get("required"):
                required.append(param["name"])
        return cls(
            name=spec["name"],
            description=spec.get("description", "")
            or f"HTTP {spec.get('method', 'GET')} {spec['url']}",
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            spec=spec,
        )

    async def execute(self, **kwargs: Any) -> str:
        spec = self.spec
        url = spec["url"]
        query: Dict[str, Any] = {}
        body: Dict[str, Any] = {}
        headers = dict(spec.get("headers") or {})

        for param in spec.get("params", []):
            name = param["name"]
            value = kwargs.get(name, param.get("default"))
            if value is None or value == "":
                continue
            where = param.get("in", "query")
            if where == "path":
                url = url.replace("{" + name + "}", str(value))
            elif where == "header":
                headers[name] = str(value)
            elif where == "body":
                body[name] = value
            else:
                query[name] = value

        method = spec.get("method", "GET").upper()
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.request(
                    method,
                    url,
                    params=query or None,
                    json=body or None,
                    headers=headers or None,
                )
        except httpx.HTTPError as exc:
            return f"Error: запрос не удался: {exc}"

        text = response.text
        if len(text) > MAX_RESPONSE_CHARS:
            text = text[:MAX_RESPONSE_CHARS] + "\n... (ответ обрезан)"
        logger.info(f"Custom API {self.name}: HTTP {response.status_code}")
        prefix = "" if response.is_success else f"Error: HTTP {response.status_code}\n"
        return f"{prefix}{text}"


def build_tools() -> List[CustomApiTool]:
    tools = []
    for spec in read_specs():
        try:
            validate(spec)
            tools.append(CustomApiTool.from_spec(spec))
        except (ValueError, KeyError) as exc:
            logger.warning(f"Skipping broken API tool: {exc}")
    return tools


async def try_call(spec: Dict[str, Any], arguments: Dict[str, Any]) -> str:
    """Run one endpoint once, so the interface can show what it returns."""
    validate(spec)
    tool = CustomApiTool.from_spec(spec)
    result = await tool.execute(**arguments)
    return result[:4000]
