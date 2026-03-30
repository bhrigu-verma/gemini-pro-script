import json
import tempfile
import unittest
from pathlib import Path

from scripts.batch_processor import CompletionController, controller_from_history


class CompletionControllerTests(unittest.TestCase):
    def test_stops_when_target_reached(self):
        c = CompletionController(target_count=5, max_iterations=10, max_empty_streak=3)
        c.register_round(3)
        c.register_round(2)
        self.assertFalse(c.should_continue())
        self.assertEqual(c.stop_reason, "target_reached")

    def test_stops_on_max_iterations(self):
        c = CompletionController(target_count=100, max_iterations=2, max_empty_streak=3)
        c.register_round(1)
        c.register_round(1)
        self.assertFalse(c.should_continue())
        self.assertEqual(c.stop_reason, "max_iterations_reached")

    def test_stops_on_empty_streak(self):
        c = CompletionController(target_count=100, max_iterations=10, max_empty_streak=2)
        c.register_round(0)
        c.register_round(0)
        self.assertFalse(c.should_continue())
        self.assertEqual(c.stop_reason, "max_empty_streak_reached")

    def test_controller_from_history_restores_accepted_total(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "history.json"
            p.write_text(json.dumps({"records": [{"url": "u1"}, {"url": "u2"}]}), encoding="utf-8")
            c, state = controller_from_history(p, target_count=10, max_iterations=5, max_empty_streak=2)
            self.assertIsInstance(state, dict)
            self.assertEqual(c.accepted_total, 2)


if __name__ == "__main__":
    unittest.main()
