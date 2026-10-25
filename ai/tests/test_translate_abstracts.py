import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from ai.translate_abstracts import build_translator, translate_files


class TranslateAbstractsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, papers):
        path = self.root / name
        path.write_text("".join(json.dumps(p) + "\n" for p in papers), encoding="utf-8")
        return path

    def test_real_translator_uses_shared_settings_and_parses_chinese_response(self):
        """Exercise client construction too: duplicate kwargs previously broke every run."""
        result = ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="",
            tool_calls=[{
                "name": "ChineseAbstract",
                "args": {"abstract_zh": "姿态误差降低了 20%。"},
                "id": "translation-test",
            }],
        ))])
        with (
            patch.dict(os.environ, {
                "OPENAI_API_KEY": "test-key-no-network",
                "OPENAI_BASE_URL": "https://api.deepseek.com",
                "MODEL_NAME": "deepseek-v4-pro",
            }),
            patch.object(ChatOpenAI, "_generate", autospec=True, return_value=result) as generate,
        ):
            translate = build_translator()
            translated = translate({"title": "Human pose", "summary": "Pose error fell by 20%."})

        self.assertEqual(translated, "姿态误差降低了 20%。")
        generate.assert_called_once()
        client, messages = generate.call_args.args[:2]
        self.assertEqual(client.temperature, 0)
        self.assertEqual(client.request_timeout, 120)
        self.assertEqual(client.max_retries, 2)
        self.assertIn("Human pose", messages[-1].content)
        self.assertIn("Pose error fell by 20%.", messages[-1].content)

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
