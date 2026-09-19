-- One row per ticker, source and trading day: what the chain looked like at
-- the end of that day.
--
-- WHY IT EXISTS. Stored history is the property this product has and a web
-- page of today's numbers does not, and until now it was shown in four places
-- that never said so out loud: the heatmap's Replay selector, the history
-- switch, OI Delta and a contract's own chart. The figures a daily summary is
-- made of — the walls, the flip, the regime, net GEX over the near term —
-- existed for the newest moment only, so "what changed since yesterday" could
-- not be answered from anything stored. This row is that answer, written from
-- the chain the collection pass has already solved.
--
-- WHAT "A DAY" IS. New York's calendar date of the collection, weekdays only:
-- exactly the rule the OI Delta and Unusual Activity baseline already use. Two
-- screens with two different "yesterday"s would be one product contradicting
-- itself, so no second rule is invented here. The row is upserted, so by the
-- close it describes the day's last collection.
--
-- WHAT IS NOT REPEATED HERE. The volume-weighted implied volatility stays in
-- snapshot_iv_summary, which already holds one per collection moment — and
-- this row names its moment, so it is one lookup away. Duplicating it would
-- create two numbers for one fact.
--
-- WHAT IS HERE THAT LOOKS LIKE A DUPLICATE AND IS NOT. The day's volume and
-- open interest by side are stored here because nothing else stores them: the
-- rollup table holds the volatility average only, and the alternative is
-- reading a whole chain of a past day to count two columns.
--
-- WHAT IT COSTS. One to two kilobytes per ticker-day, nearly all of it the GEX
-- profile the overlay chart draws. Derived, in the sense that it can be
-- rebuilt from the snapshots at any time — which is what the backfill does —
-- but not deleted on a schedule: at this size, keeping it is cheaper than
-- explaining a gap.
--
-- Additive: a new table, nothing existing touched.
CREATE TABLE IF NOT EXISTS ticker_day_summary (
    ticker            TEXT             NOT NULL,
    source            TEXT             NOT NULL,
    day               DATE             NOT NULL,
    -- The snapshot this row describes: the day's last collection as of writing.
    collected_at      TIMESTAMP        NOT NULL,
    underlying_price  DOUBLE PRECISION NOT NULL,
    -- The gamma-weather figures of that moment (metrics.gamma_weather): net
    -- GEX over the near-term horizon, the walls, the flip, the state.
    net_gex           DOUBLE PRECISION NOT NULL,
    call_wall         DOUBLE PRECISION,
    put_wall          DOUBLE PRECISION,
    gamma_flip        DOUBLE PRECISION,
    state             TEXT             NOT NULL,
    expiry_count      INTEGER          NOT NULL,
    horizon_dte       INTEGER          NOT NULL,
    -- The nearest expiry with enough open contracts to mean something: its max
    -- pain and the one-sigma expected move to it. NULL where there is none.
    nearest_expiry    DATE,
    nearest_max_pain  DOUBLE PRECISION,
    move_lower        DOUBLE PRECISION,
    move_upper        DOUBLE PRECISION,
    -- The chain's shape: how many contracts, and which expiries were listed —
    -- so an expiry that rolled off is named rather than counted as a drop.
    contracts         INTEGER          NOT NULL,
    -- The day's flow by side, summed over the chain of that moment. Four
    -- numbers that the put/call lines of the comparison are made of.
    call_volume       BIGINT,
    put_volume        BIGINT,
    call_oi           BIGINT,
    put_oi            BIGINT,
    expiries          JSONB            NOT NULL,
    -- Net GEX by strike over the same horizon, [[strike, gex], ...]: what the
    -- overlay chart draws, yesterday as an outline and today filled.
    gex_by_strike     JSONB            NOT NULL,
    computed_at       TIMESTAMP        NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    -- The formulas the row was computed with (day_summary.CODE_SHA). A row
    -- built by older code is rebuilt by the backfill rather than compared
    -- against numbers that were arrived at differently.
    core_sha          TEXT             NOT NULL,
    PRIMARY KEY (ticker, source, day)
);
