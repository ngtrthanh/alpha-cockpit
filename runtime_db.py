from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("INVEST_DB", ROOT / "data" / "investment.db"))

RUNTIME_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS service_status (
  service TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  last_ok TEXT,
  last_error TEXT,
  detail TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
  ticker TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  asset_type TEXT NOT NULL DEFAULT 'stock',
  exchange TEXT NOT NULL DEFAULT 'HOSE',
  sector TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS market_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  price REAL,
  volume REAL,
  bid REAL,
  ask REAL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_market_events_ticker_ts
ON market_events(ticker, ts DESC);

CREATE TABLE IF NOT EXISTS research_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  payload TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  score REAL NOT NULL,
  confidence REAL NOT NULL,
  direction TEXT NOT NULL,
  rationale TEXT NOT NULL,
  source_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS paper_accounts (
  track TEXT PRIMARY KEY,
  initial_cash REAL NOT NULL,
  cash REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  track TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  qty INTEGER NOT NULL,
  limit_price REAL NOT NULL,
  status TEXT NOT NULL,
  signal_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  fee REAL NOT NULL DEFAULT 0,
  tax REAL NOT NULL DEFAULT 0,
  FOREIGN KEY(order_id) REFERENCES paper_orders(id)
);

CREATE TABLE IF NOT EXISTS paper_positions (
  track TEXT NOT NULL,
  ticker TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(track, ticker)
);

CREATE TABLE IF NOT EXISTS agent_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,
  ticker TEXT,
  provider TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL,
  content TEXT NOT NULL,
  usage_json TEXT NOT NULL DEFAULT '{}'
);
"""

DEFAULT_WATCHLIST = [
    ("E1VFVN30", 1, "etf", "HOSE", "ETF"),
    ("FUEVN100", 1, "etf", "HOSE", "ETF"),
    ("FPT", 1, "stock", "HOSE", "Technology"),
    ("HPG", 1, "stock", "HOSE", "Steel"),
    ("MBB", 1, "stock", "HOSE", "Bank"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_runtime_schema(initial_cash: float = 400_000_000) -> None:
    with connect() as conn:
        conn.executescript(RUNTIME_SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist(ticker,enabled,asset_type,exchange,sector) VALUES(?,?,?,?,?)",
            DEFAULT_WATCHLIST,
        )
        ts = now_iso()
        for track in ("A_BUY_ONLY", "B_FULL_CYCLE"):
            conn.execute(
                "INSERT OR IGNORE INTO paper_accounts(track,initial_cash,cash,updated_at) VALUES(?,?,?,?)",
                (track, initial_cash, initial_cash, ts),
            )


def set_service_status(service: str, status: str, *, error: str | None = None, detail: dict[str, Any] | None = None) -> None:
    ts = now_iso()
    with connect() as conn:
        old = conn.execute("SELECT last_ok FROM service_status WHERE service=?", (service,)).fetchone()
        last_ok = ts if status == "ok" else (old["last_ok"] if old else None)
        conn.execute(
            """INSERT INTO service_status(service,status,last_ok,last_error,detail,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(service) DO UPDATE SET
                 status=excluded.status,last_ok=excluded.last_ok,last_error=excluded.last_error,
                 detail=excluded.detail,updated_at=excluded.updated_at""",
            (service, status, last_ok, error, json.dumps(detail or {}, ensure_ascii=False), ts),
        )


def get_service_status() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM service_status ORDER BY service").fetchall()]


def watchlist() -> list[str]:
    with connect() as conn:
        return [r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist WHERE enabled=1 ORDER BY ticker").fetchall()]


def record_market_event(
    ticker: str,
    price: float | None,
    *,
    volume: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    source: str = "ssi",
    kind: str = "trade",
    raw: dict[str, Any] | None = None,
    ts: str | None = None,
) -> None:
    ts = ts or now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO market_events(ticker,ts,price,volume,bid,ask,source,kind,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (ticker.upper(), ts, price, volume, bid, ask, source, kind, json.dumps(raw or {}, ensure_ascii=False, default=str)),
        )
        if price is not None:
            conn.execute(
                "INSERT OR IGNORE INTO market_snapshots(ticker,ts,price,source) VALUES(?,?,?,?)",
                (ticker.upper(), ts, float(price), source),
            )


def latest_market(ticker: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM market_events WHERE ticker=? AND price IS NOT NULL ORDER BY ts DESC,id DESC LIMIT 1",
            (ticker.upper(),),
        ).fetchone()
        return dict(row) if row else None


def save_agent_report(role: str, provider: str, model: str | None, status: str, content: str, *, ticker: str | None = None, usage: dict[str, Any] | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO agent_reports(ts,role,ticker,provider,model,status,content,usage_json) VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), role, ticker, provider, model, status, content, json.dumps(usage or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)
