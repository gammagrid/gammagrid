-- Which model produced the stored volume-weighted average IV.
--
-- WHAT CHANGED ABOVE THIS COLUMN. Until now every number in
-- snapshot_iv_summary came from the data source's own implied volatility, read
-- straight out of option_snapshots by SQL. The application now solves implied
-- volatility itself from the contract's price, and that is not a cosmetic
-- difference: Yahoo — the only source this product ships with — leaves implied
-- volatility missing on part of a chain and reports numbers that do not
-- reproduce the quoted price on another part. Every screen that reads a chain
-- gets our number at read time and needs nothing stored. This one table does:
-- it is a rollup, written once when the chain is in hand, precisely so that the
-- volatility chart does not aggregate millions of rows on every page view.
--
-- NULL MEANS "THE SOURCE'S NUMBER, FROM BEFORE THIS RELEASE" and it is the
-- default on purpose. Nothing is rewritten by this migration: the worker walks
-- the NULL rows in the background, newest first, recomputing each moment from
-- the chain still sitting in option_snapshots, and stamps 'own' as it goes.
-- Until it finishes, the volatility chart says so in one line and the reader is
-- not left to wonder about a step in a curve.
--
-- WHY THE BACKFILL IS SAFE HERE, when a recomputation of collected data
-- normally is not: this table is DERIVED. The rows it is rebuilt from are
-- untouched, so a wrong recomputation costs a rerun rather than a restore, and
-- running it twice cannot produce a different answer.
--
-- ADDITIVE, so a rollback of the code survives it: older code neither writes
-- nor reads this column, and the rows it writes come back as NULL — which is
-- exactly what "not ours" means, so the backfill picks them up again later
-- rather than trusting a number from a model it did not use.
ALTER TABLE snapshot_iv_summary ADD COLUMN IF NOT EXISTS iv_model TEXT;

-- The backfill's only query: "the newest moments still on the source's model".
-- PARTIAL, so the index covers exactly the work that is left and disappears to
-- nothing once there is none — an index over the whole table would grow forever
-- to answer a question that stops being asked.
CREATE INDEX IF NOT EXISTS idx_iv_summary_pending
    ON snapshot_iv_summary (ticker, source, collected_at DESC)
    WHERE iv_model IS NULL;
