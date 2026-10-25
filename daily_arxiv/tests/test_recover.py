import json
import tempfile
import unittest
from pathlib import Path

from scrapy.http import HtmlResponse

from daily_arxiv.daily_arxiv.recover import (
    AI_FIELDS, load_manifest, merge_versions, parse_version_page, plan_recovery,
    process_recovery, read_papers, validate_completed, write_papers,
)


def entry(identity="2610.12345v2", day="2026-10-21"):
    return {"date": day, "run_id": 123, "decision": {
        "id": identity, "title": "Human motion quality", "label": "maybe",
        "reason": "Transferable motion representation", "matched_terms": ["human motion"],
    }}


def page(selected="2", actual_id="2610.12345", title="Human motion quality", history=True):
    html = f'''<meta name="citation_arxiv_id" content="{actual_id}">
        <meta name="citation_title" content="{title}">
        <meta name="citation_author" content="Researcher One">
        <meta name="citation_author" content="Researcher Two">
        <blockquote class="abstract"><span>Abstract:</span>A full abstract with
        <a href="https://example.com/source">this https URL</a>.</blockquote>
        <td class="subjects"><span class="primary-subject">Computer Vision (cs.CV)</span>;
        Machine Learning (cs.LG); Quantitative Biology (q-bio.NC)</td>
        <td class="tablecell comments">Accepted paper</td>'''
    if history:
        html += f'''<div class="submission-history">
            <strong><a href="/abs/2610.12345v3">[v3]</a></strong>
            <strong>[v{selected}]</strong>
            <strong><a href="/abs/2610.12345v1">[v1]</a></strong></div>'''
    return HtmlResponse(url="https://arxiv.org/abs/2610.12345v2", body=html.encode(), encoding="utf-8")


def paper(identity="2610.12345v2", day="2026-10-21", completed=False):
    base = identity.rsplit("v", 1)[0]
    result = {"id": base, "version_id": identity, "title": "Human motion quality",
              "summary": "Full original abstract", "authors": ["Researcher"], "categories": ["cs.CV"],
              "relevance": {"label": "maybe", "reason": "Relevant method"},
              "recovery": {"date": day, "source_run_id": 123}}
    if completed:
        result["AI"] = {field: "完整分析" for field in AI_FIELDS}
        result["summary_zh"] = "完整中文摘要"
    return result


class VersionPageTests(unittest.TestCase):
    def test_exact_older_version_not_maximum_history_version(self):
        result = parse_version_page(page(), entry())
        self.assertEqual(result["version_id"], "2610.12345v2")
        self.assertEqual(result["pdf"], "https://arxiv.org/pdf/2610.12345v2")
        self.assertEqual(result["categories"], ["cs.CV", "cs.LG", "q-bio.NC"])
        self.assertIn("https://example.com/source", result["summary"])
        self.assertEqual(result["relevance"], {"label": "maybe", "reason": "Transferable motion representation"})
        self.assertNotIn("submission_type", result)

    def test_different_selected_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact historical version"):
            parse_version_page(page(selected="3"), entry())

    def test_different_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact historical version"):
            parse_version_page(page(actual_id="2610.99999"), entry())

    def test_missing_version_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact historical version"):
            parse_version_page(page(history=False), entry())

    def test_saved_decision_cannot_attach_to_different_title(self):
        with self.assertRaisesRegex(ValueError, "Title differs"):
            parse_version_page(page(title="Unrelated new title"), entry())

    def test_unicode_heading_matches_when_citation_uses_latex(self):
        saved = entry()
        saved["decision"]["title"] = "$λ_0$: Human motion quality"
        response = page(title=r"$\lambda_0$: Human motion quality")
        response = response.replace(body=response.body +
                                    '<h1 class="title"><span>Title:</span>$λ_0$: Human motion quality</h1>'.encode())
        result = parse_version_page(response, saved)
        self.assertEqual(result["title"], saved["decision"]["title"])
        self.assertEqual(result["version_id"], "2610.12345v2")

    def test_latex_citation_can_match_when_visible_heading_uses_unicode(self):
        saved = entry()
        saved["decision"]["title"] = r"$\lambda_0$: Human motion quality"
        response = page(title=saved["decision"]["title"])
        response = response.replace(body=response.body +
                                    '<h1 class="title"><span>Title:</span>$λ_0$: Human motion quality</h1>'.encode())
        self.assertEqual(parse_version_page(response, saved)["title"], saved["decision"]["title"])


class RecoveryPlanningTests(unittest.TestCase):
    def test_manifest_requires_selected_version_and_retains_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            decision = entry()["decision"]
            path.write_text(json.dumps([{"date": "2026-10-21", "run_id": 123, "papers": [decision]}]))
            self.assertEqual(load_manifest(path)[0]["decision"], decision)
            decision["label"] = "irrelevant"
            path.write_text(json.dumps([{"date": "2026-10-21", "run_id": 123, "papers": [decision]}]))
            with self.assertRaisesRegex(ValueError, "unselected"):
                load_manifest(path)

    def test_skip_completed_versions_across_dates_and_repair_partial_in_original_date(self):
        with tempfile.TemporaryDirectory() as temporary:
            published = Path(temporary)
            write_papers(published / "data" / "2026-10-19_AI_enhanced_Chinese.jsonl", [paper(completed=True)])
            write_papers(published / "data" / "2026-10-20.jsonl", [paper("2610.54321v1")])
            planned, existing, skipped = plan_recovery([
                entry(), entry("2610.54321v1", "2026-10-23"), entry("2610.54321v1", "2026-10-24")
            ], published)
            self.assertEqual(skipped, {"2610.12345v2"})
            self.assertEqual(len(planned), 1)
            self.assertEqual(planned[0]["date"], "2026-10-20")
            self.assertEqual(existing["2610.12345v2"]["summary_zh"], "完整中文摘要")

    def test_merge_preserves_completed_translation_and_other_versions(self):
        previous = paper(completed=True)
        newer = paper(completed=True)
        newer["summary_zh"] = "不应替换原译文"
        result = merge_versions([previous, paper("2610.12345v1", completed=True)], [newer, paper("2610.54321v1", completed=True)])
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], previous)

    def test_missing_or_fallback_results_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "dropped"):
            validate_completed([paper()], [])
        bad = paper(completed=True)
        bad["AI"]["tldr"] = "Summary generation failed"
        with self.assertRaisesRegex(RuntimeError, "complete AI"):
            validate_completed([paper()], [bad])


class RecoveryPublicationTests(unittest.TestCase):
    def fake_runner(self, fail_day=None):
        def run(command, cwd=None, check=True):
            script = Path(command[1]).name
            if script == "enhance.py":
                source = Path(command[-1])
                if fail_day and source.name.startswith(fail_day):
                    raise RuntimeError("Simulated provider failure")
                papers = read_papers(source)
                for item in papers:
                    item["AI"] = {field: "完整分析" for field in AI_FIELDS}
                write_papers(source.with_name(source.stem + "_AI_enhanced_Chinese.jsonl"), papers)
            elif script == "translate_abstracts.py":
                source = Path(command[-1])
                papers = read_papers(source)
                for item in papers:
                    item.setdefault("summary_zh", "完整中文摘要")
                write_papers(source, papers)
            elif script == "convert.py":
                source = Path(cwd) / command[-1]
                papers = read_papers(source)
                (Path(cwd) / (source.name.split("_")[0] + ".md")).write_text(
                    "\n".join(item["version_id"] for item in papers), encoding="utf-8")
            else:
                raise AssertionError(f"Unexpected child process: {command}")
        return run

    def setup_tree(self, root):
        published, output, repo = root / "published", root / "recovery-output", root / "repo"
        (repo / "to_md").mkdir(parents=True)
        (repo / "to_md" / "paper_template.md").write_text("template")
        old = paper("2610.11111v1", completed=True)
        write_papers(published / "data" / "2026-10-21.jsonl", [old])
        write_papers(published / "data" / "2026-10-21_AI_enhanced_Chinese.jsonl", [old])
        (published / "data" / "2026-10-21.md").write_text("existing report")
        return published, output, repo, old

    def test_later_failure_leaves_all_published_files_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            published, output, repo, old = self.setup_tree(Path(temporary))
            before = {path.relative_to(published): path.read_bytes() for path in published.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(RuntimeError, "provider failure"):
                process_recovery([paper(), paper("2610.54321v1", "2026-10-22")], {}, published, output, repo,
                                 runner=self.fake_runner(fail_day="2026-10-22"))
            after = {path.relative_to(published): path.read_bytes() for path in published.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertTrue((output / "staged" / "data" / "2026-10-21.md").is_file())

    def test_success_keeps_existing_papers_and_lists_all_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            published, output, repo, old = self.setup_tree(Path(temporary))
            process_recovery([paper()], {}, published, output, repo, runner=self.fake_runner())
            merged = read_papers(published / "data" / "2026-10-21_AI_enhanced_Chinese.jsonl")
            self.assertEqual(merged[0], old)
            self.assertEqual(len(merged), 2)
            self.assertIn("2610.11111v1", (published / "data" / "2026-10-21.md").read_text())
            self.assertEqual((published / "assets" / "file-list.txt").read_text().splitlines(),
                             ["2026-10-21.jsonl", "2026-10-21_AI_enhanced_Chinese.jsonl"])


if __name__ == "__main__":
    unittest.main()
