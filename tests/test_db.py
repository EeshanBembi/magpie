from __future__ import annotations

from datetime import datetime

import pytest

from magpie.db import (
    CAPTURE_INTERVAL_SECONDS,
    Database,
    day_bounds,
    fts_query,
    hash_text,
    normalize_text,
)

T0 = datetime(2025, 3, 10, 12, 0).timestamp()
DAY = "2025-03-10"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "magpie.db")
    try:
        yield database
    finally:
        database.close()


def test_normalize_and_hash():
    assert normalize_text("  Hello\n\tWorld  ") == "hello world"
    assert normalize_text("") == ""
    assert hash_text("Hello   World") == hash_text("hello world")
    assert hash_text("a") != hash_text("b")
    assert len(hash_text("a")) == 40


def test_insert_and_get_capture(db):
    capture_id = db.insert_capture(
        ts=T0,
        app="Safari",
        bundle_id="com.apple.Safari",
        window_title="Quarterly budget review",
        text="The quarterly budget review is due Friday.",
        image_path=None,
        width=1440,
        height=900,
    )
    row = db.get_capture(capture_id)
    assert row is not None
    assert row["app"] == "Safari"
    assert row["window_title"] == "Quarterly budget review"
    assert row["text_hash"] == hash_text(row["text"])
    assert row["width"] == 1440
    assert db.last_capture("Safari")["id"] == capture_id
    assert db.get_capture(999999) is None


def test_search_hits_and_snippet(db):
    db.insert_capture(
        T0,
        "Safari",
        "com.apple.Safari",
        "Quarterly budget review",
        "The quarterly budget review is due Friday. Ask Sam for numbers.",
    )
    results = db.search("budget")
    assert len(results) == 1
    row = results[0]
    assert row["app"] == "Safari"
    assert row["rank"] is not None
    assert "budget" in row["snippet"].lower()
    assert "<mark>" in row["snippet"]

    prefix = db.search("budg")
    assert len(prefix) == 1


def test_search_collapses_near_duplicate_captures(db):
    text = "The quarterly budget review is due Friday. Ask Sam for the numbers."
    first = db.insert_capture(T0, "Safari", "", "Budget", text)
    second = db.insert_capture(
        T0 + 5, "Safari", "", "Budget", text + " Sam replied."
    )
    fourth = db.insert_capture(
        T0 + 11 * 60,
        "Safari",
        "",
        "Budget",
        text + " Eleven minutes later.",
    )

    results = db.search("budget")
    assert len(results) == 2

    grouped = [row for row in results if row["id"] in (first, second)]
    assert len(grouped) == 1
    assert grouped[0]["similar_count"] == 2

    later = next(row for row in results if row["id"] == fourth)
    assert later["similar_count"] == 1


def test_search_collapse_keeps_different_apps_or_titles(db):
    text = "Please send the updated contract by noon."
    db.insert_capture(T0, "Slack", "", "Priya", text)
    db.insert_capture(T0 + 5, "Mail", "", "Priya", text)
    db.insert_capture(T0 + 10, "Slack", "", "Someone else", text)

    results = db.search("contract")
    assert len(results) == 3
    assert all(row["similar_count"] == 1 for row in results)


def test_search_filters(db):
    db.insert_capture(T0, "Safari", "", "Doc", "budget planning notes")
    db.insert_capture(T0 + 100, "Chrome", "", "Sheet", "budget spreadsheet")
    db.insert_capture(T0 + 200, "Safari", "", "Other", "holiday photos")

    assert len(db.search("budget")) == 2
    assert len(db.search("budget", app="Safari")) == 1
    assert len(db.search("budget", start_ts=T0 + 50)) == 1
    assert len(db.search("budget", end_ts=T0 + 50)) == 1
    assert db.search("budget", app="Mail") == []
    assert db.search("budget", limit=1)[0]["ts"] == T0 + 100


@pytest.mark.parametrize(
    "query",
    [
        '"',
        "AND (",
        "*",
        "OR",
        "NEAR(",
        "foo AND (bar OR",
        '"; DROP TABLE captures; --',
        "a*",
        "-",
        "()",
        "text:budget",
        "'",
    ],
)
def test_hostile_fts_input_never_raises(db, query):
    db.insert_capture(T0, "Safari", "", "Budget", "budget planning notes")
    results = db.search(query)
    assert isinstance(results, list)
    # The table is still there after any scary-looking query.
    assert db.stats()["captures"] == 1


def test_fts_query_is_sanitised():
    assert fts_query("") == ""
    assert fts_query('" AND (') == '"AND"*'
    assert fts_query("foo bar") == '"foo" OR "bar"*'
    assert fts_query("foo bar baz") == '"foo" OR "bar" OR "baz"*'


def test_activity_merging(db):
    db.insert_capture(T0, "Safari", "", "Mail", "one")
    db.insert_capture(T0 + 60, "Safari", "", "Mail", "two")
    db.insert_capture(T0 + 120, "Safari", "", "Mail", "three")
    activities = db.timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["start_ts"] == T0
    assert activities[0]["end_ts"] == T0 + 120 + CAPTURE_INTERVAL_SECONDS
    assert activities[0]["capture_count"] == 3

    # A gap over five minutes starts a new activity.
    db.insert_capture(T0 + 500, "Safari", "", "Mail", "four")
    activities = db.timeline(DAY)
    assert len(activities) == 2
    assert activities[-1]["start_ts"] == T0 + 500
    assert activities[-1]["end_ts"] == T0 + 500 + CAPTURE_INTERVAL_SECONDS
    assert activities[-1]["capture_count"] == 1

    # Switching window starts a new activity even within the gap.
    db.insert_capture(T0 + 560, "Safari", "", "Calendar", "five")
    assert len(db.timeline(DAY)) == 3


def test_timeline_and_app_usage(db):
    db.insert_capture(T0, "Safari", "", "Docs", "writing")
    db.insert_capture(T0 + 60, "Safari", "", "Docs", "writing more")
    db.insert_capture(T0 + 70, "Chrome", "", "Sheet", "numbers")

    activities = db.timeline(DAY)
    assert len(activities) == 2
    assert activities[0]["app"] == "Safari"
    assert activities[0]["duration"] == 60 + CAPTURE_INTERVAL_SECONDS

    usage = db.app_usage(DAY)
    assert usage[0] == {
        "app": "Safari",
        "seconds": 60.0 + CAPTURE_INTERVAL_SECONDS,
        "capture_count": 2,
    }
    assert usage[1] == {
        "app": "Chrome",
        "seconds": CAPTURE_INTERVAL_SECONDS,
        "capture_count": 1,
    }
    assert all(item["seconds"] > 0 for item in usage)

    with pytest.raises(ValueError):
        db.timeline("not-a-day")


def test_single_capture_has_realistic_duration(db):
    db.insert_capture(T0, "Safari", "", "Docs", "only one capture")

    activities = db.timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["start_ts"] == T0
    assert activities[0]["end_ts"] == T0 + CAPTURE_INTERVAL_SECONDS
    assert activities[0]["duration"] == CAPTURE_INTERVAL_SECONDS
    assert db.app_usage(DAY) == [
        {
            "app": "Safari",
            "seconds": CAPTURE_INTERVAL_SECONDS,
            "capture_count": 1,
        }
    ]


def test_activity_gap_is_measured_between_captures(db):
    db.insert_capture(T0, "Safari", "", "Docs", "one")
    db.insert_capture(T0 + 299, "Safari", "", "Docs", "still here")
    assert len(db.timeline(DAY)) == 1

    db.insert_capture(T0 + 599, "Safari", "", "Docs", "same again")
    assert len(db.timeline(DAY)) == 2


def test_custom_interval_and_overlapping_captures_merge(db):
    db.insert_capture(
        T0, "Safari", "", "Docs", "one", interval_seconds=30
    )
    db.insert_capture(
        T0 + 10, "Safari", "", "Docs", "two", interval_seconds=30
    )

    activities = db.timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["start_ts"] == T0
    assert activities[0]["end_ts"] == T0 + 40
    assert activities[0]["duration"] == 40
    assert activities[0]["capture_count"] == 2


def test_record_activity_uses_interval_for_duration(db):
    db.record_activity(T0, "Safari", "", "Docs", interval_seconds=10)
    db.record_activity(T0 + 5, "Safari", "", "Docs", interval_seconds=10)

    activities = db.timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["end_ts"] == T0 + 15
    assert activities[0]["duration"] == 15
    assert activities[0]["capture_count"] == 2


def test_captures_between_and_recent(db):
    first = db.insert_capture(T0, "Safari", "", "A", "first")
    second = db.insert_capture(T0 + 100, "Chrome", "", "B", "second")
    third = db.insert_capture(T0 + 200, "Mail", "", "C", "third")

    between = db.captures_between(T0 + 50, T0 + 250)
    assert [row["id"] for row in between] == [second, third]
    assert [row["id"] for row in db.recent(2)] == [third, second]
    assert db.recent(10)[0]["id"] == third
    assert first not in [row["id"] for row in between]


def test_delete_range_deletes_captures_and_images(db, tmp_path):
    image = tmp_path / "shot.jpg"
    image.write_bytes(b"fake jpeg")
    capture_id = db.insert_capture(
        T0, "Safari", "", "Secret", "sensitive text", image_path=image
    )
    keep_id = db.insert_capture(T0 + 100, "Chrome", "", "Keep", "keep me")

    result = db.delete_range(T0 - 10, T0 + 10)
    assert result["captures"] == 1
    assert result["images_deleted"] == 1
    assert not image.exists()
    assert db.get_capture(capture_id) is None
    assert db.get_capture(keep_id) is not None
    assert db.search("sensitive") == []
    assert len(db.search("keep")) == 1


def test_delete_range_clips_activity(db):
    db.insert_capture(T0, "Safari", "", "Docs", "one")
    db.insert_capture(T0 + 100, "Safari", "", "Docs", "two")

    result = db.delete_range(T0 + 20, T0 + 80)
    assert result["captures"] == 0
    assert result["activities"] >= 1

    activities = db.timeline(DAY)
    assert len(activities) == 2
    assert activities[0]["end_ts"] == T0 + 20
    assert activities[1]["start_ts"] == T0 + 80
    assert activities[1]["end_ts"] == T0 + 100 + CAPTURE_INTERVAL_SECONDS


def test_delete_range_empty_is_noop(db):
    db.insert_capture(T0, "Safari", "", "Docs", "text")
    assert db.delete_range(T0, T0) == {
        "captures": 0,
        "activities": 0,
        "images_deleted": 0,
    }
    assert db.stats()["captures"] == 1


def test_prune(db, tmp_path):
    old_image = tmp_path / "old.jpg"
    old_image.write_bytes(b"old")
    db.insert_capture(T0, "Safari", "", "Old", "ancient history", image_path=old_image)
    db.insert_capture(T0 + 9 * 86400, "Safari", "", "New", "recent work")

    result = db.prune(5, now=T0 + 10 * 86400)
    assert result["captures"] == 1
    assert result["images_deleted"] == 1
    assert not old_image.exists()
    assert db.stats()["captures"] == 1
    assert db.search("ancient") == []
    assert len(db.search("recent")) == 1


def test_export_all_and_stats(db):
    db.insert_capture(T0, "Safari", "", "Docs", "exported text")
    exported = db.export_all()
    for key in (
        "captures",
        "activities",
        "journals",
        "loops",
        "meetings",
        "chats",
        "messages",
        "stats",
        "exported_at",
        "schema_version",
    ):
        assert key in exported
    assert exported["captures"][0]["text"] == "exported text"
    assert exported["stats"]["captures"] == 1
    assert exported["stats"]["db_bytes"] > 0
    assert exported["stats"]["oldest_ts"] == T0


def test_record_activity_without_capture(db):
    db.record_activity(T0, "Safari", "", "Docs")
    db.record_activity(T0 + 30, "Safari", "", "Docs")
    assert db.stats()["captures"] == 0
    activities = db.timeline(DAY)
    assert len(activities) == 1
    assert activities[0]["capture_count"] == 2


def test_day_bounds_round_trip():
    start, end = day_bounds(DAY)
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)
    assert start_dt.strftime("%Y-%m-%d %H:%M:%S") == "2025-03-10 00:00:00"
    assert (end_dt - start_dt).total_seconds() == 86400


def test_module_level_api_uses_magpie_home(monkeypatch, tmp_path):
    from magpie import db as db_module

    monkeypatch.setenv("MAGPIE_HOME", str(tmp_path / "home"))
    try:
        capture_id = db_module.insert_capture(T0, "Safari", "", "Docs", "hello")
        assert capture_id > 0
        assert db_module.search("hello")[0]["app"] == "Safari"
        assert db_module.stats()["captures"] == 1
        assert db_module.get_default_db().get_capture(capture_id)["text"] == "hello"
    finally:
        db_module.close_default_dbs()
