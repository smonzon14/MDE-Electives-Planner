"""Detect cross-listed / jointly-offered courses.

The policy treats GSD<->SEAS cross-lists specially (count as one or the other,
never both), but my.harvard gives each school's listing its own course code and
does not link them. The signal that survives is that both listings describe the
same class meeting: same instructor, same term, same day/time. Titles are NOT
used to group -- cross-listed titles genuinely differ ("... (at SEAS)",
"Advanced ...") -- they only set a confidence score.

This is a heuristic. Everything it finds is surfaced to the user as "detected --
verify", never as fact. Run it after each ingest:

    python -m ingest.crosslist
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DEFAULT_TERM
from ingest.db import connect

# Fully derived from `courses`, so it is cheaper to rebuild than to migrate.
TABLE = """
DROP TABLE IF EXISTS crosslists;
CREATE TABLE crosslists (
    group_id    TEXT NOT NULL,
    course_key  TEXT NOT NULL,
    term        TEXT NOT NULL,
    confidence  TEXT NOT NULL DEFAULT 'medium',
    PRIMARY KEY (group_id, course_key)
);
CREATE INDEX IF NOT EXISTS idx_crosslist_key ON crosslists(course_key);
"""

# GSD marks a cross-listed offering by appending the host school, e.g.
# "Computer Vision (at SEAS)". Strip that (and level prefixes) before comparing.
AT_SCHOOL_RE = re.compile(r"\s*\(at\s+[^)]+\)\s*$", re.I)
LEVEL_PREFIX_RE = re.compile(r"^(advanced|introduction to|intro to)\s+", re.I)


def normalize_title(t: str) -> str:
    t = AT_SCHOOL_RE.sub("", t or "")
    t = LEVEL_PREFIX_RE.sub("", t)
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_overlap(titles: list[str]) -> float:
    """Jaccard overlap of the most- and least-similar normalized titles."""
    sets = [set(normalize_title(t).split()) for t in titles if t]
    sets = [s for s in sets if s]
    if len(sets) < 2:
        return 0.0
    worst = 1.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if not union:
                continue
            worst = min(worst, len(sets[i] & sets[j]) / len(union))
    return worst


def detect(term: str = DEFAULT_TERM, verbose: bool = True) -> dict:
    conn = connect()
    conn.executescript(TABLE)

    rows = conn.execute(
        """SELECT c.key, c.code, c.subject, c.catalog, c.school, c.title, c.instructors,
                  m.day_mask, m.start_min, m.end_min
           FROM courses c
           LEFT JOIN meetings m ON m.course_key = c.key
           WHERE c.term = ?""",
        (term,),
    ).fetchall()

    # Fingerprint on instructor + meeting time, NOT title. Cross-listed titles
    # genuinely differ ("Innovation in Science and Engineering: Conference
    # Course" vs "... (at SEAS)" vs "Advanced ..."), but one instructor cannot
    # teach two different classes in the same room-hour -- so a shared
    # instructor and identical meeting time is a much stronger signal.
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        instructors = tuple(sorted(json.loads(r["instructors"] or "[]")))
        if not instructors:
            continue
        if r["day_mask"] is None:
            continue  # need a shared meeting time to be confident
        key = (instructors, r["day_mask"], r["start_min"], r["end_min"])
        groups[key].append(r)

    conn.execute("DELETE FROM crosslists WHERE term = ?", (term,))

    found = 0
    gsd_seas = 0
    examples = []
    for key, members in groups.items():
        codes = {m["code"] for m in members}
        schools = {m["school"] for m in members}
        # A real cross-listing has DIFFERENT course codes for the same class.
        if len(codes) < 2:
            continue
        # Title agreement decides confidence, not membership: a GSD "(at SEAS)"
        # listing still reads as the same course once the marker is stripped.
        overlap = title_overlap([m["title"] for m in members])
        confidence = "high" if overlap >= 0.5 else "medium" if overlap >= 0.2 else "low"

        group_id = f"{term}|" + "+".join(sorted(codes))
        for m in members:
            conn.execute(
                "INSERT OR IGNORE INTO crosslists (group_id, course_key, term, confidence) "
                "VALUES (?,?,?,?)",
                (group_id, m["key"], term, confidence),
            )
        found += 1
        if "GSD" in schools and any(s != "GSD" for s in schools):
            gsd_seas += 1
            if len(examples) < 8:
                examples.append((sorted(codes), sorted(schools), confidence,
                                 members[0]["title"][:40]))

    conn.commit()
    conn.close()

    if verbose:
        print(f"term={term!r}: {found} cross-listed group(s) detected, "
              f"{gsd_seas} spanning GSD and another school")
        for codes, schools, conf, title in examples:
            print(f"  {' / '.join(codes):34} {'+'.join(schools):10} {conf:7} {title}")

    return {"groups": found, "gsd_spanning": gsd_seas}


def load_map(conn, term: str, seas_subjects: set[str]) -> dict[str, dict]:
    """course_key -> {"schools": [...], "codes": [...], "partners": [...]}"""
    rows = conn.execute(
        """SELECT x.group_id, x.course_key, x.confidence, c.code, c.school, c.subject
           FROM crosslists x JOIN courses c ON c.key = x.course_key
           WHERE x.term = ?""",
        (term,),
    ).fetchall()

    by_group: dict[str, list] = defaultdict(list)
    for r in rows:
        by_group[r["group_id"]].append(r)

    out: dict[str, dict] = {}
    for members in by_group.values():
        for r in members:
            partners = [
                {"code": o["code"], "school": o["school"], "subject": o["subject"],
                 "confidence": o["confidence"],
                 "is_seas": (o["school"] or "").upper() == "FAS"
                            and (o["subject"] or "").upper() in seas_subjects}
                for o in members if o["course_key"] != r["course_key"]
            ]
            out[r["course_key"]] = {
                "confidence": r["confidence"],
                "codes": [o["code"] for o in members],
                "schools": list({o["school"] for o in members}),
                "partners": partners,
            }
    return out


if __name__ == "__main__":
    term = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TERM
    detect(term)
