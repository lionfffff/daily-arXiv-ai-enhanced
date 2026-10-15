import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from daily_arxiv.daily_arxiv.check_stats import (
    load_history_ids,
    paper_version_id,
    perform_deduplication,
)


class HistoryTests(unittest.TestCase):
    @patch("daily_arxiv.daily_arxiv.check_stats.os.path.exists", return_value=False)
    @patch("daily_arxiv.daily_arxiv.check_stats.subprocess.run")
    def test_reads_previous_papers_from_data_branch(self, run, _exists):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"id":"2401.00001"}\n{"id":"2401.00002","version_id":"2401.00002v2"}\n'
        )
        self.assertEqual({"2401.00001v1", "2401.00002v2"}, load_history_ids("2026-10-14"))
        self.assertEqual(
            ["git", "show", "origin/data:data/2026-10-14.jsonl"],
            run.call_args.args[0],
        )

    @patch("daily_arxiv.daily_arxiv.check_stats.os.path.exists", return_value=True)
    @patch("daily_arxiv.daily_arxiv.check_stats.load_papers_data")
    @patch("daily_arxiv.daily_arxiv.check_stats.subprocess.run")
    def test_today_history_uses_published_copy_not_local_crawl(self, run, load, _exists):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"id":"2401.00001","version_id":"2401.00001v2"}\n'
        )
        self.assertEqual(
            {"2401.00001v2"}, load_history_ids("2026-10-15", published_only=True)
        )
        load.assert_not_called()

    def test_version_key_accepts_legacy_rows_and_rejects_mismatch(self):
        self.assertEqual("2401.00001v1", paper_version_id({"id": "2401.00001"}))
        self.assertEqual("cs/0601001v1", paper_version_id({"id": "cs/0601001"}))
        self.assertEqual("2401.00001v2", paper_version_id({"id": "2401.00001v2"}))
        with self.assertRaisesRegex(ValueError, "Invalid version_id"):
            paper_version_id({"id": "2401.00001", "version_id": "2401.00002v2"})

    def run_dedup(self, papers, history_ids):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            working_directory = root / "daily_arxiv"
            working_directory.mkdir()
            today_file = root / "data" / f"{datetime.now():%Y-%m-%d}.jsonl"
            today_file.write_text(
                "".join(json.dumps(paper) + "\n" for paper in papers), encoding="utf-8"
            )
            original_directory = Path.cwd()
            try:
                os.chdir(working_directory)
                history_lookup = (
                    history_ids if callable(history_ids)
                    else lambda _date, published_only=False: history_ids
                )
                with patch("daily_arxiv.daily_arxiv.check_stats.load_history_ids", side_effect=history_lookup):
                    status = perform_deduplication()
            finally:
                os.chdir(original_directory)
            published = (
                [json.loads(line) for line in today_file.read_text(encoding="utf-8").splitlines()]
                if today_file.exists() else None
            )
            return status, published

    def test_replacement_passes_legacy_v1_and_same_run_duplicates_collapse(self):
        papers = [
            {"id": "2401.00001", "version_id": "2401.00001v2", "submission_type": "cross"},
            {"id": "2401.00001", "version_id": "2401.00001v2", "submission_type": "replacement"},
            {"id": "2401.00002", "version_id": "2401.00002v1", "submission_type": "new"},
        ]
        status, published = self.run_dedup(papers, {"2401.00001v1"})
        self.assertEqual("has_new_content", status)
        self.assertEqual(["replacement", "new"], [paper["submission_type"] for paper in published])

    def test_same_version_in_history_is_skipped(self):
        papers = [{"id": "2401.00001", "version_id": "2401.00001v2"}]
        status, published = self.run_dedup(papers, {"2401.00001v2"})
        self.assertEqual("no_new_content", status)
        self.assertIsNone(published)

    def test_same_day_rerun_skips_published_revision_but_allows_next_revision(self):
        published_today = lambda _date, published_only=False: (
            {"2401.00001v2"} if published_only else set()
        )
        v2 = [{"id": "2401.00001", "version_id": "2401.00001v2"}]
        v3 = [{"id": "2401.00001", "version_id": "2401.00001v3"}]
        self.assertEqual(("no_new_content", None), self.run_dedup(v2, published_today))
        status, published = self.run_dedup(v3, published_today)
        self.assertEqual("has_new_content", status)
        self.assertEqual(v3, published)


if __name__ == "__main__":
    unittest.main()
