"""Backfill real meeting date ranges from course detail pages.

Search cards carry a session LABEL ("Full Term") but no dates. That is fine for
most of the catalog, but the professional schools -- HSPH, HSDM, HGSE, HKS,
HBSM, HMS -- run 7-week sessions, modules and quarters, and their cards render
an EMPTY session. Without real dates, two courses in non-overlapping half-terms
look like a conflict when they aren't.

Those schools are exactly the ones MDE rule 6 lets you take up to four of, so
this matters. Only courses whose dates we can't already infer are fetched, which
keeps this to a few hundred requests rather than one per course.

    python -m ingest.dates                 # backfill unknown-session courses
    python -m ingest.dates --all           # re-fetch everything (slow)
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    BASE, DEFAULT_TERM, MAX_RETRIES, REQUEST_DELAY_SEC, REQUEST_TIMEOUT_SEC, USER_AGENT,
)
from ingest.db import connect

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "Sept. 8, 2026" / "Dec 4, 2026"
DATE_RE = re.compile(r"\b([A-Z][a-z]{2,4})\.?\s+(\d{1,2}),\s*(\d{4})\b")


def parse_date(token: str) -> str | None:
    m = DATE_RE.fullmatch(token.strip())
    if not m:
        return None
    mon = MONTHS.get(m.group(1).lower().rstrip("."))
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def extract_range(html: str) -> tuple[str | None, str | None]:
    """First date pair on the detail page is the meeting's date range."""
    found = []
    for m in DATE_RE.finditer(html):
        mon = MONTHS.get(m.group(1).lower().rstrip("."))
        if not mon:
            continue
        found.append(f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}")
        if len(found) == 2:
            break
    if len(found) == 2 and found[0] <= found[1]:
        return found[0], found[1]
    return None, None


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"user-agent": USER_AGENT, "accept": "text/html"})
    return s


def fetch(session: requests.Session, url: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(BASE + url, timeout=REQUEST_TIMEOUT_SEC)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text
        except Exception:  # noqa: BLE001
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SEC * 2 * attempt)
    return None


def learn_full_term_range(conn, session, term: str, sample: int = 15) -> None:
    """Fetch a few Full Term courses so we know what "Full Term" actually means.

    The main pass only fetches unknown-session courses, so without this no
    Full Term course ever gets real dates and the modal range stays unknown.
    """
    rows = conn.execute(
        """SELECT DISTINCT c.key, c.detail_url FROM courses c
           JOIN meetings m ON m.course_key = c.key
           WHERE c.term = ? AND c.session = 'Full Term' AND m.start_date IS NULL
             AND c.detail_url != '' LIMIT ?""",
        (term, sample),
    ).fetchall()
    if not rows:
        return
    print(f"sampling {len(rows)} Full Term course(s) to learn the term range")
    for r in rows:
        html = fetch(session, r["detail_url"])
        start, end = extract_range(html) if html else (None, None)
        if start:
            conn.execute(
                "UPDATE meetings SET start_date=?, end_date=?, date_source='detail' "
                "WHERE course_key=?", (start, end, r["key"]))
        time.sleep(REQUEST_DELAY_SEC)
    conn.commit()


def full_term_range(conn, term: str) -> tuple[str | None, str | None]:
    """Modal Full Term range, learned from courses we've already dated."""
    row = conn.execute(
        """SELECT m.start_date, m.end_date, COUNT(*) n
           FROM meetings m JOIN courses c ON c.key = m.course_key
           WHERE c.term = ? AND c.session = 'Full Term'
             AND m.start_date IS NOT NULL AND m.date_source = 'detail'
           GROUP BY m.start_date, m.end_date ORDER BY n DESC LIMIT 1""",
        (term,),
    ).fetchone()
    return (row["start_date"], row["end_date"]) if row else (None, None)


def backfill(term: str = DEFAULT_TERM, do_all: bool = False,
             delay: float = REQUEST_DELAY_SEC, limit: int | None = None) -> dict:
    conn = connect()
    session = make_session()

    where = "AND m.start_date IS NULL"
    if not do_all:
        # Unknown session is the risky set: those schools run partial terms.
        where += " AND (c.session IS NULL OR c.session = '')"

    rows = conn.execute(
        f"""SELECT DISTINCT c.key, c.detail_url, c.school, c.session
            FROM courses c JOIN meetings m ON m.course_key = c.key
            WHERE c.term = ? {where} AND c.detail_url != ''""",
        (term,),
    ).fetchall()
    if limit:
        rows = rows[:limit]

    print(f"term={term!r}: {len(rows)} course(s) need date ranges")
    got = miss = 0
    for i, r in enumerate(rows, 1):
        html = fetch(session, r["detail_url"])
        start, end = extract_range(html) if html else (None, None)
        if start:
            conn.execute(
                "UPDATE meetings SET start_date=?, end_date=?, date_source='detail' "
                "WHERE course_key=?",
                (start, end, r["key"]),
            )
            got += 1
        else:
            miss += 1
        if i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}  dated={got}  no-date={miss}")
        time.sleep(delay)
    conn.commit()

    # Everything still undated but labelled "Full Term" can safely inherit the
    # term's modal range -- that is what the label means.
    learn_full_term_range(conn, session, term)
    fs, fe = full_term_range(conn, term)
    filled = 0
    if fs:
        cur = conn.execute(
            """UPDATE meetings SET start_date=?, end_date=?, date_source='session_default'
               WHERE start_date IS NULL AND course_key IN (
                 SELECT key FROM courses WHERE term=? AND session='Full Term')""",
            (fs, fe, term),
        )
        filled = cur.rowcount
        conn.commit()
        print(f"applied Full Term range {fs}..{fe} to {filled} meeting(s)")

    total = conn.execute(
        """SELECT COUNT(*) FROM meetings m JOIN courses c ON c.key=m.course_key
           WHERE c.term=?""", (term,)).fetchone()[0]
    dated = conn.execute(
        """SELECT COUNT(*) FROM meetings m JOIN courses c ON c.key=m.course_key
           WHERE c.term=? AND m.start_date IS NOT NULL""", (term,)).fetchone()[0]
    conn.close()
    print(f"done: {dated}/{total} meetings have a date range")
    return {"fetched": len(rows), "dated": got, "no_date": miss, "session_default": filled}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill meeting date ranges")
    ap.add_argument("--term", default=DEFAULT_TERM)
    ap.add_argument("--all", action="store_true", help="re-fetch every course")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    args = ap.parse_args()
    backfill(args.term, args.all, args.delay, args.limit)


if __name__ == "__main__":
    main()
