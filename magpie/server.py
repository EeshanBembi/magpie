"""FastAPI application: the Magpie HTTP API and the web UI.

Binds to ``127.0.0.1`` only (default port 7777).  On startup the capture loop
begins on a background thread (unless disabled) and yesterday's journal is
generated once in the background.  The API is the single source of truth for
the UI in ``magpie/static`` and for the CLI's ``pause``/``resume`` commands.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from . import config as config_module
from . import db as db_module
from . import helper as helper_module
from . import journal as journal_module
from . import llm as llm_module
from . import meetings as meetings_module
from . import notion as notion_module
from . import rag as rag_module
from . import remote as remote_module
from .capture import CaptureLoop

DEFAULT_PORT = 7777
MAX_CHAT_HISTORY_MESSAGES = 20

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ---------------------------------------------------------------------------
# request/response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    chat_id: int | None = None
    message: str


class LoopUpdate(BaseModel):
    status: str


class MeetingStart(BaseModel):
    title: str = ""


class EmailDraft(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""


class ReminderRequest(BaseModel):
    title: str
    due: str | None = None
    notes: str | None = None


class EventRequest(BaseModel):
    title: str
    start: str | None = None
    end: str | None = None
    notes: str | None = None


class NotionRequest(BaseModel):
    title: str
    markdown: str = ""
    content: str = ""  # backwards-compatible alias for markdown


# Settings keys that are secrets and must be masked like provider API keys.
SECRET_SETTINGS = ("notion_token", "remote_token")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _local_day(timestamp: float | None = None) -> str:
    moment = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d")


def _parse_time(value: Any, *, end: bool = False) -> float | None:
    """Accept a POSIX timestamp, ``YYYY-MM-DD`` or ISO datetime string."""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        if _DAY_RE.match(text):
            start_ts, end_ts = db_module.day_bounds(text)
            return end_ts if end else start_ts
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid time value {value!r}; use a timestamp, "
            "YYYY-MM-DD or ISO datetime",
        ) from exc


def mask_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return settings safe for the UI: API keys become ``{"set": bool}``."""

    data = copy.deepcopy(cfg)
    providers = data.get("providers")
    if isinstance(providers, dict):
        for profile in providers.values():
            if not isinstance(profile, dict):
                continue
            if "api_key" in profile:
                profile["api_key"] = {"set": bool(profile.get("api_key"))}
    for key in SECRET_SETTINGS:
        if key in data:
            data[key] = {"set": bool(data.get(key))}
    return data


def _strip_masked_secrets(body: Any) -> dict[str, Any]:
    """Drop secret fields that arrived masked so a PUT never erases a key."""

    cleaned = copy.deepcopy(body) if isinstance(body, dict) else {}
    providers = cleaned.get("providers")
    if isinstance(providers, dict):
        for profile in providers.values():
            if not isinstance(profile, dict):
                continue
            value = profile.get("api_key")
            if isinstance(value, dict) or value == "set":
                profile.pop("api_key", None)
    for key in SECRET_SETTINGS:
        value = cleaned.get(key)
        if isinstance(value, dict) or value == "set":
            cleaned.pop(key, None)
    return cleaned


# ---------------------------------------------------------------------------
# chat persistence
# ---------------------------------------------------------------------------


def _chat_title(message: str) -> str:
    title = " ".join((message or "").split())
    if len(title) > 60:
        title = title[:57].rstrip() + "…"
    return title or "New chat"


def _create_chat(database: db_module.Database, title: str) -> int:
    with database.conn:
        cursor = database.conn.execute(
            "INSERT INTO chats (created_ts, title) VALUES (?, ?)",
            (time.time(), title),
        )
    return int(cursor.lastrowid)


def _get_chat(database: db_module.Database, chat_id: int) -> dict[str, Any] | None:
    row = database.conn.execute(
        "SELECT * FROM chats WHERE id = ?", (int(chat_id),)
    ).fetchone()
    return dict(row) if row is not None else None


def _list_chats(database: db_module.Database, limit: int = 100) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        "SELECT * FROM chats ORDER BY created_ts DESC, id DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _add_message(
    database: db_module.Database,
    chat_id: int,
    role: str,
    content: str,
    citations: list[dict[str, Any]] | None = None,
) -> int:
    with database.conn:
        cursor = database.conn.execute(
            """
            INSERT INTO messages (chat_id, role, content, citations_json, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(chat_id),
                role,
                content,
                json.dumps(citations or [], ensure_ascii=False),
                time.time(),
            ),
        )
    return int(cursor.lastrowid)


def _chat_messages(
    database: db_module.Database, chat_id: int
) -> list[dict[str, Any]]:
    rows = database.conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (int(chat_id),),
    ).fetchall()
    messages: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["citations"] = json.loads(item.get("citations_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            item["citations"] = []
        messages.append(item)
    return messages


# ---------------------------------------------------------------------------
# e-mail drafts (Mail.app, never sends)
# ---------------------------------------------------------------------------


def _applescript_string(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_email_applescript(to: str, subject: str, body: str) -> str:
    """AppleScript that opens a *visible* Mail.app draft.  It never sends."""

    lines = [
        "tell application " + _applescript_string("Mail"),
        "    activate",
        "    set newMessage to make new outgoing message with properties "
        "{subject:"
        + _applescript_string(subject)
        + ", content:"
        + _applescript_string(body)
        + ", visible:true}",
    ]
    if (to or "").strip():
        lines.extend(
            [
                "    tell newMessage",
                "        make new to recipient with properties {address:"
                + _applescript_string(to.strip())
                + "}",
                "    end tell",
            ]
        )
    lines.append("end tell")
    return "\n".join(lines)


def run_applescript(script: str, timeout: float = 30.0) -> None:
    """Execute AppleScript via ``osascript``; raise RuntimeError on failure."""

    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - macOS always ships it
        raise RuntimeError("osascript is not available on this system") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"osascript timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run osascript: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"osascript failed ({completed.returncode})")


# ---------------------------------------------------------------------------
# application context
# ---------------------------------------------------------------------------


class AppContext:
    """Everything the request handlers need, in one place."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db: db_module.Database | None = None,
        *,
        provider: Any | None = None,
        screenshot_source: Any | None = None,
        helper: Any | None = None,
        recorder_factory: Callable[[], Any] | None = None,
        transcriber: Any | None = None,
        email_runner: Callable[[str], Any] | None = None,
        static_dir: str | Path | None = None,
        notion_transport: httpx.BaseTransport | None = None,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.config = config if config is not None else config_module.load_config()
        self.db = db if db is not None else db_module.get_default_db()
        self._injected_provider = provider
        self.helper = helper if helper is not None else helper_module
        self.loop = CaptureLoop(
            self.config,
            self.db,
            screenshot_source=screenshot_source,
            helper=self.helper,
        )
        self.recorder_factory = recorder_factory
        self.transcriber = transcriber
        self.email_runner = email_runner or (
            lambda script: run_applescript(script)
        )
        self.static_dir = (
            Path(static_dir)
            if static_dir is not None
            else Path(__file__).resolve().parent / "static"
        )
        self.notion_transport = notion_transport
        self.port = int(port)
        self.pair_code = remote_module.generate_pair_code()
        if self.config.get("remote_access"):
            # A token must exist from the first request; persist it so paired
            # phones survive a restart.
            if not str(self.config.get("remote_token") or "").strip():
                self.config["remote_token"] = remote_module.generate_token()
                try:
                    saved = config_module.save_config(self.config)
                    self.config = saved
                except Exception:
                    pass
        self.active_recorders: dict[int, Any] = {}
        self.journal_thread: Any | None = None

    def provider(self) -> Any:
        """The injected provider, or one built from the *current* config."""

        if self._injected_provider is not None:
            return self._injected_provider
        return llm_module.get_provider(self.config)

    def update_config(self, new_config: dict[str, Any]) -> None:
        self.config = new_config
        self.loop.config = new_config

    # -- status ---------------------------------------------------------

    def provider_status(self) -> dict[str, Any]:
        name = str(self.config.get("provider") or "ollama")
        profile = self.config.get("providers", {}).get(name, {})
        model = ""
        if isinstance(profile, dict):
            model = str(profile.get("model") or "")
        status: dict[str, Any] = {
            "name": name,
            "model": model or None,
            "reachable": False,
            "error": None,
        }
        if name == "fake" or self._injected_provider is not None:
            status["reachable"] = True
            return status
        base_url = ""
        if isinstance(profile, dict):
            base_url = str(
                profile.get("base_url")
                or llm_module.DEFAULT_BASE_URLS.get(name, "")
            )
        if not base_url:
            status["error"] = f"no base URL configured for provider {name!r}"
            return status
        try:
            httpx.get(base_url.rstrip("/") + "/", timeout=2.0)
            status["reachable"] = True
        except Exception as exc:
            status["error"] = f"could not reach {name} at {base_url}: {exc}"
        return status

    def permission_status(self) -> dict[str, Any]:
        try:
            payload = self.helper.permissions()
            granted: bool | None = bool(payload.get("screen_recording"))
        except Exception:
            granted = None
        return {"screen_recording": granted}

    def meeting_detected(self) -> bool:
        result = self.loop.last_result or {}
        return meetings_module.meeting_detected(
            {
                "app": result.get("app") or "",
                "bundle_id": result.get("bundle_id") or "",
                "window_title": result.get("window_title") or "",
            }
        )

    def helper_recording_available(self) -> bool:
        probe = getattr(self.helper, "supports_record_audio", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def calendar_events_for_day(
        self, day: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Events for ``day`` from the helper, or an error string.

        Never raises: when the helper is missing, lacks the subcommand, or
        EventKit permission is denied, the caller gets an empty list plus a
        human-readable reason so journals and the UI can degrade gracefully.
        """

        try:
            start_ts, end_ts = db_module.day_bounds(day)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fetch = getattr(self.helper, "calendar_events", None)
        if not callable(fetch):
            return [], (
                "calendar needs the Magpie helper with calendar-events "
                "support; rebuild it with: swift build -c release "
                "--package-path helper"
            )
        start_iso = datetime.fromtimestamp(start_ts).isoformat(timespec="seconds")
        end_iso = datetime.fromtimestamp(end_ts).isoformat(timespec="seconds")
        try:
            events = fetch(start_iso, end_iso)
        except helper_module.HelperError as exc:
            return [], str(exc)
        except Exception as exc:
            return [], f"could not read calendar events: {exc}"
        return [dict(item) for item in events if isinstance(item, dict)], None

    def remote_state(self) -> dict[str, Any]:
        return remote_module.remote_state(
            self.config, self.pair_code, port=self.port
        )

    def status(self) -> dict[str, Any]:
        loop_status = self.loop.status()
        return {
            "capturing": loop_status["capturing"],
            "paused": loop_status["paused"],
            "running": loop_status["running"],
            "interval_seconds": loop_status["interval_seconds"],
            "last_capture_ts": loop_status["last_capture_ts"],
            "captures": loop_status["captures"],
            "dedupes": loop_status["dedupes"],
            "counts": self.db.stats(),
            "permission": self.permission_status(),
            "permission_problem": loop_status["permission_problem"],
            "provider": self.provider_status(),
            "meeting_detected": self.meeting_detected(),
            "helper_recording": self.helper_recording_available(),
            "remote_access": bool(self.config.get("remote_access")),
            "last_error": loop_status["last_error"],
            "time": time.time(),
        }


# ---------------------------------------------------------------------------
# application factory
# ---------------------------------------------------------------------------


def create_app(
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    capture_enabled: bool = True,
    provider: Any | None = None,
    screenshot_source: Any | None = None,
    helper: Any | None = None,
    recorder_factory: Callable[[], Any] | None = None,
    transcriber: Any | None = None,
    email_runner: Callable[[str], Any] | None = None,
    static_dir: str | Path | None = None,
    notion_transport: httpx.BaseTransport | None = None,
    port: int = DEFAULT_PORT,
    auto_journal: bool = True,
) -> FastAPI:
    """Build the FastAPI app.  Everything hardware/network-facing injectable."""

    ctx = AppContext(
        config,
        db,
        provider=provider,
        screenshot_source=screenshot_source,
        helper=helper,
        recorder_factory=recorder_factory,
        transcriber=transcriber,
        email_runner=email_runner,
        static_dir=static_dir,
        notion_transport=notion_transport,
        port=port,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        if capture_enabled:
            ctx.loop.start()
        if auto_journal:
            ctx.journal_thread = journal_module.start_auto_journal(
                ctx.config, ctx.db, provider=ctx._injected_provider
            )
        yield
        ctx.loop.stop()

    app = FastAPI(title="Magpie", version="0.1.0", lifespan=lifespan)
    app.state.ctx = ctx

    # -- remote access (16c): token/cookie auth for non-loopback requests -----

    @app.middleware("http")
    async def remote_auth_middleware(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path == "/pair" or not ctx.config.get("remote_access"):
            return await call_next(request)
        client_host = request.client.host if request.client else ""
        if remote_module.is_loopback(client_host):
            return await call_next(request)
        token = str(ctx.config.get("remote_token") or "")
        if token:
            authorization = request.headers.get("authorization") or ""
            if authorization == f"Bearer {token}":
                return await call_next(request)
            if request.cookies.get(remote_module.COOKIE_NAME) == token:
                return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={
                "detail": "authentication required; pair this device from "
                "Magpie Settings → Phone access"
            },
        )

    @app.get("/pair", include_in_schema=False)
    def pair(code: str = "") -> Any:
        if not ctx.config.get("remote_access"):
            return JSONResponse(
                status_code=400,
                content={"detail": "remote access is turned off"},
            )
        if not code or code != ctx.pair_code:
            return JSONResponse(
                status_code=403,
                content={"detail": "invalid pairing code"},
            )
        token = str(ctx.config.get("remote_token") or "")
        if not token:
            token = remote_module.ensure_token(ctx.config)
            try:
                ctx.update_config(config_module.save_config(ctx.config))
            except Exception:
                pass
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            remote_module.COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=180 * 86400,
        )
        return response

    # -- status / control ------------------------------------------------

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return ctx.status()

    @app.post("/api/pause")
    def api_pause() -> dict[str, Any]:
        ctx.loop.pause()
        return {"ok": True, "paused": True}

    @app.post("/api/resume")
    def api_resume() -> dict[str, Any]:
        ctx.loop.resume()
        return {"ok": True, "paused": False}

    @app.post("/api/capture-now")
    def api_capture_now() -> dict[str, Any]:
        return ctx.loop.capture_once(force=True)

    # -- search / captures -----------------------------------------------

    @app.get("/api/search")
    def api_search(
        q: str = "",
        start: str | None = None,
        end: str | None = None,
        app: str | None = None,
        limit: int = Query(default=20, ge=0, le=500),
    ) -> dict[str, Any]:
        start_ts = _parse_time(start)
        end_ts = _parse_time(end, end=True)
        results = ctx.db.search(
            q, start_ts=start_ts, end_ts=end_ts, app=app or None, limit=limit
        )
        return {
            "query": q,
            "start": start_ts,
            "end": end_ts,
            "app": app,
            "count": len(results),
            "results": results,
        }

    @app.get("/api/captures/{capture_id}")
    def api_capture(capture_id: int) -> dict[str, Any]:
        capture = ctx.db.get_capture(capture_id)
        if capture is None:
            raise HTTPException(status_code=404, detail="capture not found")
        return capture

    @app.get("/api/captures/{capture_id}/image")
    def api_capture_image(capture_id: int) -> FileResponse:
        capture = ctx.db.get_capture(capture_id)
        if capture is None or not capture.get("image_path"):
            raise HTTPException(status_code=404, detail="image not found")
        path = Path(str(capture["image_path"]))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="image file is missing")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/timeline")
    def api_timeline(day: str | None = None) -> dict[str, Any]:
        target = day or _local_day()
        try:
            activities = ctx.db.timeline(target)
            usage = ctx.db.app_usage(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"day": target, "activities": activities, "app_usage": usage}

    # -- chat -------------------------------------------------------------

    @app.post("/api/chat")
    def api_chat(body: ChatRequest) -> StreamingResponse:
        message = (body.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        history: list[dict[str, str]] = []
        if body.chat_id is not None:
            if _get_chat(ctx.db, body.chat_id) is None:
                raise HTTPException(status_code=404, detail="chat not found")
            for item in _chat_messages(ctx.db, body.chat_id)[
                -MAX_CHAT_HISTORY_MESSAGES:
            ]:
                history.append({"role": item["role"], "content": item["content"]})

        def events() -> Iterator[str]:
            citations: list[dict[str, Any]] = []
            parts: list[str] = []
            try:
                stream = rag_module.answer_stream(
                    message,
                    history,
                    ctx.config,
                    ctx.db,
                    provider=ctx.provider(),
                )
                for chunk in stream:
                    if isinstance(chunk, str):
                        if not chunk:
                            continue
                        parts.append(chunk)
                        yield "data: " + json.dumps(
                            {"delta": chunk}, ensure_ascii=False
                        ) + "\n\n"
                    elif isinstance(chunk, dict):
                        citations = list(chunk.get("citations") or [])
            except Exception as exc:
                yield "data: " + json.dumps(
                    {"error": str(exc)}, ensure_ascii=False
                ) + "\n\n"

            chat_id = body.chat_id
            if chat_id is None:
                chat_id = _create_chat(ctx.db, _chat_title(message))
            answer_text = "".join(parts)
            _add_message(ctx.db, chat_id, "user", message)
            _add_message(ctx.db, chat_id, "assistant", answer_text, citations)
            yield "data: " + json.dumps(
                {"done": True, "citations": citations, "chat_id": chat_id},
                ensure_ascii=False,
            ) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/chats")
    def api_chats() -> dict[str, Any]:
        chats = _list_chats(ctx.db)
        return {"chats": chats, "count": len(chats)}

    @app.get("/api/chats/{chat_id}")
    def api_chat_detail(chat_id: int) -> dict[str, Any]:
        chat = _get_chat(ctx.db, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="chat not found")
        return {"chat": chat, "messages": _chat_messages(ctx.db, chat_id)}

    # -- journal / loops ---------------------------------------------------

    @app.get("/api/journal")
    def api_journal(day: str | None = None) -> dict[str, Any]:
        target = day or _local_day()
        events, calendar_error = ctx.calendar_events_for_day(target)
        try:
            result = journal_module.generate_journal(
                target, ctx.config, ctx.db,
                provider=ctx.provider(), calendar_events=events,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if calendar_error:
            result["calendar_error"] = calendar_error
        return result

    @app.post("/api/journal/regenerate")
    def api_journal_regenerate(day: str | None = None) -> dict[str, Any]:
        target = day or _local_day()
        events, calendar_error = ctx.calendar_events_for_day(target)
        try:
            result = journal_module.generate_journal(
                target, ctx.config, ctx.db,
                provider=ctx.provider(), regenerate=True,
                calendar_events=events,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if calendar_error:
            result["calendar_error"] = calendar_error
        return result

    @app.get("/api/loops")
    def api_loops(
        status: str = "open", day: str | None = None
    ) -> dict[str, Any]:
        sql = "SELECT * FROM loops WHERE status = ?"
        params: list[Any] = [status]
        if day:
            sql += " AND day = ?"
            params.append(day)
        sql += " ORDER BY created_ts DESC, id DESC"
        rows = ctx.db.conn.execute(sql, params).fetchall()
        loops = [dict(row) for row in rows]
        return {"loops": loops, "count": len(loops)}

    # NOTE: registered before /api/loops/{loop_id} so "extract" is not
    # swallowed by the path parameter.
    @app.post("/api/loops/extract")
    def api_loops_extract(day: str | None = None) -> dict[str, Any]:
        target = day or _local_day()
        try:
            inserted = journal_module.extract_loops(
                target, ctx.config, ctx.db, provider=ctx.provider()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"day": target, "loops": inserted, "count": len(inserted)}

    @app.post("/api/loops/{loop_id}")
    def api_loop_update(loop_id: int, body: LoopUpdate) -> dict[str, Any]:
        if body.status not in ("open", "done", "dismissed"):
            raise HTTPException(
                status_code=400,
                detail="status must be one of open, done, dismissed",
            )
        with ctx.db.conn:
            cursor = ctx.db.conn.execute(
                "UPDATE loops SET status = ? WHERE id = ?",
                (body.status, int(loop_id)),
            )
        if cursor.rowcount <= 0:
            raise HTTPException(status_code=404, detail="loop not found")
        row = ctx.db.conn.execute(
            "SELECT * FROM loops WHERE id = ?", (int(loop_id),)
        ).fetchone()
        return {"ok": True, "loop": dict(row)}

    # -- meetings -----------------------------------------------------------

    @app.post("/api/meetings/start")
    def api_meeting_start(body: MeetingStart | None = None) -> dict[str, Any]:
        title = (body.title if body else "") or ""
        if ctx.recorder_factory is not None:
            recorder = ctx.recorder_factory()
        else:
            recorder = meetings_module.create_recorder(
                ctx.config, helper=ctx.helper
            )
        meeting = meetings_module.start_meeting(
            title, ctx.config, ctx.db, recorder=recorder, helper=ctx.helper
        )
        if meeting.get("status") == "recording":
            ctx.active_recorders[int(meeting["id"])] = recorder
        return meeting

    @app.post("/api/meetings/{meeting_id}/stop")
    def api_meeting_stop(meeting_id: int) -> dict[str, Any]:
        recorder = ctx.active_recorders.pop(int(meeting_id), None)
        try:
            return meetings_module.stop_meeting(
                meeting_id,
                ctx.config,
                ctx.db,
                recorder=recorder,
                transcriber=ctx.transcriber,
                provider=ctx.provider(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/meetings")
    def api_meetings() -> dict[str, Any]:
        meetings = meetings_module.list_meetings(ctx.db)
        return {"meetings": meetings, "count": len(meetings)}

    @app.get("/api/meetings/{meeting_id}")
    def api_meeting(meeting_id: int) -> dict[str, Any]:
        meeting = meetings_module.get_meeting(meeting_id, ctx.db)
        if meeting is None:
            raise HTTPException(status_code=404, detail="meeting not found")
        return meeting

    # -- settings / data ------------------------------------------------------

    @app.get("/api/settings")
    def api_settings() -> dict[str, Any]:
        return mask_settings(ctx.config)

    @app.put("/api/settings")
    async def api_settings_update(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail="settings must be a JSON object"
            )
        cleaned = _strip_masked_secrets(body)
        merged = config_module._deep_merge(ctx.config, cleaned)
        saved = config_module.save_config(merged)
        ctx.update_config(saved)
        return mask_settings(saved)

    @app.delete("/api/data")
    def api_delete_data(
        start: str | None = None, end: str | None = None
    ) -> dict[str, Any]:
        start_ts = _parse_time(start)
        end_ts = _parse_time(end, end=True)
        if start_ts is None or end_ts is None:
            raise HTTPException(
                status_code=400, detail="both start and end are required"
            )
        result = ctx.db.delete_range(start_ts, end_ts)
        return {"ok": True, "deleted": result}

    @app.get("/api/export")
    def api_export() -> dict[str, Any]:
        return ctx.db.export_all()

    # -- notifications / actions (16d) -----------------------------------------

    @app.get("/api/notifications")
    def api_notifications() -> dict[str, Any]:
        today = _local_day()
        notifications: list[dict[str, Any]] = []

        rows = ctx.db.conn.execute(
            "SELECT * FROM loops WHERE day = ? AND status = 'open' "
            "ORDER BY created_ts DESC, id DESC",
            (today,),
        ).fetchall()
        for row in rows:
            loop = dict(row)
            notifications.append(
                {
                    "id": f"loop-{loop['id']}",
                    "kind": "loop",
                    "title": "Open loop",
                    "body": loop["text"],
                    "loop_id": loop["id"],
                    "day": loop["day"],
                    "created_ts": loop["created_ts"],
                }
            )

        detected = ctx.meeting_detected()
        if detected:
            notifications.append(
                {
                    "id": "meeting-detected",
                    "kind": "meeting_detected",
                    "title": "Meeting detected",
                    "body": "Record this meeting?",
                    "created_ts": time.time(),
                }
            )

        previous = _local_day(time.time() - 86400.0)
        journal = journal_module.get_journal(previous, ctx.db)
        if journal is not None and journal.get("summary"):
            notifications.append(
                {
                    "id": f"journal-{previous}",
                    "kind": "journal_ready",
                    "title": "Journal ready",
                    "body": f"Your journal for {previous} is ready",
                    "day": previous,
                    "created_ts": journal.get("created_ts"),
                }
            )

        notifications.sort(
            key=lambda item: float(item.get("created_ts") or 0.0), reverse=True
        )
        return {
            "notifications": notifications,
            "count": len(notifications),
            "meeting_detected": detected,
        }

    @app.post("/api/actions/email")
    def api_action_email(body: EmailDraft) -> dict[str, Any]:
        script = build_email_applescript(body.to, body.subject, body.body)
        try:
            ctx.email_runner(script)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"could not create the Mail draft: {exc}",
            ) from exc
        return {
            "ok": True,
            "draft": True,
            "sent": False,
            "to": body.to,
            "subject": body.subject,
        }

    def _helper_action(name: str) -> Callable[..., Any]:
        fn = getattr(ctx.helper, name, None)
        if not callable(fn):
            raise HTTPException(
                status_code=501,
                detail=(
                    f"the Magpie helper does not support {name}; rebuild it "
                    "with: swift build -c release --package-path helper"
                ),
            )
        return fn

    @app.post("/api/actions/reminder")
    def api_action_reminder(body: ReminderRequest) -> dict[str, Any]:
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        add = _helper_action("add_reminder")
        try:
            result = add(title, due=body.due, notes=body.notes)
        except helper_module.HelperError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"could not create the reminder: {exc}"
            ) from exc
        return {"ok": True, "id": result.get("id") if isinstance(result, dict) else None}

    @app.post("/api/actions/event")
    def api_action_event(body: EventRequest) -> dict[str, Any]:
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title is required")
        start_ts = _parse_time(body.start)
        end_ts = _parse_time(body.end)
        if start_ts is None or end_ts is None:
            raise HTTPException(
                status_code=400,
                detail="start and end are required (ISO datetimes)",
            )
        if end_ts <= start_ts:
            raise HTTPException(
                status_code=400, detail="end must be after start"
            )
        add = _helper_action("add_event")
        try:
            result = add(title, str(body.start), str(body.end), notes=body.notes)
        except helper_module.HelperError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"could not create the event: {exc}"
            ) from exc
        return {"ok": True, "id": result.get("id") if isinstance(result, dict) else None}

    @app.post("/api/actions/notion")
    def api_action_notion(body: NotionRequest) -> dict[str, Any]:
        markdown = body.markdown or body.content
        if not markdown.strip():
            raise HTTPException(status_code=400, detail="markdown is required")
        try:
            page = notion_module.create_page(
                ctx.config,
                body.title or "Magpie note",
                markdown,
                transport=ctx.notion_transport,
            )
        except notion_module.NotionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "id": page.get("id"), "url": page.get("url")}

    @app.get("/api/calendar")
    def api_calendar(day: str | None = None) -> dict[str, Any]:
        target = day or _local_day()
        events, error = ctx.calendar_events_for_day(target)
        if error is not None and not events:
            raise HTTPException(status_code=502, detail=error)
        return {"day": target, "events": events, "count": len(events)}

    @app.get("/api/remote")
    def api_remote() -> dict[str, Any]:
        return ctx.remote_state()

    @app.post("/api/remote/rotate")
    def api_remote_rotate() -> dict[str, Any]:
        rotated = remote_module.rotate(ctx.config)
        ctx.pair_code = rotated["pair_code"]
        try:
            ctx.update_config(config_module.save_config(ctx.config))
        except config_module.ConfigError as exc:
            raise HTTPException(
                status_code=500, detail=f"could not save the new token: {exc}"
            ) from exc
        return ctx.remote_state()

    # -- web UI (static files with index.html fallback) ------------------------

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_ui(full_path: str) -> Any:
        if full_path.startswith("api"):
            return JSONResponse(
                status_code=404, content={"detail": "not found"}
            )
        root = ctx.static_dir
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            root_resolved = root.absolute()
        target = (root_resolved / full_path).resolve(strict=False)
        if full_path and target.is_file() and str(target).startswith(
            str(root_resolved)
        ):
            return FileResponse(target)
        index = root_resolved / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Magpie UI is not built yet "
                f"(missing {index}); the API is available under /api/"
            },
        )

    return app


def run(
    port: int = DEFAULT_PORT,
    *,
    capture: bool = True,
    config: dict[str, Any] | None = None,
    remote: bool = False,
) -> None:
    """Run the server with uvicorn.

    Binds to 127.0.0.1 by default; with ``--remote`` or the ``remote_access``
    setting it also binds 0.0.0.0 so phones on the LAN (or Tailscale) can
    reach it — non-loopback requests then need the bearer token or cookie.
    """

    import uvicorn

    cfg = config if config is not None else config_module.load_config()
    remote_enabled = bool(remote) or bool(cfg.get("remote_access"))
    if remote_enabled and not cfg.get("remote_access"):
        # --remote on the CLI turns the setting on for this run (and keeps
        # the UI toggle in sync).
        cfg = config_module._deep_merge(cfg, {"remote_access": True})
    host = "0.0.0.0" if remote_enabled else "127.0.0.1"
    app = create_app(config=cfg, capture_enabled=capture, port=int(port))
    uvicorn.run(app, host=host, port=int(port), log_level="info")
