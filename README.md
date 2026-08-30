# MDE Electives Planner

Conflict-aware course search for the Harvard MDE program. Finds electives that
fit around the classes you're already locked into — the thing my.harvard won't do.

## Why this works the way it does

The original assumption was that SSO was the main bottleneck. It isn't. Recon
turned up a more convenient split:

| | Course catalog | Your personal schedule |
|---|---|---|
| Endpoint | `my.harvard.edu/search/` | `my.harvard.edu/calendar/load/` |
| Auth | **None** — fully public | Your session cookie |
| Scope | Same for everyone | Per user |

So the catalog is crawled **server-side, once, for everybody** — no login, no
Cloudflare, no credentials. Only the personal-schedule layer needs
authentication, and that's handled by a Chrome extension that rides the session
you already have in your own browser. **No password, MFA code, or cookie is ever
sent to this server.**

### Where the data lives

There are no accounts, and the server stores nothing about anyone.

| | Lives on the server | Lives in your browser |
|---|---|---|
| Course catalog, policy engine, conflict math | ✅ read-only, same for everyone | |
| Your background, locked schedule, working plan | | ✅ `localStorage`, sent per request |

The catalog needs the server: eligibility and conflict detection run against all
7,600 sections, and that's too much to ship to a phone. Personal state doesn't —
so it isn't. That removes the login, the `user_key` that anyone could guess, and
any question about holding other students' enrollment records.

The trade is no cross-device sync, handled with an explicit **Save backup** /
**Load backup** file rather than an account. See [DEPLOY.md](DEPLOY.md) for the
deployment and for what phases 2 and 3 would add.

### What recon established

- `/search/` returns JSON whose `hits` field is rendered HTML (15 course cards per page).
- **Cards already contain term, session, meeting days, and meeting times.** So a
  full ingest is ~545 page fetches, not one fetch per course.
- Day encoding is `aria-label="Friday, selected"` on the day pill.
- **`sort=relevance` is not stable** — the same page number returns different
  results on repeat fetches, which silently drops courses across a multi-page
  crawl. The crawler uses `sort=subject_catalog`, which is deterministic. Don't
  change this without re-verifying.
- Fall 2026 reports 8,163 hits, which yields **7,947 cards → 7,629 unique sections**.
  That gap is expected, not data loss: `total_hits` assumes a full 15 cards per
  page, but many pages return fewer, and some sections are rendered twice.
  Verified by running two complete back-to-back crawls — the second returned
  `new=0, changed=0, unchanged=7947`, i.e. the crawl is complete and deterministic.
- Location is sign-in-gated; meeting times are not.
- There is **no `SEAS` school code** — SEAS courses are filed under `school=FAS`
  and identified by subject (`ENG-SCI`, `COMPSCI`, `APMTH`, …).

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-ingest.txt
```

`requirements.txt` is the *server* set only (FastAPI, Pydantic, PyYAML) — that is
what a deployment installs. The crawler's extra dependencies (BeautifulSoup,
requests, openpyxl) are in `requirements-ingest.txt` and are never on the request
path.

## 1. Ingest the catalog

```bash
./.venv/bin/python -m ingest.crawl                    # defaults to 2026 Fall
./.venv/bin/python -m ingest.crawl --list-terms       # see available terms
./.venv/bin/python -m ingest.crawl --max-pages 5      # quick smoke test
```

Takes roughly 8–10 minutes at the default 0.7s politeness delay. Re-running is
safe and idempotent: it upserts, and logs every meeting-time change into the
`changes` table. That's what powers the "recently moved" feed during shopping week.

### Adding a term

**Nothing gates which terms are searchable.** The app's term dropdown is built
from `SELECT term, COUNT(*) FROM courses GROUP BY term`, and every endpoint takes
a `term`, so a term becomes selectable the moment it has been ingested. To add
one, run the ingest steps against it:

```bash
./.venv/bin/python -m ingest.crawl     --term "2027 Spring"
./.venv/bin/python -m ingest.dates     --term "2027 Spring"
./.venv/bin/python -m ingest.hbs_notes --term "2027 Spring"
./.venv/bin/python -m ingest.mit_times --term "2027 Spring"
./.venv/bin/python -m ingest.crosslist "2027 Spring"
./.venv/bin/python -m ingest.seal --in-place        # before deploying
```

Cross-listing detection is per-term by nature — it groups courses that share an
instructor and a meeting time — so it has to run once per term, not once overall.

For a deployment, add the term to `TERMS` in
`.github/workflows/refresh-catalog.yml` instead, so the daily job keeps it fresh.
A term that is ingested once and never refreshed will quietly drift out of date.

> **Your program semester is not the catalog term.** The elective count and the
> core-course list come from `electives_by_term` / `cores_by_term`, keyed
> `{year}-{season}` off **your profile** — not off the term you're browsing. A
> Year 2 student with a Fall profile looking at Spring 2027 would be told they
> need 3 electives when the answer is 2. The app detects the disagreement and
> offers to fix it rather than guessing which you meant.

## 2. Run the app

```bash
./.venv/bin/uvicorn server.main:app --reload --port 8000
```

Open http://localhost:8000. To deploy it publicly, see **[DEPLOY.md](DEPLOY.md)**.

## 3. Import your enrolled classes

my.harvard's catalog is public, but your own schedule needs your session. Two
routes read it **in your browser** — no password, MFA code or cookie ever reaches
this server, and only class titles, codes and meeting times are sent.

**Copy and paste (no install).** Click **Import my classes**, open
`https://my.harvard.edu/calendar/load/` while signed in, and paste the JSON.
Works in every browser.

**One click (Chrome extension).**

1. `chrome://extensions` → enable **Developer mode**
2. **Load unpacked** → select the `extension/` folder
3. Sign in to my.harvard normally in that browser
4. On the planner, click **Import my classes** → **Import with the extension**

The extension deliberately does **not** parse the payload and does not talk to
any server. It fetches `/calendar/load/` with the user's own cookies and hands
the raw JSON to the page, which parses it with `public/calendar.js`. So both
routes run identical code, and a my.harvard format change is fixed by a redeploy
rather than by asking every user to update their extension.

The `/calendar/load/` shape is mapped exactly, against a real authenticated
payload:

```
{ events: [ { title, startTime:"14:15:00", endTime, daysOfWeek:[1,3,5],
              item: { start:"2026-09-02", end:"2026-12-03", courseUrl,
                      code:"STU 1231 ", session, classNumber, location } } ] }
```

Three details the parser depends on:

- `daysOfWeek` is FullCalendar's convention (**0 = Sunday**), which is exactly
  our bitmask order, so the mask is a direct shift.
- **Use `item.start` / `item.end`, never `startRecur` / `endRecur`.** The recur
  fields are display bounds — every event carries the same `2026-11-30` endRecur
  while the real term ends `2026-12-03`/`04`. Trusting them shortens every
  course by about a month and breaks partial-term conflict detection.
- `item.courseUrl` (`/course/STU1231/2026-Fall/001`) is the reliable join key
  back into the catalog; `item.code` arrives as `"STU 1231 "` with stray spaces.

`POST /api/schedule/resolve` joins each entry to its catalog row and hands the
enriched list back for the browser to store — that join is what lets the policy
engine evaluate your actual enrolled courses instead of anonymous time blocks. A
tree-walking heuristic remains as a fallback if my.harvard changes the shape
mid-term. Meetings for other terms are skipped, so importing a spring calendar
into a fall plan can't invent conflicts.

## 4. The MDE elective policy

`mde_policy.yaml` encodes the real *MDE Elective Policy* (rev. 2025-08-10): the
9-elective structure, the GSD and SEAS minimums, the FAS level guide, the caps
in rules 4–7, CS50, and the cross-listing rules.

### Eligibility is per student, not per course

The policy branches on things no course record knows, so the engine always
evaluates `(course, profile)`:

| Profile field | What it changes |
|---|---|
| `seas_background` + `seas_areas` | Rule 2a vs 2b — with a SEAS background, courses in your area must be graduate level and the approved 100-level allowance does not apply |
| `physical_design_background` | Rule 1a — without one, a GSD course must be project-based |
| `year` + `season` | How many electives you take (Y1: 2+2, Y2: 3+2) and whether CS50 is still owed |
| `cs50_status` | Whether CS50 is outstanding, waived, or done |
| `completed_codes` | Counts past electives toward the 9-course totals and the caps |

Set this in the app under **My background**. It is stored in your browser and
sent as the `profile` field of each request — there is no profile endpoint,
because the server has nowhere to put one.

### The three approved lists

The policy PDF says *"See the list of approved …"* three times without including
them. They come from two spreadsheets, parsed into `approved_lists.yaml`:

| Spreadsheet / sheet | List | Policy rule |
|---|---|---|
| `MDE Approved Project-Based Electives.xlsx` → "GSD Project-Based" | `project_based` (45) | 1a |
| `MDE Approved SEAS Courses.xlsx` → "0-1000 Level Electives" | `seas_0_100` (57) | 2b-i |
| both SEAS sheets, where *Technical course fulfillment* = Yes | `technical` (68) | 2a-ii / 2b-ii |

```bash
./.venv/bin/python -m ingest.approved            # regenerate from the .xlsx files
./.venv/bin/python -m ingest.approved --check --verbose   # parse only, list unmatched
curl -X POST http://localhost:8000/api/policy/reload      # pick up changes live
```

`approved_lists.yaml` is **generated** — edit the spreadsheets and re-run.

Three details worth knowing:

- **`technical` spans both sheets.** The 0-1000 sheet has its own *Technical
  course fulfillment* column, and 55 of its 57 rows are Yes — so most approved
  100-level courses satisfy the technical requirement too. Reading only the
  "Graduate Technical Courses" sheet would have found 12 instead of 68.
- **GSD entries are catalog numbers, not codes.** The spreadsheet gives `6317`,
  not `SCI6317`. The subject prefix is deliberately **not** reconstructed: two
  thirds of the list isn't offered in any single term, so a guessed prefix
  couldn't be verified. They match on `school=GSD` + catalog instead. (The
  mapping does hold where it's checkable — 2→VIS, 4→HIS, 5→SES, 6→SCI — but
  guessing on the rest would risk a wrong match on something that matters.)
- **Most entries aren't offered this term.** Of 45 GSD numbers, 15 are in Fall
  2026; of 68 technical codes, 19. The lists span all terms, so a low match rate
  is expected, not a parse failure. `--verbose` prints exactly which are absent.

Two shorthand forms in the source needed expanding: `"2227 / 2224"` (two courses
in one cell) and `"APCOMP 209 A and B"` (→ `APCOMP209A`, `APCOMP209B`).

Courses on a list carry a blue `project-based` or `technical` badge in search,
and the sidebar has a **Project-based only** / **Technical only** filter for each
(`project_based=true` / `technical=true` on `/api/search` and `/api/combinations`).
They compose with everything else, so "GSD electives + project-based + fits my
schedule" is one query.

**Sub-100 courses on an approved list count.** The SEAS sheet is titled
*"Approved 0-100-level SEAS Elective"* and genuinely contains sub-100 entries —
CS50, CS51, CS79, APMTH 10, ENG-SCI 51. Rule 3 exempts *"CS50 and any approved
exceptions"*, and being on this list **is** that approval, so they satisfy the
SEAS requirement for a student without a SEAS background (program director
approval still required, and the app says so). A sub-100 course that is *not* on
the list is still excluded by rule 3.

### Level bands, not raw numbers

FAS levels are read as **bands**, per the policy's level guide — `1710` is
numerically above 200 but sits in the 1000–1999 *"For Undergraduates and
Graduates"* band, so it is **not** a graduate-level elective. Comparing
`level >= 200` gets this wrong.

### Cross-listing

`ingest/crosslist.py` detects cross-listed courses by **instructor + meeting
time**, not by title — cross-listed titles genuinely differ (`"Visualization"`
vs `"Visualization (at SEAS)"`), but one instructor cannot teach two different
classes in the same hour. Title agreement sets a confidence score instead.

It reproduces the policy's own worked examples: `SCI6472 / COMPSCI1710`
(Visualization) and `SCI6272 / ENG-SCI239 / ENG-SCI139` (the named override that
counts only as SEAS).

Credit follows the code you enrol under: the GSD listing gives GSD credit, and a
GSD↔SEAS pair counts at either school but not both. The non-GSD/non-SEAS listing
of a cross-listed class cannot give GSD or SEAS credit.

**Every cross-listing is a heuristic** and is labelled "detected — verify" in the
UI. It runs automatically at the end of a full crawl, or on demand:

```bash
./.venv/bin/python -m ingest.crosslist "2026 Fall"
```

## 5. Meeting date ranges (partial terms)

Search cards give a session *label* but no dates. Most of the catalog is
"Full Term", but HSPH, HSDM, HGSE, HKS, HBSM and HMS run 7-week sessions,
modules and quarters — and their cards render an **empty** session. Those are
exactly the schools rule 6 lets you take up to four of.

Without real dates, `BETH712` (Sep 2 – Oct 23) and `BETH736` (Oct 26 – Dec 19)
look like a conflict when they can never overlap. So `ingest/dates.py` fetches
detail pages for the unknown-session set only (~320 requests), then applies the
learned Full Term range to the rest:

```bash
./.venv/bin/python -m ingest.dates
```

Result: 2,338 of 2,341 meetings dated. The learned Full Term range
(`2026-09-02 .. 2026-12-03`) independently matches the dates in a real
my.harvard calendar payload.

Conflict detection requires day **and** time **and** date overlap. An unknown
date range is treated as always-running, so the engine can over-report a
conflict but never hide one.

### Can you actually audit that HBS course?

my.harvard carries no course text for HBS MBA sections. Their `description` is
literally a link to the HBS catalog:

```
https://coursecatalog.mba.hbs.edu/?details&srcdb=792148&code=CATS%201120
```

So the rule that decides whether an MDE student can sit in on an HBS course at
all was invisible to the app. `ingest/hbs_notes.py` reads it from that catalog's
public JSON API (`?page=fose&route=details`), whose `class_notes` field always
carries a **Cross-Registrant Auditors** entry, and stores it on the course as
`auditors` + `auditor_note`:

| `auditors` | 2026 Fall | Pill |
|---|---:|---|
| `closed` | 61 | `no auditors` |
| `open` | 19 | `open to auditors` |
| `limited` | 10 | `auditors: fellows only` |

**`limited` is not a nicety.** Ten sections accept only ALI Fellows, Harvard
Fellows or postdocs, and an MDE student is none of those — folding them into
"open" would promise access that does not exist.

Two details the wording has to respect: the note is written **per section**, so
two sections of the same course can differ (HBSMBA 1130 §01 and §02 both say
Harvard Fellows, but nothing guarantees that), and "open" still means faculty
approval plus a course fee. The pill is therefore a pointer, not a verdict — the
info dot shows HBS's exact wording with its links live.

The term's `srcdb` is read out of the stored links rather than hardcoded, so a
newly posted term needs no code change. A term with no MBA sections yet is a
no-op.

```bash
./.venv/bin/python -m ingest.hbs_notes --term "2026 Fall"
```

## 6. MIT cross-registration, and custom blocks

### MIT courses come from MIT's own feed

my.harvard lists **1,987 MIT courses** under `school=NONH` (subject `MIT`) and
publishes a meeting time for **not one of them**. Scheduling around an MIT
cross-registration used to mean retyping its time by hand as a custom block.

MIT publishes the whole term as a single JSON document — Hydrant, the
student-built planner, serves `latest.json` with every class and its meeting
slots. So `ingest/mit_times.py` is **one HTTP request for the entire catalog**,
not one per course: seconds, against the ~10 minutes the my.harvard crawl takes.

```bash
./.venv/bin/python -m ingest.mit_times --term "2026 Fall"
```

**It can only ever add a time to a course my.harvard already lists.** The pass
iterates over the `NONH`/`MIT` rows in our catalog and looks each one up in the
feed; Hydrant carries ~2,277 classes, so the ~315 that Harvard does not list are
ignored. If Harvard doesn't list it, you can't register for it, so it has no
business appearing here.

| | 2026 Fall |
|---|---:|
| my.harvard MIT listings | 1,987 |
| matched in the MIT feed | 1,962 |
| **given real meeting times** | **1,232** |
| no single lecture pattern | 730 |
| not in the MIT feed | 25 |

**Slot encoding** (verified against Hydrant's own `lectureRawSections` text for
1,518 classes, zero mismatches):

```
slot = day * 34 + half_hours_since_06:00        day 0 = Monday
44 -> Tuesday 11:00      [44, 3] -> Tuesday 11:00-12:30
```

**Only unambiguous times are stored.** A third of MIT classes publish either no
lecture section or several *alternative* ones (6.1010 offers four lecture times
in one room). Writing every alternative into `meetings` would make the course
collide with everything and read as unschedulable, so a class is timed only when
its lecture sections agree on one pattern. The other 730 stay untimed — exactly
where they were before — so this can only add information, never distort it.

MIT also publishes half-term flags, so `1.010A` (Sep 9 – Oct 23) stops
conflicting with `1.010B` (Oct 26 – Dec 10) the same way `ingest/dates.py` fixes
that for Harvard. Meetings from this source are tagged `date_source='mit_feed'`.

### Rooms: MIT only, and not by choice

The same feed carries the room, so **1,227 of the 1,232 timed MIT courses** get
one (`5-234`, `E51-315`, `W41-1401` — MIT numbers its buildings). It costs
nothing: it arrives in the request already being made, and was simply being
discarded. It shows next to the meeting time on the course card and in the
calendar block's hover text.

**Harvard rooms are not obtainable, and no amount of scraping will change that.**
This was checked properly rather than assumed:

| Source | Result |
|---|---|
| Search card `<!-- Begin: Location -->` block | present but **empty on 90/90** sampled cards |
| Course detail page | says `Cambridge Campus` then **"Sign In to see location"** |
| Campus on the search card | **not there** — detail page only, ~7,600 extra requests |

my.harvard puts room-level location behind HarvardKey. Getting it would mean
authenticating as the student, and this app has no login and never sends
anything that identifies a student to the server — see the top of this file.
So `meetings.location` is NULL for every Harvard meeting, and the UI renders
those rows exactly as it did before.

The one legitimate route would be the browser extension, which already reads the
student's own my.harvard calendar with their own cookies, client-side. That
could show rooms for the classes they are *enrolled in* — never for the catalog
at large. Not built.

**A display fix came with this.** my.harvard's search-card badge splits a code
into subject + catalog, and MIT's dot-numbering defeats it: `MIT 1.000` is stored
as subject `MIT`, catalog `1`. Every NONH listing therefore rendered as "MIT 1"
or "MIT 6", which nobody noticed while they were all untimed and hidden behind
the TBA filter — and became glaring the moment 1,232 of them appeared in default
results. `codeLabel()` in `public/app.js` falls back to splitting `code` whenever
`subject + catalog !== code`. That condition holds for **0 of 1,987** NONH rows
and for **every** row of every other school, so nothing else changed.

> **MIT numbering is not a level.** `1.000` parses as level 1, which the FAS
> guide would read as a sub-100 course and rule 3 would discard. NONH is
> therefore excluded from the FAS level bands entirely. Rule 5 still requires
> graduate standing, so outside courses always report **"verify"** — you confirm
> the level yourself.

### Custom blocks

**+ Block** in the sidebar adds a commitment my.harvard doesn't know about —
club meetings, work, commute. It becomes a locked block and filters the catalog
like any enrolled course, and it accepts an optional date range so a half-term
commitment stops conflicting with courses in the other half.

Blocks used to have a second kind, "Outside course (MIT)", which linked a block
to an untimed NONH listing. Real MIT times made it redundant and it is gone,
along with the `/api/courses/untimed` endpoint that fed its picker. Blocks
already saved with a course link keep working.

## Is each source actually fresh?

"Catalog updated 10 minutes ago" in the header refers to the **my.harvard crawl
only**. That became misleading once two independent, third-party-dependent
passes were added, because the refresh deliberately lets an enrichment pass fail
*without* failing the run — a feed being down must not discard a finished
ten-minute crawl. So a fresh Harvard crawl could sit on top of an MIT feed that
had not updated in days, and nothing would say so.

Every pass now records itself in `ingest_runs` (`source`, `detail`), and the
info dot beside that line breaks it down:

```
SOURCES LAST REFRESHED
Harvard catalog (my.harvard)                    4 days ago
MIT times & rooms                              just now
HBS auditor policy                          3 minutes ago
```

Each row shows when the data was last **actually refreshed**, not when we last
tried. If the most recent attempt failed, the row turns red and says so while
still reporting the last good timestamp — hover for the error and the pass's own
summary (`1232 timed, 1227 with a room; MIT feed updated 2026-08-30 21:01`,
which carries MIT's own publication time, not just ours).

`skipped` is not a failure and never sets the status: MIT publishes only its
current term, so every other term legitimately skips, and letting that count
made a healthy feed read as skipped purely because Spring ran last.

## Links back to my.harvard

Every course code, title, week-grid block, combination row and sidebar entry
links to its my.harvard page, opening in a new tab:

```
https://my.harvard.edu/course/COMPSCI2360R/2026-Fall/001
```

These are not constructed by string-building — `detail_url` is scraped from the
anchor my.harvard itself renders on each search card, so the link is whatever
my.harvard says it is. All 7,629 Fall 2026 courses have one, including the
NONH/MIT listings.

Hand-entered obligations have no linked course, so they render as plain text
rather than a dead link. An MIT block linked to its NONH listing does get one.

## Refreshing during shopping week

```bash
./.venv/bin/python -m ingest.crawl --term "2026 Fall"
```

Then re-seal before deploying, or the read-only server will refuse to start:

```bash
./.venv/bin/python -m ingest.seal --in-place
```

For a deployment you don't refresh by hand at all —
`.github/workflows/refresh-catalog.yml` crawls, backfills dates, detects
cross-listings, regenerates the approved lists, seals the database, verifies it
opens read-only, and commits. The commit is what triggers the deploy. Run it
manually from the Actions tab with `max_pages: 5` for a smoke test.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Readiness probe; proves the catalog artifact is readable |
| `GET /api/meta` | Terms, schools, requirement buckets, last ingest |
| `POST /api/search` | Catalog search, annotated with conflicts **and** policy eligibility |
| `POST /api/combinations` | Every valid set of N electives that fits |
| `POST /api/plan` | Hydrates the plan and runs the full policy check, in one call |
| `POST /api/schedule/resolve` | Joins imported calendar entries to catalog rows |
| `POST /api/schedule/block` | Validates and shapes a custom block (obligation or outside course) |
| `POST /api/course/{key}` | One course, evaluated against a profile |
| `POST /api/free` | Open windows per day |
| `GET /api/changes` | Course times that moved since last ingest |
| `POST /api/policy/reload` | Re-read the policy and approved lists (local dev) |

**Every endpoint is stateless.** The ones that depend on the student take
`{profile, locked, plan}` in the request body — the browser is the system of
record, and the server keeps nothing. That is why the personal endpoints are
POST: there is no `user_key` query parameter any more, because there is no
identity at all.

Search body fields: `term`, `q`, `school`, `subject`, `requirement`, `free_only`,
`project_based`, `technical`, `include_tba`, `buffer_min`,
`limit`, `offset`.

### Locked schedule vs. plan

Two different signals, deliberately treated differently:

- **Locked** — enrolled courses and custom blocks. Hard commitments. A collision
  is a red `clashes:` badge, and "Only courses that fit my schedule" hides them.
- **Plan** — courses you're weighing up. A collision is an amber
  `overlaps plan:` badge and the course **stays in the results**, because the
  whole point of a plan is comparing alternatives that compete for the same slot.
  Hiding them would hide exactly the trade-off you're trying to see.

A course never flags itself, and enrolled courses are counted only once (they
live in the locked set, not the plan set).

Adding or removing a plan course re-fetches the visible results, so every other
row's badge updates on the click — no refresh. Re-rendering from the cached
result set would leave stale badges, since plan membership changes the
annotation on *other* courses, not just the one you clicked. The refresh keeps
your scroll depth: if you'd hit "Load more" three times, all of it comes back.

### What the `requirement` filter means

Only **rules 1 (GSD) and 2 (SEAS) are requirements.** Rules 4–7 are
`kind: maximum` — ceilings on how many of a category may count toward the nine.
A course that only hits a cap satisfies nothing; it just consumes headroom. So
offering "FAS courses" or "Other Harvard schools" as *requirement* filters said
something untrue about them, and they are no longer offered. Use the school
filter instead.

| Value | Means |
|---|---|
| `""` | **All courses** — no filtering. Includes courses that count toward nothing. |
| `minimums` | Satisfies rule 1 or rule 2. (`any` is accepted as the old spelling.) |
| `gsd` / `seas` | That specific minimum. |

The option list is built from `kind == "minimum"`, so a new rule added to
`mde_policy.yaml` lands in the right group without touching the client.

There is no longer an `include_no_credit` flag: with `""` meaning *all*, courses
that count toward nothing simply appear, badged distinctly, and with any
specific filter they are excluded by definition. The badges are what carry the
distinction:

| Badge | Meaning |
|---|---|
| *(no fit badge)* | No overlap with anything — the absence of a warning is the signal |
| `overlaps: X` (amber) | Collides with a course you're only *considering* — flagged, never hidden |
| `clashes: X` (red) | Collides with an enrolled course or a custom block |
| *(green/amber requirement name)* | Satisfies a **minimum** -- rule 1 (GSD) or rule 2 (SEAS). Amber `?` = needs verification |
| *(grey requirement name · max N)* | Counts toward the 9 and uses up a **cap** (rules 4-7), but satisfies no requirement |
| `counts toward nothing` | Legal to take, but satisfies no elective requirement |
| `doesn't count` | Excluded outright by rule 3 (below the 100 level) |
| `core — not an elective` | A required MDE course; never counts toward the 9 |

For a Year 1 Fall student with **no** SEAS background, Fall 2026 gives 1,531
timed sections satisfying some requirement, and 2,341 once the no-credit ones
are included. With a SEAS background in COMPSCI it drops to 1,516: rule 2a holds
courses in your own area to graduate level, which excludes 15 of them.

`buffer_min` is travel time — ten minutes between Allston and Cambridge is not
really ten minutes. The UI defaults to 15.

## How conflict detection works

Meetings are stored as `(day_mask, start_min, end_min)`, where `day_mask` is a
7-bit field with Sunday at bit 0. Two meetings conflict when they share a day
(one bitwise AND) and their time intervals overlap. That's fast enough to filter
all 7,600 sections on every keystroke.

Courses with no fixed meeting time (TBA / async) never conflict with anything,
and are excluded from combination search so they don't flood the results.

## Known limitations

- **Nothing here replaces your advisor.** Rule 8: all exceptions are petitioned
  through the program manager. Cross-listings and independent-study detection are
  heuristics, and E-PSCI counts as SEAS only if the instructor is SEAS faculty —
  which the catalog does not record, so it is always flagged for you to verify.
- **No cross-device sync.** Your profile, schedule and plan live in *this*
  browser's local storage. Switching laptops or clearing site data loses them
  unless you use **Save backup** / **Load backup**. That is the deliberate
  phase-1 trade for having no accounts; see [DEPLOY.md](DEPLOY.md).
- **Three courses have no parseable date range** (of 2,341). They are treated as
  always-running, so they can over-report a conflict but never hide one.
- **MIT/NONH courses have no meeting times at all** in my.harvard (1,987 of them).
  Add them as custom blocks until an MIT feed exists. Their graduate standing
  cannot be inferred from the course number, so rule 5 is always "verify".
- **Enrollment is deliberately out of scope.** This tool tells you what fits;
  you enroll in my.harvard yourself.

## Project layout

```
config.py               shared settings (term default, crawl sort, delays)
mde_policy.yaml         encoded MDE elective policy
approved_lists.yaml     the three approved lists (generated)
ingest/parse.py         HTML card -> structured course
ingest/crawl.py         paginated catalog crawler
ingest/crosslist.py     cross-listing detection
ingest/dates.py         meeting date-range backfill (partial terms)
ingest/hbs_notes.py     HBS MBA cross-registrant auditor policy
ingest/mit_times.py     MIT meeting times from MIT's own feed
ingest/approved.py      parses the two .xlsx lists -> approved_lists.yaml
ingest/db.py            SQLite schema, upsert, change log, read-only open
ingest/seal.py          makes a deploy-ready read-only DB artifact
server/conflicts.py     bitmask conflict engine
server/policy.py        policy engine (course x student profile)
server/main.py          FastAPI app — stateless
api/index.py            Vercel entrypoint (re-exports the ASGI app)
public/                 single-page UI (no build step)
public/store.js         browser-local persistence (profile, schedule, plan)
public/calendar.js      the my.harvard calendar parser (used by both routes)
extension/              Chrome MV3 calendar fetcher (fetch + relay only)
.github/workflows/      daily catalog rebuild -> commit -> deploy
```

The Python package is `server/`, not `api/`, because Vercel turns every file
under `api/` into its own serverless function — `policy.py` and `conflicts.py`
would each be deployed separately. `api/index.py` is the only file there.
