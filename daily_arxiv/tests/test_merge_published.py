import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_arxiv.daily_arxiv.merge_published import merge_file


class MergePublishedTests(unittest.TestCase):
    @patch("daily_arxiv.daily_arxiv.merge_published.subprocess.run")
    def test_same_day_new_versions_preserve_earlier_summaries(self, run):
        earlier = {"id": "2412.12048", "version_id": "2412.12048v2", "AI": {"tldr": "Earlier"}}
        new = {"id": "2412.12048", "version_id": "2412.12048v3", "AI": {"tldr": "New"}}
        run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps(earlier) + "\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-10-15_AI_enhanced_Chinese.jsonl"
            path.write_text(json.dumps(new) + "\n", encoding="utf-8")
            merge_file(path)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([earlier, new], rows)

    @patch("daily_arxiv.daily_arxiv.merge_published.subprocess.run")
    def test_new_result_updates_same_version_without_duplicate(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout='{"id":"2412.12048"}\n')
        new = {"id": "2412.12048", "version_id": "2412.12048v1", "AI": {"tldr": "Ready"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-10-15.jsonl"
            path.write_text(json.dumps(new) + "\n", encoding="utf-8")
            merge_file(path)
            self.assertEqual([new], [json.loads(line) for line in path.read_text().splitlines()])

    @patch("daily_arxiv.daily_arxiv.merge_published.subprocess.run")
    def test_invalid_history_does_not_overwrite_current_results(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="invalid json\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-10-15.jsonl"
            original = '{"id":"2412.12048","version_id":"2412.12048v2"}\n'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError):
                merge_file(path)
            self.assertEqual(original, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
