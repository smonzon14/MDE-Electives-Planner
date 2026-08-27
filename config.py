"""Shared configuration."""
import os
from pathlib import Path

ROOT = Path(__file__).parent

# Overridable so a deployment can point at a sealed artifact somewhere else
# (see ingest/seal.py). Defaults to the working copy the crawler writes.
DB_PATH = Path(os.environ.get("MDE_DB_PATH") or (ROOT / "data" / "courses.db"))

# my.harvard endpoints (public search needs no auth)
SEARCH_URL = "https://my.harvard.edu/search/"
CALENDAR_URL = "https://my.harvard.edu/calendar/load/"
BASE = "https://my.harvard.edu"

# Default term. Facet values are literally "2026 Fall" (space, not hyphen).
DEFAULT_TERM = "2026 Fall"

# Cards per search page, fixed by my.harvard.
PAGE_SIZE = 15

# Pagination sort. MUST stay deterministic: sort=relevance returns different
# results for the same page number on repeat fetches, which makes a multi-page
# crawl silently drop and duplicate courses. sort=subject_catalog is stable.
CRAWL_SORT = "subject_catalog"

# Be a good citizen: this is our own school's server.
REQUEST_DELAY_SEC = 0.7
REQUEST_TIMEOUT_SEC = 45
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# Day bit positions: Sunday=0 .. Saturday=6
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
DAY_ABBR = ["Su", "M", "T", "W", "Th", "F", "S"]
