"""Vercel entrypoint.

Vercel turns each file under `api/` into its own serverless function, which is
why the application package is `server/` and not `api/` -- otherwise
`policy.py` and `conflicts.py` would each be deployed as a separate function.
This module is the only one here: it re-exports the ASGI app.

Locally, run the app directly instead:

    uvicorn server.main:app --reload --port 8000
"""
import sys
from pathlib import Path

# Vercel invokes this file directly, so the repo root is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.main import app  # noqa: E402

# Vercel's Python runtime looks for `app` (ASGI) or `handler`.
handler = app
