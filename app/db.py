"""The single access point to the database. Nothing outside this module opens
a connection directly."""

from __future__ import annotations

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

    rows = [
        (
            ticker,
            collected_at,
            underlying_price,
            row.expiry,
            row.strike,
            row.option_type,
            row.last_price,
            row.bid,
            row.ask,
            row.volume,
            row.open_interest,
            row.implied_volatility,
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


def get_snapshots(
    conn: psycopg.Connection, ticker: str, days: int | None = config.SNAPSHOT_HISTORY_DAYS
) -> pd.DataFrame:
    """A ticker's snapshot history, bounded to the last `days` by default.

    `days=None` fetches everything — used by the dashboard's "Load full
    history" toggle, and by tooling that genuinely needs the whole series. No
    UI path should pass None by default: see config.SNAPSHOT_HISTORY_DAYS for
    why the bound exists."""
    where = "ticker = %s"
    params: list = [ticker]
    if days is not None:
        where += " AND collected_at >= now() - make_interval(days => %s)"
        params.append(days)
    return pd.read_sql_query(
        f"SELECT * FROM option_snapshots WHERE {where} ORDER BY collected_at",
        conn,
        params=params,
        parse_dates=["collected_at", "expiry"],
    )


def get_put_call_ratio(
    conn: psycopg.Connection, ticker: str, days: int | None = config.SNAPSHOT_HISTORY_DAYS
) -> pd.DataFrame:
    """Put/call ratio per collection, aggregated in SQL.

    The Overview chart is a few hundred points. Computing it by loading every
    raw row and grouping in pandas is the single most expensive thing a page
    load did — measured on the hosted sibling at 5.0s and ~390MB of DataFrame
    for one liquid ticker, against 0.8s and a few kilobytes for the same
    numbers aggregated here."""
    where = "ticker = %s"
    params: list = [ticker]
    if days is not None:
        where += " AND collected_at >= now() - make_interval(days => %s)"
        params.append(days)
    raw = pd.read_sql_query(
        f"""SELECT collected_at, option_type,
                   SUM(volume) AS volume, SUM(open_interest) AS open_interest
            FROM option_snapshots WHERE {where}
            GROUP BY collected_at, option_type
            ORDER BY collected_at""",
        conn,
        params=params,
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


def get_snapshot_dates(conn: psycopg.Connection, ticker: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT collected_at FROM option_snapshots WHERE ticker = %s ORDER BY collected_at",
        (ticker,),
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
) -> None:
    """`rows_fetched`/`oi_zero_fraction` — diagnostics for the collection log
    (dashboard sidebar): how many chain rows actually arrived and what fraction
    of open_interest came back as zero. Written regardless of status — they
    reveal not just outright failures but also "successful" yet suspect
    collections (see spec FR23)."""
    conn.execute(
        """INSERT INTO collection_runs
           (started_at, finished_at, ticker, status, error_message, rows_fetched, oi_zero_fraction)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            started_at,
            finished_at,
            ticker,
            status,
            error_message,
            rows_fetched,
            oi_zero_fraction,
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
