"""Collect listing IDs, then fetch metadata in batches with strict completeness checks."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from scrapy import signals
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from .pipelines import DailyArxivPipeline
from .spiders.arxiv import ArxivSpider


def validate_crawl(stats, expected_urls, parsed_urls):
    if stats.get("finish_reason") != "finished" or stats.get("log_count/ERROR", 0):
        raise RuntimeError("arXiv listing crawl failed; see the preceding Scrapy errors")
    missing = set(expected_urls) - set(parsed_urls)
    if missing:
        raise RuntimeError(f"Incomplete arXiv listing crawl: {', '.join(sorted(missing))}")


def collect_listings():
    settings = get_project_settings()
    settings.setdict({
        "ITEM_PIPELINES": {},
        "FEEDS": {},
        "LOG_LEVEL": "INFO",
        "TELNETCONSOLE_ENABLED": False,
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
    }, priority="cmdline")
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(ArxivSpider)
    items = []
    failures = []

    def collect(item, response, spider):
        items.append(dict(item))

    crawler.signals.connect(collect, signal=signals.item_scraped, weak=False)
    process.crawl(crawler).addErrback(lambda failure: failures.append(failure.getErrorMessage()))
    process.start()
    if failures:
        raise RuntimeError(f"Could not run arXiv spider: {failures}")
    validate_crawl(crawler.stats.get_stats(), crawler.spider.start_urls, crawler.spider.parsed_listing_urls)
    return items


def write_papers(path, papers):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".crawl-", suffix=".jsonl", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            for paper in papers:
                target.write(json.dumps(paper, ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    items = collect_listings()
    papers = DailyArxivPipeline().enrich_papers(items)
    write_papers(args.output, papers)
    counts = {"listing_entries": len(items), "unique_papers": len({item['id'] for item in items}), "metadata_records": len(papers)}
    print(json.dumps(counts))
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"抓取完整：{counts['listing_entries']} 条分类记录，{counts['unique_papers']} 篇不同论文；元数据已全部获取。\n\n")


if __name__ == "__main__":
    main()
