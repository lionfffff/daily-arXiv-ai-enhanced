import os
import unittest
from unittest.mock import patch

from scrapy.http import HtmlResponse

from daily_arxiv.daily_arxiv.spiders.arxiv import ArxivSpider


def make_response(sections):
    links = []
    entries = []
    number = 1
    for label, paper_ids in sections:
        links.append(f'<li><a href="#item{number}">{label}</a></li>')
        entries.append(f"<h3>{label}</h3>")
        for paper_id in paper_ids:
            entries.append(
                f'<dt><a name="item{number}">[{number}]</a>'
                f'<a title="Abstract" href="/abs/{paper_id}">arXiv:{paper_id}</a></dt>'
                '<dd><div class="list-subjects"><span class="primary-subject">'
                'Computer Vision and Pattern Recognition (cs.CV)</span></div></dd>'
            )
            number += 1
    html = f'<div id="dlpage"><ul>{"".join(links)}</ul><dl>{"".join(entries)}</dl></div>'
    return HtmlResponse(
        url="https://arxiv.org/list/cs.CV/new", body=html.encode("utf-8"), encoding="utf-8"
    )


class ArxivSpiderSectionTests(unittest.TestCase):
    def paper_ids(self, sections):
        with patch.dict(os.environ, {"CATEGORIES": "cs.CV"}):
            spider = ArxivSpider()
        return [item["id"] for item in spider.parse(make_response(sections))]

    def test_new_only(self):
        self.assertEqual(
            ["2610.00001", "2610.00002"],
            self.paper_ids([("New submissions", ["2610.00001", "2610.00002"])]),
        )

    def test_new_and_cross_without_replacements(self):
        self.assertEqual(
            ["2610.00001", "2610.00002"],
            self.paper_ids(
                [
                    ("New submissions", ["2610.00001"]),
                    ("Cross-lists", ["2610.00002"]),
                ]
            ),
        )

    def test_replacements_only(self):
        self.assertEqual([], self.paper_ids([("Replacements", ["2410.00001"])]))

    def test_replacements_after_new_and_cross(self):
        self.assertEqual(
            ["2610.00001", "2610.00002"],
            self.paper_ids(
                [
                    ("New submissions", ["2610.00001"]),
                    ("Cross-lists", ["2610.00002"]),
                    ("Replacements", ["2410.00001"]),
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
