from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie import config, remote
from magpie.db import Database
from magpie.helper import HelperError
from magpie.llm import FakeProvider
from magpie.server import create_app

DAY = "2025-03-10"
EVENT = {
    "title": "Weekly sync",
    "start": "2025-03-10T10:00:00",
    "end": "2025-03-10T10:30:00",
    "location": "Zoom",
    "notes": "Quarterly plan",
    "calendar": "Work",
}


class FakeHelper:
    def __init__(self):
        self.reminder_calls = []
        self.event_calls = []
        self.events = [dict(EVENT)]
        self.calendar_error = None

    def frontmost(self):
        return {
            "app": "Slack",
            "bundle_id": "com.example.slack",
            "pid": 1,
            "window_title": "Priya Singh - DM",
        }

    def idle(self):
        return {"seconds": 0.0}

    def idle_seconds(self):
        return 0.0

    def permissions(self):
        return {"screen_recording": True}

    def calendar_events(self, start_iso, end_iso):
        if self.calendar_error:
            raise self.calendar_error
        return [dict(event) for event in self.events]

    def add_reminder(self, title, due=None, notes=None):
        self.reminder_calls.append({"title": title, "due": due, "notes": notes})
        return {"id": "rem-1"}

    def add_event(self, title, start_iso, end_iso, notes=None):
        self.event_calls.append(
            {
                "title": title,
                "start": start_iso,
                "end": end_iso,
                "notes": notes,
            }
        )
        return {"id": "evt-1"}

    def supports_record_audio(self):
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    helper = FakeHelper()

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<title>Magpie</title>")

    notion_requests = []

    def notion_handler(request: httpx.Request) -> httpx.Response:
        notion_requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"id": "page-1", "url": "https://notion.so/page-1"}
        )

    app = create_app(
        cfg,
        database,
        capture_enabled=False,
        provider=FakeProvider(answer="Answer [1]"),
        helper=helper,
        notion_transport=httpx.MockTransport(notion_handler),
        static_dir=static_dir,
        auto_journal=False,
    )
    with TestClient(app) as client:
        yield {
            "client": client,
            "app": app,
            "db": database,
            "config": cfg,
            "helper": helper,
            "notion_requests": notion_requests,
            "tmp": tmp_path,
        }
    database.close()


def test_reminder_action_calls_helper(env):
    response = env["client"].post(
        "/api/actions/reminder",
        json={
            "title": "Send the contract",
            "due": "2025-03-10T17:00:00",
            "notes": "Before EOD",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": "rem-1"}
    assert env["helper"].reminder_calls == [
        {
            "title": "Send the contract",
            "due": "2025-03-10T17:00:00",
            "notes": "Before EOD",
        }
    ]


def test_reminder_action_validation_and_errors(env):
    assert env["client"].post(
        "/api/actions/reminder", json={"title": "   "}
    ).status_code == 400

    def broken(title, due=None, notes=None):
        raise HelperError("Reminders permission denied")

    env["app"].state.ctx.helper.add_reminder = broken
    response = env["client"].post(
        "/api/actions/reminder", json={"title": "x"}
    )
    assert response.status_code == 502
    assert "permission denied" in response.json()["detail"]


def test_event_action_calls_helper(env):
    response = env["client"].post(
        "/api/actions/event",
        json={
            "title": "Review contract",
            "start": "2025-03-10T11:00:00",
            "end": "2025-03-10T11:30:00",
            "notes": "With Priya",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": "evt-1"}
    assert env["helper"].event_calls == [
        {
            "title": "Review contract",
            "start": "2025-03-10T11:00:00",
            "end": "2025-03-10T11:30:00",
            "notes": "With Priya",
        }
    ]


def test_event_action_validates_times(env):
    client = env["client"]
    assert client.post(
        "/api/actions/event", json={"title": "No time"}
    ).status_code == 400
    assert client.post(
        "/api/actions/event",
        json={
            "title": "Backwards",
            "start": "2025-03-10T12:00:00",
            "end": "2025-03-10T11:00:00",
        },
    ).status_code == 400
    assert client.post(
        "/api/actions/event",
        json={
            "title": "Bad",
            "start": "not-a-time",
            "end": "2025-03-10T11:00:00",
        },
    ).status_code == 400


def test_actions_report_missing_helper_support(env):
    env["app"].state.ctx.helper = object()
    response = env["client"].post(
        "/api/actions/reminder", json={"title": "x"}
    )
    assert response.status_code == 501
    assert "does not support add_reminder" in response.json()["detail"]
    assert env["client"].post(
        "/api/actions/event",
        json={
            "title": "x",
            "start": "2025-03-10T10:00:00",
            "end": "2025-03-10T11:00:00",
        },
    ).status_code == 501


def test_notion_action_creates_page(env):
    ctx = env["app"].state.ctx
    ctx.config["notion_token"] = "secret"
    ctx.config["notion_parent_page_id"] = "parent"
    response = env["client"].post(
        "/api/actions/notion",
        json={"title": "Journal", "markdown": "# Day\n- item"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "id": "page-1",
        "url": "https://notion.so/page-1",
    }
    body = env["notion_requests"][0]
    assert body["parent"] == {"page_id": "parent"}
    assert [block["type"] for block in body["children"]] == [
        "heading_1",
        "bulleted_list_item",
    ]


def test_notion_action_requires_configuration(env):
    response = env["client"].post(
        "/api/actions/notion",
        json={"title": "Journal", "markdown": "Hello"},
    )
    assert response.status_code == 502
    assert "notion_token" in response.json()["detail"]
    assert env["client"].post(
        "/api/actions/notion", json={"title": "Empty", "markdown": "  "}
    ).status_code == 400


def test_calendar_endpoint(env):
    payload = env["client"].get("/api/calendar", params={"day": DAY}).json()
    assert payload["day"] == DAY
    assert payload["count"] == 1
    assert payload["events"][0]["title"] == "Weekly sync"

    env["helper"].calendar_error = HelperError("Calendar permission denied")
    response = env["client"].get("/api/calendar", params={"day": DAY})
    assert response.status_code == 502
    assert "permission denied" in response.json()["detail"]


def test_journal_includes_calendar_events(env):
    env["db"].insert_capture(
        datetime(2025, 3, 10, 9, 0).timestamp(),
        "Slack",
        "com.example.slack",
        "Priya",
        "Please send the contract.",
    )
    payload = env["client"].get("/api/journal", params={"day": DAY}).json()
    assert payload["summary"]
    assert payload["calendar"] == [EVENT]

    regenerated = env["client"].post(
        "/api/journal/regenerate", params={"day": DAY}
    ).json()
    assert regenerated["calendar"] == [EVENT]


def test_settings_mask_notion_and_remote_tokens(env):
    client = env["client"]
    client.put(
        "/api/settings",
        json={
            "notion_token": "secret-notion",
            "notion_parent_page_id": "parent-1",
            "remote_token": "secret-remote",
        },
    )
    settings = client.get("/api/settings").json()
    assert settings["notion_token"] == {"set": True}
    assert settings["remote_token"] == {"set": True}
    assert settings["notion_parent_page_id"] == "parent-1"
    assert "secret-notion" not in json.dumps(settings)

    client.put(
        "/api/settings",
        json={"notion_token": {"set": True}, "remote_token": {"set": True}},
    )
    ctx = env["app"].state.ctx
    assert ctx.config["notion_token"] == "secret-notion"
    assert ctx.config["remote_token"] == "secret-remote"


# -- remote access (16c) ------------------------------------------------------


@pytest.fixture
def remote_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    cfg["remote_access"] = True
    cfg["remote_token"] = "known-token"
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<title>Magpie</title>")
    monkeypatch.setattr(remote, "lan_addresses", lambda: ["192.168.1.23"])
    monkeypatch.setattr(remote, "is_loopback", lambda host: False)
    app = create_app(
        cfg,
        database,
        capture_enabled=False,
        provider=FakeProvider(answer="ok"),
        helper=FakeHelper(),
        static_dir=static_dir,
        auto_journal=False,
    )
    with TestClient(app) as client:
        yield {"client": client, "app": app, "config": cfg}
    database.close()


def test_remote_requires_auth_for_non_loopback(remote_env):
    client = remote_env["client"]
    response = client.get("/api/status")
    assert response.status_code == 401
    assert "authentication required" in response.json()["detail"]

    assert (
        client.get(
            "/api/status", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_remote_accepts_bearer_and_pair_cookie(remote_env):
    client = remote_env["client"]
    headers = {"Authorization": "Bearer known-token"}
    assert client.get("/api/status", headers=headers).status_code == 200

    code = remote_env["app"].state.ctx.pair_code
    denied = client.get(
        "/pair", params={"code": "000000"}, follow_redirects=False
    )
    assert denied.status_code == 403

    paired = client.get(
        "/pair", params={"code": code}, follow_redirects=False
    )
    assert paired.status_code == 303
    assert paired.cookies[remote.COOKIE_NAME] == "known-token"
    # The cookie alone now authorises the browser.
    assert client.get("/api/status").status_code == 200


def test_remote_state_and_rotate(remote_env):
    client = remote_env["client"]
    headers = {"Authorization": "Bearer known-token"}
    state = client.get("/api/remote", headers=headers).json()
    assert state["enabled"] is True
    assert state["urls"] == ["http://192.168.1.23:7777/"]
    assert state["pair_code"]
    assert state["qr_svg"] and "<svg" in state["qr_svg"]

    rotated = client.post("/api/remote/rotate", headers=headers).json()
    assert rotated["enabled"] is True
    assert rotated["pair_code"] != state["pair_code"]
    new_token = remote_env["app"].state.ctx.config["remote_token"]
    assert new_token != "known-token"

    # The old token no longer works; the new one does.
    assert client.get("/api/status", headers=headers).status_code == 401
    fresh = {"Authorization": f"Bearer {new_token}"}
    assert client.get("/api/status", headers=fresh).status_code == 200


def test_pair_is_rejected_when_remote_is_off(env):
    response = env["client"].get(
        "/pair", params={"code": "123456"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "turned off" in response.json()["detail"]


def test_remote_endpoint_when_disabled(env):
    state = env["client"].get("/api/remote").json()
    assert state["enabled"] is False
    assert state["qr_svg"] is None
    assert "pair_code" in state


def test_run_binds_zero_host_when_remote_enabled(tmp_path, monkeypatch):
    import uvicorn

    from magpie import server as server_module

    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    seen = {}

    def fake_run(app, host, port, log_level):
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr(uvicorn, "run", fake_run)

    cfg = config.defaults()
    cfg["provider"] = "fake"
    server_module.run(port=1234, capture=False, config=cfg, remote=True)
    assert seen == {"host": "0.0.0.0", "port": 1234}

    cfg = config.defaults()
    cfg["provider"] = "fake"
    cfg["remote_access"] = True
    server_module.run(port=4321, capture=False, config=cfg, remote=False)
    assert seen == {"host": "0.0.0.0", "port": 4321}
