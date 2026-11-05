"""Retrieval-augmented answers over the local screen memory.

The pipeline is deliberately simple and fully local: parse a natural-language
time range, find likely captures with FTS5 (falling back to evenly sampled
captures when a time range is known), attach a compact activity timeline, and
ask the configured model to answer using numbered sources.  Every source is
returned with the answer so the UI can show the original screenshot and text.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from . import config as config_module
from . import db as db_module
from . import llm as llm_module
from . import timeparse

MAX_CAPTURES = 12
MAX_CAPTURE_CHARS = 1200
MAX_CONTEXT_CHARS = 12000
MAX_TIMELINE_LINES = 60
MAX_RANGE_DAYS = 31
MAX_SAMPLE = 12
CITATION_SNIPPET_CHARS = 300

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MARK_RE = re.compile(r"</?mark>", re.IGNORECASE)

_STOPWORDS = frozenset(
    """
    a about above after again against all almost also am an and any are around
    as at be because been before being below between both but by can cannot
    could did do does doing done down during each either else ever every few
    for from further get got had has have having he her here hers herself him
    himself his how however i if in into is it its itself just know let like
    made make many may me might more most much must my myself never no not now
    of off on once only or other our ours ourselves out over own past perhaps
    please put quite rather really said same say see she should since so some
    something still such than that the their theirs them themselves then there
    these they thing think this those though through thus to too under until
    up upon us use used using very was we well were what whatever when where
    whether which while who whom whose why will with within without would yes
    yet you your yours yourself yourselves ask asked tell told show find give
    gave go going went come came want wanted need needed happen happened
    """.split()
)

_SYSTEM_PROMPT = (
    "You are Magpie, a private assistant that answers from the user's own "
    "screen memories. Use only the memories below; do not invent facts. Cite "
    "sources with bracketed numbers like [1] that match the memory numbers. "
    "Include times in the user's local time when they help. If the memories do "
    "not contain the answer, say plainly that your memory does not contain it. "
    "Drafting requests such as 'draft a reply to ...' are allowed: use the "
    "memories as context and make clear which parts are suggested wording."
)


def _as_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is not None:
        return config
    return config_module.load_config()


def _as_db(database: db_module.Database | None) -> db_module.Database:
    return database if database is not None else db_module.get_default_db()


def _local_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def _citation_snippet(text: str, keywords: list[str] | None = None) -> str:
    body = _MARK_RE.sub("", str(text or ""))
    if not body:
        return ""

    lowered = body.lower()
    position = -1
    for keyword in keywords or []:
        if not keyword:
            continue
        found = lowered.find(keyword.lower())
        if found != -1 and (position == -1 or found < position):
            position = found

    if position == -1:
        snippet = body[:CITATION_SNIPPET_CHARS]
        truncated = len(body) > CITATION_SNIPPET_CHARS
    else:
        half = CITATION_SNIPPET_CHARS // 2
        start = max(0, position - half)
        end = min(len(body), start + CITATION_SNIPPET_CHARS)
        start = max(0, end - CITATION_SNIPPET_CHARS)
        snippet = body[start:end]
        if start > 0:
            snippet = "…" + snippet
        truncated = end < len(body)

    snippet = " ".join(snippet.split())
    if len(snippet) > CITATION_SNIPPET_CHARS:
        snippet = snippet[: CITATION_SNIPPET_CHARS - 1].rstrip() + "…"
    elif truncated and not snippet.endswith("…"):
        snippet += "…"
    return snippet


def compact_citation(
    doc: dict[str, Any], keywords: list[str] | None = None
) -> dict[str, Any]:
    """A small citation that the UI can render without the full capture text."""

    text = doc.get("text")
    if not text:
        text = doc.get("snippet")
    return {
        "id": doc.get("id"),
        "ts": doc.get("ts"),
        "app": doc.get("app") or "",
        "window_title": doc.get("window_title") or "",
        "snippet": _citation_snippet(str(text or ""), keywords),
        "has_image": bool(doc.get("image_path")),
    }


def keywords_from(question: str) -> list[str]:
    """Extract useful search terms, dropping stopwords and one-letter noise."""

    words: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(question or ""):
        token = match.group(0)
        lowered = token.lower()
        if len(token) < 2 or lowered in _STOPWORDS:
            continue
        if lowered not in seen:
            seen.add(lowered)
            words.append(token)
    return words


def _sample(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (limit - 1)
    picked: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index in range(limit):
        position = round(index * step)
        if position in seen:
            continue
        seen.add(position)
        picked.append(items[position])
    return picked


def _days_between(start_ts: float, end_ts: float) -> list[str]:
    if end_ts <= start_ts:
        return [_local_day(start_ts)]
    first = datetime.fromtimestamp(start_ts).date()
    last = datetime.fromtimestamp(max(start_ts, end_ts - 1)).date()
    days: list[str] = []
    current = first
    while current <= last and len(days) < MAX_RANGE_DAYS:
        days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days or [_local_day(start_ts)]


def _timeline_text(
    database: db_module.Database,
    start_ts: float | None,
    end_ts: float | None,
    now: float,
) -> str:
    if start_ts is not None and end_ts is not None:
        days = _days_between(start_ts, end_ts)
        range_start, range_end = start_ts, end_ts
    else:
        day = _local_day(now)
        days = [day]
        range_start, range_end = db_module.day_bounds(day)

    activities: list[dict[str, Any]] = []
    for day in days:
        for item in database.timeline(day):
            item_start = float(item.get("clipped_start_ts", item["start_ts"]))
            item_end = float(item.get("clipped_end_ts", item["end_ts"]))
            if item_end <= range_start or item_start >= range_end:
                continue
            activities.append(item)

    activities.sort(key=lambda item: (float(item["start_ts"]), int(item["id"])))
    lines: list[str] = []
    for item in activities[:MAX_TIMELINE_LINES]:
        duration = max(0.0, float(item.get("duration", 0.0)))
        minutes = max(1, round(duration / 60.0)) if duration else 0
        title = str(item.get("window_title") or "").strip()
        suffix = f" — {title}" if title else ""
        lines.append(
            f"- {_format_clock(float(item['start_ts']))}–"
            f"{_format_clock(float(item['end_ts']))} "
            f"{item.get('app') or 'Unknown'}{suffix} ({minutes}m)"
        )
    return "\n".join(lines)


def _dedupe(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("id")
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def retrieve(
    question: str,
    database: db_module.Database | None = None,
    *,
    now: float | None = None,
    limit: int = MAX_CAPTURES,
) -> dict[str, Any]:
    """Find the captures and timeline that should ground an answer."""

    db_obj = _as_db(database)
    moment = float(now if now is not None else datetime.now().timestamp())
    start_ts, end_ts, without_time = timeparse.parse_range_and_text(question, moment)
    keywords = keywords_from(without_time)
    keyword_query = " ".join(keywords)

    matches: list[dict[str, Any]] = []
    if keyword_query:
        matches = db_obj.search(
            keyword_query,
            start_ts=start_ts,
            end_ts=end_ts,
            limit=max(1, int(limit)),
        )

    if not matches and start_ts is not None and end_ts is not None:
        between = db_obj.captures_between(start_ts, end_ts)
        matches = _sample(between, min(max(1, int(limit)), MAX_SAMPLE))
    elif not matches:
        matches = list(db_obj.recent(max(1, int(limit))))

    docs = _dedupe(matches)[: max(1, int(limit))]
    timeline = _timeline_text(db_obj, start_ts, end_ts, moment)

    return {
        "question": question,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "keywords": keywords,
        "docs": docs,
        "timeline": timeline,
        "citations": [compact_citation(doc, keywords) for doc in docs],
    }


def _capture_blocks(docs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    blocks: list[str] = []
    used: list[dict[str, Any]] = []
    used_chars = 0
    for index, doc in enumerate(docs, start=1):
        text = str(doc.get("text") or "")
        if len(text) > MAX_CAPTURE_CHARS:
            text = text[:MAX_CAPTURE_CHARS] + "…"
        header = (
            f"[{index}] {_format_time(float(doc['ts']))} "
            f"{doc.get('app') or 'Unknown'}"
        )
        title = str(doc.get("window_title") or "").strip()
        if title:
            header += f" — {title}"
        block = f"{header}\n{text}"
        if used and used_chars + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        used.append(doc)
        used_chars += len(block) + 2
    return "\n\n".join(blocks), used


def _build_messages(
    question: str,
    chat_history: Iterable[Any] | None,
    retrieved: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    capture_text, used = _capture_blocks(retrieved["docs"])
    citations = [
        compact_citation(doc, retrieved.get("keywords")) for doc in used
    ]
    range_text = ""
    if retrieved["start_ts"] is not None and retrieved["end_ts"] is not None:
        range_text = (
            f"{_format_time(retrieved['start_ts'])} to "
            f"{_format_time(retrieved['end_ts'])}"
        )
    else:
        range_text = "all remembered time"

    memory_parts = [f"Time range: {range_text}"]
    if retrieved["timeline"]:
        memory_parts.append(
            "What the user was doing:\n" + retrieved["timeline"]
        )
    else:
        memory_parts.append("What the user was doing: no activity was recorded.")
    if capture_text:
        memory_parts.append("Screen memories:\n" + capture_text)
    else:
        memory_parts.append("Screen memories: none matched this request.")

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT}
    ]
    for item in chat_history or []:
        if isinstance(item, dict):
            role = str(item.get("role") or "user")
            content = str(item.get("content") or "")
        else:
            role, content = "user", str(item)
        if role not in ("user", "assistant"):
            role = "user"
        if content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": "\n\n".join(memory_parts)
            + f"\n\nQuestion: {question}",
        }
    )
    return messages, citations


def _chat_text(provider: Any, messages: list[dict[str, str]]) -> str:
    result = provider.chat(messages)
    if isinstance(result, str):
        return result
    return "".join(str(chunk) for chunk in result)


def answer(
    question: str,
    chat_history: Iterable[Any] | None = None,
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Answer ``question`` from memory and return the answer plus citations."""

    cfg = _as_config(config)
    database = _as_db(db)
    retrieved = retrieve(question, database, now=now)
    messages, citations = _build_messages(question, chat_history, retrieved)
    model = provider if provider is not None else llm_module.get_provider(cfg)
    text = _chat_text(model, messages)
    return {"answer": text, "citations": citations}


def answer_stream(
    question: str,
    chat_history: Iterable[Any] | None = None,
    config: dict[str, Any] | None = None,
    db: db_module.Database | None = None,
    *,
    provider: Any | None = None,
    now: float | None = None,
) -> Iterator[str | dict[str, Any]]:
    """Yield answer text chunks, then a final ``{"citations": [...]}`` object."""

    cfg = _as_config(config)
    database = _as_db(db)
    retrieved = retrieve(question, database, now=now)
    messages, citations = _build_messages(question, chat_history, retrieved)
    model = provider if provider is not None else llm_module.get_provider(cfg)

    result = model.chat(messages, stream=True)
    if isinstance(result, str):
        if result:
            yield result
    else:
        for chunk in result:
            if chunk:
                yield str(chunk)
    yield {"citations": citations, "done": True}
