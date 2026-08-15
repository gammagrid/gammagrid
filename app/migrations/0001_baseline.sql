-- The schema as of the move from SQLite to Postgres.
--
-- Written with IF NOT EXISTS throughout, unlike every migration that will
-- follow it: this one has to converge two starting points — a database created
-- by the SQLite-era `CREATE TABLE IF NOT EXISTS` block, imported by the
-- migration tool, and an empty one. From 0002 onwards the ledger in
-- app/migrate.py is what guarantees a file runs once, and migrations may be
-- written plainly.

CREATE TABLE IF NOT EXISTS watchlist (
    ticker   TEXT PRIMARY KEY,
    added_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS option_snapshots (
    id                 BIGSERIAL PRIMARY KEY,
    ticker             TEXT NOT NULL,
    collected_at       TIMESTAMP NOT NULL,
    underlying_price   DOUBLE PRECISION NOT NULL,
    expiry             DATE NOT NULL,
    strike             DOUBLE PRECISION NOT NULL,
    option_type        TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
    last_price         DOUBLE PRECISION,
    bid                DOUBLE PRECISION,
    ask                DOUBLE PRECISION,
    volume             INTEGER,
    open_interest      INTEGER,
    implied_volatility DOUBLE PRECISION,
    in_the_money       BOOLEAN,
    -- Greeks as the provider served them, stored rather than recomputed: they
    -- come from the provider's own model and cannot be reconstructed later.
    -- Yahoo serves none, so with the default setup these stay NULL and the
    -- reader computes greeks from implied volatility (see metrics_core).
    delta              DOUBLE PRECISION,
    gamma              DOUBLE PRECISION,
    theta              DOUBLE PRECISION,
    vega               DOUBLE PRECISION,
    -- Which provider produced this row. Charts must never mix sources:
    -- implied volatility is a *computed* number, so the same contract on the
    -- same day legitimately differs between providers, and splicing two of
    -- them draws a jump that never happened on the market.
    source             TEXT NOT NULL DEFAULT 'yahoo'
);

-- TWO INDEXES, AND THE SHAPES ARE DELIBERATE. Both now lead with
-- (ticker, source): every query filters by source, because a chart that mixes
-- providers is wrong rather than merely odd, and an index that stops at
-- `ticker` leaves that filter to be applied after the scan. The column has
-- existed since v0.3.0; the indexes had not caught up.
--
-- Time-ordered access — the latest snapshot, a date range, the list of
-- collection moments. Few distinct keys over many rows, so Postgres's btree
-- deduplication makes it very small: measured on the hosted sibling at 7.9
-- bytes per row.
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_source_date
    ON option_snapshots (ticker, source, collected_at);

-- One contract's history, and by prefix one expiry's whole chain.
--
-- WITHOUT a trailing collected_at, which is where this deliberately differs
-- from the hosted product. Adding it makes almost every index entry unique and
-- defeats deduplication — measured there at 81.6 bytes per row against 49.8
-- without. The hosted side needs it for a year-long per-expiry query over
-- millions of rows; a self-hosted install does not, and 60% more index on
-- somebody's laptop for a query they will not run is the wrong trade.
CREATE INDEX IF NOT EXISTS idx_snapshots_contract_source
    ON option_snapshots (ticker, source, expiry, strike, option_type);

CREATE TABLE IF NOT EXISTS collection_runs (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    ticker           TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error_message    TEXT,
    rows_fetched     INTEGER,
    oi_zero_fraction DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_started
    ON collection_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS tracked_contracts (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL,
    expiry      DATE NOT NULL,
    strike      DOUBLE PRECISION NOT NULL,
    option_type TEXT NOT NULL CHECK (option_type IN ('call', 'put')),
    added_at    TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (ticker, expiry, strike, option_type)
);
