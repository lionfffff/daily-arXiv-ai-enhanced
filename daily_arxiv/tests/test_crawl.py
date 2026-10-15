import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daily_arxiv.daily_arxiv.crawl import main, validate_crawl


class CrawlCompletenessTests(unittest.TestCase):
    def test_listing_errors_and_missing_categories_fail(self):
        url = "https://arxiv.org/list/cs.CV/new"
        validate_crawl({"finish_reason": "finished"}, [url], [url])
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            validate_crawl({"finish_reason": "finished"}, [url], [])
        with self.assertRaisesRegex(RuntimeError, "failed"):
            validate_crawl({"finish_reason": "finished", "log_count/ERROR": 1}, [url], [url])

    def test_metadata_failure_does_not_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "papers.jsonl"
            output.write_text("earlier output\n", encoding="utf-8")
            with (
                patch("sys.argv", ["crawl", "--output", str(output)]),
                patch("daily_arxiv.daily_arxiv.crawl.collect_listings", return_value=[{"id": "2610.00001"}]),
                patch("daily_arxiv.daily_arxiv.crawl.DailyArxivPipeline.enrich_papers", side_effect=RuntimeError("API failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "API failed"):
                    main()
            self.assertEqual("earlier output\n", output.read_text(encoding="utf-8"))
