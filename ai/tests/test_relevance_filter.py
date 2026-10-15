import json
import tempfile
import unittest
from pathlib import Path

from ai.relevance_filter import PROFILE_PATH, filter_file, matched_terms, select_papers


with PROFILE_PATH.open(encoding="utf-8") as source:
    PROFILE = json.load(source)


class RelevanceFilterTests(unittest.TestCase):
    def test_phrase_matching_handles_hyphens_and_rejects_partial_acronyms(self):
        paper = {"title": "Skeleton-based AQA for gymnastics", "summary": ""}
        self.assertEqual(
            ["skeleton based", "aqa", "gymnastics"],
            matched_terms(paper, ["skeleton based", "aqa", "gymnastics", "maqa"]),
        )
        self.assertEqual([], matched_terms({"title": "MAQA", "summary": ""}, ["aqa"]))
        self.assertEqual(["human pose"], matched_terms({"title": "Human poses", "summary": ""}, ["human pose"]))

    def test_semantic_filter_keeps_relevant_and_maybe_only(self):
        papers = [
            {"id": "1", "title": "Gymnastics coaching feedback", "summary": "Human motion."},
            {"id": "2", "title": "Hand pose estimation", "summary": "Hand keypoints."},
            {"id": "3", "title": "Car detection", "summary": "Autonomous driving."},
            {"id": "4", "title": "Dance action recognition", "summary": "Human motion."},
        ]
        decisions = {
            "1": {"label": "relevant", "reason": "体操教学反馈。"},
            "2": {"label": "irrelevant", "reason": "只研究手部姿态。"},
            "4": {"label": "maybe", "reason": "舞蹈动作方法可能迁移。"},
        }
        seen = []

        def classify(paper):
            seen.append(paper["id"])
            return decisions[paper["id"]]

        selected, counts = select_papers(papers, PROFILE, "semantic", classify)
        self.assertEqual(["1", "2", "4"], seen)
        self.assertEqual(["1", "4"], [paper["id"] for paper in selected])
        self.assertEqual("relevant", selected[0]["relevance"]["label"])
        self.assertEqual(1, counts["outside_keyword_gate"])
        self.assertEqual(1, counts["irrelevant"])
        self.assertEqual(1, counts["maybe"])

    def test_classifier_failure_does_not_overwrite_raw_data(self):
        original = '{"id":"1","title":"Human pose","summary":"Video"}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.jsonl"
            path.write_text(original, encoding="utf-8")

            def fail(_paper):
                raise RuntimeError("API unavailable")

            with self.assertRaisesRegex(RuntimeError, "API unavailable"):
                filter_file(path, PROFILE, "semantic", fail)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_keyword_mode_retains_candidates_without_api(self):
        papers = [
            {"id": "1", "title": "3D human pose estimation", "summary": ""},
            {"id": "2", "title": "Text classification", "summary": ""},
        ]
        selected, counts = select_papers(papers, PROFILE, "keywords")
        self.assertEqual(["1"], [paper["id"] for paper in selected])
        self.assertEqual(1, counts["kept"])

    def test_rejected_candidates_have_auditable_titles_terms_and_reasons(self):
        decisions = []
        paper = {"id": "1", "version_id": "1v2", "title": "Object pose estimation", "summary": "Robotic grasping"}
        selected, counts = select_papers(
            [paper], PROFILE, "semantic",
            lambda _: {"label": "irrelevant", "reason": "物体位姿，与人体运动无关。"},
            on_decision=decisions.append,
        )
        self.assertEqual([], selected)
        self.assertEqual(0, counts["relevant"])
        self.assertEqual([{
            "id": "1v2", "title": paper["title"], "matched_terms": ["pose estimation"],
            "label": "irrelevant", "reason": "物体位姿，与人体运动无关。",
        }], decisions)


if __name__ == "__main__":
    unittest.main()
