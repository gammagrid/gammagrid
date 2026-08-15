# Upgrading

What happens to **your data** when you move between versions, and whether the
upgrade is safe to do without thinking about it.

`CHANGELOG.md` answers "what changed". This file answers the only question that
decides whether you upgrade today: *what will this do to the database I have
already filled?*

Every release gets an entry here, even a boring one — "nothing happens to your
data" is information, and silence is not the same statement.

## How upgrading works at all

The app owns its schema. On the first connection after you start a new version,
`app/db.py` creates anything missing and applies any new columns to tables that
already exist. There is no migration command to run and no step you can forget.

Your data lives in one file — `data/options.db` by default, or wherever
`OPTIONS_TRACKER_DB` points. Nothing outside that file matters, which makes the
backup for any upgrade a copy:

```bash
cp data/options.db data/options.db.backup
```

Worth doing before any upgrade marked anything other than **safe** below. It
costs a second and it is the whole recovery plan.

## Reading the risk labels

| Label | What it means |
|---|---|
| **Safe** | Start the new version. Nothing is rewritten, nothing is deleted, and the old version still opens the same file afterwards. |
| **One-way** | The database is changed in a way an older version does not understand. Going back needs the backup. Your collected rows are still there and still correct. |
| **Destructive** | Something is rewritten or removed. Back up first, read the entry in full. **No release has been in this category, and the project's first rule is that collected data is never deleted** — if one ever appears here, it will say exactly what goes. |

## v0.3.0 → v0.4.0

**Risk: one-way, and it needs one command from you.** Nothing is lost, but the
application no longer opens the database you have.

**What changes.** Storage moves from SQLite to Postgres. `docker compose up`
now starts a database next to the app and a worker that can collect on a
schedule; your `data/options.db` is left exactly where it is and is never
written to again.

**What you have to do.** Bring your history across, once:

```bash
docker compose up -d
docker compose run --rm -v "$PWD/data:/import" app \
    python scripts/import_sqlite.py /import/options.db
```

It prints what it copied. Add `--dry-run` first if you would rather see the
counts before anything is written. The old file is opened read-only; delete it
yourself once you have seen your charts, and not before.

Databases from before v0.3.0 are handled too — they have no greek columns and
no `source` column, and the import fills those the same way the v0.3.0 upgrade
did: greeks empty, source `yahoo`. Both are simply true, since Yahoo was the
only provider those files ever saw.

**Why it is one-way.** v0.3.0 cannot read a Postgres database, so going back
means going back to `data/options.db` — which still holds everything up to the
day you moved, and nothing after it.

**What you gain, and what it costs.** Scheduled collection becomes possible at
all: a background writer working every few minutes while the dashboard reads is
the workload SQLite's single-writer lock is worst at. The cost is that a backup
is `pg_dump` rather than copying one file, and that running from source now
means having a Postgres to point `DATABASE_URL` at.

**Also in this release:** contracts that expired more than 30 days ago move to a
separate table. They are moved, never deleted, and every historical view reads
both tables — this only keeps the table your charts hit from carrying contracts
that can never trade again.

## v0.2.0 → v0.3.0

**Risk: one-way.** Safe to do, keep the backup until you have seen a chart.

**What changes in the database.** Five columns are added to `option_snapshots`
the first time v0.3.0 opens it: `delta`, `gamma`, `theta`, `vega` and `source`.
Nothing is rewritten and no row is touched.

- The four greek columns are for providers that serve their own greeks. Yahoo
  serves none, so on a stock setup they stay empty and greeks are computed from
  implied volatility exactly as before.
- `source` records which provider produced each row. Rows you already have are
  labelled `yahoo`, which is what they are — Yahoo was the only source this app
  has ever had.

Verified on a real 117,284-row database before release: columns added, every
existing row preserved and correctly labelled, reopening is a no-op.

**Why it is one-way.** v0.2.0 will still open and read the file — SQLite does
not mind extra columns — but any row v0.3.0 wrote carries data v0.2.0 has no
place for, and if you then collect on v0.2.0 you get rows with no `source`.
Nothing breaks loudly; it just makes the history harder to reason about later.
If you need to go back, restore the backup.

**What does NOT change: your numbers.** Greeks now accept a dividend yield, and
the formulas are the generalized Black-Scholes ones. With no dividend yield
supplied — which is every stock setup, since nothing here fetches one — they
reduce *exactly* to the previous formulas. This was checked over 432
combinations of spot, strike, maturity, volatility and rate against the previous
implementation, asserting bit-for-bit equality rather than a tolerance. Your
charts will look identical.

**One number can change, and it is a fix.** The volume-weighted average IV used
to come out empty for a whole day if a single contract in the chain had no
implied volatility. If you ever saw a gap in that chart, it will now have a
value.

**Nothing to do by hand.** No commands, no config changes, no re-collection. If
you have written your own provider against a pre-0.3.0 version, it now has to
return the four greek columns — return `None` for all of them if your source
does not serve greeks; see `app/providers/base.py`.

## Before v0.2.0

Not documented here — this file starts at the release that introduced it. Every
release up to v0.2.0 was additive to the schema and none of them removed or
rewrote data. If you are coming from further back, take the backup, start the
new version, and check that your ticker history still renders.
