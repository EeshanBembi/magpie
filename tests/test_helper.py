from __future__ import annotations

import pytest

from magpie import helper


FAKE_HELPER = """#!/bin/sh
case "$1" in
  frontmost) printf '%s\\n' '{"app":"FakeApp","bundle_id":"com.example.fake","pid":42,"window_title":"Fake Window"}' ;;
  idle) printf '%s\\n' '{"seconds": 12.5}' ;;
  permissions) printf '%s\\n' '{"screen_recording": true}' ;;
  ocr) printf '%s\\n' '{"text": "hello", "lines": [{"text":"hello","confidence":0.9,"x":0,"y":0,"w":10,"h":10}]}' ;;
  fail) printf '%s\\n' '{"error":"boom"}'; exit 2 ;;
  bad) printf '%s\\n' 'not json'; exit 0 ;;
  crash) exit 3 ;;
  *) printf '%s\\n' '{"error":"unknown"}'; exit 1 ;;
esac
"""


@pytest.fixture
def fake_helper(monkeypatch, tmp_path):
    path = tmp_path / "fake-magpie-helper"
    path.write_text(FAKE_HELPER)
    path.chmod(0o755)
    monkeypatch.setenv("MAGPIE_HELPER", str(path))
    return path


def test_helper_path_prefers_env(fake_helper):
    assert helper.helper_path() == fake_helper


def test_frontmost(fake_helper):
    assert helper.frontmost() == {
        "app": "FakeApp",
        "bundle_id": "com.example.fake",
        "pid": 42,
        "window_title": "Fake Window",
    }


def test_idle(fake_helper):
    assert helper.idle() == {"seconds": 12.5}
    assert helper.idle_seconds() == 12.5


def test_permissions(fake_helper):
    assert helper.permissions() == {"screen_recording": True}
    assert helper.has_screen_recording() is True


def test_ocr(fake_helper, tmp_path):
    image = tmp_path / "screen.png"
    image.write_bytes(b"not really an image")
    result = helper.ocr(image)
    assert result["text"] == "hello"
    assert result["lines"][0]["confidence"] == 0.9

    with pytest.raises(helper.HelperError, match="image not found"):
        helper.ocr(tmp_path / "missing.png")


def test_helper_error_payload(fake_helper):
    with pytest.raises(helper.HelperError, match="boom"):
        helper.run("fail")


def test_invalid_json(fake_helper):
    with pytest.raises(helper.HelperError, match="invalid JSON"):
        helper.run("bad")


def test_nonzero_exit_without_output(fake_helper):
    with pytest.raises(helper.HelperError, match="exit code 3"):
        helper.run("crash")


def test_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "_candidates", lambda: [tmp_path / "nope"])
    with pytest.raises(helper.HelperError, match="not found"):
        helper.helper_path()


PHASE4_HELPER = """#!/bin/sh
case "$1" in
  calendar-events) printf '%s\\n' '[{"title":"Weekly sync","start":"2025-03-10T10:00:00","end":"2025-03-10T10:30:00","location":"Zoom","notes":"","calendar":"Work"}]' ;;
  add-reminder) printf '%s\\n' '{"id":"rem-42"}' ;;
  add-event) printf '%s\\n' '{"id":"evt-99"}' ;;
  *) printf '%s\\n' '{"error":"unknown"}'; exit 1 ;;
esac
"""


@pytest.fixture
def phase4_helper(tmp_path, monkeypatch):
    path = tmp_path / "phase4-helper"
    path.write_text(PHASE4_HELPER)
    path.chmod(0o755)
    monkeypatch.setenv("MAGPIE_HELPER", str(path))
    return path


def test_calendar_events_returns_list(phase4_helper):
    events = helper.calendar_events(
        "2025-03-10T00:00:00", "2025-03-11T00:00:00"
    )
    assert events == [
        {
            "title": "Weekly sync",
            "start": "2025-03-10T10:00:00",
            "end": "2025-03-10T10:30:00",
            "location": "Zoom",
            "notes": "",
            "calendar": "Work",
        }
    ]


def test_add_reminder_and_event_round_trip(phase4_helper):
    assert helper.add_reminder("Send contract", due="2025-03-10T17:00:00") == {
        "id": "rem-42"
    }
    assert helper.add_event(
        "Review", "2025-03-10T10:00:00", "2025-03-10T10:30:00", notes="sync"
    ) == {"id": "evt-99"}


class FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePopen:
    def __init__(self, stdout=""):
        self.stdin = FakeStdin()
        self.stdout = __import__("io").StringIO(stdout)
        self.stderr = __import__("io").StringIO("")
        self.returncode = None
        self.commands = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def send_signal(self, sig):
        self.signal = sig

    def kill(self):
        self.returncode = -9


def test_record_audio_process_starts_and_stops(tmp_path):
    process = FakePopen(
        '{"system": "/tmp/system.wav", "mic": "/tmp/mic.wav"}\n'
    )
    calls = []

    def popen(command):
        calls.append(command)
        return process

    session = helper.record_audio(
        tmp_path, popen=popen, startup_grace=0
    )
    assert calls == [[str(helper.helper_path()), "record-audio", str(tmp_path)]]
    assert session.running is True

    payload = session.stop()
    assert payload == {"system": "/tmp/system.wav", "mic": "/tmp/mic.wav"}
    assert process.stdin.closed is True
    assert session.running is False


def test_record_audio_process_reports_errors(tmp_path):
    process = FakePopen('{"error":"permission denied"}\n')

    session = helper.record_audio(
        tmp_path, popen=lambda command: process, startup_grace=0
    )
    with pytest.raises(helper.HelperError, match="permission denied"):
        session.stop()


def test_supports_record_audio_uses_helper(phase4_helper, monkeypatch):
    calls = []

    def fake_supports(subcommand, use_cache=True):
        calls.append(subcommand)
        return True

    monkeypatch.setattr(helper, "supports", fake_supports)
    assert helper.supports_record_audio() is True
    assert calls == ["record-audio"]
