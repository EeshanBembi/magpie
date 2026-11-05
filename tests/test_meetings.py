from __future__ import annotations

import json
import signal
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from magpie import config
from magpie.db import Database
from magpie.llm import LLMProvider
from magpie.meetings import (
    FFmpegMicRecorder,
    HelperAudioRecorder,
    Recorder,
    RecorderError,
    TranscriptionError,
    create_recorder,
    detect_meeting_app,
    format_speaker_transcript,
    generate_notes,
    get_meeting,
    list_meetings,
    meeting_detected,
    merge_speaker_segments,
    start_meeting,
    stop_meeting,
    suggest_meeting_title,
    transcribe_audio,
    transcribe_speaker_tracks,
)

T0 = datetime(2025, 3, 10, 10, 0).timestamp()
T1 = datetime(2025, 3, 10, 10, 45).timestamp()


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.flushed = 0

    def write(self, text):
        self.writes.append(text)
        return len(text)

    def flush(self):
        self.flushed += 1


class FakeProcess:
    def __init__(
        self,
        audio_path,
        *,
        timeouts_before_exit=0,
        create_file=True,
    ):
        self.audio_path = Path(audio_path)
        self.stdin = FakeStdin()
        self.returncode = None
        self.signals = []
        self.waits = 0
        self.timeouts_before_exit = timeouts_before_exit
        self.create_file = create_file
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def _finish(self):
        self.returncode = 0
        if self.create_file:
            self.audio_path.parent.mkdir(parents=True, exist_ok=True)
            self.audio_path.write_bytes(b"RIFF....WAVE")

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits <= self.timeouts_before_exit:
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
        self._finish()
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeRunner:
    def __init__(self, tools=(), process=None, run_effect=None, run_result=None):
        self.tools = set(tools)
        self.process = process
        self.run_effect = run_effect
        self.run_result = run_result or FakeCompleted()
        self.popen_calls = []
        self.run_calls = []

    def which(self, name):
        return f"/usr/local/bin/{name}" if name in self.tools else None

    def popen(self, args, **kwargs):
        self.popen_calls.append(list(args))
        if self.process is None:
            self.process = FakeProcess(args[-1])
        return self.process

    def run(self, args, **kwargs):
        self.run_calls.append(list(args))
        if self.run_effect is not None:
            return self.run_effect(args)
        return self.run_result


class ScriptedProvider(LLMProvider):
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, messages, stream=False):
        self.calls.append([dict(item) for item in messages])
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if stream:
            return iter([answer])
        return answer


class FakeRecorder(Recorder):
    def __init__(self, audio_path, *, start_error=None, stop_error=None):
        self.audio_path = Path(audio_path)
        self.start_error = start_error
        self.stop_error = stop_error
        self.started = False
        self.stopped = False

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def stop(self):
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped = True
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_path.write_bytes(b"RIFF....WAVE")
        return self.audio_path

    @property
    def recording(self):
        return self.started and not self.stopped


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    try:
        yield {"db": database, "config": cfg, "tmp": tmp_path}
    finally:
        database.close()


def add_capture(env, ts, app, title, text):
    return env["db"].insert_capture(
        ts, app, f"com.example.{app.lower()}", title, text
    )


def prompt_text(messages):
    return "\n\n".join(item["content"] for item in messages)


def test_ffmpeg_recorder_builds_args_and_stops_with_q(tmp_path):
    audio = tmp_path / "meeting.wav"
    runner = FakeRunner()
    recorder = FFmpegMicRecorder(audio, ":0", runner=runner)

    recorder.start()
    assert recorder.recording is True
    assert runner.popen_calls == [
        [
            "ffmpeg",
            "-f",
            "avfoundation",
            "-i",
            ":0",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio),
        ]
    ]

    stopped = recorder.stop()
    assert stopped == audio
    assert audio.read_bytes().startswith(b"RIFF")
    assert runner.process.stdin.writes == ["q"]
    assert runner.process.stdin.flushed == 1
    assert recorder.recording is False


def test_ffmpeg_recorder_normalises_mic_device_and_falls_back_to_sigint(tmp_path):
    audio = tmp_path / "meeting.wav"
    process = FakeProcess(audio, timeouts_before_exit=1)
    runner = FakeRunner(process=process)
    recorder = FFmpegMicRecorder(audio, "0", runner=runner)

    recorder.start()
    assert runner.popen_calls[0][4] == ":0"

    recorder.stop()
    assert process.signals == [signal.SIGINT]
    assert audio.is_file()


def test_ffmpeg_recorder_reports_missing_audio_and_unstarted_stop(tmp_path):
    recorder = FFmpegMicRecorder(tmp_path / "missing.wav", runner=FakeRunner())
    with pytest.raises(RecorderError):
        recorder.stop()

    process = FakeProcess(tmp_path / "missing.wav", create_file=False)
    recorder = FFmpegMicRecorder(
        tmp_path / "missing.wav", runner=FakeRunner(process=process)
    )
    recorder.start()
    with pytest.raises(RecorderError):
        recorder.stop()


def test_transcribe_uses_mlx_whisper(tmp_path):
    wav = tmp_path / "meeting.wav"
    wav.write_bytes(b"RIFF....WAVE")

    def effect(args):
        out_dir = Path(args[args.index("--output-dir") + 1])
        (out_dir / f"{wav.stem}.txt").write_text("Priya: ship the contract.")
        return FakeCompleted()

    runner = FakeRunner(tools={"mlx_whisper"}, run_effect=effect)
    result = transcribe_audio(wav, config.defaults(), runner=runner)

    assert result["engine"] == "mlx_whisper"
    assert result["transcript"] == "Priya: ship the contract."
    args = runner.run_calls[0]
    assert args[0] == "mlx_whisper"
    assert args[1] == str(wav)
    assert "--model" in args
    assert args[args.index("--model") + 1] == "mlx-community/whisper-large-v3-turbo"
    assert args[args.index("--output-format") + 1] == "txt"


def test_transcribe_falls_back_to_whisper_cli(tmp_path):
    wav = tmp_path / "meeting.wav"
    wav.write_bytes(b"RIFF....WAVE")

    def effect(args):
        base = args[args.index("-of") + 1]
        Path(base + ".txt").write_text("Whisper CLI transcript")
        return FakeCompleted()

    runner = FakeRunner(tools={"whisper-cli"}, run_effect=effect)
    result = transcribe_audio(wav, config.defaults(), runner=runner)

    assert result["engine"] == "whisper-cli"
    assert result["transcript"] == "Whisper CLI transcript"
    assert runner.run_calls[0][0] == "whisper-cli"
    assert "-otxt" in runner.run_calls[0]


def test_transcribe_errors_are_clear(tmp_path):
    wav = tmp_path / "meeting.wav"
    wav.write_bytes(b"RIFF....WAVE")

    with pytest.raises(TranscriptionError) as missing:
        transcribe_audio(wav, config.defaults(), runner=FakeRunner())
    assert "install mlx_whisper or whisper-cli" in str(missing.value)

    failing = FakeRunner(
        tools={"mlx_whisper"},
        run_result=FakeCompleted(returncode=2, stderr="boom"),
    )
    with pytest.raises(TranscriptionError) as failed:
        transcribe_audio(wav, config.defaults(), runner=failing)
    assert "boom" in str(failed.value)

    with pytest.raises(TranscriptionError):
        transcribe_audio(tmp_path / "absent.wav", config.defaults(), runner=FakeRunner())


def test_generate_notes_uses_transcript_and_screen_memories(env):
    add_capture(
        env,
        T0 + 60,
        "Slack",
        "Priya Singh - DM",
        "Priya: The contract needs one more review.",
    )
    add_capture(
        env,
        T1 + 3600,
        "Safari",
        "Later",
        "This capture is outside the meeting window.",
    )
    provider = ScriptedProvider(
        ["## Summary\nA short sync.\n\n## Action items (owner if known)\n- Priya reviews."]
    )

    notes = generate_notes(
        {"title": "Weekly sync", "start_ts": T0, "end_ts": T1},
        env["config"],
        env["db"],
        provider=provider,
        transcript="Priya: I will review the contract.",
    )

    assert notes.startswith("## Summary")
    body = prompt_text(provider.calls[0])
    assert "Weekly sync" in body
    assert "Priya: I will review the contract." in body
    assert "The contract needs one more review." in body
    assert "outside the meeting window" not in body
    assert "## Key points" in provider.calls[0][0]["content"]
    assert "## Decisions" in provider.calls[0][0]["content"]
    assert "## Action items (owner if known)" in provider.calls[0][0]["content"]


@pytest.mark.parametrize(
    "app,bundle,title",
    [
        ("zoom.us", "", ""),
        ("Zoom Workplace", "", ""),
        ("Microsoft Teams", "", ""),
        ("Webex", "", ""),
        ("FaceTime", "", ""),
        ("Safari", "com.apple.Safari", "Meet - Weekly sync"),
        ("Chrome", "com.google.Chrome", "Google Meet - Standup"),
        ("Safari", "us.zoom.xos", "Anything"),
    ],
)
def test_meeting_app_detection_true(app, bundle, title):
    assert detect_meeting_app(app, bundle, title) is True
    assert meeting_detected(
        {"app": app, "bundle_id": bundle, "window_title": title}
    ) is True


@pytest.mark.parametrize(
    "app,bundle,title",
    [
        ("Safari", "com.apple.Safari", "Inbox"),
        ("Slack", "com.tinyspeck.slackmacgap", "Priya Singh - DM"),
        ("Notes", "com.apple.Notes", "Meeting notes"),
        ("", "", ""),
    ],
)
def test_meeting_app_detection_false(app, bundle, title):
    assert detect_meeting_app(app, bundle, title) is False
    assert meeting_detected(
        {"app": app, "bundle_id": bundle, "window_title": title}
    ) is False


def test_meeting_detected_handles_none():
    assert meeting_detected(None) is False


def test_start_and_stop_meeting_full_flow(env):
    audio = env["tmp"] / "meeting.wav"
    recorder = FakeRecorder(audio)
    provider = ScriptedProvider(["## Summary\nShipped the contract."])

    started = start_meeting(
        "Weekly sync",
        env["config"],
        env["db"],
        recorder,
        now=T0,
    )
    assert started["status"] == "recording"
    assert started["title"] == "Weekly sync"
    assert started["audio_path"] == str(audio)
    assert recorder.started is True

    def fake_transcriber(path, cfg, runner=None):
        return {"transcript": "Priya: Ship the contract.", "engine": "fake"}

    finished = stop_meeting(
        started["id"],
        env["config"],
        env["db"],
        recorder,
        transcriber=fake_transcriber,
        provider=provider,
        now=T1,
    )

    assert finished["status"] == "done"
    assert finished["end_ts"] == T1
    assert finished["transcript"] == "Priya: Ship the contract."
    assert finished["notes_md"] == "## Summary\nShipped the contract."
    assert finished["error"] is None
    assert recorder.stopped is True
    assert get_meeting(started["id"], env["db"]) == finished
    assert [item["id"] for item in list_meetings(env["db"])] == [started["id"]]


def test_start_meeting_records_a_start_error(env):
    recorder = FakeRecorder(
        env["tmp"] / "meeting.wav", start_error=RecorderError("no microphone")
    )
    started = start_meeting("Sync", env["config"], env["db"], recorder, now=T0)
    assert started["status"] == "error"
    assert "no microphone" in started["error"]


def test_stop_meeting_records_recorder_and_transcription_errors(env):
    audio = env["tmp"] / "meeting.wav"
    failed_stop = FakeRecorder(audio, stop_error=RecorderError("device vanished"))
    started = start_meeting("Sync", env["config"], env["db"], failed_stop, now=T0)
    stopped = stop_meeting(
        started["id"],
        env["config"],
        env["db"],
        failed_stop,
        provider=ScriptedProvider(["notes"]),
        now=T1,
    )
    assert stopped["status"] == "error"
    assert "device vanished" in stopped["error"]

    recorder = FakeRecorder(audio)
    started = start_meeting("Sync", env["config"], env["db"], recorder, now=T0)

    def broken_transcriber(path, cfg, runner=None):
        raise TranscriptionError("bad audio")

    failed = stop_meeting(
        started["id"],
        env["config"],
        env["db"],
        recorder,
        transcriber=broken_transcriber,
        provider=ScriptedProvider(["notes"]),
        now=T1,
    )
    assert failed["status"] == "error"
    assert "bad audio" in failed["error"]


def test_stop_meeting_unknown_id_raises(env):
    with pytest.raises(ValueError):
        stop_meeting(999, env["config"], env["db"])


# -- helper-backed system + mic recording ------------------------------------


class FakeRecordSession:
    def __init__(self, system, mic):
        self.system = Path(system)
        self.mic = Path(mic)
        self.stopped = False
        self.running = True

    def stop(self, timeout=10.0):
        self.stopped = True
        self.running = False
        return {"system": str(self.system), "mic": str(self.mic)}


class FakeAudioHelper:
    def __init__(self, supports=True, events=None, window_title="Fake Window"):
        self.supports = supports
        self.events = events or []
        self.window_title = window_title
        self.calls = []

    def supports_record_audio(self):
        return self.supports

    def record_audio(self, out_dir):
        self.calls.append(Path(out_dir))
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "system.wav").write_bytes(b"RIFF-system")
        (out / "mic.wav").write_bytes(b"RIFF-mic")
        return FakeRecordSession(out / "system.wav", out / "mic.wav")

    def calendar_events(self, start_iso, end_iso):
        return list(self.events)

    def frontmost(self):
        return {"window_title": self.window_title}


class MixingRunner(FakeRunner):
    def run(self, args, **kwargs):
        self.run_calls.append(list(args))
        if args and args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"RIFF-mix")
            return FakeCompleted()
        if args and args[0] == "mlx_whisper":
            audio = Path(args[1])
            out_dir = Path(args[args.index("--output-dir") + 1])
            segments = (
                [{"start": 0.0, "end": 1.0, "text": "Hi from me"}]
                if audio.name == "mic.json" or audio.stem == "mic"
                else [
                    {"start": 0.5, "end": 1.5, "text": "Hello from them"},
                    {"start": 2.0, "end": 3.0, "text": "Second point"},
                ]
            )
            (out_dir / f"{audio.stem}.json").write_text(
                json.dumps({"text": "", "segments": segments})
            )
            return FakeCompleted()
        return super().run(args, **kwargs)


def test_helper_audio_recorder_mixes_tracks_and_labels(tmp_path):
    helper = FakeAudioHelper()
    runner = MixingRunner(tools={"mlx_whisper"})
    recorder = HelperAudioRecorder(tmp_path / "tracks", helper=helper, runner=runner)

    recorder.start()
    assert recorder.recording is True
    mixed = recorder.stop()
    assert mixed == tmp_path / "tracks" / "mix.wav"
    assert mixed.read_bytes().startswith(b"RIFF")
    assert recorder.tracks == {
        "system": str(tmp_path / "tracks" / "system.wav"),
        "mic": str(tmp_path / "tracks" / "mic.wav"),
    }
    assert recorder.status()["mode"] == "system+mic"

    result = transcribe_speaker_tracks(
        recorder.system_path,
        recorder.mic_path,
        config.defaults(),
        runner=runner,
    )
    assert result["segments"] == [
        {"speaker": "Me", "start": 0.0, "end": 1.0, "text": "Hi from me"},
        {
            "speaker": "Them",
            "start": 0.5,
            "end": 1.5,
            "text": "Hello from them",
        },
        {"speaker": "Them", "start": 2.0, "end": 3.0, "text": "Second point"},
    ]
    assert "[00:00:00] Me: Hi from me" in result["transcript"]
    assert "[00:00:00] Them: Hello from them" in result["transcript"]


def test_merge_speaker_segments_sorts_and_skips_empty():
    segments = merge_speaker_segments(
        [{"start": 2, "end": 3, "text": " later "}],
        [
            {"start": 1, "end": 2, "text": "earlier"},
            {"start": 3, "end": 4, "text": "   "},
        ],
    )
    assert [item["speaker"] for item in segments] == ["Them", "Me"]
    assert format_speaker_transcript(segments).splitlines() == [
        "[00:00:01] Them: earlier",
        "[00:00:02] Me: later",
    ]


def test_helper_audio_recorder_falls_back_to_single_track(tmp_path):
    class MicOnlySession(FakeRecordSession):
        def stop(self, timeout=10.0):
            return {"system": "", "mic": str(self.mic)}

    class MicOnlyHelper(FakeAudioHelper):
        def record_audio(self, out_dir):
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "mic.wav").write_bytes(b"RIFF-mic")
            return MicOnlySession(out / "missing.wav", out / "mic.wav")

    recorder = HelperAudioRecorder(
        tmp_path / "tracks", helper=MicOnlyHelper(), runner=MixingRunner()
    )
    recorder.start()
    assert recorder.stop() == tmp_path / "tracks" / "mic.wav"
    assert recorder.tracks is None


def test_create_recorder_prefers_helper_and_falls_back(tmp_path):
    audio = tmp_path / "meeting.wav"
    helper = FakeAudioHelper(supports=True)
    recorder = create_recorder(config.defaults(), audio, helper=helper)
    assert isinstance(recorder, HelperAudioRecorder)
    assert recorder.audio_path.name == "mix.wav"

    fallback = create_recorder(
        config.defaults(), audio, helper=FakeAudioHelper(supports=False)
    )
    assert isinstance(fallback, FFmpegMicRecorder)
    assert fallback.audio_path == audio


def test_suggest_meeting_title_uses_calendar_then_window(monkeypatch):
    now = datetime(2025, 3, 10, 10, 15).timestamp()
    event = {
        "title": "Weekly sync",
        "start": "2025-03-10T10:00:00",
        "end": "2025-03-10T10:30:00",
    }
    assert (
        suggest_meeting_title(
            "", helper=FakeAudioHelper(events=[event]), now=now
        )
        == "Weekly sync"
    )
    assert (
        suggest_meeting_title(
            "", helper=FakeAudioHelper(events=[]), now=now
        )
        == "Fake Window"
    )
    assert (
        suggest_meeting_title(
            "My title", helper=FakeAudioHelper(events=[event]), now=now
        )
        == "My title"
    )


def test_stop_meeting_uses_tracks_transcriber(env):
    audio = env["tmp"] / "meeting-tracks"
    helper = FakeAudioHelper()
    recorder = HelperAudioRecorder(audio, helper=helper, runner=MixingRunner())
    started = start_meeting(
        "Tracked sync", env["config"], env["db"], recorder, now=T0
    )
    calls = []

    def fake_tracks(system, mic, cfg, runner=None):
        calls.append((system, mic))
        return {
            "transcript": "[00:00:00] Me: hi\n[00:00:01] Them: hello",
            "segments": [],
            "engine": "fake",
        }

    finished = stop_meeting(
        started["id"],
        env["config"],
        env["db"],
        recorder,
        tracks_transcriber=fake_tracks,
        provider=ScriptedProvider(["## Summary\nTracked."]),
        now=T1,
    )

    assert finished["status"] == "done"
    assert calls and calls[0][0].endswith("system.wav")
    assert calls[0][1].endswith("mic.wav")
    assert finished["transcript"].startswith("[00:00:00] Me: hi")
