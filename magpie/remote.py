"""Phone / LAN access: bearer token, pairing code, LAN URLs and QR codes.

Remote access is off by default.  When enabled (the ``remote_access`` setting
or ``magpie serve --remote``) the server binds to ``0.0.0.0`` and every
non-loopback request must present ``Authorization: Bearer <token>`` or the
``magpie_token`` cookie issued by ``GET /pair?code=<6 digits>``.  The pairing
code and QR code are shown in Settings so a phone on the same network (or
Tailscale) can pair by scanning.
"""

from __future__ import annotations

import io
import secrets
import socket
from typing import Any

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})
COOKIE_NAME = "magpie_token"


def generate_token() -> str:
    """A fresh bearer token for remote sessions."""

    return secrets.token_urlsafe(24)


def generate_pair_code() -> str:
    """A fresh 6-digit pairing code shown on the Mac."""

    return f"{secrets.randbelow(1_000_000):06d}"


def ensure_token(config: dict[str, Any]) -> str:
    """Return the configured token, generating one in-memory if absent."""

    token = str(config.get("remote_token") or "").strip()
    if token:
        return token
    token = generate_token()
    config["remote_token"] = token
    return token


def rotate(config: dict[str, Any]) -> dict[str, str]:
    """Rotate both the bearer token and the pairing code."""

    token = generate_token()
    config["remote_token"] = token
    return {"token": token, "pair_code": generate_pair_code()}


def is_loopback(host: str | None) -> bool:
    return bool(host) and str(host).strip().lower() in LOOPBACK_HOSTS


def lan_addresses() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""

    addresses: set[str] = set()
    sock: socket.socket | None = None
    try:
        # UDP "connect" sends no packets; it just picks the outbound interface.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("192.0.2.1", 80))  # TEST-NET-1: never actually contacted
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            addresses.add(ip)
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                addresses.add(ip)
    except OSError:
        pass
    return sorted(addresses)


def qr_svg(data: str) -> str:
    """Render ``data`` as a standalone SVG QR code using segno."""

    import segno

    qr = segno.make(data, error="m")
    buffer = io.BytesIO()
    qr.save(
        buffer,
        kind="svg",
        xmldecl=False,
        svgclass=None,
        lineclass=None,
        dark="#111111",
        light=None,
        scale=4,
        border=2,
    )
    return buffer.getvalue().decode("utf-8")


def remote_state(
    config: dict[str, Any],
    pair_code: str,
    *,
    port: int,
) -> dict[str, Any]:
    """The payload for ``GET /api/remote``."""

    enabled = bool(config.get("remote_access"))
    urls = [f"http://{ip}:{port}/" for ip in lan_addresses()]
    svg: str | None = None
    if enabled and urls and pair_code:
        pair_url = f"{urls[0]}pair?code={pair_code}"
        try:
            svg = qr_svg(pair_url)
        except Exception:
            svg = None
    return {
        "enabled": enabled,
        "urls": urls,
        "pair_code": pair_code,
        "qr_svg": svg,
    }
