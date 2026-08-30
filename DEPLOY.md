# Deploying — phase 1

Public, account-free, and free to run. Personal data never leaves the browser.

## The shape of it

```
        GitHub Actions (daily)                    Vercel
   ┌──────────────────────────────┐        ┌────────────────────┐
   │ crawl my.harvard  (~9 min)   │        │ public/   → CDN    │
   │ backfill dates               │        │ api/index.py → fn  │
   │ detect cross-listings        │ push   │   read-only SQLite │
   │ seal DB read-only            ├───────►│                    │
   │ commit data/courses.db       │ deploy └─────────┬──────────┘
   └──────────────────────────────┘                  │ profile + locked + plan
                                                     │ sent per request
                                            ┌────────┴──────────┐
                                            │ the student's     │
                                            │ browser owns all  │
                                            │ personal state    │
                                            └───────────────────┘
```

Three properties follow from this, and they're the point:

- **No accounts.** Nothing identifies a student to the server, so there is no
  `user_key` to guess, no session to steal, and no login to build.
- **No personal data at rest.** Enrollment schedules are education-record-shaped.
  Holding a cohort's worth would mean a retention policy, a breach surface, and
  a conversation with the program office. Holding none means none of that.
- **The catalog is a build artifact.** An 8–10 minute crawl can't run in a
  serverless function, so CI builds the database and commits it. The site always
  serves a catalog produced by a reproducible job.

The cost is no cross-device sync. **Save backup** / **Load backup** in the
sidebar is the deliberate substitute.

## One-time setup

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "MDE Electives Planner"
gh repo create mde-electives-planner --private --source=. --push
```

`data/courses.db` is **not** in git. See *The catalog artifact* below.

### 2. Seal the database before the first deploy

```bash
./.venv/bin/python -m ingest.seal --in-place
```

**This is not optional.** A freshly crawled database is left in WAL mode, and a
WAL database cannot be opened read-only *at all* — reading one still requires
writing to its `-wal` sidecar, which fails on Vercel's read-only filesystem.
Sealing checkpoints the WAL, switches to `journal_mode=DELETE`, drops any
leftover personal tables, and verifies the result opens read-only.

CI runs this on every refresh, so you only do it by hand for the first push.

### 3. Import to Vercel

Import the repo at [vercel.com/new](https://vercel.com/new). No environment
variables are required. `vercel.json` already routes `/api/*` to the Python
function and serves `public/` from the CDN.

Check the deployment:

```bash
curl https://YOUR-APP.vercel.app/api/health
# {"ok":true,"courses":7629,"policy_version":"2025-08-10"}
```

If this returns 503 with a message about WAL mode, step 2 was skipped.

### 4. Point the extension at your domain

`extension/manifest.json` ships with a placeholder:

```json
"matches": [
  "http://localhost:8000/*",
  "http://127.0.0.1:8000/*",
  "https://mde-electives-planner.vercel.app/*"
]
```

Replace the last entry with your real URL.

> **This list is a security boundary, not a convenience.** Any page the content
> script runs on can ask the extension for the user's class schedule. Never
> widen it to `https://*.vercel.app/*` — that would let *any* site hosted on
> Vercel read a user's timetable.

Then either load it unpacked (`chrome://extensions` → Developer mode → Load
unpacked) or publish it. Publishing costs a one-time $5 developer registration
and takes a few days; a listing with host permissions on `my.harvard.edu` will
get read carefully, so explain in the listing that it fetches one endpoint with
the user's own session and posts the result to the page.

**You can skip the extension entirely.** The copy-paste route needs no install,
works in Safari and Firefox, and is the default the import dialog offers.

## The catalog artifact

The catalog is ~11 MB of SQLite that the refresh job rewrites in full. It is
published as a **rolling GitHub release asset** on the `catalog` tag — replaced
every run, no history — and fetched at build time by `scripts/vercel-build.sh`.

Committing it instead added ~11 MB of permanent history per changed day (three
times a day on the current schedule), which git cannot prune without rewriting
history. And it buys nothing: the catalog is derived data, reproducible from a
crawl, so only the *current* copy is idempotent with my.harvard.

**Git LFS is deliberately not used.** It looks like the obvious answer and is
the wrong tool three times over:

| | |
|---|---|
| Doesn't fix the growth | LFS still stores every version, billed against a 1 GB allowance |
| Costs bandwidth | Separate 1 GB/month transfer quota, consumed by every checkout |
| **Breaks the build** | Vercel does not fetch LFS objects — it would deploy the ~130-byte pointer file, and SQLite would report the catalog as corrupt |

### The one secret this needs

| Where | Name | Value |
|---|---|---|
| GitHub → Settings → Secrets → Actions | `VERCEL_DEPLOY_HOOK` | A Vercel Deploy Hook URL (Project → Settings → Git → Deploy Hooks). Needed because a refresh no longer pushes to git, so nothing would otherwise trigger a deploy. |

**No `CATALOG_TOKEN` any more.** The repository is public, so the release asset
is fetched from its unauthenticated redirect URL
(`github.com/<repo>/releases/download/catalog/courses.db`). That removed a
fine-grained PAT, a GitHub API round trip, and any exposure to the
60-requests/hour unauthenticated API limit that Vercel's shared build IPs would
have had to share. **If the repo is ever made private again, the build breaks** —
restore the token and the API-based lookup together.

The build still **fails deliberately** if the catalog can't be fetched. There is
no fallback: the catalog is not in git, so the alternative would be deploying an
app whose every request 503s. Vercel keeps the previous deployment live when a
build fails, so a failed fetch means "stops updating", not "goes down". Without
`VERCEL_DEPLOY_HOOK` the job warns and the new catalog simply waits for the next
deploy.

`GET /api/health` reports `catalog_source`, which is the thing to check after a
deploy:

| Value | Meaning |
|---|---|
| `release-asset` | Correct — freshly downloaded |
| `repo-fallback` | Only possible on a checkout that still has a committed catalog. Should never appear in production. |
| `unknown` | The build script did not run |

### Working locally

A fresh clone has no catalog, so the app will 503 until you fetch one:

```bash
./scripts/fetch-catalog.sh          # uses your existing gh auth
# or build it yourself:
./.venv/bin/python -m ingest.crawl && ./.venv/bin/python -m ingest.seal --in-place
```

## The daily catalog refresh

`.github/workflows/refresh-catalog.yml` runs at 10:00 UTC (06:00 Eastern during
EDT) and on demand from the Actions tab. It crawls, backfills dates, detects
cross-listings, regenerates the approved lists from the two spreadsheets, seals
the DB, verifies it the way production opens it, and commits only if something
changed. The commit triggers the Vercel deploy.

It crawls every term listed in the workflow's `TERMS` env var (currently
`2026 Fall,2027 Spring`), one full pass each. **Adding a term there is the only
change needed to make it searchable** — the UI's dropdown is built from whatever
the database contains. Budget ~10 minutes per term; the job's timeout is 90.

For a smoke test without a full crawl, run it manually with `max_pages: 5`. The
`terms` input overrides `TERMS` for a single run, so you can refresh one term
without re-crawling the others.

The job needs `contents: write`, which is already declared. If pushes are
rejected, enable **Read and write permissions** under Settings → Actions →
General → Workflow permissions.

## Running locally

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-ingest.txt    # server + crawler
./.venv/bin/uvicorn server.main:app --reload --port 8000
```

`requirements.txt` is the *server* set only (FastAPI, Pydantic, PyYAML) — that's
what Vercel installs. BeautifulSoup, requests and openpyxl are crawler-only and
live in `requirements-ingest.txt`; keeping them off the runtime path is why cold
starts are small, and there's an assertion guarding it in the CI verify step.

To reproduce production's read-only behaviour exactly:

```bash
MDE_STRICT_READONLY=1 ./.venv/bin/uvicorn server.main:app --port 8000
```

Without that flag, the server falls back to a read-write handle after a crawl
(and says so once on stderr) so you aren't forced to re-seal during development.

## Things that will bite you

**`vercel.json` has to stay schema-pure.** Vercel validates it strictly and
rejects the whole deploy on any surprise, so two things are deliberate:

- `functions."api/index.py".includeFiles` **must be a single glob string.** An
  array is only valid in the legacy `builds` config; passing one here fails with
  `Invalid request: functions.api/index.py.includeFiles should be string`. It is
  `"**"` so it doesn't depend on brace-expansion support either — the bundle is
  narrowed by `.vercelignore` instead, which is what drops `.venv/`, the two
  spreadsheets, the extension and the logs. Result: 26 files, ~7 MB.
- **No `"//"` pseudo-comment keys.** JSON has no comments, and these objects
  reject unknown properties. Rationale goes here instead.
- **`headers[].source` is path-to-regexp, not a raw regex.** `"/(.*)"` is fine,
  but something like `"/(.*\.(html|js|css)|)"` is rejected with
  `Header at index N has invalid source pattern`. Rather than hand-craft a
  narrower pattern, `Cache-Control` is applied on the single `/(.*)` block
  alongside the security headers. It therefore also lands on `/api/*`
  responses, which is harmless — those are dynamic and must not be cached
  anyway.

**Do not add a `rewrites` rule for `/api/*`.** `api/index.py` is already the
default handler: Vercel serves `public/` from the CDN when a path matches a
static file, and passes everything else to the function *with its original
path*, which is exactly what FastAPI's router needs. An explicit
`{"source": "/api/(.*)", "destination": "/api/index.py"}` rewrites the path the
function receives, so FastAPI is handed `/api/index.py`, matches no route, and
returns its own `{"detail":"Not Found"}` for every endpoint. The tell is a JSON
404 rather than Vercel's HTML 404 page — the function is fine, the path is not.

**Re-seal after every local crawl** before deploying, or the function will 503.

**Don't add a build step without content-hashing the assets.** There isn't one
today, so `index.html`, `app.js`, `store.js`, `calendar.js` and `styles.css` are
served with `must-revalidate` (see `vercel.json`, and the matching middleware in
`server/main.py` for local dev). This is load-bearing: a browser holding a
cached `index.html` while fetching a fresh `app.js` wires the JS against a DOM
that no longer matches, and the page dies on load with a null element — which
looks exactly like a code bug and is not one.

**Personal state is per-browser and per-origin.** A student who switches laptops
or clears site data loses their plan unless they saved a backup. The app says so
in the sidebar; say it again when you tell people about the tool.

**Request size caps.** `server/main.py` limits imports to 60 locked blocks and
40 plan courses. A student with a genuinely larger schedule will get a 422.

## What phase 1 deliberately leaves out

| | Why it's out | What it would take |
|---|---|---|
| Cross-device sync | Needs identity | Phase 2: magic-link auth restricted to `*.harvard.edu`, plus a real database (Neon/Turso) |
| Harvard SSO | HarvardKey SP registration needs a sponsoring department, a security review and a stable domain — months, and not self-serve | Phase 3, and realistically only if the MDE program adopts the tool officially |
| Enrolled-course API | SSO proves *identity*; it does not grant access to registration records. That's a separate SIS integration and a much larger institutional ask | Program-level sponsorship |

The extension stays necessary in every phase: reading a personal schedule out of
my.harvard is the one thing no amount of authentication on *our* side provides.

## Before you share the link widely

Talk to the MDE program manager first. You're republishing a server-side crawl
of my.harvard, and a popular tool makes both your crawler's traffic and an
extension reading session-authenticated pages visible. Official blessing turns
that from a risk into the thing that makes SSO possible at all — and "personal
data never leaves the browser" is a much better opening line than "I have a
database of your cohort's schedules."
