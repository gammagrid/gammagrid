"""The contract every data provider implements.

collector.py used to do two unrelated jobs at once: it knew *how* to talk to
yfinance, and it knew *what to do* with the result. Pulling those apart is what
makes a second source possible at all — everything that isn't source-specific
(retry policy, the zero-open-interest quality gate, run logging, per-ticker
failure isolation) stays in collector.py and works over any provider, while a
provider only has to answer one question: "give me this ticker's chain".

Two rules hold the split in place, and both are worth keeping if you write your
own provider: nothing outside this package makes network requests, and nothing
inside it knows the database exists.

The retry helper lives here rather than in collector.py deliberately. Providers
hit the network at different granularities — yfinance makes one request per
expiry, so retrying a single flaky expiry is far cheaper than redoing a whole
chain, whereas a paginated client fetches the chain as one logical operation.
Wrapping retries at the collector level would force every provider into the
coarser of the two.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

from app import config

# The flat shape every provider must return, in this order. db.insert_snapshot
# consumes exactly these columns — a provider that returns anything else is
# broken, however sensible its own native format looked.
CHAIN_COLUMNS = [
    "expiry",
    "strike",
    "option_type",
    "last_price",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "implied_volatility",
    "in_the_money",
    # Provider-supplied greeks. A provider that doesn't serve them leaves these
    # as None — they are stored rather than recomputed, because they come from
    # the provider's own model and cannot be reconstructed afterwards. Yahoo
    # serves none, so for the default setup these are always empty and the
    # reader computes greeks from implied volatility instead.
    #
    # Nothing else belongs here: rho, vanna and charm are not served by any
    # provider we know of and are always computed at read time.
    "delta",
    "gamma",
    "theta",
    "vega",
]

# Columns a provider MAY add, and which nothing breaks without. Kept apart from
# CHAIN_COLUMNS on purpose: that list is a contract third-party providers were
# written against, and lengthening it would turn every one of them into a
# broken provider overnight.
#
# `contract_symbol` — the contract's own identifier, as the exchange writes it
# (OCC: root, expiry, type, strike). Two contracts can legitimately share a
# strike, an expiry and a type: after a split or a special dividend an adjusted
# series lives alongside the standard one, and only the symbol's root tells
# them apart. Without it db.insert_snapshot has no way to choose between them
# and the whole chain fails to store. See the dedupe there for what happens
# when a provider does not supply this.
CHAIN_OPTIONAL_COLUMNS = ["contract_symbol"]


@dataclass(frozen=True)
class ProviderStatus:
    """Result of check_access(). `ok=False` with a human-readable `message` —
    the message is shown to the user verbatim, so it has to explain what to do,
    not what HTTP code came back."""

    ok: bool
    message: str


@runtime_checkable
class DataProvider(Protocol):
    """`name` is not cosmetic: it is written into option_snapshots.source and
    decides which rows a chart may mix. Two providers derive implied volatility
    with different models, so the same contract on the same day legitimately
    differs between them — splicing two sources into one line draws a jump that
    never happened on the market. Changing an existing provider's name would
    silently orphan everything it has already collected.
    """

    name: str

    # Which source the underlying's price history actually comes from. Usually
    # the provider itself, but not always: an options-only data vendor has no
    # stock prices and has to delegate. Kept explicit so the interface can say
    # where each number came from instead of letting the user assume everything
    # on screen arrived from one place.
    price_history_source: str

    # Whether this provider authenticates at all. Anything that renders a
    # settings form reads this: a token field shown for a provider that has
    # nothing to authenticate invites pasting a credential that is then stored
    # having never been checked against the API it belongs to.
    requires_token: bool

    def fetch_ticker_snapshot(self, ticker: str) -> tuple[float, pd.DataFrame]:
        """Returns (underlying price, full option chain across all expiries)
        with exactly CHAIN_COLUMNS."""
        ...

    def fetch_price_history(self, ticker: str, period: str) -> pd.DataFrame:
        """Daily close history of the underlying, single `close` column."""
        ...

    def check_access(self) -> ProviderStatus:
        """One cheap live request answering "can this provider work right now".
        Must distinguish "credentials rejected" from "this plan does not include
        this endpoint" — those need different actions from whoever reads it, and
        a single "request failed" makes them indistinguishable.
        """
        ...


# What "the source is refusing to talk to you" looks like across libraries.
# Matched on the exception's TYPE first, because that is the reliable half —
# yfinance raises YFRateLimitError, and a type name cannot be confused with a
# number that happened to appear in a message.
#
# The text markers are the fallback, and they are deliberately narrow. An
# earlier draft matched a bare "999" (Yahoo's historical rate-limit code) and
# would have matched any message quoting a strike of 999 or a chain of 999
# rows. A misclassified failure here is expensive: it stops the whole pass.
_RATE_LIMIT_MARKERS = (
    "too many requests",
    "rate limit",
    "rate-limit",
    "http error 429",
    "error 429",
    "status 429",
    "http error 999",
    "error 999",
)


def is_rate_limited(error: BaseException | str) -> bool:
    """Is this failure the source telling us to slow down?

    Separated from every other failure because the correct response is the
    opposite one. A network blip deserves a retry; being throttled deserves
    silence, and retrying is precisely what extends it.
    """
    if isinstance(error, BaseException) and "ratelimit" in type(error).__name__.lower():
        return True
    text = str(error).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def with_retry(fn, *args, **kwargs):
    """Exponential backoff around one network call. Deliberately catches broad
    Exception: data-source libraries raise assorted connection, parsing and
    rate-limit types.

    WITH ONE EXCEPTION, and it is the reason this docstring changed. Retrying a
    throttled request is not neutral — it is the thing that keeps the throttle
    in place, and this helper did it three times per call, for every expiry, for
    every ticker in the watchlist. One mistyped symbol was enough to burn half
    a dozen requests before the first real ticker was reached, after which the
    source refused the valid ones too and the run log blamed them. So a
    rate-limit failure is raised immediately and the decision about what to do
    next is taken a level up, where the whole pass can be stopped instead of
    one call.
    """
    last_error: Exception | None = None
    for attempt in range(config.MAX_FETCH_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_rate_limited(exc):
                raise
            last_error = exc
            if attempt < config.MAX_FETCH_RETRIES - 1:
                time.sleep(config.BACKOFF_BASE_SECONDS * (2**attempt))
    raise last_error
