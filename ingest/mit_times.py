"""Give the MIT cross-registration listings real meeting times.

my.harvard lists ~2,000 MIT courses under `school=NONH` (subject `MIT`) and
publishes a meeting time for **none** of them. This pass fills those times in
from MIT's own Registrar: the *Subject Listing & Schedule* at
https://student.mit.edu/catalog/index.cgi.

Why the Registrar and not Hydrant: this used to read Hydrant's `latest.json`
(the student-run planner's one-file export). That had two structural problems.
It only ever carried MIT's CURRENT term, so the upcoming term -- the one
students are actually planning -- never got times until MIT's term had already
started; and it is a third party, whose TLS certificate expired on 2026-11-18
and failed every refresh after. The Registrar serves the upcoming term at the
catalog root as soon as it is published (IAP/Spring 2027 went up in November)
and keeps the previous fall and spring under `archive/`, so both terms this
site offers are covered.

Cost: ~94 department pages per term instead of one JSON request, at the same
polite delay as every other pass -- about a minute and a half. A term the
Harvard catalog has no MIT listings for is skipped before anything is fetched.

Schedule notation (MIT's own; verified against Hydrant's times for every Fall
2026 listing both sources timed):

    <b>Lecture:</b> <i>TR11,F2</i> (<a>2-135</a>) or <i>MW9.30-11</i> (<a>4-163</a>)

`or` separates ALTERNATIVE sections; commas join the meetings of ONE section.
A bare hour is a one-hour meeting ("MWF10" = 10-11). Daytime hours 8-12 are
AM/noon and 1-7 are PM ("9-5" is 9am-5pm); `EVE (6-9 PM)` is explicit. A block
that opens "Begins Mar 29." / "Ends Mar 19." is a half-term subject, and a
meeting can carry its own "(BEGINS APR 8)".

**Only unambiguous times are stored.** A third of MIT classes either publish no
lecture time or publish several ALTERNATIVE ones (6.1010 offers five lecture
times). Writing every alternative into `meetings` would make the course collide
with everything and read as unschedulable, so a class is timed only when its
lecture sections agree on a single pattern. Everything else stays untimed --
exactly where it was, so this can only add information, never distort it.

    python -m ingest.mit_times                     # default term
    python -m ingest.mit_times --term "2027 Spring"
"""
from __future__ import annotations

import argparse
import html as htmllib
import re
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    DAY_NAMES, DEFAULT_TERM, MAX_RETRIES, REQUEST_DELAY_SEC, REQUEST_TIMEOUT_SEC,
    USER_AGENT,
)
from ingest.db import connect, record_run
from ingest.parse import minutes_to_label

CATALOG_URL = "https://student.mit.edu/catalog/"
# Where the Registrar keeps each listing: the upcoming/current term at the root,
# the previous fall and spring under archive/. Which is which is read from each
# index page's title, never assumed -- the root flips between Fall and Spring.
LISTING_PATHS = ("", "archive/fall/", "archive/spring/")
# The academic calendar as iCal: the only place MIT states when its terms start
# and end. The subject listing gives half-term boundaries but not these.
CALENDAR_URL = "https://registrar.mit.edu/calendar-ical/current/all/all/calendar.ics"

# MIT day letters. DAY_NAMES counts from Sunday = bit 0.
DAY_BITS = {"M": 1, "T": 2, "W": 3, "R": 4, "F": 5, "S": 6}
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _ssl_context() -> ssl.SSLContext:
    """Certificate-verifying context restricted to ECDHE + AES-GCM.

    student.mit.edu prefers a DHE suite whose Diffie-Hellman key is too small
    for OpenSSL 3, so a default handshake fails with DH_KEY_TOO_SMALL. It also
    offers ECDHE, which this context forces. That is a STRICTER cipher list than
    the default, not a weaker one, and certificate checks are untouched.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("ECDHE+AESGCM")
    return ctx


_CTX = _ssl_context()


def fetch(url: str) -> str:
    """GET with retries; raises once every attempt has failed."""
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC, context=_CTX) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001 - retry any transport/decode failure
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SEC * 2 * attempt)
    raise RuntimeError(f"{url} failed after {MAX_RETRIES} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Which listing, and when does its term run
# ---------------------------------------------------------------------------

def listing_term(index_html: str) -> str | None:
    """'MIT Subject Listing & Schedule IAP/Spring 2027' -> '2027 Spring'."""
    m = re.search(r"<title>[^<]*?(Fall|Spring)\s+(\d{4})\s*</title>", index_html, re.I)
    return f"{m.group(2)} {m.group(1).title()}" if m else None


def find_listing(term: str) -> tuple[str, str] | None:
    """(base URL, index HTML) of the Registrar listing for `term`, if MIT serves one."""
    for path in LISTING_PATHS:
        base = CATALOG_URL + path
        index = fetch(base + "index.cgi")
        if listing_term(index) == term:
            return base, index
    return None


def term_bounds(term: str, ics: str) -> tuple[str | None, str | None]:
    """First and last day of classes for `term`, from the Registrar's calendar.

    The summaries are exactly "First day of classes." / "Last day of classes.";
    the summer-session and half-term events say more after "classes", which is
    why the match is anchored on the period.
    """
    year, season = term.split()
    months = range(8, 13) if season == "Fall" else range(1, 7)
    first = last = None
    for ev in ics.replace("\r\n ", "").replace("\r\n", "\n").split("BEGIN:VEVENT")[1:]:
        s = re.search(r"^SUMMARY[^:\n]*:(.*)$", ev, re.M)
        d = re.search(r"^DTSTART[^:\n]*:(\d{4})(\d{2})(\d{2})", ev, re.M)
        if not (s and d) or d.group(1) != year or int(d.group(2)) not in months:
            continue
        iso = f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
        summary = s.group(1).strip().lower()
        if summary == "first day of classes.":
            first = iso
        elif summary == "last day of classes.":
            last = iso
    return first, last


# ---------------------------------------------------------------------------
# Parsing the listing
# ---------------------------------------------------------------------------

def department_pages(base: str, index_html: str, delay: float = REQUEST_DELAY_SEC):
    """Yield every department page: m1a.html, then m1b.html, m1c.html ... as linked."""
    todo = sorted(set(re.findall(r'href="(m[^"/]+a\.html)"', index_html)))
    seen: set[str] = set()
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.add(name)
        page = fetch(base + name)
        yield page
        stem = re.escape(name[:-len("a.html")])
        todo.extend(n for n in re.findall(rf'href="({stem}[a-z]\.html)"', page) if n not in seen)
        time.sleep(delay)


def split_subjects(page: str) -> dict[str, str]:
    """{subject number: its HTML block} for one department page.

    Each subject opens with `<a name="6.1010">` and closes with `<!--end-->`.
    A joint subject is listed under one number with "(Same subject as ...)",
    so its other numbers are indexed to the same block -- my.harvard sometimes
    lists it under the other one.
    """
    out: dict[str, str] = {}
    for chunk in re.split(r'<a name="', page)[1:]:
        number = chunk[:chunk.find('"')]
        block = chunk[:chunk.find("<!--end-->")]
        out.setdefault(number, block)
        same = re.search(r"\(Same subject as (.*?)\)", block, re.S)
        if same:
            # From the link target: the link text carries a "[J]" suffix.
            for alias in re.findall(r'#([^"]+)"', same.group(1)):
                out.setdefault(alias, block)
    return out


@dataclass(frozen=True)
class Meeting:
    day_mask: int
    start_min: int
    end_min: int
    start_date: str | None
    end_date: str | None


def _clock(text: str, evening: bool) -> int | None:
    """'9.30' -> minutes past midnight, with MIT's daytime AM/PM convention."""
    m = re.fullmatch(r"(\d{1,2})(?:\.(\d{2}))?", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 12 or minute >= 60:
        return None
    if hour != 12 and (evening or hour < 8):
        hour += 12
    return hour * 60 + minute


def _date(text: str, year: int) -> str | None:
    """'Mar 29' / 'APR 8' -> ISO date in the term's year."""
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})", text.strip())
    if not m or m.group(1).upper() not in MONTHS:
        return None
    try:
        return date(year, MONTHS[m.group(1).upper()], int(m.group(2))).isoformat()
    except ValueError:
        return None


PIECE_RE = re.compile(
    r"(?P<days>[MTWRFS]+)\s*"
    r"(?:EVE\s*\(\s*(?P<eve>[\d.]+(?:\s*-\s*[\d.]+)?)\s*PM\s*\)|(?P<day>[\d.]+(?:-[\d.]+)?))"
    r"\s*(?:\(\s*(?P<note>[^)]*)\))?"
)


def parse_section(times: str, year: int, begins: str | None, ends: str | None
                  ) -> tuple[Meeting, ...] | None:
    """One section's '<i>' text, e.g. 'TR11,F2' -> its meetings. None if untimed.

    `begins`/`ends` are the block's half-term dates (or the term's own bounds);
    a meeting's own "(BEGINS APR 8)" overrides them.
    """
    meetings = []
    for piece in times.split(","):
        m = PIECE_RE.fullmatch(piece.strip())
        if not m:
            return None                 # TBA, or notation we do not understand
        evening = m.group("eve") is not None
        span = (m.group("eve") or m.group("day")).replace(" ", "").split("-")
        start = _clock(span[0], evening)
        end = _clock(span[1], evening) if len(span) > 1 else (start + 60 if start is not None else None)
        if start is None or end is None or end <= start:
            return None
        mask = 0
        for d in m.group("days"):
            mask |= 1 << DAY_BITS[d]
        start_date, end_date = begins, ends
        note = (m.group("note") or "").upper()
        if note.startswith("BEGINS "):
            start_date = _date(note[len("BEGINS "):], year) or start_date
        elif note.startswith("ENDS "):
            end_date = _date(note[len("ENDS "):], year) or end_date
        elif note:
            return None                 # an annotation we cannot interpret
        meetings.append(Meeting(mask, start, end, start_date, end_date))
    # Meetings sharing a span collapse into ONE row with a combined day mask,
    # which is the shape ingest/parse.py produces for my.harvard courses --
    # "MW 9:00-10:30" is a single meeting, not two.
    merged: dict[tuple, int] = {}
    for m in meetings:
        span = (m.start_min, m.end_min, m.start_date, m.end_date)
        merged[span] = merged.get(span, 0) | m.day_mask
    return tuple(sorted((Meeting(mask, s, e, sd, ed) for (s, e, sd, ed), mask in merged.items()),
                        key=lambda x: (x.start_min, x.day_mask))) or None


SECTION_RE = re.compile(r"<i>([^<]*)</i>\s*(?:\(\s*(?:<a[^>]*>)?([^<)]*)(?:</a>)?\s*\))?")


def lecture_sections(block: str, year: int, term_start: str | None, term_end: str | None
                     ) -> list[tuple[tuple[Meeting, ...] | None, str]]:
    """Every lecture section the subject offers in the REGULAR term, as
    (meetings or None if untimed, room).

    A subject's schedule is one or more `...<!--s-->` blocks. Blocks marked with
    the "Pre-register for IAP" button are January (IAP) offerings and are
    skipped: attaching them to the spring listing would put a January intensive
    on every week of the spring term. Archived listings carry no buttons at all,
    but they also drop IAP schedules entirely, so there is nothing to skip there.
    """
    out = []
    for raw in block.split("<!--s-->")[:-1]:
        sched = raw[raw.rfind("<br>"):] if "<br>" in raw else raw
        if "iapprereg" in sched or re.search(r"<i>\s*IAP:\s*</i>", sched):
            continue
        text = re.sub(r"<[^>]+>", " ", sched)
        begins = re.search(r"\bBegins ([A-Z][a-z]{2} \d{1,2})\.", text)
        ends = re.search(r"\bEnds ([A-Z][a-z]{2} \d{1,2})\.", text)
        b = (_date(begins.group(1), year) if begins else None) or term_start
        e = (_date(ends.group(1), year) if ends else None) or term_end
        for kind, body in re.findall(r"<b>([^<:]+):</b>(.*?)(?=<b>[^<:]+:</b>|$)", sched, re.S):
            if kind.strip() != "Lecture":
                continue
            for times, room in SECTION_RE.findall(body):
                times = htmllib.unescape(times).strip()
                if times.startswith("+"):
                    continue            # "+final" marker, not a section
                out.append((parse_section(times, year, b, e), htmllib.unescape(room).strip()))
    return out


def lecture_pattern(sections) -> tuple[tuple[Meeting, ...], str] | None:
    """The class's one agreed lecture pattern and room, or None if ambiguous.

    Any untimed (TBA) lecture section makes the class ambiguous too: "TR9 or
    TBA" does not tell a student when they will be in class. The room is
    dropped when the agreeing sections disagree about it.
    """
    if not sections or any(m is None for m, _ in sections):
        return None
    patterns = {m for m, _ in sections}
    if len(patterns) != 1:
        return None
    rooms = {room for _, room in sections}
    return patterns.pop(), (rooms.pop() if len(rooms) == 1 else "")


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def backfill(term: str = DEFAULT_TERM, delay: float = REQUEST_DELAY_SEC) -> dict:
    empty = {"matched": 0, "timed": 0, "ambiguous": 0, "unmatched": 0}

    # Cheap check first: no MIT listings on the Harvard side means nothing to
    # time, and ~94 Registrar pages would be fetched for nothing.
    conn = connect()
    rows = conn.execute(
        "SELECT key, code FROM courses WHERE term = ? AND school = 'NONH' AND subject = 'MIT'",
        (term,),
    ).fetchall()
    if not rows:
        print(f"term={term!r}: no MIT listings in the catalog")
        record_run(conn, "mit_times", term, "skipped", detail="no MIT listings in the catalog")
        conn.close()
        return empty
    conn.close()

    # A fetch failure has to be recorded, not just raised: the workflow lets
    # this pass fail without failing the run, so an unrecorded failure would
    # leave the UI showing whatever timestamp the last good run wrote.
    try:
        found = find_listing(term)
        if found is None:
            print(f"term={term!r}: the MIT Registrar does not list this term")
            conn = connect()
            record_run(conn, "mit_times", term, "skipped",
                       detail="the MIT Registrar does not list this term")
            conn.close()
            return empty
        base, index = found
        subjects: dict[str, str] = {}
        pages = 0
        for page in department_pages(base, index, delay):
            pages += 1
            for number, block in split_subjects(page).items():
                subjects.setdefault(number, block)
    except Exception as e:  # noqa: BLE001
        conn = connect()
        record_run(conn, "mit_times", term, f"error: {e}"[:200])
        conn.close()
        raise
    print(f"listing: {base} -> {term!r}, {pages} pages, {len(subjects)} subjects")

    # Term bounds are an enrichment of an enrichment: without them meetings are
    # stored undated, which the conflict check treats as "always running" -- the
    # same as before this pass existed. Never worth failing over.
    try:
        term_start, term_end = term_bounds(term, fetch(CALENDAR_URL))
    except Exception as e:  # noqa: BLE001
        print(f"warning: MIT calendar unavailable ({e}); meetings stay undated")
        term_start = term_end = None
    print(f"MIT term runs {term_start} .. {term_end}")
    year = int(term.split()[0])

    conn = connect()
    # Clear this pass's own earlier output for the term first, so a subject MIT
    # has since cancelled, or given a second alternative time, loses its stale
    # meeting instead of keeping it until the next full crawl happens to
    # rewrite it. Only 'mit_feed' rows: nothing else is this pass's to delete.
    conn.execute(
        """DELETE FROM meetings WHERE date_source = 'mit_feed' AND course_key IN
           (SELECT key FROM courses WHERE term = ? AND school = 'NONH' AND subject = 'MIT')""",
        (term,),
    )
    matched = timed = ambiguous = unmatched = roomed = meetings_written = 0
    for r in rows:
        # my.harvard renders the MIT number as the code with an "MIT" prefix:
        # "MIT1.000" is the Registrar's "1.000".
        number = r["code"][3:] if r["code"].startswith("MIT") else r["code"]
        block = subjects.get(number)
        if block is None:
            unmatched += 1
            continue
        matched += 1

        found = lecture_pattern(lecture_sections(block, year, term_start, term_end))
        if found is None:
            ambiguous += 1
            continue
        pattern, room = found

        # Replace rather than append, so a re-run is idempotent.
        conn.execute("DELETE FROM meetings WHERE course_key = ?", (r["key"],))
        for m in pattern:
            conn.execute(
                """INSERT INTO meetings (course_key, day_mask, start_min, end_min,
                                         raw_time, start_date, end_date, date_source,
                                         location)
                   VALUES (?,?,?,?,?,?,?,'mit_feed',?)""",
                (r["key"], m.day_mask, m.start_min, m.end_min,
                 _label(m.day_mask, m.start_min, m.end_min), m.start_date, m.end_date,
                 room or None),
            )
            meetings_written += 1
        timed += 1
        roomed += bool(room)

    conn.commit()
    record_run(conn, "mit_times", term, "ok", courses=timed,
               detail=f"{timed} timed, {roomed} with a room; from the MIT Registrar's "
                      f"{term} subject listing")
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
    ap = argparse.ArgumentParser(description="Backfill MIT meeting times from the MIT Registrar")
    ap.add_argument("--term", default=DEFAULT_TERM)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    args = ap.parse_args()
    backfill(args.term, args.delay)


if __name__ == "__main__":
    main()
