"""Send Markdown documents to Notion as new pages.

Optional integration: it only activates when ``notion_token`` and
``notion_parent_page_id`` are configured.  The page is created under the
configured parent page via the Notion API; Markdown is converted to Notion
paragraph / heading / bulleted-list blocks.  Everything else stays local.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

NOTION_API_BASE = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"
MAX_TEXT_CHUNK = 1900  # Notion rich_text content limit is 2000 chars


class NotionError(RuntimeError):
    """The Notion settings are missing or the API call failed."""


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def _rich_text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": chunk}} for chunk in _chunks(content)]


def _chunks(text: str) -> list[str]:
    text = text or ""
    if not text:
        return [""]
    return [text[index : index + MAX_TEXT_CHUNK] for index in range(0, len(text), MAX_TEXT_CHUNK)]


def _block(kind: str, content: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": kind,
        kind: {"rich_text": _rich_text(content)},
    }


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert Markdown to Notion paragraph / heading / bulleted blocks.

    Supports ``#``/``##``/``###`` headings, ``-``/``*``/``•`` and numbered
    bullets; everything else becomes a paragraph.  Code fences are kept as
    plain paragraph text (fences removed).
    """

    blocks: list[dict[str, Any]] = []
    in_fence = False
    for raw_line in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not line.strip():
            continue
        heading = _HEADING_RE.match(line)
        if heading and not in_fence:
            level = min(3, len(heading.group(1)))
            blocks.append(_block(f"heading_{level}", heading.group(2).strip()))
            continue
        bullet = _BULLET_RE.match(line)
        if bullet and not in_fence:
            blocks.append(_block("bulleted_list_item", bullet.group(1).strip()))
            continue
        blocks.append(_block("paragraph", line))
    return blocks


def create_page(
    config: dict[str, Any],
    title: str,
    markdown: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Create a Notion page under the configured parent page.

    Returns ``{"id": ..., "url": ...}``.  Raises :class:`NotionError` with a
    clear message when the token or parent page id is missing, the API cannot
    be reached, or it rejects the request.
    """

    token = str(config.get("notion_token") or "").strip()
    if not token:
        raise NotionError(
            "Notion is not configured: add your notion_token in Settings"
        )
    parent = str(config.get("notion_parent_page_id") or "").strip()
    if not parent:
        raise NotionError(
            "Notion is not configured: add your notion_parent_page_id in Settings"
        )

    blocks = markdown_to_blocks(markdown)
    payload: dict[str, Any] = {
        "parent": {"page_id": parent},
        "properties": {
            "title": {"title": _rich_text(title or "Magpie note")},
        },
        "children": blocks[:100],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                f"{NOTION_API_BASE}/v1/pages", json=payload, headers=headers
            )
    except httpx.HTTPError as exc:
        raise NotionError(f"could not reach the Notion API: {exc}") from exc

    if response.status_code >= 400:
        message = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                message = str(data.get("message") or "")
        except ValueError:
            message = response.text[:200]
        raise NotionError(
            f"the Notion API rejected the page ({response.status_code})"
            + (f": {message}" if message else "")
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise NotionError("the Notion API returned an invalid response") from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise NotionError("the Notion API response did not include a page id")
    return {"id": str(data.get("id")), "url": str(data.get("url") or "")}
