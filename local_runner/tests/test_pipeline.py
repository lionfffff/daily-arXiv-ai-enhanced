import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_runner import pipeline


def paper(identity="2610.12345", title="运动论文"):
    return {
        "id": identity, "version_id": identity + "v1", "title": title,
        "summary": "A study of human motion.", "summary_zh": "这是一项关于人体运动的研究。",
        "AI": {key: "完整的中文分析。" for key in pipeline.AI_FIELDS},
    }


class ValidationTests(unittest.TestCase):
    def test_rejects_dropped_papers_and_placeholder_summaries(self):
        valid = paper()
        with self.assertRaisesRegex(ValueError, "versions do not match"):
            pipeline.validate_complete([valid], [])
        broken = {**valid, "AI": {**valid["AI"], "tldr": "Summary generation failed"}}
        with self.assertRaisesRegex(ValueError, "Incomplete AI"):
            pipeline.validate_complete([valid], [broken])

    def test_rejects_untranslated_abstract(self):
        row = {**paper(), "summary_zh": "English text only"}
        with self.assertRaisesRegex(ValueError, "Chinese abstract"):
            pipeline.validate_complete([row], [row])

    def test_concurrent_published_versions_win_and_new_versions_survive(self):
        old = paper(title="Already published")
        incoming = [paper(title="Stale local copy"), paper("2610.99999")]
        merged = {row["id"]: row for row in pipeline.merge_rows([old], incoming)}
        self.assertEqual(merged[old["id"]]["title"], "Already published")
        self.assertIn("2610.99999", merged)

    def test_destructive_git_operations_require_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                pipeline.require_owned_child(runtime, runtime / "checkout")
            pipeline.write_json(runtime / pipeline.OWNER_FILE, {"application": "daily-arxiv-local-runner"})
            pipeline.require_owned_child(runtime, runtime / "checkout")
            with self.assertRaises(ValueError):
                pipeline.require_owned_child(runtime, runtime.parent)


class LifecycleTests(unittest.TestCase):
    def run_fixture(self, directory, behavior):
        root = Path(directory)
        repo, runtime, checkout = root / "source", root / "runtime", root / "fixture"
        repo.mkdir()
        for path in (checkout / "data", checkout / "daily_arxiv", checkout / "ai", checkout / "to_md"):
            path.mkdir(parents=True, exist_ok=True)
        original_file = repo / "user-file.txt"
        original_file.write_text("Do not change me", encoding="utf-8")
        raw = checkout / "data" / "2026-10-26.jsonl"
        enhanced = checkout / "data" / "2026-10-26_AI_enhanced_Chinese.jsonl"
        labels = []

        class FakeCommands(pipeline.Commands):
            def run(self, args, *, cwd, label, allowed=(0,)):
                labels.append(label)
                if label == "crawl" and behavior == "waiting":
                    self_expected_date = args[args.index("--expected-listing-date") + 1]
                    if self_expected_date != "2026-10-26":
                        raise AssertionError("Expected announcement date was not passed to crawler")
                    return pipeline.WAITING_RELEASE_EXIT_CODE, "Waiting for release"
                if label == "crawl":
                    pipeline.write_rows(raw, [paper()])
                if label == "deduplicate" and behavior == "duplicate":
                    raw.unlink()
                    return 1, "all duplicates"
                if label == "relevance" and behavior == "irrelevant":
                    pipeline.write_rows(raw, [])
                    return 0, '{"total": 1, "kept": 0}'
                if label == "enhance":
                    row = paper()
                    if behavior == "broken":
                        row["summary_zh"] = "English"
                    pipeline.write_rows(enhanced, [row])
                if label == "markdown":
                    (checkout / "data" / "2026-10-26.md").write_text("# paper", encoding="utf-8")
                return 0, ""

        with patch.object(pipeline, "Commands", FakeCommands), patch.object(
            pipeline, "prepare_checkout", return_value=(checkout, "https://github.com/test/repo.git")
        ), patch.object(pipeline, "publish_batch") as publish:
            if behavior == "broken":
                with self.assertRaisesRegex(ValueError, "Chinese abstract"):
                    pipeline.run_pipeline(repo, runtime, "2026-10-26", {"OPENAI_API_KEY": "test-secret"})
            else:
                pipeline.run_pipeline(repo, runtime, "2026-10-26", {"OPENAI_API_KEY": "test-secret"}, no_publish=True, expected_listing_date="2026-10-26")
            publish.assert_not_called()
        self.assertEqual(original_file.read_text(encoding="utf-8"), "Do not change me")
        status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
        if behavior != "waiting":
            self.assertTrue((Path(status["attempt"]) / "original-crawl.jsonl").is_file())
        self.assertNotIn("test-secret", (runtime / "status.json").read_text(encoding="utf-8"))
        return status, labels

    def test_duplicate_run_succeeds_without_ai_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            status, labels = self.run_fixture(directory, "duplicate")
        self.assertEqual(status["reason"], "no_new_content")
        self.assertEqual(status["counts"]["new_versions"], 0)
        self.assertNotIn("relevance", labels)

    def test_stale_announcement_waits_before_dedup_or_paid_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            status, labels = self.run_fixture(directory, "waiting")
        self.assertEqual(status["status"], "waiting_release")
        self.assertFalse(status["published"])
        self.assertEqual(labels, ["crawl"])

    def test_cli_propagates_waiting_release_exit_code(self):
        with patch.object(pipeline, "run_pipeline", return_value={"status": "waiting_release"}):
            self.assertEqual(pipeline.main(["--workspace", "fixture", "--date", "2026-10-26", "--expected-listing-date", "2026-10-26"]), 3)

    def test_empty_relevance_run_succeeds_without_enhancement(self):
        with tempfile.TemporaryDirectory() as directory:
            status, labels = self.run_fixture(directory, "irrelevant")
        self.assertEqual(status["reason"], "no_relevant_content")
        self.assertNotIn("enhance", labels)

    def test_failed_translation_cannot_publish_and_preserves_recovery_data(self):
        with tempfile.TemporaryDirectory() as directory:
            status, _ = self.run_fixture(directory, "broken")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["stage"], "validate")
        self.assertFalse(status["published"])

    def test_no_publish_runs_all_processing_but_not_git_push(self):
        with tempfile.TemporaryDirectory() as directory:
            status, labels = self.run_fixture(directory, "valid")
        self.assertEqual(status["reason"], "prepared_without_publication")
        self.assertEqual(status["counts"]["complete"], 1)
        self.assertIn("translate", labels)
        self.assertIn("markdown", labels)


class ConcurrentPublicationTests(unittest.TestCase):
    @staticmethod
    def git(directory, *args):
        result = subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip()

    def test_push_race_refetches_and_preserves_other_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, seed, runtime = root / "remote.git", root / "seed", root / "runtime"
            remote.mkdir()
            self.git(remote, "init", "--bare")
            seed.mkdir()
            self.git(seed, "init", "-b", "data")
            self.git(seed, "config", "user.name", "Fixture")
            self.git(seed, "config", "user.email", "fixture@example.test")
            self.git(seed, "remote", "add", "origin", str(remote))
            (seed / "README.md").write_text("fixture", encoding="utf-8")
            self.git(seed, "add", ".")
            self.git(seed, "commit", "-m", "initial")
            self.git(seed, "push", "origin", "data")
            attempt = runtime / "runs" / "attempt"
            checkout = attempt / "checkout"
            (checkout / "to_md").mkdir(parents=True)
            raw_name = "2026-10-26.jsonl"
            enhanced_name = "2026-10-26_AI_enhanced_Chinese.jsonl"
            pipeline.write_rows(checkout / "data" / raw_name, [paper()])
            pipeline.write_rows(checkout / "data" / enhanced_name, [paper()])
            pipeline.write_json(runtime / pipeline.OWNER_FILE, {"application": "daily-arxiv-local-runner"})
            test = self

            class RacingCommands(pipeline.Commands):
                def run(self, args, *, cwd, label, allowed=(0,)):
                    if label.startswith("publish-markdown-"):
                        (checkout / "data" / "2026-10-26.md").write_text("# merged papers", encoding="utf-8")
                        return 0, ""
                    if label == "publish-push-1":
                        other = paper("2610.77777", "Concurrent result")
                        pipeline.write_rows(seed / "data" / raw_name, [other])
                        pipeline.write_rows(seed / "data" / enhanced_name, [other])
                        (seed / "other-publisher.txt").write_text("preserve", encoding="utf-8")
                        test.git(seed, "add", ".")
                        test.git(seed, "commit", "-m", "concurrent publisher")
                        test.git(seed, "push", "origin", "data")
                    return super().run(args, cwd=cwd, label=label, allowed=allowed)

            result = pipeline.publish_batch(runtime, attempt, checkout, str(remote), "2026-10-26", "Chinese", RacingCommands(attempt / "logs", dict(os.environ)))
            self.assertTrue(result["published"])
            self.assertEqual(result["published_count"], 2)
            self.git(seed, "pull", "--ff-only", "origin", "data")
            self.assertEqual({row["id"] for row in pipeline.read_rows(seed / "data" / raw_name)}, {"2610.12345", "2610.77777"})
            self.assertEqual((seed / "other-publisher.txt").read_text(encoding="utf-8"), "preserve")
            self.assertIn(enhanced_name, (seed / "assets" / "file-list.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
