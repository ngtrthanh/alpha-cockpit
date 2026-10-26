from __future__ import annotations

from dataclasses import dataclass

from runtime_db import connect, latest_market, now_iso


@dataclass
class PaperOrder:
    track: str
    ticker: str
    side: str
    qty: int
    limit_price: float
    signal_id: int | None = None


def submit(order: PaperOrder) -> int:
    if order.qty <= 0:
        raise ValueError("qty must be positive")
    if order.side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if order.track == "A_BUY_ONLY" and order.side == "SELL":
        raise ValueError("Track A is buy-only")
    ts = now_iso()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO paper_orders(track,ticker,side,qty,limit_price,status,signal_id,created_at,updated_at) VALUES(?,?,?,?,?,'OPEN',?,?,?)",
            (order.track, order.ticker.upper(), order.side, order.qty, order.limit_price, order.signal_id, ts, ts),
        )
        return int(cur.lastrowid)


def _position(conn, track: str, ticker: str):
    row = conn.execute("SELECT * FROM paper_positions WHERE track=? AND ticker=?", (track, ticker)).fetchone()
    return dict(row) if row else {"track": track, "ticker": ticker, "qty": 0, "avg_cost": 0.0}


def _apply_fill(conn, order, qty: int, price: float, fee_rate: float = 0.0015, sell_tax_rate: float = 0.001):
    gross = qty * price
    fee = gross * fee_rate
    tax = gross * sell_tax_rate if order["side"] == "SELL" else 0.0
    account = conn.execute("SELECT * FROM paper_accounts WHERE track=?", (order["track"],)).fetchone()
    if not account:
        raise RuntimeError("paper account missing")
    pos = _position(conn, order["track"], order["ticker"])

    if order["side"] == "BUY":
        required = gross + fee
        if account["cash"] < required:
            return 0
        new_qty = pos["qty"] + qty
        new_cost = pos["avg_cost"] * pos["qty"] + gross + fee
        avg = new_cost / new_qty
        cash = account["cash"] - required
    else:
        qty = min(qty, int(pos["qty"]))
        if qty <= 0:
            return 0
        cash = account["cash"] + gross - fee - tax
        new_qty = pos["qty"] - qty
        avg = pos["avg_cost"] if new_qty else 0.0

    ts = now_iso()
    conn.execute("UPDATE paper_accounts SET cash=?,updated_at=? WHERE track=?", (cash, ts, order["track"]))
    conn.execute(
        """INSERT INTO paper_positions(track,ticker,qty,avg_cost,updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(track,ticker) DO UPDATE SET qty=excluded.qty,avg_cost=excluded.avg_cost,updated_at=excluded.updated_at""",
        (order["track"], order["ticker"], new_qty, avg, ts),
    )
    conn.execute("INSERT INTO paper_fills(order_id,ts,qty,price,fee,tax) VALUES(?,?,?,?,?,?)", (order["id"], ts, qty, price, fee, tax))
    filled = conn.execute("SELECT COALESCE(SUM(qty),0) AS q FROM paper_fills WHERE order_id=?", (order["id"],)).fetchone()["q"]
    status = "FILLED" if filled >= order["qty"] else "PARTIAL"
    conn.execute("UPDATE paper_orders SET status=?,updated_at=? WHERE id=?", (status, ts, order["id"]))
    return qty


def process_open_orders(max_participation: float = 0.05) -> int:
    fills = 0
    with connect() as conn:
        orders = conn.execute("SELECT * FROM paper_orders WHERE status IN ('OPEN','PARTIAL') ORDER BY id").fetchall()
        for order in orders:
            market = latest_market(order["ticker"])
            if not market or market.get("price") is None:
                continue
            price = float(market["price"])
            can_fill = price <= order["limit_price"] if order["side"] == "BUY" else price >= order["limit_price"]
            if not can_fill:
                continue
            already = conn.execute("SELECT COALESCE(SUM(qty),0) AS q FROM paper_fills WHERE order_id=?", (order["id"],)).fetchone()["q"]
            remaining = int(order["qty"] - already)
            if remaining <= 0:
                continue
            market_volume = float(market.get("volume") or 0)
            cap = remaining if market_volume <= 0 else max(1, int(market_volume * max_participation))
            qty = min(remaining, cap)
            fills += _apply_fill(conn, order, qty, price)
    return fills


def snapshot() -> dict:
    with connect() as conn:
        accounts = [dict(r) for r in conn.execute("SELECT * FROM paper_accounts ORDER BY track").fetchall()]
        positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE qty<>0 ORDER BY track,ticker").fetchall()]
        orders = [dict(r) for r in conn.execute("SELECT * FROM paper_orders ORDER BY id DESC LIMIT 100").fetchall()]
        fills = [dict(r) for r in conn.execute("SELECT * FROM paper_fills ORDER BY id DESC LIMIT 100").fetchall()]
        return {"accounts": accounts, "positions": positions, "orders": orders, "fills": fills}
