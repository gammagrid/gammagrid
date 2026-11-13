# Upgrading

What happens to **your data** when you move between versions, and whether the
upgrade is safe to do without thinking about it.

`CHANGELOG.md` answers "what changed". This file answers the only question that
decides whether you upgrade today: *what will this do to the database I have
already filled?*

Every release gets an entry here, even a boring one — "nothing happens to your
data" is information, and silence is not the same statement.

## How upgrading works at all

The app owns its schema. Every change to it is a numbered file in
`app/migrations/`, and the first connection either container opens applies
whatever is pending — under a lock, so the app and the collector starting
together is safe, and recorded, so nothing runs twice. There is no migration
command to run and no step you can forget.

Your data lives in Postgres (since v0.4.0), in the volume `docker compose`
creates for it. The backup for any upgrade is a dump:

```bash
docker compose exec postgres pg_dump -U gammagrid gammagrid > gammagrid-backup.sql
```

Worth doing before any upgrade marked anything other than **safe** below. It
costs a moment and it is the whole recovery plan.

*(Before v0.4.0 everything lived in `data/options.db`. That file is never
touched by the move and is still the backup for those versions.)*

## Reading the risk labels

| Label | What it means |
|---|---|
| **Safe** | Start the new version. Nothing is rewritten, nothing is deleted, and the old version still opens the same file afterwards. |
| **One-way** | The database is changed in a way an older version does not understand. Going back needs the backup. Your collected rows are still there and still correct. |
| **Destructive** | Something is rewritten or removed. Back up first, read the entry in full. **No release has been in this category, and the project's first rule is that collected data is never deleted** — if one ever appears here, it will say exactly what goes. |

## v0.9.0 → Unreleased

**Risk: safe. No migration; nothing in the database is rewritten or deleted.**

**What you may notice on the first run.**

- **The gamma flip may move, appear or change sides of the price.** It is now
  the price at which total GEX, recomputed as if the underlying stood there,
  changes sign — the definition other GEX tools use. Chains that showed no
  flip, most often with net GEX negative, now usually show one above the
  price; with net GEX positive the level now lies below the price rather than
  above it. The heatmap and the summary compute it as you look, so they show
  the new level at once.
- **The worker rebuilds the Changes rows once.** Each row records which
  version of the formulas produced it (`CODE_SHA`), and this release changes
  them. Rows built by the older code are rebuilt from the stored snapshots at
  the worker's next start, up to 45 days back (`DAY_SUMMARY_BACKFILL_DAYS`),
  so the upgrade does not show up in Changes as a flip that moved overnight.
  Days older than that keep the flip they were stored with.
- **In-the-money contracts count again.** Contracts the data source filed
  with a placeholder volatility (0.00001) now take the volatility of the
  out-of-the-money side of their strike, so on chains heavy in in-the-money
  open interest net GEX, the flip and the heatmap move, and the expected move
  is no longer narrowed. Every screen solves volatility as it reads a chain,
  so this shows at once. The stored volatility average of moments collected
  before the upgrade is not recomputed; it is weighted by volume, which these
  contracts carry little of, so the seam is small.
- **The heatmap and the summary take a little longer.** Re-pricing the chain
  on a grid of prices costs about 50 ms more per summary on an SPX-sized
  chain and 20-80 ms more per heatmap slider move.

## v0.8.0 → v0.9.0

**Risk: safe. No migration; nothing in the database is rewritten or deleted.**

**What you may notice on the first run.**

- **The call and put walls may move.** A call wall now stands at or above the
  price and a put wall at or below it, within twice the expected move (at
  least 5%) of the price; a side with nothing there reads "no wall above
  price" or "no wall below price". Nothing stored holds a wall the screens
  read back except the Changes rows, and those are covered next.
- **The worker rebuilds the Changes rows once.** Each row records which
  version of the formulas produced it, and this release changes them. Rows
  built by the older code are rebuilt from the stored snapshots at the
  worker's next start, up to 45 days back (`DAY_SUMMARY_BACKFILL_DAYS`), so
  the upgrade does not show up in Changes as a wall that moved overnight.
- **SPX, NDX, RUT and DJX on their monthly expiration date.** Time to expiry
  for those dates is measured to the 09:30 open rather than the 16:00 close
  until that morning's open, so their volatility, greeks and GEX shift a
  little in the days before a monthly expiration. Time to expiry for every
  other ticker and date is exactly what it was.

## v0.7.0 → v0.8.0

**Risk: safe. No migration; nothing in the database is rewritten or deleted.**

**What you may notice on the first run.**

- **`yfinance` is pinned to exactly 1.7.0.** It used to be `>=0.2.40`, which
  let the same commit collect differently depending on the day the image was
  built. `docker compose up --build` installs the pinned version on its own.
  Running from source, run `pip install -r requirements.txt` again — an older
  yfinance left in your environment is not what this release was checked
  against.
- **The worker rebuilds the Changes rows once.** Each row records which
  version of the formulas produced it, and this release changes the
  calculation code (for speed — every number comes out identical). Rows built
  by the older code are rebuilt from the stored snapshots at the worker's next
  start, up to 45 days back, exactly as on the upgrade to v0.7.0 — at under
  half of that release's cost per ticker-day.
- **Changes may offer fewer days than before.** A day now counts only once
  its session has opened (weekends, exchange holidays and the snapshot the
  collector takes overnight before the open are not days), and the rows an
  older version wrote for such days are skipped when read — nothing is
  deleted by the upgrade itself, and going back to v0.7.0 shows them again.
  To remove them from the table as well, look first, then delete:

  ```bash
  docker compose run --rm --no-deps worker \
      python -m app.day_summary --drop-non-session-days --dry-run
  docker compose run --rm --no-deps worker \
      python -m app.day_summary --drop-non-session-days
  ```

  It removes only day-summary rows — never a collected snapshot, which is what
  they are rebuilt from — and running it a second time finds nothing.
  `--ticker SPY` narrows it to one symbol.
- **SPX, XSP, NDX, RUT and DJX can be added.** Nothing already collected is
  touched. If an earlier version refused one of them, add it again; the ETF it
  suggested (SPY, QQQ, IWM, DIA) keeps its own history and can stay. SPX is a
  large chain — about 18,000 contracts and some 20 seconds per collection —
  so every pass with it in the watchlist takes that much longer and stores
  roughly twice what SPY does.
- **Fewer failures are counted.** The collection log is not rewritten:
  runs refused because the market was closed stay `failed` with their reason,
  and are simply no longer counted towards the "collection is failing" note
  or towards suspending a symbol. If a symbol is currently suspended only
  because of overnight refusals, it is retried on the next pass.

## v0.6.1 → v0.7.0

**Risk: safe. Two additive migrations, nothing rewritten and nothing deleted.**

`0008_realized_volatility` and `0009_ticker_day_summary` create two new tables
and touch nothing that exists. An older build opens the same database
afterwards and ignores both; every snapshot you have collected is read exactly
as it was.

**What you may notice on the first run.**

- **A new view, "Changes", is empty at first and fills itself in.** It compares
  two trading days, and the rows it compares are written after each collection.
  Days already in your database are filled in from the snapshots — the worker
  does it once at startup and once a day, up to 45 days back, and one day that
  cannot be rebuilt is skipped rather than stopping the rest. On a database
  with a long history this is the one noticeably busy thing in the release; it
  runs in the collector, not on a screen, and costs roughly 80 ms per
  ticker-day. To do it yourself, or to go further back:

  ```bash
  docker compose run --rm --no-deps worker \
      python -m app.day_summary --backfill --days 120 --dry-run
  ```

  Drop `--dry-run` to write. With two days on record the view works; with one
  it says so plainly instead of drawing a screenful of "new".

- **Realized volatility on the Contract tab now names its date.** The figures
  used to be fetched from the data source while the page was drawing; they are
  now stored once a day by the collection pass. Right after the upgrade a
  ticker has no stored row yet, and the caption says so — the next collection
  fills it. The numbers themselves are computed exactly as before.

- **Screens now say when they stopped being current.** Nothing changes while
  collection is healthy. If your collector has been failing on a symbol, or the
  worker has not been running, you will see that said out loud where before the
  charts simply showed the last thing they had.

- **A one-line summary sits above the views.** It reads the near-term expiries
  of the chain you already loaded; no extra collection and no new network call.

## v0.6.0 → v0.6.1

**Risk: safe. No migration, no schema change, nothing rewritten and nothing
deleted.**

Everything in this release is a fix to code already in place — the shared
calculation core and one dashboard control — so nothing here reads or writes
the database differently than before.

**What you may notice on the first run.**

- **Gamma Flip may show a different number.** The level is meant to be the
  zero crossing of the cumulative GEX profile nearest the underlying price;
  it was instead reporting the first crossing found while walking the whole
  strike range, which on a real chain is reliably the deepest one, far below
  where the chart actually crosses. The new number is the one nearer the
  money — closer to what the chart already shows changing sign.
- **Charm's own description now says "per year" instead of "per day."** The
  number itself never changed, only the sentence under it; the daily figure
  is still quoted alongside it for anyone who thinks in days.
- **The GEX Heatmap's second slider now asks for a count of strikes instead
  of a percentage.** The old percentage control silently stopped doing
  anything past a point on dense chains (SPY, SPX) — it produced the same
  table from 5% to 50%. The replacement responds visibly across its whole
  range, on every chain.

## v0.5.2 → v0.6.0

**Risk: safe. One additive migration. Nothing collected is rewritten, nothing
is deleted, and an older version still opens the same database afterwards.**

`0006_own_implied_volatility.sql` adds one nullable column to
`snapshot_iv_summary` and one partial index; `0007_symbol_catalogue.sql` adds
one new table for the downloaded symbol directory. Neither touches a chain row.
An older version neither writes nor reads either of them, so going back works
and costs nothing.

**What you may notice on the first run.**

- **Volatility numbers move, everywhere.** The application now solves implied
  volatility from the contract's price instead of reading the data source's
  own figure — which Yahoo leaves missing on part of a chain and computes
  differently on the rest. Every greek, the GEX profile, the screener's
  filters and the surface are built on that column, so they all shift a
  little. Nothing about your collected rows changed; the source's own value is
  still stored beside ours.
- **The Volatility chart says that its earliest points are still catching
  up.** That chart reads a stored average, and every point of it written
  before this version holds the source's number. There is nothing to run: the
  collector recomputes them in the background, newest first, a batch per pass,
  and the line disappears when there are none left. On a year of
  fifteen-minute collection this takes a few dozen collection cycles. Turning
  the schedule off simply pauses it.
- **Nothing is collected on market holidays.** Previously a holiday looked
  like a normal weekday and produced a day of duplicate snapshots. Those
  duplicates are still in your database — this project does not delete
  collected data — and they still count as trading days in the metrics built
  on daily history. New ones stop appearing.
- **An index symbol in your watchlist stops being requested and explains
  itself.** SPX, XSP, NDX, RUT, DJX and VIX have never collected anything here
  and never could: Yahoo serves no option chain for a cash-settled index. They
  now say so on the page, and name what tracks the same underlying. Removing
  them is up to you; nothing about them is deleted.
- **The Add-ticker box searches by company name now.** The first collector
  pass after the upgrade downloads the public Cboe directory of symbols with
  listed US options — about 5,300 rows, roughly 1.5 MB in your database — and
  refreshes it weekly after that. **This is a new outbound call**, the only one
  in the product that is not to the data source and not the TradingView widget
  in the page; it sends nothing about you, your watchlist or what you collect.
  If the machine cannot reach `cboe.com`, or you would rather it did not, the
  only consequence is that the box stays what it was: a field you type a symbol
  into. Nothing about collecting or drawing depends on it.
- **A new dependency, `exchange_calendars`.** `docker compose up --build`
  installs it with everything else. Running from source needs
  `pip install -r requirements.txt` again — and if you skip it, the
  application still starts and simply keeps its old, holiday-blind calendar
  rather than failing.

## v0.5.1 → v0.5.2

**Risk: safe. No migration, no schema change, nothing rewritten and nothing
deleted.**

Everything in this release is code and text. The suspension of hopeless
symbols, the rate-limit cooldown and the version display all read state that
already exists — the collection log and `app_settings` — precisely so that an
upgrade does not need a schema change.

**What you may notice on the first run.**

- **A ticker in your watchlist may appear with a ⏸ mark.** That means every
  collection it has ever had has failed and none has ever succeeded — it is
  almost always a typo that has been quietly failing for a while. It stops
  being requested; nothing about it is deleted, and removing or correcting it
  is up to you.
- **A ticker with adjusted contracts starts collecting for the first time.**
  If one of yours has been failing since you added it and you never found out
  why, this is the likely reason. There is no backfill: history begins now,
  because the failed collections never stored anything to recover.
- **The estimated monthly disk figure drops by roughly a factor of five.** The
  old number was wrong, not the new one. Nothing about your database changed.
- **Adding a ticker can now refuse it.** Only when the data source says the
  symbol has no options at all. If the source cannot be reached, the ticker is
  accepted as before.

## v0.5.0 → v0.5.1

**Risk: safe. No migration, no schema change, nothing touched in the database.**

Text and one button. The panel that splits a contract's price change by greek
now says which of its two reasons refused a decomposition; before, a contract
that was simply too cheap was told its implied volatility had failed the
data-quality guard, which is a different problem and sends you looking for one
you do not have. No stored row changes and no number on any chart moves.

## v0.4.2 → v0.5.0

**Risk: safe. One new table, nothing rewritten, no data touched.**

Migration `0005_contract_registry.sql` creates `contract_registry` and fills it
from the snapshots you already have. On a database with a few hundred thousand
rows this takes seconds; it is one aggregation, run once, and it is the reason
the expired-contract lists are a lookup rather than a scan.

Nothing is deleted or rewritten. The registry is an index over the snapshots,
not a second source of truth — it can be rebuilt from them at any time.

**What changes in behaviour:** with collection on a schedule, the collector now
takes ONE snapshot per closed day instead of one per interval. If you want the
old behaviour back — collecting round the clock — there is no setting for it in
this release; open an issue and say why, because the measurements say those
snapshots are duplicates.

## v0.4.1 → v0.4.2

**Risk: safe.** Start the new version. Nothing is rewritten and nothing is
deleted.

**What changes in the database.** One column is added — `collection_runs.source`
— and existing rows get `yahoo`, which is what they were: yahoo is the only
provider that has ever shipped. Two indexes are added alongside it. Your
snapshots are not touched.

**What you will notice.** Nothing at all, if you use one data source — which
today means everybody running a stock install. If you have written your own
provider and collected with both, the screens now show one of them at a time:
the one that collected most recently. The other's rows stay in the database and
come back into view the moment that provider is the freshest again.

This is a correctness fix rather than a preference. Implied volatility is a
number a provider *calculates*, not one it observes, so two providers
legitimately disagree about the same contract on the same day — and the views
that show "the latest collection" previously took the newest timestamp
regardless of source and then every row at it, so two providers collecting the
same minute counted each contract twice in dealer GEX and Max Pain.

**Going back.** v0.4.1 runs fine on the same database: it does not write the new
column, and the default fills it in.

## v0.4.0 → v0.4.1

**Risk: safe.** Start the new version. Nothing is rewritten and nothing is
deleted.

**What changes in the database.** Two tables are added on first start —
`snapshot_iv_summary` and `contract_volume_stats` — and the first is filled
from what you have already collected, archive included. Expect that backfill to
take seconds rather than minutes; it is one pass and it runs once.

Both hold numbers that used to be recomputed on every page view. Your snapshots
are not touched.

**What you will notice.** Pages that used to get slower as your history grew no
longer do. One number moves: the volume-weighted average IV on the Volatility
chart is now what it was at the moment of collection, and stops drifting as
contracts expire and move to the archive — historical points may sit slightly
differently than they did yesterday, once, and then never again.

**Going back.** v0.4.0 ignores the two new tables and runs fine on the same
database. One caveat if you do go back and keep collecting: the old version
does not write `snapshot_iv_summary`, so the Volatility chart will have gaps
for whatever you collect while you are there, and coming forward again does not
fill them — the backfill has already run. Everything else catches up on its own.

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
