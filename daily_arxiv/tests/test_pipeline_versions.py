import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import arxiv

from daily_arxiv.daily_arxiv.pipelines import DailyArxivPipeline


def make_paper(version_id):
    return SimpleNamespace(
        get_short_id=lambda: version_id,
        authors=[SimpleNamespace(name="Researcher")],
        title="A paper",
        categories=["cs.CV"],
        comment=None,
        summary="An abstract",
    )


class PipelineVersionTests(unittest.TestCase):
    def process(self, base_id, version_id):
        pipeline = DailyArxivPipeline()
        pipeline.client.results = Mock(return_value=iter([make_paper(version_id)]))
        item = {"id": base_id, "submission_type": "replacement"}
        return pipeline.process_item(item, spider=None)

    def test_stores_version_and_pins_links(self):
        item = self.process("2412.12048", "2412.12048v2")
        self.assertEqual("2412.12048", item["id"])
        self.assertEqual("replacement", item["submission_type"])
        self.assertEqual("2412.12048v2", item["version_id"])
        self.assertEqual("https://arxiv.org/abs/2412.12048v2", item["abs"])
        self.assertEqual("https://arxiv.org/pdf/2412.12048v2", item["pdf"])

    def test_legacy_version_id(self):
        item = self.process("cs/0601001", "cs/0601001v3")
        self.assertEqual("cs/0601001v3", item["version_id"])

    def test_rejects_version_for_different_paper(self):
        with self.assertRaisesRegex(RuntimeError, "missing IDs"):
            self.process("2412.12048", "2412.12877v2")

    def test_reuses_api_metadata_across_category_listings(self):
        pipeline = DailyArxivPipeline()
        pipeline.client.results = Mock(return_value=iter([make_paper("2412.12048v2")]))

        first = pipeline.process_item(
            {"id": "2412.12048", "submission_type": "new"}, spider=None
        )
        second = pipeline.process_item(
            {"id": "2412.12048", "submission_type": "cross"}, spider=None
        )

        pipeline.client.results.assert_called_once()
        self.assertEqual("2412.12048v2", second["version_id"])
        self.assertEqual("new", first["submission_type"])
        self.assertEqual("cross", second["submission_type"])

    def test_batches_unique_ids_and_maps_unordered_results(self):
        pipeline = DailyArxivPipeline()
        ids = [f"2610.{number:05d}" for number in range(205)]
        pipeline.client.results = Mock(side_effect=lambda search: iter(
            make_paper(f"{paper_id}v2") for paper_id in reversed(search.id_list)
        ))
        items = [{"id": paper_id, "submission_type": "replacement"} for paper_id in ids + ids[:20]]
        enriched = pipeline.enrich_papers(items)
        self.assertEqual(3, pipeline.client.results.call_count)
        self.assertEqual([100, 100, 5], [len(call.args[0].id_list) for call in pipeline.client.results.call_args_list])
        self.assertEqual([item["id"] + "v2" for item in items], [item["version_id"] for item in enriched])

    def test_missing_metadata_fails_instead_of_returning_partial_batch(self):
        pipeline = DailyArxivPipeline()
        pipeline.client.results = Mock(return_value=iter([make_paper("2610.00001v2")]))
        with self.assertRaisesRegex(RuntimeError, "2610.00002"):
            pipeline.enrich_papers([{"id": "2610.00001"}, {"id": "2610.00002"}])

    def test_uses_listing_data_and_only_queries_missing_versions(self):
        pipeline = DailyArxivPipeline()
        listing = {
            "id": "2610.00001", "version_id": "2610.00001v3", "metadata_source": "arxiv_listing",
            "title": "Human pose", "summary": "Original abstract", "authors": ["Researcher"],
            "categories": ["cs.CV"], "submission_type": "replacement", "comment": None,
        }
        unknown = {**listing, "id": "2610.00002"}
        unknown.pop("version_id")
        pipeline.client.results = Mock(return_value=iter([make_paper("2610.00002v4")]))
        result = pipeline.enrich_papers([listing, unknown, listing])
        pipeline.client.results.assert_called_once()
        self.assertEqual(["2610.00002"], pipeline.client.results.call_args.args[0].id_list)
        self.assertEqual("https://arxiv.org/abs/2610.00001v3", result[0]["abs"])
        self.assertEqual("Original abstract", result[0]["summary"])
        self.assertEqual("2610.00002v4", result[1]["version_id"])

    def test_invalid_listing_version_cannot_be_published(self):
        pipeline = DailyArxivPipeline()
        with self.assertRaisesRegex(ValueError, "Unexpected listing version"):
            pipeline.enrich_papers([{
                "id": "2610.00001", "version_id": "2610.00002v3", "metadata_source": "arxiv_listing",
                "title": "Paper", "summary": "Abstract", "authors": ["Researcher"], "categories": ["cs.CV"],
            }])

    @patch("daily_arxiv.daily_arxiv.pipelines.time.sleep")
    def test_rate_limit_retries_with_backoff(self, sleep):
        pipeline = DailyArxivPipeline()
        pipeline.client.results = Mock(side_effect=[
            arxiv.HTTPError("https://export.arxiv.org/api/query", 0, 429),
            iter([make_paper("2610.00001v2")]),
        ])
        result = pipeline.enrich_papers([{"id": "2610.00001"}])
        sleep.assert_called_once_with(15)
        self.assertEqual("2610.00001v2", result[0]["version_id"])

    @patch("daily_arxiv.daily_arxiv.pipelines.time.sleep")
    def test_persistent_rate_limit_fails_after_bounded_retries(self, sleep):
        pipeline = DailyArxivPipeline()
        pipeline.client.results = Mock(side_effect=arxiv.HTTPError("https://export.arxiv.org/api/query", 0, 429))
        with self.assertRaises(arxiv.HTTPError):
            pipeline.enrich_papers([{"id": "2610.00001"}])
        self.assertEqual([15, 30, 60], [call.args[0] for call in sleep.call_args_list])


if __name__ == "__main__":
    unittest.main()
