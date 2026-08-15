"""Schema migrations: an ordered list of .sql files, applied exactly once.

Why this exists at all, when `CREATE TABLE IF NOT EXISTS` had been enough for
two years: it stopped being enough the moment a second process appeared. The
app and the collector now start together, both open the database at once, and
"create everything if missing" run twice concurrently is a race — one of them
sees a half-created table. It also cannot express anything but creation: an
`ALTER` guarded by catching the error, which is what this replaces, silently
re-runs on every start and tells you nothing about whether it worked.

The design is deliberately small — a ledger table, a lock, and a checksum:

- **the ledger** (`schema_migrations`) records what has been applied, so a
  migration runs once no matter how many containers start;
- **the advisory lock** makes concurrent starts safe: the second process waits
  and then finds nothing to do, rather than applying the same file twice;
- **the checksum** refuses to run if a file that was already applied has since
  been edited. Editing an applied migration produces two databases with the
  same version number and different shapes, which is the failure that takes
  days to understand — so it is turned into a refusal on startup instead.

Adding one: drop `NNNN_name.sql` in app/migrations/. Never edit an applied file;
write the next one.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

# One arbitrary but fixed number, so every process in this application competes
# for the same lock and no other application shares it by accident.
_LOCK_KEY = 8_531_197

_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover() -> list[tuple[str, str, pathlib.Path]]:
    """Every migration on disk, in version order. Rejects duplicates rather
    than picking one: two files claiming version 0003 means two developers
    wrote the same step, and applying either silently is how the databases
    diverge."""
    found: dict[str, tuple[str, pathlib.Path]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise ValueError(
                f"{path.name} is not a migration filename — expected NNNN_lower_case_name.sql"
            )
        version = match.group("version")
        if version in found:
            raise ValueError(f"two migrations claim version {version}: {found[version][1].name}, {path.name}")
        found[version] = (match.group("name"), path)
    return [(version, name, path) for version, (name, path) in sorted(found.items())]


def applied(conn: psycopg.Connection) -> dict[str, str]:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version     TEXT PRIMARY KEY,
               name        TEXT NOT NULL,
               checksum    TEXT NOT NULL,
               applied_at  TIMESTAMP NOT NULL DEFAULT now()
           )"""
    )
    conn.commit()
    return {
        version: checksum
        for version, checksum in conn.execute(
            "SELECT version, checksum FROM schema_migrations"
        ).fetchall()
    }


def ensure_current(conn: psycopg.Connection) -> int:
    """Apply whatever is pending and return how many ran.

    Called on the first connection either container opens, so there is no step
    to remember and no window where the app runs against an older shape than
    the code expects.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        already = applied(conn)
        ran = 0
        for version, name, path in discover():
            sql = path.read_text(encoding="utf-8")
            digest = _checksum(sql)
            if version in already:
                if already[version] != digest:
                    raise RuntimeError(
                        f"migration {version}_{name}.sql has changed since it was applied. "
                        "An applied migration must never be edited — the databases that ran "
                        "the old text and the new one would report the same version and hold "
                        "different shapes. Write the next migration instead."
                    )
                continue
            # Each migration is one transaction: a file that fails halfway
            # leaves nothing behind and is not recorded, so the next start
            # tries it again from the same place.
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (version, name, digest),
                )
            ran += 1
        return ran
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
