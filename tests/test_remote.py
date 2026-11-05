from __future__ import annotations

from magpie import remote


def test_generate_token_and_pair_code():
    token = remote.generate_token()
    code = remote.generate_pair_code()
    assert len(token) >= 20
    assert token != remote.generate_token()
    assert len(code) == 6 and code.isdigit()


def test_ensure_token_generates_once_and_rotate_replaces():
    cfg = {}
    token = remote.ensure_token(cfg)
    assert token
    assert cfg["remote_token"] == token
    assert remote.ensure_token(cfg) == token

    rotated = remote.rotate(cfg)
    assert rotated["token"] == cfg["remote_token"]
    assert rotated["token"] != token
    assert len(rotated["pair_code"]) == 6


def test_is_loopback():
    assert remote.is_loopback("127.0.0.1")
    assert remote.is_loopback("::1")
    assert remote.is_loopback("localhost")
    assert not remote.is_loopback("192.168.1.23")
    assert not remote.is_loopback(None)


def test_qr_svg_contains_svg_markup():
    svg = remote.qr_svg("http://192.168.1.23:7777/pair?code=123456")
    assert "<svg" in svg
    assert "123456" not in svg  # it is encoded as modules, not plain text
    assert svg.strip().endswith("</svg>")


def test_remote_state_disabled_has_no_qr(monkeypatch):
    monkeypatch.setattr(remote, "lan_addresses", lambda: ["192.168.1.23"])
    state = remote.remote_state({}, "123456", port=7777)
    assert state == {
        "enabled": False,
        "urls": ["http://192.168.1.23:7777/"],
        "pair_code": "123456",
        "qr_svg": None,
    }


def test_remote_state_enabled_builds_pair_qr(monkeypatch):
    monkeypatch.setattr(remote, "lan_addresses", lambda: ["192.168.1.23"])
    state = remote.remote_state(
        {"remote_access": True}, "654321", port=7777
    )
    assert state["enabled"] is True
    assert state["pair_code"] == "654321"
    assert state["qr_svg"] and "<svg" in state["qr_svg"]
