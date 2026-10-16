import os
import unittest
from unittest.mock import patch

from scrapy.http import HtmlResponse
from scrapy import Request

from daily_arxiv.daily_arxiv.spiders.arxiv import ArxivSpider


DEFAULT_SUBJECTS = '<span class="primary-subject">Computer Vision and Pattern Recognition (cs.CV)</span>'


def make_response(sections, subjects=DEFAULT_SUBJECTS):
    links = []
    entries = []
    number = 1
    for label, paper_ids in sections:
        links.append(f'<li><a href="#item{number}">{label}</a></li>')
        entries.append(f"<h3>{label}</h3>")
        for paper_id in paper_ids:
            entries.append(
                f'<dt><a name="item{number}">[{number}]</a>'
                f'<a title="Abstract" href="/abs/{paper_id}">arXiv:{paper_id}</a>'
                f'<a href="https://arxiv.org/html/{paper_id}v1">html</a></dt>'
                f'<dd><div class="list-subjects">{subjects}</div>'
                '<div class="list-title"><span class="descriptor">Title:</span>Human pose</div>'
                '<div class="list-authors"><a>Researcher</a></div>'
                '<p class="mathjax">An abstract with <a href="https://example.org/code">this https URL</a></p></dd>'
            )
            number += 1
    html = f'<div id="dlpage"><ul>{"".join(links)}</ul><dl>{"".join(entries)}</dl></div>'
    return HtmlResponse(
        url="https://arxiv.org/list/cs.CV/new", body=html.encode("utf-8"), encoding="utf-8"
    )


class ArxivSpiderSectionTests(unittest.TestCase):
    def papers(self, sections, subjects=DEFAULT_SUBJECTS):
        with patch.dict(os.environ, {"CATEGORIES": "cs.CV"}):
            spider = ArxivSpider()
        return list(spider.parse(make_response(sections, subjects)))

    def paper_ids(self, sections):
        return [item["id"] for item in self.papers(sections)]

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
        self.assertEqual(
            [("2410.00001", "replacement", ["cs.CV"])],
            [(p["id"], p["submission_type"], p["categories"]) for p in self.papers([("Replacements", ["2410.00001"])])],
        )

    def test_replacements_after_new_and_cross(self):
        self.assertEqual(
            ["new", "cross", "replacement"],
            [item["submission_type"] for item in self.papers(
                [
                    ("New submissions", ["2610.00001"]),
                    ("Cross-lists", ["2610.00002"]),
                    ("Replacements", ["2410.00001"]),
                ]
            )],
        )

    def test_legacy_replacement_id_keeps_category_prefix(self):
        self.assertEqual(
            ["cs/0601001"],
            self.paper_ids([("Replacements", ["cs/0601001"])]),
        )

    def test_cross_list_with_different_primary_category(self):
        papers = self.papers(
            [("Cross-lists", ["2610.00003"])],
            '<span class="primary-subject">Robotics (cs.RO)</span>; '
            'Computer Vision and Pattern Recognition (cs.CV)',
        )
        self.assertEqual(["2610.00003"], [paper["id"] for paper in papers])
        self.assertEqual({"cs.RO", "cs.CV"}, set(papers[0]["categories"]))

    def test_listing_metadata_keeps_urls_and_explicit_revision(self):
        response = make_response([("Replacements", ["2410.00001"])])
        body = response.text.replace('2410.00001v1', '2410.00001v3')
        spider = ArxivSpider()
        paper = list(spider.parse(response.replace(body=body.encode('utf-8'))))[0]
        self.assertEqual("2410.00001v3", paper["version_id"])
        self.assertEqual("Human pose", paper["title"])
        self.assertEqual(["Researcher"], paper["authors"])
        self.assertEqual("An abstract with https://example.org/code", paper["summary"])

    def test_unversioned_replacement_resolves_once_for_multiple_categories(self):
        response = make_response([("Replacements", ["2410.00001"])])
        response = response.replace(body=response.text.replace('https://arxiv.org/html/2410.00001v1', '/format/2410.00001').encode('utf-8'))
        spider = ArxivSpider()
        request = list(spider.parse(response))[0]
        self.assertIsInstance(request, Request)
        self.assertEqual('https://arxiv.org/abs/2410.00001', request.url)
        self.assertEqual([], list(spider.parse(response)))
        detail = HtmlResponse(url=request.url, encoding='utf-8', body=b'''
            <meta name="citation_arxiv_id" content="2410.00001">
            <meta name="citation_title" content="Updated title">
            <meta name="citation_author" content="Author">
            <div class="submission-history">[v1] date [v3] date [v2] date</div>
            <blockquote class="abstract">Abstract: Updated abstract</blockquote>
        ''')
        papers = list(spider.parse_details(detail, '2410.00001'))
        self.assertEqual(2, len(papers))
        self.assertEqual('2410.00001v3', papers[0]['version_id'])
        self.assertEqual('Updated abstract', papers[0]['summary'])
        self.assertEqual({}, spider.pending_metadata)
        cached = list(spider.parse(response))[0]
        self.assertEqual('2410.00001v3', cached['version_id'])

    def test_detail_page_requires_matching_id_and_explicit_version_history(self):
        for body in [b'<meta name="citation_arxiv_id" content="other"><div class="submission-history">[v2]</div>', b'<meta name="citation_arxiv_id" content="2410.00001">']:
            with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                list(ArxivSpider().parse_details(HtmlResponse(url='https://arxiv.org/abs/2410.00001', body=body, encoding='utf-8'), '2410.00001'))

    def test_missing_abstract_fails_instead_of_excluding_from_keyword_gate(self):
        response = make_response([("New submissions", ["2610.00001"])])
        response = response.replace(body=response.text.replace('class="mathjax"', 'class="changed-layout"').encode('utf-8'))
        with self.assertRaisesRegex(ValueError, "Missing title, abstract or authors"):
            list(ArxivSpider().parse(response))


if __name__ == "__main__":
    unittest.main()
