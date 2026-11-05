from __future__ import annotations

from datetime import datetime

import pytest

from magpie import config
from magpie.db import Database
from magpie.llm import LLMProvider
from magpie.rag import answer, answer_stream, keywords_from, retrieve

MORNING = datetime(2025, 3, 10, 9, 30).timestamp()
LUNCH = datetime(2025, 3, 10, 12, 30).timestamp()
AFTERNOON = datetime(2025, 3, 10, 15, 0).timestamp()
DAY = "2025-03-10"


class RecordingProvider(LLMProvider):
    name = "fake"

    def __init__(self, answer_text="Priya asked for the contract [1]"):
        self.answer_text = answer_text
        self.calls = []

    def chat(self, messages, stream=False):
        self.calls.append([dict(item) for item in messages])
        if stream:
            return iter([self.answer_text[:10], self.answer_text[10:]])
        return self.answer_text


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    database = Database(tmp_path / "magpie.db")
    cfg = config.defaults()
    cfg["provider"] = "fake"
    try:
        yield {"db": database, "config": cfg, "tmp": tmp_path}
    finally:
        database.close()


def add_capture(env, ts, app, title, text, bundle=""):
    return env["db"].insert_capture(
        ts, app, bundle or f"com.example.{app.lower()}", title, text
    )


def prompt_text(messages):
    return "\n\n".join(item["content"] for item in messages)


def test_keywords_drop_stopwords_and_duplicates():
    words = keywords_from("What did Priya ask me about the contract?")
    assert "Priya" in words
    assert "contract" in words
    assert "what" not in [word.lower() for word in words]
    assert "did" not in [word.lower() for word in words]
    assert words.count("Priya") == 1


def test_answer_returns_citations_for_a_time_question(env):
    slack_id = add_capture(
        env,
        MORNING,
        "Slack",
        "Priya Singh - DM",
        "Priya: Can you send the updated contract by noon?",
    )
    add_capture(
        env,
        AFTERNOON,
        "Safari",
        "Budget spreadsheet",
        "Quarterly budget spreadsheet is still open.",
    )
    provider = RecordingProvider()

    result = answer(
        "What did Priya ask me this morning?",
        [],
        env["config"],
        env["db"],
        provider=provider,
        now=LUNCH,
    )

    assert result["answer"] == "Priya asked for the contract [1]"
    assert result["citations"]
    assert result["citations"][0]["id"] == slack_id
    assert result["citations"][0]["app"] == "Slack"
    assert all(item["id"] == slack_id for item in result["citations"])

    messages = provider.calls[0]
    assert messages[0]["role"] == "system"
    assert "Magpie" in messages[0]["content"]
    body = prompt_text(messages)
    assert "Priya" in body
    assert "updated contract" in body
    assert "What the user was doing" in body
    assert "Time range:" in body


def test_answer_citations_are_compact_snippets(env):
    filler = "Meeting notes that are not relevant. " * 20
    full_text = filler + "Please send the updated contract to legal by noon."
    capture_id = env["db"].insert_capture(
        MORNING,
        "Slack",
        "com.tinyspeck",
        "Priya DM",
        full_text,
        image_path=env["tmp"] / "shot.jpg",
    )
    provider = RecordingProvider("Priya asked for the contract [1]")

    result = answer(
        "What did Priya ask about the contract?",
        [],
        env["config"],
        env["db"],
        provider=provider,
        now=LUNCH,
    )

    citation = next(
        item for item in result["citations"] if item["id"] == capture_id
    )
    assert set(citation) == {
        "id",
        "ts",
        "app",
        "window_title",
        "snippet",
        "has_image",
    }
    assert citation["has_image"] is True
    assert len(citation["snippet"]) <= 300
    assert "contract" in citation["snippet"].lower()
    assert "text" not in citation

    full = env["db"].get_capture(capture_id)
    assert full["text"] == full_text


def test_answer_includes_chat_history_between_system_and_question(env):
    add_capture(env, MORNING, "Slack", "Priya", "Priya asked about the contract.")
    provider = RecordingProvider()

    answer(
        "What did Priya ask?",
        [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ],
        env["config"],
        env["db"],
        provider=provider,
        now=LUNCH,
    )

    roles = [item["role"] for item in provider.calls[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert provider.calls[0][1]["content"] == "Earlier question"
    assert provider.calls[0][2]["content"] == "Earlier answer"


def test_range_fallback_samples_captures_and_timeline_when_keywords_miss(env):
    ids = [
        add_capture(
            env,
            MORNING + index * 60,
            "VS Code",
            f"file_{index}.py",
            f"Editing module number {index}",
        )
        for index in range(5)
    ]
    add_capture(
        env,
        AFTERNOON,
        "Safari",
        "Afternoon",
        "An afternoon capture that should not be cited.",
    )
    provider = RecordingProvider()

    result = answer(
        "What happened this morning?",
        [],
        env["config"],
        env["db"],
        provider=provider,
        now=LUNCH,
    )

    cited = {item["id"] for item in result["citations"]}
    assert cited <= set(ids)
    assert len(cited) == 5
    assert "VS Code" in prompt_text(provider.calls[0])


def test_retrieve_limits_and_evenly_samples_a_morning(env):
    for index in range(40):
        add_capture(
            env,
            MORNING + index * 30,
            "Safari",
            f"Page {index}",
            f"Research notes for page {index}",
        )

    retrieved = retrieve("What happened this morning?", env["db"], now=LUNCH)

    assert len(retrieved["docs"]) == 12
    assert retrieved["docs"][0]["window_title"] == "Page 0"
    assert retrieved["docs"][-1]["window_title"] == "Page 39"
    assert retrieved["timeline"]


def test_retrieve_falls_back_to_recent_without_time_or_keywords(env):
    add_capture(env, MORNING, "Safari", "Old", "An older memory")
    newest = add_capture(env, AFTERNOON, "Mail", "New", "The newest memory")

    retrieved = retrieve("", env["db"], now=AFTERNOON + 60)

    assert retrieved["docs"][0]["id"] == newest
    assert retrieved["keywords"] == []


def test_stream_yields_text_chunks_then_final_citations(env):
    capture_id = add_capture(
        env, MORNING, "Gmail", "Inbox", "Priya emailed the signed contract."
    )
    provider = RecordingProvider("The contract arrived [1]")

    events = list(
        answer_stream(
            "What did Priya send this morning?",
            [],
            env["config"],
            env["db"],
            provider=provider,
            now=LUNCH,
        )
    )

    assert all(isinstance(event, str) for event in events[:-1])
    assert "".join(events[:-1]) == "The contract arrived [1]"
    final = events[-1]
    assert isinstance(final, dict)
    assert final["done"] is True
    assert final["citations"][0]["id"] == capture_id


def test_non_streaming_provider_used_in_stream_still_yields_once(env):
    add_capture(env, MORNING, "Slack", "Priya", "Priya says hello.")

    class StringProvider(LLMProvider):
        name = "fake"

        def chat(self, messages, stream=False):
            return "One string answer [1]"

    events = list(
        answer_stream(
            "What did Priya say?",
            [],
            env["config"],
            env["db"],
            provider=StringProvider(),
            now=LUNCH,
        )
    )
    assert events[0] == "One string answer [1]"
    assert events[-1]["citations"]


def test_empty_memory_still_builds_prompt_and_citations(env):
    provider = RecordingProvider("My memory does not contain that.")
    result = answer(
        "What did Priya ask me yesterday?",
        [],
        env["config"],
        env["db"],
        provider=provider,
        now=LUNCH,
    )
    assert result["citations"] == []
    assert "none matched" in prompt_text(provider.calls[0])
