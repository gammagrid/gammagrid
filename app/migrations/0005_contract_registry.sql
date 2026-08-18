-- One row per contract, so that expired contracts stay reachable.
--
-- THE PROBLEM IT SOLVES. The Contract tab builds its expiry and strike lists
-- from the LATEST snapshot, and a contract that has expired is not in it. Its
-- whole collected history sits in the database, perfectly readable, and there
-- is no way to ask for it: on expiry day the contract vanishes from the
-- dropdown. Worse, if a stale expiry survives in the widget's state, the strike
-- list under it comes back empty and the page shows a validly selected contract
-- with no data at all.
--
-- WHY NOT `SELECT DISTINCT` OVER THE SNAPSHOTS, which is the obvious answer and
-- was measured before this table was written. On this project's own database:
-- 152,409 index entries read to return 13,449 contracts, 39.6 ms — for ONE
-- ticker with about twelve snapshots each. The cost tracks the number of
-- SNAPSHOTS, not the number of contracts, so it grows with how long you have
-- been collecting and not with the size of the answer. With scheduled
-- collection at fifteen minutes a single ticker reaches two thousand snapshots
-- per contract in a month, and the same query then reads millions of entries to
-- return the same few thousand rows. That is the worst shape of failure: fine
-- in testing, unusable after a few months, and gradual enough that nobody
-- connects it to a change.
--
-- This table is an INDEX OVER the snapshots, never a second source of truth.
-- Counts are recomputed from the rows themselves when it is rebuilt.
CREATE TABLE IF NOT EXISTS contract_registry (
    ticker        TEXT NOT NULL,
    source        TEXT NOT NULL,
    expiry        DATE NOT NULL,
    strike        DOUBLE PRECISION NOT NULL,
    option_type   TEXT NOT NULL,
    first_seen_at TIMESTAMP NOT NULL,
    last_seen_at  TIMESTAMP NOT NULL,
    snapshots     BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, source, expiry, strike, option_type)
);

-- The primary key already answers the main question index-only ("this ticker's
-- contracts for this source, in expiry order"). This one answers the reverse —
-- "what expired in this window" — across tickers.
CREATE INDEX IF NOT EXISTS idx_registry_expiry ON contract_registry(expiry);

-- Backfill from whatever is already on disk, both tables. A contract's rows can
-- be split across them: archiving triggers only some days after expiry, so
-- everything that expired recently is still in the hot table while older
-- contracts are already in the archive. Reading one of them would leave exactly
-- the contracts this feature exists for out of the list.
--
-- The expensive aggregation runs here, ONCE, instead of on every page load.
INSERT INTO contract_registry
    (ticker, source, expiry, strike, option_type, first_seen_at, last_seen_at, snapshots)
SELECT ticker, source, expiry, strike, option_type,
       min(collected_at), max(collected_at), count(*)
FROM (
    SELECT ticker, source, expiry, strike, option_type, collected_at FROM option_snapshots
    UNION ALL
    SELECT ticker, source, expiry, strike, option_type, collected_at FROM option_snapshots_archive
) every_row
GROUP BY ticker, source, expiry, strike, option_type
ON CONFLICT (ticker, source, expiry, strike, option_type) DO UPDATE
SET first_seen_at = LEAST(contract_registry.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at  = GREATEST(contract_registry.last_seen_at, EXCLUDED.last_seen_at),
    snapshots     = EXCLUDED.snapshots;
