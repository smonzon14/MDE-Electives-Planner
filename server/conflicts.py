"""Schedule conflict detection.

Meetings are stored as (day_mask, start_min, end_min). Two meetings conflict when
they share a day AND their time intervals overlap. The day bitmask makes the
day test a single AND, so filtering 8k sections against a locked schedule is
fast enough to run on every keystroke.

A `buffer_min` allows for travel time -- ten minutes between Allston and
Cambridge is not really ten minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DAY_NAMES


@dataclass(frozen=True)
class Block:
    """A single meeting occurrence.

    `start_date` / `end_date` are ISO dates bounding the weeks the meeting
    actually runs. They matter because the professional schools run partial
    terms: a course meeting Sep 2 - Oct 23 and one meeting Oct 26 - Dec 19 can
    share a day and hour and still never collide. `None` means "unknown", which
    is treated as always-running so we never hide a real conflict.
    """
    day_mask: int
    start_min: int
    end_min: int
    label: str = ""
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def days(self) -> list[str]:
        return [DAY_NAMES[i] for i in range(7) if self.day_mask & (1 << i)]


def dates_overlap(a: Block, b: Block) -> bool:
    """Do the two blocks' calendar ranges intersect? Unknown ranges count as yes."""
    if a.start_date is None or a.end_date is None:
        return True
    if b.start_date is None or b.end_date is None:
        return True
    return a.start_date <= b.end_date and b.start_date <= a.end_date


def overlaps(a: Block, b: Block, buffer_min: int = 0) -> bool:
    """True if two blocks collide, padding `b` by buffer_min on both sides."""
    if not (a.day_mask & b.day_mask):
        return False
    if not (a.start_min < b.end_min + buffer_min and b.start_min - buffer_min < a.end_min):
        return False
    return dates_overlap(a, b)


def conflicts_with(candidate: Sequence[Block], locked: Sequence[Block],
                   buffer_min: int = 0) -> list[Block]:
    """Return the locked blocks that the candidate collides with (empty = free)."""
    hits = []
    for c in candidate:
        for l in locked:
            if overlaps(c, l, buffer_min) and l not in hits:
                hits.append(l)
    return hits


def is_free(candidate: Sequence[Block], locked: Sequence[Block],
            buffer_min: int = 0) -> bool:
    """A course with no scheduled meetings (TBA / async) never conflicts."""
    if not candidate:
        return True
    return not conflicts_with(candidate, locked, buffer_min)


def internally_consistent(blocks: Sequence[Block], buffer_min: int = 0) -> bool:
    """Check a proposed set of courses doesn't collide with itself."""
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            if overlaps(blocks[i], blocks[j], buffer_min):
                return False
    return True


def find_combinations(candidates: dict[str, list[Block]], locked: Sequence[Block],
                      pick: int = 2, buffer_min: int = 0,
                      limit: int = 200,
                      max_per_anchor: int | None = None) -> list[tuple[str, ...]]:
    """Enumerate valid sets of `pick` courses that fit around `locked`.

    This is the MDE case directly: two cores are locked, choose two electives.
    Candidates are pre-filtered against `locked` first, so the combinatorial
    step only ever runs over courses that individually already fit.

    `max_per_anchor` caps how many results may share the same first course.
    Without it, plain depth-first order returns a page of options that all begin
    with the same course, which is useless for actually comparing schedules.
    """
    viable = [k for k, blocks in candidates.items()
              if is_free(blocks, locked, buffer_min)]

    if max_per_anchor is None:
        # Aim for roughly 8 distinct anchors across the returned page.
        max_per_anchor = max(1, limit // 8)

    results: list[tuple[str, ...]] = []
    per_anchor: dict[str, int] = {}

    def recurse(start: int, chosen: list[str], chosen_blocks: list[Block]) -> bool:
        """Returns True when the global limit is reached."""
        if len(chosen) == pick:
            anchor = chosen[0]
            if per_anchor.get(anchor, 0) >= max_per_anchor:
                return False
            per_anchor[anchor] = per_anchor.get(anchor, 0) + 1
            results.append(tuple(chosen))
            return len(results) >= limit

        for i in range(start, len(viable)):
            key = viable[i]
            if chosen and per_anchor.get(chosen[0], 0) >= max_per_anchor:
                return False
            blocks = candidates[key]
            if any(overlaps(b, cb, buffer_min) for b in blocks for cb in chosen_blocks):
                continue
            if recurse(i + 1, chosen + [key], chosen_blocks + list(blocks)):
                return True
        return False

    recurse(0, [], [])
    return results


def free_windows(locked: Sequence[Block], day_index: int,
                 day_start: int = 8 * 60, day_end: int = 22 * 60) -> list[tuple[int, int]]:
    """Open time ranges on a given day, for the "when am I actually free" view."""
    bit = 1 << day_index
    busy = sorted(
        (b.start_min, b.end_min) for b in locked if b.day_mask & bit
    )
    windows = []
    cursor = day_start
    for start, end in busy:
        if start > cursor:
            windows.append((cursor, min(start, day_end)))
        cursor = max(cursor, end)
    if cursor < day_end:
        windows.append((cursor, day_end))
    return [(s, e) for s, e in windows if e > s]
