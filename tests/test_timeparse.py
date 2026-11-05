from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from magpie.timeparse import parse_range, parse_range_and_text, strip_time_phrases

NOW = datetime(2026, 11, 4, 15, 30)  # Wednesday


def ts(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute).timestamp()


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What did I do today?", (ts(2026, 11, 4), ts(2026, 11, 5))),
        ("What happened yesterday?", (ts(2026, 11, 3), ts(2026, 11, 4))),
        ("What did I do this morning?", (ts(2026, 11, 4, 6), ts(2026, 11, 4, 12))),
        (
            "Show me this afternoon",
            (ts(2026, 11, 4, 12), ts(2026, 11, 4, 18)),
        ),
        ("What did I do tonight?", (ts(2026, 11, 4, 18), ts(2026, 11, 5))),
        (
            "What did I do this evening?",
            (ts(2026, 11, 4, 18), ts(2026, 11, 5)),
        ),
        ("What happened last night?", (ts(2026, 11, 3, 18), ts(2026, 11, 4, 6))),
        ("What did I do yesterday morning?", (ts(2026, 11, 3, 6), ts(2026, 11, 3, 12))),
        (
            "What did I do yesterday afternoon?",
            (ts(2026, 11, 3, 12), ts(2026, 11, 3, 18)),
        ),
        (
            "What did I do yesterday evening?",
            (ts(2026, 11, 3, 18), ts(2026, 11, 4)),
        ),
    ],
)
def test_day_and_part_of_day_ranges(question, expected):
    assert parse_range(question, NOW) == expected


def test_last_hour():
    expected = ((NOW - timedelta(hours=1)).timestamp(), NOW.timestamp())
    assert parse_range("What did I just do in the last hour?", NOW) == expected


def test_last_n_hours():
    expected = ((NOW - timedelta(hours=3)).timestamp(), NOW.timestamp())
    assert parse_range("Summarize the last 3 hours", NOW) == expected


def test_last_n_minutes():
    expected = ((NOW - timedelta(minutes=45)).timestamp(), NOW.timestamp())
    assert parse_range("What happened in the past 45 minutes?", NOW) == expected


def test_this_week():
    monday = (NOW - timedelta(days=NOW.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert parse_range("What did I do this week?", NOW) == (
        monday.timestamp(),
        NOW.timestamp(),
    )


def test_last_week():
    monday = (NOW - timedelta(days=NOW.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert parse_range("Summarize last week", NOW) == (
        (monday - timedelta(days=7)).timestamp(),
        monday.timestamp(),
    )


def test_weekday_most_recent_past():
    expected = (ts(2026, 11, 2), ts(2026, 11, 3))
    assert parse_range("What did I do on Monday?", NOW) == expected


def test_weekday_same_day_means_today():
    expected = (ts(2026, 11, 4), ts(2026, 11, 5))
    assert parse_range("What did I do on Wednesday?", NOW) == expected


def test_n_days_ago():
    expected = (ts(2026, 11, 1), ts(2026, 11, 2))
    assert parse_range("What did I do 3 days ago?", NOW) == expected


def test_no_range():
    assert parse_range("What did I read about vector databases?", NOW) is None


def test_strip_matched_phrase():
    assert (
        strip_time_phrases("What did I work on yesterday?", NOW)
        == "What did I work on?"
    )
    assert (
        strip_time_phrases("What happened this morning at the office?", NOW)
        == "What happened at the office?"
    )
    assert (
        strip_time_phrases("What did I do on Monday?", NOW) == "What did I do?"
    )
    assert strip_time_phrases("Summarize the last 2 hours.", NOW) == "Summarize."


def test_parse_range_and_text():
    start, end, cleaned = parse_range_and_text(
        "What did I do yesterday afternoon in the design doc?", NOW
    )
    assert start == ts(2026, 11, 3, 12)
    assert end == ts(2026, 11, 3, 18)
    assert cleaned == "What did I do in the design doc?"


def test_accepts_posix_timestamp():
    assert parse_range("today", NOW.timestamp()) == (
        ts(2026, 11, 4),
        ts(2026, 11, 5),
    )
