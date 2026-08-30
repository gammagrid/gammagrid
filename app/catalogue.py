"""The directory of symbols somebody may add, and how the search box is filled.

WHY THIS EXISTS. `Add ticker` used to be an empty text field, and everything a
person is likely to type has the shape of a ticker: `APPL`, `NVIDIA`, `NASDAQ`
and `BTCUSD` were all accepted, added to a watchlist, collected against, and
then failed forever. A field you can only type into is also a field you can
only be wrong in — it cannot tell you that the thing you want exists under
another name, because it does not know what exists.

A directory turns that around. It is the one statement this product holds that
a string is a real security with listed options, which is what makes three
different answers possible: it distinguishes "AAPX, deliberately typed" from
"APPL, mistyped"; it resolves `SAP.DE` to `SAP` without a map of European
companies; and it lets somebody search for `Apple` when they do not know the
symbol at all.

WHAT IT IS NOT. It is not a statement that Yahoo will serve a chain for the
symbol — Cboe lists SPX, XSP, NDX and RUT, and Yahoo has no chain endpoint for
any of them. "Has listed options" and "our source will serve it" are different
questions; the provider answers the second one (`unsupported_symbols`, and the
live check behind the Add button).

DOWNLOADED LAZILY, THEN WEEKLY, AND NEVER BLOCKING. The first process to find
the table empty fills it; after that the worker refreshes it on a weekly
cadence. A directory of 5,300 symbols changes by a handful of rows a week, so
asking a third party for it every night is a request this product would be
signing somebody up for without a reason. Every failure here is survivable by
construction: no catalogue means the box is a plain text field again, which is
what it was before this file existed.

NOTHING IS SENT. This downloads a public file and stores it locally. No part of
what you collect, watch or type leaves the machine — see SECURITY.md, which
lists this as one of the three outbound calls the product makes.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import urllib.request

import psycopg

from app import config, suggestions

log = logging.getLogger(__name__)

# Cboe publishes the directory of every symbol with listed US options as a CSV.
# Public, no account, no key.
CBOE_URL = "https://www.cboe.com/us/options/symboldir/equity_index_options/?download=csv"

# Cboe redirects the download and rejects the default urllib agent. Named here
# rather than inlined so that the day it stops working, the reason is one line
# away from the request that failed.
_USER_AGENT = f"Mozilla/5.0 (compatible; gammagrid/{config.APP_VERSION})"

_REFRESHED_KEY = "catalogue_refreshed_at"


def fetch_directory(url: str = CBOE_URL, timeout: int = 60) -> list[tuple[str, str]]:
    """[(symbol, company)] straight from Cboe.

    Raises on anything it cannot parse, and every caller treats that as "keep
    what we already have". A half-parsed file quietly emptying the table would
    take the search box down; a failed download costs a week of staleness on a
    file that changes by a handful of rows.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read().decode("utf-8-sig", errors="replace")
    return parse_directory(payload)


def parse_directory(payload: str) -> list[tuple[str, str]]:
    """The parse, separated from the download so it can be checked on the real
    header without a network call — and the real header is what breaks it."""
    reader = csv.reader(io.StringIO(payload))
    header = next(reader, None)
    if not header:
        raise ValueError("Cboe directory has no header row")
    # THE HEADER IS `Company Name, Stock Symbol, ...` — with a space after each
    # comma, so the raw field names carry a leading blank. Read through
    # DictReader without stripping, the file parses cleanly into zero symbols,
    # which is the most expensive shape of failure: nothing raises, the
    # catalogue empties, and the search box goes quiet.
    columns = {name.strip().lower(): index for index, name in enumerate(header)}
    try:
        symbol_at, company_at = columns["stock symbol"], columns["company name"]
    except KeyError as missing:
        raise ValueError(
            f"Cboe directory is missing column {missing}; header was {header}"
        ) from None

    rows = []
    for row in reader:
        if len(row) <= max(symbol_at, company_at):
            continue
        symbol = row[symbol_at].strip().upper()
        company = row[company_at].strip()
        if symbol:
            rows.append((symbol, company))
    if not rows:
        raise ValueError("Cboe directory parsed to zero symbols")
    return rows


def load(conn: psycopg.Connection, rows: list[tuple[str, str]]) -> int:
    """Merge one download into the catalogue. Returns how many symbols it saw.

    A SYMBOL THAT HAS DISAPPEARED FROM THE FILE IS NOT DELETED. Delisting is an
    event rather than the absence of a row, and somebody whose watchlist holds
    a delisted symbol still needs the search box to be able to say something
    about it. Nothing here is large enough for the leftovers to matter.
    """
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS cboe_load (symbol TEXT, company TEXT) "
            "ON COMMIT DROP"
        )
        with cur.copy("COPY cboe_load (symbol, company) FROM STDIN") as copy:
            for symbol, company in rows:
                copy.write_row((symbol, company))
        # DISTINCT ON, because the file is somebody else's and has repeated a
        # symbol before. Without it the whole statement fails with a cardinality
        # violation and the catalogue is left at whatever it held.
        cur.execute(
            """INSERT INTO option_symbols (symbol, company, last_seen_at)
               SELECT DISTINCT ON (symbol) symbol, company, now()
               FROM cboe_load ORDER BY symbol
               ON CONFLICT (symbol) DO UPDATE
               SET company = EXCLUDED.company, last_seen_at = EXCLUDED.last_seen_at"""
        )
    return len(rows)


def is_empty(conn: psycopg.Connection) -> bool:
    return not conn.execute("SELECT EXISTS (SELECT 1 FROM option_symbols)").fetchone()[0]


def refresh_if_due(conn: psycopg.Connection, force: bool = False) -> int:
    """Download the directory if it is missing or old. Returns symbols loaded.

    THE MARKER IS WRITTEN WHATEVER HAPPENS, and that is the difference between
    a weekly refresh and a request on every worker cycle. A download that fails
    against a catalogue we already hold must not be retried in fifteen minutes
    — the failure is nearly always the third party being unreachable, and
    hammering it is both rude and useless. An EMPTY catalogue is the exception:
    there the box does not work at all, so the next cycle tries again.

    Returns 0 for "nothing to do" and for "it did not work", because the
    caller's behaviour is the same either way: this is a convenience, and no
    part of collecting or drawing depends on it.
    """
    if not force and not is_empty(conn) and not _is_due(conn):
        return 0
    try:
        rows = fetch_directory()
    except Exception:  # noqa: BLE001 — a directory we could not fetch is not an error
        log.warning(
            "Could not download the symbol directory from Cboe. The Add-ticker box "
            "keeps whatever it already had — on a fresh install that means typing "
            "the symbol yourself, which still works. Trying again later.",
            exc_info=True,
        )
        if not is_empty(conn):
            _mark_refreshed(conn)
        return 0
    loaded = load(conn, rows)
    _mark_refreshed(conn)
    log.info("Symbol directory refreshed: %s symbols.", loaded)
    return loaded


def _is_due(conn: psycopg.Connection) -> bool:
    from app import db

    stamp = db.get_setting(conn, _REFRESHED_KEY)
    if not stamp:
        return True
    try:
        last = dt.date.fromisoformat(stamp)
    except ValueError:
        return True
    return (dt.date.today() - last).days >= config.CATALOGUE_REFRESH_DAYS


def _mark_refreshed(conn: psycopg.Connection) -> None:
    from app import db

    db.set_setting(conn, _REFRESHED_KEY, dt.date.today().isoformat())


def symbols(conn: psycopg.Connection) -> set[str]:
    """Every symbol in the directory, for "is this a real listed security".

    A set rather than a query per question: the caller asks it once per
    keystroke, and the whole catalogue is already being read for the box beside
    it.
    """
    return {row[0] for row in conn.execute("SELECT symbol FROM option_symbols").fetchall()}


def options(conn: psycopg.Connection, watched: list[str] | None = None) -> list[tuple[str, str]]:
    """The whole catalogue as (symbol, label), best guess first.

    FOR A BOX THAT FILTERS IN THE BROWSER. Handing the whole list to the widget
    is what makes it filter on every keystroke and clear the moment the box is
    emptied. A server-side search needs a round trip, and in Streamlit a text
    field's value only arrives when it loses focus — so suggestions appeared
    after a click somewhere else, or not at all.

    The cost is that the ORDER has to be decided without knowing the query, and
    the order is most of what makes this useful: "APPLE" matches AAPL, APLE and
    AAPX, and only one of them is Apple.

    THE ORDER IS THE NUMBER OF WORDS IN THE COMPANY NAME, chosen by measurement
    rather than taste. Three candidates against seven real queries:

        by symbol length     apple ✓  tesla ✗ (TSL)   nasdaq ✗ (IQQ)
        by name length       apple ✓  tesla ✓         nasdaq ✗ (NDAQ)
        by words in name     apple ✓  tesla ✓         nasdaq ✓  — and the other four

    It works because it is a real signal rather than a coincidence: a primary
    listing is named "TESLA INC", and the products derived from it are named
    "TESLA 1X SHORT DAILY ETF". Fewer words means closer to the thing itself.
    That is the honest substitute for a liquidity ranking, which the directory
    does not carry — it has no turnover and no market cap.

    Ahead of all of it: the symbols this installation already watches. What
    somebody actually follows beats any guess made from a name.
    """
    watched = [t.upper() for t in (watched or [])]
    rows = conn.execute(
        """SELECT symbol, company FROM option_symbols
           ORDER BY (symbol = ANY(%(watched)s)) DESC,
                    array_length(string_to_array(COALESCE(company, ''), ' '), 1) NULLS FIRST,
                    length(symbol),
                    symbol""",
        {"watched": watched},
    ).fetchall()

    labels = []
    for symbol, company in rows:
        # The company name is what makes a list of four-letter symbols legible,
        # and it is the half people actually type. Truncated because the widget
        # is in a sidebar and a label that wraps is a label that hides the next
        # one.
        label = f"{symbol} · {company[:38]}" if company else symbol
        labels.append((symbol, label))

    # THE ANSWER PUT WHERE THE QUESTION IS TYPED. Somebody who pastes BTCUSDT
    # out of an exchange reads `BTCUSDT → IBIT` in the list and picks it,
    # instead of committing the symbol, reading a sentence and typing IBIT by
    # hand. Same answer, two steps fewer.
    #
    # APPENDED LAST, and that ordering is the whole safety of it. Several of
    # these spellings are themselves real companies — DOW is Dow Holdings, GOLD
    # is Gold Inc, WTI is W&T Offshore — so a query matching both must find the
    # real symbol first and the alias underneath it. The guard below drops the
    # alias entirely when the spelling IS a catalogued symbol: the directory
    # listing it is the statement that somebody typing it may well mean it.
    #
    # AND AN ALIAS MAY ONLY POINT AT SOMETHING THE DIRECTORY HOLDS. A row
    # offering a symbol that is not listed would be the box promising what the
    # Add button then refuses.
    company_of = {symbol: company for symbol, company in rows}
    for typed, target in suggestions.dropdown_aliases().items():
        if typed in company_of or target not in company_of:
            continue
        company = company_of[target]
        suffix = f" · {company[:30].rstrip()}" if company else ""
        labels.append((target, f"{typed} → {target}{suffix}"))
    return labels
