from __future__ import annotations

import io
import time
from datetime import datetime

import pytest
from PIL import Image

from magpie import config
from magpie.capture import (
    CaptureLoop,
    ScreenshotError,
    capture_once,
    is_excluded,
    is_private_title,
    texts_are_similar,
)
from magpie.db import Database

T0 = datetime(2025, 3, 10, 12, 0).timestamp()
DAY = "2025-03-10"


class FakeHelper:
    def __init__(
        self,
        *,
        app="Safari",
        bundle_id="com.apple.Safari",
        window_title="Design doc",
        idle=0.0,
        text="Quarterly planning notes",
        frontmost_error=None,
        ocr_error=None,
    ):
        self.app = app
        self.bundle_id = bundle_id
        self.window_title = window_title
        self.idle = idle
        self.text = text
        self.frontmost_error = frontmost_error
        self.ocr_error = ocr_error
        self.frontmost_calls = 0
        self.ocr_calls = 0
        self.ocr_paths = []

    def frontmost(self):
        self.frontmost_calls += 1
        if self.frontmost_error is not None:
            raise self.frontmost_error
        return {
            "app": self.app,
            "bundle_id": self.bundle_id,
            "pid": 42,
            "window_title": self.window_title,
        }

    def idle(self):
        return {"seconds": self.idle}

    def idle_seconds(self):
        return self.idle

    def ocr(self, image_path):
        self.ocr_calls += 1
        self.ocr_paths.append(image_path)
        if self.ocr_error is not None:
            raise self.ocr_error
        return {"text": self.text, "lines": []}


class FakeScreen:
    def __init__(
        self,
        *,
        size=(2000, 1000),
        color=(245, 245, 245),
        fail=None,
        return_bytes=False,
    ):
        self.size = size
        self.color = color
        self.fail = fail
        self.return_bytes = return_bytes
        self.calls = 0

    def __call__(self, dest):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        image = Image.new("RGB", self.size, self.color)
        if self.return_bytes:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        image.save(dest, format="JPEG")
        return dest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["interval_seconds"] = 15
    try:
        yield {
            "db": database,
            "config": cfg,
            "shots": tmp_path / "shots",
            "tmp": tmp_path,
        }
    finally:
        database.close()


def run_capture(env, helper=None, screen=None, **kwargs):
    return capture_once(
        env["config"],
        env["db"],
        screenshot_source=screen or FakeScreen(),
        helper=helper or FakeHelper(),
        shots_root=env["shots"],
        **kwargs,
    )


def test_capture_pipeline_stores_redacted_text_and_screenshot(env):
    helper = FakeHelper(
        text=(
            "Quarterly planning notes\n"
            "password: hunter2\n"
            "Authorization: Bearer abc123DEF456\n"
            "Contact 123-45-6789"
        )
    )
    screen = FakeScreen(size=(2000, 1000), return_bytes=True)

    result = run_capture(env, helper=helper, screen=screen, now=T0)

    assert result["status"] == "captured"
    assert result["captured"] is True
    assert result["ok"] is True
    assert result["app"] == "Safari"
    assert result["window_title"] == "Design doc"
    assert result["capture_id"] is not None
    assert result["width"] == 1600
    assert result["height"] == 800
    assert result["redactions"]["password"] == 1
    assert result["redactions"]["bearer_token"] == 1
    assert result["redactions"]["ssn"] == 1
    assert "hunter2" not in result["text"]
    assert "abc123DEF456" not in result["text"]

    row = env["db"].get_capture(result["capture_id"])
    assert row is not None
    assert "hunter2" not in row["text"]
    assert row["text_hash"]
    assert row["image_path"] == result["image_path"]
    assert row["image_path"].endswith(f"{row['id']}.jpg")
    assert row["image_path"].startswith(str(env["shots"]))
    assert (env["tmp"] / "shots" / DAY / f"{row['id']}.jpg").is_file()

    activities = env["db"].timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["start_ts"] == T0
    assert activities[0]["end_ts"] == T0 + 15
    assert activities[0]["duration"] == 15


def test_capture_redacts_ocr_split_secret_from_a_real_style_capture(env):
    helper = FakeHelper(text="My key is sk_live_51 Habcdefghijk/mnopqrstuv")
    result = run_capture(env, helper=helper, now=T0)

    assert result["status"] == "captured"
    assert "sk_live_51" not in result["text"]
    assert "Habcdefghijk" not in result["text"]
    assert "mnopqrstuv" not in result["text"]
    assert result["redactions"]["stripe_key"] == 1
    row = env["db"].get_capture(result["capture_id"])
    assert row["text"] == "My key is [REDACTED:stripe_key]"


def test_keep_screenshots_false_skips_image(env):
    env["config"]["keep_screenshots"] = False
    screen = FakeScreen()
    result = run_capture(env, screen=screen, now=T0)

    assert result["status"] == "captured"
    assert result["image_path"] is None
    row = env["db"].get_capture(result["capture_id"])
    assert row["image_path"] is None
    assert not env["shots"].exists()


def test_excluded_app_is_skipped_without_taking_a_screenshot(env):
    env["config"]["excluded_apps"].append("Safari")
    screen = FakeScreen()
    result = run_capture(env, screen=screen, now=T0)

    assert result["status"] == "skipped"
    assert result["captured"] is False
    assert result["error"] == "excluded app"
    assert screen.calls == 0
    assert env["db"].stats()["captures"] == 0


def test_excluded_bundle_id_is_skipped(env):
    env["config"]["excluded_apps"].append("com.apple.Safari")
    result = run_capture(env, now=T0)
    assert result["status"] == "skipped"
    assert env["db"].stats()["captures"] == 0


def test_private_browsing_window_is_skipped(env):
    screen = FakeScreen()
    helper = FakeHelper(window_title="Inbox - Private Browsing")
    result = run_capture(env, helper=helper, screen=screen, now=T0)

    assert result["status"] == "skipped"
    assert result["error"] == "private browsing window"
    assert screen.calls == 0
    assert env["db"].stats()["captures"] == 0


def test_idle_and_paused_skip(env):
    screen = FakeScreen()
    idle_result = run_capture(
        env, helper=FakeHelper(idle=500), screen=screen, now=T0
    )
    assert idle_result["status"] == "skipped"
    assert "idle" in idle_result["error"]
    assert screen.calls == 0

    paused_result = run_capture(env, screen=screen, now=T0, paused=True)
    assert paused_result["status"] == "skipped"
    assert screen.calls == 0
    assert env["db"].stats()["captures"] == 0


def test_dedupe_extends_activity_without_storing_a_second_capture(env):
    helper = FakeHelper(text="The same long document is still open")
    screen = FakeScreen()

    first = run_capture(env, helper=helper, screen=screen, now=T0)
    second = run_capture(env, helper=helper, screen=screen, now=T0 + 15)

    assert first["status"] == "captured"
    assert second["status"] == "deduped"
    assert second["deduped"] is True
    assert second["capture_id"] is None
    assert env["db"].stats()["captures"] == 1
    assert screen.calls == 2

    activities = env["db"].timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["capture_count"] == 2
    assert activities[0]["end_ts"] == T0 + 30
    assert activities[0]["duration"] == 30


def test_similar_but_not_identical_text_is_deduped(env):
    helper = FakeHelper(text="A" * 500 + " budget review")
    first = run_capture(env, helper=helper, now=T0)
    helper.text = "A" * 500 + " budget review!"
    second = run_capture(env, helper=helper, now=T0 + 15)

    assert first["status"] == "captured"
    assert second["status"] == "deduped"
    assert env["db"].stats()["captures"] == 1


def test_black_screenshot_reports_permission_problem(env):
    screen = FakeScreen(color=(0, 0, 0))
    result = run_capture(env, screen=screen, now=T0)

    assert result["status"] == "permission"
    assert result["permission_problem"] is True
    assert result["ok"] is False
    assert "black" in result["error"].lower()
    assert env["db"].stats()["captures"] == 0


def test_screenshot_failure_reports_permission_problem(env):
    screen = FakeScreen(fail=ScreenshotError("screen recording denied"))
    result = run_capture(env, screen=screen, now=T0)

    assert result["status"] == "permission"
    assert result["permission_problem"] is True
    assert "denied" in result["error"]
    assert env["db"].stats()["captures"] == 0


def test_frontmost_and_ocr_failures_are_reported_without_storing(env):
    frontmost = run_capture(
        env, helper=FakeHelper(frontmost_error=RuntimeError("no app")), now=T0
    )
    assert frontmost["status"] == "error"
    assert "frontmost" in frontmost["error"]

    ocr = run_capture(
        env, helper=FakeHelper(ocr_error=RuntimeError("ocr broke")), now=T0
    )
    assert ocr["status"] == "error"
    assert "OCR failed" in ocr["error"]
    assert env["db"].stats()["captures"] == 0


def test_helpers_for_exclusions_private_titles_and_similarity():
    cfg = config.defaults()
    assert is_excluded(cfg, "1Password")
    assert not is_excluded(cfg, "Something", "com.example.secret")
    cfg["excluded_apps"].append("com.example.secret")
    assert is_excluded(cfg, "Something", "com.example.secret")
    assert not is_excluded(cfg, "Safari", "com.apple.Safari")

    assert is_private_title("New Tab - Incognito")
    assert is_private_title("Mail - InPrivate")
    assert not is_private_title("Inbox - Safari")

    assert texts_are_similar("hello world", "hello world")
    assert texts_are_similar("hello world", "hello world!")
    assert not texts_are_similar("hello world", "completely different")


def test_capture_loop_runs_in_background_and_stops(env):
    env["config"]["interval_seconds"] = 0.01
    loop = CaptureLoop(
        env["config"],
        env["db"],
        screenshot_source=FakeScreen(),
        helper=FakeHelper(),
        shots_root=env["shots"],
        clock=lambda: T0,
    )
    try:
        loop.start()
        deadline = time.time() + 3.0
        while (
            env["db"].stats()["captures"] < 1 or loop.last_result is None
        ) and time.time() < deadline:
            time.sleep(0.01)
        assert loop.running is True
        assert loop.last_result is not None
        assert env["db"].stats()["captures"] >= 1
    finally:
        loop.stop()

    assert loop.running is False
    assert loop.captures >= 1
    status = loop.status()
    assert status["running"] is False
    assert status["interval_seconds"] == 0.01
    assert status["captures"] >= 1


def test_capture_loop_pause_resume_and_status(env):
    screen = FakeScreen()
    loop = CaptureLoop(
        env["config"],
        env["db"],
        screenshot_source=screen,
        helper=FakeHelper(),
        shots_root=env["shots"],
        clock=lambda: T0,
    )

    loop.pause()
    paused = loop.capture_once()
    assert paused["status"] == "skipped"
    assert screen.calls == 0

    loop.resume()
    captured = loop.capture_once()
    assert captured["status"] == "captured"
    assert loop.captures == 1

    status = loop.status()
    assert status["paused"] is False
    assert status["last_capture_ts"] == T0
    assert status["last_result"] == captured
    assert status["permission_problem"] is False
