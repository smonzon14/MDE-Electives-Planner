"""Turn a working catalog DB into a deploy-ready, read-only artifact.

The deployed API opens the catalog with `connect_readonly()`, which cannot
write anything. Two properties have to be true before that works:

1. **No WAL.** A journal_mode=WAL database writes to its `-wal` sidecar even on
   a pure read, so opening one with `mode=ro` fails outright. The crawler wants
   WAL (it lets people browse during an 8-minute ingest); the shipped copy must
   not have it.

2. **No personal data.** `user_schedule`, `student_profile` and `plan_items` are
   dead as of phase 1 -- personal state lives in the browser now. Any rows left
   over from local testing would otherwise be baked into a public deployment,
   so they are dropped rather than merely emptied.

    python -m ingest.seal                      # -> data/courses.deploy.db
    python -m ingest.seal --output /tmp/x.db
    python -m ingest.seal --in-place           # seal data/courses.db itself
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import DB_PATH

# Dropped, not truncated: an empty table still invites someone to write to it,
# and the read-only server has no code left that touches these.
PERSONAL_TABLES = ["user_schedule", "student_profile", "plan_items"]


def seal(src: Path, dest: Path) -> dict:
    if not src.exists():
        raise SystemExit(f"no database at {src} -- run `python -m ingest.crawl` first")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    for sidecar in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    # VACUUM INTO writes a fresh, fully-checkpointed copy without disturbing the
    # source -- safer than mutating the working DB the crawler owns.
    src_conn = sqlite3.connect(src)
    src_conn.execute("PRAGMA busy_timeout = 30000")
    src_conn.execute("VACUUM INTO ?", (str(dest),))
    src_conn.close()

    out = sqlite3.connect(dest)
    dropped = []
    for table in PERSONAL_TABLES:
        exists = out.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists:
            n = out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            out.execute(f"DROP TABLE {table}")
            dropped.append((table, n))
    out.commit()

    # DELETE mode leaves no sidecar for the reader to need. Must come after the
    # drops, since changing journal_mode is itself a write.
    mode = out.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    out.execute("VACUUM")
    counts = {
        t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("courses", "meetings")
    }
    out.close()

    # VACUUM can leave a -wal behind from the connection that ran it.
    for sidecar in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()

    return {"journal_mode": mode, "dropped": dropped, "counts": counts,
            "bytes": dest.stat().st_size}


def verify(dest: Path) -> None:
    """Prove the artifact is actually readable the way the server opens it."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from ingest.db import connect_readonly

    conn = connect_readonly(dest)
    n = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    conn.close()
    print(f"  verified: opened read-only, {n:,} courses visible")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DB_PATH)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--in-place", action="store_true",
                    help="replace the source database with the sealed copy")
    args = ap.parse_args()

    dest = args.output or args.source.with_name("courses.deploy.db")
    if args.in_place:
        dest = args.source.with_name("courses.sealing.tmp.db")

    info = seal(args.source, dest)

    if args.in_place:
        final = args.source
        for sidecar in (final.with_name(final.name + "-wal"),
                        final.with_name(final.name + "-shm")):
            if sidecar.exists():
                sidecar.unlink()
        dest.replace(final)
        dest = final

    print(f"sealed {args.source} -> {dest}")
    print(f"  journal_mode={info['journal_mode']}  "
          f"{info['bytes'] / 1e6:.1f} MB  "
          f"{info['counts']['courses']:,} courses / {info['counts']['meetings']:,} meetings")
    for table, n in info["dropped"]:
        print(f"  dropped {table} ({n} row(s) of personal data)")
    if not info["dropped"]:
        print("  no personal tables present")
    verify(dest)


if __name__ == "__main__":
    main()
