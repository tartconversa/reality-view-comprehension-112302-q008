from __future__ import annotations

import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaterialTests(unittest.TestCase):
    def test_files_have_declared_headers_and_rows(self):
        expected = EXPECTED
        for filename, (columns, minimum_rows) in expected.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertGreaterEqual(len(rows), minimum_rows)
                self.assertTrue(all(None not in row for row in rows))


EXPECTED = {'view_sessions.csv': (['session_id', 'driver_key', 'ui_version', 'started_at', 'exposure_state', 'route_intent_version', 'task_type'], 50), 'rendered_objects.csv': (['session_id', 'object_id', 'object_type', 'frame_time', 'source_version', 'confidence_band'], 60), 'interactions.csv': (['interaction_id', 'session_id', 'target_id', 'action', 'action_time', 'task_outcome'], 44), 'study_responses.csv': (['response_id', 'session_id', 'questionnaire', 'submitted_at', 'coded_intent', 'review_state'], 23)}


if __name__ == "__main__":
    unittest.main()
