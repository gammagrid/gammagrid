-- Two numbers that used to be computed on every page view, stored instead.
--
-- Both are the same shape of problem, and it is the one scheduled collection
-- created: an aggregate whose input grows with how long you have been
-- collecting while its answer does not. Measured on the hosted product, which
-- hit this first: a year of one liquid ticker was 3.1M rows, 22 seconds and
-- 0.8 GB of memory to draw charts that are a few hundred points wide.

-- Volume-weighted average IV, one row per collection moment.
--
-- Computed when the chain is collected rather than when somebody looks, so the
-- Volatility chart is a lookup over one row per collection instead of an
-- aggregate over one row per contract per collection.
--
-- It also stops the chart from changing under you. Computed on read against
-- the live table, a point from three months ago was re-averaged over a
-- shrinking set of contracts as they expired and moved to the archive — the
-- same July point showed one value in August and another in October. Stored at
-- collection time, it is what it was.
--
-- NULL is a value here, not a missing row: that moment had nothing with both an
-- implied volatility and a nonzero volume, and the chart must show a gap rather
-- than a straight line between the neighbours.
CREATE TABLE IF NOT EXISTS snapshot_iv_summary (
    ticker          TEXT NOT NULL,
    source          TEXT NOT NULL,
    collected_at    TIMESTAMP NOT NULL,
    iv_weighted_avg DOUBLE PRECISION,
    PRIMARY KEY (ticker, source, collected_at)
);

-- Per-contract volume statistics for Unusual Activity, recomputed once a day.
--
-- The z-score compares today's volume against the contract's own recent
-- history, which in pandas means loading one snapshot per day per contract for
-- the whole window first — and the result is one row per contract, which does
-- not grow with time. Recomputed by the worker after a day closes rather than
-- on somebody's click; staleness is not a compromise, since the baseline
-- deliberately excludes today.
--
-- NULL std_volume is a value: a contract seen on a single day has no sample
-- deviation, and that is what stops a one-point history from producing an
-- infinite z-score.
CREATE TABLE IF NOT EXISTS contract_volume_stats (
    ticker         TEXT NOT NULL,
    source         TEXT NOT NULL,
    expiry         DATE NOT NULL,
    strike         DOUBLE PRECISION NOT NULL,
    option_type    TEXT NOT NULL,
    avg_volume     DOUBLE PRECISION,
    std_volume     DOUBLE PRECISION,
    history_points INTEGER NOT NULL,
    -- The last day included above, so a stale row reads as stale rather than
    -- merely old.
    through_day    DATE NOT NULL,
    computed_at    TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, source, expiry, strike, option_type)
);

-- Backfill what has already been collected, archive included. The union
-- happens before the aggregate on purpose: one collection moment can have rows
-- in both tables, since archiving moves expired contracts rather than whole
-- snapshots, and aggregating each table separately would produce two partial
-- averages for the same moment.
--
-- Rows with no implied volatility are dropped rather than weighted at zero
-- (NaN * 0 is still NaN, and one contract without IV would empty the moment),
-- and contracts whose expiry has already passed are excluded entirely: they
-- have no volatility left, a source reporting one is reporting a sentinel, and
-- they still carry the whole session's volume. On an index ETF that turned a
-- ticker average of 0.163 into 13.53, once a day, every day.
INSERT INTO snapshot_iv_summary (ticker, source, collected_at, iv_weighted_avg)
SELECT
    ticker,
    source,
    collected_at,
    CASE WHEN SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                       THEN COALESCE(volume, 0) ELSE 0 END) > 0
         THEN SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                       THEN implied_volatility * COALESCE(volume, 0) ELSE 0 END)
              / SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                         THEN COALESCE(volume, 0) ELSE 0 END)
    END
FROM (
    SELECT ticker, source, collected_at, expiry, implied_volatility, volume
    FROM option_snapshots
    UNION ALL
    SELECT ticker, source, collected_at, expiry, implied_volatility, volume
    FROM option_snapshots_archive
) all_collected_rows
GROUP BY ticker, source, collected_at
ON CONFLICT (ticker, source, collected_at) DO NOTHING;
