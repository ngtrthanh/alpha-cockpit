from __future__ import annotations

import json
import os
import threading
import time

import app
from agent_runtime import HermesClient
from paper_broker import PaperOrder, process_open_orders, submit
from fundamental_research import needs_refresh, research_ticker
from runtime_db import connect, init_runtime_schema, latest_market, now_iso, set_service_status, watchlist
from ssi_market import run_stream_forever

PAPER_ENABLED = os.getenv("PAPER_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
AGENT_INTERVAL_SECONDS = int(os.getenv("AGENT_INTERVAL_SECONDS", "3600"))
PAPER_INTERVAL_SECONDS = int(os.getenv("PAPER_INTERVAL_SECONDS", "5"))


def create_signals() -> int:
    made = 0
    for ticker in watchlist():
        with connect() as conn:
            fundamental = conn.execute("SELECT payload FROM fundamentals WHERE ticker=? AND source<>'demo'", (ticker,)).fetchone()
        if not fundamental:
            continue
        try:
            decision = app.evaluate(ticker)
        except Exception:
            continue
        direction = "BUY_CANDIDATE" if decision.decision_index >= 65 and decision.confidence >= 0.55 else "HOLD"
        if decision.decision_index < 45 and decision.confidence >= 0.55:
            direction = "REDUCE_REVIEW"
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO signals(ticker,ts,score,confidence,direction,rationale) VALUES(?,?,?,?,?,?)",
                (ticker, now_iso(), decision.decision_index, decision.confidence, direction, decision.rationale),
            )
            signal_id = int(cur.lastrowid)
        made += 1

        market = latest_market(ticker)
        if not PAPER_ENABLED or not market or not decision.suggested_qty:
            continue
        price = float(market["price"])
        if direction == "BUY_CANDIDATE":
            limit = float(market.get("ask") or price)
            for track in ("A_BUY_ONLY", "B_FULL_CYCLE"):
                submit(PaperOrder(track, ticker, "BUY", decision.suggested_qty, limit, signal_id))
        elif direction == "REDUCE_REVIEW":
            with connect() as conn:
                pos = conn.execute("SELECT qty FROM paper_positions WHERE track='B_FULL_CYCLE' AND ticker=?", (ticker,)).fetchone()
            if pos and pos["qty"] > 0:
                limit = float(market.get("bid") or price)
                submit(PaperOrder("B_FULL_CYCLE", ticker, "SELL", int(pos["qty"]), limit, signal_id))
    return made


def refresh_fundamentals(client: HermesClient) -> None:
    for ticker in watchlist():
        if not needs_refresh(ticker):
            continue
        try:
            research_ticker(client, ticker)
        except Exception as e:
            set_service_status("fundamental_research", "error", error=f"{ticker}: {e}")


def committee_cycle(client: HermesClient) -> None:
    try:
        client.health()
    except Exception:
        return
    for ticker in watchlist():
        with connect() as conn:
            f = conn.execute("SELECT * FROM fundamentals WHERE ticker=? AND source<>'demo'", (ticker,)).fetchone()
            m = latest_market(ticker)
            recent = conn.execute("SELECT * FROM score_runs WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        if not f or not m:
            continue
        context = {
            "ticker": ticker,
            "market": m,
            "fundamentals": json.loads(f["payload"]),
            "fundamentals_source": f["source"],
            "fundamentals_updated_at": f["updated_at"],
            "latest_score": dict(recent) if recent else None,
            "policy": {"broker_write": False, "margin": 0, "derivatives": 0, "deposit_hurdle_pct": 8.8},
        }
        try:
            client.committee(context, ticker=ticker)
        except Exception as e:
            set_service_status("agent_committee", "error", error=f"{ticker}: {e}")
            continue
    set_service_status("agent_committee", "ok")


def main() -> None:
    app.init_db()
    init_runtime_schema()
    stop = threading.Event()
    market_thread = threading.Thread(target=run_stream_forever, args=(stop,), name="ssi-market", daemon=True)
    market_thread.start()
    client = HermesClient()
    last_agents = 0.0
    set_service_status("worker", "ok", detail={"paper": PAPER_ENABLED})
    try:
        while True:
            if PAPER_ENABLED:
                process_open_orders()
            now = time.time()
            if now - last_agents >= AGENT_INTERVAL_SECONDS:
                refresh_fundamentals(client)
                create_signals()
                committee_cycle(client)
                last_agents = now
            time.sleep(PAPER_INTERVAL_SECONDS)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
