-- Refusing to mix two providers on one screen.
--
-- option_snapshots has carried `source` since the provider interface landed,
-- and every row is stamped at write time. Nothing on the read side ever looked
-- at it. With one provider that is invisible; with two it is wrong in a way
-- that reads as a market move — implied volatility is a number a provider
-- *computes*, not one it observes, so the same contract on the same day
-- legitimately differs between sources, and a chart that concatenates them
-- draws a jump nobody traded.
--
-- The worst case is not a mixed chart, though. The views that show "the latest
-- moment" took the newest timestamp regardless of source and then every row at
-- it, so two providers collecting the same minute would feed GEX and Max Pain
-- each contract twice.

-- collection_runs is where the list of collection moments comes from since the
-- read work in v0.4.1 — every history screen now stands on it — and it had no
-- source column at all, so those moments could not be scoped even in
-- principle.
--
-- DEFAULT 'yahoo' backfills the existing rows with the truth rather than a
-- guess: yahoo is the only provider that has ever shipped, so every run already
-- recorded was its. The default also keeps scripts/import_sqlite.py working,
-- which copies this table by an explicit column list written before this
-- column existed.
ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'yahoo';

-- The moment list, now scoped: (ticker, source) equality then started_at as the
-- ordered tail, which is the shape both the list and the "latest run" lookup
-- ask for.
CREATE INDEX IF NOT EXISTS idx_collection_runs_ticker_source_started
    ON collection_runs (ticker, source, started_at DESC);

-- "Which source produced this ticker's newest collection" is asked once per
-- page load and answered from tables that hold one row per collection, never
-- from option_snapshots: its indexes lead with (ticker, source), so the newest
-- moment *across* sources would mean walking every source's whole range —
-- millions of entries to return one string.
CREATE INDEX IF NOT EXISTS idx_iv_summary_ticker_collected
    ON snapshot_iv_summary (ticker, collected_at DESC);
