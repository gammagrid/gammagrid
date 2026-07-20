"""Единственная точка доступа к SQLite. Ничего, кроме этого модуля,
не открывает соединение с БД напрямую."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import pandas as pd

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    ticker TEXT PRIMARY KEY,
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS option_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    underlying_price REAL NOT NULL,
    expiry DATE NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL CHECK (option_type IN ('call','put')),
    last_price REAL,
    bid REAL,
    ask REAL,
    volume INTEGER,
    open_interest INTEGER,
    implied_volatility REAL,
    in_the_money BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_date ON option_snapshots(ticker, collected_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_contract ON option_snapshots(ticker, expiry, strike, option_type);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success','failed')),
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS tracked_contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    expiry DATE NOT NULL,
    strike REAL NOT NULL,
    option_type TEXT NOT NULL CHECK (option_type IN ('call','put')),
    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, expiry, strike, option_type)
);
"""


# Аддитивные миграции для уже существующих БД — CREATE TABLE IF NOT EXISTS
# в SCHEMA не добавляет колонки в таблицу, которая уже была создана раньше
# без них. ADD COLUMN не поддерживает IF NOT EXISTS во всех версиях SQLite,
# поэтому идемпотентность — через отлов OperationalError на повторный запуск.
MIGRATIONS = [
    "ALTER TABLE collection_runs ADD COLUMN rows_fetched INTEGER",
    "ALTER TABLE collection_runs ADD COLUMN oi_zero_fraction REAL",
]


def get_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # колонка уже добавлена в предыдущем запуске
    conn.commit()
    return conn


# --- watchlist ---

def add_ticker(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("INSERT OR IGNORE INTO watchlist (ticker) VALUES (?)", (ticker.upper(),))
    conn.commit()


def remove_ticker(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))
    conn.commit()


def get_watchlist(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
    return [r[0] for r in rows]


# --- snapshots ---

def insert_snapshot(
    conn: sqlite3.Connection,
    ticker: str,
    collected_at: datetime,
    underlying_price: float,
    chain_df: pd.DataFrame,
) -> None:
    """chain_df: колонки expiry, strike, option_type, last_price, bid, ask,
    volume, open_interest, implied_volatility, in_the_money (см. collector.py)."""
    rows = [
        (
            ticker,
            collected_at.isoformat(),
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
        )
        for row in chain_df.itertuples()
    ]
    conn.executemany(
        """INSERT INTO option_snapshots
           (ticker, collected_at, underlying_price, expiry, strike, option_type,
            last_price, bid, ask, volume, open_interest, implied_volatility, in_the_money)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def get_snapshots(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM option_snapshots WHERE ticker = ? ORDER BY collected_at",
        conn,
        params=(ticker,),
        parse_dates=["collected_at", "expiry"],
    )


def get_snapshot_dates(conn: sqlite3.Connection, ticker: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT collected_at FROM option_snapshots WHERE ticker = ? ORDER BY collected_at",
        (ticker,),
    ).fetchall()
    return [r[0] for r in rows]


# --- collection runs ---

def log_run(
    conn: sqlite3.Connection,
    started_at: datetime,
    finished_at: datetime,
    ticker: str,
    status: str,
    error_message: str | None = None,
    rows_fetched: int | None = None,
    oi_zero_fraction: float | None = None,
) -> None:
    """`rows_fetched`/`oi_zero_fraction` — диагностика для лога сборов (сайдбар
    дашборда): сколько строк цепочки реально пришло и какая доля open_interest
    оказалась нулевой. Пишутся независимо от статуса — по ним видно не только
    явные сбои, но и "успешные", но подозрительные сборы (см. FR23, ТЗ)."""
    conn.execute(
        """INSERT INTO collection_runs
           (started_at, finished_at, ticker, status, error_message, rows_fetched, oi_zero_fraction)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            started_at.isoformat(),
            finished_at.isoformat(),
            ticker,
            status,
            error_message,
            rows_fetched,
            oi_zero_fraction,
        ),
    )
    conn.commit()


def get_recent_runs(conn: sqlite3.Connection, limit: int = 50) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT ?",
        conn,
        params=(limit,),
    )


# --- tracked contracts (ТЗ, FR14) ---

def add_tracked_contract(
    conn: sqlite3.Connection, ticker: str, expiry, strike: float, option_type: str
) -> None:
    expiry_str = pd.Timestamp(expiry).strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR IGNORE INTO tracked_contracts (ticker, expiry, strike, option_type)
           VALUES (?, ?, ?, ?)""",
        (ticker.upper(), expiry_str, float(strike), option_type),
    )
    conn.commit()


def remove_tracked_contract(conn: sqlite3.Connection, contract_id: int) -> None:
    conn.execute("DELETE FROM tracked_contracts WHERE id = ?", (contract_id,))
    conn.commit()


def get_tracked_contracts(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM tracked_contracts WHERE ticker = ? ORDER BY expiry, strike",
        conn,
        params=(ticker.upper(),),
        parse_dates=["expiry"],
    )
