"""Run one overdue arXiv release on this computer, with durable retry state.

Windows Task Scheduler may call this every minute. The state and process lock
make those calls safe even when a previous invocation is still processing.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=10)
PIPELINE_TIMEOUT_SECONDS = 45 * 60
CODE_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ReleaseSlot:
    date: str
    scheduled_at: datetime


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def latest_due_slot(now: datetime) -> ReleaseSlot:
    """Return the latest Sun–Thu 20:05 US Eastern slot, named by Beijing date."""
    if now.tzinfo is None:
        raise ValueError("An aware datetime is required")
    eastern_date = now.astimezone(NEW_YORK).date()
    for days_back in range(8):
        date = eastern_date - timedelta(days=days_back)
        if date.weekday() not in {6, 0, 1, 2, 3}:
            continue
        scheduled = datetime.combine(date, time(20, 5), tzinfo=NEW_YORK)
        if scheduled.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            return ReleaseSlot(
                scheduled.astimezone(BEIJING).date().isoformat(), scheduled
            )
    raise RuntimeError("No release slot found in the previous week")


def read_state(workspace: Path) -> dict:
    path = workspace / "scheduler-state.json"
    if not path.exists():
        return {}
    # Corrupt state must not silently reset a paid-processing retry limit.
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("scheduler-state.json must contain a JSON object")
    return state


def write_state(workspace: Path, state: dict) -> None:
    temporary = workspace / f"scheduler-state.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, workspace / "scheduler-state.json")


def log(workspace: Path, message: str) -> None:
    stamp = now_utc().astimezone(BEIJING).isoformat(timespec="seconds")
    line = f"[{stamp} 北京时间] {message}"
    print(line, flush=True)
    folder = workspace / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "scheduler.log").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


@contextmanager
def exclusive_lock(workspace: Path) -> Iterator[bool]:
    """Keep an OS-managed lock held across the entire pipeline subprocess."""
    with (workspace / ".scheduler.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
            try:
                yield acquired
            finally:
                if acquired:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            try:
                yield acquired
            finally:
                if acquired:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def network_ready(base_url: str) -> tuple[bool, str]:
    provider_host = urlparse(base_url).hostname
    if not provider_host:
        return False, "AI 接口地址没有有效主机名"
    for host in dict.fromkeys(["github.com", "arxiv.org", provider_host]):
        try:
            with socket.create_connection((host, 443), timeout=5):
                pass
        except OSError:
            return False, f"等待联网：无法连接 {host}:443"
    return True, ""


def run_pipeline(workspace: Path, date: str) -> int:
    folder = workspace / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    output_path = folder / f"pipeline-{date}-{stamp}.log"
    log(workspace, f"启动论文处理；执行日期 {date}；日志 {output_path}")
    with output_path.open("a", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "local_runner.pipeline",
             "--workspace", str(workspace), "--date", date],
            cwd=CODE_ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            return process.wait(timeout=PIPELINE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Scrapy and AI stages are children of the pipeline. Kill the tree
            # before releasing our lock so a retry cannot overlap orphan work.
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=30,
                )
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            raise


def status(workspace: Path, now: datetime | None = None) -> dict:
    """Inspect scheduling only: no credentials, connections, or mutations."""
    now = now or now_utc()
    slot = latest_due_slot(now)
    state = read_state(workspace)
    complete = (state.get("last_success_slot") or "") >= slot.date
    return {
        "beijing_time": now.astimezone(BEIJING).isoformat(timespec="seconds"),
        "due_slot": slot.date,
        "due_at_beijing": slot.scheduled_at.astimezone(BEIJING).isoformat(),
        "last_success_slot": state.get("last_success_slot"),
        "last_success_at": state.get("last_success_at"),
        "waiting": "up_to_date" if complete else (
            state.get("status", "due") if state.get("active_slot") == slot.date else "due"
        ),
        "attempts": state.get("attempts", 0) if state.get("active_slot") == slot.date else 0,
        "next_retry_at": state.get("next_retry_at") if state.get("active_slot") == slot.date else None,
        "last_error": state.get("last_error"),
    }


def run_once(workspace: Path, *, run_now: bool = False) -> int:
    workspace.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(workspace) as acquired:
        if not acquired:
            log(workspace, "已有论文任务运行，本次触发跳过。")
            return 0
        now = now_utc()
        slot = latest_due_slot(now)
        state = read_state(workspace)
        if not run_now and (state.get("last_success_slot") or "") >= slot.date:
            return 0
        if state.get("active_slot") != slot.date:
            state.update(active_slot=slot.date, attempts=0, next_retry_at=None, last_error=None)
        if not run_now and state.get("attempts", 0) >= MAX_ATTEMPTS:
            if state.get("status") != "retry_limit_reached":
                state["status"] = "retry_limit_reached"
                write_state(workspace, state)
                log(workspace, f"公告日 {slot.date} 已尝试 {MAX_ATTEMPTS} 次，等待人工检查或下一公告日。")
            return 0
        retry_at = state.get("next_retry_at")
        if not run_now and retry_at and now < datetime.fromisoformat(retry_at):
            return 0
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            state.update(status="waiting_credentials", last_error="尚未配置 OPENAI_API_KEY")
            changed = read_state(workspace).get("status") != state["status"]
            write_state(workspace, state)
            if changed:
                log(workspace, "等待配置 AI 密钥；本次不计入失败次数。")
            return 0
        online, reason = network_ready(os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"))
        if not online:
            changed = state.get("status") != "waiting_network"
            state.update(status="waiting_network", last_error=reason)
            write_state(workspace, state)
            if changed:
                log(workspace, reason + "；本次不计入失败次数。")
            return 0
        state.update(
            status="running", attempts=state.get("attempts", 0) + 1,
            last_attempt_at=now.astimezone(BEIJING).isoformat(),
            next_retry_at=(now + RETRY_DELAY).astimezone(BEIJING).isoformat(),
            last_error=None,
        )
        write_state(workspace, state)
        # Use the actual execution date; catching up a missed slot must never
        # label today's /new listing as an earlier day's historical listing.
        run_date = now.astimezone(BEIJING).date().isoformat()
        error = None
        try:
            returncode = run_pipeline(workspace, run_date)
        except subprocess.TimeoutExpired:
            returncode = 1
            error = f"论文处理超过 {PIPELINE_TIMEOUT_SECONDS // 60} 分钟超时"
        except OSError as exc:
            returncode = 1
            error = f"无法启动论文处理：{type(exc).__name__}"
        finished = now_utc().astimezone(BEIJING)
        state["last_run_time"] = finished.isoformat()
        if returncode == 0:
            state.update(
                status="success", last_success_slot=slot.date,
                last_success_at=finished.isoformat(), next_retry_at=None, last_error=None,
            )
            log(workspace, f"公告日 {slot.date} 处理完成（无新增相关论文也视为成功）。")
        else:
            state.update(
                status="retry_limit_reached" if state["attempts"] >= MAX_ATTEMPTS else "retry_wait",
                next_retry_at=(finished + RETRY_DELAY).isoformat(),
                last_error=error or f"论文处理失败，退出码 {returncode}；请查看 pipeline 日志",
            )
            log(workspace, f"公告日 {slot.date} 第 {state['attempts']} 次失败：{state['last_error']}。"
                + ("已达到自动重试上限。" if state["attempts"] >= MAX_ATTEMPTS else "至少 10 分钟后重试。"))
        write_state(workspace, state)
        return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true")
    group.add_argument("--run-now", action="store_true")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    try:
        if args.status:
            print(json.dumps(status(workspace), ensure_ascii=False, indent=2))
            return 0
        return run_once(workspace, run_now=args.run_now)
    except (ValueError, OSError) as exc:
        print(f"调度状态错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
