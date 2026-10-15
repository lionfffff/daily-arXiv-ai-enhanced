import scrapy
import os
import re


class ArxivSpider(scrapy.Spider):
    section_types = {
        "new": "new",
        "cross": "cross",
        "replacement": "replacement",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        categories = os.environ.get("CATEGORIES", "cs.CV")
        categories = categories.split(",")
        # 保存目标分类列表，用于后续验证
        self.target_categories = set(map(str.strip, categories))
        self.start_urls = [
            f"https://arxiv.org/list/{cat}/new" for cat in self.target_categories
        ]  # 起始URL（计算机科学领域的最新论文）

    name = "arxiv"  # 爬虫名称
    allowed_domains = ["arxiv.org"]  # 允许爬取的域名

    def parse(self, response):
        sections = [heading.strip() for heading in response.css("h3::text").getall()]
        self.logger.info("arXiv listing %s (HTTP %s): %s", response.url, response.status, sections)
        # The list navigation marks the first item in each announcement section.
        section_starts = []
        for li in response.css("div[id=dlpage] ul li"):
            href = li.css("a::attr(href)").get()
            label = li.css("a::text").get(default="").strip().casefold()
            match = re.fullmatch(r"#item(\d+)", href or "")
            if match:
                section_type = next(
                    (kind for prefix, kind in self.section_types.items() if label.startswith(prefix)),
                    None,
                )
                if section_type:
                    section_starts.append((int(match.group(1)), section_type))
        section_starts.sort()

        # 遍历每篇论文的详细信息
        for paper in response.css("dl dt"):
            paper_anchor = paper.css("a[name^='item']::attr(name)").get()
            if not paper_anchor:
                continue
                
            match = re.fullmatch(r"item(\d+)", paper_anchor)
            if not match:
                continue
            paper_id = int(match.group(1))
            submission_type = next(
                (kind for start, kind in reversed(section_starts) if paper_id >= start),
                None,
            )
            if submission_type is None:
                self.logger.warning("Could not identify announcement type for item %s", paper_id)
                continue

            # 获取论文ID
            abstract_link = paper.css("a[title='Abstract']::attr(href)").get()
            if not abstract_link:
                continue
                
            arxiv_id = abstract_link.split("/abs/", 1)[-1]
            
            # 获取对应的论文描述部分 (dd元素)
            paper_dd = paper.xpath("following-sibling::dd[1]")
            if not paper_dd:
                continue
            
            # Cross-listed papers may have the target category after the primary one.
            subjects_text = paper_dd.css(".list-subjects").xpath("string(.)").get()
            
            if subjects_text:
                # 解析分类信息，通常格式如 "Computer Vision and Pattern Recognition (cs.CV)"
                # 提取括号中的分类代码
                categories_in_paper = re.findall(r'\(([^)]+)\)', subjects_text)
                
                # 检查论文分类是否与目标分类有交集
                paper_categories = set(categories_in_paper)
                if paper_categories.intersection(self.target_categories):
                    yield {
                        "id": arxiv_id,
                        "submission_type": submission_type,
                        "categories": list(paper_categories),  # 添加分类信息用于调试
                    }
                    self.logger.info(f"Found paper {arxiv_id} with categories {paper_categories}")
                else:
                    self.logger.debug(f"Skipped paper {arxiv_id} with categories {paper_categories} (not in target {self.target_categories})")
            else:
                # 如果无法获取分类信息，记录警告但仍然返回论文（保持向后兼容）
                self.logger.warning(f"Could not extract categories for paper {arxiv_id}, including anyway")
                yield {
                    "id": arxiv_id,
                    "submission_type": submission_type,
                    "categories": [],
                }
