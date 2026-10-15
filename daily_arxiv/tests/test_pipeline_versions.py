import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
        with self.assertRaises(ValueError):
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


if __name__ == "__main__":
    unittest.main()
