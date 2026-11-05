from __future__ import annotations

import json

from magpie import config


def test_defaults_have_expected_values():
    data = config.defaults()
    assert data["interval_seconds"] == 15
    assert data["idle_pause_seconds"] == 120
    assert data["keep_screenshots"] is True
    assert data["retention_days"] == 30
    assert "1Password" in data["excluded_apps"]
    assert "Messages" not in data["excluded_apps"]
    assert data["providers"]["ollama"]["model"] == "llama3.2:1b"
    assert data["providers"]["openai"]["model"] == "gpt-4o-mini"
    assert data["providers"]["anthropic"]["model"] == "claude-sonnet-5-5"


def test_magpie_home_env_override(monkeypatch, tmp_path):
    home = tmp_path / "custom-home"
    monkeypatch.setenv("MAGPIE_HOME", str(home))
    assert config.magpie_home() == home
    assert config.config_path() == home / "config.json"
    assert config.db_path() == home / "magpie.db"
    assert config.shots_dir() == home / "shots"
    assert config.audio_dir() == home / "audio"


def test_load_missing_returns_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    assert config.load_config() == config.defaults()


def test_save_and_load_deep_merge(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    partial = {"providers": {"openai": {"api_key": "secret-key"}}}
    saved = config.save_config(partial)

    assert config.config_path().is_file()
    assert saved["providers"]["openai"]["api_key"] == "secret-key"
    assert saved["providers"]["openai"]["base_url"] == "https://api.openai.com"
    assert saved["providers"]["ollama"]["model"] == "llama3.2:1b"
    assert saved["interval_seconds"] == 15

    loaded = config.load_config()
    assert loaded == saved
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk["providers"]["openai"]["api_key"] == "secret-key"


def test_masked_config_hides_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    original = config.defaults()
    original["providers"]["openai"]["api_key"] = "sk-secret"
    original["providers"]["anthropic"]["api_key"] = ""

    masked = config.masked_config(original)
    assert masked["providers"]["openai"]["api_key"] == "set"
    assert masked["providers"]["anthropic"]["api_key"] == ""
    assert original["providers"]["openai"]["api_key"] == "sk-secret"


def test_invalid_config_falls_back_to_defaults(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{not valid json")
    monkeypatch.setenv("MAGPIE_HOME", str(home))

    assert config.load_config() == config.defaults()
    assert "invalid config" in capsys.readouterr().err


def test_ensure_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    config.ensure_dirs()
    assert config.magpie_home().is_dir()
    assert config.shots_dir().is_dir()
    assert config.audio_dir().is_dir()
