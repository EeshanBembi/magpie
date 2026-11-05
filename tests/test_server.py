from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from magpie import config, journal
from magpie.db import Database
from magpie.llm import FakeProvider
from magpie.server import build_email_applescript, create_app

DAY = "2025-03-10"
T0 = datetime(2025, 3, 10, 9, 30).timestamp()
T1 = datetime(2025, 3, 10, 10, 15).timestamp()

OCR_TEXT = "Priya: can you send the updated contract by noon?"


class FakeHelper:
    """Stands in for the Swift helper: no screen, no permissions dialog."""

    def frontmost(self):
        return {
            "app": "Slack",
            "bundle_id": "com.tinyspeck.slackmacgap",
            "pid": 1234,
            "window_title": "Priya Singh - DM",
        }

    def idle(self):
        return {"seconds": 0.0}

    def idle_seconds(self):
        return 0.0

    def ocr(self, path):
        return {"text": OCR_TEXT, "lines": []}

    def permissions(self):
        return {"screen_recording": True}


def fake_screenshot(dest):
    image = Image.new("RGB", (320, 200), (255, 255, 255))
    for x in range(40, 120):
        for y in range(40, 80):
            image.putpixel((x, y), (20, 20, 20))
    image.save(dest, format="JPEG")
    return Path(dest)


class FakeRecorder:
    def __init__(self, audio_path):
        self.audio_path = Path(audio_path)
        self.started = False

    @property
    def recording(self):
        return self.started

    def start(self):
        self.started = True

    def stop(self):
        self.started = False
        self.audio_path.write_bytes(b"RIFF" + b"\x00" * 100)
        return self.audio_path


def fake_transcriber(audio_path, cfg, **kwargs):
    return {
        "transcript": "Priya: please send the updated contract by noon.",
        "engine": "fake",
    }


def make_provider():
    return FakeProvider(answer="You talked with Priya about the contract [1]")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    provider = make_provider()

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><title>Magpie</title>", encoding="utf-8"
    )

    email_scripts = []
    recorders = []

    def recorder_factory():
        recorder = FakeRecorder(tmp_path / f"meeting-{len(recorders)}.wav")
        recorders.append(recorder)
        return recorder

    app = create_app(
        cfg,
        database,
        capture_enabled=False,
        provider=provider,
        screenshot_source=fake_screenshot,
        helper=FakeHelper(),
        recorder_factory=recorder_factory,
        transcriber=fake_transcriber,
        email_runner=email_scripts.append,
        static_dir=static_dir,
    )
    with TestClient(app) as client:
        yield {
            "app": app,
            "client": client,
            "db": database,
            "config": cfg,
            "provider": provider,
            "tmp": tmp_path,
            "email_scripts": email_scripts,
        }
    database.close()


def seed_capture(env, ts=T0, app="Slack", title="Priya Singh - DM",
                 text=OCR_TEXT):
    return env["db"].insert_capture(
        ts, app, "com.example." + app.lower(), title, text
    )


def parse_sse(body):
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


# -- status / control --------------------------------------------------------


def test_status_shape(env):
    status = env["client"].get("/api/status").json()
    assert status["capturing"] is False
    assert status["paused"] is False
    assert status["running"] is False
    assert status["permission"] == {"screen_recording": True}
    assert status["provider"]["reachable"] is True
    assert status["provider"]["name"] == "fake"
    assert status["meeting_detected"] is False
    assert "counts" in status and "captures" in status["counts"]
    assert "time" in status


def test_pause_and_resume(env):
    client = env["client"]
    assert client.post("/api/pause").json() == {"ok": True, "paused": True}
    assert client.get("/api/status").json()["paused"] is True
    assert client.post("/api/resume").json() == {"ok": True, "paused": False}
    assert client.get("/api/status").json()["paused"] is False


def test_capture_now_stores_and_dedupes(env):
    client = env["client"]
    first = client.post("/api/capture-now").json()
    assert first["status"] == "captured"
    assert first["capture_id"]
    assert first["text"] == OCR_TEXT
    second = client.post("/api/capture-now").json()
    assert second["status"] == "deduped"


def test_capture_now_while_paused_skips(env):
    client = env["client"]
    client.post("/api/pause")
    result = client.post("/api/capture-now").json()
    assert result["status"] == "skipped"
    client.post("/api/resume")


# -- search / captures -------------------------------------------------------


def test_search_finds_seeded_capture(env):
    seed_capture(env)
    payload = env["client"].get("/api/search", params={"q": "contract"}).json()
    assert payload["count"] == 1
    result = payload["results"][0]
    assert "contract" in result["snippet"].lower()
    assert result["app"] == "Slack"


def test_search_collapses_near_duplicates(env):
    text = "Please send the updated contract to legal by noon."
    seed_capture(env, ts=T0, text=text)
    seed_capture(env, ts=T0 + 5, text=text + " Thanks!")

    payload = env["client"].get(
        "/api/search", params={"q": "contract"}
    ).json()
    assert payload["count"] == 1
    assert payload["results"][0]["similar_count"] == 2


def test_search_hostile_query_does_not_raise(env):
    seed_capture(env)
    for hostile in ('"', "AND (", "*", "OR NEAR"):
        response = env["client"].get("/api/search", params={"q": hostile})
        assert response.status_code == 200, hostile


def test_search_filters_by_app_and_time(env):
    seed_capture(env, ts=T0, app="Slack", text="contract draft in slack")
    seed_capture(env, ts=T1, app="Safari", text="contract draft in safari")
    payload = env["client"].get(
        "/api/search", params={"q": "contract", "app": "Safari"}
    ).json()
    assert payload["count"] == 1
    assert payload["results"][0]["app"] == "Safari"
    payload = env["client"].get(
        "/api/search",
        params={"q": "contract", "start": str(T1 - 60), "end": str(T1 + 60)},
    ).json()
    assert payload["count"] == 1
    assert payload["results"][0]["ts"] == T1


def test_get_capture_and_image(env, tmp_path):
    capture_id = seed_capture(env)
    capture = env["client"].get(f"/api/captures/{capture_id}").json()
    assert capture["id"] == capture_id
    assert capture["text"] == OCR_TEXT

    missing = env["client"].get("/api/captures/424242")
    assert missing.status_code == 404

    no_image = env["client"].get(f"/api/captures/{capture_id}/image")
    assert no_image.status_code == 404

    image_path = env["tmp"] / "shot.jpg"
    Image.new("RGB", (8, 8), (200, 200, 200)).save(image_path, "JPEG")
    env["db"].set_capture_image(capture_id, image_path)
    response = env["client"].get(f"/api/captures/{capture_id}/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_timeline(env):
    seed_capture(env, ts=T0, app="Slack")
    seed_capture(env, ts=T1, app="Safari", title="Budget", text="budget sheet")
    payload = env["client"].get("/api/timeline", params={"day": DAY}).json()
    assert payload["day"] == DAY
    assert len(payload["activities"]) == 2
    apps = {item["app"] for item in payload["app_usage"]}
    assert apps == {"Slack", "Safari"}
    assert all(item["seconds"] > 0 for item in payload["app_usage"])


# -- chat --------------------------------------------------------------------


def test_chat_streams_sse_and_persists(env):
    client = env["client"]
    with client.stream(
        "POST", "/api/chat", json={"message": "What did Priya ask about the contract?"}
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    events = parse_sse(body)
    deltas = [event for event in events if "delta" in event]
    assert deltas, "expected at least one delta event"
    final = events[-1]
    assert final["done"] is True
    assert final["chat_id"]
    assert isinstance(final["citations"], list)
    answer = "".join(event["delta"] for event in deltas)
    assert "[1]" in answer

    chats = client.get("/api/chats").json()
    assert chats["count"] == 1
    chat_id = final["chat_id"]
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["chat"]["title"]
    roles = [message["role"] for message in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert detail["messages"][1]["citations"] == final["citations"]


def test_chat_citations_are_compact(env):
    filler = "Notes from the meeting. " * 20
    full_text = filler + "Please send the updated contract to legal by noon."
    capture_id = seed_capture(env, text=full_text)

    with env["client"].stream(
        "POST",
        "/api/chat",
        json={"message": "What did Priya ask about the contract?"},
    ) as response:
        events = parse_sse("".join(response.iter_text()))
    final = events[-1]
    citation = final["citations"][0]

    assert set(citation) == {
        "id",
        "ts",
        "app",
        "window_title",
        "snippet",
        "has_image",
    }
    assert citation["id"] == capture_id
    assert "text" not in citation
    assert len(citation["snippet"]) <= 300
    assert "contract" in citation["snippet"].lower()
    assert citation["has_image"] is False

    detail = env["client"].get(f"/api/chats/{final['chat_id']}").json()
    assert detail["messages"][1]["citations"] == final["citations"]

    capture = env["client"].get(f"/api/captures/{capture_id}").json()
    assert capture["text"] == full_text


def test_chat_continues_existing_chat(env):
    client = env["client"]
    with client.stream(
        "POST", "/api/chat", json={"message": "first question"}
    ) as response:
        first = parse_sse("".join(response.iter_text()))[-1]
    with client.stream(
        "POST",
        "/api/chat",
        json={"chat_id": first["chat_id"], "message": "follow up"},
    ) as response:
        second = parse_sse("".join(response.iter_text()))[-1]
    assert second["chat_id"] == first["chat_id"]
    detail = client.get(f"/api/chats/{first['chat_id']}").json()
    assert len(detail["messages"]) == 4
    assert client.get("/api/chats").json()["count"] == 1


def test_chat_validation(env):
    client = env["client"]
    assert client.post("/api/chat", json={"message": "  "}).status_code == 400
    assert (
        client.post(
            "/api/chat", json={"chat_id": 999, "message": "hi"}
        ).status_code
        == 404
    )
    assert client.get("/api/chats/999").status_code == 404


# -- journal / loops ---------------------------------------------------------


def test_journal_generate_and_cache(env):
    client = env["client"]
    first = client.get("/api/journal", params={"day": DAY}).json()
    assert first["day"] == DAY
    assert first["summary"]
    assert first["cached"] is False
    second = client.get("/api/journal", params={"day": DAY}).json()
    assert second["cached"] is True
    regenerated = client.post(
        "/api/journal/regenerate", params={"day": DAY}
    ).json()
    assert regenerated["cached"] is False


def test_loops_extract_update_and_list(env):
    client = env["client"]
    seed_capture(env, ts=T0, text=OCR_TEXT)
    seed_capture(env, ts=T1, text="please water the plants today")
    env["provider"].answer = (
        '["Send updated contract by noon", "Water the plants today", '
        '"Send the updated contract"]'
    )
    extracted = client.post("/api/loops/extract", params={"day": DAY}).json()
    assert extracted["count"] == 3

    loops = client.get("/api/loops", params={"status": "open"}).json()
    assert loops["count"] == 3
    loop_id = loops["loops"][0]["id"]

    updated = client.post(f"/api/loops/{loop_id}", json={"status": "done"})
    assert updated.status_code == 200
    assert updated.json()["loop"]["status"] == "done"
    assert client.get("/api/loops", params={"status": "open"}).json()["count"] == 2
    assert (
        client.get("/api/loops", params={"status": "done"}).json()["count"] == 1
    )

    assert (
        client.post(f"/api/loops/{loop_id}", json={"status": "nope"}).status_code
        == 400
    )
    assert (
        client.post("/api/loops/9999", json={"status": "done"}).status_code
        == 404
    )


# -- meetings ----------------------------------------------------------------


def test_meeting_lifecycle(env):
    client = env["client"]
    started = client.post("/api/meetings/start", json={"title": "Priya sync"}).json()
    assert started["status"] == "recording"
    assert started["title"] == "Priya sync"
    meeting_id = started["id"]

    listing = client.get("/api/meetings").json()
    assert listing["count"] == 1

    stopped = client.post(f"/api/meetings/{meeting_id}/stop").json()
    assert stopped["status"] == "done"
    assert "contract" in stopped["transcript"]
    assert stopped["notes_md"]

    fetched = client.get(f"/api/meetings/{meeting_id}").json()
    assert fetched["status"] == "done"

    assert client.get("/api/meetings/999").status_code == 404
    assert client.post("/api/meetings/999/stop").status_code == 404


# -- settings / data ----------------------------------------------------------


def test_settings_mask_api_keys(env):
    client = env["client"]
    raw_key = "sk-test-1234567890abcdefghij"
    response = client.put(
        "/api/settings",
        json={
            "interval_seconds": 7,
            "providers": {"openai": {"api_key": raw_key}},
        },
    )
    assert response.status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["interval_seconds"] == 7
    assert settings["providers"]["openai"]["api_key"] == {"set": True}
    assert raw_key not in json.dumps(settings)

    # PUTting the masked value back must keep the stored key.
    client.put(
        "/api/settings",
        json={"providers": {"openai": {"api_key": {"set": True}}}},
    )
    ctx = env["app"].state.ctx
    assert ctx.config["providers"]["openai"]["api_key"] == raw_key
    assert client.get("/api/settings").json()["providers"]["openai"][
        "api_key"
    ] == {"set": True}

    # Unset keys show as not set.
    assert client.get("/api/settings").json()["providers"]["anthropic"][
        "api_key"
    ] == {"set": False}


def test_delete_range_and_export(env):
    client = env["client"]
    seed_capture(env, ts=T0, text="contract notes alpha")
    seed_capture(env, ts=T1, text="contract notes beta")
    assert client.get("/api/search", params={"q": "contract"}).json()[
        "count"
    ] == 2

    deleted = client.delete(
        "/api/data", params={"start": str(T0 - 10), "end": str(T0 + 10)}
    ).json()
    assert deleted["ok"] is True
    assert deleted["deleted"]["captures"] == 1
    remaining = client.get("/api/search", params={"q": "contract"}).json()
    assert remaining["count"] == 1

    assert (
        client.delete("/api/data", params={"start": str(T0)}).status_code == 400
    )

    exported = client.get("/api/export").json()
    for table in ("captures", "activities", "journals", "loops", "meetings"):
        assert table in exported
    assert exported["stats"]["captures"] == 1


# -- notifications / actions ---------------------------------------------------


def test_notifications(env):
    client = env["client"]
    today = datetime.now().strftime("%Y-%m-%d")
    with env["db"].conn:
        env["db"].conn.execute(
            "INSERT INTO loops (text, source_capture_id, day, status, created_ts)"
            " VALUES (?, NULL, ?, 'open', ?)",
            ("Reply to Sam about the contract", today, time.time()),
        )
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    journal.generate_journal(
        yesterday, env["config"], env["db"], provider=make_provider()
    )

    payload = client.get("/api/notifications").json()
    kinds = {item["kind"] for item in payload["notifications"]}
    assert "loop" in kinds
    assert "journal_ready" in kinds
    assert payload["meeting_detected"] is False
    loop_note = next(
        item for item in payload["notifications"] if item["kind"] == "loop"
    )
    assert "contract" in loop_note["body"]

    env["app"].state.ctx.loop.last_result = {
        "app": "zoom.us",
        "bundle_id": "us.zoom.xos",
        "window_title": "Zoom Meeting",
    }
    payload = client.get("/api/notifications").json()
    assert payload["meeting_detected"] is True
    kinds = {item["kind"] for item in payload["notifications"]}
    assert "meeting_detected" in kinds


def test_email_action_builds_visible_draft(env):
    client = env["client"]
    payload = client.post(
        "/api/actions/email",
        json={
            "to": "priya@example.com",
            "subject": "Updated contract",
            "body": "Hi Priya, attaching the contract.",
        },
    ).json()
    assert payload["ok"] is True
    assert payload["draft"] is True
    assert payload["sent"] is False
    assert len(env["email_scripts"]) == 1
    script = env["email_scripts"][0]
    assert "Mail" in script
    assert "priya@example.com" in script
    assert "Updated contract" in script
    assert "visible:true" in script
    assert "send" not in script.lower()


def test_email_action_reports_failure(env):
    def raising_runner(script):
        raise RuntimeError("Mail is not running")

    env["app"].state.ctx.email_runner = raising_runner
    response = env["client"].post(
        "/api/actions/email", json={"subject": "Hi", "body": "there"}
    )
    assert response.status_code == 500
    assert "Mail is not running" in response.json()["detail"]


def test_email_applescript_escapes_quotes():
    script = build_email_applescript(
        'sam+"tag"@example.com', 'He said "hi"', "Line one\nLine two"
    )
    assert '\\"hi\\"' in script
    assert "make new to recipient" in script


# -- static UI -----------------------------------------------------------------


def test_static_index_and_fallback(env):
    client = env["client"]
    index = client.get("/")
    assert index.status_code == 200
    assert "Magpie" in index.text
    fallback = client.get("/ask/some/deep/route")
    assert fallback.status_code == 200
    assert "Magpie" in fallback.text
    assert client.get("/api/definitely-not-a-route").status_code == 404


def test_static_missing_ui_returns_json_404(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    app = create_app(
        config.defaults(),
        database,
        capture_enabled=False,
        provider=make_provider(),
        helper=FakeHelper(),
        auto_journal=False,
        static_dir=tmp_path / "no-such-dir",
    )
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 404
        assert "not built" in response.json()["detail"]
    database.close()


# -- background capture + auto journal -----------------------------------------


def test_capture_loop_and_auto_journal_run_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    cfg["interval_seconds"] = 0.05
    app = create_app(
        cfg,
        database,
        capture_enabled=True,
        provider=make_provider(),
        screenshot_source=fake_screenshot,
        helper=FakeHelper(),
        static_dir=tmp_path / "static",
    )
    with TestClient(app) as client:
        deadline = time.time() + 10
        status = {}
        while time.time() < deadline:
            status = client.get("/api/status").json()
            if status["captures"] > 0:
                break
            time.sleep(0.05)
        assert status["captures"] > 0
        assert status["running"] is True
        assert status["capturing"] is True
        assert status["last_capture_ts"] is not None

        marker = tmp_path / "home" / ".journal-auto"
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), "auto journal should run once on startup"
    database.close()
