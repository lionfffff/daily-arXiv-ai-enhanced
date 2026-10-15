import subprocess
import unittest
from unittest.mock import patch

from daily_arxiv.daily_arxiv.check_stats import load_history_ids


class HistoryTests(unittest.TestCase):
    @patch("daily_arxiv.daily_arxiv.check_stats.os.path.exists", return_value=False)
    @patch("daily_arxiv.daily_arxiv.check_stats.subprocess.run")
    def test_reads_previous_papers_from_data_branch(self, run, _exists):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"id":"2401.00001"}\n{"id":"2401.00002"}\n'
        )
        self.assertEqual({"2401.00001", "2401.00002"}, load_history_ids("2026-10-14"))
        self.assertEqual(
            ["git", "show", "origin/data:data/2026-10-14.jsonl"],
            run.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
