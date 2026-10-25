#!/usr/bin/env python3
"""
检查Scrapy爬取统计信息的脚本 / Script to check Scrapy crawling statistics
用于获取去重检查的状态结果 / Used to get deduplication check status results

功能说明 / Features:
- 检查当日与昨日论文数据的重复情况 / Check duplication between today's and yesterday's paper data
- 删除重复论文条目，保留新内容 / Remove duplicate papers, keep new content
- 根据去重后的结果决定工作流是否继续 / Decide workflow continuation based on deduplication results
"""
import argparse
import json
import sys
import os
import re
import subprocess
from datetime import date, datetime, timedelta

BASE_ID_PATTERN = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})\Z")
VERSION_SUFFIX_PATTERN = re.compile(r"v[1-9]\d*\Z")
SUBMISSION_PRIORITY = {"cross": 1, "new": 2, "replacement": 3}


def paper_version_id(paper):
    """Identify a specific revision, treating unversioned historic rows as v1."""
    if not isinstance(paper, dict):
        raise ValueError("Paper must be an object")

    paper_id = paper.get("id")
    if not isinstance(paper_id, str) or not paper_id:
        raise ValueError("Paper is missing an arXiv ID")

    version_id = paper.get("version_id")
    if version_id is not None:
        if (
            not isinstance(version_id, str)
            or not version_id.startswith(f"{paper_id}v")
            or not VERSION_SUFFIX_PATTERN.fullmatch(version_id[len(paper_id):])
        ):
            raise ValueError(f"Invalid version_id for {paper_id}: {version_id}")
        return version_id

    base_id = re.sub(r"v[1-9]\d*\Z", "", paper_id)
    if not BASE_ID_PATTERN.fullmatch(base_id):
        raise ValueError(f"Invalid arXiv ID: {paper_id}")
    return paper_id if base_id != paper_id else f"{paper_id}v1"


def load_papers_data(file_path):
    """
    从jsonl文件中加载完整的论文数据
    Load complete paper data from jsonl file
    
    Args:
        file_path (str): JSONL文件路径 / JSONL file path
        
    Returns:
        list: 论文数据列表 / List of paper data
        set: 论文版本ID集合 / Set of paper version IDs
    """
    if not os.path.exists(file_path):
        return [], set()
    
    papers = []
    ids = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    papers.append(data)
                    ids.add(paper_version_id(data))
        return papers, ids
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        raise

def save_papers_data(papers, file_path):
    """
    保存论文数据到jsonl文件
    Save paper data to jsonl file
    
    Args:
        papers (list): 论文数据列表 / List of paper data
        file_path (str): 文件路径 / File path
    """
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            for paper in papers:
                f.write(json.dumps(paper, ensure_ascii=False) + '\n')
        return True
    except Exception as e:
        print(f"Error saving {file_path}: {e}", file=sys.stderr)
        return False


def load_history_ids(date_str, *, published_only=False):
    """Read published history from the data branch when it is absent on main."""
    history_file = f"../data/{date_str}.jsonl"
    if not published_only and os.path.exists(history_file):
        _, ids = load_papers_data(history_file)
        return ids

    result = subprocess.run(
        ["git", "show", f"origin/data:data/{date_str}.jsonl"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        return set()
    try:
        return {paper_version_id(json.loads(line)) for line in result.stdout.splitlines() if line.strip()}
    except ValueError as exc:
        raise ValueError(f"Invalid history data for {date_str}") from exc

def parse_date(value):
    """Parse the workflow's fixed crawl date, rejecting ambiguous formats."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must be a valid YYYY-MM-DD value") from exc


def perform_deduplication(target_date=None):
    """
    执行多日去重：删除与历史多日重复的论文条目，保留新内容
    Perform deduplication over multiple past days
    
    Returns:
        str: 去重状态 / Deduplication status
             - "has_new_content": 有新内容 / Has new content
             - "no_new_content": 无新内容 / No new content  
             - "no_data": 无数据 / No data
             - "error": 处理错误 / Processing error
    """

    explicit_date = target_date is not None
    target_date = target_date or datetime.now().date()
    today = target_date.isoformat()
    today_file = f"../data/{today}.jsonl"
    history_days = 7  # 向前追溯几天的数据进行对比

    if not os.path.exists(today_file):
        print(f"数据文件不存在 / Data file does not exist: {today_file}", file=sys.stderr)
        # An explicitly requested file should have been produced by the crawler.
        # Missing it is a workflow error, not a day with zero submissions.
        return "error" if explicit_date else "no_data"

    try:
        today_papers, _ = load_papers_data(today_file)
        print(f"今日论文总数: {len(today_papers)} / Today's total papers: {len(today_papers)}", file=sys.stderr)

        if not today_papers:
            return "no_data"

        # 收集历史多日 ID 集合
        # Today's local file is the fresh crawl; compare it with the published copy.
        history_ids = load_history_ids(today, published_only=True)
        for i in range(1, history_days + 1):
            date_str = (target_date - timedelta(days=i)).isoformat()
            history_ids.update(load_history_ids(date_str))

        print(f"历史{history_days}日去重库大小: {len(history_ids)} / History {history_days} days deduplication library size: {len(history_ids)}", file=sys.stderr)

        unique_papers = {}
        for paper in today_papers:
            version_id = paper_version_id(paper)
            previous = unique_papers.get(version_id)
            if previous is None or SUBMISSION_PRIORITY.get(paper.get("submission_type"), 0) > SUBMISSION_PRIORITY.get(previous.get("submission_type"), 0):
                unique_papers[version_id] = paper

        duplicate_ids = unique_papers.keys() & history_ids
        new_papers = [paper for version_id, paper in unique_papers.items() if version_id not in history_ids]
        print(f"同日重复: {len(today_papers) - len(unique_papers)}; 历史重复: {len(duplicate_ids)} / Same-day duplicates: {len(today_papers) - len(unique_papers)}; historical duplicates: {len(duplicate_ids)}", file=sys.stderr)

        if not new_papers:
            try:
                os.remove(today_file)
                print("所有论文均为重复内容，已删除今日文件 / All papers are duplicate content, today's file deleted", file=sys.stderr)
            except Exception as e:
                print(f"删除文件失败: {e} / Failed to delete file: {e}", file=sys.stderr)
            return "no_new_content"

        if len(new_papers) != len(today_papers):
            if not save_papers_data(new_papers, today_file):
                print("保存去重后的数据失败 / Failed to save deduplicated data", file=sys.stderr)
                return "error"

        print(f"去重后剩余论文数: {len(new_papers)} / Remaining papers after deduplication: {len(new_papers)}", file=sys.stderr)
        return "has_new_content"

    except Exception as e:
        print(f"去重处理失败: {e} / Deduplication processing failed: {e}", file=sys.stderr)
        return "error"

def main(argv=None):
    """
    检查去重状态并返回相应的退出码
    Check deduplication status and return corresponding exit code
    
    退出码含义 / Exit code meanings:
    0: 有新内容，继续处理 / Has new content, continue processing
    1: 无新内容，停止工作流 / No new content, stop workflow
    2: 处理错误 / Processing error
    """
    
    parser = argparse.ArgumentParser(description="Deduplicate crawled papers against published history")
    parser.add_argument("--date", type=parse_date, help="Fixed crawl date in YYYY-MM-DD format")
    args = parser.parse_args(argv)

    print("正在执行去重检查... / Performing intelligent deduplication check...", file=sys.stderr)
    
    # 执行去重处理 / Perform deduplication processing
    dedup_status = perform_deduplication(args.date)
    
    if dedup_status == "has_new_content":
        print("✅ 去重完成，发现新内容，继续工作流 / Deduplication completed, new content found, continue workflow", file=sys.stderr)
        sys.exit(0)
    elif dedup_status == "no_new_content":
        print("⏹️ 去重完成，无新内容，停止工作流 / Deduplication completed, no new content, stop workflow", file=sys.stderr)
        sys.exit(1)
    elif dedup_status == "no_data":
        print("⏹️ 今日无数据，停止工作流 / No data today, stop workflow", file=sys.stderr)
        sys.exit(1)
    elif dedup_status == "error":
        print("❌ 去重处理出错，停止工作流 / Deduplication processing error, stop workflow", file=sys.stderr)
        sys.exit(2)
    else:
        # 意外情况：未知状态 / Unexpected case: unknown status
        print("❌ 未知去重状态，停止工作流 / Unknown deduplication status, stop workflow", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
