"""The single access point to the database. Nothing outside this module opens
a connection directly."""

from __future__ import annotations

import datetime as dt
import warnings
from datetime import datetime

import pandas as pd
import psycopg

from app import config, migrate

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
        # The moment's weighted-average IV, computed here and read back from
        # the rows just written rather than recomputed from chain_df in Python:
        # the number then has one definition instead of two that must be kept
        # in step. Reading option_snapshots alone is correct at collection time
        # and would not be in a rebuild — nothing has been archived yet.
        cur.execute(
            f"""INSERT INTO snapshot_iv_summary (ticker, source, collected_at, iv_weighted_avg)
                SELECT %(ticker)s, %(source)s, %(at)s, {_IV_WEIGHTED_AVG_SQL}
                FROM option_snapshots
                WHERE ticker = %(ticker)s AND source = %(source)s AND collected_at = %(at)s
                ON CONFLICT (ticker, source, collected_at)
                DO UPDATE SET iv_weighted_avg = EXCLUDED.iv_weighted_avg""",  # noqa: S608
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
    passes_per_month = (60 / interval_minutes) * 24 * 30
    return rows_per_pass * passes_per_month * config.BYTES_PER_SNAPSHOT_ROW / 1_000_000


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
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO option_snapshots_archive
               SELECT * FROM option_snapshots
               WHERE expiry < current_date - make_interval(days => %s)""",
            (grace_days,),
        )
        moved = cur.rowcount
        cur.execute(
            "DELETE FROM option_snapshots WHERE expiry < current_date - make_interval(days => %s)",
            (grace_days,),
        )
    return moved


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
        statement = f"""
            SELECT DISTINCT ON (started_at::date) started_at
            FROM collection_runs WHERE {where}
            ORDER BY started_at::date DESC, started_at DESC
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
    return bool(row and row[0] and row[0] >= dt.date.today() - dt.timedelta(days=1))


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
