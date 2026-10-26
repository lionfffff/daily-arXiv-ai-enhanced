"""Run and publish a complete daily batch in an isolated, recoverable checkout.

No GitHub runner is involved. Credentials are inherited through the process
environment and Git's configured credential helper, never copied into files.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

from daily_arxiv.daily_arxiv.check_stats import paper_version_id


AI_FIELDS = ("tldr", "motivation", "method", "result", "conclusion")
FAILED_AI_VALUES = {
    "Summary generation failed", "Motivation analysis unavailable",
    "Method extraction failed", "Result analysis unavailable",
    "Conclusion extraction failed",
}
OWNER_FILE = ".daily-arxiv-local-runner.json"
WAITING_RELEASE_EXIT_CODE = 3


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_rows(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def merge_rows(existing, incoming):
    """A concurrent publisher's already-published row wins for the same version."""
    merged = {paper_version_id(row): row for row in incoming}
    merged.update({paper_version_id(row): row for row in existing})
    return list(merged.values())


def validate_complete(raw, enhanced):
    raw_ids = [paper_version_id(row) for row in raw]
    enhanced_ids = [paper_version_id(row) for row in enhanced]
    if len(set(raw_ids)) != len(raw_ids) or len(set(enhanced_ids)) != len(enhanced_ids):
        raise ValueError("Duplicate paper versions in the batch")
    if set(raw_ids) != set(enhanced_ids):
        raise ValueError("Raw and enhanced paper versions do not match; refusing partial publication")
    for row in enhanced:
        identity = paper_version_id(row)
        if not isinstance(row.get("summary_zh"), str) or not re.search(r"[\u3400-\u9fff]", row["summary_zh"]):
            raise ValueError(f"Missing Chinese abstract for {identity}")
        ai = row.get("AI")
        if not isinstance(ai, dict) or any(
            not isinstance(ai.get(key), str) or not ai[key].strip() or ai[key].strip() in FAILED_AI_VALUES
            for key in AI_FIELDS
        ):
            raise ValueError(f"Incomplete AI summary for {identity}")


def require_owned_child(runtime: Path, child: Path):
    """Destructive Git operations are allowed only in this runner's directory."""
    runtime, child = runtime.resolve(), child.resolve()
    if not (runtime / OWNER_FILE).is_file() or child == runtime or not child.is_relative_to(runtime):
        raise ValueError(f"Refusing to modify unmanaged checkout: {child}")


@contextmanager
def exclusive_workspace(runtime: Path):
    """OS advisory lock releases automatically if the process crashes."""
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "pipeline.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another local pipeline is already using this workspace") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Commands:
    def __init__(self, logs: Path, env):
        self.logs = logs
        self.logs.mkdir(parents=True, exist_ok=True)
        self.env = env
        self.secrets = [value for name, value in env.items() if value and any(
            word in name.upper() for word in ("API_KEY", "TOKEN", "PASSWORD")
        )]

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def run(self, args, *, cwd: Path, label: str, allowed=(0,)):
        args = [str(arg) for arg in args]
        print(f"[{label}]", flush=True)
        with (self.logs / f"{label}.log").open("a", encoding="utf-8") as output:
            output.write(f"\nStarted: {utc_now()}\n")
            process = subprocess.Popen(
                args, cwd=cwd, env=self.env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            lines = []
            with process.stdout:
                for line in process.stdout:
                    line = self.redact(line)
                    lines.append(line)
                    output.write(line)
                    output.flush()
                    print(line, end="", flush=True)
            returncode = process.wait()
            output.write(f"\nExit code: {returncode}\n")
        if returncode not in allowed:
            raise RuntimeError(f"{label} failed (exit {returncode}); see {self.logs / (label + '.log')}")
        return returncode, "".join(lines)


def git_value(repo: Path, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return result.stdout.strip()


def prepare_checkout(repo: Path, attempt: Path, commands: Commands):
    """Clone committed code locally; exclude untracked files and local .env files."""
    checkout = attempt / "checkout"
    remote = git_value(repo, "remote", "get-url", "origin")
    if "://" in remote and urlsplit(remote).username:
        raise ValueError("Origin URL contains credentials; configure Git Credential Manager instead")
    commands.run(
        ["git", "clone", "--depth", "1", "--no-hardlinks", repo.resolve().as_uri(), checkout],
        cwd=attempt, label="clone-code",
    )
    commands.run(["git", "remote", "set-url", "origin", remote], cwd=checkout, label="set-origin")
    commands.run(
        ["git", "fetch", "--depth", "1", "origin", "data:refs/remotes/origin/data"],
        cwd=checkout, label="fetch-history",
    )
    # Data belongs only to this attempt; tracked data on main would be ambiguous.
    if (checkout / "data").exists():
        raise RuntimeError("Code branch unexpectedly contains a data directory")
    (checkout / "data").mkdir()
    return checkout, remote


def publish_batch(runtime, attempt, checkout, remote, target_date, language, commands):
    """Publish atomically, rebuilding the merge if another writer wins the race."""
    publication = attempt / "publication"
    publication.mkdir()
    require_owned_child(runtime, publication)
    commands.run(["git", "init"], cwd=publication, label="publish-init")
    commands.run(["git", "remote", "add", "origin", remote], cwd=publication, label="publish-origin")
    raw_name = f"{target_date}.jsonl"
    enhanced_name = f"{target_date}_AI_enhanced_{language}.jsonl"
    new_raw = read_rows(checkout / "data" / raw_name)
    new_enhanced = read_rows(checkout / "data" / enhanced_name)
    validate_complete(new_raw, new_enhanced)
    for retry in range(1, 4):
        commands.run(["git", "fetch", "--depth", "1", "origin", "data"], cwd=publication, label=f"publish-fetch-{retry}")
        # This fresh checkout contains only this runner's staged output.
        require_owned_child(runtime, publication)
        commands.run(["git", "checkout", "-B", "data", "FETCH_HEAD"], cwd=publication, label=f"publish-checkout-{retry}")
        merged_raw = merge_rows(read_rows(publication / "data" / raw_name), new_raw)
        merged_enhanced = merge_rows(read_rows(publication / "data" / enhanced_name), new_enhanced)
        validate_complete(merged_raw, merged_enhanced)
        write_rows(publication / "data" / raw_name, merged_raw)
        write_rows(publication / "data" / enhanced_name, merged_enhanced)
        # Render in the code checkout to use its checked-in template. A relative
        # data argument also avoids convert.py interpreting underscores in paths.
        write_rows(checkout / "data" / enhanced_name, merged_enhanced)
        commands.run(
            [sys.executable, "-X", "utf8", "convert.py", "--data", f"../data/{enhanced_name}"],
            cwd=checkout / "to_md", label=f"publish-markdown-{retry}",
        )
        shutil.copy2(checkout / "data" / f"{target_date}.md", publication / "data" / f"{target_date}.md")
        listing = publication / "assets" / "file-list.txt"
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_text("".join(path.name + "\n" for path in sorted((publication / "data").glob("*.jsonl"))), encoding="utf-8")
        commands.run(["git", "add", "data", "assets/file-list.txt"], cwd=publication, label=f"publish-stage-{retry}")
        changed, _ = commands.run(["git", "diff", "--cached", "--quiet"], cwd=publication, label=f"publish-changed-{retry}", allowed=(0, 1))
        if not changed:
            return {"published": True, "commit": git_value(publication, "rev-parse", "HEAD"), "published_count": len(merged_enhanced)}
        commands.run([
            "git", "-c", "user.name=arXiv local runner", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", f"update: {target_date} arXiv papers (Windows)",
        ], cwd=publication, label=f"publish-commit-{retry}")
        code, _ = commands.run(["git", "push", "origin", "HEAD:refs/heads/data"], cwd=publication, label=f"publish-push-{retry}", allowed=(0, 1, 128))
        if code == 0:
            return {"published": True, "commit": git_value(publication, "rev-parse", "HEAD"), "published_count": len(merged_enhanced)}
    raise RuntimeError("Publication failed after three fetch-and-merge attempts; complete local results are preserved")


def run_pipeline(repo: Path, runtime: Path, target_date: str, env=None, *, no_publish=False, expected_listing_date=None):
    date.fromisoformat(target_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        raise ValueError("Date must use YYYY-MM-DD")
    if expected_listing_date:
        expected_listing_date = date.fromisoformat(expected_listing_date).isoformat()
    repo, runtime = Path(repo).resolve(), Path(runtime).resolve()
    if runtime == repo or repo.is_relative_to(runtime / "runs") or runtime.is_relative_to(repo):
        raise ValueError("Runtime must be separate from the source repository")
    environment = {**os.environ, **(env or {})}
    environment.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    for name, default in {
        "LANGUAGE": "Chinese", "CATEGORIES": "cs.CV,cs.AI,cs.LG",
        "MODEL_NAME": "deepseek-v4-pro", "OPENAI_BASE_URL": "https://api.deepseek.com",
        "RELEVANCE_MODE": "semantic",
    }.items():
        environment.setdefault(name, default)
    language = environment["LANGUAGE"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", language):
        raise ValueError("LANGUAGE must be a simple filename-safe language name")
    with exclusive_workspace(runtime):
        owner = runtime / OWNER_FILE
        if owner.exists():
            if json.loads(owner.read_text(encoding="utf-8")).get("application") != "daily-arxiv-local-runner":
                raise ValueError("Runtime belongs to a different application")
        else:
            write_json(owner, {"application": "daily-arxiv-local-runner", "schema": 1})
        attempt = runtime / "runs" / target_date / (datetime.now(timezone.utc).strftime("%H%M%S") + "-" + uuid.uuid4().hex[:8])
        attempt.mkdir(parents=True)
        status = {"date": target_date, "status": "running", "stage": "prepare", "started_at": utc_now(), "attempt": str(attempt), "counts": {}, "published": False}
        if expected_listing_date:
            status["expected_listing_date"] = expected_listing_date
        commands = Commands(attempt / "logs", environment)

        def stage(name):
            status["stage"] = name
            write_json(attempt / "status.json", status)
            write_json(runtime / "status.json", status)

        def complete(reason):
            status.update(status="success", stage="complete", reason=reason, finished_at=utc_now())
            write_json(attempt / "status.json", status)
            write_json(runtime / "status.json", status)
            print(json.dumps(status, ensure_ascii=False), flush=True)
            return status

        try:
            stage("prepare")
            if not environment.get("OPENAI_API_KEY", "").strip():
                raise ValueError("Missing OPENAI_API_KEY in the runner's process environment")
            checkout, remote = prepare_checkout(repo, attempt, commands)
            raw = checkout / "data" / f"{target_date}.jsonl"
            enhanced = checkout / "data" / f"{target_date}_AI_enhanced_{language}.jsonl"
            stage("crawl")
            crawl_args = [sys.executable, "-X", "utf8", "-m", "daily_arxiv.crawl", "--output", f"../data/{raw.name}"]
            if expected_listing_date:
                crawl_args.extend(["--expected-listing-date", expected_listing_date])
            crawl_code, _ = commands.run(crawl_args, cwd=checkout / "daily_arxiv", label="crawl", allowed=(0, WAITING_RELEASE_EXIT_CODE))
            if crawl_code == WAITING_RELEASE_EXIT_CODE:
                status.update(status="waiting_release", reason="announcement_not_ready", finished_at=utc_now())
                write_json(attempt / "status.json", status)
                write_json(runtime / "status.json", status)
                print(json.dumps(status, ensure_ascii=False), flush=True)
                return status
            if not raw.is_file():
                raise RuntimeError("Crawler did not produce the requested data file")
            shutil.copy2(raw, attempt / "original-crawl.jsonl")
            status["counts"]["crawled"] = len(read_rows(raw))
            stage("deduplicate")
            code, _ = commands.run([sys.executable, "-X", "utf8", "daily_arxiv/check_stats.py", "--date", target_date], cwd=checkout / "daily_arxiv", label="deduplicate", allowed=(0, 1))
            status["counts"]["new_versions"] = len(read_rows(raw))
            if code == 1:
                return complete("no_new_content" if status["counts"]["crawled"] else "no_papers")
            shutil.copy2(raw, attempt / "deduplicated.jsonl")
            stage("relevance")
            _, output = commands.run([sys.executable, "-X", "utf8", "-m", "ai.relevance_filter", "--data", f"data/{raw.name}"], cwd=checkout, label="relevance")
            for line in reversed(output.splitlines()):
                try:
                    stats = json.loads(line)
                except ValueError:
                    continue
                if isinstance(stats, dict) and "kept" in stats:
                    status["relevance"] = stats
                    break
            status["counts"]["selected"] = len(read_rows(raw))
            if not status["counts"]["selected"]:
                return complete("no_relevant_content")
            shutil.copy2(raw, attempt / "selected.jsonl")
            stage("enhance")
            commands.run([sys.executable, "-X", "utf8", "enhance.py", "--data", f"../data/{raw.name}"], cwd=checkout / "ai", label="enhance")
            stage("merge_same_date")
            commands.run([sys.executable, "-X", "utf8", "-m", "daily_arxiv.daily_arxiv.merge_published", f"data/{raw.name}", f"data/{enhanced.name}"], cwd=checkout, label="merge-same-date")
            stage("translate")
            commands.run([sys.executable, "-X", "utf8", "ai/translate_abstracts.py", f"data/{enhanced.name}"], cwd=checkout, label="translate")
            stage("validate")
            validate_complete(read_rows(raw), read_rows(enhanced))
            commands.run([sys.executable, "-X", "utf8", "convert.py", "--data", f"../data/{enhanced.name}"], cwd=checkout / "to_md", label="markdown")
            status["counts"]["complete"] = len(read_rows(enhanced))
            if no_publish:
                return complete("prepared_without_publication")
            stage("publish")
            status.update(publish_batch(runtime, attempt, checkout, remote, target_date, language, commands))
            return complete("published")
        except Exception as exc:
            status.update(status="failed", error=commands.redact(str(exc)), finished_at=utc_now())
            write_json(attempt / "status.json", status)
            write_json(runtime / "status.json", status)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--date", required=True, help="Fixed Beijing calendar date, YYYY-MM-DD")
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--expected-listing-date", help="Minimum announced batch date, supplied by the scheduler")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(Path(__file__).resolve().parents[1], args.workspace, args.date, no_publish=args.no_publish, expected_listing_date=args.expected_listing_date)
    except Exception as exc:
        # Detailed (redacted) failure information is also preserved in status.json.
        print(f"Local pipeline failed: {type(exc).__name__}; see {args.workspace / 'status.json'}", file=sys.stderr)
        return 1
    return WAITING_RELEASE_EXIT_CODE if result["status"] == "waiting_release" else 0


if __name__ == "__main__":
    raise SystemExit(main())
