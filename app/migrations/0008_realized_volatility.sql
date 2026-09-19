-- Realized volatility of the underlying: one row per ticker, day and window.
--
-- WHAT IT REPLACES. The Contract view prints RV(10d), RV(20d) and RV(30d)
-- beside the contract's implied volatility, and until now every one of those
-- captions was computed from six months of daily closes fetched over the
-- network WHILE THE PAGE WAS BEING DRAWN — with a thirty-minute cache in front
-- of it as the only thing keeping that bearable. The file that does it says,
-- twenty lines higher and about the market status, that "a page render has no
-- business making a network call". Both cannot be right.
--
-- WHY A ROW A DAY. The inputs are daily closes, so the number physically
-- cannot change during a session: a row per (ticker, as_of_date) is exactly as
-- fresh as the data it comes from and no fresher. Refreshed by the collector,
-- once a day per ticker, on a pass somebody has already asked for — so nothing
-- new is scheduled, no second process is introduced, and an installation that
-- collects by hand with the worker switched off gets the same figures as one
-- that collects on a timer.
--
-- WHY A ROW PER WINDOW rather than three columns: the windows are a tuple in
-- metrics_core.realized_volatility, and a fourth one would otherwise be a
-- migration. Keeping the history costs a few dozen bytes a day and is the
-- series any future "where does today's volatility sit against its own past"
-- would need.
--
-- `source` records where the closes came from, which need not be the provider
-- serving the option chains — a paid chain source may sell price history
-- separately, and the view says which one answered.
--
-- Additive: a new table, nothing existing touched. Rolling back the code
-- leaves it in place and unread.
CREATE TABLE IF NOT EXISTS realized_volatility (
    ticker      TEXT    NOT NULL,
    as_of_date  DATE    NOT NULL,
    window_days INTEGER NOT NULL,
    value       DOUBLE PRECISION NOT NULL,   -- annualised, a fraction: 0.187, never 18.7
    source      TEXT    NOT NULL,
    fetched_at  TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (ticker, as_of_date, window_days)
);
