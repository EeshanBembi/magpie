"""Run the compiled ``magpie-helper`` Swift binary and parse its JSON output."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

HELPER_NAME = "magpie-helper"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HelperError(RuntimeError):
    """Raised when the helper is missing, crashes, or returns invalid JSON."""


def _candidates() -> list[Path]:
    candidates: list[Path] = []
    override = os.environ.get("MAGPIE_HELPER")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(
        [
            _PROJECT_ROOT / "helper" / ".build" / "release" / HELPER_NAME,
            _PROJECT_ROOT / "helper" / ".build" / "debug" / HELPER_NAME,
        ]
    )
    found = shutil.which(HELPER_NAME)
    if found:
        candidates.append(Path(found))
    return candidates


def helper_path() -> Path:
    """Locate the helper binary, preferring ``$MAGPIE_HELPER`` and release builds."""

    for candidate in _candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    searched = "\n  ".join(str(path) for path in _candidates())
    raise HelperError(
        "magpie-helper was not found or is not executable. Build it with:\n"
        "  swift build -c release --package-path helper\n"
        f"Searched:\n  {searched}"
    )


def _run_raw(*args: str, timeout: float = 30.0) -> Any:
    """Run the helper and return the decoded JSON payload of any type."""

    binary = helper_path()
    command = [str(binary), *[str(arg) for arg in args]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HelperError(
            f"magpie-helper {' '.join(args)} timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise HelperError(f"could not run {binary}: {exc}") from exc

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    payload: Any = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            detail = stderr or stdout[:500]
            raise HelperError(
                f"magpie-helper {' '.join(args)} returned invalid JSON: {detail}"
            ) from exc

    if completed.returncode != 0:
        message = None
        if isinstance(payload, dict):
            message = payload.get("error")
        raise HelperError(
            message
            or f"magpie-helper {' '.join(args)} failed with exit code "
            f"{completed.returncode}: {stderr or stdout}"
        )

    if isinstance(payload, dict) and payload.get("error"):
        raise HelperError(str(payload["error"]))
    return payload


def run(*args: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run the helper with ``args`` and return the decoded JSON object."""

    payload = _run_raw(*args, timeout=timeout)
    if not isinstance(payload, dict):
        raise HelperError(
            f"magpie-helper {' '.join(args)} returned {type(payload).__name__}, "
            "expected a JSON object"
        )
    return payload


def ocr(image_path: str | Path) -> dict[str, Any]:
    """Recognise text in an image, returning ``{"text", "lines"}``."""

    path = Path(image_path)
    if not path.is_file():
        raise HelperError(f"image not found: {path}")
    return run("ocr", str(path), timeout=120.0)


def frontmost() -> dict[str, Any]:
    """Return the frontmost app, bundle id, pid and window title."""

    return run("frontmost")


def idle() -> dict[str, Any]:
    """Return ``{"seconds": float}`` since the last keyboard/mouse input."""

    return run("idle")


def permissions() -> dict[str, Any]:
    """Return ``{"screen_recording": bool}`` for this process."""

    return run("permissions")


def idle_seconds() -> float:
    return float(idle().get("seconds", 0.0))


def has_screen_recording() -> bool:
    return bool(permissions().get("screen_recording", False))


# ---------------------------------------------------------------------------
# Phase 4 subcommands (EventKit + ScreenCaptureKit audio)
# ---------------------------------------------------------------------------

PopenFactory = Callable[[list[str]], Any]


def calendar_events(start_iso: str, end_iso: str) -> list[dict[str, Any]]:
    """Return calendar events between two ISO datetimes.

    Each event is ``{title, start, end, location, notes, calendar}``.
    """

    payload = _run_raw("calendar-events", start_iso, end_iso, timeout=30.0)
    if not isinstance(payload, list):
        raise HelperError(
            "magpie-helper calendar-events returned "
            f"{type(payload).__name__}, expected a JSON array"
        )
    return [dict(item) for item in payload if isinstance(item, dict)]


def add_reminder(
    title: str, due: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    """Create an Apple Reminders item, returning ``{"id": ...}``."""

    args = ["add-reminder", title]
    if due:
        args += ["--due", due]
    if notes:
        args += ["--notes", notes]
    return run(*args, timeout=30.0)


def add_event(
    title: str,
    start_iso: str,
    end_iso: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a Calendar event, returning ``{"id": ...}``."""

    args = ["add-event", title, start_iso, end_iso]
    if notes:
        args += ["--notes", notes]
    return run(*args, timeout=30.0)


class RecordAudioProcess:
    """Handle for a running ``magpie-helper record-audio`` subprocess.

    The helper writes ``system.wav`` and ``mic.wav`` into the output directory
    until it receives SIGINT/SIGTERM or its stdin closes, then prints the
    final ``{"system": path, "mic": path}`` JSON object on stdout.
    """

    def __init__(
        self,
        out_dir: str | Path,
        *,
        binary: str | Path | None = None,
        popen: PopenFactory | None = None,
        startup_grace: float = 0.15,
    ) -> None:
        self.out_dir = Path(out_dir)
        self._binary = Path(binary) if binary is not None else None
        self._popen_factory = popen
        self.startup_grace = float(startup_grace)
        self._process: Any | None = None
        self._stopped = False

    # -- lifecycle -----------------------------------------------------

    def _default_popen(self, command: list[str]) -> Any:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start(self) -> None:
        if self._process is not None:
            return
        binary = self._binary if self._binary is not None else helper_path()
        command = [str(binary), "record-audio", str(self.out_dir)]
        factory = self._popen_factory or self._default_popen
        try:
            self._process = factory(command)
        except OSError as exc:
            raise HelperError(f"could not start record-audio: {exc}") from exc
        self._stopped = False
        # Give the helper a moment to fail fast (permissions, bad arguments)
        # so a broken recording surfaces at meeting start, not at stop time.
        if self.startup_grace > 0:
            time.sleep(self.startup_grace)
        if self._process is not None and self._process.poll() is not None:
            raise HelperError(self._exit_error("record-audio failed to start"))

    @property
    def running(self) -> bool:
        if self._process is None or self._stopped:
            return False
        try:
            return self._process.poll() is None
        except Exception:
            return False

    # -- stopping -------------------------------------------------------

    def _exit_error(self, prefix: str) -> str:
        process = self._process
        stdout = _read_pipe(getattr(process, "stdout", None))
        stderr = _read_pipe(getattr(process, "stderr", None))
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])
        code = getattr(process, "returncode", "?") if process is not None else "?"
        detail = stderr or stdout
        suffix = f": {detail.strip()}" if detail and detail.strip() else ""
        return f"{prefix} (exit code {code}){suffix}"

    def stop(self, timeout: float = 10.0) -> dict[str, Any]:
        """Stop recording and return ``{"system": path, "mic": path}``."""

        process = self._process
        if process is None:
            raise HelperError("record-audio was not started")
        if not self._stopped:
            self._stopped = True
            _close_stdin(process)
            if not _wait_for(process, timeout):
                _send_signal(process, signal.SIGINT)
                if not _wait_for(process, timeout):
                    _send_signal(process, signal.SIGTERM)
                    if not _wait_for(process, 2.0):
                        try:
                            process.kill()
                        except (ProcessLookupError, OSError):
                            pass
                        _wait_for(process, 2.0)

        stdout = _read_pipe(getattr(process, "stdout", None)) or ""
        payload: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                payload = decoded
                break
        if payload is None:
            raise HelperError(self._exit_error("record-audio returned no JSON"))
        if payload.get("error"):
            raise HelperError(str(payload["error"]))
        return {
            "system": payload.get("system"),
            "mic": payload.get("mic"),
        }


def _read_pipe(pipe: Any) -> str | None:
    if pipe is None:
        return None
    try:
        return pipe.read()
    except (ValueError, OSError):
        return None


def _close_stdin(process: Any) -> None:
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _wait_for(process: Any, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _send_signal(process: Any, sig: int) -> None:
    try:
        process.send_signal(sig)
    except (ProcessLookupError, OSError, AttributeError):
        pass


def record_audio(
    out_dir: str | Path,
    *,
    popen: PopenFactory | None = None,
    startup_grace: float = 0.15,
) -> RecordAudioProcess:
    """Start ``magpie-helper record-audio`` and return its process handle."""

    session = RecordAudioProcess(
        out_dir, popen=popen, startup_grace=startup_grace
    )
    session.start()
    return session


_capability_cache: dict[tuple[str, str], bool] = {}


def supports(subcommand: str, *, use_cache: bool = True) -> bool:
    """Probe whether the installed helper knows ``subcommand``.

    Runs the binary with the bare subcommand and no arguments.  An
    "unknown subcommand" error means the feature is missing; a usage error
    (or a long-running process, killed here) means it is recognised.
    """

    try:
        binary = str(helper_path())
    except HelperError:
        return False
    key = (binary, subcommand)
    if use_cache and key in _capability_cache:
        return _capability_cache[key]

    supported = False
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [binary, subcommand],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5.0)
            output = f"{stdout or ''}\n{stderr or ''}".lower()
            supported = "unknown subcommand" not in output
        except subprocess.TimeoutExpired:
            # The subcommand was recognised and started doing work; stop it.
            supported = True
            _send_signal(process, signal.SIGINT)
            if not _wait_for(process, 2.0):
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
                _wait_for(process, 2.0)
    except OSError:
        supported = False
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass

    _capability_cache[key] = supported
    return supported


def supports_record_audio() -> bool:
    """True when the helper can capture system + mic audio for meetings."""

    return supports("record-audio")
