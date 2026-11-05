"""Parse natural-language time ranges such as "yesterday afternoon".

Times are interpreted in the machine's local timezone.  ``parse_range`` returns
a ``(start_ts, end_ts)`` pair of POSIX timestamps, or ``None`` when the text has
no recognisable range.  ``strip_time_phrases`` removes the matched phrase so it
is not also treated as a search keyword.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

RangeBuilder = Callable[[re.Match[str], datetime], tuple[float, float]]

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _as_datetime(now: datetime | float | int | None) -> datetime:
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now)
    raise TypeError("now must be a datetime, timestamp, or None")


def _day_start(moment: datetime, offset_days: int = 0) -> datetime:
    return (moment + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _window(
    moment: datetime, offset_days: int, start_hour: int, end_hour: int
) -> tuple[float, float]:
    day = _day_start(moment, offset_days)
    start = day.replace(hour=start_hour)
    end = day + timedelta(days=1) if end_hour >= 24 else day.replace(hour=end_hour)
    return start.timestamp(), end.timestamp()


def _full_day(moment: datetime, offset_days: int = 0) -> tuple[float, float]:
    start = _day_start(moment, offset_days)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _from_to(start: datetime, end: datetime) -> tuple[float, float]:
    return start.timestamp(), end.timestamp()


# -- builders ----------------------------------------------------------


def _last_night(match: re.Match[str], now: datetime) -> tuple[float, float]:
    today = _day_start(now)
    start = (today - timedelta(days=1)).replace(hour=18)
    end = today.replace(hour=6)
    return _from_to(start, end)


def _yesterday_morning(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, -1, 6, 12)


def _yesterday_afternoon(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, -1, 12, 18)


def _yesterday_evening(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, -1, 18, 24)


def _yesterday(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _full_day(now, -1)


def _this_morning(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, 0, 6, 12)


def _this_afternoon(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, 0, 12, 18)


def _this_evening(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _window(now, 0, 18, 24)


def _today(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _full_day(now, 0)


def _last_hours(match: re.Match[str], now: datetime) -> tuple[float, float]:
    count = int(match.group(1))
    end = now
    start = now - timedelta(hours=count)
    return _from_to(start, end)


def _last_minutes(match: re.Match[str], now: datetime) -> tuple[float, float]:
    count = int(match.group(1))
    end = now
    start = now - timedelta(minutes=count)
    return _from_to(start, end)


def _last_hour(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _from_to(now - timedelta(hours=1), now)


def _this_week(match: re.Match[str], now: datetime) -> tuple[float, float]:
    monday = _day_start(now, -now.weekday())
    return _from_to(monday, now)


def _last_week(match: re.Match[str], now: datetime) -> tuple[float, float]:
    this_monday = _day_start(now, -now.weekday())
    last_monday = this_monday - timedelta(days=7)
    return _from_to(last_monday, this_monday)


def _days_ago(match: re.Match[str], now: datetime) -> tuple[float, float]:
    return _full_day(now, -int(match.group(1)))


def _weekday(match: re.Match[str], now: datetime) -> tuple[float, float]:
    target = _WEEKDAYS[match.group(1).lower()]
    days_back = (now.weekday() - target) % 7
    return _full_day(now, -days_back)


# -- rule table --------------------------------------------------------

_RULES: list[tuple[str, re.Pattern[str], RangeBuilder]] = [
    ("last_night", re.compile(r"\blast\s+night\b", re.IGNORECASE), _last_night),
    (
        "yesterday_morning",
        re.compile(r"\byesterday\s+morning\b", re.IGNORECASE),
        _yesterday_morning,
    ),
    (
        "yesterday_afternoon",
        re.compile(r"\byesterday\s+afternoon\b", re.IGNORECASE),
        _yesterday_afternoon,
    ),
    (
        "yesterday_evening",
        re.compile(r"\byesterday\s+(?:evening|night)\b", re.IGNORECASE),
        _yesterday_evening,
    ),
    ("yesterday", re.compile(r"\byesterday\b", re.IGNORECASE), _yesterday),
    ("this_morning", re.compile(r"\bthis\s+morning\b", re.IGNORECASE), _this_morning),
    (
        "this_afternoon",
        re.compile(r"\bthis\s+afternoon\b", re.IGNORECASE),
        _this_afternoon,
    ),
    (
        "this_evening",
        re.compile(r"\b(?:this\s+evening|tonight)\b", re.IGNORECASE),
        _this_evening,
    ),
    ("today", re.compile(r"\btoday\b", re.IGNORECASE), _today),
    (
        "last_hours",
        re.compile(
            r"\b(?:the\s+)?(?:last|past)\s+(\d+)\s*(?:hours?|hrs?)\b",
            re.IGNORECASE,
        ),
        _last_hours,
    ),
    (
        "last_minutes",
        re.compile(
            r"\b(?:the\s+)?(?:last|past)\s+(\d+)\s*(?:minutes?|mins?)\b",
            re.IGNORECASE,
        ),
        _last_minutes,
    ),
    (
        "last_hour",
        re.compile(
            r"\b(?:the\s+)?(?:last|past)\s+(?:one\s+)?hour\b", re.IGNORECASE
        ),
        _last_hour,
    ),
    ("this_week", re.compile(r"\bthis\s+week\b", re.IGNORECASE), _this_week),
    (
        "last_week",
        re.compile(r"\b(?:the\s+)?last\s+week\b", re.IGNORECASE),
        _last_week,
    ),
    (
        "days_ago",
        re.compile(r"\b(\d+)\s+days?\s+ago\b", re.IGNORECASE),
        _days_ago,
    ),
    (
        "weekday",
        re.compile(
            r"\b(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            re.IGNORECASE,
        ),
        _weekday,
    ),
]


def parse_range(
    question: str, now: datetime | float | int | None = None
) -> tuple[float, float] | None:
    """Return the first recognised time range in ``question``, if any."""

    moment = _as_datetime(now)
    text = question or ""
    for _name, pattern, builder in _RULES:
        match = pattern.search(text)
        if match is not None:
            return builder(match, moment)
    return None


def strip_time_phrases(
    question: str, now: datetime | float | int | None = None
) -> str:
    """Remove every recognised time phrase, leaving the retrieval keywords."""

    text = question or ""
    # Validates ``now`` even though the patterns do not need it.
    _as_datetime(now)
    for _name, pattern, _builder in _RULES:
        text = pattern.sub(" ", text)
    return _clean(text)


def parse_range_and_text(
    question: str, now: datetime | float | int | None = None
) -> tuple[float | None, float | None, str]:
    """Convenience wrapper returning ``(start, end, cleaned_question)``."""

    moment = _as_datetime(now)
    found = parse_range(question, moment)
    start, end = found if found is not None else (None, None)
    return start, end, strip_time_phrases(question, moment)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([?.!,;:])", r"\1", text)
    text = re.sub(r"^[\s,;:\-]+", "", text)
    text = re.sub(r"[\s,;:\-]+$", "", text)
    return text.strip()
