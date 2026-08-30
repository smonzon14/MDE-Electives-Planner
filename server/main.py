"""FastAPI backend for MDE Electives Planner -- stateless.

The catalog is public and identical for every student, so it is crawled once,
server-side, and served read-only to everybody. Nothing else lives here.

**The server stores no personal data.** A student's profile, locked schedule and
working plan are held by their browser (see public/store.js) and travel with
each request. That is a deliberate design choice, not a shortcut:

  - There are no accounts to build, and no `user_key` to guess. The previous
    design namespaced schedules behind a short plaintext string, which meant
    anyone could read anyone else's by typing their name.
  - Enrollment data is student-education-record-shaped. Holding a cohort's
    schedules would mean a retention policy, a breach surface and a
    conversation with the program office. Holding none means none of that.
  - Serverless hosts give each invocation a fresh, read-only filesystem, so
    server-side SQLite writes would silently vanish anyway.

The cost is no cross-device sync -- addressed with an explicit export/import
file rather than an account. See DEPLOY.md.

Conflict math and policy evaluation stay on the server: they need the full
7,600-section catalog, and both are fast enough to run per keystroke.

Run locally:  uvicorn server.main:app --reload --port 8000
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DAY_NAMES, DB_PATH, DEFAULT_TERM, ROOT
from server.conflicts import (Block, conflicts_with, find_slot_combinations,
                              free_windows)
from server.policy import Policy, StudentProfile
from ingest.crosslist import load_map
from ingest.db import connect_readonly

app = FastAPI(title="MDE Electives Planner", version="1.0.0")

# The app is served from the same origin as the API, and the browser extension
# talks to the page (not to us) via a content script, so cross-origin access is
# not needed by anything. Kept only for a split local setup.
_origins = [o for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)

POLICY = Policy()
_XL_CACHE: dict[str, dict] = {}
_RW_FALLBACK_WARNED = False


def db() -> sqlite3.Connection:
    """Open the catalog read-only.

    A freshly crawled database is left in WAL mode, which cannot be opened with
    `mode=ro` at all -- reading a WAL database still requires writing to its
    sidecar. In a deployment that is a hard error and should stay one: the
    shipped artifact must be sealed. Locally it is just friction after a crawl,
    so fall back to a normal read-write handle and say so once.
    """
    global _RW_FALLBACK_WARNED
    try:
        return connect_readonly()
    except sqlite3.OperationalError:
        if os.environ.get("VERCEL") or os.environ.get("MDE_STRICT_READONLY"):
            raise HTTPException(
                503,
                "Catalog is not readable read-only (likely left in WAL mode). "
                "Seal it before deploying: python -m ingest.seal --in-place",
            )
        from ingest.db import connect

        if not _RW_FALLBACK_WARNED:
            print("[mde] catalog not openable read-only (WAL?); using a "
                  "read-write handle for local dev. Run `python -m ingest.seal "
                  "--in-place` to match production.", file=sys.stderr)
            _RW_FALLBACK_WARNED = True
        return connect()


def crosslists(conn, term: str) -> dict:
    if term not in _XL_CACHE:
        _XL_CACHE[term] = load_map(conn, term, set(POLICY.seas_subjects))
    return _XL_CACHE[term]


# ------------------------------------------------------------ request parts ---
#
# Every personal-state endpoint takes the same three pieces, because the client
# is the system of record for all of them.

class ProfileIn(BaseModel):
    """The student's background, as their browser stores it."""
    year: int = Field(1, ge=1, le=2)
    season: str = "Fall"
    seas_background: bool = False
    seas_areas: list[str] = Field(default_factory=list, max_length=40)
    physical_design_background: bool = False
    cs50_status: str = "required"
    completed_codes: list[str] = Field(default_factory=list, max_length=60)

    def to_profile(self) -> StudentProfile:
        return StudentProfile(
            year=self.year,
            season=self.season if self.season in ("Fall", "Spring") else "Fall",
            seas_background=self.seas_background,
            seas_areas=list(self.seas_areas),
            physical_design_background=self.physical_design_background,
            cs50_status=self.cs50_status,
            completed_codes=list(self.completed_codes),
        )


class LockedIn(BaseModel):
    """One hard commitment: an enrolled class, or a hand-added block.

    `course_key` links the block to a catalog row where one is known, which is
    what lets the policy engine evaluate an actual enrolled course rather than
    an anonymous rectangle of time.
    """
    title: str = ""
    code: str = ""
    section: str = ""
    day_mask: int = Field(0, ge=0, le=127)
    start_min: int = Field(..., ge=0, le=24 * 60)
    end_min: int = Field(..., ge=0, le=24 * 60)
    start_date: str | None = None
    end_date: str | None = None
    source: str = "manual"          # harvard | manual
    category: str = "obligation"    # obligation | course
    course_key: str = ""

    def to_block(self) -> Block:
        return Block(self.day_mask, self.start_min, self.end_min,
                     self.title or self.code or "Locked",
                     self.start_date, self.end_date)


MAX_LOCKED = 60
MAX_PLAN = 40


class PersonalIn(BaseModel):
    profile: ProfileIn = Field(default_factory=ProfileIn)
    locked: list[LockedIn] = Field(default_factory=list, max_length=MAX_LOCKED)
    plan: list[str] = Field(default_factory=list, max_length=MAX_PLAN)


class SearchIn(PersonalIn):
    term: str = DEFAULT_TERM
    q: str = Field("", max_length=200)
    school: str = ""
    subject: str = ""
    # "" means no filter at all -- the whole catalog, including courses that
    # count toward nothing. "minimums" means the course satisfies rule 1 or
    # rule 2. Anything else is a specific requirement id.
    #
    # Cap requirements (kind: maximum) are deliberately not offered: a course
    # that only hits a cap satisfies nothing, so "filter by requirement" would
    # be a lie. Use the school filter for those.
    requirement: str = ""      # "" | minimums | gsd | seas
    free_only: bool = False
    # Approved-list filters (rule 1a / rule 2). Independent of `requirement`:
    # a course can be on a list yet still fail its school's level gate.
    project_based: bool = False
    technical: bool = False
    buffer_min: int = Field(0, ge=0, le=120)
    include_tba: bool = True
    limit: int = Field(200, ge=1, le=500)
    offset: int = Field(0, ge=0)


# ---------------------------------------------------------------- catalog ---

# Punctuation the catalog and humans disagree about. The catalog stores
# "ENG-SCI51"; the UI renders it "ENG-SCI 51"; the program office's spreadsheets
# write "ENGSCI51". Someone typing any of those means the same course.
_CODE_PUNCT = " -._/"


def _norm_code_sql(column: str) -> str:
    """SQL that strips code punctuation from `column`, for comparison."""
    expr = f"UPPER({column})"
    for ch in _CODE_PUNCT:
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


def _course_rows(conn, term: str, filters: dict) -> list[dict]:
    sql = ["SELECT * FROM courses WHERE term = ?"]
    params: list = [term]
    if filters.get("q"):
        # Match the code with punctuation ignored on both sides, so searching
        # for the string the UI displays actually finds the course.
        q = filters["q"]
        like = f"%{q}%"
        stripped = "".join(ch for ch in q.upper() if ch not in _CODE_PUNCT)
        clause = "title LIKE ? OR code LIKE ? OR description LIKE ? OR instructors LIKE ?"
        params += [like, like, like, like]
        # Only when something survives stripping: a query of pure punctuation
        # would otherwise collapse to '%%' and match the entire catalog.
        if stripped:
            clause += f" OR {_norm_code_sql('code')} LIKE ?"
            params.append(f"%{stripped}%")
        sql.append(f"AND ({clause})")
    if filters.get("school"):
        sql.append("AND school = ?")
        params.append(filters["school"])
    if filters.get("subject"):
        sql.append("AND subject = ?")
        params.append(filters["subject"])
    rows = conn.execute(" ".join(sql), params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["instructors"] = json.loads(d.get("instructors") or "[]")
        out.append(d)
    return out


def _attach_meetings(conn, courses: list[dict]) -> None:
    if not courses:
        return
    keys = [c["key"] for c in courses]
    by_key: dict[str, list[dict]] = {k: [] for k in keys}
    CHUNK = 500
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i : i + CHUNK]
        q = f"SELECT * FROM meetings WHERE course_key IN ({','.join('?' * len(chunk))})"
        for m in conn.execute(q, chunk).fetchall():
            by_key[m["course_key"]].append({
                "day_mask": m["day_mask"],
                "start_min": m["start_min"],
                "end_min": m["end_min"],
                "raw_time": m["raw_time"],
                "start_date": m["start_date"],
                "end_date": m["end_date"],
                "date_source": m["date_source"],
                "location": m["location"],
                "days": [DAY_NAMES[i] for i in range(7) if m["day_mask"] & (1 << i)],
            })
    for c in courses:
        c["meetings"] = by_key.get(c["key"], [])


def _requirement_ok(requirement: str, satisfied: list[str]) -> bool:
    """Does this course pass the requirement filter?

    "" is no filter (the whole catalog). "minimums" is rule 1 or rule 2 --
    the only requirements a student must actually satisfy. Anything else is a
    specific id. "any" is accepted as the old spelling of "minimums".
    """
    if not requirement or requirement == "all":
        return True
    if requirement in ("minimums", "any"):
        # satisfied_ids() is a list, not a set.
        return any(r in POLICY.minimum_requirement_ids for r in satisfied)
    return requirement in satisfied


def _blocks(meetings: list[dict], label: str = "") -> list[Block]:
    return [Block(m["day_mask"], m["start_min"], m["end_min"], label,
                  m.get("start_date"), m.get("end_date")) for m in meetings]


def _plan_courses(conn, term: str, keys: list[str]) -> list[dict]:
    """Hydrate plan course keys into catalog rows, with meetings attached."""
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return []
    marks = ",".join("?" * len(keys))
    rows = conn.execute(
        f"SELECT * FROM courses WHERE key IN ({marks}) AND term = ?", [*keys, term]
    ).fetchall()
    courses = []
    for r in rows:
        d = dict(r)
        d["instructors"] = json.loads(d.get("instructors") or "[]")
        courses.append(d)
    _attach_meetings(conn, courses)
    return courses


@app.get("/api/health")
def health():
    """Cheap readiness probe that proves the catalog artifact is readable."""
    try:
        conn = db()
        n = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"catalog unavailable: {type(e).__name__}")
    # Where the catalog came from. The build downloads it from a release asset
    # (scripts/vercel-build.sh); "repo-fallback" means that download failed and
    # a committed copy was used instead, which would otherwise look healthy
    # while serving a stale catalog.
    marker = ROOT / "data" / "CATALOG_SOURCE"
    db_file = Path(str(DB_PATH))
    return {"ok": True, "courses": n,
            "policy_version": POLICY.as_dict()["policy_version"],
            "catalog_source": marker.read_text().strip() if marker.exists() else "unknown",
            "db_bytes": db_file.stat().st_size if db_file.exists() else None}


@app.get("/api/meta")
def meta():
    conn = db()
    terms = [dict(r) for r in conn.execute(
        "SELECT term, COUNT(*) n FROM courses GROUP BY term ORDER BY term DESC"
    ).fetchall()]
    schools = [r[0] for r in conn.execute(
        "SELECT DISTINCT school FROM courses WHERE school != '' ORDER BY school"
    ).fetchall()]
    last = conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
    n_xl = conn.execute("SELECT COUNT(DISTINCT group_id) FROM crosslists").fetchone()[0] \
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='crosslists'").fetchone() else 0
    conn.close()
    return {
        "default_term": DEFAULT_TERM,
        "terms": terms,
        "schools": schools,
        "last_ingest": dict(last) if last else None,
        "crosslist_groups": n_xl,
        "policy": POLICY.as_dict(),
    }


@app.post("/api/search")
def search(inp: SearchIn):
    conn = db()
    profile = inp.profile.to_profile()
    xl = crosslists(conn, inp.term)
    courses = _course_rows(conn, inp.term,
                           {"q": inp.q, "school": inp.school, "subject": inp.subject})
    _attach_meetings(conn, courses)
    # Paired with course_key so a course can be excluded from its own conflict
    # check; "" never matches a real catalog key.
    locked = [(l.course_key, l.to_block()) for l in inp.locked]
    locked_keys = {l.course_key for l in inp.locked if l.course_key}

    # Courses the student is *considering* are a softer signal than their locked
    # schedule: colliding with one is worth flagging but must never hide the
    # course, since the whole point is comparing alternatives against each other.
    # Enrolled courses are excluded here -- they already live in `locked`.
    plan_blocks: list[tuple[str, Block]] = []
    for pc in _plan_courses(conn, inp.term, inp.plan):
        if pc["key"] in locked_keys:
            continue
        label = f"{pc['subject']} {pc['catalog']}".strip() or pc["code"]
        for b in _blocks(pc["meetings"], label):
            plan_blocks.append((pc["key"], b))

    results = []
    # Courses hidden ONLY because they have no published meeting time. Counted
    # so the UI can say so: a whole school can be untimed early in a cycle (GSD
    # had 153 Spring 2027 sections and no times), and silently showing zero
    # results invites the conclusion that nothing is offered.
    hidden_tba = 0
    for c in courses:
        el = POLICY.evaluate(c, profile, xl.get(c["key"]))
        if inp.project_based and not el.is_project_based:
            continue
        if inp.technical and not el.is_technical:
            continue
        if not _requirement_ok(inp.requirement, el.satisfied_ids()):
            continue

        if not c["meetings"] and not inp.include_tba:
            hidden_tba += 1
            continue

        blocks = _blocks(c["meetings"], c["title"])
        # Exclude the course's own schedule entry, or an enrolled course would
        # report itself as a clash -- and with "only courses that fit" on (the
        # UI default) would then hide itself from its own search results.
        own = [b for k, b in locked if k != c["key"]]
        hits = conflicts_with(blocks, own, inp.buffer_min) if own else []
        c["conflicts"] = [h.label for h in hits]
        c["fits"] = not hits
        c["enrolled"] = c["key"] in locked_keys
        c["policy"] = el.to_dict()

        # Annotate-only: never used to filter.
        others = [b for k, b in plan_blocks if k != c["key"]]
        phits = conflicts_with(blocks, others, inp.buffer_min) if others else []
        seen_labels: list[str] = []
        for h in phits:
            if h.label not in seen_labels:
                seen_labels.append(h.label)
        c["plan_conflicts"] = seen_labels
        c["in_plan"] = any(k == c["key"] for k, _ in plan_blocks)

        if inp.free_only and hits:
            continue
        results.append(c)

    conn.close()
    results.sort(key=lambda c: (c["subject"] or "", c["catalog"] or "", c["section"] or ""))
    return {"total": len(results), "offset": inp.offset, "limit": inp.limit,
            "profile": profile.to_dict(),
            "electives_this_term": POLICY.electives_this_term(profile),
            "hidden_tba": hidden_tba,
            "results": results[inp.offset : inp.offset + inp.limit]}


class CourseIn(PersonalIn):
    term: str = DEFAULT_TERM


@app.post("/api/course/{key}")
def course_detail(key: str, inp: CourseIn):
    conn = db()
    row = conn.execute("SELECT * FROM courses WHERE key = ?", (key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "course not found")
    c = dict(row)
    c["instructors"] = json.loads(c.get("instructors") or "[]")
    _attach_meetings(conn, [c])
    xl = crosslists(conn, c["term"])
    c["policy"] = POLICY.evaluate(c, inp.profile.to_profile(), xl.get(key)).to_dict()
    c["crosslist"] = xl.get(key)
    c["changes"] = [dict(r) for r in conn.execute(
        "SELECT * FROM changes WHERE course_key = ? ORDER BY id DESC LIMIT 20", (key,)
    ).fetchall()]
    conn.close()
    return c


@app.get("/api/changes")
def recent_changes(term: str = DEFAULT_TERM, limit: int = Query(100, ge=1, le=500)):
    conn = db()
    rows = conn.execute(
        """SELECT ch.*, c.code, c.section, c.title
           FROM changes ch JOIN courses c ON c.key = ch.course_key
           WHERE c.term = ? AND ch.kind = 'time_changed'
           ORDER BY ch.id DESC LIMIT ?""",
        (term, limit),
    ).fetchall()
    conn.close()
    return {"changes": [dict(r) for r in rows]}


# --------------------------------------------------------------- extension ---

EXTENSION_DIR = ROOT / "extension"
# Never shipped to the user: notes for whoever edits the manifest.
_EXT_SKIP = {"MANIFEST-NOTES.md"}
_EXT_CACHE: dict[str, bytes] = {}
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def _request_origin(request: Request) -> str | None:
    """The public origin this request arrived on, behind Vercel's proxy."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host or not _HOST_RE.match(host):
        return None
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto not in ("http", "https"):
        return None
    # Host is client-controllable, so only ever widen `matches` to the origin
    # actually being served. Someone spoofing Host could produce a zip trusting
    # their own domain -- but they could equally write their own extension, so
    # this grants nothing they don't already have.
    return f"{proto}://{host}"


def _build_extension_zip(origin: str | None) -> bytes:
    """Zip extension/, pointing content_scripts at the serving origin.

    The match list is a security boundary: any page the content script runs on
    can ask the extension for the user's class schedule. Rewriting it here means
    the copy someone downloads trusts exactly the site they downloaded it from,
    so moving the deployment to a new domain needs no edit and no re-release --
    and no one is ever tempted to widen it to https://*.vercel.app/*.
    """
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    local = ["http://localhost:8000/*", "http://127.0.0.1:8000/*"]
    matches = local + ([f"{origin}/*"] if origin else [])
    if origin is None:
        matches = manifest["content_scripts"][0]["matches"]
    manifest["content_scripts"][0]["matches"] = list(dict.fromkeys(matches))

    buf = io.BytesIO()
    # Deterministic: fixed timestamps so the same source yields the same bytes,
    # which keeps ETags stable across redeploys.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(EXTENSION_DIR.iterdir()):
            if not path.is_file() or path.name in _EXT_SKIP:
                continue
            info = zipfile.ZipInfo(f"mde-electives-planner/{path.name}",
                                   date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            data = (json.dumps(manifest, indent=2) + "\n").encode() \
                if path.name == "manifest.json" else path.read_bytes()
            z.writestr(info, data)
    return buf.getvalue()


@app.api_route("/api/extension.zip", methods=["GET", "HEAD"])
def extension_zip(request: Request):
    """The browser extension, packaged for Load unpacked.

    Built on demand rather than committed, so it can never drift from the source
    in extension/ and so the manifest can be pointed at this deployment.
    """
    if not EXTENSION_DIR.is_dir():
        raise HTTPException(404, "extension source is not bundled in this deployment")
    origin = _request_origin(request)
    key = origin or "-"
    if key not in _EXT_CACHE:
        _EXT_CACHE[key] = _build_extension_zip(origin)
    blob = _EXT_CACHE[key]
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "content-disposition": 'attachment; filename="mde-electives-planner-extension.zip"',
            "content-length": str(len(blob)),
        },
    )


# ---------------------------------------------------------------- schedule ---

class ImportItem(BaseModel):
    """One meeting as the calendar parser produces it (extension or paste)."""
    title: str = ""
    code: str = ""
    section: str = ""
    term: str = ""
    day_mask: int = Field(0, ge=0, le=127)
    start_min: int = Field(..., ge=0, le=24 * 60)
    end_min: int = Field(..., ge=0, le=24 * 60)
    raw_time: str = ""
    # my.harvard's calendar gives real date ranges; the catalog often doesn't.
    start_date: str | None = None
    end_date: str | None = None
    session: str = ""
    class_number: str = ""
    location: str = ""
    instructor: str = ""


class ResolveIn(BaseModel):
    term: str = DEFAULT_TERM
    items: list[ImportItem] = Field(default_factory=list, max_length=MAX_LOCKED)


@app.post("/api/schedule/resolve")
def resolve_schedule(inp: ResolveIn):
    """Join imported calendar entries to catalog rows and hand them back.

    This is the one thing the import genuinely needs a server for: matching
    `code`/`section` against the catalog to recover `course_key`. Without it the
    client would hold anonymous time blocks; with it, each enrolled class points
    at a real catalog row, so the policy engine can evaluate the student's
    actual courses and count them toward the nine.

    Nothing is stored. The enriched list is returned for the browser to keep.
    """
    conn = db()
    out = []
    for it in inp.items:
        if it.end_min <= it.start_min or not it.day_mask:
            continue
        course_key, detail_url, subject, catalog, school = None, None, "", "", ""
        if it.code:
            row = conn.execute(
                "SELECT * FROM courses WHERE code=? AND term=? AND section=? LIMIT 1",
                (it.code, it.term or inp.term, it.section),
            ).fetchone() or conn.execute(
                "SELECT * FROM courses WHERE code=? AND term=? LIMIT 1",
                (it.code, it.term or inp.term),
            ).fetchone()
            if row:
                course_key = row["key"]
                detail_url = row["detail_url"]
                subject, catalog, school = row["subject"], row["catalog"], row["school"]
        out.append({
            **it.model_dump(),
            "term": it.term or inp.term,
            "source": "harvard",
            "category": "course",
            "course_key": course_key or "",
            "detail_url": detail_url,
            "subject": subject,
            "catalog": catalog,
            "school": school,
            "days": [DAY_NAMES[i] for i in range(7) if it.day_mask & (1 << i)],
        })
    conn.close()
    matched = sum(1 for o in out if o["course_key"])
    return {"items": out, "count": len(out), "matched": matched,
            "unmatched": [o["code"] or o["title"] for o in out if not o["course_key"]]}


class BlockIn(BaseModel):
    """A commitment my.harvard doesn't know about.

    Two uses: real-life obligations (club meetings, work, commute), and
    cross-registration courses -- my.harvard lists ~2,000 MIT courses under
    school NONH with no meeting times at all, so the only way to schedule
    around one is to enter its time by hand. Linking `course_key` to the NONH
    listing makes the policy engine count it toward the rule-5 cap.
    """
    term: str = DEFAULT_TERM
    title: str = Field(..., min_length=1, max_length=200)
    day_mask: int = Field(0, ge=0, le=127)
    days: list[int] = Field(default_factory=list, max_length=7)
    start_min: int = Field(..., ge=0, le=24 * 60)
    end_min: int = Field(..., ge=0, le=24 * 60)
    start_date: str | None = None
    end_date: str | None = None
    category: str = "obligation"   # 'obligation' | 'course'
    course_key: str = ""
    notes: str = Field("", max_length=500)


@app.post("/api/schedule/block")
def make_block(b: BlockIn):
    """Validate a hand-entered block and resolve its optional catalog link.

    Returns the block for the browser to store. Kept server-side so the two
    entry points (this and the calendar import) produce identically shaped
    records, and so the NONH course link is verified against the real catalog.
    """
    mask = b.day_mask
    for d in b.days:
        if 0 <= d <= 6:
            mask |= 1 << d
    if not mask:
        raise HTTPException(400, "pick at least one day")
    if b.end_min <= b.start_min:
        raise HTTPException(400, "end time must be after start time")
    if b.start_date and b.end_date and b.end_date < b.start_date:
        raise HTTPException(400, "last date is before the first date")

    conn = db()
    code = section = subject = catalog = school = ""
    detail_url = None
    if b.course_key:
        row = conn.execute(
            "SELECT * FROM courses WHERE key = ?", (b.course_key,)
        ).fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, "linked course not found")
        code, section = row["code"], row["section"]
        subject, catalog, school = row["subject"], row["catalog"], row["school"]
        detail_url = row["detail_url"]
    conn.close()

    return {"item": {
        "title": b.title, "code": code, "section": section, "term": b.term,
        "day_mask": mask, "start_min": b.start_min, "end_min": b.end_min,
        "raw_time": "", "start_date": b.start_date, "end_date": b.end_date,
        "source": "manual", "category": b.category,
        "course_key": b.course_key or "", "detail_url": detail_url,
        "subject": subject, "catalog": catalog, "school": school,
        "notes": b.notes,
        "days": [DAY_NAMES[i] for i in range(7) if mask & (1 << i)],
    }}


class FreeIn(BaseModel):
    locked: list[LockedIn] = Field(default_factory=list, max_length=MAX_LOCKED)


@app.post("/api/free")
def free(inp: FreeIn):
    locked = [l.to_block() for l in inp.locked]
    return {"windows": {
        DAY_NAMES[i]: [{"start_min": s, "end_min": e} for s, e in free_windows(locked, i)]
        for i in range(7)
    }}


# -------------------------------------------------------------------- plan ---

class PlanIn(PersonalIn):
    term: str = DEFAULT_TERM
    include_completed: bool = True
    outside_harvard_count: int = Field(0, ge=0, le=20)
    buffer_min: int = Field(0, ge=0, le=120)


@app.post("/api/plan")
def plan_view(inp: PlanIn):
    """Hydrate the plan and run the full policy check, in one round trip.

    Returns the plan's course cards (same shape search results use, so the UI
    renders them through the same component) plus the requirement report.
    """
    conn = db()
    profile = inp.profile.to_profile()
    xl = crosslists(conn, inp.term)

    courses = _plan_courses(conn, inp.term, inp.plan)
    seen = {c["key"] for c in courses}

    # Show what the student is already enrolled in alongside what they're
    # considering -- the policy check covers both, so the list should too.
    enrolled_keys = [l.course_key for l in inp.locked
                     if l.course_key and l.course_key not in seen]
    for c in _plan_courses(conn, inp.term, enrolled_keys):
        c["in_plan"] = 0
        c["enrolled"] = True
        courses.append(c)
        seen.add(c["key"])

    for c in courses:
        c.setdefault("in_plan", 1)
        c["policy"] = POLICY.evaluate(c, profile, xl.get(c["key"])).to_dict()
        # Keep the course_key alongside each locked block so an enrolled course
        # is not reported as clashing with its own schedule entry.
        locked = [l.to_block() for l in inp.locked if l.course_key != c["key"]]
        hits = conflicts_with(_blocks(c["meetings"], c["title"]), locked, inp.buffer_min)
        c["conflicts"] = [h.label for h in hits]
        c["fits"] = not hits
        c["plan_conflicts"] = []

    # Completed courses are matched by code across all terms so past electives
    # count toward the 9-course totals and the caps. They are part of the
    # report, never of the on-screen plan.
    report_courses = list(courses)
    if inp.include_completed and profile.completed_codes:
        codes = [c for c in profile.completed_codes if c][:60]
        if codes:
            marks = ",".join("?" * len(codes))
            for r in conn.execute(
                f"SELECT * FROM courses WHERE code IN ({marks}) GROUP BY code", codes
            ).fetchall():
                if r["key"] not in seen:
                    report_courses.append(dict(r))
                    seen.add(r["key"])

    # Hand-added outside courses (MIT blocks linked to a NONH listing) count
    # toward the rule-5 cap even though they are not in the plan.
    outside = inp.outside_harvard_count or sum(
        1 for l in inp.locked
        if l.source == "manual" and l.category == "course" and l.course_key
    )
    report = POLICY.validate_plan(report_courses, profile, xl, outside)
    conn.close()

    return {"items": courses, "report": report,
            "electives_this_term": POLICY.electives_this_term(profile)}


# ---------------------------------------------------------- combinations ---

class SlotIn(BaseModel):
    """Filters for one elective slot.

    Slots are independent: "a project-based GSD course AND a technical SEAS
    course" is two different candidate pools, not one pool chosen from twice.
    """
    q: str = Field("", max_length=200)
    school: str = ""
    requirement: str = ""      # "" | minimums | gsd | seas
    project_based: bool = False
    technical: bool = False
    label: str = ""


MAX_SLOTS = 4


class CombosIn(PersonalIn):
    term: str = DEFAULT_TERM
    slots: list[SlotIn] = Field(default_factory=list, max_length=MAX_SLOTS)
    buffer_min: int = Field(0, ge=0, le=120)
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)
    # Ceiling on enumeration, not on what is returned. Two unfiltered slots is
    # ~2.3M pairs, so results are always a bounded, anchor-spread sample.
    cap: int = Field(2000, ge=50, le=5000)

    # --- legacy flat form, kept so an old client keeps working ---
    pick: int = Field(0, ge=0, le=MAX_SLOTS)
    q: str = Field("", max_length=200)
    school: str = ""
    requirement: str = ""
    project_based: bool = False
    technical: bool = False

    def resolved_slots(self, default_pick: int) -> list[SlotIn]:
        if self.slots:
            return list(self.slots)
        n = self.pick or default_pick
        flat = SlotIn(q=self.q, school=self.school, requirement=self.requirement,
                      project_based=self.project_based, technical=self.technical)
        return [flat.model_copy() for _ in range(max(1, n))]


def _slot_pool(conn, term: str, slot: SlotIn, profile: StudentProfile,
               xl: dict) -> tuple[dict[str, list[Block]], dict[str, dict]]:
    """Candidate courses for one slot, as {key: blocks} plus their metadata."""
    courses = _course_rows(conn, term, {"q": slot.q, "school": slot.school})
    _attach_meetings(conn, courses)
    pool: dict[str, list[Block]] = {}
    meta: dict[str, dict] = {}
    for c in courses:
        if not c["meetings"]:
            continue  # TBA fits everything and would flood the results
        el = POLICY.evaluate(c, profile, xl.get(c["key"]))
        if slot.project_based and not el.is_project_based:
            continue
        if slot.technical and not el.is_technical:
            continue
        if not _requirement_ok(slot.requirement, el.satisfied_ids()):
            continue
        pool[c["key"]] = _blocks(c["meetings"], c["title"])
        c["policy"] = el.to_dict()
        meta[c["key"]] = c
    return pool, meta


@app.post("/api/combinations")
def combinations(inp: CombosIn):
    """Every set of electives that fits, one course drawn from each slot."""
    conn = db()
    profile = inp.profile.to_profile()
    xl = crosslists(conn, inp.term)
    slots = inp.resolved_slots(POLICY.electives_this_term(profile))
    locked = [l.to_block() for l in inp.locked]

    pools: list[dict[str, list[Block]]] = []
    meta_by_key: dict[str, dict] = {}
    for slot in slots:
        pool, meta = _slot_pool(conn, inp.term, slot, profile, xl)
        pools.append(pool)
        meta_by_key.update(meta)
    conn.close()

    combos, truncated = find_slot_combinations(
        pools, locked, buffer_min=inp.buffer_min, cap=inp.cap)

    page = combos[inp.offset : inp.offset + inp.limit]

    def card(key: str) -> dict:
        c = meta_by_key[key]
        return {"key": key, "code": c["code"], "section": c["section"],
                "title": c["title"], "school": c["school"],
                "subject": c["subject"], "catalog": c["catalog"],
                "meetings": c["meetings"], "detail_url": c["detail_url"],
                "policy": c["policy"]}

    return {
        "total": len(combos),
        "truncated": truncated,
        "offset": inp.offset,
        "limit": inp.limit,
        "slots": [{"label": s.label, "pool_size": len(p)}
                  for s, p in zip(slots, pools)],
        "profile": profile.to_dict(),
        "electives_this_term": POLICY.electives_this_term(profile),
        "combinations": [[card(k) for k in combo] for combo in page],
    }


@app.post("/api/policy/reload")
def reload_policy():
    """Re-read mde_policy.yaml and approved_lists.yaml.

    Useful while editing the policy locally. On a serverless host each container
    already loads them fresh at cold start, and containers you cannot reach
    won't see this call -- redeploy instead.
    """
    POLICY.reload()
    _XL_CACHE.clear()
    return POLICY.as_dict()


# ------------------------------------------------------------------ static ---
#
# In production Vercel serves public/ straight from its CDN and only /api/*
# reaches this function (see vercel.json). This mount is what makes `uvicorn
# server.main:app` behave the same way locally. It is registered last so the
# catch-all never shadows an API route.

PUBLIC_DIR = ROOT / "public"


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """Force revalidation of the front-end files.

    There is no build step, so nothing is content-hashed. A browser holding a
    cached index.html while fetching a fresh app.js (or the reverse) wires the
    JS up against a DOM that no longer matches, and the page dies on load with
    a null element -- which looks like a code bug and is not one. Vercel gets
    the same rule from vercel.json; this keeps local dev honest too.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


if PUBLIC_DIR.exists():
    @app.get("/")
    def index():
        return FileResponse(PUBLIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
