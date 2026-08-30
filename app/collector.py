"""Collection orchestration. Everything here is provider-agnostic.

This module used to be "the only module that talks to the external data
source". That is now app/providers/ — but the rule it protected still holds:
nothing outside app/providers/ makes network requests, and nothing inside
app/providers/ knows about the database.

What stays here is the part that is the same whichever source is active: the
quality gate, run logging, and the guarantee that one failing ticker cannot
take down the rest of the batch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app import config, db, iv_backfill, providers


def fetch_ticker_snapshot(
    ticker: str, provider: providers.DataProvider | None = None
) -> tuple[float, pd.DataFrame]:
    """Kept as a module-level function for call sites that predate providers."""
    return (provider or providers.get_provider()).fetch_ticker_snapshot(ticker)


def fetch_price_history(
    ticker: str, period: str = "6mo", provider: providers.DataProvider | None = None
) -> pd.DataFrame:
    return (provider or providers.get_provider()).fetch_price_history(ticker, period)


def _now_utc() -> datetime:
    """Naive datetime, UTC by convention. No tzinfo — avoids tz-aware/tz-naive
    conflicts in date arithmetic with expiry dates in metrics.py, which come
    from the DB as naive (dates without time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _oi_zero_fraction(chain_df: pd.DataFrame) -> float:
    """Fraction of contracts with open_interest=0 in a freshly collected
    chain. The real-world signal for detecting corrupted data-source
    responses — see `config.MAX_ZERO_OI_FRACTION`."""
    if chain_df.empty:
        return 1.0
    return float((chain_df["open_interest"].fillna(0) == 0).mean())


def collect_watchlist(
    conn, tickers: list[str], provider: providers.DataProvider | None = None
) -> dict[str, str]:
    """Collects a snapshot for every ticker in the watchlist. One ticker
    failing does not interrupt collection for the rest (spec FR12). Returns
    a status per ticker.

    Before saving, the fraction of contracts with open_interest=0 is checked
    (spec FR23): the data source sometimes responds "successfully" (no
    exception, chain structure intact — same strikes/expiries, volume and
    prices look normal) but open_interest and implied_volatility come back
    almost entirely zero/near-zero. Such a snapshot looks ordinary but breaks
    the GEX Heatmap (gamma is computed from OI) and OI Delta (day-over-day
    comparison). The check rejects it before it reaches the DB — better to
    skip one collection cycle for a ticker than to silently corrupt the
    history once.

    A warning if you add a provider: this threshold was calibrated against
    Yahoo. The hosted version had to scope it per source after a wider chain
    tripped it for five days on a ticker whose data was perfectly good — a
    provider that serves the full chain, including everything untraded, has a
    much higher baseline of zero-OI contracts and is not corrupt for it. The
    fraction is computed and logged either way.

    Every row and every log line is stamped with the provider's name: charts
    must never mix sources, and that is only enforceable if the source is
    recorded at write time.

    A symbol that has failed repeatedly and never once produced a snapshot is
    skipped rather than attempted — see db.unresolvable_tickers for why that
    second condition is the whole safeguard. Skipped tickers are reported in
    the result but NOT written to the run log: the log is a record of attempts,
    and filling it with a decision not to attempt is how a log stops being
    read.

    And if the source says it is being asked too often, the pass stops there
    rather than working through the rest of the list. Retrying a throttled
    source is what extends the throttling, and the failures it produces are
    logged against tickers that have nothing wrong with them — which is how a
    typo in one symbol came to look like every symbol being broken.
    """
    active = provider or providers.get_provider()
    results: dict[str, str] = {}
    # Symbols that have failed every time and never once worked are not asked
    # for again. Read once per pass rather than per ticker: it is one small
    # query over the run log, and asking it inside the loop would put a query
    # between every pair of network calls.
    #
    # The set is recomputed each pass, so nothing has to un-suspend anything —
    # a symbol that starts working leaves it by itself, permanently.
    # The source told us to stop, and it has not been long enough yet. Answered
    # before the loop and without a single request: the whole point of a
    # cooldown is that it is the absence of traffic.
    cooldown = db.provider_cooldown_until(conn)
    if cooldown is not None:
        return {
            ticker: (
                "skipped: the data source is limiting requests from this installation. "
                f"Nothing will be collected until {cooldown:%H:%M} UTC."
            )
            for ticker in tickers
        }

    suspended = db.unresolvable_tickers(conn)
    failed_this_pass: list[str] = []
    for index, ticker in enumerate(tickers):
        if ticker.upper() in suspended:
            results[ticker] = (
                "skipped: no snapshot has ever been collected for this symbol after "
                f"{config.UNRESOLVABLE_AFTER_FAILURES} attempts. Nothing has been deleted; "
                "remove it from the watchlist, or fix the symbol and add it again."
            )
            continue
        started_at = _now_utc()
        rows_fetched: int | None = None
        oi_zero_fraction: float | None = None
        try:
            underlying_price, chain_df = active.fetch_ticker_snapshot(ticker)
            if chain_df.empty:
                raise ValueError("Empty option chain")
            rows_fetched = len(chain_df)
            oi_zero_fraction = _oi_zero_fraction(chain_df)
            if oi_zero_fraction > config.MAX_ZERO_OI_FRACTION:
                raise ValueError(
                    f"{oi_zero_fraction:.0%} of contracts have open_interest=0 "
                    f"(threshold {config.MAX_ZERO_OI_FRACTION:.0%}) — either the data "
                    "source returned a corrupted/incomplete snapshot, or the market is "
                    "closed right now (Yahoo Finance commonly reports open_interest=0 "
                    "across the whole chain outside regular trading hours); not saving. "
                    "Try again while the market is open."
                )
            db.insert_snapshot(
                conn, ticker, started_at, underlying_price, chain_df, source=active.name
            )
            db.log_run(
                conn, started_at, _now_utc(), ticker, "success",
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
                source=active.name,
            )
            # The moment just stored carries the source's volatility average,
            # because that half of the rollup is written in SQL. Ours is solved
            # from the price, so it lands here, one moment later — and it is
            # done from the collector rather than only from the worker so that
            # somebody collecting by hand with no worker running is not left
            # with the one number this product does not compute itself. The
            # newest pending moment IS the one just written; older ones are the
            # worker's job. This cannot raise — see app/iv_backfill.py.
            iv_backfill.backfill_ticker(conn, ticker, limit=1, source=active.name)
            results[ticker] = "success"
        except Exception as exc:
            message = _scrub(str(exc), active)
            # BEING THROTTLED IS NOT THIS TICKER'S FAULT, and continuing down
            # the watchlist is what makes it everybody's. Every remaining
            # symbol would fail the same way, each failure would be logged
            # against a symbol that is probably fine, and each request would
            # extend the block. So the pass stops here, the reason is recorded
            # in the ticker's own words rather than as whatever the library
            # raised, and nothing is asked of the source until the cooldown
            # expires.
            if providers.is_rate_limited(exc):
                until = db.start_provider_cooldown(conn)
                message = (
                    "the data source is limiting requests from this installation "
                    f"({message}). Collection is paused until {until:%H:%M} UTC."
                )
                db.log_run(
                    conn, started_at, _now_utc(), ticker, "failed", message,
                    rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
                    source=active.name,
                )
                results[ticker] = f"failed: {message}"
                for skipped in tickers[index + 1:]:
                    results[skipped] = (
                        "skipped: collection paused because the data source is limiting "
                        f"requests. Next attempt after {until:%H:%M} UTC."
                    )
                break
            db.log_run(
                conn, started_at, _now_utc(), ticker, "failed", message,
                rows_fetched=rows_fetched, oi_zero_fraction=oi_zero_fraction,
                source=active.name,
            )
            results[ticker] = f"failed: {message}"
            failed_this_pass.append(ticker)

    # The one moment worth telling somebody about is the moment a symbol stops
    # being retried — after that it is silent by design, and silence is what
    # people mistake for the tool being broken. Asked once, after the loop, and
    # only if something failed: it is a second query over the run log, and the
    # answer cannot change for a ticker that succeeded.
    if failed_this_pass:
        now_suspended = db.unresolvable_tickers(conn)
        for ticker in failed_this_pass:
            if ticker.upper() in now_suspended and ticker.upper() not in suspended:
                results[ticker] += (
                    f" — this symbol has now failed {config.UNRESOLVABLE_AFTER_FAILURES} times "
                    "without ever collecting anything, so it will not be requested again. "
                    "Nothing has been deleted."
                )
    return results


def _scrub(message: str, provider: providers.DataProvider) -> str:
    """Strips the provider's credential out of an error message before it is
    stored.

    Yahoo has no token, so today this does nothing — it is here because the
    moment someone adds an authenticated provider it stops doing nothing.
    Collection failures are written to collection_runs and rendered verbatim in
    the interface, and HTTP client errors routinely quote the request URL. A
    key that leaks into that log is readable by anyone who can open the page.
    """
    secret = getattr(provider, "token", None)
    if not secret:
        return message
    return message.replace(secret, "***")
