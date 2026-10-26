import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from daily_arxiv.daily_arxiv.crawl import main, validate_crawl, validate_release_dates, ReleaseNotReady
from daily_arxiv.daily_arxiv.spiders.arxiv import ArxivSpider, listing_date
from scrapy.http import HtmlResponse


class CrawlCompletenessTests(unittest.TestCase):
    def test_all_categories_must_have_reached_expected_announcement(self):
        urls = [f"https://arxiv.org/list/{category}/new" for category in ("cs.CV", "cs.AI", "cs.LG")]
        dates = {url: "2026-10-26" for url in urls}
        validate_release_dates(date(2026, 10, 26), dates, urls)
        dates[urls[2]] = "2026-10-23"
        with self.assertRaisesRegex(ReleaseNotReady, "cs.LG"):
            validate_release_dates(date(2026, 10, 26), dates, urls)
        del dates[urls[2]]
        with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
            validate_release_dates(date(2026, 10, 26), dates, urls)

    def test_stale_listing_stops_before_emitting_papers(self):
        spider = ArxivSpider(expected_listing_date="2026-10-26")
        url = "https://arxiv.org/list/cs.CV/new"
        response = HtmlResponse(url=url, body=b'<div id="dlpage"><h3>Showing new listings for Friday, 23 October 2026</h3><dl><dt>old data</dt></dl></div>', encoding="utf-8")
        self.assertEqual(list(spider.parse(response)), [])
        self.assertEqual(spider.listing_dates, {url: "2026-10-23"})
        self.assertEqual(spider.expected_entries, 0)
        self.assertEqual(listing_date(["Showing new listings for Monday, 2 November 2026"]), date(2026, 11, 2))

    def test_waiting_release_does_not_publish_or_enrich(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "papers.jsonl"
            with (
                patch("sys.argv", ["crawl", "--output", str(output), "--expected-listing-date", "2026-10-26"]),
                patch("daily_arxiv.daily_arxiv.crawl.collect_listings", side_effect=ReleaseNotReady("Old batch")),
                patch("daily_arxiv.daily_arxiv.crawl.DailyArxivPipeline") as enrich,
            ):
                self.assertEqual(main(), 3)
            enrich.assert_not_called()
            self.assertFalse(output.exists())

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
                patch("daily_arxiv.daily_arxiv.crawl.collect_listings", return_value=[{
                    "id": "2610.00001", "version_id": "2610.00001v2", "metadata_source": "arxiv_listing",
                    "title": "Paper", "summary": "Abstract", "authors": ["Author"], "categories": ["cs.CV"],
                }]),
                patch("daily_arxiv.daily_arxiv.crawl.DailyArxivPipeline.enrich_papers", side_effect=RuntimeError("API failed")),
            ):
                with self.assertRaisesRegex(RuntimeError, "API failed"):
                    main()
            self.assertEqual("earlier output\n", output.read_text(encoding="utf-8"))
