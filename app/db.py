"""The single access point to the database. Nothing outside this module opens
a connection directly."""

from __future__ import annotations

import datetime as dt
import warnings
from datetime import datetime

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from app import config, market_calendar, migrate

# pandas prefers SQLAlchemy and says so on every read against a plain DBAPI
# connection. The advice does not apply here — this module owns every query and
# has no use for an ORM — and the warning would otherwise print on each page
# load, training everyone to ignore the console.
warnings.filterwarnings(
    "ignore", message="pandas only supports SQLAlchemy connectable.*", category=UserWarning
)

_schema_ready = False

def get_connection() -> psycopg.Connection:
    """Open a connection, bringing the schema up to date on the first one.

    Migrations run here rather than from a command you have to remember: the
    app and the collector both start from `docker compose up`, and a step that
    can be forgotten is a step that will be. `migrate.ensure_current` takes an
    advisory lock, so the two starting at once is safe and only one of them
    does the work.

    autocommit=True because most functions here only read, and a plain SELECT
    under autocommit=False still opens a transaction — leaving connections
    "idle in transaction" for as long as a Streamlit session lives. Writers
    that need atomicity open `conn.transaction()` explicitly.
    """
    global _schema_ready
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    if not _schema_ready:
        migrate.ensure_current(conn)
        _schema_ready = True
    return conn


# --- watchlist ---

def add_ticker(conn: psycopg.Connection, ticker: str) -> None:
    conn.execute("INSERT INTO watchlist (ticker) VALUES (%s) ON CONFLICT DO NOTHING", (ticker.upper(),))
    conn.commit()


def remove_ticker(conn: psycopg.Connection, ticker: str) -> None:
    conn.execute("DELETE FROM watchlist WHERE ticker = %s", (ticker.upper(),))
    conn.commit()


def get_watchlist(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


# The one SQL definition of the volume-weighted average IV, used by the rollup
# written on every collection. metrics.iv_weighted_average remains the
# definition of the number; tests assert the two agree, which is the only form
# that guarantee can take once one of them runs in the database.
#
# `expiry >= collected_at::date` excludes contracts whose expiry has passed:
# they have no volatility left, a source reporting one is reporting a sentinel,
# and they still carry the whole session's volume. Inclusive and by date — a
# zero-day contract during its own session is real trading.
_IV_WEIGHTED_AVG_SQL = """
    CASE WHEN SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                       THEN COALESCE(volume, 0) ELSE 0 END) > 0
         THEN SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                       THEN implied_volatility * COALESCE(volume, 0) ELSE 0 END)
              / SUM(CASE WHEN implied_volatility IS NOT NULL AND expiry >= collected_at::date
                         THEN COALESCE(volume, 0) ELSE 0 END)
    END
"""

# --- which provider's data a screen is showing ---

def active_source(conn: psycopg.Connection, ticker: str) -> str | None:
    """The provider that produced this ticker's most recent collection.

    Every read below is scoped to one source, and this is how that source is
    chosen: the freshest one wins, and rows from any other provider are hidden
    rather than blended in. Nothing is deleted — switch back and the older
    provider's history is visible again the moment it is the freshest one.

    Why refuse rather than offer a choice. Implied volatility is *computed* by
    the provider, not observed on the market, so the same contract on the same
    day legitimately differs between two of them; a chart that concatenates the
    two draws a move that never happened. Worse, the views showing "the latest
    moment" would take the newest timestamp regardless of source and then every
    row at it, so two providers collecting the same minute feed GEX and Max Pain
    each contract twice. A source switcher is the full answer and only earns its
    complexity once somebody actually runs two — refusing to mix is the part
    that has to exist first.

    None means "not known", and every read then applies no filter at all. That
    is the safe degradation: a database imported from before any of this existed
    keeps showing everything it did, instead of going blank because it failed to
    match a source nobody recorded.
    """
    ticker = ticker.upper()
    # The run log first, because it is what get_collection_moments reads: the
    # source decision and the moment list then come from the same table and
    # cannot disagree about which collection was last.
    row = conn.execute(
        """SELECT source FROM collection_runs
           WHERE ticker = %s AND status = 'success' AND COALESCE(rows_fetched, 0) > 0
           ORDER BY started_at DESC LIMIT 1""",
        (ticker,),
    ).fetchone()
    if row:
        return row[0]
    # An imported database may have snapshots and no runs. One row per
    # collection either way, so this stays a lookup rather than a scan.
    row = conn.execute(
        "SELECT source FROM snapshot_iv_summary WHERE ticker = %s ORDER BY collected_at DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def sources_for(conn: psycopg.Connection, ticker: str) -> list[str]:
    """Every provider that has data for this ticker, freshest first.

    Used by the interface to say that older rows are hidden rather than gone —
    a hidden chunk of history with no explanation is indistinguishable from a
    bug, and the person looking is the one who switched providers.
    """
    rows = conn.execute(
        """SELECT source FROM (
               SELECT source, max(collected_at) AS last_seen
               FROM snapshot_iv_summary WHERE ticker = %s GROUP BY source
           ) per_source ORDER BY last_seen DESC""",
        (ticker.upper(),),
    ).fetchall()
    return [r[0] for r in rows]


def _scope(conn: psycopg.Connection, ticker: str, source: str | None) -> str | None:
    """Resolve the source a read should be scoped to.

    Callers that already know it pass it — the dashboard resolves once per page
    load and hands it down, so a render costs one lookup rather than one per
    query. Callers that do not (the worker, scripts, checks) leave it out.
    """
    return source if source is not None else active_source(conn, ticker)


# --- snapshots ---

def _contract_root(symbol: str | None) -> str | None:
    """The root of an OCC contract symbol — everything before the date.

    OCC symbols end in a fixed 15 characters: six of expiry date, one of type,
    eight of strike. Whatever comes before that is the root, and it is the only
    place an adjustment shows up: the standard series carries the plain ticker,
    an adjusted one carries the ticker plus a digit (TSLL1, FCEL2).
    """
    if not symbol or len(symbol) <= 15:
        return None
    return symbol[:-15].strip().upper()


def _one_contract_per_identity(chain_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """One row per (expiry, strike, option_type), keeping the standard series.

    THE FAILURE THIS PREVENTS. contract_registry is keyed on
    (ticker, source, expiry, strike, option_type), and the upsert below feeds
    it from the rows just written. When a chain contains two contracts sharing
    that identity, Postgres refuses the whole statement:

        ON CONFLICT DO UPDATE command cannot affect row a second time

    and because the chain and the registry are written in one transaction, the
    snapshot is lost with it. Not degraded — nothing at all is collected for
    that ticker, on every cycle, for as long as the adjusted series exists. On
    the hosted sibling this silently disabled TSLL, FCEL and NDX; none of them
    had ever collected once. It is invisible from the outside because the
    ticker looks perfectly ordinary.

    Two contracts share an identity after a split or a special dividend: the
    adjusted series (deliverable no longer 100 shares) trades beside the
    standard one at the same strike and expiry. They are different instruments,
    and the schema has no room for the difference — the strike and expiry are
    the identity here.

    WHICH ONE SURVIVES. The standard series: its root is the plain ticker,
    while an adjusted root carries a digit. That is the contract almost all the
    volume and open interest sits in, and the one a reader means by "the 100
    strike". The other is dropped from THIS SNAPSHOT — nothing already stored
    is touched, and no history is deleted, which is the rule this project does
    not break.

    WITHOUT A SYMBOL, the first row wins and the frame's own order decides. A
    provider that serves no contract symbol cannot distinguish the two anyway,
    and losing one contract beats losing the ticker.
    """
    if chain_df.empty:
        return chain_df
    identity = ["expiry", "strike", "option_type"]
    if not chain_df.duplicated(subset=identity).any():
        return chain_df

    ranked = chain_df
    if "contract_symbol" in chain_df.columns:
        roots = chain_df["contract_symbol"].map(_contract_root)
        # False sorts before True, so the standard series comes first and
        # `keep="first"` takes it. A row with no symbol at all ranks last: it
        # tells us nothing, and any row that does is a better answer.
        ranked = chain_df.assign(
            _adjusted=[root != ticker.strip().upper() for root in roots],
            _unknown=roots.isna(),
        ).sort_values(["_unknown", "_adjusted"], kind="stable")

    deduped = ranked.drop_duplicates(subset=identity, keep="first")
    # sort_index puts the survivors back in the provider's own order: ranking
    # is how the winner is chosen, not how the chain is stored.
    return deduped.drop(columns=["_adjusted", "_unknown"], errors="ignore").sort_index()


def insert_snapshot(
    conn: psycopg.Connection,
    ticker: str,
    collected_at: datetime,
    underlying_price: float,
    chain_df: pd.DataFrame,
    source: str = "yahoo",
) -> None:
    """chain_df: exactly providers.CHAIN_COLUMNS.

    `source` is the active provider's `name` and is stored per row rather than
    per collection: a chart that mixes two providers' implied volatility draws
    a move that never happened, and the only place that can be prevented is at
    write time. It defaults to 'yahoo' so that callers written before providers
    existed keep working and mean what they always meant.

    The four greek columns are optional in the frame. Yahoo supplies none, and
    a provider that supplies some leaves the rest as None — stored as NULL, not
    0, because a zero delta is a real value a deep-OTM contract can have.
    """
    chain_df = _one_contract_per_identity(chain_df, ticker)
    def greek(row, name: str):
        value = getattr(row, name, None)
        return None if value is None or pd.isna(value) else float(value)

    def whole_number(value):
        """Missing counts become NULL, not NaN.

        Providers use NaN for "not quoted" — yfinance does it throughout — and a
        chain routinely carries it in volume and open interest for contracts
        that never traded. SQLite accepted that happily. Postgres INTEGER has no
        representation for NaN and rejects the whole statement with
        NumericValueOutOfRange, which under one transaction per chain means the
        entire snapshot is lost rather than one field. Found by running a real
        collection after the move: every ticker failed, and the message pointed
        at a range problem when nothing was out of range.
        """
        return None if value is None or pd.isna(value) else int(value)

    def real_number(value):
        """Same for the floating-point columns, where the failure is quieter.

        DOUBLE PRECISION does accept NaN, so nothing raises — but NaN then
        outranks every number in Postgres, so those rows pass `> 0` tests and
        survive ORDER BY as maxima. Normalising at the boundary keeps "we have
        no value" spelled one way everywhere below.
        """
        return None if value is None or pd.isna(value) else float(value)

    rows = [
        (
            ticker,
            collected_at,
            underlying_price,
            row.expiry,
            row.strike,
            row.option_type,
            real_number(row.last_price),
            real_number(row.bid),
            real_number(row.ask),
            whole_number(row.volume),
            whole_number(row.open_interest),
            real_number(row.implied_volatility),
            bool(row.in_the_money),
            greek(row, "delta"),
            greek(row, "gamma"),
            greek(row, "theta"),
            greek(row, "vega"),
            source,
        )
        for row in chain_df.itertuples()
    ]
    # One transaction for the whole chain, and a cursor because psycopg keeps
    # executemany there rather than on the connection. Under autocommit each
    # row would otherwise commit on its own, so a failure halfway would leave
    # a partial chain stored — a snapshot missing contracts is worse than no
    # snapshot, because nothing downstream can tell the difference.
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO option_snapshots
           (ticker, collected_at, underlying_price, expiry, strike, option_type,
            last_price, bid, ask, volume, open_interest, implied_volatility, in_the_money,
            delta, gamma, theta, vega, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            rows,
        )
        # The contract registry, in the same transaction as the chain it
        # describes: a registry that can lag behind the snapshots is a registry
        # that lists contracts with no data and hides contracts that have some.
        cur.execute(
            """INSERT INTO contract_registry
                   (ticker, source, expiry, strike, option_type,
                    first_seen_at, last_seen_at, snapshots)
               SELECT %(ticker)s, %(source)s, expiry, strike, option_type,
                      %(at)s, %(at)s, 1
               FROM option_snapshots
               WHERE ticker = %(ticker)s AND source = %(source)s AND collected_at = %(at)s
               ON CONFLICT (ticker, source, expiry, strike, option_type) DO UPDATE
               SET last_seen_at = GREATEST(contract_registry.last_seen_at, EXCLUDED.last_seen_at),
                   snapshots = contract_registry.snapshots + 1""",
            {"ticker": ticker, "source": source, "at": collected_at},
        )
        # The moment's weighted-average IV, computed here and read back from
        # the rows just written rather than recomputed from chain_df in Python:
        # the number then has one definition instead of two that must be kept
        # in step. Reading option_snapshots alone is correct at collection time
        # and would not be in a rebuild — nothing has been archived yet.
        #
        # THE SOURCE'S OWN NUMBER, AND `iv_model` STAYS NULL TO SAY SO. Ours is
        # solved from the contract's price and cannot be written in SQL; it
        # arrives moments later, from app/iv_backfill.py, which treats a NULL
        # here exactly as it treats the rows collected before that existed.
        # Writing the source's number first rather than leaving the row absent
        # is what keeps the chart whole if the solver never gets to it.
        #
        # The conflict branch clears `iv_model` for the same reason it rewrites
        # the average: a moment collected twice has been recomputed from the
        # provider column, and a stamp saying the number is ours would then
        # describe the previous one.
        cur.execute(
            f"""INSERT INTO snapshot_iv_summary (ticker, source, collected_at, iv_weighted_avg)
                SELECT %(ticker)s, %(source)s, %(at)s, {_IV_WEIGHTED_AVG_SQL}
                FROM option_snapshots
                WHERE ticker = %(ticker)s AND source = %(source)s AND collected_at = %(at)s
                ON CONFLICT (ticker, source, collected_at)
                DO UPDATE SET iv_weighted_avg = EXCLUDED.iv_weighted_avg,
                              iv_model = NULL""",  # noqa: S608
            {"ticker": ticker, "source": source, "at": collected_at},
        )


def get_snapshots(
    conn: psycopg.Connection,
    ticker: str,
    days: int | None = config.SNAPSHOT_HISTORY_DAYS,
    source: str | None = None,
) -> pd.DataFrame:
    """A ticker's snapshot history, bounded to the last `days` by default.

    `days=None` fetches everything — used by the dashboard's "Load full
    history" toggle, and by tooling that genuinely needs the whole series. No
    UI path should pass None by default: see config.SNAPSHOT_HISTORY_DAYS for
    why the bound exists.

    `source=None` resolves to the ticker's freshest provider; see
    `active_source` for why one is picked rather than all of them read."""
    where = "ticker = %s"
    params: list = [ticker]
    scoped = _scope(conn, ticker, source)
    if scoped is not None:
        where += " AND source = %s"
        params.append(scoped)
    if days is not None:
        where += " AND collected_at >= now() - make_interval(days => %s)"
        params.append(days)
    # Both tables, always. A snapshot is never archived as a whole — only the
    # contracts inside it that have since expired — so a moment from a few
    # months ago has its chain split across the two, and reading one of them
    # draws a chain quietly missing contracts. Found on the hosted product,
    # where 69 of one ticker's 288 moments were split, on average 8.3% of the
    # chain on the archive side.
    return pd.read_sql_query(
        f"""SELECT * FROM option_snapshots WHERE {where}
            UNION ALL
            SELECT * FROM option_snapshots_archive WHERE {where}
            ORDER BY collected_at""",
        conn,
        params=params + params,
        parse_dates=["collected_at", "expiry"],
    )


def get_put_call_ratio(
    conn: psycopg.Connection,
    ticker: str,
    days: int | None = config.SNAPSHOT_HISTORY_DAYS,
    source: str | None = None,
) -> pd.DataFrame:
    """Put/call ratio per collection, aggregated in SQL.

    The Overview chart is a few hundred points. Computing it by loading every
    raw row and grouping in pandas is the single most expensive thing a page
    load did — measured on the hosted sibling at 5.0s and ~390MB of DataFrame
    for one liquid ticker, against 0.8s and a few kilobytes for the same
    numbers aggregated here."""
    where = "ticker = %s"
    params: list = [ticker]
    scoped = _scope(conn, ticker, source)
    if scoped is not None:
        where += " AND source = %s"
        params.append(scoped)
    if days is not None:
        where += " AND collected_at >= now() - make_interval(days => %s)"
        params.append(days)
    raw = pd.read_sql_query(
        f"""SELECT collected_at, option_type,
                   SUM(volume) AS volume, SUM(open_interest) AS open_interest
            FROM (
                SELECT collected_at, option_type, volume, open_interest
                FROM option_snapshots WHERE {where}
                UNION ALL
                SELECT collected_at, option_type, volume, open_interest
                FROM option_snapshots_archive WHERE {where}
            ) both_tables
            GROUP BY collected_at, option_type
            ORDER BY collected_at""",
        conn,
        params=params + params,
        parse_dates=["collected_at"],
    )
    if raw.empty:
        return pd.DataFrame(columns=["collected_at", "pcr_volume", "pcr_oi"])
    wide = raw.pivot(index="collected_at", columns="option_type",
                     values=["volume", "open_interest"])
    # A collection with no puts (or no calls) leaves the column missing rather
    # than zero — reindex so the division yields NaN instead of raising on a
    # thin ticker.
    for measure in ("volume", "open_interest"):
        for side in ("call", "put"):
            if (measure, side) not in wide.columns:
                wide[(measure, side)] = pd.NA
    result = pd.DataFrame({
        "pcr_volume": wide[("volume", "put")] / wide[("volume", "call")],
        "pcr_oi": wide[("open_interest", "put")] / wide[("open_interest", "call")],
    })
    return result.reset_index()


def get_snapshot_dates(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> list[str]:
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s" + (" AND source = %(source)s" if scoped else "")
    rows = conn.execute(
        f"""SELECT collected_at FROM option_snapshots WHERE {where}
           UNION
           SELECT collected_at FROM option_snapshots_archive WHERE {where}
           ORDER BY collected_at""",  # noqa: S608 — filter is a fixed string
        {"ticker": ticker, "source": scoped},
    ).fetchall()
    return [r[0] for r in rows]


# --- collection runs ---

def log_run(
    conn: psycopg.Connection,
    started_at: datetime,
    finished_at: datetime,
    ticker: str,
    status: str,
    error_message: str | None = None,
    rows_fetched: int | None = None,
    oi_zero_fraction: float | None = None,
    source: str = "yahoo",
) -> None:
    """`rows_fetched`/`oi_zero_fraction` — diagnostics for the collection log
    (dashboard sidebar): how many chain rows actually arrived and what fraction
    of open_interest came back as zero. Written regardless of status — they
    reveal not just outright failures but also "successful" yet suspect
    collections (see spec FR23).

    `source` is the provider that ran, and it matters beyond the log: this table
    is where the list of collection moments comes from, so a run recorded
    without its provider is a moment no screen can scope. Defaults to 'yahoo'
    for the same reason the column does — it is what every run recorded before
    the column existed actually was."""
    conn.execute(
        """INSERT INTO collection_runs
           (started_at, finished_at, ticker, status, error_message, rows_fetched,
            oi_zero_fraction, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            started_at,
            finished_at,
            ticker,
            status,
            error_message,
            rows_fetched,
            oi_zero_fraction,
            source,
        ),
    )
    conn.commit()


def get_recent_runs(conn: psycopg.Connection, limit: int = 50) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT %s",
        conn,
        params=(limit,),
    )


def unresolvable_tickers(
    conn: psycopg.Connection, limit: int | None = None
) -> dict[str, datetime]:
    """Symbols that have failed enough times, and have never once worked.

    WHY THIS EXISTS. A symbol that does not exist is asked for on every cycle,
    forever, and fills the collection log with the same failure — which is how
    a real failure stops being noticed. A typed-in `APPL` costs a request and a
    log line every fifteen minutes until somebody spots it.

    THE CONDITION THAT MATTERS is "has never produced a snapshot". It is what
    separates "this symbol is imaginary" from "the data source was down": an
    outage fails everything at once, including tickers that have been
    collecting happily for months, and none of those may be suspended. It also
    keeps the project's first rule intact — a symbol with history is a real
    position somebody holds, and a delisting is precisely the case that would
    otherwise be tidied away.

    Because suspension only ever applies to a symbol with no successes at all,
    "failures in a row" and "failures" are the same number, and no counter has
    to be stored anywhere. One success and the symbol leaves this set for good;
    the run log is the state.

    NO MIGRATION, DELIBERATELY. The hosted sibling keeps a counter column on
    its tickers table. Here the run log already holds one row per attempt and
    is indexed by (ticker, source, started_at) — a second place holding the
    same fact would have to be kept in step with it, and self-hosted upgrades
    are somebody else's Sunday afternoon.

    Returns {ticker: when it was suspended}, the suspending failure's own
    timestamp, so the interface can say since when rather than just "off".
    """
    threshold = config.UNRESOLVABLE_AFTER_FAILURES if limit is None else limit
    rows = conn.execute(
        """SELECT ticker, min(started_at) FROM (
               SELECT ticker, started_at,
                      row_number() OVER (PARTITION BY ticker ORDER BY started_at) AS n
               FROM collection_runs
               WHERE status = 'failed'
                 AND ticker NOT IN (SELECT ticker FROM collection_runs WHERE status = 'success')
           ) ranked
           WHERE n >= %(limit)s
           GROUP BY ticker""",
        {"limit": threshold},
    ).fetchall()
    return {row[0].upper(): row[1] for row in rows}


def store_realized_volatility(
    conn: psycopg.Connection,
    ticker: str,
    as_of: dt.date,
    values: dict[int, float],
    source: str,
) -> int:
    """Write one day's realized volatility for one ticker. Returns rows written.

    UPSERT rather than insert: a pass that runs twice in a day (two manual
    collections, a restart) must cost nothing the second time instead of
    raising on the primary key. Nothing is ever deleted here — the series is
    the point, and a few dozen bytes a day is not a reason to throw away the
    only record of what volatility used to be.
    """
    if not values:
        return 0
    with conn.transaction():
        for window, value in sorted(values.items()):
            conn.execute(
                """INSERT INTO realized_volatility
                       (ticker, as_of_date, window_days, value, source)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (ticker, as_of_date, window_days)
                   DO UPDATE SET value = EXCLUDED.value,
                                 source = EXCLUDED.source,
                                 fetched_at = now()""",
                (ticker.upper(), as_of, int(window), float(value), source),
            )
    return len(values)


def get_realized_volatility(
    conn: psycopg.Connection, ticker: str
) -> tuple[dt.date, dict[int, float], str] | None:
    """The most recent stored day for one ticker, or None if there is none.

    THE WHOLE POINT OF THE TABLE IS THAT THIS IS A READ. The screen that shows
    these numbers used to fetch six months of daily closes over the network
    while it was being drawn; this is one index lookup against a handful of
    rows, and it cannot fail in a way that the reader experiences as the
    product being broken.

    The date comes back with the values because it is part of the answer: a
    figure from three days ago is still worth showing on a Monday morning, and
    worth labelling rather than passing off as today's.
    """
    rows = conn.execute(
        """SELECT as_of_date, window_days, value, source
           FROM realized_volatility
           WHERE ticker = %(ticker)s
             AND as_of_date = (SELECT max(as_of_date) FROM realized_volatility
                                WHERE ticker = %(ticker)s)
           ORDER BY window_days""",
        {"ticker": ticker.upper()},
    ).fetchall()
    if not rows:
        return None
    return rows[0][0], {int(r[1]): float(r[2]) for r in rows}, rows[0][3]


def tickers_missing_realized_volatility(
    conn: psycopg.Connection, tickers: list[str], day: dt.date
) -> list[str]:
    """Which of these have no realized volatility stored for `day`.

    The guard that keeps this to one price-history request per ticker per day.
    Without it a fifteen-minute collection interval would ask the source for
    six months of daily closes ninety-six times a day per symbol, to recompute
    a number that only moves when a session closes — which is the sort of
    traffic that gets an installation rate-limited for everybody's sake but its
    own.
    """
    wanted = [t.upper() for t in tickers]
    if not wanted:
        return []
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM realized_volatility
           WHERE as_of_date = %s AND ticker = ANY(%s)""",
        (day, wanted),
    ).fetchall()
    stored = {r[0].upper() for r in rows}
    return [t for t in wanted if t not in stored]


def collection_state(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> tuple[datetime | None, int]:
    """(last successful collection, failures since it) for one ticker.

    The two facts the freshness note is built from, read together so that they
    cannot disagree: counting failures in a second query would count the ones
    that happened before the success the first query found, and report a symbol
    as failing minutes after it recovered.

    SCOPED BY SOURCE, like everything else a screen shows. A ticker collected
    by one provider and refused by another is not "failing" on the screen
    drawing the provider that serves it — and saying so would be both wrong and
    unfixable by the person reading it.

    The run log is the state, exactly as it is for suspension: no counter is
    stored anywhere, so nothing has to be kept in step with it. Both the
    subquery and the count hit the (ticker, source, started_at) index.

    The timestamp comes back naive and is UTC by the collector's convention,
    the same as every other moment in this schema.
    """
    row = conn.execute(
        """WITH last_ok AS (
               SELECT max(started_at) AS at
               FROM collection_runs
               WHERE ticker = %(ticker)s AND status = 'success'
                 AND (%(source)s::text IS NULL OR source = %(source)s::text)
           )
           SELECT (SELECT at FROM last_ok),
                  (SELECT count(*) FROM collection_runs
                    WHERE ticker = %(ticker)s AND status = 'failed'
                      AND (%(source)s::text IS NULL OR source = %(source)s::text)
                      AND (started_at > (SELECT at FROM last_ok)
                           OR (SELECT at FROM last_ok) IS NULL))""",
        {"ticker": ticker.upper(), "source": source},
    ).fetchone()
    if row is None:
        return None, 0
    return row[0], int(row[1] or 0)


def collection_depth(conn: psycopg.Connection) -> dict[str, dt.date]:
    """First collection date per ticker — how much history each one has.

    The product's whole argument is that history exists. Somebody who adds a
    symbol today and opens the chart sees one point, which is the opposite of
    the pitch, told to them silently by the product itself. This is what lets
    the interface say so BEFORE they are disappointed rather than explain it
    afterwards.

    It is worse here than on the hosted version, where a database has been
    filling since long before any given user arrived. A self-hosted install
    starts empty: the first chart is one point BY CONSTRUCTION, and nothing on
    screen says that this is the normal beginning rather than a broken tool.

    Read from snapshot_iv_summary — one row per ticker per collection moment,
    a few thousand rows against millions of option rows — so the aggregate
    never touches option_snapshots. Whole table at once, for the caller to
    cache: asking per keystroke would put a query on a text input.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ticker, min(collected_at)::date FROM snapshot_iv_summary GROUP BY ticker"
        )
        return {row[0].upper(): row[1] for row in cur.fetchall()}


# --- tracked contracts (spec FR14) ---

def add_tracked_contract(
    conn: psycopg.Connection, ticker: str, expiry, strike: float, option_type: str
) -> None:
    expiry_str = pd.Timestamp(expiry).strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO tracked_contracts (ticker, expiry, strike, option_type)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (ticker.upper(), expiry_str, float(strike), option_type),
    )
    conn.commit()


def remove_tracked_contract(conn: psycopg.Connection, contract_id: int) -> None:
    conn.execute("DELETE FROM tracked_contracts WHERE id = %s", (contract_id,))
    conn.commit()


def get_tracked_contracts(conn: psycopg.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM tracked_contracts WHERE ticker = %s ORDER BY expiry, strike",
        conn,
        params=(ticker.upper(),),
        parse_dates=["expiry"],
    )


# --- settings the running application changes ---

def get_setting(conn: psycopg.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
        (key, str(value)),
    )


COLLECTOR_INTERVAL_KEY = "collector_interval_minutes"

# When the data source last said "stop asking", expressed as the moment it is
# reasonable to ask again. In app_settings rather than a new table: the setting
# is one timestamp, it belongs to the installation rather than to any ticker,
# and the table already exists — a self-hosted upgrade that needs no migration
# is one fewer way for somebody's Sunday to go wrong.
COOLDOWN_UNTIL_KEY = "provider_cooldown_until"


def provider_cooldown_until(conn: psycopg.Connection) -> datetime | None:
    """When the source may be asked again, or None if it may be asked now.

    A cooldown in the past is not a cooldown — it is returned as None rather
    than cleared, so that reading this never writes. The value is left behind
    on purpose: it is the only record that throttling happened at all, and it
    is worth seeing in app_settings when somebody asks why a morning is missing
    from the history.
    """
    raw = get_setting(conn, COOLDOWN_UNTIL_KEY)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return until if until > datetime.utcnow() else None


def start_provider_cooldown(conn: psycopg.Connection, minutes: int | None = None) -> datetime:
    """Stop talking to the source until this moment, and say when that is."""
    span = config.PROVIDER_COOLDOWN_MINUTES if minutes is None else minutes
    until = datetime.utcnow() + dt.timedelta(minutes=span)
    set_setting(conn, COOLDOWN_UNTIL_KEY, until.isoformat())
    conn.commit()
    return until


def get_collector_interval(conn: psycopg.Connection) -> int:
    """How often the collector should run, in minutes. Zero means never.

    Read from the database rather than the environment so that changing it is a
    setting rather than maintenance: the worker re-reads this every cycle, and
    nothing has to be restarted.

    The floor is applied HERE rather than only in the interface. A value can
    reach the database by other routes — a hand-written UPDATE, a future import,
    a list of choices edited without noticing what it implies — and the source
    that would be hit too often has no way to defend itself. Zero passes
    through untouched: "off" is not a frequency.
    """
    raw = get_setting(conn, COLLECTOR_INTERVAL_KEY)
    minutes = int(raw) if raw is not None else config.COLLECTOR_INTERVAL_DEFAULT_MINUTES
    if minutes <= 0:
        return 0
    return max(minutes, config.PROVIDER_MIN_INTERVAL_MINUTES)


def set_collector_interval(conn: psycopg.Connection, minutes: int) -> None:
    set_setting(conn, COLLECTOR_INTERVAL_KEY, str(int(minutes)))


def estimated_growth_mb_per_month(conn: psycopg.Connection, interval_minutes: int) -> float:
    """What continuous collection will cost on disk, for THIS watchlist.

    Answered from the rows already collected rather than from a guess: the
    average chain size differs by an order of magnitude between a single small
    ticker and a handful of index ETFs, so a generic number would be wrong in
    the only direction that matters. With nothing collected yet there is
    nothing to measure and the answer is zero — the interface says so rather
    than inventing a figure.

    This exists to be shown at the moment somebody switches collection on. A
    background process filling a stranger's disk without ever having named a
    number is how a tool gets uninstalled angrily.
    """
    if interval_minutes <= 0:
        return 0.0
    row = conn.execute(
        """SELECT count(*)::float / GREATEST(count(DISTINCT collected_at), 1)
           FROM option_snapshots"""
    ).fetchone()
    rows_per_pass = float(row[0] or 0)
    return (
        rows_per_pass
        * _passes_per_month(interval_minutes)
        * config.BYTES_PER_SNAPSHOT_ROW
        / 1_000_000
    )


# Trading days in an average month: 252 a year is the US market's own figure,
# and 252/12 is 21. The remaining nine days of a 30-day month are weekends and
# holidays, and the collector spends exactly one snapshot on each of them.
_TRADING_DAYS_PER_MONTH = 21.0
_CLOSED_DAYS_PER_MONTH = 9.0


def _passes_per_month(interval_minutes: int) -> float:
    """How many collections a month at this interval, given the calendar.

    THIS USED TO MULTIPLY BY 24 AND BY 30 — it assumed collection ran around
    the clock, every day. That stopped being true when the market calendar
    landed in v0.5.0: the collector now sleeps through a closed market and
    takes a single snapshot per closed day as a safety net. US options trade
    6.5 hours on 21 days a month, so the old estimate was too high by roughly
    a factor of five (2,880 passes a month against 555, at a 15-minute
    interval).

    The error was in the safe direction — it frightened people rather than
    surprising them — which is exactly why it could have lived here for a
    long time: nobody complains that the disk filled slower than promised. It
    is still worth fixing, because the number is shown at the one moment
    somebody decides whether to switch collection on at all, and a fivefold
    exaggeration can make that decision for them.

    Fractional passes per session are deliberate and not rounded up. At a
    4-hour interval a 6.5-hour session gets 1.625 collections — some days two,
    some days one — and rounding that to two would reintroduce a smaller
    version of the same overstatement.

    The session length is read from market_calendar rather than written down
    again: it is the module that decides when the collector actually runs, so
    a change there must move this number with it.
    """
    open_minutes = (
        dt.datetime.combine(dt.date.min, market_calendar.CLOSE_TIME)
        - dt.datetime.combine(dt.date.min, market_calendar.OPEN_TIME)
    ).total_seconds() / 60
    passes_per_session = max(open_minutes / interval_minutes, 1.0)
    return _TRADING_DAYS_PER_MONTH * passes_per_session + _CLOSED_DAYS_PER_MONTH


# --- archiving ---

def archive_expired_contracts(
    conn: psycopg.Connection, grace_days: int = config.CONTRACT_ARCHIVE_GRACE_DAYS
) -> int:
    """Move snapshots of long-expired contracts to option_snapshots_archive.

    MOVED, NEVER DELETED — the first rule of this project. What this buys is
    that the table every live query reads stops carrying contracts that can
    never trade again; the history itself stays, and every historical read
    unions both tables.

    One transaction: an INSERT that committed without its DELETE would double
    every archived row, and the union means you would see them twice.

    REACHED THROUGH contract_registry, NOT BY FILTERING ON `expiry`.
    option_snapshots has no index on `expiry` alone — it appears only third in
    idx_snapshots_contract_source, behind two equalities, where it cannot be
    seeked. So the obvious `WHERE expiry < …` could be answered exactly one
    way: a sequential scan of the whole hot table, in BOTH statements, on
    every pass, whether or not anything qualified. The cost grows with the
    table forever and is paid even on a day with no work to do. Measured on
    the hosted sibling's 18.5M-row database: 4,912 ms and 333,025 blocks per
    scan, on an empty day.

    The registry answers the same question for almost nothing: it holds one row
    per CONTRACT and indexes expiry (idx_registry_expiry). Its rows then reach
    their snapshots through idx_snapshots_contract_source — the expensive index
    this product already pays for, finally used for the shape it was built for.
    Same database, same day: 7 blocks. A dedicated index on `expiry` was
    considered instead and rejected — cheap on disk, but +21% on every insert,
    and inserts are what this product does all day.

    This matters more here than it did there. Self-hosted installations run on
    whatever is spare — a single-board computer, a home server, the smallest
    VPS — and two full table scans per pass cost more on those than on the
    eight-core machine the numbers above came from.

    WHY THE JOIN CANNOT MISS A CONTRACT: insert_snapshot writes the chain and
    upserts the registry in one transaction, so a contract absent from the
    registry is absent from the hot table too. If that invariant broke anyway,
    the failure is benign — unmatched rows stay where they are. Nothing is
    lost; growth is merely unbounded again, which shows up on disk rather than
    silently.
    """
    move, remove = archive_statements()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(move, {"days": grace_days})
        moved = cur.rowcount
        cur.execute(remove, {"days": grace_days})
    return moved


def archive_statements() -> tuple[str, str]:
    """The two statements of an archiving pass — the move and the removal.

    Built here rather than written inline so that a check can read them without
    running them. The regression this guards is invisible in a result: both
    shapes return exactly the same rows whether the planner reads seven blocks
    or three hundred thousand, so nothing but the statement itself can tell the
    fast version from the one that scans the whole table.
    """
    match = (
        "r.ticker = o.ticker AND r.source = o.source AND r.expiry = o.expiry "
        "AND r.strike = o.strike AND r.option_type = o.option_type"
    )
    horizon = "r.expiry < current_date - make_interval(days => %(days)s)"
    # `o.*` rather than a column list: option_snapshots_archive is declared LIKE
    # option_snapshots, so the two shapes cannot drift apart, and a migration
    # that added a column to one and not the other would fail loudly here
    # instead of writing every value into its neighbour's column.
    move = (
        "INSERT INTO option_snapshots_archive "  # noqa: S608 — fragments are literals above
        f"SELECT o.* FROM contract_registry r JOIN option_snapshots o ON {match} "
        f"WHERE {horizon}"
    )
    remove = (
        f"DELETE FROM option_snapshots o USING contract_registry r "  # noqa: S608 — same
        f"WHERE {match} AND {horizon}"
    )
    return move, remove


# --- narrow reads: each view asks for what it shows ---



def get_latest_snapshot(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> pd.DataFrame:
    """The most recent collection's chain, and nothing else.

    Most views show a moment rather than a history — the screener, max pain,
    the GEX profile, the expiry and strike selectors — and used to get it by
    filtering the whole ticker's history in pandas after loading it.

    The moment and the rows at it are scoped to the same source, and that is
    the pairing that matters: taking the newest timestamp across all providers
    and then every row at it hands the chain to GEX and Max Pain twice if two
    of them happened to collect the same minute.
    """
    scoped = _scope(conn, ticker, source)
    started_at = _latest_moment(conn, ticker, scoped)
    moments = [] if started_at is None else [started_at]
    return get_snapshots_at(conn, ticker, moments, source=scoped)


def _latest_moment(conn: psycopg.Connection, ticker: str, source: str | None = None):
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s" + (" AND source = %(source)s" if scoped else "")
    row = conn.execute(
        f"""SELECT max(collected_at) FROM (
               SELECT max(collected_at) AS collected_at FROM option_snapshots WHERE {where}
               UNION ALL
               SELECT max(collected_at) FROM option_snapshots_archive WHERE {where}
           ) both_tables""",  # noqa: S608 — filter is a fixed string
        {"ticker": ticker.upper(), "source": scoped},
    ).fetchone()
    return row[0] if row else None


# `started_at` is stored naive in UTC, so it is read as UTC first and only then
# converted — `started_at AT TIME ZONE 'America/New_York'` alone would read the
# stored value AS New York time and shift it the wrong way.
_MARKET_TIME = "((started_at AT TIME ZONE 'UTC') AT TIME ZONE 'America/New_York')"
_MARKET_DATE = f"{_MARKET_TIME}::date"


def get_collection_moments(
    conn: psycopg.Connection,
    ticker: str,
    days: int | None = config.SNAPSHOT_HISTORY_DAYS,
    per_day: bool = False,
    source: str | None = None,
) -> list:
    """The moments this ticker was collected at, newest first — from the run
    log, not from the snapshots.

    The two hold the same instant, not merely close ones: the collector takes
    one timestamp per pass and writes it both as the run's `started_at` and as
    every row's `collected_at`.

    Why not `SELECT DISTINCT collected_at` on the snapshots, which is what this
    replaces: Postgres has no loose index scan, so that query reads one index
    entry per row and its cost grows with collection time rather than with the
    size of the answer — measured on the hosted product at 946 ms to read 3.26M
    index entries and return 247 values.

    `per_day=True` returns the last collection of each calendar day, which is
    what every metric built on daily history actually uses.

    Scoped to one source like every other read. A run recorded before the
    source column existed reads as 'yahoo', which is what it was.
    """
    scoped = _scope(conn, ticker, source)
    params: dict = {"ticker": ticker.upper(), "source": scoped}
    where = "ticker = %(ticker)s AND status = 'success' AND COALESCE(rows_fetched, 0) > 0"
    if scoped is not None:
        where += " AND source = %(source)s"
    if days is not None:
        where += " AND started_at >= now() - make_interval(days => %(days)s)"
        params["days"] = days
    if per_day:
        # A "day" is New York's, and only trading days count. Two corrections,
        # both needed and both measured on the sibling product:
        #
        # The timezone one, because a collection at 01:15 UTC on Saturday is
        # Friday 21:15 in New York — the evening of that trading day. Grouped by
        # the UTC date it lands in Saturday's bucket, so "Friday's last
        # snapshot" is really its 20:00 UTC one and the true post-close state is
        # filed under the weekend.
        #
        # The weekend one, because the chain does not change while the market is
        # closed: Saturday and Sunday hold copies of Friday. Counted as days
        # they make OI Delta compare Friday with Friday and report no change,
        # and they fill two sevenths of the Unusual Activity baseline with
        # repetitions, which pulls the mean toward Friday and understates the
        # spread.
        statement = f"""
            SELECT DISTINCT ON ({_MARKET_DATE}) started_at
            FROM collection_runs WHERE {where}
              AND extract(isodow from {_MARKET_TIME}) <= 5
            ORDER BY {_MARKET_DATE} DESC, started_at DESC
        """
    else:
        statement = f"""
            SELECT started_at FROM collection_runs WHERE {where}
            ORDER BY started_at DESC
        """
    return [row[0] for row in conn.execute(statement, params).fetchall()]  # noqa: S608


def get_snapshots_at(
    conn: psycopg.Connection, ticker: str, moments: list, source: str | None = None
) -> pd.DataFrame:
    """Chain rows for a named set of collection moments, both tables.

    No early return on an empty list: the query then yields zero rows but with
    the table's columns, while an empty frame has no columns at all and every
    caller dies indexing `collected_at`. A ticker with nothing collected yet is
    not an error.

    A moment is not by itself a unique key — two providers can collect the same
    instant — so this is scoped to one source too.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s AND collected_at = ANY(%(moments)s::timestamp[])"
    if scoped is not None:
        where += " AND source = %(source)s"
    return pd.read_sql_query(
        f"""SELECT * FROM option_snapshots WHERE {where}
           UNION ALL
           SELECT * FROM option_snapshots_archive WHERE {where}
           ORDER BY collected_at""",  # noqa: S608 — filter is a fixed string
        conn,
        params={"ticker": ticker.upper(), "moments": list(moments), "source": scoped},
        parse_dates=["collected_at", "expiry"],
    )


def get_expired_contracts(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> pd.DataFrame:
    """Contracts of this ticker that have expired but still have collected
    history, newest expiry first.

    Read from contract_registry and never from option_snapshots: the equivalent
    SELECT DISTINCT costs one index entry per snapshot per contract, so it grows
    with collection time rather than with the size of the answer (measured at
    152,409 entries read for 13,449 contracts — see the migration).
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s AND expiry < current_date"
    if scoped is not None:
        where += " AND source = %(source)s"
    return pd.read_sql_query(
        f"""SELECT expiry, strike, option_type, snapshots, first_seen_at, last_seen_at
           FROM contract_registry WHERE {where}
           ORDER BY expiry DESC, strike, option_type""",  # noqa: S608 — filter is a fixed string
        conn,
        params={"ticker": ticker.upper(), "source": scoped},
        parse_dates=["expiry", "first_seen_at", "last_seen_at"],
    )


def get_contract_history(
    conn: psycopg.Connection,
    ticker: str,
    expiry,
    strike: float,
    option_type: str,
    days: int | None = config.SNAPSHOT_HISTORY_DAYS,
    source: str | None = None,
) -> pd.DataFrame:
    """One contract across every collection, filtered in SQL.

    One row per collection for one contract stays small no matter how liquid
    the ticker's full chain is — the Contract view used to reach it by loading
    the whole chain's history and filtering in pandas.

    Scoping matters here more than anywhere: this is the one view that plots a
    single contract's own implied volatility over time, so two providers'
    differing calculations of it would show up as the contract moving.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s AND expiry = %(expiry)s AND strike = %(strike)s AND option_type = %(option_type)s"
    if scoped is not None:
        where += " AND source = %(source)s"
    params = {
        "ticker": ticker.upper(),
        "expiry": pd.Timestamp(expiry).strftime("%Y-%m-%d"),
        "strike": float(strike),
        "option_type": option_type,
        "source": scoped,
    }
    date_filter = ""
    if days is not None:
        date_filter = "AND collected_at >= now() - make_interval(days => %(days)s)"
        params["days"] = days
    return pd.read_sql_query(
        f"""SELECT * FROM option_snapshots WHERE {where} {date_filter}
            UNION ALL
            SELECT * FROM option_snapshots_archive WHERE {where} {date_filter}
            ORDER BY collected_at""",  # noqa: S608 — filters are fixed strings
        conn,
        params=params,
        parse_dates=["collected_at", "expiry"],
    )


def get_iv_weighted_average(
    conn: psycopg.Connection,
    ticker: str,
    days: int | None = config.SNAPSHOT_HISTORY_DAYS,
    source: str | None = None,
) -> pd.DataFrame:
    """Volume-weighted average IV per collection, read from the rollup.

    The rollup is stored per source already, so without a filter this would
    return two rows for the same instant and the chart would zigzag between
    two providers' opinions of the same market."""
    scoped = _scope(conn, ticker, source)
    params: dict = {"ticker": ticker.upper(), "source": scoped}
    where = "ticker = %(ticker)s"
    if scoped is not None:
        where += " AND source = %(source)s"
    if days is not None:
        where += " AND collected_at >= now() - make_interval(days => %(days)s)"
        params["days"] = days
    return pd.read_sql_query(
        f"""SELECT collected_at, iv_weighted_avg FROM snapshot_iv_summary
            WHERE {where}
            ORDER BY collected_at""",  # noqa: S608 — filter is a fixed string
        conn,
        params=params,
        parse_dates=["collected_at"],
    )


def iv_summary_pending(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> int:
    """How many stored averages still carry the data source's volatility.

    Asked on the volatility screen so the caption there can be a fact rather
    than a warning that never goes away, and asked by the worker so a machine
    with nothing left to do stops looking. One index-only read of the partial
    index, which is empty once the backfill has finished — see migration 0006.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s AND iv_model IS NULL"
    if scoped is not None:
        where += " AND source = %(source)s"
    row = conn.execute(
        f"SELECT count(*) FROM snapshot_iv_summary WHERE {where}",  # noqa: S608
        {"ticker": ticker.upper(), "source": scoped},
    ).fetchone()
    return int(row[0]) if row else 0


def iv_summary_pending_moments(
    conn: psycopg.Connection, ticker: str, limit: int, source: str | None = None
) -> list:
    """The moments whose stored average is still the source's, NEWEST FIRST.

    The order is the whole user-visible design of the backfill. Both ends of
    the chart converge on the same numbers eventually, but only one of them
    does it where people are looking: recomputing forwards from the oldest
    collection leaves the recent weeks — the part anybody actually reads —
    showing the source's model for as long as the catch-up takes.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s AND iv_model IS NULL"
    if scoped is not None:
        where += " AND source = %(source)s"
    rows = conn.execute(
        f"""SELECT collected_at FROM snapshot_iv_summary WHERE {where}
            ORDER BY collected_at DESC LIMIT %(limit)s""",  # noqa: S608
        {"ticker": ticker.upper(), "source": scoped, "limit": limit},
    ).fetchall()
    return [r[0] for r in rows]


def store_own_iv_weighted_average(
    conn: psycopg.Connection, ticker: str, source: str | None, values: dict
) -> int:
    """Write averages solved by our own model, stamped as ours.

    `values` is {moment: average or None}. A moment whose average could not be
    computed — a chain with no volume left to weight by — is stamped all the
    same, with the average it already had: the alternative is asking the same
    unanswerable question on every pass for the life of the installation.

    Scoped by source like every other write here. The rollup's key includes it,
    and a moment collected by two providers is two different numbers.
    """
    scoped = _scope(conn, ticker, source)
    if not values:
        return 0
    where = "ticker = %(ticker)s AND collected_at = %(at)s"
    if scoped is not None:
        where += " AND source = %(source)s"
    written = 0
    with conn.transaction(), conn.cursor() as cur:
        for moment, average in values.items():
            cur.execute(
                f"""UPDATE snapshot_iv_summary
                    SET iv_weighted_avg = COALESCE(%(avg)s, iv_weighted_avg),
                        iv_model = 'own'
                    WHERE {where}""",  # noqa: S608
                {"ticker": ticker.upper(), "source": scoped, "at": moment, "avg": average},
            )
            written += cur.rowcount
    return written


def rebuild_volume_stats(
    conn: psycopg.Connection,
    ticker: str,
    days: int = config.UNUSUAL_HISTORY_DAYS,
    source: str | None = None,
) -> int:
    """Recompute the per-contract volume baseline for one ticker and store it.

    WHOLE CALENDAR DAYS ONLY, and strictly before today: volume accumulates
    within a session, so today's partial figure is not comparable with
    completed days — and the metric asks for a baseline that excludes the day
    being judged anyway. metrics.unusual_activity applies the same rule when it
    aggregates for itself.

    One source at a time. The moments come from that source's runs and the rows
    are filtered to it, so a baseline is always built from one provider's
    numbers even when two of them collected the same days.
    """
    scoped = _scope(conn, ticker, source)
    moments = [m for m in get_collection_moments(conn, ticker, days=days, per_day=True, source=scoped)
               if m.date() < dt.date.today()]
    if not moments:
        return 0
    row_filter = " AND source = %(source)s" if scoped is not None else ""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO contract_volume_stats
                   (ticker, source, expiry, strike, option_type,
                    avg_volume, std_volume, history_points, through_day, computed_at)
               SELECT %(ticker)s, source, expiry, strike, option_type,
                      avg(volume)::double precision,
                      stddev(volume)::double precision,
                      count(volume),
                      max(collected_at)::date,
                      now()
               FROM (
                   SELECT source, collected_at, expiry, strike, option_type, volume
                   FROM option_snapshots
                   WHERE ticker = %(ticker)s
                     AND collected_at = ANY(%(moments)s::timestamp[]){row_filter}
                   UNION ALL
                   SELECT source, collected_at, expiry, strike, option_type, volume
                   FROM option_snapshots_archive
                   WHERE ticker = %(ticker)s
                     AND collected_at = ANY(%(moments)s::timestamp[]){row_filter}
               ) all_rows
               GROUP BY source, expiry, strike, option_type
               ON CONFLICT (ticker, source, expiry, strike, option_type) DO UPDATE
               SET avg_volume = EXCLUDED.avg_volume,
                   std_volume = EXCLUDED.std_volume,
                   history_points = EXCLUDED.history_points,
                   through_day = EXCLUDED.through_day,
                   computed_at = EXCLUDED.computed_at""",  # noqa: S608 — filter is a fixed string
            {"ticker": ticker.upper(), "moments": moments, "source": scoped},
        )
        return cur.rowcount


def volume_stats_are_current(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> bool:
    """Whether the stored baseline already covers every closed day.

    Cheap enough to ask on every collection pass, which is what makes the
    rebuild self-healing: a machine that was off when the day rolled over
    catches up on its next pass rather than waiting for a scheduler nobody
    watches.

    Asked per source, because a baseline is per source: the active provider's
    statistics being stale is not excused by another provider's being fresh.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s" + (" AND source = %(source)s" if scoped else "")
    row = conn.execute(
        f"SELECT max(through_day) FROM contract_volume_stats WHERE {where}",  # noqa: S608
        {"ticker": ticker.upper(), "source": scoped},
    ).fetchone()
    # Measured in TRADING days: on a Monday "yesterday" is Sunday, which was
    # never a collection day, so a baseline built through Friday would look
    # stale on every pass and be rebuilt to the same numbers.
    return bool(row and row[0] and row[0] >= market_calendar.last_completed_trading_day())


def get_volume_stats(
    conn: psycopg.Connection, ticker: str, source: str | None = None
) -> pd.DataFrame:
    """The stored per-contract baseline — a lookup, not an aggregate.

    Empty for a ticker with no completed day yet, and that is the right answer
    rather than a missing one: metrics.unusual_activity treats a contract with
    no history as having none and falls back to its crude rule, which is what
    "we have not watched this long enough" means.

    One row per contract *per source* is stored, and the caller joins on the
    contract alone — so without the filter every contract would match twice and
    Unusual Activity would report each one as two rows.
    """
    scoped = _scope(conn, ticker, source)
    where = "ticker = %(ticker)s" + (" AND source = %(source)s" if scoped else "")
    return pd.read_sql_query(
        f"""SELECT expiry, strike, option_type, avg_volume, std_volume, history_points
           FROM contract_volume_stats WHERE {where}""",  # noqa: S608 — filter is a fixed string
        conn,
        params={"ticker": ticker.upper(), "source": scoped},
        parse_dates=["expiry"],
    )


# --- the per-day summary (the Changes view) -------------------------------

_DAY_SUMMARY_COLUMNS = (
    "day", "collected_at", "underlying_price", "net_gex", "call_wall", "put_wall", "gamma_flip",
    "state", "expiry_count", "horizon_dte", "nearest_expiry", "nearest_max_pain", "move_lower",
    "move_upper", "contracts", "call_volume", "put_volume", "call_oi", "put_oi",
    "expiries", "gex_by_strike", "computed_at", "core_sha",
)


def _day_summary_row(record) -> dict:
    return dict(zip(_DAY_SUMMARY_COLUMNS, record))


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


def upsert_day_summary(
    conn: psycopg.Connection, ticker: str, source: str, row: dict, core_sha: str
) -> None:
    """Store one ticker-day (day_summary.summarize).

    An UPSERT on the day: every collection of the day rewrites the row, so by
    the close it describes the day's last snapshot. A row per collection would
    be a different product — an intraday series nobody asked for, in a table
    whose whole purpose is "what did this day end up looking like".
    """
    conn.execute(
        """INSERT INTO ticker_day_summary (ticker, source, day, collected_at, underlying_price,
                                           net_gex, call_wall, put_wall, gamma_flip, state,
                                           expiry_count, horizon_dte, nearest_expiry, nearest_max_pain,
                                           move_lower, move_upper, contracts,
                                           call_volume, put_volume, call_oi, put_oi,
                                           expiries, gex_by_strike, computed_at, core_sha)
           VALUES (%(ticker)s, %(source)s, %(day)s, %(collected_at)s, %(underlying_price)s,
                   %(net_gex)s, %(call_wall)s, %(put_wall)s, %(gamma_flip)s, %(state)s,
                   %(expiry_count)s, %(horizon_dte)s, %(nearest_expiry)s, %(nearest_max_pain)s,
                   %(move_lower)s, %(move_upper)s, %(contracts)s,
                   %(call_volume)s, %(put_volume)s, %(call_oi)s, %(put_oi)s,
                   %(expiries)s, %(gex_by_strike)s, (now() AT TIME ZONE 'utc'), %(core_sha)s)
           ON CONFLICT (ticker, source, day) DO UPDATE
           SET collected_at = EXCLUDED.collected_at, underlying_price = EXCLUDED.underlying_price,
               net_gex = EXCLUDED.net_gex, call_wall = EXCLUDED.call_wall, put_wall = EXCLUDED.put_wall,
               gamma_flip = EXCLUDED.gamma_flip, state = EXCLUDED.state,
               expiry_count = EXCLUDED.expiry_count, horizon_dte = EXCLUDED.horizon_dte,
               nearest_expiry = EXCLUDED.nearest_expiry, nearest_max_pain = EXCLUDED.nearest_max_pain,
               move_lower = EXCLUDED.move_lower, move_upper = EXCLUDED.move_upper,
               contracts = EXCLUDED.contracts,
               call_volume = EXCLUDED.call_volume, put_volume = EXCLUDED.put_volume,
               call_oi = EXCLUDED.call_oi, put_oi = EXCLUDED.put_oi,
               expiries = EXCLUDED.expiries,
               gex_by_strike = EXCLUDED.gex_by_strike, computed_at = EXCLUDED.computed_at,
               core_sha = EXCLUDED.core_sha""",
        {
            "ticker": ticker.upper(), "source": source, "day": row["day"],
            "collected_at": row["collected_at"],
            "underlying_price": float(row["underlying_price"]), "net_gex": float(row["net_gex"]),
            "call_wall": _float_or_none(row["call_wall"]), "put_wall": _float_or_none(row["put_wall"]),
            "gamma_flip": _float_or_none(row["gamma_flip"]), "state": row["state"],
            "expiry_count": int(row["expiry_count"]), "horizon_dte": int(row["horizon_dte"]),
            "nearest_expiry": row["nearest_expiry"],
            "nearest_max_pain": _float_or_none(row["nearest_max_pain"]),
            "move_lower": _float_or_none(row["move_lower"]),
            "move_upper": _float_or_none(row["move_upper"]),
            "contracts": int(row["contracts"]),
            "call_volume": row.get("call_volume"), "put_volume": row.get("put_volume"),
            "call_oi": row.get("call_oi"), "put_oi": row.get("put_oi"),
            "expiries": Jsonb(list(row["expiries"])),
            "gex_by_strike": Jsonb(list(row["gex_by_strike"])), "core_sha": core_sha,
        },
    )
    conn.commit()


def get_day_summary(
    conn: psycopg.Connection, ticker: str, source: str, day: dt.date | None = None
) -> dict | None:
    """The row for one day — the newest day when None — or None."""
    filter_day = "AND day = %(day)s" if day is not None else ""
    record = conn.execute(
        f"""SELECT {", ".join(_DAY_SUMMARY_COLUMNS)} FROM ticker_day_summary
             WHERE ticker = %(ticker)s AND source = %(source)s {filter_day}
             ORDER BY day DESC LIMIT 1""",  # noqa: S608 — the filter is a fixed string
        {"ticker": ticker.upper(), "source": source, "day": day},
    ).fetchone()
    return None if record is None else _day_summary_row(record)


def previous_day_summary(
    conn: psycopg.Connection, ticker: str, source: str, before: dt.date
) -> dict | None:
    """The newest row strictly before a day.

    "The previous trading day" as the table knows it, which skips weekends,
    holidays and days nothing was collected without consulting a calendar —
    the row exists or it does not.
    """
    record = conn.execute(
        f"""SELECT {", ".join(_DAY_SUMMARY_COLUMNS)} FROM ticker_day_summary
             WHERE ticker = %(ticker)s AND source = %(source)s AND day < %(before)s
             ORDER BY day DESC LIMIT 1""",  # noqa: S608 — the column list is a constant
        {"ticker": ticker.upper(), "source": source, "before": before},
    ).fetchone()
    return None if record is None else _day_summary_row(record)


def list_day_summaries(
    conn: psycopg.Connection, ticker: str, source: str, limit: int = 400
) -> list[dt.date]:
    """The days on record, newest first — what the two date pickers offer."""
    rows = conn.execute(
        """SELECT day FROM ticker_day_summary
            WHERE ticker = %(ticker)s AND source = %(source)s
            ORDER BY day DESC LIMIT %(limit)s""",
        {"ticker": ticker.upper(), "source": source, "limit": limit},
    ).fetchall()
    return [r[0] for r in rows]


def get_moment_rollup(
    conn: psycopg.Connection, ticker: str, source: str, collected_at
) -> dict | None:
    """The volume-weighted implied volatility of one collection, by key.

    Only that one number: the volume and open-interest totals the comparison
    also needs are written into the day row itself, because this table does not
    carry them and re-deriving them would mean reading a whole past chain to
    sum two columns.
    """
    record = conn.execute(
        """SELECT iv_weighted_avg FROM snapshot_iv_summary
            WHERE ticker = %(ticker)s AND source = %(source)s AND collected_at = %(at)s""",
        {"ticker": ticker.upper(), "source": source, "at": collected_at},
    ).fetchone()
    if record is None:
        return None
    return {"iv_weighted_avg": record[0]}


def missing_day_summaries(
    conn: psycopg.Connection, ticker: str, source: str, days: int, core_sha: str
) -> list:
    """The collection moments of the last `days` days with no day row, a row
    built by other formulas, or a row older than the day's last collection.

    One per trading day, the day's last collection — the same per-day rule the
    rest of the product applies, expressed against the run log so that the
    answer cannot disagree with the moment list the views read.
    """
    rows = conn.execute(
        f"""WITH per_day AS (
                SELECT DISTINCT ON ({_MARKET_DATE}) started_at, {_MARKET_DATE} AS day
                  FROM collection_runs
                 WHERE ticker = %(ticker)s AND source = %(source)s
                   AND status = 'success' AND COALESCE(rows_fetched, 0) > 0
                   AND extract(isodow from {_MARKET_TIME}) <= 5
                   AND started_at >= now() - make_interval(days => %(days)s)
                 ORDER BY {_MARKET_DATE} DESC, started_at DESC
            )
            SELECT p.started_at FROM per_day p
              LEFT JOIN ticker_day_summary s
                ON s.ticker = %(ticker)s AND s.source = %(source)s AND s.day = p.day
             WHERE s.day IS NULL OR s.core_sha <> %(sha)s OR s.collected_at < p.started_at
             ORDER BY p.started_at DESC""",  # noqa: S608 — the date expressions are constants
        {"ticker": ticker.upper(), "source": source, "days": days, "sha": core_sha},
    ).fetchall()
    return [r[0] for r in rows]
