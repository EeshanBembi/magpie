from __future__ import annotations

from datetime import datetime

import pytest

from magpie import config
from magpie.db import Database
from magpie.journal import (
    auto_generate_previous_day,
    extract_loops,
    generate_journal,
    get_journal,
    heuristic_loops,
    loops_for_day,
    parse_journal_loops,
    parse_loops,
)
from magpie.llm import LLMProvider

T0 = datetime(2025, 3, 10, 9, 0).timestamp()
T1 = datetime(2025, 3, 10, 11, 0).timestamp()
DAY = "2025-03-10"


class ScriptedProvider(LLMProvider):
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, messages, stream=False):
        self.calls.append([dict(item) for item in messages])
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if stream:
            return iter([answer])
        return answer


JOURNAL_MD = (
    "## Summary\nA focused morning on the contract.\n\n"
    "## What you worked on\nSlack and the contract draft.\n\n"
    "## People & conversations\nPriya asked for the contract.\n\n"
    "## Open loops\nSend Priya the signed contract."
)


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


def add_capture(env, ts, app, title, text):
    return env["db"].insert_capture(
        ts, app, f"com.example.{app.lower()}", title, text
    )


def prompt_text(messages):
    return "\n\n".join(item["content"] for item in messages)


def seed_day(env):
    add_capture(
        env,
        T0,
        "Slack",
        "Priya Singh - DM",
        "Priya: Please send the signed contract today.",
    )
    add_capture(
        env,
        T1,
        "VS Code",
        "contract.py",
        "Writing the contract export function.",
    )


def test_generate_journal_builds_context_and_caches(env):
    seed_day(env)
    provider = ScriptedProvider([JOURNAL_MD])

    result = generate_journal(DAY, env["config"], env["db"], provider=provider)
    assert result["day"] == DAY
    assert result["summary"] == JOURNAL_MD
    assert result["cached"] is False
    assert result["model"].startswith("fake")
    assert len(provider.calls) == 1

    body = prompt_text(provider.calls[0])
    assert "App usage:" in body
    assert "Slack" in body
    assert "Priya" in body
    assert "contract.py" in body
    assert "Activity timeline:" in body
    assert "Write the journal for this day." in body
    assert "## Summary" in provider.calls[0][0]["content"]
    assert "## Open loops" in provider.calls[0][0]["content"]

    cached = generate_journal(DAY, env["config"], env["db"], provider=provider)
    assert cached["cached"] is True
    assert cached["summary"] == JOURNAL_MD
    assert len(provider.calls) == 1
    assert get_journal(DAY, env["db"])["summary"] == JOURNAL_MD


def test_generate_journal_regenerates_on_request(env):
    seed_day(env)
    provider = ScriptedProvider([JOURNAL_MD, "## Summary\nA second version."])

    first = generate_journal(DAY, env["config"], env["db"], provider=provider)
    second = generate_journal(
        DAY,
        env["config"],
        env["db"],
        provider=provider,
        regenerate=True,
    )

    assert first["summary"] == JOURNAL_MD
    assert second["summary"] == "## Summary\nA second version."
    assert second["cached"] is False
    assert len(provider.calls) == 2


def test_generate_journal_handles_an_empty_day(env):
    provider = ScriptedProvider([JOURNAL_MD])
    result = generate_journal(DAY, env["config"], env["db"], provider=provider)
    body = prompt_text(provider.calls[0])
    assert result["summary"] == JOURNAL_MD
    assert "No app usage was recorded" in body
    assert "No screen memories were recorded" in body


def test_parse_loops_clean_json():
    loops = parse_loops(
        '["Reply to Sam about the contract", "Send Priya the deck"]'
    )
    assert loops == ["Reply to Sam about the contract", "Send Priya the deck"]


def test_parse_loops_wrapped_in_prose_and_code_fence():
    reply = (
        "Here are the open loops I found:\n"
        "```json\n"
        '[{"task": "Reply to Sam about the contract"}, '
        '{"task": "Book the venue"}]\n'
        "```\n"
        "Let me know if you want more."
    )
    assert parse_loops(reply) == [
        "Reply to Sam about the contract",
        "Book the venue",
    ]


def test_parse_loops_object_with_items_and_dedupe():
    reply = (
        '{"loops": ["Email Priya", "email priya", "Update the roadmap"]}'
    )
    assert parse_loops(reply) == ["Email Priya", "Update the roadmap"]


def test_parse_loops_bullet_fallback():
    reply = (
        "Open loops:\n"
        "- Reply to Sam about the contract\n"
        "* Send Priya the deck\n"
        "2. Book the venue\n"
        "TODO: update the roadmap\n"
        "Some trailing prose."
    )
    assert parse_loops(reply) == [
        "Reply to Sam about the contract",
        "Send Priya the deck",
        "Book the venue",
        "update the roadmap",
    ]


def test_parse_loops_empty_and_garbage():
    assert parse_loops("") == []
    assert parse_loops("Nothing to do.") == []
    assert parse_loops("[]") == []


def test_extract_loops_inserts_and_dedupes(env):
    seed_day(env)
    provider = ScriptedProvider(
        [
            '["Send Priya the signed contract", '
            '"Writing the contract export function", '
            '"Contract export function"]',
            '["Send Priya the signed contract", '
            '"Writing the contract export function", '
            '"Send the signed contract today"]',
        ]
    )

    first = extract_loops(DAY, env["config"], env["db"], provider=provider)
    assert [item["text"] for item in first] == [
        "Send Priya the signed contract",
        "Writing the contract export function",
        "Contract export function",
    ]
    assert all(item["status"] == "open" for item in first)

    second = extract_loops(DAY, env["config"], env["db"], provider=provider)
    assert [item["text"] for item in second] == ["Send the signed contract today"]

    stored = loops_for_day(DAY, env["db"])
    assert len(stored) == 4
    assert {item["text"] for item in stored} == {
        "Send Priya the signed contract",
        "Writing the contract export function",
        "Contract export function",
        "Send the signed contract today",
    }


def test_extract_loops_falls_back_to_bullets(env):
    seed_day(env)
    provider = ScriptedProvider(
        [
            "- Send Priya the signed contract\n"
            "- Writing the contract export function\n"
            "- Contract export function"
        ]
    )
    inserted = extract_loops(DAY, env["config"], env["db"], provider=provider)
    assert [item["text"] for item in inserted] == [
        "Send Priya the signed contract",
        "Writing the contract export function",
        "Contract export function",
    ]


def test_extract_loops_prompt_includes_json_example(env):
    seed_day(env)
    provider = ScriptedProvider(["[]"])

    extract_loops(DAY, env["config"], env["db"], provider=provider)

    body = prompt_text(provider.calls[0])
    assert "Return ONLY a JSON array" in body
    assert '["<task> for <person>"' in body
    assert '"<task> by <day>"' in body
    assert "Never repeat" in body
    assert "Reply to Sam" not in body
    assert "Book the venue" not in body


def test_parse_journal_loops_reads_open_loops_section():
    markdown = (
        "## Summary\nBusy day.\n\n"
        "## Open loops\n"
        "- Reply to Sam about the contract\n"
        "* Send Priya the deck\n\n"
        "## Tomorrow\nNothing else."
    )
    assert parse_journal_loops(markdown) == [
        "Reply to Sam about the contract",
        "Send Priya the deck",
    ]

    paragraph = (
        "## Summary\nBusy day.\n\n"
        "## Open loops\nSend Priya the signed contract.\n"
    )
    assert parse_journal_loops(paragraph) == ["Send Priya the signed contract."]
    assert parse_journal_loops("## Summary\nNo open loops heading.") == []


def test_extract_loops_falls_back_to_journal_open_loops(env):
    seed_day(env)
    generate_journal(
        DAY,
        env["config"],
        env["db"],
        provider=ScriptedProvider([JOURNAL_MD]),
    )
    provider = ScriptedProvider(["[]"])

    inserted = extract_loops(DAY, env["config"], env["db"], provider=provider)

    assert [item["text"] for item in inserted] == [
        "Send Priya the signed contract.",
        "Priya: Please send the signed contract today.",
    ]
    assert loops_for_day(DAY, env["db"])[0]["text"] == (
        "Priya: Please send the signed contract today."
    )


def test_extract_loops_drops_prompt_examples_for_grounded_captures(env):
    start = datetime(2025, 3, 10, 9, 0).timestamp()
    capture_ids = [
        add_capture(
            env,
            start,
            "Slack",
            "Priya Singh - DM",
            "Can you send the updated contract to legal by noon?",
        ),
        add_capture(
            env,
            start + 60,
            "Mail",
            "Q4 pricing",
            "please confirm the Q4 pricing in section 3 before Friday",
        ),
        add_capture(
            env,
            start + 120,
            "VS Code",
            "notes.md",
            "# TODO: add Q4 pricing table",
        ),
    ]
    provider = ScriptedProvider(
        ['["Reply to Sam about the contract", "Book the venue by Friday"]']
    )

    inserted = extract_loops(DAY, env["config"], env["db"], provider=provider)

    texts = [item["text"] for item in inserted]
    joined = " ".join(texts).lower()
    assert "sam" not in joined
    assert "venue" not in joined
    assert "updated contract" in joined
    assert "q4 pricing in section 3" in joined
    assert "todo: add q4 pricing table" in joined
    assert all(item["source_capture_id"] in capture_ids for item in inserted)


def test_grounding_keeps_paraphrase_and_links_best_capture(env):
    start = datetime(2025, 3, 10, 9, 0).timestamp()
    contract_id = add_capture(
        env,
        start,
        "Slack",
        "Priya Singh - DM",
        "Can you send the updated contract to legal by noon?",
    )
    add_capture(
        env,
        start + 60,
        "Mail",
        "Q4 pricing",
        "please confirm the Q4 pricing in section 3 before Friday",
    )
    provider = ScriptedProvider(
        ['["Send updated contract to legal by noon"]']
    )

    inserted = extract_loops(DAY, env["config"], env["db"], provider=provider)

    texts = [item["text"] for item in inserted]
    assert "Send updated contract to legal by noon" in texts
    paraphrase = next(
        item
        for item in inserted
        if item["text"] == "Send updated contract to legal by noon"
    )
    assert paraphrase["source_capture_id"] == contract_id
    assert "please confirm the Q4 pricing in section 3 before Friday" in texts


def test_extract_loops_heuristic_fallback_over_captures(env):
    start = datetime(2025, 3, 10, 9, 0).timestamp()
    add_capture(
        env,
        start,
        "Slack",
        "Priya Singh - DM",
        "Can you send the updated contract to legal by noon?",
    )
    add_capture(
        env,
        start + 60,
        "Mail",
        "Q4 pricing",
        "please confirm the Q4 pricing in section 3 before Friday",
    )
    add_capture(
        env,
        start + 120,
        "VS Code",
        "notes.md",
        "# TODO: add Q4 pricing table",
    )
    provider = ScriptedProvider(["[]"])

    inserted = extract_loops(DAY, env["config"], env["db"], provider=provider)

    texts = [item["text"] for item in inserted]
    joined = " ".join(texts).lower()
    assert len(texts) == 3
    assert "updated contract" in joined
    assert "q4 pricing in section 3" in joined
    assert "todo: add q4 pricing table" in joined


def test_heuristic_loops_dedupes_and_caps_at_fifteen():
    captures = [
        {"text": f"Please finish item {index}. Also please finish item {index}."}
        for index in range(20)
    ]
    loops = heuristic_loops(captures)
    assert len(loops) == 15
    assert len({loop.casefold() for loop in loops}) == 15


def test_auto_generate_previous_day_only_runs_once_per_day(env):
    yesterday = datetime(2025, 3, 9, 10, 0).timestamp()
    add_capture(
        env,
        yesterday,
        "Gmail",
        "Inbox",
        "Sam asked for the revised contract.",
    )
    provider = ScriptedProvider([JOURNAL_MD])

    first = auto_generate_previous_day(
        env["config"],
        env["db"],
        provider=provider,
        now=T0,
    )
    assert first is not None
    assert first["day"] == "2025-03-09"

    second = auto_generate_previous_day(
        env["config"],
        env["db"],
        provider=provider,
        now=T0,
    )
    assert second is None
    assert len(provider.calls) == 1


def test_generate_journal_includes_calendar_events(env):
    seed_day(env)
    provider = ScriptedProvider([JOURNAL_MD])
    events = [
        {
            "title": "Weekly sync",
            "start": "2025-03-10T10:00:00",
            "end": "2025-03-10T10:30:00",
            "location": "Zoom",
            "calendar": "Work",
        }
    ]

    result = generate_journal(
        DAY,
        env["config"],
        env["db"],
        provider=provider,
        calendar_events=events,
    )
    assert result["calendar"] == events
    body = prompt_text(provider.calls[0])
    assert "Calendar events:" in body
    assert "Weekly sync" in body
    assert "10:00–10:30" in body

    cached = generate_journal(
        DAY,
        env["config"],
        env["db"],
        provider=provider,
        calendar_events=events,
    )
    assert cached["cached"] is True
    assert cached["calendar"] == events
