"""The ``magpie`` command-line interface.

Subcommands: ``serve``, ``capture --once``, ``search``, ``ask``, ``status``,
``pause``/``resume`` (via HTTP to the running server), ``mcp``, ``doctor``
and ``export``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

from . import capture as capture_module
from . import config as config_module
from . import db as db_module
from . import helper as helper_module
from . import llm as llm_module
from . import rag as rag_module
from . import server as server_module

DEFAULT_PORT = 7777


def _port() -> int:
    try:
        return int(os.environ.get("MAGPIE_PORT", "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def _base_url() -> str:
    return f"http://127.0.0.1:{_port()}"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    remote = bool(args.remote) or bool(config_module.load_config().get("remote_access"))
    host = "0.0.0.0" if remote else "127.0.0.1"
    print(
        f"Magpie listening on http://{host}:{args.port} "
        f"(remote access {'on — pair from Settings → Phone access' if remote else 'off'}; "
        f"capture {'on' if args.capture else 'off'})",
        file=sys.stderr,
    )
    server_module.run(args.port, capture=args.capture, remote=args.remote)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    if args.once:
        return capture_module.main(["--once"])
    return capture_module.main([])


def cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    try:
        start_ts = (
            server_module._parse_time(args.start) if args.start else None
        )
        end_ts = (
            server_module._parse_time(args.end, end=True) if args.end else None
        )
    except Exception as exc:
        detail = getattr(exc, "detail", str(exc))
        print(f"magpie search: {detail}", file=sys.stderr)
        return 2
    results = db_module.search(
        query, start_ts=start_ts, end_ts=end_ts, app=args.app, limit=args.limit
    )
    _print_json(results)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    question = " ".join(args.question).strip()
    if not question:
        print("magpie ask: a question is required", file=sys.stderr)
        return 2
    try:
        result = rag_module.answer(question, None, config_module.load_config())
    except llm_module.LLMError as exc:
        print(f"magpie ask: {exc}", file=sys.stderr)
        return 1
    print(result.get("answer") or "")
    citations = result.get("citations") or []
    if citations:
        print("\nSources:")
        from datetime import datetime

        for index, doc in enumerate(citations, start=1):
            stamp = datetime.fromtimestamp(float(doc.get("ts") or 0)).strftime(
                "%Y-%m-%d %H:%M"
            )
            app = doc.get("app") or "Unknown"
            title = (doc.get("window_title") or "").strip()
            suffix = f" — {title}" if title else ""
            print(f"  [{index}] {stamp} {app}{suffix}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        response = httpx.get(_base_url() + "/api/status", timeout=3.0)
        response.raise_for_status()
        _print_json(response.json())
        return 0
    except httpx.HTTPError:
        pass
    stats = db_module.stats()
    _print_json(
        {
            "server": "not running",
            "hint": "start it with 'magpie serve'; "
            "showing the local database instead",
            "settings": server_module.mask_settings(
                config_module.load_config()
            ),
            "counts": stats,
        }
    )
    return 0


def _post(path: str) -> tuple[int, dict[str, Any] | None]:
    try:
        response = httpx.post(_base_url() + path, timeout=5.0)
        response.raise_for_status()
        return 0, response.json()
    except httpx.HTTPError as exc:
        print(
            f"magpie: could not reach the server at {_base_url()}: {exc}\n"
            "is it running? start it with 'magpie serve'",
            file=sys.stderr,
        )
        return 1, None


def cmd_pause(args: argparse.Namespace) -> int:
    code, payload = _post("/api/pause")
    if payload is not None:
        _print_json(payload)
    return code


def cmd_resume(args: argparse.Namespace) -> int:
    code, payload = _post("/api/resume")
    if payload is not None:
        _print_json(payload)
    return code


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp_server

    mcp_server.main()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    json.dump(
        db_module.export_all(),
        sys.stdout,
        indent=2,
        ensure_ascii=False,
        default=str,
    )
    sys.stdout.write("\n")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _doctor_line(ok: bool | None, label: str, detail: str = "") -> bool:
    mark = "✓" if ok else ("!" if ok is None else "✗")
    suffix = f" — {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    return bool(ok)


def cmd_doctor(args: argparse.Namespace) -> int:
    print("Magpie doctor\n")
    healthy = True

    # 1. helper binary
    try:
        path = helper_module.helper_path()
        _doctor_line(True, "magpie-helper built", str(path))
    except helper_module.HelperError:
        healthy &= _doctor_line(
            False,
            "magpie-helper built",
            "build it with: swift build -c release --package-path helper",
        )

    # 2. screen recording permission
    try:
        granted = helper_module.has_screen_recording()
    except helper_module.HelperError:
        granted = False
    healthy &= _doctor_line(
        granted,
        "Screen Recording permission",
        "granted"
        if granted
        else "grant it in System Settings → Privacy & Security → "
        "Screen Recording for your terminal (or the Magpie app), then restart",
    )

    # 3. configured LLM provider reachable
    cfg = config_module.load_config()
    provider_name = str(cfg.get("provider") or "ollama")
    profile = cfg.get("providers", {}).get(provider_name, {})
    base_url = ""
    model = ""
    if isinstance(profile, dict):
        base_url = str(
            profile.get("base_url")
            or llm_module.DEFAULT_BASE_URLS.get(provider_name, "")
        )
        model = str(profile.get("model") or "")
    if provider_name == "fake":
        _doctor_line(True, "LLM provider", "fake provider (offline)")
    elif not base_url:
        healthy &= _doctor_line(
            False, "LLM provider", f"no base URL configured for {provider_name!r}"
        )
    else:
        reachable = False
        detail = ""
        try:
            response = httpx.get(base_url.rstrip("/") + "/api/tags", timeout=3.0)
            reachable = response.status_code < 500
            if provider_name == "ollama" and model and reachable:
                try:
                    names = {
                        str(item.get("name") or "")
                        for item in response.json().get("models", [])
                        if isinstance(item, dict)
                    }
                    if model not in names:
                        detail = f"reachable, but model {model!r} is not pulled; run: ollama pull {model}"
                        healthy &= _doctor_line(False, "LLM provider", detail)
                        reachable = True  # server is fine; model missing
                except (ValueError, AttributeError):
                    pass
        except httpx.HTTPError as exc:
            detail = f"could not reach {provider_name} at {base_url}: {exc}"
        if reachable and not detail:
            _doctor_line(
                True,
                "LLM provider",
                f"{provider_name} at {base_url} (model {model or '?'})",
            )
        elif not reachable:
            healthy &= _doctor_line(
                False,
                "LLM provider",
                detail
                + (f"; start it with 'ollama serve'"
                   if provider_name == "ollama" else ""),
            )

    # 4. ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    healthy &= _doctor_line(
        bool(ffmpeg),
        "ffmpeg",
        ffmpeg or "install it with: brew install ffmpeg",
    )

    # 5. transcription tool (mlx_whisper preferred, whisper-cli as fallback)
    mlx_whisper = shutil.which("mlx_whisper")
    whisper_cli = shutil.which("whisper-cli")
    if mlx_whisper:
        _doctor_line(True, "mlx_whisper", mlx_whisper)
    elif whisper_cli:
        healthy &= _doctor_line(
            None,
            "mlx_whisper",
            f"not installed; falling back to whisper-cli ({whisper_cli}). "
            "install with: pip install mlx-whisper",
        )
    else:
        healthy &= _doctor_line(
            False,
            "mlx_whisper",
            "no transcription tool found; install with: "
            "pip install mlx-whisper (or whisper.cpp's whisper-cli)",
        )

    # 6. data directory + database
    try:
        config_module.ensure_dirs()
        database = db_module.get_default_db()
        stats = database.stats()
        _doctor_line(
            True,
            "database",
            f"{config_module.db_path()} ({stats['captures']} captures)",
        )
    except Exception as exc:
        healthy &= _doctor_line(False, "database", str(exc))

    print()
    if healthy:
        print("All checks passed.")
        return 0
    print("Some checks failed; see the hints above.")
    return 1


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="magpie",
        description="Magpie — a private, local-first memory assistant for macOS",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the API + web UI server")
    p_serve.add_argument("--port", type=int, default=_port())
    p_serve.add_argument(
        "--no-capture",
        dest="capture",
        action="store_false",
        help="do not start the screen capture loop",
    )
    p_serve.add_argument(
        "--remote",
        action="store_true",
        help="also bind 0.0.0.0 for phone/LAN access (token or pair cookie "
        "required; see Settings → Phone access)",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_capture = sub.add_parser("capture", help="run the capture loop headless")
    p_capture.add_argument(
        "--once",
        action="store_true",
        help="take a single capture, print JSON and exit",
    )
    p_capture.set_defaults(func=cmd_capture)

    p_search = sub.add_parser("search", help="search screen memory")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("--app", default=None)
    p_search.add_argument("--start", default=None)
    p_search.add_argument("--end", default=None)
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_ask = sub.add_parser("ask", help="ask a question about your screen memory")
    p_ask.add_argument("question", nargs="+")
    p_ask.set_defaults(func=cmd_ask)

    p_status = sub.add_parser("status", help="show server (or local) status")
    p_status.set_defaults(func=cmd_status)

    p_pause = sub.add_parser("pause", help="pause capturing (running server)")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="resume capturing (running server)")
    p_resume.set_defaults(func=cmd_resume)

    p_mcp = sub.add_parser("mcp", help="run the MCP stdio server")
    p_mcp.set_defaults(func=cmd_mcp)

    p_doctor = sub.add_parser("doctor", help="check the local setup")
    p_doctor.set_defaults(func=cmd_doctor)

    p_export = sub.add_parser("export", help="export everything as JSON")
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
