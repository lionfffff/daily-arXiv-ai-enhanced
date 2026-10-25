import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from daily_arxiv.daily_arxiv.check_stats import (
    load_history_ids,
    main,
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

    def run_dedup(self, papers, history_ids, target_date=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            working_directory = root / "daily_arxiv"
            working_directory.mkdir()
            crawl_date = target_date or datetime.now().date()
            today_file = root / "data" / f"{crawl_date.isoformat()}.jsonl"
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
                    status = perform_deduplication(target_date)
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

    def test_crawl_date_and_history_stay_fixed_when_check_runs_after_midnight(self):
        papers = [
            {"id": "2610.00001", "version_id": "2610.00001v1"},
            {"id": "2610.00002", "version_id": "2610.00002v1"},
        ]
        history_dates = []

        def history_lookup(history_date, published_only=False):
            history_dates.append((history_date, published_only))
            # The earliest day must still be included even after midnight.
            return {"2610.00001v1"} if history_date == "2026-10-17" else set()

        with patch("daily_arxiv.daily_arxiv.check_stats.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 25, 0, 0, 20)
            status, published = self.run_dedup(
                papers, history_lookup, target_date=date(2026, 10, 24)
            )
        self.assertEqual("has_new_content", status)
        self.assertEqual([papers[1]], published)
        self.assertEqual(
            [("2026-10-24", True)]
            + [(f"2026-10-{day}", False) for day in range(23, 16, -1)],
            history_dates,
        )

    @patch("daily_arxiv.daily_arxiv.check_stats.os.path.exists", return_value=False)
    def test_explicit_missing_crawl_file_fails_cli_instead_of_reporting_empty(self, _exists):
        with self.assertRaises(SystemExit) as exit_result:
            main(["--date", "2026-10-24"])
        self.assertEqual(2, exit_result.exception.code)
        self.assertEqual("no_data", perform_deduplication())

    def test_cli_rejects_invalid_or_ambiguous_dates(self):
        for value in ("2026-10-32", "2026-1-2", "20261024"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as exit_result:
                main(["--date", value])
            self.assertEqual(2, exit_result.exception.code)


if __name__ == "__main__":
    unittest.main()
