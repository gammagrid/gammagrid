"""What to offer somebody whose ticker will never work.

A refusal that names the reason and stops there leaves the person exactly
where they were. Every symbol seen failing forever in a real watchlist had an answer
nobody told them about:

    APPL      -> AAPL     a typo
    NVIDIA    -> NVDA     the company's name, not its symbol
    NASDAQ    -> QQQ      an index name; the tradeable proxy is the ETF
    BTCUSD    -> IBIT     a currency pair; US options exist on the ETF
    XAUUSD    -> GLD      the same, for gold
    BTCUSDT   -> IBIT     the same pair as an exchange writes it
    SAP.DE    -> SAP      a non-US listing of a company that also trades here

ONE ENTRY POINT, AND THAT IS THE POINT OF THE MODULE. These kinds used to be
answered by three separate mechanisms living in three places — this module's
typo search, this module's instrument map, and the provider's own refusal list
— which fired at different moments and wrote in different words. Somebody who
typed SPX got one sentence, NASDAQ another, APPL a third, and SAP.DE nothing at
all, while the help text under the very same box named ADRs as the answer. As
four defects it was arguable; as one product it was four answers to one
question: "I want this, give me the thing you have".

So `propose` is the single answer, and it asks in the order the reasons rule
each other out:

    1. the source declared it will not serve this symbol   (a fact about US)
    2. the instrument has no US-listed options at all      (a fact about it)
    3. it is a non-US listing of something we do have      (a fact about form)
    4. it is nearly a symbol we already collect            (a guess)

Only the last is a guess, and it comes last for that reason. Every message
built here ends the same way — the substitute, then `_CARRY_OVER` — so the
answer reads as one product speaking rather than three subsystems.
"""

from __future__ import annotations

import dataclasses
import difflib

# Instruments people reasonably ask for that have no US-listed options, mapped
# to the tradeable thing that tracks them. Written out rather than guessed at,
# because "close spelling" cannot get from BTCUSD to IBIT.
#
# The keys are what people type: the pair, the bare asset, the index's popular
# name. Values must be symbols with real US options — the suites check every
# one of them against Cboe's own directory, so a fund that closes cannot rot
# here unnoticed.
#
# THE PAIRS ARE STORED IN ONE FORM ONLY. `BTCUSDT`, `BTC-USD` and `BTC/USD` are
# the same request typed by three terminals, and putting each spelling in the
# map would mean the next spelling is missing too. `normalize_pair` folds them
# before the lookup instead. `BTCUSDT` is the form people copy most
# often out of an exchange, and the one that used to answer nothing.
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
    "SP500": "SPY",
    "SPX500": "SPY",
    "DOW": "DIA",
    "DJIA": "DIA",
    "RUSSELL": "IWM",
    "RUSSELL2000": "IWM",
    "OIL": "USO",
    "WTI": "USO",
    "EURUSD": None,  # no equivalent worth recommending; say so rather than invent
    "GBPUSD": None,
    "USDJPY": None,
}

# Quote currencies that get written differently by different terminals. USDT
# and USDC are stablecoins, not dollars — but somebody typing BTCUSDT wants
# bitcoin, and pretending not to understand that is the product being pedantic
# at its own front door.
_QUOTE_ALIASES = ("USDT", "USDC")
_PAIR_SEPARATORS = str.maketrans("", "", "-/_. ")

# Exchange suffixes Yahoo-style symbols carry, and the seven are exactly the
# ones people were measured typing. Deliberately a closed list rather than
# "anything after a dot": US symbols have dots too — BRK.B, BF.B — and treating
# those as foreign listings would refuse a share class we collect perfectly
# well.
EXCHANGE_SUFFIXES = ("DE", "AS", "L", "PA", "MX", "HK", "SW")

# Foreign listings whose US symbol is NOT what is left after dropping the
# suffix. Everything else needs no map at all: SAP.DE, ASML.AS, SHEL.L, TSLA.MX
# resolve by stripping and looking the base up in the catalogue.
#
# CHECKED BEFORE THE STRIP, not after, and SAN.PA is why. Stripped it becomes
# SAN — Banco Santander, a real symbol we collect, and the wrong company
# entirely: SAN.PA is Sanofi, whose US listing is SNY. A suggestion that
# confidently names a different business is worse than none.
#
# Every value is verified against Cboe's directory by the suites. An ADR that
# trades only over the counter — Volkswagen, Roche, Nestlé, Tencent — has no
# listed options and is therefore absent here on purpose: we would be sending
# somebody to a symbol we cannot collect either.
FOREIGN_LISTINGS = {
    "9988.HK": "BABA",
    "9618.HK": "JD",
    "9888.HK": "BIDU",
    "9866.HK": "NIO",
    "9626.HK": "BILI",
    "9868.HK": "XPEV",
    "9961.HK": "TCOM",
    "2015.HK": "LI",
    "2423.HK": "BEKE",
    "HSBA.L": "HSBC",
    "ULVR.L": "UL",
    "BATS.L": "BTI",
    "SAN.PA": "SNY",
    "INGA.AS": "ING",
    "NOVN.SW": "NVS",
    "UBSG.SW": "UBS",
}

# How alike two symbols must be before a typo is worth suggesting. 0.75 keeps
# APPL->AAPL and NVDA->NVDIA while refusing to connect unrelated four-letter
# symbols, of which the market has thousands. Deliberately conservative: a
# wrong suggestion is worse than none, because it is confidently wrong.
_TYPO_CUTOFF = 0.75

# The tail every substitute sentence ends with, in one place so that the
# product speaks with one voice. Changing it here changes it in
# all four answers.
_CARRY_OVER = "is fully supported — the analytics carry over."


@dataclasses.dataclass(frozen=True)
class Proposal:
    """One answer to "I typed X and it will not work".

    `reason` is what the caller branches on and the message is what the person
    reads; both come from the same decision, which is the point of returning an
    object rather than a string. `blocking` says whether the symbol can still be
    attempted: a source that has declared it will not serve a symbol is final,
    while everything else here is our reading of what somebody meant and must
    not take the button away from them.
    """

    typed: str
    substitute: str | None
    reason: str  # "source" | "instrument" | "foreign" | "typo"
    message: str
    blocking: bool = False


def dropdown_aliases() -> dict[str, str]:
    """{what somebody types: what we would collect instead}, for the search box.

    WHY THE ANSWER HAS TO REACH THE LIST AND NOT ONLY THE MESSAGE. `propose`
    answers after the symbol is committed — the person types BTCUSDT, presses
    enter, reads that IBIT is the thing we have, and then has to type IBIT.
    Three steps for an answer we held all along. In the list it is one: type
    BTCUSDT, see `BTCUSDT → IBIT`, choose it.

    THE ARROW IS IN THE LABEL ON PURPOSE. Silently resolving BTCUSDT to IBIT
    would be the box swapping somebody's input for another symbol without
    saying so — right up to the moment they wonder why their watchlist has a
    ticker they never typed.

    WHAT CANNOT BE IN HERE, AND IT IS NOT AN OVERSIGHT. The box filters in the
    BROWSER, so every spelling it can match has to have been sent to the browser
    first. That bounds this to what can be enumerated: the maps above. Foreign
    listings resolved by dropping a suffix cannot be — 5,300 symbols by seven
    suffixes is 37,000 rows against a list that costs 178 KB at 5,300 — so
    `SAP.DE` is still answered by `propose` a keystroke later. Enumerable
    answers arrive in the list; generated ones arrive as a sentence.
    """
    aliases: dict[str, str] = {}
    for typed, substitute in NO_US_OPTIONS.items():
        if not substitute:
            continue
        aliases[typed] = substitute
        # The same pair as three terminals write it. Each spelling needs its own
        # row: the browser matches the text it was given, so a row reading
        # "BTCUSD" is not found by somebody typing "BTCUSDT".
        if typed.endswith("USD") and len(typed) > 3:
            base = typed[:-3]
            for spelling in (f"{base}USDT", f"{base}USDC", f"{base}-USD", f"{base}/USD"):
                aliases[spelling] = substitute
    aliases.update(FOREIGN_LISTINGS)
    return aliases


def normalize_pair(typed: str) -> str:
    """`BTC-USD`, `BTC/USD`, `BTCUSDT` -> `BTCUSD`. Anything else, untouched.

    Only ever used to look up `NO_US_OPTIONS`; the symbol the person typed is
    what gets quoted back to them, because correcting somebody's spelling while
    answering their question is two conversations at once.
    """
    candidate = (typed or "").strip().upper().translate(_PAIR_SEPARATORS)
    for alias in _QUOTE_ALIASES:
        if candidate.endswith(alias) and len(candidate) > len(alias):
            return candidate[: -len(alias)] + "USD"
    return candidate


def adr_for(typed: str, in_catalogue=None) -> str | None:
    """The US symbol behind a non-US listing, or None.

    `in_catalogue` answers "is this a symbol with listed options" — in the
    product it is the Cboe directory we already hold, which is why this needs no
    map for the ordinary case. Without it only the exception map can answer,
    which is the honest degradation: guessing that SAP.DE means SAP is only
    safe because something independent says SAP exists.
    """
    candidate = (typed or "").strip().upper()
    if candidate in FOREIGN_LISTINGS:
        return FOREIGN_LISTINGS[candidate]
    base, _, suffix = candidate.rpartition(".")
    if not base or suffix not in EXCHANGE_SUFFIXES:
        return None
    if in_catalogue is not None and in_catalogue(base):
        return base
    return None


def is_foreign_listing(typed: str) -> bool:
    """Does this look like a listing on an exchange we do not cover at all.

    Separated from `adr_for` because the two answers are independent: SIE.DE is
    plainly a German listing and has no US equivalent worth naming, and saying
    "that is not a US listing" is still a better answer than silence.
    """
    candidate = (typed or "").strip().upper()
    if candidate in FOREIGN_LISTINGS:
        return True
    base, _, suffix = candidate.rpartition(".")
    return bool(base) and suffix in EXCHANGE_SUFFIXES


def suggest(ticker: str, known: list[str] | set[str] | None = None) -> str | None:
    """A better symbol to try, or None when there is nothing honest to offer.

    `known` is what the product already collects. Passing it is what makes the
    typo half work at all — the suggestion has to be a symbol that exists here,
    not merely a plausible string.
    """
    candidate = (ticker or "").strip().upper()
    if not candidate:
        return None

    folded = normalize_pair(candidate)
    if folded in NO_US_OPTIONS:
        return NO_US_OPTIONS[folded]

    # The company's name rather than its symbol: NVIDIA, TESLA, APPLE. Long
    # words are never real tickers, so a near match against a known symbol is
    # not what helps here — a prefix is.
    pool = sorted({t.upper() for t in (known or [])})
    if len(candidate) > 5:
        for symbol in pool:
            if candidate.startswith(symbol) and len(symbol) >= 3:
                return symbol

    matches = difflib.get_close_matches(candidate, pool, n=1, cutoff=_TYPO_CUTOFF)
    return matches[0] if matches and matches[0] != candidate else None


def source_refusal(ticker: str, substitute: str | None, provider: str = "the current data source") -> str:
    """What to say about a symbol the data source will not serve.

    Separated from `refusal` because the reason is different and the difference
    matters to the reader: this symbol is real, its options are real, and the
    limitation is ours.

    THE SUBSTITUTE IS PASSED IN RATHER THAN LOOKED UP HERE. Which symbols a
    source refuses is a fact about the SOURCE — a licensed feed serves SPX,
    Yahoo has no endpoint for it at all — so the provider carries the map
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
        return f"{head} **{substitute}** tracks the same underlying and {_CARRY_OVER}"
    return (
        f"{head} There is no honest substitute: an ETF on VIX futures is a "
        "different instrument with its own term structure, not a proxy for the index."
    )


def instrument_refusal(ticker: str, substitute: str | None) -> str:
    """A pair, a metal or an index name — something that is not a US-listed
    security at all, so no spelling of it will ever collect."""
    candidate = (ticker or "").strip().upper()
    head = f"“{candidate}” has no US-listed options of its own."
    if substitute:
        return f"{head} **{substitute}** tracks the same underlying and {_CARRY_OVER}"
    return (
        f"{head} Currency pairs have no US-listed options at all — this product "
        "covers what trades on US options exchanges."
    )


def foreign_refusal(ticker: str, substitute: str | None) -> str:
    """A listing on a non-US exchange, answered before anybody waits for it.

    THE PRODUCT ALREADY KNEW THIS ANSWER AND PRINTS IT IN THE HELP TEXT — "most
    large non-US companies also trade in the US as ADRs" — while refusing to
    apply it to the symbol in the box. Said here instead, at the moment the
    symbol is typed and without asking the source, because `SAP.DE` passes the
    shape check and the network round trip only arrives at the same no, seconds
    later.
    """
    candidate = (ticker or "").strip().upper()
    head = (
        f"“{candidate}” is a listing on a non-US exchange, and this product "
        "covers options listed in the US."
    )
    if substitute:
        return (
            f"{head} **{substitute}** is the same company's US listing and {_CARRY_OVER}"
        )
    return (
        f"{head} Most large non-US companies also trade here as ADRs — try the "
        "US symbol if there is one."
    )


def typo_refusal(ticker: str, substitute: str) -> str:
    """The near miss. Phrased as a question because it is the only one of the
    four that is a guess about what somebody meant."""
    candidate = (ticker or "").strip().upper()
    return (
        f"“{candidate}” has no listed options, so there is nothing to collect "
        f"for it. Did you mean **{substitute}**?"
    )


def propose(
    ticker: str,
    known: list[str] | set[str] | None = None,
    *,
    in_catalogue=None,
    unsupported: dict[str, str | None] | None = None,
    provider: str = "our data source",
) -> Proposal | None:
    """The one answer to "what should I type instead of X", or None.

    The order is the order the reasons rule each other out — see the module
    docstring. Two guards are worth naming because they are what keeps the
    thing quiet enough to run on every keystroke:

    A SYMBOL IN THE CATALOGUE IS NEVER "A TYPO". Cboe's directory is the
    statement that a symbol has listed options; correcting AAPX to AAPL when
    AAPX is a real security somebody deliberately typed would be the product
    arguing with its own data. So when a catalogue is supplied and knows the
    symbol, only the source's own refusal can still speak.

    NOTHING IS SAID ABOUT A SYMBOL WE ALREADY COLLECT. That is the same guard
    seen from the other side, and it is why `known` is consulted before the
    fuzzy match rather than after.
    """
    candidate = (ticker or "").strip().upper()
    if not candidate:
        return None

    refused = unsupported or {}
    if candidate in refused:
        substitute = refused[candidate]
        return Proposal(
            candidate, substitute, "source",
            source_refusal(candidate, substitute, provider), blocking=True,
        )

    catalogued = in_catalogue is not None and in_catalogue(candidate)

    folded = normalize_pair(candidate)
    if folded in NO_US_OPTIONS and not catalogued:
        substitute = NO_US_OPTIONS[folded]
        return Proposal(
            candidate, substitute, "instrument", instrument_refusal(candidate, substitute)
        )

    if not catalogued and is_foreign_listing(candidate):
        substitute = adr_for(candidate, in_catalogue)
        return Proposal(
            candidate, substitute, "foreign", foreign_refusal(candidate, substitute)
        )

    if catalogued:
        # Real symbol, source has not objected: there is nothing to propose,
        # and proposing anything would be noise on a valid choice.
        return None

    hint = suggest(candidate, known)
    if hint:
        return Proposal(candidate, hint, "typo", typo_refusal(candidate, hint))
    return None


def refusal(
    ticker: str, known: list[str] | set[str] | None = None, in_catalogue=None
) -> str:
    """The whole message a person sees after the source has said no.

    Delegates to `propose` so that the words are decided in one place: this is
    the same question asked one moment later, when the source has confirmed
    what the front door already suspected.
    """
    candidate = (ticker or "").strip().upper()
    answer = propose(candidate, known, in_catalogue=in_catalogue)
    if answer is not None:
        return answer.message
    return (
        f"“{candidate}” has no listed options, so there is nothing to collect "
        "for it. Check the symbol as it trades in the US."
    )
