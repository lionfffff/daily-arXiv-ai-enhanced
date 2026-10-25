"""Recover selected paper versions from saved workflow decisions, without reclassifying."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy import signals
from scrapy.crawler import CrawlerProcess

from .check_stats import paper_version_id
from .crawl import write_papers
from .spiders.arxiv import text_content


VERSION_PATTERN = re.compile(r"(?P<base>\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})v(?P<version>[1-9]\d*)\Z")
AI_FIELDS = ("tldr", "motivation", "method", "result", "conclusion")
FAILED_AI = {
    "Summary generation failed", "Motivation analysis unavailable", "Method extraction failed",
    "Result analysis unavailable", "Conclusion extraction failed",
}


def read_papers(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def has_ai(paper):
    ai = paper.get("AI", {})
    return isinstance(ai, dict) and all(
        isinstance(ai.get(field), str) and ai[field].strip() and ai[field] not in FAILED_AI
        for field in AI_FIELDS
    )


def has_translation(paper):
    return isinstance(paper.get("summary_zh"), str) and bool(re.search(r"[\u3400-\u9fff]", paper["summary_zh"]))


def complete(paper):
    return has_ai(paper) and has_translation(paper)


def load_manifest(path):
    batches = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(batches, list) or not batches:
        raise ValueError("Recovery manifest must be a nonempty list of dated workflow runs")
    entries = []
    for batch in batches:
        day = batch["date"]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or date.fromisoformat(day).isoformat() != day:
            raise ValueError(f"Invalid recovery date: {day!r}")
        if not isinstance(batch["run_id"], int) or batch["run_id"] <= 0:
            raise ValueError("Recovery run_id must be a positive integer")
        for decision in batch["papers"]:
            if not VERSION_PATTERN.fullmatch(decision["id"]):
                raise ValueError(f"Recovery requires an explicit valid version: {decision['id']}")
            if decision["label"] not in {"relevant", "maybe"}:
                raise ValueError(f"Refusing to recover unselected paper: {decision['id']}")
            if not decision.get("title", "").strip() or not decision.get("reason", "").strip():
                raise ValueError(f"Incomplete saved relevance decision: {decision['id']}")
            entries.append({"date": day, "run_id": batch["run_id"], "decision": decision})
    return sorted(entries, key=lambda entry: entry["date"])


def plan_recovery(entries, published, language="Chinese"):
    """Skip completed versions across all dates, repairing partial rows in their original date."""
    existing = {}
    locations = {}
    for path in sorted((published / "data").glob("*.jsonl")):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:_AI_enhanced_" + re.escape(language) + r")?\.jsonl", path.name)
        if not match:
            continue
        for paper in read_papers(path):
            identity = paper_version_id(paper)
            locations.setdefault(identity, match.group(1))
            previous = existing.get(identity)
            # Prefer the completed enhanced record over a raw or partial row.
            if previous is None or (complete(paper), has_ai(paper), has_translation(paper)) > (
                complete(previous), has_ai(previous), has_translation(previous)
            ):
                existing[identity] = paper

    planned = {}
    skipped = set()
    for entry in entries:
        identity = entry["decision"]["id"]
        if identity in existing and complete(existing[identity]):
            skipped.add(identity)
            continue
        if identity not in planned:
            planned[identity] = {**entry, "date": locations.get(identity, entry["date"])}
    return list(planned.values()), existing, skipped


def normalized_title(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def parse_version_page(response, entry):
    """The unlinked history version is the requested page, including older revisions."""
    decision = entry["decision"]
    identity = decision["id"]
    match = VERSION_PATTERN.fullmatch(identity)
    base = match.group("base")
    actual_id = response.css('meta[name="citation_arxiv_id"]::attr(content)').get()
    selected = response.css(".submission-history").xpath(".//strong[not(.//a)]/text()").getall()
    selected_versions = re.findall(r"\[v([1-9]\d*)\]", " ".join(selected))
    url = urlparse(response.url)
    if (response.status != 200 or url.hostname != "arxiv.org" or url.path != f"/abs/{identity}"
            or actual_id != base or selected_versions != [match.group("version")]):
        raise ValueError(f"Cannot verify exact historical version {identity}: {response.url}")

    # arXiv citation metadata can use LaTeX (e.g. \\lambda) while the listing
    # and visible heading use Unicode (λ). Accept an exact normalized match
    # against either title from the same verified version page.
    page_titles = [value for value in (
        text_content(response.css("h1.title"), "Title:"),
        response.css('meta[name="citation_title"]::attr(content)').get(),
    ) if value]
    title = next((value for value in page_titles
                  if normalized_title(value) == normalized_title(decision["title"])), None)
    authors = response.css('meta[name="citation_author"]::attr(content)').getall()
    summary = text_content(response.css("blockquote.abstract"), "Abstract:")
    categories = list(dict.fromkeys(re.findall(r"\(([a-z][a-z.-]*(?:\.[A-Z]{2})?)\)", text_content(response.css(".subjects")))))
    if not page_titles or not summary or not authors or not categories:
        raise ValueError(f"Incomplete historical metadata: {identity}")
    if title is None:
        raise ValueError(f"Title differs from saved decision for {identity}; review the manifest before recovery")

    return {
        "id": base, "version_id": identity, "title": title, "authors": authors,
        "summary": summary, "categories": categories,
        "comment": text_content(response.css(".tablecell.comments")) or None,
        "abs": f"https://arxiv.org/abs/{identity}", "pdf": f"https://arxiv.org/pdf/{identity}",
        "metadata_source": "arxiv_abstract",
        # Saved decisions did not record new/cross/replacement, so do not invent it.
        "relevance": {"label": decision["label"], "reason": decision["reason"]},
        "recovery": {"date": entry["date"], "source_run_id": entry["run_id"],
                     "matched_terms": decision.get("matched_terms", [])},
    }


class RecoverySpider(scrapy.Spider):
    name = "recover_versions"
    allowed_domains = ["arxiv.org"]

    def __init__(self, entries, **kwargs):
        super().__init__(**kwargs)
        self.entries = entries

    def start_requests(self):
        for entry in self.entries:
            yield scrapy.Request(f"https://arxiv.org/abs/{entry['decision']['id']}",
                                 callback=self.parse, cb_kwargs={"entry": entry})

    def parse(self, response, entry):
        yield parse_version_page(response, entry)


def collect_metadata(entries, output):
    if not entries:
        return []
    process = CrawlerProcess({
        "ROBOTSTXT_OBEY": True, "CONCURRENT_REQUESTS": 1, "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": 3, "RANDOMIZE_DOWNLOAD_DELAY": False, "DOWNLOAD_TIMEOUT": 90,
        "RETRY_TIMES": 2, "TELNETCONSOLE_ENABLED": False, "LOG_LEVEL": "INFO",
        "ITEM_PIPELINES": {}, "FEEDS": {}, "COOKIES_ENABLED": False,
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "USER_AGENT": "daily-arxiv-recovery (+https://github.com/lionfffff/daily-arXiv-ai-enhanced)",
    })
    crawler = process.create_crawler(RecoverySpider)
    papers, failures = [], []

    def collect(item, response, spider):
        papers.append(dict(item))
        # Preserve completed metadata in the artifact even if a later request fails.
        write_papers(output / "fetched.jsonl", papers)

    crawler.signals.connect(collect, signal=signals.item_scraped, weak=False)
    process.crawl(crawler, entries=entries).addErrback(lambda failure: failures.append(failure.getErrorMessage()))
    process.start()
    stats = crawler.stats.get_stats()
    expected = {entry["decision"]["id"] for entry in entries}
    if (failures or stats.get("finish_reason") != "finished" or stats.get("log_count/ERROR", 0)
            or {paper_version_id(paper) for paper in papers} != expected):
        raise RuntimeError(f"Historical metadata incomplete: got {len(papers)} of {len(expected)} versions; no publication")
    return papers


def merge_versions(existing, recovered):
    merged = {paper_version_id(paper): paper for paper in existing}
    for paper in recovered:
        identity = paper_version_id(paper)
        if identity not in merged or not complete(merged[identity]):
            merged[identity] = paper
    return list(merged.values())


def validate_completed(expected, enhanced):
    expected_ids = {paper_version_id(paper) for paper in expected}
    actual_ids = {paper_version_id(paper) for paper in enhanced}
    if expected_ids != actual_ids or len(enhanced) != len(actual_ids):
        raise RuntimeError("Recovery enhancement dropped or duplicated selected paper versions; refusing publication")
    if not all(complete(paper) for paper in enhanced):
        raise RuntimeError("Recovery requires complete AI fields and Chinese abstracts for every selected paper")


def process_recovery(papers, existing, published, output, repo, language="Chinese", runner=subprocess.run):
    days = sorted({paper["recovery"]["date"] for paper in papers})
    staged = output / "staged" / "data"
    staged.mkdir(parents=True, exist_ok=True)
    for day in days:
        day_dir = output / day
        day_dir.mkdir(parents=True, exist_ok=True)
        recovered = [dict(paper) for paper in papers if paper["recovery"]["date"] == day]
        for paper in recovered:
            previous = existing.get(paper_version_id(paper), {})
            if previous.get("summary") == paper["summary"]:
                if has_ai(previous):
                    paper["AI"] = previous["AI"]
                if has_translation(previous):
                    paper["summary_zh"] = previous["summary_zh"]

        needs_ai = [paper for paper in recovered if not has_ai(paper)]
        raw_path = day_dir / f"{day}.jsonl"
        enhanced_path = day_dir / f"{day}_AI_enhanced_{language}.jsonl"
        if needs_ai:
            write_papers(raw_path, needs_ai)
            runner([sys.executable, "enhance.py", "--data", str(raw_path.resolve())], cwd=repo / "ai", check=True)
            enhanced = read_papers(enhanced_path)
            if {paper_version_id(paper) for paper in enhanced} != {paper_version_id(paper) for paper in needs_ai}:
                raise RuntimeError(f"AI enhancement did not preserve every selected version for {day}")
            recovered = merge_versions([paper for paper in recovered if has_ai(paper)], enhanced)
        write_papers(raw_path, [paper for paper in papers if paper["recovery"]["date"] == day])
        write_papers(enhanced_path, recovered)
        runner([sys.executable, str(repo / "ai" / "translate_abstracts.py"), str(enhanced_path.resolve())], check=True)
        recovered = read_papers(enhanced_path)
        validate_completed([paper for paper in papers if paper["recovery"]["date"] == day], recovered)

        merged_raw = merge_versions(read_papers(published / "data" / raw_path.name), read_papers(raw_path))
        merged_ai = merge_versions(read_papers(published / "data" / enhanced_path.name), recovered)
        write_papers(staged / raw_path.name, merged_raw)
        write_papers(staged / enhanced_path.name, merged_ai)
        # convert.py resolves its template and output from cwd; a basename also
        # avoids its legacy underscore-based path splitting on parent directories.
        shutil.copy2(repo / "to_md" / "paper_template.md", staged / "paper_template.md")
        runner([sys.executable, str(repo / "to_md" / "convert.py"), "--data", enhanced_path.name], cwd=staged, check=True)
        (staged / "paper_template.md").unlink()
        if not (staged / f"{day}.md").is_file():
            raise RuntimeError(f"Missing recovered Markdown for {day}")

    # Touch the publication checkout only after ALL requested days succeeded.
    for path in sorted(staged.iterdir()):
        destination = published / "data" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    if days:
        file_list = published / "assets" / "file-list.txt"
        file_list.parent.mkdir(parents=True, exist_ok=True)
        file_list.write_text("".join(f"{path.name}\n" for path in sorted((published / "data").glob("*.jsonl"))), encoding="utf-8")
    return days


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--published", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("recovery-output"))
    parser.add_argument("--fetch-only", action="store_true", help="Validate historical metadata without AI or publication")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty recovery output directory to avoid mixing runs")
    language = os.environ.get("LANGUAGE", "Chinese")
    if language != "Chinese":
        raise ValueError("Historical recovery requires LANGUAGE=Chinese")
    entries = load_manifest(args.manifest)
    planned, existing, skipped = plan_recovery(entries, args.published, language)
    shutil.copy2(args.manifest, output / "manifest.json")
    (output / "plan.json").write_text(json.dumps(planned, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest_entries": len(entries), "already_complete": len(skipped), "recover_versions": len(planned)}), flush=True)
    papers = collect_metadata(planned, output)
    if args.fetch_only:
        print(json.dumps({"verified_versions": len(papers), "publication": False}), flush=True)
        return
    days = process_recovery(papers, existing, args.published.resolve(), output, repo, language)
    result = {"recovered_versions": len(papers), "already_complete": len(skipped), "dates": days}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"Recovered {len(papers)} exact paper versions on {len(days)} dates; skipped {len(skipped)} already complete versions.\n\n")
            summary.write("Saved relevance decisions were preserved; every recovered paper has a complete Chinese abstract and AI report.\n")


if __name__ == "__main__":
    main()
