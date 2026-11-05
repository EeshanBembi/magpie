"""Capture what is on screen, OCR it, redact secrets and store it.

The capture loop is deliberately tiny and injectable: the screenshot source and
the Swift helper can both be replaced, which keeps tests entirely offline (no
real screen and no network).  :class:`CaptureLoop` runs the same pipeline on a
background thread for the server, while :func:`capture_once` is the headless
entry point used by ``magpie capture --once``.
"""

from __future__ import annotations

import argparse
import difflib
import inspect
import json
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from . import config as config_module
from . import db as db_module
from . import helper as helper_module
from .redact import redact

MAX_IMAGE_WIDTH = 1600
DEFAULT_INTERVAL_SECONDS = float(
    config_module.DEFAULTS.get("interval_seconds", 15)
)
DEFAULT_IDLE_PAUSE_SECONDS = float(
    config_module.DEFAULTS.get("idle_pause_seconds", 120)
)
DEFAULT_RETENTION_DAYS = int(config_module.DEFAULTS.get("retention_days", 30))
PRUNE_INTERVAL_SECONDS = 60 * 60
SIMILARITY_THRESHOLD = 0.90
BLACK_LEVEL = 8

PRIVATE_TITLE_PATTERNS = (
    "private browsing",
    "incognito",
    "inprivate",
)

ScreenshotSource = Callable[[Path], Any]


class CaptureError(RuntimeError):
    """A capture could not be completed (helper, OCR or image problem)."""


class ScreenshotError(CaptureError):
    """The screenshot itself failed or was unusable (often permissions)."""


def macos_screenshot(dest: str | Path) -> Path:
    """Take a JPEG screenshot of the main display with ``screencapture``."""

    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            ["screencapture", "-x", "-C", "-t", "jpg", str(path)],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScreenshotError("screencapture timed out") from exc
    except OSError as exc:
        raise ScreenshotError(f"could not run screencapture: {exc}") from exc

    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode != 0:
        raise ScreenshotError(
            detail or f"screencapture failed with exit code {completed.returncode}"
        )
    if not path.is_file() or path.stat().st_size == 0:
        raise ScreenshotError(
            "screencapture produced no image; grant Screen Recording permission "
            "to the terminal or Magpie"
        )
    return path


def is_excluded(
    config: dict[str, Any], app: str, bundle_id: str | None = None
) -> bool:
    """Return True when ``app`` or ``bundle_id`` is in ``excluded_apps``."""

    excluded = {
        str(item).strip().casefold()
        for item in config.get("excluded_apps", [])
        if str(item).strip()
    }
    if not excluded:
        return False
    return (
        (app or "").strip().casefold() in excluded
        or (bundle_id or "").strip().casefold() in excluded
    )


def is_private_title(window_title: str | None) -> bool:
    """Return True for private/incognito browser windows."""

    lowered = (window_title or "").casefold()
    return any(pattern in lowered for pattern in PRIVATE_TITLE_PATTERNS)


def texts_are_similar(previous: str | None, current: str | None) -> bool:
    """Return True when two screen texts are at least 90% similar."""

    before = db_module.normalize_text(previous or "")
    after = db_module.normalize_text(current or "")
    if before == after:
        return True
    if not before or not after:
        return False
    return (
        difflib.SequenceMatcher(None, before, after, autojunk=False).quick_ratio()
        >= SIMILARITY_THRESHOLD
    )


def _read_idle(helper: Any) -> float | None:
    try:
        if hasattr(helper, "idle_seconds"):
            return float(helper.idle_seconds())
        payload = helper.idle()
        return float(payload.get("seconds", 0.0))
    except Exception:
        return None


def _call_source(source: Any, dest: Path) -> Any:
    """Call a screenshot source that takes ``dest`` or no arguments."""

    target = source.capture if hasattr(source, "capture") else source
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            and parameter.default is parameter.empty
        ]
        if not required:
            return target()
    return target(dest)


def _take_screenshot(source: ScreenshotSource | Any, dest: Path) -> Path:
    result = _call_source(source, dest)

    if isinstance(result, Image.Image):
        result.save(dest, format="JPEG")
        result = dest
    elif isinstance(result, (bytes, bytearray)):
        dest.write_bytes(bytes(result))
        result = dest

    if result is None:
        result = dest

    path = Path(result)
    if not path.is_file():
        raise ScreenshotError("screenshot source produced no image")
    if path.stat().st_size == 0:
        raise ScreenshotError("screenshot source produced an empty image")
    return path


def _image_is_black(image: Image.Image) -> bool:
    try:
        low, high = image.convert("L").getextrema()
    except Exception:
        return False
    del low
    return high <= BLACK_LEVEL


def _prepare_image(image: Image.Image) -> Image.Image:
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if image.width > MAX_IMAGE_WIDTH:
        height = max(1, round(image.height * MAX_IMAGE_WIDTH / image.width))
        image = image.resize((MAX_IMAGE_WIDTH, height), Image.Resampling.LANCZOS)
    return image


def _shot_path(
    shots_root: str | Path | None, timestamp: float, capture_id: int
) -> Path:
    root = Path(shots_root) if shots_root is not None else config_module.shots_dir()
    day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    return root / day / f"{capture_id}.jpg"


def _setting(config: dict[str, Any], key: str, default: Any) -> Any:
    if key in config:
        return config[key]
    return config_module.DEFAULTS.get(key, default)


def _result(
    *,
    timestamp: float,
    interval: float,
    status: str,
    app: str = "",
    bundle_id: str = "",
    window_title: str = "",
    capture_id: int | None = None,
    image_path: str | None = None,
    text: str | None = None,
    redactions: dict[str, int] | None = None,
    width: int = 0,
    height: int = 0,
    error: str | None = None,
    permission_problem: bool = False,
) -> dict[str, Any]:
    return {
        "ok": status in ("captured", "deduped", "skipped"),
        "status": status,
        "captured": status == "captured",
        "stored": status == "captured",
        "deduped": status == "deduped",
        "skipped": status == "skipped",
        "reason": error,
        "error": error,
        "permission_problem": permission_problem,
        "ts": timestamp,
        "app": app,
        "bundle_id": bundle_id,
        "window_title": window_title,
        "capture_id": capture_id,
        "image_path": image_path,
        "text": text,
        "redactions": redactions or {},
        "width": width,
        "height": height,
        "duration": interval,
    }


def capture_once(
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    screenshot_source: ScreenshotSource | Any | None = None,
    helper: Any | None = None,
    *,
    now: float | None = None,
    paused: bool = False,
    shots_root: str | Path | None = None,
    force: bool = False,
    interval_seconds: float | None = None,
) -> dict[str, Any]:
    """Run one capture cycle and return a JSON-friendly result.

    ``screenshot_source`` is either a callable receiving a destination path or
    an object with a ``capture(dest)`` method.  ``helper`` is the
    :mod:`magpie.helper` module or any object with the same ``frontmost``,
    ``idle`` and ``ocr`` methods.  Neither touches real hardware in tests.
    """

    cfg = config if config is not None else config_module.load_config()
    database = db if db is not None else db_module.get_default_db()
    helper_obj = helper if helper is not None else helper_module
    source = screenshot_source if screenshot_source is not None else macos_screenshot
    timestamp = float(now if now is not None else time.time())
    interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else float(_setting(cfg, "interval_seconds", DEFAULT_INTERVAL_SECONDS))
    )

    if paused:
        return _result(
            timestamp=timestamp, interval=interval, status="skipped", error=None
        )

    if not force:
        idle = _read_idle(helper_obj)
        idle_limit = float(
            _setting(cfg, "idle_pause_seconds", DEFAULT_IDLE_PAUSE_SECONDS)
        )
        if idle is not None and idle > idle_limit:
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="skipped",
                error=f"idle for {idle:.0f}s",
            )

    try:
        front = helper_obj.frontmost()
    except Exception as exc:
        return _result(
            timestamp=timestamp,
            interval=interval,
            status="error",
            error=f"could not read the frontmost app: {exc}",
        )

    app = str(front.get("app") or "")
    bundle_id = str(front.get("bundle_id") or "")
    window_title = str(front.get("window_title") or "")

    if is_excluded(cfg, app, bundle_id):
        return _result(
            timestamp=timestamp,
            interval=interval,
            status="skipped",
            app=app,
            bundle_id=bundle_id,
            window_title=window_title,
            error="excluded app",
        )
    if is_private_title(window_title):
        return _result(
            timestamp=timestamp,
            interval=interval,
            status="skipped",
            app=app,
            bundle_id=bundle_id,
            window_title=window_title,
            error="private browsing window",
        )

    with tempfile.TemporaryDirectory(prefix="magpie-capture-") as temp_dir:
        raw_path = Path(temp_dir) / "raw.jpg"
        try:
            shot_path = _take_screenshot(source, raw_path)
        except Exception as exc:
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="permission",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                error=str(exc),
                permission_problem=True,
            )

        try:
            image = Image.open(shot_path)
            image.load()
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="permission",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                error=f"screenshot could not be read: {exc}",
                permission_problem=True,
            )

        if _image_is_black(image):
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="permission",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                error=(
                    "screenshot is black; grant Screen Recording permission to "
                    "the terminal or Magpie"
                ),
                permission_problem=True,
            )

        try:
            processed = _prepare_image(image)
            ocr_path = Path(temp_dir) / "ocr.jpg"
            processed.save(ocr_path, "JPEG", quality=90)
        except Exception as exc:
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="error",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                error=f"could not prepare screenshot: {exc}",
            )

        try:
            ocr = helper_obj.ocr(ocr_path)
        except Exception as exc:
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="error",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                error=f"OCR failed: {exc}",
            )

        raw_text = str(ocr.get("text") or "")
        cleaned, redactions = redact(raw_text)
        digest = db_module.hash_text(cleaned)

        previous = database.last_capture(app, window_title)
        if previous is not None and texts_are_similar(previous.get("text"), cleaned):
            database.record_activity(
                timestamp,
                app,
                bundle_id,
                window_title,
                1,
                interval,
            )
            return _result(
                timestamp=timestamp,
                interval=interval,
                status="deduped",
                app=app,
                bundle_id=bundle_id,
                window_title=window_title,
                text=cleaned,
                redactions=redactions,
                width=processed.width,
                height=processed.height,
            )

        capture_id = database.insert_capture(
            timestamp,
            app,
            bundle_id,
            window_title,
            cleaned,
            image_path=None,
            width=processed.width,
            height=processed.height,
            text_hash=digest,
            interval_seconds=interval,
        )

        image_path: str | None = None
        if bool(_setting(cfg, "keep_screenshots", True)):
            final_path = _shot_path(shots_root, timestamp, capture_id)
            try:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                processed.save(final_path, "JPEG", quality=60)
                database.set_capture_image(capture_id, final_path)
                image_path = str(final_path)
            except Exception as exc:
                database.set_capture_image(capture_id, None)
                return _result(
                    timestamp=timestamp,
                    interval=interval,
                    status="captured",
                    app=app,
                    bundle_id=bundle_id,
                    window_title=window_title,
                    capture_id=capture_id,
                    text=cleaned,
                    redactions=redactions,
                    width=processed.width,
                    height=processed.height,
                    error=f"could not save screenshot: {exc}",
                )

        return _result(
            timestamp=timestamp,
            interval=interval,
            status="captured",
            app=app,
            bundle_id=bundle_id,
            window_title=window_title,
            capture_id=capture_id,
            image_path=image_path,
            text=cleaned,
            redactions=redactions,
            width=processed.width,
            height=processed.height,
        )


class CaptureLoop:
    """Background-thread capture runner used by the server."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db: db_module.Database | None = None,
        screenshot_source: ScreenshotSource | Any | None = None,
        helper: Any | None = None,
        *,
        shots_root: str | Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config if config is not None else config_module.load_config()
        self.db = db
        self.screenshot_source = screenshot_source
        self.helper = helper
        self.shots_root = shots_root
        self._clock = clock if clock is not None else time.time
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self._state_lock = threading.Lock()
        self._last_prune = 0.0
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.permission_problem = False
        self.last_capture_ts: float | None = None
        self.captures = 0
        self.dedupes = 0

    # -- controls ------------------------------------------------------

    @property
    def interval(self) -> float:
        return float(_setting(self.config, "interval_seconds", DEFAULT_INTERVAL_SECONDS))

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def start(self) -> threading.Thread:
        """Start the background thread (idempotent)."""

        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="magpie-capture",
                daemon=True,
            )
            self._thread.start()
            return self._thread

    def stop(self, timeout: float | None = 5.0) -> None:
        """Ask the background thread to stop and wait for it to finish."""

        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)

    def run_forever(self) -> None:
        """Run the capture loop on the calling thread."""

        self._run()

    # -- loop ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.capture_once()
            except Exception as exc:  # never let the capture loop kill the server
                with self._state_lock:
                    self.last_error = str(exc)
            self._maybe_prune()
            if self._stop_event.wait(max(0.0, self.interval)):
                break

    def _maybe_prune(self) -> None:
        now = float(self._clock())
        if self._last_prune and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        database = self.db if self.db is not None else db_module.get_default_db()
        try:
            retention = int(
                _setting(self.config, "retention_days", DEFAULT_RETENTION_DAYS)
            )
            database.prune(retention, now=now)
        except Exception as exc:
            with self._state_lock:
                self.last_error = f"prune failed: {exc}"

    def capture_once(self, force: bool = False) -> dict[str, Any]:
        """Run one cycle using this loop's configuration and update status."""

        timestamp = float(self._clock())
        result = capture_once(
            self.config,
            self.db,
            screenshot_source=self.screenshot_source,
            helper=self.helper,
            now=timestamp,
            paused=self._paused,
            shots_root=self.shots_root,
            force=force,
        )
        with self._state_lock:
            self.last_result = result
            self.last_error = result.get("error") if result.get("status") == "error" else None
            self.permission_problem = bool(result.get("permission_problem"))
            if result.get("captured"):
                self.captures += 1
                self.last_capture_ts = float(result["ts"])
            elif result.get("deduped"):
                self.dedupes += 1
                self.last_capture_ts = float(result["ts"])
        return result

    def status(self) -> dict[str, Any]:
        """Return a compact status snapshot for the API/UI."""

        with self._state_lock:
            return {
                "running": self.running,
                "capturing": self.running and not self._paused,
                "paused": self._paused,
                "interval_seconds": self.interval,
                "last_capture_ts": self.last_capture_ts,
                "captures": self.captures,
                "dedupes": self.dedupes,
                "permission_problem": self.permission_problem,
                "last_error": self.last_error,
                "last_result": self.last_result,
            }


CaptureRunner = CaptureLoop


def main(argv: list[str] | None = None) -> int:
    """Small headless entry point: ``python -m magpie.capture --once``."""

    parser = argparse.ArgumentParser(description="Magpie capture loop")
    parser.add_argument(
        "--once", action="store_true", help="capture once, print JSON, then exit"
    )
    args = parser.parse_args(argv)

    if args.once:
        result = capture_once()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1

    loop = CaptureLoop()
    loop.start()
    try:
        while loop.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        loop.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the CLI
    raise SystemExit(main())
