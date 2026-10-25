import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = tempfile.TemporaryDirectory()
os.environ["INVEST_DB"] = str(Path(TMP.name) / "test.db")

import app

class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()

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

    def test_no_broker_write_route(self):
        # Architectural assertion: server exposes transaction import and analysis, not broker order submission.
        source = Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/broker/order", source)
        self.assertNotIn("PLACE_ORDER", source)

if __name__ == "__main__":
    unittest.main()
