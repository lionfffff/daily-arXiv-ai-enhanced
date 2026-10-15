# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html


# useful for handling different item types with a single interface
import arxiv
import json
import os
import re
import sys
from datetime import datetime, timedelta


class DailyArxivPipeline:
    def __init__(self):
        self.page_size = 100
        self.client = arxiv.Client(self.page_size)
        self._paper_cache = {}

    def process_item(self, item: dict, spider):
        paper = self._paper_cache.get(item["id"])
        if paper is None:
            search = arxiv.Search(
                id_list=[item["id"]],
            )
            paper = next(self.client.results(search))
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
