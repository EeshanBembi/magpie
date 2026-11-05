"""Meeting recording, transcription and notes.

Recording is behind the small :class:`Recorder` interface so the ffmpeg
microphone recorder can later be swapped for a ScreenCaptureKit helper that
also captures system audio.  Transcription prefers ``mlx_whisper`` and falls
back to ``whisper-cli``.  Notes combine the transcript with what was on screen
during the meeting.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config as config_module
from . import db as db_module
from . import helper as helper_module
from . import llm as llm_module

MAX_CAPTURES = 20
MAX_CAPTURE_CHARS = 800
MAX_TRANSCRIPT_CHARS = 16000
MAX_CONTEXT_CHARS = 12000

MEETING_APPS = frozenset(
    {
        "zoom",
        "zoom.us",
        "zoom workplace",
        "microsoft teams",
        "teams",
        "webex",
        "cisco webex meetings",
        "facetime",
        "google meet",
    }
)

MEETING_BUNDLE_HINTS = (
    "zoom.us",
    "us.zoom",
    "microsoft.teams",
    "com.microsoft.teams",
    "webex",
    "facetime",
    "google.meet",
)

_MEET_TITLE_RE = re.compile(r"\bmeet\s*[-–—]\s*", re.IGNORECASE)

_NOTES_SYSTEM = (
    "You are Magpie, writing private meeting notes from a transcript and the "
    "user's own screen memories. Be accurate and concise. Use only the provided "
    "material; if something is unclear, say so. Return markdown with exactly "
    "these sections:\n## Summary\n## Key points\n## Decisions\n"
    "## Action items (owner if known)"
)


class MeetingError(RuntimeError):
    """Base class for meeting recording and processing failures."""


class RecorderError(MeetingError):
    """Audio could not be recorded."""


class TranscriptionError(MeetingError):
    """Audio could not be transcribed."""


class SubprocessRunner:
    """Thin wrapper so tests can replace process creation and tool lookup."""

    def popen(self, args: list[str], **kwargs: Any) -> Any:
        kwargs.setdefault("stdin", subprocess.PIPE)
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("text", True)
        return subprocess.Popen(args, **kwargs)

    def run(self, args: list[str], **kwargs: Any) -> Any:
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("check", False)
        return subprocess.run(args, **kwargs)

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class Recorder:
    """Interface for an audio recorder.

    Implementations expose ``audio_path`` and create a mono 16 kHz WAV file on
    ``stop()``.  A future helper-backed system-audio recorder can implement the
    same three methods.
    """

    audio_path: Path

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> Path:
        raise NotImplementedError

    @property
    def recording(self) -> bool:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"recording": self.recording, "audio_path": str(self.audio_path)}


def _normalise_device(mic_device: str | None) -> str:
    value = str(mic_device or ":0").strip()
    if not value:
        return ":0"
    if value.startswith(":"):
        return value
    return f":{value}"


class FFmpegMicRecorder(Recorder):
    """Record the default microphone with ffmpeg's avfoundation input."""

    def __init__(
        self,
        audio_path: str | Path,
        mic_device: str = ":0",
        *,
        runner: Any | None = None,
        stop_timeout: float = 5.0,
    ) -> None:
        self.audio_path = Path(audio_path)
        self.mic_device = _normalise_device(mic_device)
        self.runner = runner if runner is not None else SubprocessRunner()
        self.stop_timeout = float(stop_timeout)
        self._process: Any | None = None
        self._stopped = False
        self._error: str | None = None

    @property
    def recording(self) -> bool:
        if self._process is None or self._stopped:
            return False
        try:
            return self._process.poll() is None
        except Exception:
            return False

    def start(self) -> None:
        if self.recording:
            return
        self.audio_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.audio_path.exists():
                self.audio_path.unlink()
        except OSError:
            pass
        args = [
            "ffmpeg",
            "-f",
            "avfoundation",
            "-i",
            self.mic_device,
            "-ac",
            "1",
            "-ar",
            "16000",
            str(self.audio_path),
        ]
        try:
            self._process = self.runner.popen(args)
        except OSError as exc:
            self._error = str(exc)
            raise RecorderError(f"could not start ffmpeg: {exc}") from exc
        self._stopped = False
        self._error = None

    def _wait(self, process: Any, timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _interrupt(self, process: Any) -> None:
        try:
            process.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError, AttributeError):
            pass

    def stop(self) -> Path:
        process = self._process
        if process is None:
            raise RecorderError("recorder was not started")
        if not self._stopped:
            self._stopped = True
            stdin = getattr(process, "stdin", None)
            if stdin is not None:
                try:
                    stdin.write("q")
                    stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    pass
            if not self._wait(process, self.stop_timeout):
                self._interrupt(process)
                if not self._wait(process, self.stop_timeout):
                    try:
                        process.terminate()
                    except (ProcessLookupError, OSError):
                        pass
                    if not self._wait(process, 1.0):
                        try:
                            process.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        self._wait(process, 1.0)

        if not self.audio_path.is_file() or self.audio_path.stat().st_size == 0:
            raise RecorderError(
                f"ffmpeg did not produce an audio file at {self.audio_path}; "
                "check the microphone device and Microphone permission"
            )
        return self.audio_path


class HelperAudioRecorder(Recorder):
    """Record system audio + microphone through ``magpie-helper record-audio``.

    The helper writes ``system.wav`` (everyone else on the call) and
    ``mic.wav`` (the user).  ``stop()`` finalises the helper, mixes both
    tracks into ``mix.wav`` with ffmpeg, and remembers the track paths so the
    transcription pipeline can label speakers.
    """

    def __init__(
        self,
        out_dir: str | Path,
        *,
        helper: Any | None = None,
        runner: Any | None = None,
        stop_timeout: float = 10.0,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.audio_path = self.out_dir / "mix.wav"
        self.system_path: Path | None = None
        self.mic_path: Path | None = None
        self._helper = helper if helper is not None else helper_module
        self.runner = runner if runner is not None else SubprocessRunner()
        self.stop_timeout = float(stop_timeout)
        self._session: Any | None = None
        self._stopped = False
        self._error: str | None = None

    @property
    def recording(self) -> bool:
        if self._session is None or self._stopped:
            return False
        return bool(getattr(self._session, "running", False))

    @property
    def tracks(self) -> dict[str, str] | None:
        """Both track paths, when system and mic audio exist on disk."""

        if self.system_path is None or self.mic_path is None:
            return None
        if not _audio_ok(self.system_path) or not _audio_ok(self.mic_path):
            return None
        return {"system": str(self.system_path), "mic": str(self.mic_path)}

    def start(self) -> None:
        if self.recording:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._session = self._helper.record_audio(self.out_dir)
        except Exception as exc:
            self._error = str(exc)
            raise RecorderError(
                "could not start helper audio recording; grant Screen "
                f"Recording and Microphone permissions ({exc})"
            ) from exc
        self._stopped = False
        self._error = None

    def _mix(self, system: Path, mic: Path) -> Path:
        mix_path = self.out_dir / "mix.wav"
        try:
            if mix_path.exists():
                mix_path.unlink()
        except OSError:
            pass
        args = [
            "ffmpeg",
            "-y",
            "-i",
            str(system),
            "-i",
            str(mic),
            "-filter_complex",
            "amix=inputs=2",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(mix_path),
        ]
        completed = self.runner.run(args)
        if getattr(completed, "returncode", 1) != 0:
            raise RecorderError(
                "ffmpeg could not mix system and mic audio: "
                + _completed_detail(completed)
            )
        if not _audio_ok(mix_path):
            raise RecorderError(
                f"ffmpeg mixed the tracks but {mix_path} is missing or empty"
            )
        return mix_path

    def stop(self) -> Path:
        session = self._session
        if session is None:
            raise RecorderError("recorder was not started")
        if not self._stopped:
            self._stopped = True
            try:
                payload = session.stop(timeout=self.stop_timeout)
            except Exception as exc:
                raise RecorderError(
                    f"could not finalise the helper recording: {exc}"
                ) from exc
            system = payload.get("system") if isinstance(payload, dict) else None
            mic = payload.get("mic") if isinstance(payload, dict) else None
            self.system_path = Path(str(system)) if system else None
            self.mic_path = Path(str(mic)) if mic else None

        system_ok = self.system_path is not None and _audio_ok(self.system_path)
        mic_ok = self.mic_path is not None and _audio_ok(self.mic_path)
        if system_ok and mic_ok:
            assert self.system_path is not None and self.mic_path is not None
            self.audio_path = self._mix(self.system_path, self.mic_path)
            return self.audio_path
        if mic_ok and self.mic_path is not None:
            self.audio_path = self.mic_path
            return self.audio_path
        if system_ok and self.system_path is not None:
            self.audio_path = self.system_path
            return self.audio_path
        raise RecorderError(
            "the helper did not produce system.wav or mic.wav in "
            f"{self.out_dir}; check Screen Recording and Microphone permissions"
        )

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["mode"] = "system+mic"
        return base


def _audio_ok(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _completed_detail(completed: Any) -> str:
    for attribute in ("stderr", "stdout"):
        value = getattr(completed, attribute, "") or ""
        if str(value).strip():
            return str(value).strip().splitlines()[-1]
    return f"exit code {getattr(completed, 'returncode', '?')}"


def create_recorder(
    config: dict[str, Any] | None = None,
    audio_path: str | Path | None = None,
    *,
    runner: Any | None = None,
    now: float | None = None,
    helper: Any | None = None,
) -> Recorder:
    """Build the best available recorder for a new meeting.

    Prefers the Swift helper's ``record-audio`` (system + mic audio via
    ScreenCaptureKit) and falls back to the ffmpeg microphone recorder when
    the installed helper does not support it.
    """

    cfg = config if config is not None else config_module.load_config()
    moment = datetime.fromtimestamp(now if now is not None else time.time())
    if audio_path is None:
        audio_path = config_module.audio_dir() / (
            f"meeting-{moment.strftime('%Y%m%d-%H%M%S')}.wav"
        )
    helper_obj = helper if helper is not None else helper_module
    supports = getattr(helper_obj, "supports_record_audio", None)
    try:
        helper_ready = bool(supports()) if callable(supports) else False
    except Exception:
        helper_ready = False
    if helper_ready:
        target = Path(audio_path)
        out_dir = target.parent / f"{target.stem}-tracks"
        return HelperAudioRecorder(out_dir, helper=helper_obj, runner=runner)
    mic_device = str(cfg.get("mic_device") or ":0")
    return FFmpegMicRecorder(audio_path, mic_device, runner=runner)


def _read_transcript(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TranscriptionError(f"could not read transcript {path}: {exc}") from exc


def _find_text_output(directory: Path, stem: str) -> Path | None:
    exact = directory / f"{stem}.txt"
    if exact.is_file():
        return exact
    candidates = sorted(
        directory.glob("*.txt"), key=lambda item: item.stat().st_mtime
    )
    return candidates[-1] if candidates else None


def _run_error(name: str, completed: Any) -> TranscriptionError:
    detail = ""
    for attribute in ("stderr", "stdout"):
        value = getattr(completed, attribute, "") or ""
        if value.strip():
            detail = str(value).strip().splitlines()[-1]
            break
    code = getattr(completed, "returncode", "?")
    suffix = f": {detail}" if detail else ""
    return TranscriptionError(f"{name} failed with exit code {code}{suffix}")


def transcribe_audio(
    audio_path: str | Path,
    config: dict[str, Any] | None = None,
    *,
    runner: Any | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Transcribe a WAV file with mlx_whisper, falling back to whisper-cli."""

    cfg = config if config is not None else config_module.load_config()
    path = Path(audio_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise TranscriptionError(f"audio file not found or empty: {path}")
    tools = runner if runner is not None else SubprocessRunner()
    out_dir = Path(output_dir) if output_dir is not None else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    model = str(
        cfg.get("whisper_model")
        or config_module.DEFAULTS.get(
            "whisper_model", "mlx-community/whisper-large-v3-turbo"
        )
    )

    if tools.which("mlx_whisper"):
        args = [
            "mlx_whisper",
            str(path),
            "--model",
            model,
            "--output-format",
            "txt",
            "--output-dir",
            str(out_dir),
        ]
        completed = tools.run(args)
        if getattr(completed, "returncode", 1) != 0:
            raise _run_error("mlx_whisper", completed)
        text_path = _find_text_output(out_dir, path.stem)
        if text_path is None:
            raise TranscriptionError(
                f"mlx_whisper finished but wrote no .txt file in {out_dir}"
            )
        return {
            "transcript": _read_transcript(text_path),
            "engine": "mlx_whisper",
            "path": str(text_path),
        }

    if tools.which("whisper-cli"):
        out_base = out_dir / path.stem
        args = [
            "whisper-cli",
            "-f",
            str(path),
            "-otxt",
            "-of",
            str(out_base),
        ]
        completed = tools.run(args)
        if getattr(completed, "returncode", 1) != 0:
            raise _run_error("whisper-cli", completed)
        text_path = Path(str(out_base) + ".txt")
        if not text_path.is_file():
            text_path = _find_text_output(out_dir, path.stem)
        if text_path is None:
            raise TranscriptionError(
                f"whisper-cli finished but wrote no .txt file in {out_dir}"
            )
        return {
            "transcript": _read_transcript(text_path),
            "engine": "whisper-cli",
            "path": str(text_path),
        }

    raise TranscriptionError(
        "no transcription tool is installed; install mlx_whisper or whisper-cli"
    )


def _find_json_output(directory: Path, stem: str) -> Path | None:
    exact = directory / f"{stem}.json"
    if exact.is_file():
        return exact
    candidates = sorted(
        directory.glob("*.json"), key=lambda item: item.stat().st_mtime
    )
    return candidates[-1] if candidates else None


def _transcribe_json(
    audio_path: Path,
    config: dict[str, Any],
    tools: Any,
    output_dir: Path,
) -> dict[str, Any]:
    """Run mlx_whisper with JSON output and return the decoded document."""

    model = str(
        config.get("whisper_model")
        or config_module.DEFAULTS.get(
            "whisper_model", "mlx-community/whisper-large-v3-turbo"
        )
    )
    if not tools.which("mlx_whisper"):
        raise TranscriptionError(
            "speaker-labelled transcription needs mlx_whisper; install it "
            "with: pip install mlx-whisper"
        )
    args = [
        "mlx_whisper",
        str(audio_path),
        "--model",
        model,
        "--output-format",
        "json",
        "--output-dir",
        str(output_dir),
    ]
    completed = tools.run(args)
    if getattr(completed, "returncode", 1) != 0:
        raise _run_error("mlx_whisper", completed)
    json_path = _find_json_output(output_dir, audio_path.stem)
    if json_path is None:
        raise TranscriptionError(
            f"mlx_whisper finished but wrote no .json file in {output_dir}"
        )
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptionError(
            f"could not read transcription {json_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptionError(
            f"transcription {json_path} did not contain a JSON object"
        )
    return payload


def _segment_time(segment: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = segment.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _format_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def merge_speaker_segments(
    mic_segments: list[dict[str, Any]],
    system_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Label mic segments as "Me" and system segments as "Them", sorted by time."""

    merged: list[dict[str, Any]] = []
    for speaker, segments in (("Me", mic_segments), ("Them", system_segments)):
        for segment in segments or []:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            merged.append(
                {
                    "speaker": speaker,
                    "start": _segment_time(segment, ("start", "start_ts", "t0")),
                    "end": _segment_time(segment, ("end", "end_ts", "t1")),
                    "text": text,
                }
            )
    merged.sort(key=lambda item: (item["start"], 0 if item["speaker"] == "Me" else 1))
    return merged


def format_speaker_transcript(segments: list[dict[str, Any]]) -> str:
    """Render merged segments as ``[hh:mm:ss] Speaker: text`` lines."""

    return "\n".join(
        f"[{_format_hms(item['start'])}] {item['speaker']}: {item['text']}"
        for item in segments
    )


def transcribe_speaker_tracks(
    system_path: str | Path,
    mic_path: str | Path,
    config: dict[str, Any] | None = None,
    *,
    runner: Any | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Transcribe the helper's two tracks and merge them with speaker labels.

    Each track goes through ``mlx_whisper --output-format json``; the mic
    track is labelled "Me" and the system track "Them", merged by timestamp.
    """

    cfg = config if config is not None else config_module.load_config()
    system = Path(system_path)
    mic = Path(mic_path)
    if not _audio_ok(system):
        raise TranscriptionError(f"system audio not found or empty: {system}")
    if not _audio_ok(mic):
        raise TranscriptionError(f"mic audio not found or empty: {mic}")
    tools = runner if runner is not None else SubprocessRunner()
    out_dir = Path(output_dir) if output_dir is not None else mic.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    mic_doc = _transcribe_json(mic, cfg, tools, out_dir)
    system_doc = _transcribe_json(system, cfg, tools, out_dir)
    segments = merge_speaker_segments(
        mic_doc.get("segments") or [],
        system_doc.get("segments") or [],
    )
    transcript = format_speaker_transcript(segments)
    if not transcript.strip():
        transcript = (
            str(mic_doc.get("text") or "").strip()
            or str(system_doc.get("text") or "").strip()
        )
    return {
        "transcript": transcript,
        "segments": segments,
        "engine": "mlx_whisper",
    }


def _sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    step = (len(items) - 1) / max(1, limit - 1)
    picked: list[dict[str, Any]] = []
    for index in range(limit):
        item = items[round(index * step)]
        if item not in picked:
            picked.append(item)
    return picked


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _captures_text(
    database: db_module.Database, start_ts: float, end_ts: float
) -> str:
    captures = database.captures_between(start_ts, end_ts)
    blocks: list[str] = []
    used = 0
    for doc in _sample(captures, MAX_CAPTURES):
        text = str(doc.get("text") or "")
        if len(text) > MAX_CAPTURE_CHARS:
            text = text[:MAX_CAPTURE_CHARS] + "…"
        title = str(doc.get("window_title") or "").strip()
        header = f"{_format_time(float(doc['ts']))} {doc.get('app') or 'Unknown'}"
        if title:
            header += f" — {title}"
        block = f"{header}\n{text}"
        if blocks and used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def generate_notes(
    meeting: dict[str, Any],
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    transcript: str | None = None,
) -> str:
    """Turn a meeting transcript plus screen memories into markdown notes."""

    cfg = config if config is not None else config_module.load_config()
    database = db if db is not None else db_module.get_default_db()
    body_text = (
        transcript
        if transcript is not None
        else str(meeting.get("transcript") or "")
    )
    if len(body_text) > MAX_TRANSCRIPT_CHARS:
        body_text = body_text[:MAX_TRANSCRIPT_CHARS] + "…"
    start_ts = float(meeting.get("start_ts") or 0.0)
    end_ts = float(meeting.get("end_ts") or start_ts)
    screen = (
        _captures_text(database, start_ts, end_ts)
        if end_ts >= start_ts and start_ts > 0
        else ""
    )
    title = str(meeting.get("title") or "Meeting")
    body = (
        f"Meeting: {title}\n"
        f"Time: {_format_time(start_ts)} to {_format_time(end_ts)}\n\n"
        f"Transcript:\n{body_text or 'No transcript was captured.'}\n\n"
        f"Screen memories during the meeting:\n"
        f"{screen or 'No screen memories were recorded during the meeting.'}\n\n"
        "Write the meeting notes."
    )
    model = provider if provider is not None else llm_module.get_provider(cfg)
    result = model.chat(
        [
            {"role": "system", "content": _NOTES_SYSTEM},
            {"role": "user", "content": body},
        ]
    )
    if isinstance(result, str):
        return result
    return "".join(str(chunk) for chunk in result)


def detect_meeting_app(
    app: str = "", bundle_id: str = "", window_title: str = ""
) -> bool:
    """Return True for known meeting apps or a Google Meet browser tab."""

    lowered_app = (app or "").strip().casefold()
    lowered_bundle = (bundle_id or "").strip().casefold()
    title = (window_title or "").strip()

    if lowered_app in MEETING_APPS:
        return True
    if any(alias in lowered_app for alias in ("zoom", "webex")):
        return True
    if any(hint in lowered_bundle for hint in MEETING_BUNDLE_HINTS):
        return True
    if _MEET_TITLE_RE.search(title):
        return True
    if "google meet" in title.casefold():
        return True
    return False


def meeting_detected(frontmost: dict[str, Any] | None) -> bool:
    """Convenience wrapper around :func:`detect_meeting_app`."""

    if not frontmost:
        return False
    return detect_meeting_app(
        str(frontmost.get("app") or ""),
        str(frontmost.get("bundle_id") or ""),
        str(frontmost.get("window_title") or ""),
    )


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_meeting(
    meeting_id: int, db: db_module.Database | None = None
) -> dict[str, Any] | None:
    database = db if db is not None else db_module.get_default_db()
    row = database.conn.execute(
        "SELECT * FROM meetings WHERE id = ?", (int(meeting_id),)
    ).fetchone()
    return _row_to_dict(row)


def list_meetings(
    db: db_module.Database | None = None, *, limit: int = 50
) -> list[dict[str, Any]]:
    database = db if db is not None else db_module.get_default_db()
    rows = database.conn.execute(
        "SELECT * FROM meetings ORDER BY start_ts DESC, id DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _insert_meeting(
    database: db_module.Database,
    title: str,
    start_ts: float,
    audio_path: str,
    status: str,
    error: str | None = None,
) -> int:
    with database.conn:
        cursor = database.conn.execute(
            """
            INSERT INTO meetings
                (title, start_ts, end_ts, audio_path, transcript, notes_md,
                 status, error)
            VALUES (?, ?, NULL, ?, '', '', ?, ?)
            """,
            (title, start_ts, audio_path, status, error),
        )
    return int(cursor.lastrowid)


def _update_meeting(
    database: db_module.Database, meeting_id: int, **fields: Any
) -> None:
    allowed = {
        "title",
        "start_ts",
        "end_ts",
        "audio_path",
        "transcript",
        "notes_md",
        "status",
        "error",
    }
    assignments = [f"{key} = ?" for key in fields if key in allowed]
    if not assignments:
        return
    values = [fields[key] for key in fields if key in allowed]
    values.append(int(meeting_id))
    with database.conn:
        database.conn.execute(
            f"UPDATE meetings SET {', '.join(assignments)} WHERE id = ?",
            values,
        )


def _parse_iso(value: Any) -> float | None:
    """Parse an ISO-8601 datetime (with or without timezone) to a timestamp."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def _calendar_title(helper: Any, now: float) -> str | None:
    """Title of the calendar event overlapping ``now``, if the helper can see one."""

    fetch = getattr(helper, "calendar_events", None)
    if not callable(fetch):
        return None
    moment = datetime.fromtimestamp(now)
    start = (moment - timedelta(hours=6)).isoformat(timespec="seconds")
    end = (moment + timedelta(hours=2)).isoformat(timespec="seconds")
    try:
        events = fetch(start, end)
    except Exception:
        return None
    overlapping: list[tuple[float, str]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        title = str(event.get("title") or "").strip()
        start_ts = _parse_iso(event.get("start"))
        end_ts = _parse_iso(event.get("end"))
        if not title or start_ts is None:
            continue
        if start_ts <= now <= (end_ts if end_ts is not None else start_ts):
            overlapping.append((start_ts, title))
    if not overlapping:
        return None
    overlapping.sort(key=lambda item: item[0])
    return overlapping[-1][1]


def _window_title(helper: Any) -> str | None:
    frontmost = getattr(helper, "frontmost", None)
    if not callable(frontmost):
        return None
    try:
        payload = frontmost()
    except Exception:
        return None
    title = str(payload.get("window_title") or "").strip()
    return title or None


def suggest_meeting_title(
    explicit: str = "",
    *,
    helper: Any | None = None,
    now: float | None = None,
) -> str:
    """Pick a meeting title: explicit > calendar event > window title > default."""

    if explicit.strip():
        return explicit.strip()
    moment = float(now if now is not None else time.time())
    helper_obj = helper if helper is not None else helper_module
    title = _calendar_title(helper_obj, moment)
    if title:
        return title
    title = _window_title(helper_obj)
    if title:
        return title
    return "Meeting " + datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M")


def start_meeting(
    title: str = "",
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    recorder: Recorder | None = None,
    *,
    now: float | None = None,
    helper: Any | None = None,
) -> dict[str, Any]:
    """Start a recording and create the meeting row."""

    cfg = config if config is not None else config_module.load_config()
    database = db if db is not None else db_module.get_default_db()
    moment = float(now if now is not None else time.time())
    active = recorder if recorder is not None else create_recorder(cfg, now=moment)
    meeting_title = suggest_meeting_title(title, helper=helper, now=moment)
    try:
        active.start()
    except Exception as exc:
        meeting_id = _insert_meeting(
            database,
            meeting_title,
            moment,
            str(active.audio_path),
            "error",
            str(exc),
        )
        result = get_meeting(meeting_id, database) or {}
        return result

    meeting_id = _insert_meeting(
        database,
        meeting_title,
        moment,
        str(active.audio_path),
        "recording",
    )
    return get_meeting(meeting_id, database) or {}


def stop_meeting(
    meeting_id: int,
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    recorder: Recorder | None = None,
    *,
    runner: Any | None = None,
    transcriber: Any | None = None,
    tracks_transcriber: Any | None = None,
    provider: Any | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Stop a recording, transcribe it, write notes and update the row."""

    cfg = config if config is not None else config_module.load_config()
    database = db if db is not None else db_module.get_default_db()
    meeting = get_meeting(meeting_id, database)
    if meeting is None:
        raise ValueError(f"meeting {meeting_id} was not found")
    moment = float(now if now is not None else time.time())

    audio_path = str(meeting.get("audio_path") or "")
    tracks: dict[str, str] | None = None
    if recorder is not None:
        try:
            audio_path = str(recorder.stop())
            tracks = getattr(recorder, "tracks", None)
        except Exception as exc:
            _update_meeting(
                database,
                meeting_id,
                end_ts=moment,
                status="error",
                error=str(exc),
            )
            return get_meeting(meeting_id, database) or {}

    _update_meeting(
        database,
        meeting_id,
        end_ts=moment,
        audio_path=audio_path,
        status="transcribing",
        error=None,
    )

    transcribe = transcriber if transcriber is not None else transcribe_audio
    transcribe_tracks = (
        tracks_transcriber
        if tracks_transcriber is not None
        else transcribe_speaker_tracks
    )
    try:
        if tracks:
            result = transcribe_tracks(
                tracks["system"], tracks["mic"], cfg, runner=runner
            )
        else:
            result = transcribe(audio_path, cfg, runner=runner)
        transcript = (
            str(result.get("transcript") or "")
            if isinstance(result, dict)
            else str(result)
        )
    except Exception as exc:
        _update_meeting(
            database, meeting_id, status="error", error=str(exc)
        )
        return get_meeting(meeting_id, database) or {}

    _update_meeting(
        database,
        meeting_id,
        transcript=transcript,
        status="transcribing",
        error=None,
    )
    updated = get_meeting(meeting_id, database) or meeting
    try:
        notes = generate_notes(
            updated, cfg, database, provider=provider, transcript=transcript
        )
    except Exception as exc:
        _update_meeting(
            database, meeting_id, status="error", error=str(exc)
        )
        return get_meeting(meeting_id, database) or {}

    _update_meeting(
        database,
        meeting_id,
        transcript=transcript,
        notes_md=notes,
        status="done",
        error=None,
    )
    return get_meeting(meeting_id, database) or {}


MicRecorder = FFmpegMicRecorder
transcribe = transcribe_audio
is_meeting_app = detect_meeting_app
detect_meeting = meeting_detected
