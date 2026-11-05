"""Daily journals and open-loop extraction.

A journal is generated from the day's app usage, activity timeline and a sample
of captures, then cached in SQLite so opening the same day again is instant.
``extract_loops`` asks the model for follow-ups and stores them as open loops.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from . import config as config_module
from . import db as db_module
from . import llm as llm_module

MAX_CAPTURES = 30
MAX_CAPTURE_CHARS = 900
MAX_CONTEXT_CHARS = 12000
MAX_ACTIVITIES = 80

_JOURNAL_SYSTEM = (
    "You are Magpie, a private assistant writing a personal daily journal from "
    "the user's own screen memories. Be concrete, calm and concise. Use local "
    "times. Only describe what the memories support; say when something is "
    "unclear. Return markdown with exactly these sections:\n"
    "## Summary\n## What you worked on\n## People & conversations\n"
    "## Open loops"
)

_LOOPS_SYSTEM = (
    "You extract open loops from the user's screen memories. Return ONLY a JSON "
    "array of short action strings for follow-ups, todos and promises the user "
    "made or received. Ground every loop in the screen memories and use only "
    "names, tasks and dates that actually appear there. Use exactly this "
    "format:\n"
    '["<task> for <person>", "<task> by <day>"]\n'
    "The placeholders above only show the shape. Never repeat, copy or "
    "paraphrase the example text itself. Do not include explanations, "
    "markdown, keys or a code fence. Return [] when there are none."
)

_PROMPT_EXAMPLE_LOOPS = (
    "<task> for <person>",
    "<task> by <day>",
    "reply to sam about the contract",
    "book the venue by friday",
)

_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at
    be because been before being below between both but by
    can cannot could did do does doing down during
    each few for from further
    had has have having he her here hers herself him himself his how
    i if in into is it its itself
    just
    me more most my myself
    no nor not now
    of off on once only or other our ours ourselves out over own
    please
    same she should so some such
    than that the their theirs them themselves then there these they this those
    through to too
    under until up
    very
    was we were what when where which while who whom why will with would
    you your yours yourself yourselves
    """.split()
)
_CONTENT_WORD_RE = re.compile(r"[a-z0-9]+")
_GROUNDING_THRESHOLD = 0.6
_LOOP_SIMILARITY_THRESHOLD = 0.8

_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
_KEYED_BULLET_RE = re.compile(
    r"(?im)^\s*(?:todo|follow[ -]?up|action(?: item)?)\b\s*[:\-]\s*(.+?)\s*$"
)
_ITEM_KEYS = ("text", "task", "title", "item", "todo", "followup", "action")

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_OPEN_LOOPS_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(?:open\s+loops?|loops?|follow[ -]?ups?|todos?|to-dos?|action\s+items?)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_HEURISTIC_RE = re.compile(
    r"\b(?:can|could|would)\s+you\b"
    r"|\bplease\b"
    r"|\bTODO\b"
    r"|\bFIXME\b"
    r"|\bremind(?:er)?\b"
    r"|\bfollow[ -]?up\b"
    r"|\bdue\b"
    r"|\bdeadline\b"
    r"|\bEOD\b"
    r"|\bby\s+(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?|noon|midnight|tomorrow|today"
    r"|tonight|eod|end\s+of\s+(?:day|week|month))\b",
    re.IGNORECASE,
)
MAX_HEURISTIC_LOOPS = 15


def _as_config(config: dict[str, Any] | None) -> dict[str, Any]:
    return config if config is not None else config_module.load_config()


def _as_db(database: db_module.Database | None) -> db_module.Database:
    return database if database is not None else db_module.get_default_db()


def _day_bounds(day: str) -> tuple[float, float]:
    return db_module.day_bounds(day)


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{max(1, minutes)}m"


def _sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (limit - 1)
    picked: list[dict[str, Any]] = []
    for index in range(limit):
        position = round(index * step)
        item = items[position]
        if item not in picked:
            picked.append(item)
    return picked


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_journal(day: str, db: db_module.Database | None = None) -> dict[str, Any] | None:
    """Return the cached journal row for ``day``, if there is one."""

    _day_bounds(day)
    database = _as_db(db)
    row = database.conn.execute(
        "SELECT day, summary, created_ts, model FROM journals WHERE day = ?",
        (day,),
    ).fetchone()
    return _row_to_dict(row)


def _save_journal(
    day: str, summary: str, model: str, database: db_module.Database
) -> dict[str, Any]:
    created = time.time()
    with database.conn:
        database.conn.execute(
            """
            INSERT INTO journals(day, summary, created_ts, model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                summary = excluded.summary,
                created_ts = excluded.created_ts,
                model = excluded.model
            """,
            (day, summary, created, model),
        )
    return {
        "day": day,
        "summary": summary,
        "created_ts": created,
        "model": model,
        "cached": False,
    }


def _model_name(provider: Any, config: dict[str, Any]) -> str:
    name = str(getattr(provider, "name", "") or "")
    model = str(getattr(provider, "model", "") or "")
    if model:
        return f"{name}:{model}" if name else model
    provider_name = str(config.get("provider") or "")
    profile = config.get("providers", {}).get(provider_name, {})
    configured = str(profile.get("model") or "") if isinstance(profile, dict) else ""
    if configured:
        return f"{provider_name}:{configured}" if provider_name else configured
    return name or provider_name or "unknown"


def _activities_text(database: db_module.Database, day: str) -> str:
    lines: list[str] = []
    for item in database.timeline(day)[:MAX_ACTIVITIES]:
        title = str(item.get("window_title") or "").strip()
        suffix = f" — {title}" if title else ""
        lines.append(
            f"- {_format_time(float(item['start_ts']))}–"
            f"{_format_time(float(item['end_ts']))} "
            f"{item.get('app') or 'Unknown'}{suffix} "
            f"({_format_duration(float(item.get('duration', 0.0)))})"
        )
    return "\n".join(lines)


def _usage_text(database: db_module.Database, day: str) -> str:
    return "\n".join(
        f"- {item['app'] or 'Unknown'}: {_format_duration(item['seconds'])} "
        f"({item['capture_count']} captures)"
        for item in database.app_usage(day)
    )


def _captures_text(captures: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    used = 0
    for doc in captures:
        text = str(doc.get("text") or "")
        if len(text) > MAX_CAPTURE_CHARS:
            text = text[:MAX_CAPTURE_CHARS] + "…"
        title = str(doc.get("window_title") or "").strip()
        header = f"{_format_time(float(doc['ts']))} {doc.get('app') or 'Unknown'}"
        if title:
            header += f" — {title}"
        block = f"{header}\n{text}"
        if blocks and used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


def _event_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _calendar_text(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        title = str(event.get("title") or "").strip() or "(untitled event)"
        start_ts = _event_ts(event.get("start"))
        end_ts = _event_ts(event.get("end"))
        when = ""
        if start_ts is not None and end_ts is not None:
            when = f"{_format_time(start_ts)}–{_format_time(end_ts)}"
        elif start_ts is not None:
            when = _format_time(start_ts)
        location = str(event.get("location") or "").strip()
        calendar = str(event.get("calendar") or "").strip()
        extras = [item for item in (location, calendar) if item]
        suffix = f" ({', '.join(extras)})" if extras else ""
        prefix = f"- {when} " if when else "- "
        lines.append(f"{prefix}{title}{suffix}")
    return "\n".join(lines)


def build_journal_context(
    day: str,
    database: db_module.Database | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect the usage, activities and sampled captures used for a journal."""

    start, end = _day_bounds(day)
    db_obj = _as_db(database)
    captures = db_obj.captures_between(start, end)
    sampled = _sample(captures, MAX_CAPTURES)
    events = list(calendar_events or [])
    return {
        "day": day,
        "usage": db_obj.app_usage(day),
        "activities": db_obj.timeline(day),
        "capture_count": len(captures),
        "captures": sampled,
        "usage_text": _usage_text(db_obj, day),
        "activities_text": _activities_text(db_obj, day),
        "captures_text": _captures_text(sampled),
        "calendar": events,
        "calendar_text": _calendar_text(events),
    }


def _journal_messages(day: str, context: dict[str, Any]) -> list[dict[str, str]]:
    usage = context["usage_text"] or "No app usage was recorded."
    activities = context["activities_text"] or "No activities were recorded."
    captures = context["captures_text"] or "No screen memories were recorded."
    calendar_block = ""
    if context.get("calendar_text"):
        calendar_block = f"Calendar events:\n{context['calendar_text']}\n\n"
    body = (
        f"Day: {day}\n\n"
        f"App usage:\n{usage}\n\n"
        f"Activity timeline:\n{activities}\n\n"
        f"{calendar_block}"
        f"Screen memories ({context['capture_count']} captures, sampled):\n"
        f"{captures}\n\n"
        "Write the journal for this day."
    )
    return [
        {"role": "system", "content": _JOURNAL_SYSTEM},
        {"role": "user", "content": body},
    ]


def _chat_text(provider: Any, messages: list[dict[str, str]]) -> str:
    result = provider.chat(messages)
    if isinstance(result, str):
        return result
    return "".join(str(chunk) for chunk in result)


def generate_journal(
    day: str,
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    regenerate: bool = False,
    now: float | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate (or return the cached) markdown journal for ``day``.

    When ``calendar_events`` is provided the day's events are included both in
    the model prompt and in the returned payload under ``"calendar"``.
    """

    cfg = _as_config(config)
    database = _as_db(db)
    _day_bounds(day)
    events = list(calendar_events or [])
    if not regenerate:
        cached = get_journal(day, database)
        if cached is not None and cached.get("summary"):
            cached["cached"] = True
            cached["calendar"] = events
            return cached

    context = build_journal_context(day, database, calendar_events=events)
    model = provider if provider is not None else llm_module.get_provider(cfg)
    summary = _chat_text(model, _journal_messages(day, context))
    result = _save_journal(day, summary, _model_name(model, cfg), database)
    result["calendar"] = events
    return result


def _loop_text(item: Any) -> str:
    if isinstance(item, str):
        text = item
    elif isinstance(item, dict):
        text = ""
        for key in _ITEM_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        if not text:
            for value in item.values():
                if isinstance(value, str) and value.strip():
                    text = value
                    break
    else:
        text = str(item)
    text = text.strip().strip("\"'").strip()
    text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_json_array(text: str) -> list[Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "[":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for key in ("loops", "items", "todos", "actions"):
                nested = value.get(key)
                if isinstance(nested, list):
                    return nested
    return None


def parse_loops(text: str) -> list[str]:
    """Parse a model reply into a clean, de-duplicated list of loop strings.

    Handles a clean JSON array, an array wrapped in prose or a code fence, an
    object containing a ``loops``/``items`` list, and finally markdown bullets.
    """

    items: list[Any] = []
    found = _find_json_array(text or "")
    if found is not None:
        items = found
    else:
        items = [match.group(1) for match in _BULLET_RE.finditer(text or "")]
        items.extend(
            match.group(1) for match in _KEYED_BULLET_RE.finditer(text or "")
        )

    loops: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = _loop_text(item)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        loops.append(cleaned)
    return loops


def _open_loops_section(markdown: str) -> str:
    """Return the body of the markdown "Open loops" section, if present."""

    lines = (markdown or "").splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if _OPEN_LOOPS_HEADING_RE.match(line):
            start = index + 1
            break
    if start is None:
        return ""

    section: list[str] = []
    for line in lines[start:]:
        if _HEADING_RE.match(line):
            break
        section.append(line)
    return "\n".join(section).strip()


def _clean_loop_candidate(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)]|#+|//+)\s*", "", cleaned)
    cleaned = cleaned.strip().strip("\"'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 240:
        cleaned = cleaned[:239].rstrip() + "…"
    return cleaned


def _dedupe_loops(items: list[str], limit: int | None = None) -> list[str]:
    loops: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = _clean_loop_candidate(item)
        if not cleaned:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        loops.append(cleaned)
        if limit is not None and len(loops) >= limit:
            break
    return loops


def _loop_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _content_words(text: str) -> set[str]:
    """Return lowercased alphanumeric content words of length three or more."""

    return {
        word
        for word in _CONTENT_WORD_RE.findall(str(text or "").lower())
        if len(word) >= 3 and word not in _STOPWORDS
    }


def _loop_similarity(left: str, right: str) -> float:
    first = _loop_key(left)
    second = _loop_key(right)
    if not first or not second:
        return 1.0 if first == second else 0.0
    if first == second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def _capture_index(
    captures: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], set[str]]], set[str]]:
    entries: list[tuple[dict[str, Any], set[str]]] = []
    day_words: set[str] = set()
    for doc in captures:
        words = _content_words(str(doc.get("text") or ""))
        entries.append((doc, words))
        day_words |= words
    return entries, day_words


def _loop_source_id(
    loop_words: set[str],
    entries: list[tuple[dict[str, Any], set[str]]],
) -> int | None:
    """Return the capture id with the strongest word overlap with the loop."""

    best_id: int | None = None
    best_score = (0, 0.0)
    if not loop_words:
        return None
    for doc, words in entries:
        overlap = loop_words & words
        if not overlap:
            continue
        score = (len(overlap), len(overlap) / len(loop_words))
        if score > best_score:
            best_score = score
            capture_id = doc.get("id")
            best_id = int(capture_id) if capture_id is not None else None
    return best_id


def _with_sources(
    items: list[str], captures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Clean loops and attach the best-matching source capture."""

    entries, _day_words = _capture_index(captures)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        text = _clean_loop_candidate(item)
        if not text:
            continue
        key = _loop_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "text": text,
                "source_capture_id": _loop_source_id(
                    _content_words(text), entries
                ),
            }
        )
    return result


def _ground_loops(
    items: list[str], captures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep only loops supported by the day's capture text."""

    entries, day_words = _capture_index(captures)
    example_keys = {_loop_key(text) for text in _PROMPT_EXAMPLE_LOOPS}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        text = _clean_loop_candidate(item)
        if not text:
            continue
        key = _loop_key(text)
        if not key or key in example_keys or key in seen:
            continue
        words = _content_words(text)
        if not words:
            continue
        overlap = words & day_words
        if len(overlap) / len(words) < _GROUNDING_THRESHOLD:
            continue
        seen.add(key)
        result.append(
            {
                "text": text,
                "source_capture_id": _loop_source_id(words, entries),
            }
        )
    return result


def _merge_loops(
    primary: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append extra loops, dropping near-duplicates of the primary ones."""

    merged = list(primary)
    for item in extra:
        if any(
            _loop_similarity(item["text"], existing["text"])
            >= _LOOP_SIMILARITY_THRESHOLD
            for existing in merged
        ):
            continue
        merged.append(item)
    return merged


def parse_journal_loops(markdown: str) -> list[str]:
    """Pull loops out of the "Open loops" section of a journal."""

    section = _open_loops_section(markdown)
    if not section:
        return []

    loops = parse_loops(section)
    if loops:
        return _dedupe_loops(loops)

    candidates: list[str] = []
    for line in section.splitlines():
        candidate = _clean_loop_candidate(line)
        if not candidate:
            continue
        if candidate.casefold().strip(" .") in (
            "none",
            "no open loops",
            "nothing",
            "n/a",
        ):
            continue
        candidates.append(candidate)
    return _dedupe_loops(candidates)


def heuristic_loops(captures: list[dict[str, Any]]) -> list[str]:
    """Extract likely open loops from capture text without using a model."""

    candidates: list[str] = []
    for doc in captures:
        text = str(doc.get("text") or "")
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            candidate = _clean_loop_candidate(sentence)
            if not candidate or not _HEURISTIC_RE.search(candidate):
                continue
            candidates.append(candidate)
    return _dedupe_loops(candidates, limit=MAX_HEURISTIC_LOOPS)


def _loops_messages(
    day: str, context: dict[str, Any]
) -> list[dict[str, str]]:
    body = (
        f"Day: {day}\n\n"
        f"App usage:\n{context['usage_text'] or 'No app usage was recorded.'}\n\n"
        f"Activity timeline:\n"
        f"{context['activities_text'] or 'No activities were recorded.'}\n\n"
        f"Screen memories:\n"
        f"{context['captures_text'] or 'No screen memories were recorded.'}"
    )
    return [
        {"role": "system", "content": _LOOPS_SYSTEM},
        {"role": "user", "content": body},
    ]


def extract_loops(
    day: str,
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Ask the model for the day's open loops and store new ones."""

    cfg = _as_config(config)
    database = _as_db(db)
    start, end = _day_bounds(day)
    context = build_journal_context(day, database)
    captures = database.captures_between(start, end)
    model = provider if provider is not None else llm_module.get_provider(cfg)
    reply = _chat_text(model, _loops_messages(day, context))

    loops = _ground_loops(parse_loops(reply), captures)
    if not loops:
        journal = get_journal(day, database)
        loops = _ground_loops(
            parse_journal_loops(
                str(journal.get("summary") or "") if journal else ""
            ),
            captures,
        )

    if not loops:
        loops = _with_sources(heuristic_loops(captures), captures)
    elif len(loops) < 3:
        loops = _merge_loops(
            loops, _with_sources(heuristic_loops(captures), captures)
        )

    created = time.time()
    inserted: list[dict[str, Any]] = []
    for loop in loops:
        text = loop["text"]
        source_capture_id = loop["source_capture_id"]
        with database.conn:
            cursor = database.conn.execute(
                """
                INSERT OR IGNORE INTO loops
                    (text, source_capture_id, day, status, created_ts)
                VALUES (?, ?, ?, 'open', ?)
                """,
                (text, source_capture_id, day, created),
            )
            if cursor.rowcount <= 0:
                continue
            loop_id = int(cursor.lastrowid)
        inserted.append(
            {
                "id": loop_id,
                "text": text,
                "source_capture_id": source_capture_id,
                "day": day,
                "status": "open",
                "created_ts": created,
            }
        )
    return inserted


def loops_for_day(
    day: str,
    db: db_module.Database | None = None,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored loops for a day, newest first."""

    _day_bounds(day)
    database = _as_db(db)
    sql = "SELECT * FROM loops WHERE day = ?"
    params: list[Any] = [day]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_ts DESC, id DESC"
    rows = database.conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def auto_generate_previous_day(
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Generate yesterday's journal once per calendar day.

    A small marker file records the last day this ran so restarting the server
    several times does not regenerate the same journal.
    """

    moment = datetime.fromtimestamp(
        now if now is not None else time.time()
    )
    today = moment.strftime("%Y-%m-%d")
    marker = config_module.magpie_home() / ".journal-auto"
    try:
        if marker.read_text(encoding="utf-8").strip() == today:
            return None
    except OSError:
        pass

    previous = (moment - timedelta(days=1)).strftime("%Y-%m-%d")
    result = generate_journal(
        previous,
        config,
        db,
        provider=provider,
        regenerate=False,
        now=now,
    )
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(today + "\n", encoding="utf-8")
    except OSError:
        pass
    return result


def start_auto_journal(
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    now: float | None = None,
) -> threading.Thread:
    """Run :func:`auto_generate_previous_day` on a daemon thread."""

    def worker() -> None:
        try:
            auto_generate_previous_day(
                config, db, provider=provider, now=now
            )
        except Exception:
            return

    thread = threading.Thread(
        target=worker, name="magpie-auto-journal", daemon=True
    )
    thread.start()
    return thread
