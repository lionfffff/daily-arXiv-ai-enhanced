import copy
from datetime import date
import os
import re

import scrapy


MONTHS = {name: number for number, name in enumerate((
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
), start=1)}


def listing_date(sections):
    """Read arXiv's announcement date without depending on Windows locale."""
    for heading in sections:
        match = re.fullmatch(r"Showing new listings for [A-Za-z]+, (\d{1,2}) ([A-Za-z]+) (\d{4})", heading)
        if match and match.group(2) in MONTHS:
            return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1)))
    return None


def text_content(selector, prefix=""):
    """Keep real URLs where arXiv renders a link as 'this https URL'."""
    if not selector:
        return ""
    node = selector[0] if isinstance(selector, list) else selector
    root = copy.deepcopy(node.root)
    for link in root.xpath(".//a[@href]"):
        if "".join(link.itertext()).strip() in {"this https URL", "this http URL"}:
            link.text = link.get("href")
    return " ".join("".join(root.itertext()).split()).removeprefix(prefix).strip()


class ArxivSpider(scrapy.Spider):
    name = "arxiv"
    allowed_domains = ["arxiv.org"]
    section_types = {"new": "new", "cross": "cross", "replacement": "replacement"}

    def __init__(self, *args, expected_listing_date=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.target_categories = {category.strip() for category in os.environ.get("CATEGORIES", "cs.CV").split(",") if category.strip()}
        if not self.target_categories:
            raise ValueError("CATEGORIES must contain at least one arXiv category")
        self.parsed_listing_urls = set()
        self.pending_metadata = {}
        self.detail_cache = {}
        self.expected_entries = 0
        self.expected_listing_date = date.fromisoformat(expected_listing_date) if expected_listing_date else None
        self.listing_dates = {}
        self.pending_listing_dates = {}
        self.start_urls = [f"https://arxiv.org/list/{cat}/new" for cat in sorted(self.target_categories)]

    def parse(self, response):
        if not response.css("div[id=dlpage]"):
            raise ValueError(f"Unexpected arXiv listing page: {response.url}")
        sections = [heading.strip() for heading in response.css("h3::text").getall()]
        self.logger.info("arXiv listing %s (HTTP %s): %s", response.url, response.status, sections)
        announced = listing_date(sections)
        if announced:
            self.listing_dates[response.url] = announced.isoformat()
        if self.expected_listing_date:
            if announced is None:
                raise ValueError(f"Cannot verify arXiv listing date: {response.url}")
            if announced < self.expected_listing_date:
                self.pending_listing_dates[response.url] = announced.isoformat()
                self.logger.info("Waiting for arXiv announcement %s; %s still shows %s", self.expected_listing_date, response.url, announced)
                return
        section_starts = []
        for li in response.css("div[id=dlpage] ul li"):
            href = li.css("a::attr(href)").get()
            label = li.css("a::text").get(default="").strip().casefold()
            match = re.fullmatch(r"#item(\d+)", href or "")
            section_type = next((kind for prefix, kind in self.section_types.items() if label.startswith(prefix)), None)
            if match and section_type:
                section_starts.append((int(match.group(1)), section_type))
        section_starts.sort()
        if response.css("dl dt") and not section_starts:
            raise ValueError(f"Could not identify arXiv announcement sections: {response.url}")

        count = 0
        for paper in response.css("dl dt"):
            anchor = paper.css("a[name^='item']::attr(name)").get(default="")
            match = re.fullmatch(r"item(\d+)", anchor)
            if not match:
                raise ValueError(f"Unrecognized arXiv listing entry: {response.url}")
            number = int(match.group(1))
            submission_type = next((kind for start, kind in reversed(section_starts) if number >= start), None)
            abstract_link = paper.css("a[title='Abstract']::attr(href)").get()
            dd = paper.xpath("following-sibling::dd[1]")
            if submission_type is None or not abstract_link or not dd:
                raise ValueError(f"Incomplete arXiv listing entry {number}: {response.url}")
            arxiv_id = abstract_link.split("/abs/", 1)[-1]
            subjects = text_content(dd.css(".list-subjects"))
            categories = set(re.findall(r"\(([^)]+)\)", subjects))
            if categories and not categories.intersection(self.target_categories):
                continue

            item = {
                "id": arxiv_id,
                "submission_type": submission_type,
                "categories": sorted(categories),
                "title": text_content(dd.css(".list-title"), "Title:"),
                "summary": text_content(dd.css("p.mathjax")),
                "authors": [text_content(author) for author in dd.css(".list-authors a")],
                "comment": text_content(dd.css(".list-comments"), "Comments:") or None,
                "metadata_source": "arxiv_listing",
            }
            if not item["title"] or not item["summary"] or not item["authors"]:
                raise ValueError(f"Missing title, abstract or authors in listing for {arxiv_id}")

            # Use an explicit version advertised by the listing. Never infer v1
            # for cross-lists or replacements that lack a versioned link.
            versions = set()
            for link in paper.css("a::attr(href)").getall():
                version_match = re.fullmatch(
                    rf"(?:https?://arxiv\.org)?/(?:html|pdf|abs)/({re.escape(arxiv_id)}v[1-9]\d*)(?:\.pdf)?", link
                )
                if version_match:
                    versions.add(version_match.group(1))
            if len(versions) > 1:
                raise ValueError(f"Conflicting listing versions for {arxiv_id}: {sorted(versions)}")
            if versions:
                item["version_id"] = versions.pop()
            self.expected_entries += 1
            if item.get("version_id"):
                yield item
            elif arxiv_id in self.detail_cache:
                yield {**item, **self.detail_cache[arxiv_id]}
            elif arxiv_id in self.pending_metadata:
                self.pending_metadata[arxiv_id].append(item)
            else:
                self.pending_metadata[arxiv_id] = [item]
                yield scrapy.Request(
                    f"https://arxiv.org/abs/{arxiv_id}",
                    callback=self.parse_details,
                    cb_kwargs={"arxiv_id": arxiv_id},
                )
            count += 1

        self.parsed_listing_urls.add(response.url)
        self.logger.info("Parsed %s complete listing entries from %s", count, response.url)

    def parse_details(self, response, arxiv_id):
        actual_id = response.css('meta[name="citation_arxiv_id"]::attr(content)').get()
        history = text_content(response.css(".submission-history"))
        versions = re.findall(r"\[v([1-9]\d*)\]", history)
        if actual_id != arxiv_id or not versions:
            raise ValueError(f"Cannot verify arXiv version for {arxiv_id}: {response.url}")
        metadata = {
            "version_id": f"{arxiv_id}v{max(map(int, versions))}",
            "title": response.css('meta[name="citation_title"]::attr(content)').get(),
            "authors": response.css('meta[name="citation_author"]::attr(content)').getall(),
            "summary": text_content(response.css("blockquote.abstract"), "Abstract:"),
            "comment": text_content(response.css(".tablecell.comments")) or None,
            "metadata_source": "arxiv_abstract",
        }
        if not all(metadata.get(key) for key in ("title", "authors", "summary")):
            raise ValueError(f"Incomplete arXiv abstract metadata for {arxiv_id}")
        self.detail_cache[arxiv_id] = metadata
        for item in self.pending_metadata.pop(arxiv_id):
            yield {**item, **metadata}
