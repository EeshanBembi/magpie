"""SQLite storage for Magpie captures, activities, journals and chat history.

All screen data lives in a single SQLite database using WAL mode.  Free-text
search is backed by FTS5 with the porter stemmer.  The database is deliberately
plain: any SQLite client can inspect or export it.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import sqlite3
import threading
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import config as config_module

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    app TEXT NOT NULL DEFAULT '',
    bundle_id TEXT NOT NULL DEFAULT '',
    window_title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    text_hash TEXT NOT NULL DEFAULT '',
    image_path TEXT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(ts);
CREATE INDEX IF NOT EXISTS idx_captures_app ON captures(app);
CREATE INDEX IF NOT EXISTS idx_captures_hash ON captures(text_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
    text,
    app,
    window_title,
    content='captures',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS captures_ai AFTER INSERT ON captures BEGIN
    INSERT INTO captures_fts(rowid, text, app, window_title)
    VALUES (new.id, new.text, new.app, new.window_title);
END;

CREATE TRIGGER IF NOT EXISTS captures_ad AFTER DELETE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, text, app, window_title)
    VALUES ('delete', old.id, old.text, old.app, old.window_title);
END;

CREATE TRIGGER IF NOT EXISTS captures_au AFTER UPDATE ON captures BEGIN
    INSERT INTO captures_fts(captures_fts, rowid, text, app, window_title)
    VALUES ('delete', old.id, old.text, old.app, old.window_title);
    INSERT INTO captures_fts(rowid, text, app, window_title)
    VALUES (new.id, new.text, new.app, new.window_title);
END;

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app TEXT NOT NULL DEFAULT '',
    bundle_id TEXT NOT NULL DEFAULT '',
    window_title TEXT NOT NULL DEFAULT '',
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    capture_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_activities_match ON activities(app, window_title, end_ts);

CREATE TABLE IF NOT EXISTS journals (
    day TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL,
    model TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source_capture_id INTEGER NULL,
    day TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'done', 'dismissed')),
    created_ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_loops_dedupe ON loops(day, lower(text));
CREATE INDEX IF NOT EXISTS idx_loops_status ON loops(status, day);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    start_ts REAL NOT NULL,
    end_ts REAL NULL,
    audio_path TEXT NULL,
    transcript TEXT NOT NULL DEFAULT '',
    notes_md TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'recording'
        CHECK (status IN ('recording', 'transcribing', 'done', 'error')),
    error TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_start ON meetings(start_ts);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

PRAGMA user_version = 1;
"""

ACTIVITY_GAP_SECONDS = 5 * 60
# Each capture stands for the interval that follows it, so a lone capture still
# produces a realistic (non-zero) activity duration.
CAPTURE_INTERVAL_SECONDS = float(
    config_module.DEFAULTS.get("interval_seconds", 15)
)
SIMILAR_WINDOW_SECONDS = 10 * 60
SIMILAR_TEXT_THRESHOLD = 0.8
_FTS_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_TABLES = (
    "captures",
    "activities",
    "journals",
    "loops",
    "meetings",
    "chats",
    "messages",
)


def normalize_text(text: str) -> str:
    """Collapse whitespace and case so hashes and dedupe are stable."""

    return " ".join((text or "").split()).strip().lower()


def hash_text(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def fts_query(query: str) -> str:
    """Turn arbitrary user input into a safe FTS5 MATCH expression.

    Every token is quoted (so punctuation and FTS operators cannot raise) and
    the final token is a prefix query.  Tokens are OR-ed for recall.
    """

    tokens = _FTS_TOKEN_RE.findall(query or "")
    if not tokens:
        return ""
    parts = [f'"{token}"' for token in tokens[:-1]]
    parts.append(f'"{tokens[-1]}"*')
    return " OR ".join(parts)


def day_bounds(day: str) -> tuple[float, float]:
    """Return local-midnight timestamps for a ``YYYY-MM-DD`` string."""

    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid day {day!r}; expected YYYY-MM-DD") from exc
    start = datetime.combine(parsed, dt_time.min)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _text_similarity(left: str, right: str) -> float:
    first = normalize_text(left)
    second = normalize_text(right)
    if not first or not second:
        return 1.0 if first == second else 0.0
    if first == second:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def collapse_similar(
    rows: Iterable[dict[str, Any]], limit: int | None = None
) -> list[dict[str, Any]]:
    """Collapse near-duplicate captures from the same app/window.

    Rows in the same app and window within ten minutes whose text is at least
    80% similar are grouped.  The first (best-ranked) row is kept and gains a
    ``similar_count`` field.
    """

    groups: list[dict[str, Any]] = []
    for row in rows:
        app = str(row.get("app") or "").casefold()
        title = str(row.get("window_title") or "").casefold()
        ts = float(row.get("ts") or 0.0)
        text = str(row.get("text") or "")

        matched: dict[str, Any] | None = None
        for group in groups:
            rep = group["rep"]
            if str(rep.get("app") or "").casefold() != app:
                continue
            if str(rep.get("window_title") or "").casefold() != title:
                continue
            if not any(
                abs(ts - member_ts) <= SIMILAR_WINDOW_SECONDS
                for member_ts in group["times"]
            ):
                continue
            if (
                _text_similarity(text, str(rep.get("text") or ""))
                < SIMILAR_TEXT_THRESHOLD
            ):
                continue
            matched = group
            break

        if matched is not None:
            matched["count"] += 1
            matched["times"].append(ts)
        else:
            groups.append({"rep": row, "count": 1, "times": [ts]})

    result: list[dict[str, Any]] = []
    for group in groups:
        item = dict(group["rep"])
        item["similar_count"] = int(group["count"])
        result.append(item)
    if limit is not None:
        result = result[: max(0, int(limit))]
    return result


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    """A thin, thread-safe wrapper around the Magpie SQLite database."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            target: str | Path = config_module.db_path()
        else:
            target = path
        if isinstance(target, Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path: str | Path = target
        else:
            self.path = target
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # -- lifecycle -----------------------------------------------------

    def init_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- captures ------------------------------------------------------

    def insert_capture(
        self,
        ts: float,
        app: str,
        bundle_id: str = "",
        window_title: str = "",
        text: str = "",
        image_path: str | Path | None = None,
        width: int = 0,
        height: int = 0,
        text_hash: str | None = None,
        interval_seconds: float | None = None,
    ) -> int:
        """Insert a capture and extend (or start) the matching activity.

        ``interval_seconds`` is how long this capture represents on screen.  It
        defaults to the configured capture interval so a single capture has a
        positive duration rather than collapsing to zero seconds.
        """

        timestamp = float(ts)
        body = text or ""
        digest = text_hash or hash_text(body)
        image = str(image_path) if image_path is not None else None

        with self._lock, self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO captures
                    (ts, app, bundle_id, window_title, text, text_hash,
                     image_path, width, height)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    app or "",
                    bundle_id or "",
                    window_title or "",
                    body,
                    digest,
                    image,
                    int(width or 0),
                    int(height or 0),
                ),
            )
            capture_id = int(cursor.lastrowid)
            self._record_activity(
                timestamp,
                app or "",
                bundle_id or "",
                window_title or "",
                1,
                interval_seconds,
            )
        return capture_id

    def record_activity(
        self,
        ts: float,
        app: str,
        bundle_id: str = "",
        window_title: str = "",
        capture_count: int = 1,
        interval_seconds: float | None = None,
    ) -> None:
        """Extend or start an activity without storing a capture (dedupe path)."""

        with self._lock, self.conn:
            self._record_activity(
                float(ts),
                app or "",
                bundle_id or "",
                window_title or "",
                int(capture_count),
                interval_seconds,
            )

    def _record_activity(
        self,
        ts: float,
        app: str,
        bundle_id: str,
        window_title: str,
        capture_count: int,
        interval_seconds: float | None = None,
    ) -> None:
        span = (
            CAPTURE_INTERVAL_SECONDS
            if interval_seconds is None
            else max(0.0, float(interval_seconds))
        )
        covered_until = ts + span
        last = self.conn.execute(
            "SELECT * FROM activities ORDER BY end_ts DESC, id DESC LIMIT 1"
        ).fetchone()
        if (
            last is not None
            and last["app"] == app
            and last["window_title"] == window_title
            and ts >= float(last["start_ts"])
            and ts - float(last["end_ts"]) + span < ACTIVITY_GAP_SECONDS
        ):
            new_end = max(float(last["end_ts"]), covered_until)
            self.conn.execute(
                "UPDATE activities SET end_ts = ?, capture_count = capture_count + ? WHERE id = ?",
                (new_end, capture_count, last["id"]),
            )
            return
        self.conn.execute(
            """
            INSERT INTO activities
                (app, bundle_id, window_title, start_ts, end_ts, capture_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (app, bundle_id, window_title, ts, covered_until, capture_count),
        )

    def get_capture(self, capture_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM captures WHERE id = ?", (int(capture_id),)
            ).fetchone()
        return _row_to_dict(row)

    def set_capture_image(
        self, capture_id: int, image_path: str | Path | None
    ) -> bool:
        """Attach (or clear) the screenshot path for an existing capture."""

        value = str(image_path) if image_path is not None else None
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE captures SET image_path = ? WHERE id = ?",
                (value, int(capture_id)),
            )
        return cursor.rowcount > 0

    def last_capture(
        self, app: str | None = None, window_title: str | None = None
    ) -> dict[str, Any] | None:
        clauses: list[str] = []
        params: list[Any] = []
        if app is not None:
            clauses.append("app = ? COLLATE NOCASE")
            params.append(app)
        if window_title is not None:
            clauses.append("window_title = ?")
            params.append(window_title)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self.conn.execute(
                f"SELECT * FROM captures {where} ORDER BY ts DESC, id DESC LIMIT 1",
                params,
            ).fetchone()
        return _row_to_dict(row)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM captures ORDER BY ts DESC, id DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def captures_between(self, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM captures
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC, id ASC
                """,
                (float(start_ts), float(end_ts)),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- search --------------------------------------------------------

    def search(
        self,
        query: str,
        start_ts: float | None = None,
        end_ts: float | None = None,
        app: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search with optional time and app filters."""

        match = fts_query(query)
        clauses: list[str] = []
        params: list[Any] = []
        if start_ts is not None:
            clauses.append("c.ts >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("c.ts < ?")
            params.append(float(end_ts))
        if app:
            clauses.append("c.app = ? COLLATE NOCASE")
            params.append(app)
        limit = max(0, int(limit))
        if limit == 0:
            return []
        fetch_limit = max(limit * 4, limit + 20)

        with self._lock:
            if match:
                where = ["captures_fts MATCH ?", *clauses]
                rows = self.conn.execute(
                    f"""
                    SELECT c.*, bm25(captures_fts) AS rank,
                           snippet(captures_fts, 0, '<mark>', '</mark>', '…', 24) AS snippet
                    FROM captures_fts
                    JOIN captures c ON c.id = captures_fts.rowid
                    WHERE {' AND '.join(where)}
                    ORDER BY rank ASC, c.ts DESC
                    LIMIT ?
                    """,
                    [match, *params, fetch_limit],
                ).fetchall()
            else:
                where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = self.conn.execute(
                    f"""
                    SELECT c.*, NULL AS rank, substr(c.text, 1, 320) AS snippet
                    FROM captures c
                    {where_sql}
                    ORDER BY c.ts DESC, c.id DESC
                    LIMIT ?
                    """,
                    [*params, fetch_limit],
                ).fetchall()
        return collapse_similar([dict(row) for row in rows], limit=limit)

    # -- timeline / usage ---------------------------------------------

    def timeline(self, day: str) -> list[dict[str, Any]]:
        start, end = day_bounds(day)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM activities
                WHERE end_ts > ? AND start_ts < ?
                ORDER BY start_ts ASC, id ASC
                """,
                (start, end),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            clipped_start = max(float(item["start_ts"]), start)
            clipped_end = min(float(item["end_ts"]), end)
            item["clipped_start_ts"] = clipped_start
            item["clipped_end_ts"] = clipped_end
            item["duration"] = max(0.0, clipped_end - clipped_start)
            result.append(item)
        return result

    def app_usage(self, day: str) -> list[dict[str, Any]]:
        start, end = day_bounds(day)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT app,
                       SUM(MIN(end_ts, ?) - MAX(start_ts, ?)) AS seconds,
                       SUM(capture_count) AS capture_count
                FROM activities
                WHERE end_ts > ? AND start_ts < ?
                GROUP BY app
                HAVING seconds > 0
                ORDER BY seconds DESC, app ASC
                """,
                (end, start, start, end),
            ).fetchall()
        return [
            {
                "app": row["app"],
                "seconds": float(row["seconds"] or 0.0),
                "capture_count": int(row["capture_count"] or 0),
            }
            for row in rows
        ]

    # -- deletion / retention -----------------------------------------

    def delete_range(self, start_ts: float, end_ts: float) -> dict[str, int]:
        """Delete captures and activity slices in ``[start_ts, end_ts)``."""

        start = float(start_ts)
        end = float(end_ts)
        if end <= start:
            return {"captures": 0, "activities": 0, "images_deleted": 0}

        images: list[str] = []
        deleted_captures = 0
        changed_activities = 0

        with self._lock, self.conn:
            for row in self.conn.execute(
                "SELECT image_path FROM captures WHERE ts >= ? AND ts < ? AND image_path IS NOT NULL",
                (start, end),
            ):
                if row["image_path"]:
                    images.append(row["image_path"])

            self.conn.execute(
                """
                UPDATE loops SET source_capture_id = NULL
                WHERE source_capture_id IN (
                    SELECT id FROM captures WHERE ts >= ? AND ts < ?
                )
                """,
                (start, end),
            )
            deleted_captures = self.conn.execute(
                "DELETE FROM captures WHERE ts >= ? AND ts < ?", (start, end)
            ).rowcount

            activities = self.conn.execute(
                "SELECT * FROM activities WHERE end_ts > ? AND start_ts < ?",
                (start, end),
            ).fetchall()
            for activity in activities:
                act_start = float(activity["start_ts"])
                act_end = float(activity["end_ts"])
                left = act_start < start
                right = act_end > end
                changed_activities += 1
                if left and right:
                    self.conn.execute(
                        "UPDATE activities SET end_ts = ? WHERE id = ?",
                        (start, activity["id"]),
                    )
                    self.conn.execute(
                        """
                        INSERT INTO activities
                            (app, bundle_id, window_title, start_ts, end_ts,
                             capture_count)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            activity["app"],
                            activity["bundle_id"],
                            activity["window_title"],
                            end,
                            act_end,
                            activity["capture_count"],
                        ),
                    )
                elif left:
                    self.conn.execute(
                        "UPDATE activities SET end_ts = ? WHERE id = ?",
                        (start, activity["id"]),
                    )
                elif right:
                    self.conn.execute(
                        "UPDATE activities SET start_ts = ? WHERE id = ?",
                        (end, activity["id"]),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM activities WHERE id = ?", (activity["id"],)
                    )

        images_deleted = _delete_images(images)
        return {
            "captures": int(deleted_captures),
            "activities": int(changed_activities),
            "images_deleted": images_deleted,
        }

    def prune(self, retention_days: int, now: float | None = None) -> dict[str, int]:
        """Forget captures and activities older than ``retention_days``."""

        current = time.time() if now is None else float(now)
        days = max(0, int(retention_days))
        cutoff = current - days * 86400.0

        images: list[str] = []
        deleted_captures = 0
        changed_activities = 0

        with self._lock, self.conn:
            for row in self.conn.execute(
                "SELECT image_path FROM captures WHERE ts < ? AND image_path IS NOT NULL",
                (cutoff,),
            ):
                if row["image_path"]:
                    images.append(row["image_path"])

            self.conn.execute(
                """
                UPDATE loops SET source_capture_id = NULL
                WHERE source_capture_id IN (SELECT id FROM captures WHERE ts < ?)
                """,
                (cutoff,),
            )
            deleted_captures = self.conn.execute(
                "DELETE FROM captures WHERE ts < ?", (cutoff,)
            ).rowcount

            # Activities fully inside the retention window continue unchanged.
            # Activities straddling the cutoff lose their older part.
            straddling = self.conn.execute(
                "SELECT id FROM activities WHERE start_ts < ? AND end_ts > ?",
                (cutoff, cutoff),
            ).fetchall()
            for row in straddling:
                self.conn.execute(
                    "UPDATE activities SET start_ts = ? WHERE id = ?",
                    (cutoff, row["id"]),
                )
                changed_activities += 1
            deleted_activities = self.conn.execute(
                "DELETE FROM activities WHERE end_ts <= ?", (cutoff,)
            ).rowcount
            changed_activities += int(deleted_activities)

        images_deleted = _delete_images(images)
        return {
            "captures": int(deleted_captures),
            "activities": int(changed_activities),
            "images_deleted": images_deleted,
        }

    # -- export / stats ------------------------------------------------

    def export_all(self) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "schema_version": SCHEMA_VERSION,
            }
            for table in _TABLES:
                rows = self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                data[table] = [dict(row) for row in rows]
            data["stats"] = self.stats()
        return data

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for table in _TABLES:
                counts[table] = int(
                    self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
            oldest, newest = self.conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM captures"
            ).fetchone()
            page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]

        result: dict[str, Any] = dict(counts)
        result["db_bytes"] = int(page_count) * int(page_size)
        result["oldest_ts"] = float(oldest) if oldest is not None else None
        result["newest_ts"] = float(newest) if newest is not None else None
        return result


def _delete_images(paths: Iterable[str]) -> int:
    deleted = 0
    for raw in paths:
        path = Path(raw)
        try:
            if not path.exists():
                continue
            path.unlink()
            deleted += 1
        except OSError:
            continue
    return deleted


def open_db(path: str | Path | None = None) -> Database:
    """Create a new :class:`Database` connection."""

    return Database(path)


# -- default-database convenience API ---------------------------------
#
# The class above is the primary API.  These wrappers make the module pleasant
# to use from a script or REPL without threading a connection through every
# call.  Connections are cached by resolved path, so a changed MAGPIE_HOME
# (as in tests) still gets its own database.

_default_databases: dict[str, Database] = {}
_default_databases_lock = threading.Lock()


def get_default_db(path: str | Path | None = None) -> Database:
    target = path if path is not None else config_module.db_path()
    key = ":memory:" if str(target) == ":memory:" else str(Path(target).expanduser().resolve())
    with _default_databases_lock:
        database = _default_databases.get(key)
        if database is None:
            database = Database(target)
            _default_databases[key] = database
        return database


def close_default_dbs() -> None:
    with _default_databases_lock:
        for database in _default_databases.values():
            database.close()
        _default_databases.clear()


def insert_capture(*args: Any, **kwargs: Any) -> int:
    return get_default_db().insert_capture(*args, **kwargs)


def set_capture_image(capture_id: int, image_path: str | Path | None) -> bool:
    return get_default_db().set_capture_image(capture_id, image_path)


def search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return get_default_db().search(*args, **kwargs)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    return get_default_db().recent(limit)


def captures_between(start_ts: float, end_ts: float) -> list[dict[str, Any]]:
    return get_default_db().captures_between(start_ts, end_ts)


def timeline(day: str) -> list[dict[str, Any]]:
    return get_default_db().timeline(day)


def app_usage(day: str) -> list[dict[str, Any]]:
    return get_default_db().app_usage(day)


def delete_range(start_ts: float, end_ts: float) -> dict[str, int]:
    return get_default_db().delete_range(start_ts, end_ts)


def prune(retention_days: int, now: float | None = None) -> dict[str, int]:
    return get_default_db().prune(retention_days, now=now)


def export_all() -> dict[str, Any]:
    return get_default_db().export_all()


def stats() -> dict[str, Any]:
    return get_default_db().stats()
