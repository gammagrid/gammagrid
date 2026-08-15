-- What the scheduled collector needs: a place to keep settings it re-reads,
-- and somewhere for expired contracts to go.

-- Settings the running application changes, as opposed to settings the person
-- who starts it chooses. The collection interval belongs here rather than in an
-- environment variable for one reason: a self-hosted user has no operations
-- team, and "edit compose, then restart both containers" is maintenance, not a
-- setting. The worker re-reads this every cycle, so a change takes effect on
-- the next one without anything being restarted.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- Snapshots of contracts that expired long enough ago to be out of the way.
--
-- MOVED, NEVER DELETED. Collected option data is the most valuable thing this
-- application holds — it is time, and time cannot be re-fetched — so nothing
-- here runs a bare DELETE. A contract that expired last year is history, not
-- rubbish; it simply does not belong in the table every live query reads.
--
-- Same columns as option_snapshots, because archiving is INSERT ... SELECT *
-- and a mismatch would be discovered only when it ran. Deliberately no foreign
-- keys and no defaults: rows arrive already complete.
CREATE TABLE IF NOT EXISTS option_snapshots_archive (
    LIKE option_snapshots INCLUDING DEFAULTS INCLUDING CONSTRAINTS
);

-- Historical reads union this table, because a snapshot is never archived as a
-- whole — only the contracts inside it that have since expired — so a moment
-- from a few months ago has its chain split across the two.
--
-- The index shape is the lesson from the hosted product, where the archive had
-- only a contract-shaped index: looking a moment up there scanned the ticker's
-- entire range and cost 161 ms to return zero rows, on every historical read,
-- whether or not the archive held anything. Add it with the table rather than
-- after somebody measures it again.
CREATE INDEX IF NOT EXISTS idx_snapshots_archive_ticker_source_date
    ON option_snapshots_archive (ticker, source, collected_at);

CREATE INDEX IF NOT EXISTS idx_snapshots_archive_contract_source
    ON option_snapshots_archive (ticker, source, expiry, strike, option_type);
