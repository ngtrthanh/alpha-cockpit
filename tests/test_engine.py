import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = tempfile.TemporaryDirectory()
os.environ["INVEST_DB"] = str(Path(TMP.name) / "test.db")

import app
from paper_broker import PaperOrder, process_open_orders, submit
from runtime_db import connect, init_runtime_schema, record_market_event


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        init_runtime_schema()

    def test_score_is_deterministic(self):
        metrics = {k: 70 for k in app.WEIGHTS}
        metrics.update(data_completeness=1.0, freshness_hours=0)
        a = app.evaluate("TEST", metrics)
        b = app.evaluate("TEST", metrics)
        self.assertEqual(a.decision_index, 70.0)
        self.assertEqual(a.decision_index, b.decision_index)

    def test_low_confidence_gates_action(self):
        metrics = {k: 90 for k in app.WEIGHTS}
        metrics.update(data_completeness=0.30, freshness_hours=500)
        d = app.evaluate("TEST", metrics)
        self.assertEqual(d.status, "REVIEW")
        self.assertEqual(d.allocation_bias, "DATA_GAP")
        self.assertEqual(d.suggested_value, 0)

    def test_no_demo_data_seeded_by_default(self):
        with connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM fundamentals WHERE source='demo'").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_market_data_disabled_by_default(self):
        import worker
        self.assertEqual(worker.MARKET_DATA_PROVIDER, "none")
        self.assertFalse(worker.PAPER_ENABLED)

    def test_track_a_rejects_sell(self):
        with self.assertRaises(ValueError):
            submit(PaperOrder("A_BUY_ONLY", "TST", "SELL", 100, 10_000))

    def test_paper_buy_fills_from_real_market_event_table(self):
        record_market_event("TST", 10_000, volume=10_000, source="test-live-feed", kind="trade")
        order_id = submit(PaperOrder("A_BUY_ONLY", "TST", "BUY", 100, 10_000))
        process_open_orders()
        with connect() as conn:
            order = conn.execute("SELECT status FROM paper_orders WHERE id=?", (order_id,)).fetchone()
            pos = conn.execute("SELECT qty FROM paper_positions WHERE track='A_BUY_ONLY' AND ticker='TST'").fetchone()
        self.assertEqual(order["status"], "FILLED")
        self.assertEqual(pos["qty"], 100)

    def test_no_broker_write_route(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/broker/order", source)
        self.assertNotIn("PLACE_ORDER", source)


if __name__ == "__main__":
    unittest.main()
