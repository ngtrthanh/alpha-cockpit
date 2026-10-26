from __future__ import annotations

import dataclasses
import os
import threading
import time
from typing import Any

from runtime_db import record_market_event, set_service_status, watchlist

SSI_CLIENT_ID = os.getenv("SSI_CLIENT_ID", "")
SSI_API_KEY = os.getenv("SSI_API_KEY", "")
SSI_API_SECRET = os.getenv("SSI_API_SECRET", "")


def configured() -> bool:
    return bool(SSI_CLIENT_ID and SSI_API_KEY and SSI_API_SECRET)


def _asdict(msg: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(msg):
        return dataclasses.asdict(msg)
    if hasattr(msg, "__dict__"):
        return {k: v for k, v in vars(msg).items() if not k.startswith("_")}
    return {"repr": repr(msg)}


def _num(obj: Any, *names: str) -> float | None:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def run_stream_forever(stop_event: threading.Event | None = None) -> None:
    if not configured():
        set_service_status("ssi_market", "missing_config", error="SSI_CLIENT_ID / SSI_API_KEY / SSI_API_SECRET required")
        return

    from ssi_sdk import Auth, Config, Stream
    from ssi_sdk.models.streaming import QuoteMessage, TradeMessage

    symbols = watchlist()
    if not symbols:
        set_service_status("ssi_market", "idle", detail={"watchlist": []})
        return

    config = Config(
        client_id=SSI_CLIENT_ID,
        api_key=SSI_API_KEY,
        api_secret=SSI_API_SECRET,
    )

    while not (stop_event and stop_event.is_set()):
        try:
            with Auth(config) as auth:
                auth.ensure_authenticated()
                with Stream(auth) as stream:
                    ws = stream.streaming

                    def on_connection(event):
                        state = getattr(event, "state", "unknown")
                        set_service_status("ssi_market", "ok" if state in {"connected", "reconnected"} else state, detail={"state": state, "symbols": symbols})

                    def on_data(msg):
                        raw = _asdict(msg)
                        ticker = str(getattr(msg, "symbol", "") or "").upper()
                        if not ticker:
                            return
                        if isinstance(msg, TradeMessage):
                            price = _num(msg, "price", "last_price", "match_price")
                            volume = _num(msg, "quantity", "volume", "match_quantity")
                            record_market_event(ticker, price, volume=volume, source="ssi-fastconnect-v3", kind="trade", raw=raw)
                        elif isinstance(msg, QuoteMessage):
                            bids = getattr(msg, "bid_prices", None) or []
                            asks = getattr(msg, "ask_prices", None) or []
                            bid = float(bids[0]) if bids else None
                            ask = float(asks[0]) if asks else None
                            mid = (bid + ask) / 2 if bid is not None and ask is not None else bid or ask
                            record_market_event(ticker, mid, bid=bid, ask=ask, source="ssi-fastconnect-v3", kind="quote", raw=raw)

                    ws.on_connection = on_connection
                    ws.on_data = on_data
                    ws.connect()
                    ws.subscribe_symbol_trade(symbols)
                    ws.subscribe_symbol_quote(symbols)
                    ws.ping(interval_ms=30000)
                    set_service_status("ssi_market", "ok", detail={"symbols": symbols, "mode": "websocket"})
                    while not (stop_event and stop_event.is_set()):
                        ws.wait(timeout_ms=30000)
        except Exception as e:
            set_service_status("ssi_market", "error", error=str(e), detail={"retry_seconds": 10})
            if stop_event and stop_event.wait(10):
                break
            time.sleep(10)
