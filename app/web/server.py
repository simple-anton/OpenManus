"""FastAPI application exposing Manus through a browser UI."""

import asyncio
import base64
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import config
from app.logger import logger
from app.web import api_tools as api_tools_store
from app.web import diagnostics
from app.web import settings as settings_store
from app.web import skills as skills_store
from app.web.session import Session


STATIC_DIR = Path(__file__).parent / "static"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_CHARS = 200_000
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# session_id -> Session
SESSIONS: Dict[str, Session] = {}


class PromptRequest(BaseModel):
    prompt: str
    mode: str = "agent"  # agent runs the tool loop, chat answers, flow plans first


class AnswerRequest(BaseModel):
    answer: str


class SessionPatch(BaseModel):
    title: Optional[str] = None
    max_steps: Optional[int] = None
    skills: Optional[List[str]] = None


class SkillRequest(BaseModel):
    slug: str = ""
    name: str
    description: str = ""
    body: str


class ImportRequest(BaseModel):
    url: str
    force: bool = False


class ApiToolsRequest(BaseModel):
    tools: List[Dict[str, Any]]


class ApiTestRequest(BaseModel):
    spec: Dict[str, Any]
    arguments: Dict[str, Any] = {}


class SettingsRequest(BaseModel):
    sections: Dict[str, Any]


class McpRequest(BaseModel):
    servers: Dict[str, Any]
    disabled: Dict[str, Any] = {}


def _log_sink(message) -> None:
    """Loguru sink forwarding agent logs to the session that produced them."""
    record = message.record
    session = SESSIONS.get(record["extra"].get("web_session"))
    if session is None:
        return
    level = record["level"].name
    session.publish("log", level=level, message=record["message"], name=record["name"])
    if level in {"WARNING", "ERROR", "CRITICAL"}:
        # do not leave the user staring at an empty screen while retries happen
        session.publish(
            "notice", level=level, message=record["message"], name=record["name"]
        )


def _workspace_root(session_id: Optional[str]) -> Path:
    """A task's own folder when it has one, the shared workspace otherwise."""
    root = Path(config.workspace_root)
    session = SESSIONS.get(session_id) if session_id else None
    if session is not None and Path(session.workspace).exists():
        return Path(session.workspace)
    return root


def _resolve_in_workspace(path: str, session_id: Optional[str]) -> Path:
    root = _workspace_root(session_id).resolve()
    target = (root / path).resolve()
    if not target.is_file() or root not in target.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _incompatible(exc) -> Dict[str, Any]:
    """A refusal the interface can explain and offer a way around."""
    return {
        "reason": "incompatible",
        "message": f"Навык «{exc.name}» не заработает в этой сборке.",
        "notes": exc.verdict["notes"],
        "advice": "Он рассчитан на среду, откуда его выгрузили: без неё скрипты навыка "
        "не запустятся. Можно взять только текстовую методику — без скриптов.",
    }


def _get_session(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return session


def _llm_info() -> Dict[str, Any]:
    llm = config.llm.get("default")
    if llm is None:
        return {}
    return {
        "model": llm.model,
        "base_url": llm.base_url,
        "api_type": llm.api_type or "openai",
        "max_tokens": llm.max_tokens,
        "temperature": llm.temperature,
    }


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sink_id = logger.add(
            _log_sink,
            level="INFO",
            filter=lambda record: "web_session" in record["extra"],
        )
        SESSIONS.update(Session.restore_all())
        logger.info("OpenManus web interface ready")
        try:
            yield
        finally:
            for session in list(SESSIONS.values()):
                await session.cleanup()
            SESSIONS.clear()
            logger.remove(sink_id)

    app = FastAPI(title="OpenManus Web", lifespan=lifespan)

    # ------------------------------------------------------------------- pages

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def read_config() -> Dict[str, Any]:
        return {
            "llm": _llm_info(),
            "workspace": str(config.workspace_root),
            "runflow": {
                "use_data_analysis_agent": config.run_flow_config.use_data_analysis_agent
            },
            "mcp_servers": sorted(config.mcp_config.servers),
        }

    # ---------------------------------------------------------------- sessions

    @app.get("/api/sessions")
    async def list_sessions() -> Dict[str, List[Dict[str, Any]]]:
        sessions = sorted(SESSIONS.values(), key=lambda s: s.created_at, reverse=True)
        return {"sessions": [s.info() for s in sessions]}

    @app.post("/api/sessions")
    async def create_session() -> Dict[str, Any]:
        try:
            session = Session(uuid.uuid4().hex[:12])
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        SESSIONS[session.id] = session
        return session.info()

    @app.get("/api/sessions/{session_id}")
    async def read_session(session_id: str) -> Dict[str, Any]:
        return _get_session(session_id).info()

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, patch: SessionPatch) -> Dict[str, Any]:
        session = _get_session(session_id)
        if patch.title is not None:
            session.title = patch.title.strip()[:80] or session.title
        if patch.max_steps is not None:
            session.max_steps = max(1, min(patch.max_steps, 100))
            if session.agent is not None:
                session.agent.max_steps = session.max_steps
        if patch.skills is not None:
            session.set_skills(patch.skills)
        session._save_meta()
        return session.info()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> Dict[str, str]:
        session = _get_session(session_id)
        await session.cleanup()
        session.forget()
        SESSIONS.pop(session_id, None)
        return {"status": "deleted"}

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, request: PromptRequest) -> Dict[str, Any]:
        session = _get_session(session_id)
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Empty prompt")
        action = session.submit(prompt, mode=request.mode)
        return {**session.info(), "action": action}

    @app.post("/api/sessions/{session_id}/answer")
    async def answer(session_id: str, request: AnswerRequest) -> Dict[str, Any]:
        session = _get_session(session_id)
        if not session.answer(request.answer):
            raise HTTPException(status_code=409, detail="No question is pending")
        return session.info()

    @app.post("/api/sessions/{session_id}/stop")
    async def stop(session_id: str) -> Dict[str, Any]:
        session = _get_session(session_id)
        await session.stop()
        return session.info()

    @app.get("/api/sessions/{session_id}/events")
    async def events(session_id: str) -> StreamingResponse:
        session = _get_session(session_id)

        async def event_stream():
            try:
                async for event in session.stream():
                    if event is None:
                        yield ": keep-alive\n\n"
                    else:
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:  # browser closed the tab
                raise

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --------------------------------------------------------------- workspace

    @app.get("/api/workspace")
    async def workspace(session: Optional[str] = None) -> Dict[str, Any]:
        root = _workspace_root(session)
        files = []
        if root.exists():
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue  # session store and other bookkeeping
                if path.is_file():
                    stat = path.stat()
                    files.append(
                        {
                            "path": str(relative),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
        files.sort(key=lambda item: item["modified"], reverse=True)
        return {"files": files[:200], "root": str(root)}

    @app.get("/api/workspace/file")
    async def workspace_file(path: str, session: Optional[str] = None) -> FileResponse:
        target = _resolve_in_workspace(path, session)
        return FileResponse(target, filename=target.name)

    @app.get("/api/workspace/preview")
    async def workspace_preview(
        path: str, session: Optional[str] = None
    ) -> Dict[str, Any]:
        """Content of a file, so the panel can show it without downloading."""
        target = _resolve_in_workspace(path, session)
        size = target.stat().st_size
        suffix = target.suffix.lower()

        if suffix in IMAGE_SUFFIXES:
            if size > MAX_PREVIEW_BYTES:
                return {"kind": "binary", "size": size}
            data = base64.b64encode(target.read_bytes()).decode()
            mime = "svg+xml" if suffix == ".svg" else suffix.lstrip(".")
            return {
                "kind": "image",
                "size": size,
                "data_url": f"data:image/{mime};base64,{data}",
            }

        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return {"kind": "binary", "size": size}
        return {
            "kind": "text",
            "size": size,
            "truncated": len(text) > MAX_PREVIEW_CHARS,
            "content": text[:MAX_PREVIEW_CHARS],
        }

    @app.put("/api/sessions/{session_id}/files")
    async def upload_file(
        session_id: str, name: str, request: Request
    ) -> Dict[str, Any]:
        """Put a file the user picked into the task's folder."""
        session = _get_session(session_id)
        safe_name = Path(name).name
        if not safe_name or safe_name.startswith("."):
            raise HTTPException(status_code=400, detail="Bad file name")
        payload = await request.body()
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File is too large")
        target = Path(session.workspace) / safe_name
        target.write_bytes(payload)
        session.publish(
            "upload", message=f"Загружен файл: {safe_name}", path=str(target)
        )
        return {"path": safe_name, "size": len(payload), "full_path": str(target)}

    @app.delete("/api/sessions/{session_id}/files")
    async def delete_file(session_id: str, path: str) -> Dict[str, str]:
        target = _resolve_in_workspace(path, session_id)
        target.unlink()
        return {"status": "deleted"}

    # ---------------------------------------------------------------- settings

    @app.get("/api/settings")
    async def read_settings() -> Dict[str, Any]:
        return settings_store.read_settings()

    @app.post("/api/settings")
    async def write_settings(request: SettingsRequest) -> Dict[str, Any]:
        try:
            settings_store.write_settings(request.sections)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        for session in SESSIONS.values():
            await session.reset_agent()
        return {"status": "saved", "llm": _llm_info()}

    @app.get("/api/settings/models")
    async def list_models() -> Dict[str, Any]:
        """Models offered by the configured server, when it can list them."""
        llm = config.llm.get("default")
        if llm is None:
            return {"models": []}
        base = llm.base_url.rstrip("/")
        candidates = []
        if base.endswith("/v1"):
            candidates.append(base[: -len("/v1")] + "/api/tags")  # Ollama
        candidates.append(base + "/models")  # OpenAI-compatible
        async with httpx.AsyncClient(timeout=8) as client:
            for url in candidates:
                try:
                    response = await client.get(
                        url, headers={"Authorization": f"Bearer {llm.api_key}"}
                    )
                    if response.status_code != 200:
                        continue
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                if isinstance(payload.get("models"), list):
                    names = [m.get("name") or m.get("model") for m in payload["models"]]
                elif isinstance(payload.get("data"), list):
                    names = [m.get("id") for m in payload["data"]]
                else:
                    continue
                return {"models": sorted(n for n in names if n), "source": url}
        return {"models": [], "source": None}

    @app.post("/api/settings/test")
    async def test_connection() -> Dict[str, Any]:
        """Ask the configured model to say one word, and report what happened."""
        llm = config.llm.get("default")
        if llm is None:
            raise HTTPException(status_code=400, detail="No LLM configured")
        url = llm.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": llm.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {llm.api_key}"},
                )
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"Нет соединения: {exc}"}
        if response.status_code != 200:
            return {
                "ok": False,
                "detail": f"HTTP {response.status_code}: {response.text[:300]}",
            }
        try:
            content = response.json()["choices"][0]["message"].get("content", "")
        except (ValueError, KeyError, IndexError):
            return {"ok": False, "detail": "Неожиданный ответ сервера"}
        return {"ok": True, "detail": (content or "(пустой ответ)")[:200]}

    @app.get("/api/mcp")
    async def read_mcp() -> Dict[str, Any]:
        stored = settings_store.read_mcp()
        return {
            "servers": stored.get("mcpServers", {}),
            "disabled": stored.get("disabledServers", {}),
            "catalogue": settings_store.MCP_CATALOGUE,
            "node": settings_store.node_available(),
            "connected": sorted(config.mcp_config.servers),
        }

    @app.post("/api/mcp")
    async def write_mcp(request: McpRequest) -> Dict[str, Any]:
        try:
            settings_store.write_mcp(request.servers, request.disabled)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        for session in SESSIONS.values():
            await session.reset_agent()
        return {"status": "saved", "connected": sorted(config.mcp_config.servers)}

    # ------------------------------------------------------------------ skills

    @app.get("/api/skills")
    async def list_skills() -> Dict[str, Any]:
        return {
            "skills": skills_store.list_skills(),
            "catalogue": skills_store.PUBLIC_CATALOGUE,
        }

    @app.get("/api/skills/{slug}")
    async def read_skill(slug: str) -> Dict[str, Any]:
        skill = skills_store.read_skill(slug)
        if skill is None:
            raise HTTPException(status_code=404, detail="Навык не найден")
        return skill

    @app.post("/api/skills")
    async def write_skill(request: SkillRequest) -> Dict[str, Any]:
        try:
            return skills_store.write_skill(
                request.slug, request.name, request.description, request.body
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/skills/{slug}")
    async def delete_skill(slug: str) -> Dict[str, str]:
        skills_store.delete_skill(slug)
        for session in SESSIONS.values():
            if slug in session.skills:
                session.set_skills([s for s in session.skills if s != slug])
        return {"status": "deleted"}

    @app.put("/api/skills/archive")
    async def upload_skill_archive(
        request: Request, force: bool = False
    ) -> Dict[str, Any]:
        """A zipped skill folder, as exported from another agent."""
        try:
            return skills_store.import_archive(await request.body(), force=force)
        except skills_store.Incompatible as exc:
            raise HTTPException(status_code=409, detail=_incompatible(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/skills/import")
    async def import_skill(request: ImportRequest) -> Dict[str, Any]:
        try:
            return await skills_store.import_from_url(request.url, force=request.force)
        except skills_store.Incompatible as exc:
            raise HTTPException(status_code=409, detail=_incompatible(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # -------------------------------------------------------------- api tools

    @app.get("/api/api-tools")
    async def read_api_tools() -> Dict[str, Any]:
        return {"tools": api_tools_store.read_specs()}

    @app.post("/api/api-tools")
    async def write_api_tools(request: ApiToolsRequest) -> Dict[str, Any]:
        # drafts the user added but never named are simply dropped
        specs = [spec for spec in request.tools if (spec.get("name") or "").strip()]
        try:
            api_tools_store.write_specs(specs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        for session in SESSIONS.values():
            await session.reset_agent()
        return {"status": "saved", "tools": api_tools_store.read_specs()}

    @app.post("/api/api-tools/test")
    async def test_api_tool(request: ApiTestRequest) -> Dict[str, Any]:
        try:
            result = await api_tools_store.try_call(request.spec, request.arguments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"result": result, "ok": not result.startswith("Error")}

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {"checks": await diagnostics.health_checks()}

    # -------------------------------------------------------------------- logs

    @app.get("/api/logs")
    async def list_logs() -> Dict[str, Any]:
        return {"files": settings_store.list_logs()}

    @app.get("/api/logs/{name}")
    async def read_log(name: str) -> Dict[str, Any]:
        try:
            return {"name": name, "content": settings_store.read_log(name)}
        except (FileNotFoundError, OSError):
            raise HTTPException(status_code=404, detail="Log not found")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
