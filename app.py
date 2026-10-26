from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

from agent_runtime import HermesClient
from paper_broker import snapshot as paper_snapshot
from runtime_db import connect as runtime_connect, get_service_status, init_runtime_schema, watchlist

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("INVEST_DB", ROOT / "data" / "investment.db"))
STATIC = ROOT / "static"
HANDBOOK_PATH = ROOT / "data" / "handbook.json"

DEFAULT_BENCHMARK_RATE = 8.8

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  qty REAL NOT NULL CHECK(qty > 0),
  price REAL NOT NULL CHECK(price >= 0),
  fee REAL NOT NULL DEFAULT 0,
  tax REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS targets (
  ticker TEXT PRIMARY KEY,
  target_weight REAL NOT NULL,
  min_weight REAL NOT NULL,
  max_weight REAL NOT NULL,
  asset_type TEXT NOT NULL DEFAULT 'stock'
);

CREATE TABLE IF NOT EXISTS benchmarks (
  key TEXT PRIMARY KEY,
  value REAL NOT NULL,
  unit TEXT NOT NULL DEFAULT '%',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  price REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'unknown',
  UNIQUE(ticker, ts)
);

CREATE TABLE IF NOT EXISTS fundamentals (
  ticker TEXT PRIMARY KEY,
  sector TEXT NOT NULL DEFAULT 'general',
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS score_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  decision_index REAL NOT NULL,
  confidence REAL NOT NULL,
  action_band TEXT NOT NULL,
  suggested_value REAL NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL,
  source_freshness TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS score_components (
  run_id INTEGER NOT NULL REFERENCES score_runs(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  score REAL NOT NULL,
  weight REAL NOT NULL,
  details TEXT NOT NULL,
  PRIMARY KEY(run_id, name)
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  role TEXT NOT NULL,
  provider TEXT NOT NULL,
  status TEXT NOT NULL,
  input_summary TEXT NOT NULL,
  output_summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail TEXT NOT NULL
);
"""

WEIGHTS = {
    "quality": 0.18,
    "growth": 0.14,
    "valuation": 0.17,
    "momentum": 0.08,
    "financial_strength": 0.14,
    "risk": 0.12,
    "liquidity": 0.07,
    "macro": 0.10,
}

SECTOR_KPI_PACKS = {
    "general": ["revenue_growth", "eps_growth", "roe", "roic", "fcf_margin", "debt_to_equity", "pe", "pb", "ev_ebitda"],
    "bank": ["roe", "nim", "npl", "llr", "casa", "car", "credit_growth", "cost_income", "pb"],
    "technology": ["revenue_growth", "overseas_growth", "eps_growth", "operating_margin", "fcf_conversion", "recurring_revenue", "roe", "pe"],
    "steel": ["volume_growth", "hRC_spread", "gross_margin", "inventory_days", "net_debt_ebitda", "roic", "pe", "capacity_utilization"],
    "etf": ["tracking_error", "aum", "turnover", "expense_ratio", "liquidity", "index_valuation", "breadth", "drawdown"],
}

@dataclass
class Decision:
    ticker: str
    decision_index: float
    confidence: float
    status: str
    allocation_bias: str
    suggested_value: float
    suggested_qty: int
    timing_state: str
    rationale: str
    components: dict[str, float]
    source_freshness: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO benchmarks(key,value,unit,updated_at) VALUES(?,?,?,?)",
            ("deposit_rate", DEFAULT_BENCHMARK_RATE, "%/year", now_iso()),
        )
        targets = [
            ("E1VFVN30", 0.60, 0.50, 0.60, "etf"),
            ("FUEVN100", 0.20, 0.10, 0.20, "etf"),
            ("FPT", 0.0667, 0.0, 0.10, "stock"),
            ("HPG", 0.0667, 0.0, 0.10, "stock"),
            ("MBB", 0.0666, 0.0, 0.10, "stock"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO targets(ticker,target_weight,min_weight,max_weight,asset_type) VALUES(?,?,?,?,?)",
            targets,
        )
        conn.execute(
            "INSERT OR IGNORE INTO audit_log(id,ts,event_type,detail) VALUES(1,?,?,?)",
            (now_iso(), "SYSTEM_INIT", "Database initialized in live-only mode; brokerage write access absent."),
        )


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def workflow_status(score: float, confidence: float, risk_score: float) -> tuple[str, str]:
    # Status vocabulary is intentionally limited to OK / WATCH / REVIEW / ALERT.
    # allocation_bias describes direction for human review; it is not an order.
    if confidence < 0.55:
        return "REVIEW", "DATA_GAP"
    if score < 30 or risk_score < 30:
        return "ALERT", "REDUCE_TO_POLICY"
    if score < 45:
        return "REVIEW", "REDUCE_TO_POLICY"
    if score >= 80:
        return "REVIEW", "ADD_TO_TARGET"
    if score >= 65:
        return "WATCH", "ADD_TO_TARGET"
    return "OK", "HOLD_TARGET"


def compute_confidence(metrics: dict[str, Any]) -> float:
    completeness = float(metrics.get("data_completeness", 0.6))
    freshness = float(metrics.get("freshness_hours", 72))
    freshness_factor = max(0.35, math.exp(-freshness / 168.0))
    component_values = [float(metrics.get(k, 50)) for k in WEIGHTS]
    dispersion = (max(component_values) - min(component_values)) / 100.0
    agreement_factor = max(0.55, 1.0 - 0.35 * dispersion)
    return round(max(0.0, min(1.0, completeness * freshness_factor * agreement_factor)), 3)


def timing_state(score: float, confidence: float, momentum: float) -> str:
    if confidence < 0.55:
        return "DATA_WAIT"
    if score < 45:
        return "DEFENSIVE"
    if score >= 65 and momentum >= 60:
        return "FAVORABLE"
    if score >= 65:
        return "WAIT_CONFIRMATION"
    return "NEUTRAL"


def lot_quantity(value: float, price: float, lot: int = 100) -> int:
    if value <= 0 or price <= 0:
        return 0
    raw = int(value // price)
    return max(0, (raw // lot) * lot)


def xirr(cashflows: list[tuple[str, float]]) -> float | None:
    if len(cashflows) < 2 or not any(v < 0 for _, v in cashflows) or not any(v > 0 for _, v in cashflows):
        return None
    dated = [(datetime.fromisoformat(d).date(), float(v)) for d, v in cashflows]
    base = min(d for d, _ in dated)
    def npv(rate: float) -> float:
        return sum(v / ((1 + rate) ** ((d - base).days / 365.0)) for d, v in dated)
    lo, hi = -0.999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(120):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-8:
            return mid
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def position_summary(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    rows = conn.execute("SELECT * FROM transactions ORDER BY trade_date,id").fetchall()
    pos: dict[str, dict[str, float]] = {}
    for r in rows:
        p = pos.setdefault(r["ticker"], {"qty": 0.0, "cost": 0.0})
        if r["side"] == "BUY":
            p["qty"] += r["qty"]
            p["cost"] += r["qty"] * r["price"] + r["fee"] + r["tax"]
        else:
            avg = p["cost"] / p["qty"] if p["qty"] > 0 else 0
            sold = min(r["qty"], p["qty"])
            p["qty"] -= sold
            p["cost"] -= sold * avg
    return pos


def latest_prices(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """SELECT m.ticker,m.price FROM market_snapshots m
        JOIN (
          SELECT ticker,MAX(ts) ts
          FROM market_snapshots
          WHERE source<>'demo'
          GROUP BY ticker
        ) x ON x.ticker=m.ticker AND x.ts=m.ts
        WHERE m.source<>'demo'"""
    ).fetchall()
    return {r["ticker"]: float(r["price"]) for r in rows}


def compute_sizing(conn: sqlite3.Connection, ticker: str, score: float, confidence: float, equity_budget: float = 400_000_000) -> float:
    target = conn.execute("SELECT * FROM targets WHERE ticker=?", (ticker,)).fetchone()
    if not target or score < 65 or confidence < 0.55:
        return 0.0
    positions = position_summary(conn)
    prices = latest_prices(conn)
    current_value = positions.get(ticker, {}).get("qty", 0.0) * prices.get(ticker, 0.0)
    max_value = equity_budget * float(target["max_weight"])
    target_value = equity_budget * float(target["target_weight"])
    gap = max(0.0, target_value - current_value)
    # Confidence and score gate the tranche; never proposes more than half of remaining target gap.
    strength = max(0.0, min(1.0, (score - 60.0) / 30.0)) * confidence
    tranche = min(gap * 0.50, gap * strength)
    return round(min(tranche, max(0.0, max_value - current_value)), -3)


def evaluate(ticker: str, metrics: dict[str, Any] | None = None, equity_budget: float = 400_000_000) -> Decision:
    with db() as conn:
        if metrics is None:
            row = conn.execute("SELECT payload,updated_at,source FROM fundamentals WHERE ticker=?", (ticker,)).fetchone()
            if not row:
                raise KeyError(f"Unknown ticker: {ticker}")
            metrics = json.loads(row["payload"])
            freshness = row["source"]
        else:
            freshness = "manual"
        components = {k: clamp(float(metrics.get(k, 50))) for k in WEIGHTS}
        score = round(sum(components[k] * WEIGHTS[k] for k in WEIGHTS), 1)
        confidence = compute_confidence(metrics)
        status, bias = workflow_status(score, confidence, components["risk"])
        sizing = compute_sizing(conn, ticker, score, confidence, equity_budget)
        prices = latest_prices(conn)
        qty = lot_quantity(sizing, prices.get(ticker, 0.0))
        timing = timing_state(score, confidence, components["momentum"])
        ordered = sorted(components.items(), key=lambda x: x[1], reverse=True)
        top = ", ".join(f"{k} {v:.0f}" for k, v in ordered[:2])
        weak = ", ".join(f"{k} {v:.0f}" for k, v in ordered[-2:])
        rationale = f"Strengths: {top}. Weakest: {weak}. Score is deterministic; confidence reflects data completeness/freshness and component disagreement."
        cur = conn.execute(
            "INSERT INTO score_runs(ticker,ts,decision_index,confidence,action_band,suggested_value,rationale,source_freshness) VALUES(?,?,?,?,?,?,?,?)",
            (ticker, now_iso(), score, confidence, status, sizing, rationale, freshness),
        )
        run_id = cur.lastrowid
        for name, val in components.items():
            conn.execute(
                "INSERT INTO score_components(run_id,name,score,weight,details) VALUES(?,?,?,?,?)",
                (run_id, name, val, WEIGHTS[name], "deterministic score component"),
            )
        conn.execute(
            "INSERT INTO audit_log(ts,event_type,detail) VALUES(?,?,?)",
            (now_iso(), "SCORE_RUN", f"{ticker}: index={score}, confidence={confidence}, status={status}, bias={bias}, timing={timing}, suggested_value={sizing}, suggested_qty={qty}"),
        )
        return Decision(ticker, score, confidence, status, bias, sizing, qty, timing, rationale, components, freshness)


def run_agent(role: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = HermesClient()
    role = role.lower()
    prompt = json.dumps(payload, ensure_ascii=False, default=str)
    if role == "hermes":
        result = client.supervisor(prompt)
    elif role == "codex":
        result = client.codex(prompt)
    elif role == "claude":
        result = client.claude(prompt)
    else:
        raise ValueError("role must be hermes, codex or claude")
    return {
        "role": result.role,
        "provider": result.provider,
        "model": result.model,
        "status": "ok",
        "output": result.content,
        "usage": result.usage,
    }

def portfolio_payload() -> dict[str, Any]:
    with db() as conn:
        pos = position_summary(conn)
        prices = latest_prices(conn)
        target_rows = conn.execute("SELECT * FROM targets ORDER BY asset_type,ticker").fetchall()
        targets = [dict(r) for r in target_rows]
        holdings = []
        total = 0.0
        for ticker, p in pos.items():
            value = p["qty"] * prices.get(ticker, 0.0)
            total += value
            avg = p["cost"] / p["qty"] if p["qty"] else 0
            holdings.append({"ticker": ticker, "qty": p["qty"], "avg_cost": avg, "price": prices.get(ticker, 0), "value": value})
        for h in holdings:
            h["weight"] = h["value"] / total if total else 0.0
        benchmark = conn.execute("SELECT * FROM benchmarks WHERE key='deposit_rate'").fetchone()
        flows = []
        tx_rows = conn.execute("SELECT trade_date,side,qty,price,fee,tax FROM transactions ORDER BY trade_date,id").fetchall()
        for r in tx_rows:
            gross = float(r["qty"]) * float(r["price"])
            if r["side"] == "BUY":
                flows.append((r["trade_date"], -(gross + float(r["fee"]) + float(r["tax"]))))
            else:
                flows.append((r["trade_date"], gross - float(r["fee"]) - float(r["tax"])))
        if total > 0 and flows:
            flows.append((datetime.now(timezone.utc).date().isoformat(), total))
        irr = xirr(flows) if flows else None
        return {
            "holdings": holdings,
            "targets": targets,
            "market_prices": prices,
            "market_value": total,
            "xirr": irr,
            "equity_budget": 400_000_000,
            "deposit_rate": float(benchmark["value"]) if benchmark else DEFAULT_BENCHMARK_RATE,
            "rules": {
                "max_single_etf": 0.60,
                "max_single_stock": 0.10,
                "max_individual_stocks": 0.20,
                "margin": 0,
                "derivatives": 0,
                "review": "weekly",
                "deep_rebalance": "6 months",
            },
        }


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        if path == "/":
            path = "/index.html"
        clean = path.split("?", 1)[0].lstrip("/")
        return str(STATIC / clean)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(size) if size else b"{}"
        return json.loads(raw.decode())

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/health":
                return self.json_response({"ok": True, "mode": "live", "demo_data": False, "db": str(DB_PATH), "broker_write_access": False, "ts": now_iso()})
            if path == "/api/handbook":
                return self.json_response(json.loads(HANDBOOK_PATH.read_text(encoding="utf-8")))
            if path == "/api/portfolio":
                return self.json_response(portfolio_payload())
            if path == "/api/runtime":
                with runtime_connect() as rconn:
                    reports = [dict(r) for r in rconn.execute("SELECT id,ts,role,ticker,provider,model,status FROM agent_reports ORDER BY id DESC LIMIT 30").fetchall()]
                return self.json_response({"services": get_service_status(), "watchlist": watchlist(), "agent_reports": reports})
            if path == "/api/paper":
                return self.json_response(paper_snapshot())
            if path == "/api/scores":
                with db() as conn:
                    tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM fundamentals WHERE source<>'demo' ORDER BY ticker").fetchall()]
                results = []
                for ticker in tickers:
                    try:
                        results.append(asdict(evaluate(ticker)))
                    except Exception:
                        continue
                return self.json_response(results)
            if path == "/api/audit":
                with db() as conn:
                    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 80").fetchall()
                    return self.json_response([dict(r) for r in rows])
            if path == "/api/kpi-packs":
                return self.json_response(SECTOR_KPI_PACKS)
            return super().do_GET()
        except Exception as e:
            return self.json_response({"error": str(e)}, 500)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            data = self.read_json()
            if path == "/api/score":
                ticker = str(data.get("ticker", "MANUAL")).upper()
                metrics = data.get("metrics")
                budget = float(data.get("equity_budget", 400_000_000))
                result = evaluate(ticker, metrics, budget)
                return self.json_response(asdict(result))
            if path == "/api/transactions":
                required = ["trade_date", "ticker", "side", "qty", "price"]
                if any(k not in data for k in required):
                    return self.json_response({"error": "missing required fields"}, 400)
                if data["side"] not in {"BUY", "SELL"}:
                    return self.json_response({"error": "side must be BUY or SELL"}, 400)
                with db() as conn:
                    conn.execute(
                        "INSERT INTO transactions(trade_date,ticker,side,qty,price,fee,tax,note) VALUES(?,?,?,?,?,?,?,?)",
                        (data["trade_date"], str(data["ticker"]).upper(), data["side"], float(data["qty"]), float(data["price"]), float(data.get("fee", 0)), float(data.get("tax", 0)), str(data.get("note", ""))),
                    )
                    conn.execute("INSERT INTO audit_log(ts,event_type,detail) VALUES(?,?,?)", (now_iso(), "TRANSACTION_IMPORT", json.dumps(data)))
                return self.json_response({"ok": True, "portfolio": portfolio_payload()}, 201)
            if path == "/api/agent/run":
                return self.json_response(run_agent(str(data.get("role", "hermes")), data.get("payload", {})))
            return self.json_response({"error": "not found"}, 404)
        except (ValueError, KeyError) as e:
            return self.json_response({"error": str(e)}, 400)
        except Exception as e:
            return self.json_response({"error": str(e)}, 500)


def main() -> None:
    init_db()
    init_runtime_schema()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "6868"))
    print(f"Alpha Cockpit listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
