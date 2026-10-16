import json
import tempfile
import unittest
from pathlib import Path

from ai.translate_abstracts import translate_files


class TranslateAbstractsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, papers):
        path = self.root / name
        path.write_text("".join(json.dumps(p) + "\n" for p in papers), encoding="utf-8")
        return path

    def test_preserves_original_and_ai_fields_and_reuses_existing_translation(self):
        paper = {"id": "1234.56789", "version_id": "1234.56789v2", "summary": "Pose error fell by 20%.", "AI": {"tldr": "已有简述"}}
        path = self.write("papers.jsonl", [paper])
        translate_files([path], lambda p: "姿态误差降低了 20%。")
        translated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(translated.pop("summary_zh"), "姿态误差降低了 20%。")
        self.assertEqual(translated, paper)
        stats = translate_files([path], lambda p: self.fail("Should reuse the translation"))
        self.assertEqual(stats["translated_abstracts"], 0)
        self.assertEqual(stats["updated_files"], 0)

    def test_duplicate_versions_share_translation_but_revisions_do_not(self):
        paths = [self.write(f"{i}.jsonl", [{"version_id": version, "summary": text}]) for i, (version, text) in enumerate([
            ("1234.56789v1", "First abstract."), ("1234.56789v1", "First abstract."),
            ("1234.56789v2", "Revised abstract."), ("1234.56789v1", "Corrected source text."),
        ])]
        calls = []
        stats = translate_files(paths, lambda p: calls.append(p["summary"]) or "完整中文摘要。")
        self.assertEqual(len(calls), 3)
        self.assertEqual(stats["updated_files"], 4)

    def test_failure_leaves_all_input_files_unchanged(self):
        first = self.write("first.jsonl", [{"id": "first", "summary": "Abstract one."}])
        second = self.write("second.jsonl", [{"id": "second", "summary": "Abstract two."}])
        originals = [p.read_bytes() for p in (first, second)]
        with self.assertRaises(ValueError):
            translate_files([first, second], lambda p: "中文摘要。" if p["id"] == "first" else "English only")
        self.assertEqual([p.read_bytes() for p in (first, second)], originals)


if __name__ == "__main__":
    unittest.main()
