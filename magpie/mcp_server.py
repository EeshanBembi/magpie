"""MCP stdio server exposing Magpie's memory as tools.

Run with ``magpie mcp``.  The server speaks stdio so any MCP client (Claude
Desktop, opencode, …) can search the local screen memory, read timelines and
journals, list open loops and ask questions against the same SQLite database
the UI uses.  No data leaves the machine unless the configured LLM is a cloud
provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# The SPEC calls this FastMCP.  The installed mcp 2.x SDK renamed it to
# MCPServer (importing mcp.server.fastmcp raises); fall back so either major
# version of the SDK works.
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _FastMCP
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _FastMCP

from . import config as config_module
from . import db as db_module
from . import helper as helper_module
from . import journal as journal_module
from . import llm as llm_module
from . import rag as rag_module

FastMCP = _FastMCP

SERVER_NAME = "magpie"
INSTRUCTIONS = (
    "Magpie is a private, local-first memory assistant. It records what is on "
    "the user's screen (redacted and stored in local SQLite) so you can "
    "search it, read daily journals, list open loops and answer questions "
    "about what the user saw or worked on."
)

mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)


def _db() -> db_module.Database:
    return db_module.get_default_db()


def _config() -> dict[str, Any]:
    return config_module.load_config()


def _provider() -> Any:
    return llm_module.get_provider(_config())


@mcp.tool()
def search_memory(
    query: str,
    start: float | None = None,
    end: float | None = None,
    app: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Full-text search across everything Magpie saw on screen.

    ``query`` is plain text (operators are not needed); ``start``/``end`` are
    optional POSIX timestamps and ``app`` filters by application name.
    Returns matching captures with a highlighted snippet and bm25 rank.
    """

    results = _db().search(
        query,
        start_ts=start,
        end_ts=end,
        app=app or None,
        limit=max(0, int(limit)),
    )
    return {"query": query, "count": len(results), "results": results}


@mcp.tool()
def get_timeline(day: str) -> dict[str, Any]:
    """Activities and per-app usage for one day (``YYYY-MM-DD``).

    Activities are contiguous spans in one app/window with start/end
    timestamps and durations; app usage is total seconds per app.
    """

    database = _db()
    return {
        "day": day,
        "activities": database.timeline(day),
        "app_usage": database.app_usage(day),
    }


@mcp.tool()
def get_journal(day: str) -> dict[str, Any]:
    """The markdown journal for a day (``YYYY-MM-DD``), generated if needed.

    Contains the sections Summary, What you worked on, People & conversations
    and Open loops, written only from that day's screen memories.
    """

    return journal_module.generate_journal(day, _config(), _db(), provider=_provider())


@mcp.tool()
def list_open_loops() -> dict[str, Any]:
    """Every open loop (follow-up, todo or promise) seen on screen."""

    rows = _db().conn.execute(
        "SELECT * FROM loops WHERE status = 'open' "
        "ORDER BY created_ts DESC, id DESC"
    ).fetchall()
    loops = [dict(row) for row in rows]
    return {"loops": loops, "count": len(loops)}


@mcp.tool()
def ask_memory(question: str) -> dict[str, Any]:
    """Ask a question about anything the user saw on screen.

    Parses natural time ranges ("this morning", "yesterday", "last week"),
    retrieves the relevant screen memories and answers with numbered
    citations; each citation is a capture the UI can open.
    """

    return rag_module.answer(question, None, _config(), _db(), provider=_provider())


@mcp.tool()
def create_reminder(
    title: str, due: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Create an Apple Reminders item, optionally with a due date and notes.

    ``due`` is an ISO-8601 datetime.  Returns the new reminder id.
    """

    result = helper_module.add_reminder(title, due=due, notes=notes)
    return {
        "ok": True,
        "id": result.get("id") if isinstance(result, dict) else None,
    }


@mcp.tool()
def create_calendar_event(
    title: str, start: str, end: str, notes: str | None = None
) -> dict[str, Any]:
    """Create a Calendar event between two ISO-8601 datetimes.

    Returns the new event id.
    """

    result = helper_module.add_event(title, start, end, notes=notes)
    return {
        "ok": True,
        "id": result.get("id") if isinstance(result, dict) else None,
    }


@mcp.tool()
def draft_email(to: str = "", subject: str = "", body: str = "") -> dict[str, Any]:
    """Open a visible draft in Mail.app.  The message is never sent."""

    from . import server as server_module

    script = server_module.build_email_applescript(to, subject, body)
    server_module.run_applescript(script)
    return {"ok": True, "draft": True, "sent": False, "to": to, "subject": subject}


@mcp.tool()
def get_calendar(day: str | None = None) -> dict[str, Any]:
    """Calendar events for a day (``YYYY-MM-DD``, default today)."""

    target = day or datetime.now().strftime("%Y-%m-%d")
    start_ts, end_ts = db_module.day_bounds(target)
    start_iso = datetime.fromtimestamp(start_ts).isoformat(timespec="seconds")
    end_iso = datetime.fromtimestamp(end_ts).isoformat(timespec="seconds")
    events = helper_module.calendar_events(start_iso, end_iso)
    return {"day": target, "events": events, "count": len(events)}


def main() -> None:
    """Run the MCP server over stdio (``magpie mcp``)."""

    mcp.run("stdio")


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    main()
