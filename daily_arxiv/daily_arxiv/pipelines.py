# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
import arxiv
import re
import logging
import time

import requests


logger = logging.getLogger(__name__)


class DailyArxivPipeline:
    def __init__(self):
        self.page_size = 100
        self.client = arxiv.Client(page_size=self.page_size, delay_seconds=3, num_retries=0)
        self._paper_cache = {}

    def prefetch(self, paper_ids):
        """Fetch unique IDs in batches; never silently accept a partial API response."""
        pending = [paper_id for paper_id in dict.fromkeys(paper_ids) if paper_id not in self._paper_cache]
        for start in range(0, len(pending), self.page_size):
            batch = pending[start:start + self.page_size]
            search = arxiv.Search(id_list=batch, max_results=len(batch))
            for attempt in range(4):
                try:
                    results = list(self.client.results(search))
                    break
                except (arxiv.HTTPError, arxiv.UnexpectedEmptyPageError, requests.RequestException) as exc:
                    if isinstance(exc, arxiv.HTTPError) and exc.status not in {429, 500, 502, 503, 504}:
                        raise
                    if attempt == 3:
                        raise
                    delay = 15 * (2 ** attempt)
                    logger.warning("arXiv metadata batch failed (%s); retrying in %s seconds", type(exc).__name__, delay)
                    time.sleep(delay)
            fetched = {re.sub(r"v[1-9]\d*$", "", paper.get_short_id()): paper for paper in results}
            missing = set(batch) - fetched.keys()
            if missing:
                raise RuntimeError(f"Incomplete arXiv metadata response; missing IDs: {', '.join(sorted(missing))}")
            self._paper_cache.update(fetched)
            logger.info("Fetched metadata for %s papers in one batch", len(batch))

    def enrich_papers(self, items):
        self.prefetch(item["id"] for item in items)
        return [self.process_item(dict(item), None) for item in items]

    def process_item(self, item: dict, spider):
        paper = self._paper_cache.get(item["id"])
        if paper is None:
            self.prefetch([item["id"]])
            paper = self._paper_cache[item["id"]]
        version_id = paper.get_short_id()
        if not re.fullmatch(rf"{re.escape(item['id'])}v[1-9]\d*", version_id):
            raise ValueError(f"Unexpected arXiv version ID {version_id!r} for {item['id']!r}")
        self._paper_cache[item["id"]] = paper
        item["version_id"] = version_id
        item["pdf"] = f"https://arxiv.org/pdf/{version_id}"
        item["abs"] = f"https://arxiv.org/abs/{version_id}"
        item["authors"] = [a.name for a in paper.authors]
        item["title"] = paper.title
        item["categories"] = list(paper.categories)
        item["comment"] = paper.comment
        item["summary"] = paper.summary
        return item
