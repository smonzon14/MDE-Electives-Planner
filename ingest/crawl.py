"""Crawl the public my.harvard course search into SQLite.

The search endpoint requires no authentication, so this runs server-side on a
schedule and serves every user. Nobody needs to be logged in to search.

Usage:
    python -m ingest.crawl                      # default term, all pages
    python -m ingest.crawl --term "2027 Spring"
    python -m ingest.crawl --max-pages 5        # smoke test
    python -m ingest.crawl --list-terms
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    CRAWL_SORT, DEFAULT_TERM, MAX_RETRIES, PAGE_SIZE, REQUEST_DELAY_SEC,
    REQUEST_TIMEOUT_SEC, SEARCH_URL, USER_AGENT,
)
from ingest.db import connect, finish_run, start_run, upsert_courses
from ingest.parse import parse_hits, parse_term_facets


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://my.harvard.edu/",
        "user-agent": USER_AGENT,
    })
    return s


def fetch_page(session: requests.Session, term: str, page: int, query: str = "") -> dict:
    params = {
        "q": query,
        "school": "All",
        "term": term,
        "sort": CRAWL_SORT,
        "page": page,
        "browseSchool": "false",
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(params=params, url=SEARCH_URL, timeout=REQUEST_TIMEOUT_SEC)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - retry any transport/parse failure
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SEC * 2 * attempt)
    raise RuntimeError(f"page {page} failed after {MAX_RETRIES} attempts: {last_err}")


def list_terms() -> list[tuple[str, int]]:
    session = make_session()
    data = fetch_page(session, term="All", page=1)
    return parse_term_facets(data["facets"])


def crawl(term: str = DEFAULT_TERM, max_pages: int | None = None,
          query: str = "", delay: float = REQUEST_DELAY_SEC) -> dict:
    session = make_session()
    conn = connect()

    first = fetch_page(session, term, 1, query)
    total_hits = first.get("total_hits", 0)
    total_pages = max(1, math.ceil(total_hits / PAGE_SIZE))
    if max_pages:
        total_pages = min(total_pages, max_pages)

    run_id = start_run(conn, term)
    print(f"term={term!r}  hits={total_hits}  pages={total_pages}")

    totals = {"new": 0, "changed": 0, "unchanged": 0}
    seen = 0
    status = "ok"

    try:
        for page in range(1, total_pages + 1):
            data = first if page == 1 else fetch_page(session, term, page, query)
            courses = parse_hits(data["hits"])
            if not courses:
                print(f"  page {page}: 0 cards -- stopping early")
                break
            stats = upsert_courses(conn, courses)
            for k in totals:
                totals[k] += stats[k]
            seen += len(courses)

            if page % 10 == 0 or page == total_pages:
                pct = 100 * page / total_pages
                print(f"  page {page}/{total_pages} ({pct:.0f}%)  "
                      f"courses={seen}  new={totals['new']}  changed={totals['changed']}")

            if page < total_pages:
                time.sleep(delay)
    except KeyboardInterrupt:
        status = "interrupted"
        print("\ninterrupted -- partial data committed")
    except Exception as e:  # noqa: BLE001
        status = f"error: {e}"
        print(f"\nfailed: {e}")
    finally:
        finish_run(conn, run_id, page, seen, status)

    stored = conn.execute(
        "SELECT COUNT(*) FROM courses WHERE term = ?", (term,)
    ).fetchone()[0]

    print(f"\ndone: {seen} cards fetched  new={totals['new']}  "
          f"changed={totals['changed']}  unchanged={totals['unchanged']}  status={status}")

    # Coverage only means anything for a complete pass; a --max-pages smoke test
    # compares a partial fetch against a whole-term stored count.
    full_pass = status == "ok" and page >= math.ceil(total_hits / PAGE_SIZE)
    if total_hits and full_pass:
        # `total_hits` assumes a full 15 cards on every page, but my.harvard
        # returns slightly fewer on many pages, and repeats some sections across
        # pages. Verified empirically: two back-to-back full crawls both yield
        # 7947 cards / 7629 unique sections for 2026 Fall, with new=0 on the
        # second pass -- so the crawl is complete and deterministic even though
        # `stored` sits below `total_hits`. Only a shortfall in *cards fetched*
        # indicates a real problem.
        print(f"coverage: {seen} cards fetched / {total_hits} claimed "
              f"({100 * seen / total_hits:.1f}%), {stored} unique sections stored "
              f"({seen - stored} duplicate cards collapsed)")
        if seen < total_hits * 0.95:
            print(f"NOTE: fetched notably fewer cards than claimed. "
                  f"Re-run to converge -- repeat passes are idempotent.")

    conn.close()

    # Cross-list detection is derived from the catalog, so it must be rebuilt
    # whenever the catalog changes -- otherwise the GSD/SEAS "either but not
    # both" rule would be evaluated against stale groupings.
    if full_pass:
        from ingest.crosslist import detect
        detect(term)
    return {"term": term, "cards": seen, "stored": stored,
            "total_hits": total_hits, "status": status, **totals}


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest my.harvard course catalog")
    ap.add_argument("--term", default=DEFAULT_TERM)
    ap.add_argument("--query", default="")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    ap.add_argument("--list-terms", action="store_true")
    args = ap.parse_args()

    if args.list_terms:
        for value, count in list_terms():
            print(f"{value:20} {count:>6}")
        return

    crawl(args.term, args.max_pages, args.query, args.delay)


if __name__ == "__main__":
    main()
