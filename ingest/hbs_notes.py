"""Tag HBS MBA sections with their cross-registrant auditor policy.

my.harvard does not carry HBS MBA course text. Every HBSM search card stores a
LINK to the HBS catalog as its description -- literally
"https://coursecatalog.mba.hbs.edu/?details&srcdb=792148&code=CATS%201120" --
so the auditor rule, which is what decides whether an MDE student can sit in on
an HBS course at all, is invisible to the rest of the pipeline.

That catalog is a FOSE app with a public JSON API. Its `details` route returns a
`class_notes` field holding a definition list that always includes a
"Cross-Registrant Auditors:" entry, and the entry is written PER SECTION -- two
sections of the same course can differ -- so this keys off (code, section) and
never off the course.

The term's `srcdb` is read out of the stored links rather than hardcoded, so a
newly posted term needs no change here. Terms with no HBS MBA sections yet (as
of 2026-08, 2027 Spring has only doctoral courses) are a no-op.

    python -m ingest.hbs_notes                     # default term
    python -m ingest.hbs_notes --term "2027 Spring"
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DEFAULT_TERM, MAX_RETRIES, REQUEST_DELAY_SEC, REQUEST_TIMEOUT_SEC, USER_AGENT
from ingest.db import connect, record_run

CATALOG_HOST = "coursecatalog.mba.hbs.edu"
API = f"https://{CATALOG_HOST}/api/?page=fose&route="

# The label HBS puts on the auditor entry in class_notes. Matched case-
# insensitively and tolerant of the singular, since it is hand-entered.
AUDITOR_LABEL_RE = re.compile(r"cross-?registrants?\s+auditors?\s*:?\s*$", re.I)

# "This section does not accept cross-registrant auditors." Also seen without
# the "cross-registrant" qualifier, hence the loose middle.
CLOSED_RE = re.compile(r"\b(?:does not|will not|do not)\b[^.]{0,60}\bauditor", re.I)
OPEN_RE = re.compile(r"\b(?:will be |is |are )?accept(?:ing|s)?\b[^.]{0,120}\bauditor", re.I)

# "accepting ALI Fellows as auditors" / "accepting Harvard Fellows and postdocs
# as auditors" -- open, but only to a named cohort an MDE student is not in.
# Anchored between "accepting" and "auditor" so the boilerplate tail ("Should
# you be admitted to this course as an auditor...") cannot trigger it.
COHORT_RE = re.compile(r"accept(?:ing|s)?\s+(?![Aa]uditor)(.{0,60}?)\bauditor", re.I)
COHORT_WORDS_RE = re.compile(r"\bfellows?\b|\bpostdocs?\b", re.I)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    })
    return s


def post(session: requests.Session, route: str, payload: dict) -> dict:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.post(API + route, data=json.dumps(payload),
                             timeout=REQUEST_TIMEOUT_SEC)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001 - retry any transport/parse failure
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(REQUEST_DELAY_SEC * 2 * attempt)
    raise RuntimeError(f"{route} failed after {MAX_RETRIES} attempts: {last_err}")


def strip_html(fragment: str) -> str:
    """class_notes is a small hand-authored HTML blob; flatten it to one line."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    # Dropping a tag leaves a space where there was none ("</a> ." -> " ."),
    # which reads as a typo once the note is shown as prose.
    return re.sub(r"\s+([.,;:])", r"\1", text)


def auditor_note(class_notes: str) -> str:
    """The body of the "Cross-Registrant Auditors:" entry, as plain text.

    HBS is inconsistent about the markup: some rows separate label from body
    with </b><br/>, others wrap the body in its own <p>. Splitting each <li> at
    its closing </b> handles both without caring which.
    """
    for li in re.findall(r"<li\b[^>]*>(.*?)</li>", class_notes or "", re.S | re.I):
        m = re.match(r"\s*<b\b[^>]*>(.*?)</b>(.*)", li, re.S | re.I)
        if m and AUDITOR_LABEL_RE.search(strip_html(m.group(1))):
            return strip_html(m.group(2))
    return ""


def classify(note: str) -> str | None:
    """'closed' | 'limited' | 'open', or None when the note says neither.

    "limited" exists because roughly a tenth of the open sections accept only
    ALI Fellows, Harvard Fellows or postdocs. Folding those into "open" would
    tell an MDE student they can audit a course that will turn them away, which
    is a worse error than saying nothing.
    """
    if not note:
        return None
    if CLOSED_RE.search(note):
        return "closed"
    if OPEN_RE.search(note):
        m = COHORT_RE.search(note)
        if m and COHORT_WORDS_RE.search(m.group(1)):
            return "limited"
        return "open"
    return None


def catalog_ref(url: str) -> tuple[str, str] | None:
    """('792148', 'CATS 1120') from a stored HBS catalog link."""
    if not url or CATALOG_HOST not in url:
        return None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    srcdb, code = (q.get("srcdb") or [""])[0], (q.get("code") or [""])[0]
    return (srcdb, code) if srcdb and code else None


def tag(term: str = DEFAULT_TERM, delay: float = REQUEST_DELAY_SEC) -> dict:
    conn = connect()
    rows = [
        r for r in conn.execute(
            "SELECT key, section, description FROM courses WHERE term = ?", (term,)
        ).fetchall()
        if catalog_ref(r["description"])
    ]
    if not rows:
        print(f"term={term!r}: no HBS MBA sections in the catalog -- nothing to tag")
        record_run(conn, "hbs_notes", term, "skipped",
                   detail="no HBS MBA sections in the catalog")
        conn.close()
        return {"sections": 0, "tagged": 0, "unmatched": 0, "unclassified": 0}

    srcdbs = {catalog_ref(r["description"])[0] for r in rows}
    session = make_session()

    # One search per srcdb gives every section's CRN, which the details route
    # needs. Cheaper and far more stable than scraping the catalog's HTML shell.
    crns: dict[tuple[str, str, str], str] = {}
    for srcdb in sorted(srcdbs):
        data = post(session, "search", {
            "other": {"srcdb": srcdb},
            "criteria": [{"field": "srcdb", "value": srcdb}],
        })
        for hit in data.get("results", []):
            crns[(srcdb, hit["code"], hit["no"])] = hit["crn"]
        print(f"srcdb={srcdb}: {len(data.get('results', []))} section(s) in the HBS catalog")

    counts = {"open": 0, "limited": 0, "closed": 0}
    tagged = unmatched = unclassified = 0

    for i, r in enumerate(rows, 1):
        srcdb, code = catalog_ref(r["description"])
        crn = crns.get((srcdb, code, r["section"]))
        if not crn:
            unmatched += 1
            continue
        detail = post(session, "details", {
            "group": f"code:{code}", "key": f"crn:{crn}",
            "srcdb": srcdb, "matched": f"crn:{crn}",
        })
        note = auditor_note(detail.get("class_notes", ""))
        policy = classify(note)
        if policy is None:
            unclassified += 1
        else:
            counts[policy] += 1
        conn.execute(
            "UPDATE courses SET auditors = ?, auditor_note = ? WHERE key = ?",
            (policy, note or None, r["key"]),
        )
        tagged += 1
        if i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}  tagged={tagged}  unmatched={unmatched}")
        time.sleep(delay)

    conn.commit()
    record_run(conn, "hbs_notes", term, "ok", courses=tagged,
               detail=f"{tagged} sections tagged; {counts['open']} open, "
                      f"{counts['limited']} fellows-only, {counts['closed']} closed")
    conn.close()
    print(f"done: {tagged}/{len(rows)} section(s) tagged  "
          f"open={counts['open']}  limited={counts['limited']}  closed={counts['closed']}  "
          f"no-note={unclassified}  unmatched={unmatched}")
    return {"sections": len(rows), "tagged": tagged, "unmatched": unmatched,
            "unclassified": unclassified, **counts}


def main() -> None:
    ap = argparse.ArgumentParser(description="Tag HBS MBA auditor policy")
    ap.add_argument("--term", default=DEFAULT_TERM)
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    args = ap.parse_args()
    tag(args.term, args.delay)


if __name__ == "__main__":
    main()
