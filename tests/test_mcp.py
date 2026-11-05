from __future__ import annotations

import time
from datetime import datetime

import pytest

from magpie import config, mcp_server
from magpie import db as db_module

DAY = "2025-03-10"
T0 = datetime(2025, 3, 10, 9, 30).timestamp()
T1 = datetime(2025, 3, 10, 11, 0).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    cfg = config.defaults()
    cfg["provider"] = "fake"
    config.save_config(cfg)
    database = db_module.get_default_db()
    try:
        yield {"db": database, "tmp": tmp_path}
    finally:
        db_module.close_default_dbs()


def seed(env):
    env["db"].insert_capture(
        T0,
        "Slack",
        "com.tinyspeck.slackmacgap",
        "Priya Singh - DM",
        "Priya: can you send the updated contract by noon?",
    )
    env["db"].insert_capture(
        T1,
        "Safari",
        "com.apple.Safari",
        "Budget",
        "The quarterly budget spreadsheet is ready.",
    )


def test_all_tools_registered():
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert names == {
        "search_memory",
        "get_timeline",
        "get_journal",
        "list_open_loops",
        "ask_memory",
        "create_reminder",
        "create_calendar_event",
        "draft_email",
        "get_calendar",
    }


def test_search_memory(env):
    seed(env)
    result = mcp_server.search_memory("contract")
    assert result["count"] == 1
    row = result["results"][0]
    assert row["app"] == "Slack"
    assert "contract" in row["snippet"].lower()


def test_search_memory_collapses_near_duplicates(env):
    text = "Priya: can you send the updated contract by noon?"
    env["db"].insert_capture(T0, "Slack", "com.tinyspeck", "Priya DM", text)
    env["db"].insert_capture(
        T0 + 4, "Slack", "com.tinyspeck", "Priya DM", text + " Thanks!"
    )

    result = mcp_server.search_memory("contract")

    assert result["count"] == 1
    assert result["results"][0]["similar_count"] == 2


def test_search_memory_with_filters(env):
    seed(env)
    result = mcp_server.search_memory(
        "contract OR budget", start=T1 - 60, end=T1 + 60
    )
    assert result["count"] == 1
    assert result["results"][0]["app"] == "Safari"
    assert mcp_server.search_memory("budget", app="Slack")["count"] == 0
    assert mcp_server.search_memory("budget", limit=1)["count"] == 1


def test_get_timeline(env):
    seed(env)
    timeline = mcp_server.get_timeline(DAY)
    assert timeline["day"] == DAY
    assert len(timeline["activities"]) == 2
    usage = {item["app"]: item["seconds"] for item in timeline["app_usage"]}
    assert set(usage) == {"Slack", "Safari"}


def test_get_journal(env):
    seed(env)
    journal = mcp_server.get_journal(DAY)
    assert journal["day"] == DAY
    assert journal["summary"]
    assert journal["model"] == "fake"


def test_list_open_loops(env):
    with env["db"].conn:
        env["db"].conn.execute(
            "INSERT INTO loops (text, source_capture_id, day, status, created_ts)"
            " VALUES (?, NULL, ?, 'open', ?)",
            ("Reply to Sam about the contract", DAY, time.time()),
        )
        env["db"].conn.execute(
            "INSERT INTO loops (text, source_capture_id, day, status, created_ts)"
            " VALUES (?, NULL, ?, 'done', ?)",
            ("Old loop", DAY, time.time()),
        )
    result = mcp_server.list_open_loops()
    assert result["count"] == 1
    assert result["loops"][0]["text"] == "Reply to Sam about the contract"


def test_ask_memory(env):
    seed(env)
    result = mcp_server.ask_memory("What did Priya ask about the contract?")
    assert result["answer"]
    assert "[1]" in result["answer"]
    assert result["citations"]
    assert result["citations"][0]["app"] == "Slack"


def test_create_reminder_and_event_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_server.helper_module,
        "add_reminder",
        lambda title, due=None, notes=None: calls.append(
            ("reminder", title, due, notes)
        )
        or {"id": "rem-1"},
    )
    monkeypatch.setattr(
        mcp_server.helper_module,
        "add_event",
        lambda title, start, end, notes=None: calls.append(
            ("event", title, start, end, notes)
        )
        or {"id": "evt-1"},
    )

    assert mcp_server.create_reminder(
        "Send contract", due="2025-03-10T17:00:00"
    ) == {"ok": True, "id": "rem-1"}
    assert mcp_server.create_calendar_event(
        "Review", "2025-03-10T10:00:00", "2025-03-10T10:30:00"
    ) == {"ok": True, "id": "evt-1"}
    assert calls == [
        ("reminder", "Send contract", "2025-03-10T17:00:00", None),
        (
            "event",
            "Review",
            "2025-03-10T10:00:00",
            "2025-03-10T10:30:00",
            None,
        ),
    ]


def test_draft_email_tool_opens_a_draft(monkeypatch):
    scripts = []
    from magpie import server

    monkeypatch.setattr(server, "run_applescript", scripts.append)
    result = mcp_server.draft_email(
        to="priya@example.com", subject="Hi", body="Hello"
    )
    assert result["ok"] is True
    assert result["sent"] is False
    assert "priya@example.com" in scripts[0]
    assert "visible:true" in scripts[0]


def test_get_calendar_tool(monkeypatch):
    monkeypatch.setattr(
        mcp_server.helper_module,
        "calendar_events",
        lambda start, end: [
            {
                "title": "Weekly sync",
                "start": start,
                "end": end,
                "location": "",
                "notes": "",
                "calendar": "Work",
            }
        ],
    )
    result = mcp_server.get_calendar(DAY)
    assert result["day"] == DAY
    assert result["count"] == 1
    assert result["events"][0]["title"] == "Weekly sync"
