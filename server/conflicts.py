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


def find_slot_combinations(pools: list[dict[str, list[Block]]],
                           locked: Sequence[Block],
                           buffer_min: int = 0,
                           cap: int = 2000) -> tuple[list[tuple[str, ...]], bool]:
    """Pick one course per slot, where each slot has its own candidate pool.

    Differs from find_combinations in that the slots are not interchangeable:
    "a GSD project-based course AND a technical SEAS course" is two different
    pools, not one pool chosen from twice.

    Returns (results, truncated). Each result is one course key per slot, in
    slot order.

    Three things this has to get right:

    - **A course cannot fill two slots.** With overlapping filters the same
      course appears in several pools, and pairing it with itself is not a
      schedule.
    - **Unordered de-duplication.** When two slots have identical filters,
      (a, b) and (b, a) are the same set of courses and must be reported once.
      With identical filters either assignment is equally valid, so collapsing
      on the frozenset loses nothing.
    - **Round-robin fill, not depth-first.** Two unfiltered slots is ~2.3M
      pairs, far past any cap worth enumerating, so results are a bounded
      sample. Taking them depth-first would return page after page sharing the
      same first course. Instead each anchor yields its first completion, then
      its second, and so on, so the sample spreads across distinct first
      courses and early pages are actually comparable.

      This deliberately has no per-anchor cap. An earlier version capped at 5
      per anchor, which silently made `total` an artifact of the cap rather than
      a property of the filters: loosening a filter grew the candidate pool and
      left the reported total unchanged, which reads as a bug in the filters.
    """
    # Pre-filter: a course that does not fit the locked schedule on its own can
    # never appear in a valid combination, so drop it once rather than re-check
    # it inside the recursion.
    viable = [
        [k for k, blocks in pool.items() if is_free(blocks, locked, buffer_min)]
        for pool in pools
    ]
    if not pools or not all(viable):
        return [], False

    def completions(slot: int, chosen: list[str], chosen_blocks: list[Block]):
        """Lazily yield every valid way to fill `slot` onwards."""
        if slot == len(pools):
            yield tuple(chosen)
            return
        for key in viable[slot]:
            if key in chosen:
                continue
            blocks = pools[slot][key]
            if any(overlaps(b, cb, buffer_min) for b in blocks for cb in chosen_blocks):
                continue
            yield from completions(slot + 1, chosen + [key], chosen_blocks + list(blocks))

    # One lazy generator per first-slot course, advanced round-robin.
    gens = {a: completions(1, [a], list(pools[0][a])) for a in viable[0]}
    active = list(viable[0])
    results: list[tuple[str, ...]] = []
    seen: set[frozenset[str]] = set()

    while active and len(results) < cap:
        still_active = []
        for anchor in active:
            if len(results) >= cap:
                still_active.append(anchor)
                continue
            for combo in gens[anchor]:
                key_set = frozenset(combo)
                if key_set in seen:
                    continue          # same set reached via another anchor
                seen.add(key_set)
                results.append(combo)
                still_active.append(anchor)
                break                 # one per anchor per pass
        active = still_active

    # More remain only if some generator was left mid-stream.
    truncated = len(results) >= cap and bool(active)
    return results, truncated
