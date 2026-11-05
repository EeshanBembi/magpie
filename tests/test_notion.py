from __future__ import annotations

import json

import httpx
import pytest

from magpie import config
from magpie.notion import NotionError, create_page, markdown_to_blocks


def test_markdown_to_blocks_converts_headings_bullets_and_paragraphs():
    blocks = markdown_to_blocks(
        "# Title\n\nSome prose.\n\n## Section\n- one\n* two\n1. three"
    )
    kinds = [block["type"] for block in blocks]
    assert kinds == [
        "heading_1",
        "paragraph",
        "heading_2",
        "bulleted_list_item",
        "bulleted_list_item",
        "bulleted_list_item",
    ]
    assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "Title"
    assert blocks[3]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "one"


def test_markdown_to_blocks_skips_code_fences_and_handles_long_text():
    long_text = "x" * 4500
    blocks = markdown_to_blocks(f"```\ncode\n```\n{long_text}")
    assert [block["type"] for block in blocks] == ["paragraph", "paragraph"]
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "code"
    chunks = blocks[1]["paragraph"]["rich_text"]
    assert len(chunks) >= 3
    assert "".join(chunk["text"]["content"] for chunk in chunks) == long_text


def test_create_page_posts_to_notion_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"id": "page-123", "url": "https://notion.so/page-123"},
        )

    cfg = config.defaults()
    cfg["notion_token"] = "secret-token"
    cfg["notion_parent_page_id"] = "parent-99"
    result = create_page(
        cfg,
        "My journal",
        "## Summary\nHello",
        transport=httpx.MockTransport(handler),
    )

    assert result == {
        "id": "page-123",
        "url": "https://notion.so/page-123",
    }
    assert seen["url"].endswith("/v1/pages")
    assert seen["headers"]["authorization"] == "Bearer secret-token"
    assert seen["body"]["parent"] == {"page_id": "parent-99"}
    title = seen["body"]["properties"]["title"]["title"][0]["text"]["content"]
    assert title == "My journal"
    assert [block["type"] for block in seen["body"]["children"]] == [
        "heading_2",
        "paragraph",
    ]


def test_create_page_requires_settings():
    with pytest.raises(NotionError, match="notion_token"):
        create_page({}, "Title", "Body")
    with pytest.raises(NotionError, match="notion_parent_page_id"):
        create_page({"notion_token": "t"}, "Title", "Body")


def test_create_page_surfaces_api_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"message": "parent page not found"}
        )

    cfg = {
        "notion_token": "t",
        "notion_parent_page_id": "p",
    }
    with pytest.raises(NotionError, match="parent page not found"):
        create_page(
            cfg, "Title", "Body", transport=httpx.MockTransport(handler)
        )


def test_create_page_surfaces_network_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    cfg = {
        "notion_token": "t",
        "notion_parent_page_id": "p",
    }
    with pytest.raises(NotionError, match="could not reach"):
        create_page(
            cfg, "Title", "Body", transport=httpx.MockTransport(handler)
        )
