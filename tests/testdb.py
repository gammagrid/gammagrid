"""The one place that decides which database the checks are allowed to touch.

They TRUNCATE. Against a throwaway database that is housekeeping; against a
real one it is unrecoverable, and the only thing between the two used to be the
default value of an environment variable.

That stopped being enough on 2026-08-15. A refactor dropped the `setdefault`
out of one suite, the suite fell back to `app.config`'s default — which is the
same connection string somebody running from source uses for their own data —
and the first thing it would have done is empty every table. Nothing was lost:
an unrelated check happened to raise first. There is no reason to expect an
unrelated check next time.

So the target is verified rather than assumed. A database name that does not
say it is disposable is refused, loudly, before anything is opened.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

DEFAULT_TEST_URL = "postgresql://gammagrid:gammagrid@localhost:5432/gammagrid_test"

REQUIRED_SUFFIX = "_test"


def configure() -> str:
    """Point this process at the checks' database and confirm it is one.

    Call before importing anything from `app`: `app.config` reads the
    environment at import time, so a later assignment has no effect and the
    checks would run somewhere else entirely.

    setdefault rather than assignment, because `tests/coverage_report.py` runs
    several suites in one process — whoever imports first decides, and the rest
    agree rather than fighting over it.
    """
    os.environ.setdefault("DATABASE_URL", DEFAULT_TEST_URL)
    url = os.environ["DATABASE_URL"]
    name = urlparse(url).path.lstrip("/")
    if not name.endswith(REQUIRED_SUFFIX):
        raise SystemExit(
            f"refusing to run the checks against a database named {name!r}.\n"
            f"These scripts empty every table they use, so they only run against a database "
            f"whose name ends in {REQUIRED_SUFFIX!r} — create one and point DATABASE_URL at it:\n"
            f"    createdb gammagrid_test\n"
            f"    DATABASE_URL={DEFAULT_TEST_URL} python tests/coverage_report.py"
        )
    return url


def truncate_all(conn) -> None:
    """Empty the tables the checks write to, leaving the schema alone.

    TRUNCATE rather than DROP: the shape belongs to the migrations, and a copy
    of it here would be a second definition to keep in step. RESTART IDENTITY
    so that checks asserting on row ids see the same numbers on every run.
    """
    with conn.cursor() as cur:
        for table in ("option_snapshots", "collection_runs", "tracked_contracts", "watchlist"):
            cur.execute(f"TRUNCATE {table} RESTART IDENTITY")  # noqa: S608 — fixed list above
