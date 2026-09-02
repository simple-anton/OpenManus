"""FastAPI application exposing Manus through a browser UI."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import config
from app.logger import logger
from app.web.session import Session


STATIC_DIR = Path(__file__).parent / "static"

# session_id -> Session
SESSIONS: Dict[str, Session] = {}


class PromptRequest(BaseModel):
    prompt: str
    mode: str = "agent"  # "agent" runs the tool loop, "chat" answers directly


class AnswerRequest(BaseModel):
    answer: str


def _log_sink(message) -> None:
    """Loguru sink forwarding agent logs to the session that produced them."""
    record = message.record
    session = SESSIONS.get(record["extra"].get("web_session"))
    if session is None:
        return
    session.publish(
        "log",
        level=record["level"].name,
        message=record["message"],
        name=record["name"],
    )


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
    }


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sink_id = logger.add(
            _log_sink,
            level="INFO",
            filter=lambda record: "web_session" in record["extra"],
        )
        logger.info("OpenManus web interface ready")
        try:
            yield
        finally:
            for session in list(SESSIONS.values()):
                await session.cleanup()
            SESSIONS.clear()
            logger.remove(sink_id)

    app = FastAPI(title="OpenManus Web", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def read_config() -> Dict[str, Any]:
        return {
            "llm": _llm_info(),
            "workspace": str(config.workspace_root),
        }

    @app.get("/api/sessions")
    async def list_sessions() -> Dict[str, List[Dict[str, Any]]]:
        return {"sessions": [s.info() for s in SESSIONS.values()]}

    @app.post("/api/sessions")
    async def create_session() -> Dict[str, Any]:
        session = Session(uuid.uuid4().hex[:12])
        SESSIONS[session.id] = session
        return session.info()

    @app.get("/api/sessions/{session_id}")
    async def read_session(session_id: str) -> Dict[str, Any]:
        return _get_session(session_id).info()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> Dict[str, str]:
        session = _get_session(session_id)
        await session.cleanup()
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

    @app.get("/api/workspace")
    async def workspace() -> Dict[str, List[Dict[str, Any]]]:
        root = Path(config.workspace_root)
        files = []
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    stat = path.stat()
                    files.append(
                        {
                            "path": str(path.relative_to(root)),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        }
                    )
        files.sort(key=lambda item: item["modified"], reverse=True)
        return {"files": files[:200]}

    @app.get("/api/workspace/file")
    async def workspace_file(path: str) -> FileResponse:
        root = Path(config.workspace_root).resolve()
        target = (root / path).resolve()
        if not target.is_file() or root not in target.parents:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(target, filename=target.name)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
