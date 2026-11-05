"""Load and save Magpie configuration.

The data directory is ``~/.magpie`` unless ``MAGPIE_HOME`` is set.  Configuration
lives in ``<home>/config.json``; everything else (database, screenshots, audio)
is stored beside it so the whole installation is one self-contained folder.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "config.json"

DEFAULTS: dict[str, Any] = {
    "interval_seconds": 15,
    "idle_pause_seconds": 120,
    "keep_screenshots": True,
    "retention_days": 30,
    "excluded_apps": [
        "1Password",
        "Keychain Access",
        "Bitwarden",
        "LastPass",
        "Dashlane",
    ],
    "mic_device": ":0",
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "remote_access": False,
    "remote_token": "",
    "notion_token": "",
    "notion_parent_page_id": "",
    "provider": "ollama",
    "providers": {
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "llama3.2:1b",
        },
        "openai": {
            "base_url": "https://api.openai.com",
            "api_key": "",
            "model": "gpt-4o-mini",
        },
        "anthropic": {
            "api_key": "",
            "model": "claude-sonnet-5-5",
        },
    },
}

SECRET_KEYS = frozenset({"api_key"})


class ConfigError(RuntimeError):
    """Raised when configuration cannot be read or written."""


def magpie_home() -> Path:
    """Return the Magpie data directory (``$MAGPIE_HOME`` or ``~/.magpie``)."""

    raw = os.environ.get("MAGPIE_HOME") or "~/.magpie"
    return Path(raw).expanduser()


def config_path() -> Path:
    return magpie_home() / CONFIG_FILENAME


def db_path() -> Path:
    return magpie_home() / "magpie.db"


def shots_dir() -> Path:
    return magpie_home() / "shots"


def audio_dir() -> Path:
    return magpie_home() / "audio"


def ensure_dirs() -> None:
    """Create the data directory and its well-known subdirectories."""

    home = magpie_home()
    for path in (home, shots_dir(), audio_dir()):
        path.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base`` without mutating either."""

    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def defaults() -> dict[str, Any]:
    return copy.deepcopy(DEFAULTS)


def load_config() -> dict[str, Any]:
    """Read ``config.json``, filling in any missing defaults.

    A corrupt file is reported once on stderr and defaults are returned rather
    than making the whole app unusable.
    """

    path = config_path()
    if not path.exists():
        return defaults()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"magpie: ignoring invalid config at {path}: {exc}", file=sys.stderr)
        return defaults()

    if not isinstance(data, dict):
        print(f"magpie: ignoring invalid config at {path}: expected an object", file=sys.stderr)
        return defaults()

    return _deep_merge(DEFAULTS, data)


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    """Persist ``config`` atomically and return the merged result."""

    merged = _deep_merge(DEFAULTS, config if isinstance(config, dict) else {})
    path = config_path()
    ensure_dirs()

    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".config-", suffix=".json", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ConfigError(f"could not write {path}: {exc}") from exc

    return merged


def masked_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy safe to send to the UI: secrets become ``set``/empty."""

    data = copy.deepcopy(config if config is not None else load_config())
    providers = data.get("providers")
    if isinstance(providers, dict):
        for profile in providers.values():
            if not isinstance(profile, dict):
                continue
            for key in SECRET_KEYS:
                if key in profile:
                    profile[key] = "set" if profile.get(key) else ""
    return data
