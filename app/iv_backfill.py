"""Bringing the stored volatility average onto our own model, by itself.

WHAT THIS IS FOR. Every screen that draws a chain solves implied volatility at
read time, from the contract's price, and needs nothing stored to do it. One
number is different: the volume-weighted average per collection lives in
`snapshot_iv_summary`, written once when the chain is in hand, so that the
volatility chart is a few hundred rows to read instead of a few million to
aggregate. That rollup was written from the data source's own volatility for as
long as this product has existed, and the rows are still there.

WHY IT IS NOT A COMMAND SOMEBODY RUNS. This product is installed by one person
on one machine and then left alone. A documented "run this after upgrading"
step is a step almost nobody performs, and the result would be a permanent step
in a curve that every reader has to be told about. So the worker does it: a
bounded batch per pass, newest moments first, until there are none left. A
machine that was switched off catches up on its next start, and one that never
runs the worker still gets its newest moment converted by the collector, which
calls this after every successful collection.

WHY RECOMPUTING IS SAFE HERE. `snapshot_iv_summary` is DERIVED — every value in
it can be rebuilt from `option_snapshots`, which this module only reads. The
rules for a recomputation that touches collected data do not apply because
nothing collected is touched: a wrong answer costs a rerun rather than a
restore, and running the same moment twice cannot produce a different number.

NOTHING HERE IS ALLOWED TO RAISE. A failure to solve a volatility must not take
down a collection pass or a worker cycle, and a moment that failed keeps its
NULL stamp — so the next pass picks it up again instead of recording somebody
else's model as ours.
"""

from __future__ import annotations

import logging

from app import config, db, metrics

log = logging.getLogger(__name__)


def backfill_ticker(
    conn, ticker: str, limit: int = config.IV_BACKFILL_MOMENTS_PER_PASS,
    source: str | None = None,
) -> int:
    """Recompute up to `limit` stored averages for one ticker. Returns how many.

    The chain for each moment is read back from the database rather than passed
    in, and that is what makes one function serve both callers: the collector
    has the chain in memory and the worker does not, but neither has to know
    which of them is running.
    """
    try:
        moments = db.iv_summary_pending_moments(conn, ticker, limit, source=source)
        if not moments:
            return 0
        chains = db.get_snapshots_at(conn, ticker, moments, source=source)
        if chains.empty:
            # The rollup names a moment whose rows are gone. Nothing to solve
            # and nothing to wait for, so the stamp is applied anyway with the
            # average untouched — otherwise this moment is re-read on every
            # pass forever, and the pass never reaches the ones it can fix.
            return db.store_own_iv_weighted_average(
                conn, ticker, source, dict.fromkeys(moments)
            )
        solved = metrics.with_solved_iv(chains, metrics.DEFAULT_PRICING, ticker=ticker)
        averages = metrics.iv_weighted_average(solved)
        values = {
            row.collected_at: (None if row.iv_weighted_avg != row.iv_weighted_avg
                               else float(row.iv_weighted_avg))
            for row in averages.itertuples()
        }
        # Every moment asked for is stamped, including any the average came back
        # empty for: see store_own_iv_weighted_average for why a moment that
        # cannot be answered is not asked again.
        for moment in moments:
            values.setdefault(moment, None)
        written = db.store_own_iv_weighted_average(conn, ticker, source, values)
        if written:
            log.info(
                "Recomputed %s stored volatility average(s) for %s with our own model.",
                written, ticker,
            )
        return written
    except Exception:  # noqa: BLE001 — see the module docstring: never fail a caller
        log.error(
            "Could not recompute the stored volatility averages for %s. The chart keeps "
            "the data source's numbers for those moments and the next pass tries again; "
            "collection itself is unaffected.",
            ticker, exc_info=True,
        )
        return 0


def backfill_watchlist(conn, limit: int = config.IV_BACKFILL_MOMENTS_PER_PASS) -> int:
    """One batch per watched ticker. Returns how many averages were rewritten.

    Per ticker rather than one global batch: a ticker with two years of history
    would otherwise hold the whole queue and everything added later would wait
    behind it, which is the shape of a backfill people conclude is stuck.
    """
    return sum(backfill_ticker(conn, ticker, limit) for ticker in db.get_watchlist(conn))
