"""SQLite storage for the course catalog, with change tracking.

Course times shift daily during shopping week, so every ingest diffs meeting
patterns against what's already stored and logs what moved. That's what powers
the "3 of your candidates changed time" alert.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DB_PATH

if TYPE_CHECKING:      # ingest.parse imports BeautifulSoup, which the read-only
    from ingest.parse import Course   # API server has no need to install.

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    key             TEXT PRIMARY KEY,
    course_id       TEXT NOT NULL,
    crse_offer_nbr  TEXT,
    code            TEXT NOT NULL,
    subject         TEXT,
    catalog         TEXT,
    section         TEXT,
    term            TEXT NOT NULL,
    title           TEXT,
    school          TEXT,
    department      TEXT,
    description     TEXT,
    session         TEXT,
    detail_url      TEXT,
    instructors     TEXT,           -- JSON array
    first_seen      TEXT,
    last_seen       TEXT
);
CREATE INDEX IF NOT EXISTS idx_courses_term   ON courses(term);
CREATE INDEX IF NOT EXISTS idx_courses_school ON courses(school);
CREATE INDEX IF NOT EXISTS idx_courses_code   ON courses(code);

CREATE TABLE IF NOT EXISTS meetings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_key  TEXT NOT NULL REFERENCES courses(key) ON DELETE CASCADE,
    day_mask    INTEGER NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL,
    raw_time    TEXT,
    start_date  TEXT,
    end_date    TEXT
);
CREATE INDEX IF NOT EXISTS idx_meetings_course ON meetings(course_key);

CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_key  TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,     -- added | removed | time_changed
    old_value   TEXT,
    new_value   TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_key  ON changes(course_key);
CREATE INDEX IF NOT EXISTS idx_changes_time ON changes(changed_at);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    term         TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    pages        INTEGER,
    courses      INTEGER,
    status       TEXT
);

"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
# them to an existing database, so apply them explicitly.
#
# The user_schedule / student_profile / plan_items tables were removed in phase
# 1: personal state now lives in the browser and the server is stateless. See
# ingest/seal.py, which drops them from any database that still has them.
MIGRATIONS = {
    "meetings": [
        ("date_source", "TEXT"),   # 'detail' | 'session_default' | NULL (unknown)
    ],
    "courses": [
        # HBS MBA cross-registrant auditor rule, scraped by ingest/hbs_notes.py
        # from the HBS catalog because my.harvard stores only a link to it.
        # 'open' | 'limited' | 'closed' | NULL (not an HBS MBA section, or the
        # note was missing/unparseable).
        ("auditors", "TEXT"),
        ("auditor_note", "TEXT"),  # the note verbatim, shown in the UI
    ],
}


def migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A crawl or date backfill can run for minutes while people are browsing.
    # WAL lets readers work during a write; busy_timeout makes concurrent
    # writers wait for the lock instead of failing with "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def connect_readonly(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the catalog for reading only, touching nothing on disk.

    `connect()` cannot be used by the deployed API: it mkdirs, sets
    journal_mode=WAL, runs the schema script and applies migrations -- four
    writes, on a platform whose filesystem is read-only. Serverless hosts also
    give each invocation its own container, so a write would be pointless even
    where it succeeded.

    The DB must be sealed (see ingest/seal.py) before it ships. A WAL-mode
    database needs to write to its -wal sidecar even to be read, so an unsealed
    file fails here with "attempt to write a readonly database".
    """
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _meeting_signature(rows: Iterable) -> str:
    """Order-independent fingerprint of a course's meeting pattern."""
    parts = sorted(f"{r['day_mask']}:{r['start_min']}-{r['end_min']}" for r in rows)
    return ";".join(parts) or "TBA"


def _course_signature(course: "Course") -> str:
    parts = sorted(f"{m.day_mask}:{m.start_min}-{m.end_min}" for m in course.meetings)
    return ";".join(parts) or "TBA"


def upsert_courses(conn: sqlite3.Connection, courses: list["Course"]) -> dict:
    """Insert or update courses, logging any meeting-pattern changes.

    Returns counts of {new, changed, unchanged}.
    """
    ts = now_iso()
    stats = {"new": 0, "changed": 0, "unchanged": 0}

    for c in courses:
        existing = conn.execute(
            "SELECT key FROM courses WHERE key = ?", (c.key,)
        ).fetchone()

        old_sig = None
        if existing:
            old_rows = conn.execute(
                "SELECT day_mask, start_min, end_min FROM meetings WHERE course_key = ?",
                (c.key,),
            ).fetchall()
            old_sig = _meeting_signature(old_rows)

        conn.execute(
            """
            INSERT INTO courses (key, course_id, crse_offer_nbr, code, subject, catalog,
                                 section, term, title, school, department, description,
                                 session, detail_url, instructors, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                title=excluded.title, school=excluded.school,
                department=excluded.department, description=excluded.description,
                session=excluded.session, detail_url=excluded.detail_url,
                instructors=excluded.instructors, last_seen=excluded.last_seen
            """,
            (
                c.key, c.course_id, c.crse_offer_nbr, c.code, c.subject, c.catalog,
                c.section, c.term, c.title, c.school, c.department, c.description,
                c.session, c.detail_url, json.dumps(c.instructors), ts, ts,
            ),
        )

        # Meetings are small and fully derived; replace rather than diff-patch.
        conn.execute("DELETE FROM meetings WHERE course_key = ?", (c.key,))
        for m in c.meetings:
            conn.execute(
                """INSERT INTO meetings (course_key, day_mask, start_min, end_min, raw_time)
                   VALUES (?,?,?,?,?)""",
                (c.key, m.day_mask, m.start_min, m.end_min, m.raw_time),
            )

        new_sig = _course_signature(c)
        if existing is None:
            stats["new"] += 1
            conn.execute(
                "INSERT INTO changes (course_key, changed_at, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                (c.key, ts, "added", None, new_sig),
            )
        elif old_sig != new_sig:
            stats["changed"] += 1
            conn.execute(
                "INSERT INTO changes (course_key, changed_at, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                (c.key, ts, "time_changed", old_sig, new_sig),
            )
        else:
            stats["unchanged"] += 1

    conn.commit()
    return stats


def start_run(conn: sqlite3.Connection, term: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs (term, started_at, status) VALUES (?,?,?)",
        (term, now_iso(), "running"),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, pages: int, courses: int, status: str = "ok") -> None:
    conn.execute(
        "UPDATE ingest_runs SET finished_at=?, pages=?, courses=?, status=? WHERE id=?",
        (now_iso(), pages, courses, status, run_id),
    )
    conn.commit()
