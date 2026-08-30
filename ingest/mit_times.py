"""Give the MIT cross-registration listings real meeting times.

my.harvard lists ~1,987 MIT courses under `school=NONH` (subject `MIT`) and
publishes a meeting time for **none** of them. Until now the only way to
schedule around an MIT cross-registration was to retype its time by hand as a
custom block.

MIT publishes the whole term as one JSON document -- Hydrant, the student-built
course planner, serves `latest.json` with every class, its sections and its
meeting slots. So this is ONE request for the entire catalog, not one per
course, and it costs seconds rather than the ~10 minutes the my.harvard crawl
takes.

Slot encoding (verified against Hydrant's own `lectureRawSections` strings for
1,893 sections, zero mismatches):

    slot = day * 34 + half_hours_since_06:00      day 0 = Monday

so slot 44 is Tuesday 11:00, and `[44, 3]` is Tuesday 11:00-12:30.

**Only unambiguous times are stored.** A third of MIT classes either publish no
lecture section at all or publish several ALTERNATIVE ones (6.1010 offers four
lecture times in the same room). Writing every alternative into `meetings`
would make the course collide with everything and read as unschedulable, so a
class is timed only when its lecture sections agree on a single pattern.
Everything else stays untimed -- exactly where it is today, so this can only
add information, never remove or distort it.

    python -m ingest.mit_times                     # default term
    python -m ingest.mit_times --term "2026 Fall"
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DAY_NAMES, DEFAULT_TERM, MAX_RETRIES, REQUEST_DELAY_SEC, REQUEST_TIMEOUT_SEC,
    USER_AGENT,
)
from ingest.db import connect, record_run
from ingest.parse import minutes_to_label

FEED_URL = "https://hydrant.mit.edu/latest.json"

# Hydrant's slot grid. Both constants are load-bearing: see the module docstring.
SLOTS_PER_DAY = 34
GRID_START_MIN = 6 * 60

SEASONS = {"f": "Fall", "s": "Spring"}   # i (IAP) and m (summer) have no Harvard term


def fetch(url: str = FEED_URL) -> dict:
    """One request, so retry it -- there is no partial progress to fall back on.

    A single transient timeout here once failed the whole refresh and discarded
    a completed ten-minute crawl, because this was the only ingest module
    without the retry every sibling has.
    """
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - retry any transport/parse failure
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SEC * 2 * attempt)
    raise RuntimeError(f"{url} failed after {MAX_RETRIES} attempts: {last_err}")


def feed_term(url_name: str) -> str | None:
    """'f26' -> '2026 Fall'. None for IAP/summer, which Harvard has no term for."""
    if len(url_name) < 3 or url_name[0] not in SEASONS or not url_name[1:].isdigit():
        return None
    return f"20{url_name[1:]} {SEASONS[url_name[0]]}"


def decode_slot(slot: int) -> tuple[int, int]:
    """Hydrant slot -> (day_mask bit position, minutes past midnight).

    Hydrant counts days from Monday; DAY_NAMES counts from Sunday, so the day
    index shifts by one.
    """
    day, offset = divmod(slot, SLOTS_PER_DAY)
    return day + 1, GRID_START_MIN + offset * 30


def lecture_pattern(cls: dict) -> tuple[list[list[int]], str] | None:
    """The class's one agreed lecture pattern and room, or None if ambiguous.

    Returns (Hydrant's raw [[start_slot, half_hours], ...], room). The room is
    dropped when the agreeing sections disagree about it, which is rare but
    would otherwise put one section's room on another's meeting.
    """
    sections = cls.get("lectureSections") or []
    if not sections:
        return None
    patterns = {tuple(sorted(tuple(s) for s in sec[0])) for sec in sections}
    if len(patterns) != 1:
        return None          # alternative meeting times -- ambiguous, so skip
    pattern = [list(s) for s in patterns.pop()]
    if not pattern:
        return None
    rooms = {(sec[1] or "").strip() for sec in sections}
    return pattern, (rooms.pop() if len(rooms) == 1 else "")


def to_meetings(pattern: list[list[int]]) -> list[tuple[int, int, int]]:
    """Slots -> [(day_mask, start_min, end_min)].

    Slots that share a start and end collapse into ONE row with a combined day
    mask, which is the shape ingest/parse.py produces for my.harvard courses --
    "MW 9:00-10:30" is a single meeting, not two.
    """
    by_span: dict[tuple[int, int], int] = {}
    for slot, half_hours in pattern:
        bit, start = decode_slot(slot)
        end = start + half_hours * 30
        by_span[(start, end)] = by_span.get((start, end), 0) | (1 << bit)
    return [(mask, s, e) for (s, e), mask in sorted(by_span.items())]


def date_range(cls: dict, info: dict) -> tuple[str | None, str | None]:
    """MIT publishes half-term flags, so a first-half class stops conflicting
    with a second-half one -- the same win ingest/dates.py buys for Harvard."""
    half = cls.get("half")
    if half == 1:
        return info.get("startDate"), info.get("h1EndDate")
    if half == 2:
        return info.get("h2StartDate"), info.get("endDate")
    return info.get("startDate"), info.get("endDate")


def backfill(term: str = DEFAULT_TERM, url: str = FEED_URL) -> dict:
    # A fetch failure has to be recorded, not just raised: the workflow lets
    # this pass fail without failing the run, so an unrecorded failure would
    # leave the UI showing whatever timestamp the last good run wrote.
    try:
        data = fetch(url)
    except Exception as e:  # noqa: BLE001
        conn = connect()
        record_run(conn, "mit_times", term, f"error: {e}"[:200])
        conn.close()
        raise
    info = data.get("termInfo") or {}
    feed = feed_term(info.get("urlName", ""))
    classes = data.get("classes") or {}
    print(f"feed: {info.get('urlName')!r} -> {feed!r}, {len(classes)} classes, "
          f"updated {data.get('lastUpdated')}")

    # The feed only ever carries MIT's CURRENT term, so a mismatch is normal
    # (and the reason this is a no-op rather than an error).
    if feed != term:
        print(f"term={term!r}: feed covers {feed!r} -- nothing to do")
        conn = connect()
        record_run(conn, "mit_times", term, "skipped",
                   detail=f"MIT publishes only its current term ({feed})")
        conn.close()
        return {"matched": 0, "timed": 0, "ambiguous": 0, "unmatched": 0}

    conn = connect()
    rows = conn.execute(
        "SELECT key, code FROM courses WHERE term = ? AND school = 'NONH' AND subject = 'MIT'",
        (term,),
    ).fetchall()
    if not rows:
        print(f"term={term!r}: no MIT listings in the catalog")
        record_run(conn, "mit_times", term, "skipped", detail="no MIT listings in the catalog")
        conn.close()
        return {"matched": 0, "timed": 0, "ambiguous": 0, "unmatched": 0}

    matched = timed = ambiguous = unmatched = roomed = 0
    meetings_written = 0

    for r in rows:
        # my.harvard renders the MIT number as the code with an "MIT" prefix:
        # "MIT1.000" is Hydrant's "1.000".
        number = r["code"][3:] if r["code"].startswith("MIT") else r["code"]
        cls = classes.get(number)
        if cls is None:
            unmatched += 1
            continue
        matched += 1

        found = lecture_pattern(cls)
        if found is None:
            ambiguous += 1
            continue
        pattern, room = found

        start_date, end_date = date_range(cls, info)
        # Replace rather than append: this pass has to be idempotent, and a
        # re-crawl clears these rows anyway (upsert_courses rewrites meetings).
        conn.execute("DELETE FROM meetings WHERE course_key = ?", (r["key"],))
        for mask, start, end in to_meetings(pattern):
            conn.execute(
                """INSERT INTO meetings (course_key, day_mask, start_min, end_min,
                                         raw_time, start_date, end_date, date_source,
                                         location)
                   VALUES (?,?,?,?,?,?,?,'mit_feed',?)""",
                (r["key"], mask, start, end, _label(mask, start, end), start_date,
                 end_date, room or None),
            )
            meetings_written += 1
        timed += 1
        roomed += bool(room)

    conn.commit()
    # The feed's own timestamp matters as much as ours: it says how fresh MIT's
    # data is, independent of when we last managed to fetch it.
    record_run(conn, "mit_times", term, "ok", courses=timed,
               detail=f"{timed} timed, {roomed} with a room; MIT feed updated "
                      f"{data.get('lastUpdated', 'unknown')}")
    conn.close()
    print(f"done: {len(rows)} MIT listing(s)  matched={matched}  timed={timed}  "
          f"({meetings_written} meetings, {roomed} with a room)  "
          f"no-single-pattern={ambiguous}  unmatched={unmatched}")
    return {"matched": matched, "timed": timed, "ambiguous": ambiguous,
            "unmatched": unmatched, "meetings": meetings_written, "roomed": roomed}


def _label(mask: int, start: int, end: int) -> str:
    days = "".join(DAY_NAMES[i][:3] + " " for i in range(7) if mask & (1 << i)).strip()
    return f"{days} {minutes_to_label(start)} - {minutes_to_label(end)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill MIT meeting times from Hydrant")
    ap.add_argument("--term", default=DEFAULT_TERM)
    ap.add_argument("--url", default=FEED_URL)
    args = ap.parse_args()
    backfill(args.term, args.url)


if __name__ == "__main__":
    main()
