"""What to offer somebody whose ticker will never work.

A refusal that names the reason and stops there leaves the person exactly where
they were. Every symbol seen failing forever in a real watchlist had an answer
nobody told them about:

    APPL    -> AAPL     a typo
    NVIDIA  -> NVDA     the company's name, not its symbol
    NASDAQ  -> QQQ      an index name; the tradeable proxy is the ETF
    BTCUSD  -> IBIT     a currency pair; US options exist on the ETF
    XAUUSD  -> GLD      the same, for gold

Two mechanisms, because these are two different mistakes. A typo is a symbol
that nearly matches something real, and the fix is spelling. An instrument that
simply has no US-listed options is not a spelling problem at all — the person
wants exposure to bitcoin, and the answer is a different instrument. Only the
second can be written down in advance; the first has to be computed against
what actually exists.

Nothing here talks to a network or a database. It is given the symbol and the
list of things already being collected, and returns a string.
"""

from __future__ import annotations

import difflib

# Instruments people reasonably ask for that have no options on the default
# source, mapped to the tradeable thing that tracks them. Written out rather
# than guessed at, because no amount of string similarity gets from BTCUSD to
# IBIT.
#
# The keys are what people type: the pair, the bare asset, the index's popular
# name. The index symbols (SPX, NDX, RUT) are here for a reason worth stating —
# they are real, tradeable option products on US exchanges, but Yahoo Finance,
# the default source, does not serve their chains. Someone whose data comes
# from a licensed feed can collect them; with the default setup the honest
# answer is the ETF.
NO_US_OPTIONS = {
    "BTC": "IBIT",
    "BTCUSD": "IBIT",
    "XBT": "IBIT",
    "BITCOIN": "IBIT",
    "ETH": "ETHA",
    "ETHUSD": "ETHA",
    "ETHEREUM": "ETHA",
    "XAU": "GLD",
    "XAUUSD": "GLD",
    "GOLD": "GLD",
    "XAG": "SLV",
    "XAGUSD": "SLV",
    "SILVER": "SLV",
    "NASDAQ": "QQQ",
    "NASDAQ100": "QQQ",
    "NDX": "QQQ",
    "SP500": "SPY",
    "SPX": "SPY",
    "SPX500": "SPY",
    "DOW": "DIA",
    "DJIA": "DIA",
    "DJX": "DIA",
    "RUSSELL": "IWM",
    "RUSSELL2000": "IWM",
    "RUT": "IWM",
    "OIL": "USO",
    "WTI": "USO",
    "EURUSD": None,  # no equivalent worth recommending; say so rather than invent
    "GBPUSD": None,
    "USDJPY": None,
}

# How alike two symbols must be before a typo is worth suggesting. 0.75 keeps
# APPL->AAPL while refusing to connect unrelated four-letter symbols, of which
# the market has thousands. Deliberately conservative: a wrong suggestion is
# worse than none, because it is confidently wrong.
_TYPO_CUTOFF = 0.75


def suggest(ticker: str, known: list[str] | set[str] | None = None) -> str | None:
    """A better symbol to try, or None when there is nothing honest to offer.

    `known` is what this installation already collects. Passing it is what
    makes the typo half work at all — a suggestion has to be a symbol that
    exists here, not merely a plausible string. On a fresh install the list is
    empty and only the table above can help, which is the correct degradation.
    """
    candidate = (ticker or "").strip().upper()
    if not candidate:
        return None

    if candidate in NO_US_OPTIONS:
        return NO_US_OPTIONS[candidate]

    # The company's name rather than its symbol: NVIDIA, TESLA, APPLE. Long
    # words are never real tickers, so a near match is not what helps here —
    # a prefix is.
    pool = sorted({t.upper() for t in (known or [])})
    if len(candidate) > 5:
        for symbol in pool:
            if candidate.startswith(symbol) and len(symbol) >= 3:
                return symbol

    matches = difflib.get_close_matches(candidate, pool, n=1, cutoff=_TYPO_CUTOFF)
    return matches[0] if matches and matches[0] != candidate else None


def source_refusal(ticker: str, substitute: str | None, provider: str = "the current data source") -> str:
    """What to say about a symbol the data source will not serve.

    SEPARATE FROM `refusal` BECAUSE THE REASON IS DIFFERENT, and the difference
    is the whole point. "SPX has no options" is false, the person typing SPX
    knows it is false, and a message somebody knows to be wrong devalues every
    other message this product prints. What is true is narrower and more
    useful: the symbol is real, its options are real, and the limitation is on
    our side of the line.

    THE SUBSTITUTE IS PASSED IN RATHER THAN LOOKED UP HERE. Which symbols a
    source refuses is a fact about the SOURCE — a licensed feed serves SPX,
    Yahoo has no endpoint for it — so the provider carries the map
    (`unsupported_symbols`) for the same reason it carries its own interval
    floor. Keeping it here would mean this module had to know which provider is
    active, and that is exactly the shape worth avoiding.
    """
    candidate = (ticker or "").strip().upper()
    head = (
        f"“{candidate}” is a real symbol with listed options, but {provider} "
        "does not serve its option chain, so there would be nothing to collect."
    )
    if substitute:
        return (
            f"{head} **{substitute}** tracks the same underlying and is fully "
            "supported — the analytics carry over."
        )
    return (
        f"{head} There is no honest substitute: an ETF on VIX futures is a "
        "different instrument with its own term structure, not a proxy for the index."
    )


def refusal(ticker: str, known: list[str] | set[str] | None = None) -> str:
    """The whole message a person sees, suggestion included when there is one.

    Written here rather than at the call site so the wording stays in one
    place: this text is the entire experience of getting it wrong.
    """
    candidate = (ticker or "").strip().upper()
    hint = suggest(candidate, known)
    head = f"“{candidate}” has no options to collect on the current data source."
    if hint:
        return f"{head} Did you mean **{hint}**?"
    if candidate in NO_US_OPTIONS:
        return (
            f"{head} Currency pairs have no listed options at all — this product "
            "covers what trades on options exchanges."
        )
    return f"{head} Check the symbol as it trades in the US."
