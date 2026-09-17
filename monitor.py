#!/usr/bin/env python3
"""Local Chinese word-count change monitor.

The monitor deliberately has no dependencies outside Python's standard
library.  The repository counter is treated as an external, read-only CLI:
its output is captured in a log and only the summary CSV is parsed.
"""

from __future__ import annotations

import argparse
import csv
import contextlib
import datetime as dt
import html
import json
import locale
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence


VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "monitor.config.json"
LOCAL_CONFIG_PATH = ROOT / "monitor.config.local.json"
COUNTER_MARKER = "old ref 和 new ref 指向同一个 commit"


class MonitorError(RuntimeError):
    """A user-facing configuration or runtime error."""


@dataclass
class FileChange:
    path: str
    path_new: str | None
    changed_sentences: int
    added_chars: int
    added_images: int
    deleted_lines: int
    state: str


@dataclass
class Totals:
    changed_sentences: int = 0
    added_chars: int = 0
    added_images: int = 0
    deleted_lines: int = 0

    @property
    def text_only(self) -> int:
        return self.added_chars - 200 * self.added_images

    def add(self, other: "Totals") -> None:
        self.changed_sentences += other.changed_sentences
        self.added_chars += other.added_chars
        self.added_images += other.added_images
        self.deleted_lines += other.deleted_lines


@dataclass
class CounterOutcome:
    totals: Totals
    whole_totals: Totals
    files: list[FileChange]
    matched_files: int
    csv_path: Path | None = None
    no_change: bool = False
    whole_matched_files: int = 0


@dataclass
class DailyRecord:
    date: str
    repo: str
    repo_url: str
    branch: str
    window_start: str
    window_end: str
    effective_paths: list[str]
    status: str
    reason: str | None = None
    word_delta: int | None = None
    text_only_delta: int | None = None
    image_delta: int | None = None
    sentence_delta: int | None = None
    deleted_lines: int | None = None
    matched_files: int | None = None
    base_commit: str | None = None
    base_commit_time: str | None = None
    base_commit_subject: str | None = None
    tip_commit: str | None = None
    commit_subject: str | None = None
    commit_time: str | None = None
    generated_at: str = ""
    duration_s: float = 0.0
    run_id: str = ""
    slot_end: str | None = None
    whole_word_delta: int | None = None
    whole_matched_files: int | None = None
    files: list[FileChange] = field(default_factory=list)


DEFAULT_SCHEDULE = {
    "time": "19:00",
    "window_hours": 24,
    "window_mode": "slot",
    "clamp_tip_to_window_end": True,
    "missed_run_grace_minutes": 180,
    "catch_up_on_start": True,
    "catch_up_max_slots": 1,
}
DEFAULT_RUNTIME = {
    "concurrency": 4,
    "fetch_timeout_s": 300,
    "compare_timeout_s": 900,
    "keep_raw_days": 30,
}
DEFAULT_REPORT = {
    "default_days": 7,
    "title": "Ascend 仓库中文字数变化监控",
    "top_n_changed": 10,
}
DEFAULT_PUBLISH = {
    "enabled": False,
    "method": "scp",
    "target": "",
    "remote": "origin",
    "branch": "main",
    "files": ["latest", "dated"],
    "commit_message": "report: {date} ({generated_at})",
    "timeout_s": 180,
    "on_error": "warn",
    "extra_args": [],
    "retry_delays_s": [60, 180],
}
MAX_PUBLISH_RETRIES = 5
TIP_CLAMP_GRACE_MINUTES = 5


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_time(value: dt.datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def display_time(value: dt.datetime) -> str:
    return iso_time(value).replace("T", " ")


def latest_due_slot(now: dt.datetime, schedule_time: str | dict[str, Any] = "19:00") -> dt.datetime:
    """Return the most recent daily schedule slot at or before *now*."""
    if isinstance(schedule_time, dict):
        schedule_time = str(schedule_time.get("time", "19:00"))
    hour, minute = _parse_schedule_time(str(schedule_time))
    local_now = now.astimezone() if now.tzinfo is not None else now.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    slot = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot > local_now:
        slot -= dt.timedelta(days=1)
    return slot


def parse_slot(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitorError(f"slot 必须是 ISO8601 时间：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed.astimezone().replace(second=0, microsecond=0)


def ensure_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().replace("\\", "/").strip("/") for item in value if str(item).strip()]


def _normalize_publish_settings(value: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one normalized publisher, inheriting from *base* when given."""
    settings = dict(DEFAULT_PUBLISH)
    settings["files"] = list(DEFAULT_PUBLISH["files"])
    settings["extra_args"] = list(DEFAULT_PUBLISH["extra_args"])
    settings["retry_delays_s"] = list(DEFAULT_PUBLISH["retry_delays_s"])
    if base is not None:
        settings.update(base)
    if isinstance(value, dict):
        settings.update(value)
    settings["enabled"] = bool(settings.get("enabled", False))
    settings["method"] = str(settings.get("method", "scp") or "scp").strip().lower()
    target = settings.get("target", "")
    settings["target"] = "" if target is None else str(target).strip()
    settings["remote"] = str(settings.get("remote", "origin") or "origin").strip()
    settings["branch"] = str(settings.get("branch", "main") or "main").strip()
    settings["commit_message"] = str(settings.get("commit_message", DEFAULT_PUBLISH["commit_message"]))
    files = settings.get("files")
    settings["files"] = (
        [str(item).strip().lower() for item in files if str(item).strip()]
        if isinstance(files, list)
        else list(DEFAULT_PUBLISH["files"])
    )
    extra_args = settings.get("extra_args")
    settings["extra_args"] = [str(item) for item in extra_args] if isinstance(extra_args, list) else []
    retries = settings.get("retry_delays_s")
    if isinstance(retries, list):
        delays: list[float] = []
        for item in retries:
            try:
                delay = float(item)
            except (TypeError, ValueError):
                continue
            if delay > 0:
                delays.append(delay)
        settings["retry_delays_s"] = delays[:MAX_PUBLISH_RETRIES]
    else:
        settings["retry_delays_s"] = list(DEFAULT_PUBLISH["retry_delays_s"])
    on_error = str(settings.get("on_error", "warn") or "warn").strip().lower()
    settings["on_error"] = on_error if on_error in {"warn", "fail"} else "warn"
    return settings


def _publish_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized top-level publish section with safe defaults."""
    return _normalize_publish_settings(config.get("publish"))


def _publishers(config: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Return normalized publishers and whether array syntax was used."""
    publish = config.get("publish")
    top = _publish_settings(config)
    if isinstance(publish, dict) and isinstance(publish.get("publishers"), list):
        return [
            _normalize_publish_settings(item, top) if isinstance(item, dict) else _normalize_publish_settings({}, top)
            for item in publish["publishers"]
        ], True
    return [top], False


def normalize_path(value: str) -> str:
    value = str(value).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def effective_paths(config: dict[str, Any], repo_config: dict[str, Any]) -> list[str]:
    """Return the configured path scope, preserving configuration order."""
    common = [normalize_path(p) for p in ensure_list(config.get("common_paths_filter"))]
    local = [normalize_path(p) for p in ensure_list(repo_config.get("paths_filter"))]
    mode = str(repo_config.get("paths_mode", "union")).lower()
    paths = local if mode == "replace" else common + local
    result: list[str] = []
    for path in paths:
        if path and path not in result:
            result.append(path)
    return result


def _default_repos_root() -> Path:
    configured = os.environ.get("REPO_MONITOR_REPOS_ROOT")
    if configured:
        return Path(configured).expanduser()
    # Discover the conventional sibling directory without embedding a drive
    # letter.  This also leaves a useful empty config on another machine.
    for parent in (ROOT.parent.parent, ROOT.parent, Path.cwd()):
        for candidate in sorted(parent.glob("*/Ascend")) if parent.exists() else []:
            if candidate.is_dir():
                return candidate
    return ROOT.parent / "Ascend"


def default_config(repos_root: Path | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "repos_root": str((repos_root or _default_repos_root()).resolve()),
        "counter_script": "tools/zh-refresh-wordcount/count_zh_refresh.py",
        "python_exe": sys.executable,
        "schedule": dict(DEFAULT_SCHEDULE),
        "runtime": dict(DEFAULT_RUNTIME),
        "report": dict(DEFAULT_REPORT),
        "common_paths_filter": ["docs/zh"],
        "branch_default": "auto",
        "repos": [],
        "publish": {
            **DEFAULT_PUBLISH,
            "files": list(DEFAULT_PUBLISH["files"]),
            "extra_args": list(DEFAULT_PUBLISH["extra_args"]),
        },
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"配置文件无法读取：{path}（{exc}）") from exc
    if not isinstance(value, dict):
        raise MonitorError(f"配置文件顶层必须是 JSON 对象：{path}")
    return value


def load_config(path: Path | str = CONFIG_PATH, local_path: Path | str | None = None) -> dict[str, Any]:
    """Load base, shared-local, then platform-local configuration."""
    config_path = Path(path)
    config = _read_json(config_path)
    if not config:
        config = default_config()
    publish = dict(config.get("publish", {}) if isinstance(config.get("publish"), dict) else {})
    shared_path = Path(local_path) if local_path is not None else config_path.with_name("monitor.config.local.json")
    shared_overlay = _read_json(shared_path) if shared_path.exists() else {}
    config.update(shared_overlay)
    if isinstance(shared_overlay.get("publish"), dict):
        publish.update(shared_overlay["publish"])
    platform_name = "windows" if os.name == "nt" else "linux"
    platform_path = config_path.with_name(f"monitor.config.local.{platform_name}.json")
    platform_overlay = _read_json(platform_path) if platform_path.exists() else {}
    config.update(platform_overlay)
    if isinstance(platform_overlay.get("publish"), dict):
        publish.update(platform_overlay["publish"])
    schedule = dict(DEFAULT_SCHEDULE)
    schedule.update(config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {})
    runtime = dict(DEFAULT_RUNTIME)
    runtime.update(config.get("runtime", {}) if isinstance(config.get("runtime"), dict) else {})
    report = dict(DEFAULT_REPORT)
    report.update(config.get("report", {}) if isinstance(config.get("report"), dict) else {})
    config["schedule"] = schedule
    config["runtime"] = runtime
    config["report"] = report
    config["publish"] = _normalize_publish_settings(publish)
    config.setdefault("common_paths_filter", [])
    config.setdefault("repos", [])
    return config


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _decode_output(data: bytes | None) -> str:
    """Decode subprocess bytes without allowing locale errors to escape."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            try:
                return data.decode(encoding, errors="replace")
            except (LookupError, UnicodeError):
                return data.decode("utf-8", errors="replace")


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=False,
        )
        # Keep the existing str-returning contract regardless of the caller's
        # legacy ``text`` argument.
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            _decode_output(result.stdout),
            _decode_output(result.stderr),
        )
    except FileNotFoundError as exc:
        raise MonitorError(f"找不到可执行文件：{args[0]}") from exc


def git(repo_path: Path, args: Sequence[str], *, timeout: float | None = 60) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo_path), "-c", "core.quotepath=false", *args], timeout=timeout)


def command_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    detail = re.sub(r"\s+", " ", detail)
    return detail[:500] or f"命令退出码 {result.returncode}"


def repo_remote_url(repo_path: Path) -> str:
    result = git(repo_path, ["remote", "get-url", "origin"])
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def resolve_remote_branch(repo_path: Path, url: str = "", timeout: float = 60) -> str | None:
    result = git(repo_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=timeout)
    if result.returncode == 0 and (result.stdout or "").strip():
        value = (result.stdout or "").strip()
        return value["origin/".__len__() :] if value.startswith("origin/") else value
    if not url:
        url = repo_remote_url(repo_path)
    if not url:
        return None
    result = _run(["git", "ls-remote", "--symref", url, "HEAD"], timeout=timeout)
    for line in (result.stdout or "").splitlines():
        match = re.match(r"ref:\s+refs/heads/([^\s]+)\s+HEAD$", line.strip())
        if match:
            return match.group(1)
    return None


def _branch_ref(repo_path: Path, branch: str) -> str:
    for candidate in (f"origin/{branch}", branch):
        result = git(repo_path, ["rev-parse", "--verify", candidate], timeout=30)
        if result.returncode == 0:
            return candidate
    return branch


def _repo_has_git(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _commit_info(repo_path: Path, ref: str) -> tuple[str | None, str | None]:
    result = git(repo_path, ["log", "-1", "--format=%H|%cI|%s", ref], timeout=60)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None, None
    parts = (result.stdout or "").strip().split("|", 2)
    if len(parts) != 3:
        return None, None
    return parts[1], parts[2]


def _tip(repo_path: Path, branch: str, *, before: dt.datetime | None = None) -> str | None:
    ref = _branch_ref(repo_path, branch)
    args = ["log", "-1", "--format=%H"]
    if before is None:
        args = ["rev-parse"]
    else:
        args.append(f"--before={iso_time(before)}")
    args.append(ref)
    result = git(repo_path, args, timeout=60)
    if result.returncode != 0:
        raise MonitorError(f"无法解析 origin/{branch}：{command_error(result)}")
    value = (result.stdout or "").strip()
    return value or None


def _rolling_base(repo_path: Path, ref: str, cutoff: dt.datetime) -> str | None:
    result = git(repo_path, ["log", "-1", "--format=%H", f"--before={iso_time(cutoff)}", ref], timeout=60)
    if result.returncode != 0:
        raise MonitorError(f"无法计算窗口基线：{command_error(result)}")
    value = (result.stdout or "").strip()
    return value or None


def _int_value(value: str | None) -> int:
    if value is None or not str(value).strip():
        return 0
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _parse_file_row(row: Sequence[str]) -> FileChange:
    display = str(row[0]).strip()
    path = display
    path_new: str | None = None
    if " -> " in display:
        path, path_new = (part.strip() for part in display.split(" -> ", 1))
    return FileChange(
        path=normalize_path(path),
        path_new=normalize_path(path_new) if path_new else None,
        changed_sentences=_int_value(row[1] if len(row) > 1 else ""),
        added_chars=_int_value(row[2] if len(row) > 2 else ""),
        added_images=_int_value(row[3] if len(row) > 3 else ""),
        deleted_lines=_int_value(row[4] if len(row) > 4 else ""),
        state=str(row[5]).strip() if len(row) > 5 else "",
    )


def _totals_from_files(files: Iterable[FileChange]) -> Totals:
    totals = Totals()
    for file_change in files:
        totals.add(
            Totals(
                file_change.changed_sentences,
                file_change.added_chars,
                file_change.added_images,
                file_change.deleted_lines,
            )
        )
    return totals


def _path_matches(path: str, configured: str) -> bool:
    path = normalize_path(path)
    configured = normalize_path(configured)
    return path == configured or path.startswith(configured + "/")


def filter_file_changes(files: Sequence[FileChange], paths: Sequence[str]) -> list[FileChange]:
    normalized = [normalize_path(path) for path in paths if normalize_path(path)]
    if not normalized:
        return list(files)
    selected: list[FileChange] = []
    for file_change in files:
        if _path_matches(file_change.path, normalized[0]) or any(
            _path_matches(file_change.path, path) or (file_change.path_new and _path_matches(file_change.path_new, path))
            for path in normalized
        ):
            selected.append(file_change)
    return selected


def parse_summary_csv(path: Path | str, paths: Sequence[str] = ()) -> CounterOutcome:
    """Parse the counter's position-based summary CSV and recompute a scope."""
    files: list[FileChange] = []
    csv_totals: Totals | None = None
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if not row or not any(str(cell).strip() for cell in row):
                continue
            if row[0].strip() == "TOTAL":
                csv_totals = Totals(
                    _int_value(row[1] if len(row) > 1 else ""),
                    _int_value(row[2] if len(row) > 2 else ""),
                    _int_value(row[3] if len(row) > 3 else ""),
                    _int_value(row[4] if len(row) > 4 else ""),
                )
                continue
            if row[0].strip() and not row[0].startswith("文件"):
                files.append(_parse_file_row(row))
    if csv_totals is None:
        csv_totals = _totals_from_files(files)
    selected = filter_file_changes(files, paths)
    totals = csv_totals if not paths else _totals_from_files(selected)
    return CounterOutcome(totals, csv_totals, selected, len(selected), path, False, len(files))


def should_no_change(base: str | None, tip: str | None, previous_tip: str | None = None) -> bool:
    """Both no-change routes used by collection: same refs and fast skip."""
    return bool(tip and ((base and base == tip) or (previous_tip and previous_tip == tip)))


def _scan_for_marker(path: Path, marker: str) -> bool:
    marker_bytes = marker.encode("utf-8")
    with path.open("rb") as handle:
        carry = b""
        while True:
            chunk = handle.read(65536)
            if not chunk:
                return marker_bytes in carry
            data = carry + chunk
            if marker_bytes in data:
                return True
            carry = data[-len(marker_bytes) :]


def _counter_script(config: dict[str, Any]) -> Path:
    value = str(config.get("counter_script", "tools/zh-refresh-wordcount/count_zh_refresh.py"))
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _python_executable(config: dict[str, Any]) -> str:
    value = str(config.get("python_exe", "") or "").strip()
    return value or sys.executable


def _log_path(date: str, repo: str) -> Path:
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo)
    return logs / f"collect-{date}-{safe_repo}.log"


def run_counter(
    config: dict[str, Any],
    repo_path: Path,
    repo_name: str,
    base: str,
    tip: str,
    paths: Sequence[str],
    date: str,
) -> CounterOutcome:
    """Run the counter with a repository-local scratch CWD and always remove it."""
    scratch = Path(tempfile.mkdtemp(prefix=f".zh_monitor_scratch_{os.getpid()}_{repo_name}_", dir=str(repo_path)))
    log_path = _log_path(date, repo_name)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    args = [
        _python_executable(config),
        str(_counter_script(config)),
        "--base-compare-ref",
        base,
        "--target-compare-ref",
        tip,
    ]
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log_handle:
            try:
                result = subprocess.run(
                    args,
                    cwd=str(scratch),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    timeout=float(config.get("runtime", {}).get("compare_timeout_s", 900)),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise MonitorError("compare 超时") from exc
        if result.returncode != 0:
            if _scan_for_marker(log_path, COUNTER_MARKER):
                return CounterOutcome(Totals(), Totals(), [], 0, no_change=True)
            raise MonitorError(f"统计工具失败：{_read_error_tail(log_path)}")
        csv_paths = sorted(scratch.glob("*_compare_*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not csv_paths:
            raise MonitorError("统计工具未生成摘要 CSV")
        outcome = parse_summary_csv(csv_paths[0], paths)
        raw_dir = ROOT / "data" / "raw" / date
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(csv_paths[0], raw_dir / csv_paths[0].name)
        return outcome
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _read_error_tail(path: Path, limit: int = 600) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read().decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", data).strip()[-limit:] or "未知错误"


def _record_base(
    repo_config: dict[str, Any],
    date: str,
    now: dt.datetime,
    window_start: dt.datetime,
    run_id: str,
    *,
    window_end: dt.datetime | None = None,
    slot_end: dt.datetime | None = None,
    branch: str = "",
    paths: Sequence[str] = (),
) -> DailyRecord:
    return DailyRecord(
        date=date,
        repo=str(repo_config.get("name", "")),
        repo_url=str(repo_config.get("url", "")),
        branch=branch,
        window_start=iso_time(window_start),
        window_end=iso_time(window_end or now),
        effective_paths=list(paths),
        status="error",
        generated_at=iso_time(now),
        run_id=run_id,
        slot_end=iso_time(slot_end) if slot_end else None,
    )


def _finish_no_change(record: DailyRecord, tip: str | None, commit_time: str | None, subject: str | None) -> DailyRecord:
    record.status = "no_change"
    record.base_commit = tip
    record.tip_commit = tip
    record.base_commit_time = commit_time
    record.base_commit_subject = subject
    record.commit_time = commit_time
    record.commit_subject = subject
    record.word_delta = 0
    record.text_only_delta = 0
    record.image_delta = 0
    record.sentence_delta = 0
    record.deleted_lines = 0
    record.matched_files = 0
    record.whole_word_delta = 0
    return record


def _preserve_same_slot(
    record: DailyRecord,
    previous: DailyRecord,
    started: float,
    *,
    base_commit_time: str | None = None,
    base_commit_subject: str | None = None,
) -> DailyRecord:
    """Reuse the already computed row when a slot is collected again.

    A slot is the idempotency boundary.  In particular, an ``ok`` row must
    never be turned into a zero-valued ``no_change`` row merely because a
    later invocation used the same commit tip.
    """
    preserved = replace(
        previous,
        generated_at=record.generated_at,
        duration_s=round(time.monotonic() - started, 3),
        run_id=record.run_id,
        base_commit_time=(
            previous.base_commit_time if base_commit_time is None else base_commit_time
        ),
        base_commit_subject=(
            previous.base_commit_subject if base_commit_subject is None else base_commit_subject
        ),
    )
    preserved.files = list(previous.files)
    return preserved


def collect_repository(
    config: dict[str, Any],
    repo_config: dict[str, Any],
    *,
    date: str,
    now: dt.datetime,
    run_id: str,
    previous_tip: str | None = None,
    previous_ok_tip: str | None = None,
    previous_record: DailyRecord | None = None,
    slot_end: dt.datetime | None = None,
) -> DailyRecord:
    started = time.monotonic()
    name = str(repo_config.get("name", ""))
    paths = effective_paths(config, repo_config)
    schedule = config.get("schedule", {})
    mode = str(schedule.get("window_mode", "slot")).lower()
    effective_slot = slot_end
    if effective_slot is None and mode != "rolling":
        effective_slot = latest_due_slot(now, schedule)
    window_end = now if effective_slot is None else effective_slot
    window_hours = float(schedule.get("window_hours", 24))
    window_start = window_end - dt.timedelta(hours=window_hours)
    record = _record_base(
        repo_config,
        date,
        now,
        window_start,
        run_id,
        window_end=window_end,
        slot_end=effective_slot,
        paths=paths,
    )
    record.duration_s = 0.0
    repo_root = Path(str(config.get("repos_root", ""))).expanduser()
    if not repo_root.is_absolute():
        repo_root = ROOT / repo_root
    repo_path = repo_root / name
    try:
        if not _repo_has_git(repo_path):
            raise MonitorError("仓库目录不存在")
        configured_branch = str(repo_config.get("branch") or config.get("branch_default", "auto"))
        branch = configured_branch
        if configured_branch.lower() == "auto":
            branch = resolve_remote_branch(repo_path, str(repo_config.get("url", ""))) or ""
            if not branch:
                raise MonitorError("远端默认分支解析失败")
        record.branch = branch
        fetch_timeout = float(config.get("runtime", {}).get("fetch_timeout_s", 300))
        fetch = git(repo_path, ["fetch", "origin", branch, "--no-tags"], timeout=fetch_timeout)
        if fetch.returncode != 0:
            raise MonitorError(f"fetch 失败：{command_error(fetch)}")
        clamp_tip = (
            effective_slot is not None
            and mode != "rolling"
            and bool(schedule.get("clamp_tip_to_window_end", True))
            and now - window_end > dt.timedelta(minutes=TIP_CLAMP_GRACE_MINUTES)
        )
        tip = _tip(repo_path, branch, before=window_end) if clamp_tip else _tip(repo_path, branch)
        record.tip_commit = tip
        commit_time, subject = _commit_info(repo_path, tip) if tip else (None, None)
        if not tip:
            record.duration_s = round(time.monotonic() - started, 3)
            return _finish_no_change(record, None, None, None)
        current_slot_text = iso_time(effective_slot) if effective_slot else None
        if previous_record is not None:
            if current_slot_text and previous_record.slot_end == current_slot_text:
                base_commit_time = previous_record.base_commit_time
                base_commit_subject = previous_record.base_commit_subject
                if previous_record.base_commit and (base_commit_time is None or base_commit_subject is None):
                    base_commit_time, base_commit_subject = _commit_info(repo_path, previous_record.base_commit)
                return _preserve_same_slot(
                    record,
                    previous_record,
                    started,
                    base_commit_time=base_commit_time,
                    base_commit_subject=base_commit_subject,
                )
            if (
                previous_record.tip_commit == tip
                and current_slot_text
                and previous_record.slot_end
                and previous_record.slot_end < current_slot_text
            ):
                record.duration_s = round(time.monotonic() - started, 3)
                return _finish_no_change(record, tip, commit_time, subject)
            if (
                previous_record.tip_commit == tip
                and current_slot_text is None
                and previous_record.slot_end is None
            ):
                record.duration_s = round(time.monotonic() - started, 3)
                return _finish_no_change(record, tip, commit_time, subject)
        elif previous_tip and previous_tip == tip and current_slot_text is None:
            # Keep the legacy rolling-mode fast path for callers that do not
            # have a prior DailyRecord available.
            record.duration_s = round(time.monotonic() - started, 3)
            return _finish_no_change(record, tip, commit_time, subject)

        ref = _branch_ref(repo_path, branch)
        base: str | None = None
        if mode == "since_last_run" and previous_ok_tip:
            base = previous_ok_tip
        else:
            base = _rolling_base(repo_path, ref, window_start)
        if should_no_change(base, tip):
            record.duration_s = round(time.monotonic() - started, 3)
            return _finish_no_change(record, tip, commit_time, subject)

        if not base:
            record.duration_s = round(time.monotonic() - started, 3)
            return _finish_no_change(record, tip, commit_time, subject)
        base_commit_time, base_commit_subject = _commit_info(repo_path, base)
        record.base_commit_time = base_commit_time
        record.base_commit_subject = base_commit_subject
        outcome = run_counter(config, repo_path, name, base, tip, paths, date)
        if outcome.no_change:
            record.duration_s = round(time.monotonic() - started, 3)
            return _finish_no_change(record, tip, commit_time, subject)
        record.status = "ok"
        record.base_commit = base
        record.tip_commit = tip
        record.commit_time = commit_time
        record.commit_subject = subject
        record.word_delta = outcome.totals.added_chars
        record.text_only_delta = outcome.totals.text_only
        record.image_delta = outcome.totals.added_images
        record.sentence_delta = outcome.totals.changed_sentences
        record.deleted_lines = outcome.totals.deleted_lines
        record.matched_files = outcome.matched_files
        record.whole_word_delta = outcome.whole_totals.added_chars
        record.whole_matched_files = outcome.whole_matched_files
        record.files = outcome.files
        record.duration_s = round(time.monotonic() - started, 3)
        return record
    except (MonitorError, OSError, ValueError) as exc:
        record.reason = str(exc)
        record.duration_s = round(time.monotonic() - started, 3)
        return record


DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  repo TEXT PRIMARY KEY, url TEXT, branch TEXT, paths_filter TEXT, paths_mode TEXT,
  remote TEXT, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  slot_end TEXT,
  window_start TEXT, window_end TEXT, window_hours REAL,
  repo_total INTEGER, repo_ok INTEGER, repo_changed INTEGER, repo_failed INTEGER,
  exit_code INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS repo_daily (
  date TEXT NOT NULL, repo TEXT NOT NULL,
  repo_url TEXT, branch TEXT,
  slot_end TEXT,
  window_start TEXT, window_end TEXT,
  word_delta INTEGER, text_only_delta INTEGER, image_delta INTEGER,
  sentence_delta INTEGER, deleted_lines INTEGER, matched_files INTEGER,
  effective_paths TEXT, base_commit TEXT, base_commit_time TEXT, base_commit_subject TEXT,
  tip_commit TEXT, commit_subject TEXT,
  commit_time TEXT, status TEXT, reason TEXT, generated_at TEXT, duration_s REAL, run_id TEXT,
  whole_word_delta INTEGER,
  whole_matched_files INTEGER,
  PRIMARY KEY (date, repo)
);
CREATE INDEX IF NOT EXISTS idx_repo_daily_repo_date ON repo_daily(repo, date);
CREATE TABLE IF NOT EXISTS repo_daily_files (
  date TEXT NOT NULL, repo TEXT NOT NULL,
  path TEXT NOT NULL, path_new TEXT,
  changed_sentences INTEGER, added_chars INTEGER, added_images INTEGER,
  deleted_lines INTEGER, state TEXT,
  PRIMARY KEY (date, repo, path)
);
"""


def connect_db(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(repo_daily)")}
    for column, definition in (
        ("whole_word_delta", "INTEGER"),
        ("slot_end", "TEXT"),
        ("base_commit_time", "TEXT"),
        ("base_commit_subject", "TEXT"),
        ("whole_matched_files", "INTEGER"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE repo_daily ADD COLUMN {column} {definition}")
            columns.add(column)
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "slot_end" not in run_columns:
        conn.execute("ALTER TABLE runs ADD COLUMN slot_end TEXT")
    conn.commit()
    return conn


def upsert_repo_daily(conn: sqlite3.Connection, record: DailyRecord) -> None:
    conn.execute(
        """
        INSERT INTO repo_daily (
          date, repo, repo_url, branch, slot_end, window_start, window_end,
          word_delta, text_only_delta, image_delta, sentence_delta,
          deleted_lines, matched_files, effective_paths, base_commit, base_commit_time,
          base_commit_subject, tip_commit, commit_subject, commit_time, status, reason,
          generated_at, duration_s, run_id, whole_word_delta, whole_matched_files
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, repo) DO UPDATE SET
          repo_url=excluded.repo_url, branch=excluded.branch, slot_end=excluded.slot_end,
          window_start=excluded.window_start, window_end=excluded.window_end,
          word_delta=excluded.word_delta, text_only_delta=excluded.text_only_delta,
          image_delta=excluded.image_delta, sentence_delta=excluded.sentence_delta,
          deleted_lines=excluded.deleted_lines, matched_files=excluded.matched_files,
          effective_paths=excluded.effective_paths, base_commit=excluded.base_commit,
          base_commit_time=excluded.base_commit_time, base_commit_subject=excluded.base_commit_subject,
          tip_commit=excluded.tip_commit, commit_subject=excluded.commit_subject,
          commit_time=excluded.commit_time, status=excluded.status, reason=excluded.reason,
          generated_at=excluded.generated_at, duration_s=excluded.duration_s,
          run_id=excluded.run_id, whole_word_delta=excluded.whole_word_delta,
          whole_matched_files=excluded.whole_matched_files
        """,
        (
            record.date,
            record.repo,
            record.repo_url,
            record.branch,
            record.slot_end,
            record.window_start,
            record.window_end,
            record.word_delta,
            record.text_only_delta,
            record.image_delta,
            record.sentence_delta,
            record.deleted_lines,
            record.matched_files,
            json.dumps(record.effective_paths, ensure_ascii=False),
            record.base_commit,
            record.base_commit_time,
            record.base_commit_subject,
            record.tip_commit,
            record.commit_subject,
            record.commit_time,
            record.status,
            record.reason,
            record.generated_at,
            record.duration_s,
            record.run_id,
            record.whole_word_delta,
            record.whole_matched_files,
        ),
    )
    conn.execute("DELETE FROM repo_daily_files WHERE date=? AND repo=?", (record.date, record.repo))
    conn.executemany(
        """
        INSERT INTO repo_daily_files
          (date, repo, path, path_new, changed_sentences, added_chars,
           added_images, deleted_lines, state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                record.date,
                record.repo,
                file_change.path,
                file_change.path_new,
                file_change.changed_sentences,
                file_change.added_chars,
                file_change.added_images,
                file_change.deleted_lines,
                file_change.state,
            )
            for file_change in record.files
        ],
    )


def upsert_run(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO runs
          (run_id, started_at, finished_at, slot_end, window_start, window_end, window_hours,
           repo_total, repo_ok, repo_changed, repo_failed, exit_code, note)
        VALUES (:run_id, :started_at, :finished_at, :slot_end, :window_start, :window_end, :window_hours,
                :repo_total, :repo_ok, :repo_changed, :repo_failed, :exit_code, :note)
        ON CONFLICT(run_id) DO UPDATE SET
          finished_at=excluded.finished_at, repo_total=excluded.repo_total,
          repo_ok=excluded.repo_ok, repo_changed=excluded.repo_changed,
          repo_failed=excluded.repo_failed, exit_code=excluded.exit_code, note=excluded.note
        """,
        values,
    )


def sync_repos(conn: sqlite3.Connection, config: dict[str, Any], generated_at: str) -> None:
    rows = []
    for repo_config in config.get("repos", []):
        if not isinstance(repo_config, dict) or not repo_config.get("name"):
            continue
        rows.append(
            (
                str(repo_config["name"]),
                str(repo_config.get("url", "")),
                str(repo_config.get("branch", config.get("branch_default", "auto"))),
                json.dumps(ensure_list(repo_config.get("paths_filter")), ensure_ascii=False),
                str(repo_config.get("paths_mode", "union")),
                "origin",
                1 if repo_config.get("enabled", True) else 0,
                generated_at,
            )
        )
    conn.executemany(
        """
        INSERT INTO repos (repo, url, branch, paths_filter, paths_mode, remote, enabled, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo) DO UPDATE SET url=excluded.url, branch=excluded.branch,
          paths_filter=excluded.paths_filter, paths_mode=excluded.paths_mode,
          remote=excluded.remote, enabled=excluded.enabled, updated_at=excluded.updated_at
        """,
        rows,
    )


def _data_dir() -> Path:
    return ROOT / "data"


def _db_path() -> Path:
    return _data_dir() / "monitor.db"


def _selected_repo_configs(config: dict[str, Any], limit: int | None, only: Sequence[str]) -> list[dict[str, Any]]:
    configured = [
        item for item in config.get("repos", [])
        if isinstance(item, dict) and item.get("name") is not None
    ]
    if only:
        by_name = {str(item["name"]): item for item in configured}
        selected = []
        seen = set()
        for requested in only:
            name = str(requested)
            if name in seen:
                continue
            seen.add(name)
            item = by_name.get(name)
            if item is None:
                print(f"warn: --only 指定的仓库不在配置中：{name}")
                continue
            if item.get("enabled", True):
                selected.append(item)
    else:
        selected = [item for item in configured if item.get("enabled", True)]
    return selected[:limit] if limit is not None else selected


def _daily_record_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> DailyRecord:
    files = [
        FileChange(
            path=str(file_row["path"] or ""),
            path_new=file_row["path_new"],
            changed_sentences=int(file_row["changed_sentences"] or 0),
            added_chars=int(file_row["added_chars"] or 0),
            added_images=int(file_row["added_images"] or 0),
            deleted_lines=int(file_row["deleted_lines"] or 0),
            state=str(file_row["state"] or ""),
        )
        for file_row in _file_rows(conn, str(row["date"]), str(row["repo"]))
    ]
    return DailyRecord(
        date=str(row["date"]),
        repo=str(row["repo"]),
        repo_url=str(row["repo_url"] or ""),
        branch=str(row["branch"] or ""),
        slot_end=row["slot_end"],
        window_start=str(row["window_start"] or ""),
        window_end=str(row["window_end"] or ""),
        effective_paths=_load_paths(row["effective_paths"]),
        status=str(row["status"] or "error"),
        reason=row["reason"],
        word_delta=row["word_delta"],
        text_only_delta=row["text_only_delta"],
        image_delta=row["image_delta"],
        sentence_delta=row["sentence_delta"],
        deleted_lines=row["deleted_lines"],
        matched_files=row["matched_files"],
        base_commit=row["base_commit"],
        base_commit_time=row["base_commit_time"],
        base_commit_subject=row["base_commit_subject"],
        tip_commit=row["tip_commit"],
        commit_subject=row["commit_subject"],
        commit_time=row["commit_time"],
        generated_at=str(row["generated_at"] or ""),
        duration_s=float(row["duration_s"] or 0),
        run_id=str(row["run_id"] or ""),
        whole_word_delta=row["whole_word_delta"],
        whole_matched_files=row["whole_matched_files"],
        files=files,
    )


def _latest_tips(conn: sqlite3.Connection) -> tuple[dict[str, DailyRecord], dict[str, str]]:
    latest: dict[str, DailyRecord] = {}
    successful: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT * FROM repo_daily
        WHERE tip_commit IS NOT NULL
        ORDER BY repo COLLATE NOCASE,
          CASE WHEN slot_end IS NULL THEN 0 ELSE 1 END DESC,
          slot_end DESC, date DESC, generated_at DESC
        """
    )
    for row in rows:
        repo = str(row["repo"])
        if repo not in latest:
            latest[repo] = _daily_record_from_row(conn, row)
        if row["status"] == "ok" and repo not in successful:
            successful[repo] = str(row["tip_commit"])
    return latest, successful


def slot_data_complete(conn: sqlite3.Connection, slot: dt.datetime | str) -> bool:
    """Apply the §7.1 completeness rule for one slot.

    The required row count is taken from the most recent run, intentionally
    making an empty database incomplete even when repo_daily was populated by
    an interrupted/manual operation.
    """
    slot_value = iso_time(slot) if isinstance(slot, dt.datetime) else str(slot)
    latest_run = conn.execute("SELECT repo_total FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if latest_run is None or latest_run[0] is None:
        return False
    count = conn.execute("SELECT COUNT(*) FROM repo_daily WHERE slot_end=?", (slot_value,)).fetchone()[0]
    return count >= int(latest_run[0])


def _historical_slot_ends(conn: sqlite3.Connection) -> list[dt.datetime]:
    slots: list[dt.datetime] = []
    for row in conn.execute("SELECT DISTINCT slot_end FROM runs WHERE slot_end IS NOT NULL"):
        try:
            slots.append(parse_slot(row[0]))  # type: ignore[arg-type]
        except MonitorError:
            continue
    return [slot for slot in slots if slot is not None]


def _missing_catch_up_slots(
    conn: sqlite3.Connection,
    latest_slot: dt.datetime,
    max_slots: int,
) -> tuple[list[dt.datetime], int]:
    if slot_data_complete(conn, latest_slot):
        return [], 0
    historical = _historical_slot_ends(conn)
    previous_known = max((slot for slot in historical if slot < latest_slot), default=None)
    missing: list[dt.datetime] = [latest_slot]
    if previous_known is not None:
        cursor = latest_slot - dt.timedelta(days=1)
        while cursor > previous_known:
            if not slot_data_complete(conn, cursor):
                missing.append(cursor)
            cursor -= dt.timedelta(days=1)
    selected = missing[: max(1, max_slots)]
    return selected, max(0, len(missing) - len(selected))


def _monitor_log_line(message: str, date: str | None = None) -> None:
    print(message)
    log_date = date or now_local().date().isoformat()
    path = ROOT / "logs" / f"monitor-{log_date}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def _publish_error_detail(value: Any, fallback: str) -> str:
    if isinstance(value, bytes):
        detail = _decode_output(value)
    else:
        detail = str(value or "")
    detail = re.sub(r"\s+", " ", detail).strip()
    return detail[-500:] or fallback


def _publish_failure(
    settings: dict[str, Any],
    date: str,
    exit_code: int,
    detail: Any,
) -> tuple[int, bool, str]:
    label = settings.get("_log_label", "publish")
    message = f"{label}: failed (exit {exit_code}): {_publish_error_detail(detail, '未知错误')}"
    _monitor_log_line(message, date)
    return 0, settings["on_error"] == "fail", message


def _publish_timeout(settings: dict[str, Any]) -> float | None:
    try:
        timeout_s = float(settings["timeout_s"])
    except (TypeError, ValueError):
        return None
    return timeout_s if timeout_s > 0 else None


def _publish_transfer(
    settings: dict[str, Any],
    date: str,
    attempt: Callable[[], tuple[bool, int, Any]],
) -> tuple[bool, int, Any]:
    """Run one transfer attempt, retrying per ``retry_delays_s``.

    Retries wrap only the transport step (copy / scp / git push) so a retried
    ``git push`` still pushes the commit the first attempt already created —
    retrying the whole publisher would instead see a clean index and report
    ``unchanged``, hiding the failure.
    """
    delays = list(settings.get("retry_delays_s") or [])
    label = settings.get("_log_label", "publish")
    outcome: tuple[bool, int, Any] = (False, 1, "publish 未执行")
    for attempt_no in range(len(delays) + 1):
        outcome = attempt()
        ok, exit_code, detail = outcome
        if ok or attempt_no >= len(delays):
            return outcome
        delay = float(delays[attempt_no])
        _monitor_log_line(
            f"{label}: 第 {attempt_no + 1} 次尝试失败（exit {exit_code}）："
            f"{_publish_error_detail(detail, '未知错误')}；{delay:g}s 后重试（共 {len(delays) + 1} 次）",
            date,
        )
        time.sleep(delay)
    return outcome


def _copy_bytes(source: Path, destination: Path) -> None:
    """Copy a report without text-mode newline conversion."""
    with source.open("rb") as source_handle, destination.open("wb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle)


def _publish_one(
    settings: dict[str, Any],
    output: Path,
    date: str,
    generated_at: str,
) -> tuple[int, bool, str | None]:
    method = settings["method"]
    target = settings["target"]
    sources = [output.parent / "latest.html"]
    if method != "git" and "dated" in settings["files"]:
        sources.append(output.parent / f"{date}.html")
    missing = next((source for source in sources if not source.is_file()), None)
    if missing is not None:
        return _publish_failure(settings, date, 1, f"报表文件不存在：{missing}")
    if not target:
        return _publish_failure(settings, date, 1, "publish.target 不能为空")
    if method not in {"scp", "copy", "git"}:
        return _publish_failure(settings, date, 1, f"不支持的 publish.method：{method}")

    timeout_s = _publish_timeout(settings)
    if timeout_s is None:
        return _publish_failure(settings, date, 1, "publish.timeout_s 必须是正数")

    if method == "copy":
        destination = Path(target).expanduser()
        if not destination.is_absolute():
            destination = ROOT / destination
        if not destination.is_dir():
            return _publish_failure(settings, date, 1, f"copy 目标目录不存在：{destination}")

        def _copy_once() -> tuple[bool, int, Any]:
            try:
                for source in sources:
                    _copy_bytes(source, destination / source.name)
            except OSError as exc:
                return False, 1, exc
            return True, 0, None

        ok, exit_code, detail = _publish_transfer(settings, date, _copy_once)
        if not ok:
            return _publish_failure(settings, date, exit_code, detail)
    elif method == "scp":
        command = [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            *settings["extra_args"],
            *(str(source) for source in sources),
            target,
        ]

        def _scp_once() -> tuple[bool, int, Any]:
            try:
                result = _run(command, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                return False, 124, f"scp 超时（{timeout_s:g}s）"
            except MonitorError as exc:
                return False, 1, exc
            return result.returncode == 0, result.returncode, (result.stderr or result.stdout)

        ok, exit_code, detail = _publish_transfer(settings, date, _scp_once)
        if not ok:
            return _publish_failure(settings, date, exit_code, detail)
    else:
        destination = Path(target).expanduser()
        if not destination.is_absolute():
            destination = ROOT / destination
        if not destination.is_dir():
            return _publish_failure(settings, date, 1, f"git 目标工作树不存在：{destination}")
        remote = settings["remote"]
        branch = settings["branch"]
        if not remote or not branch:
            return _publish_failure(settings, date, 1, "git 的 remote 和 branch 不能为空")
        try:
            _copy_bytes(sources[0], destination / "latest.html")
            result = git(destination, ["add", "latest.html"], timeout=timeout_s)
            if result.returncode != 0:
                return _publish_failure(settings, date, result.returncode, result.stderr or result.stdout)
            result = git(destination, ["diff", "--cached", "--quiet", "--", "latest.html"], timeout=timeout_s)
            if result.returncode == 0:
                _monitor_log_line(f"{settings['_log_label']}: ok 0 files -> {target} (unchanged)", date)
                return 0, False, None
            if result.returncode != 1:
                return _publish_failure(settings, date, result.returncode, result.stderr or result.stdout)
            try:
                commit_message = settings["commit_message"].format(date=date, generated_at=generated_at)
            except (AttributeError, KeyError, ValueError) as exc:
                return _publish_failure(settings, date, 1, f"commit_message 模板错误：{exc}")
            result = git(
                destination,
                ["commit", "--only", "-m", commit_message, "--", "latest.html"],
                timeout=timeout_s,
            )
            if result.returncode != 0:
                return _publish_failure(settings, date, result.returncode, result.stderr or result.stdout)

            def _push_once() -> tuple[bool, int, Any]:
                try:
                    pushed = git(destination, ["push", remote, branch], timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    return False, 124, f"git push 超时（{timeout_s:g}s）"
                except (MonitorError, OSError) as exc:
                    return False, 1, exc
                return pushed.returncode == 0, pushed.returncode, (pushed.stderr or pushed.stdout)

            ok, exit_code, detail = _publish_transfer(settings, date, _push_once)
            if not ok:
                return _publish_failure(settings, date, exit_code, detail)
        except subprocess.TimeoutExpired:
            return _publish_failure(settings, date, 124, f"git 超时（{timeout_s:g}s）")
        except (MonitorError, OSError) as exc:
            return _publish_failure(settings, date, 1, exc)

    _monitor_log_line(f"{settings['_log_label']}: ok {len(sources)} files -> {target}", date)
    return len(sources), False, None


def publish_report(
    config: dict[str, Any],
    report_path: Path | str,
    *,
    report_date: str | None = None,
    generated_at: str | None = None,
    no_publish: bool = False,
) -> int | None:
    """Publish a completed report to one or more independently configured targets."""
    top = _publish_settings(config)
    if no_publish or not top["enabled"]:
        return None

    output = Path(report_path).resolve()
    date = report_date or (output.stem if re.fullmatch(r"\d{4}-\d{2}-\d{2}", output.stem) else now_local().date().isoformat())
    generated = generated_at or display_time(now_local())
    publishers, array_syntax = _publishers(config)
    total = 0
    any_enabled = False
    fail_message: str | None = None
    for settings in publishers:
        if not settings["enabled"]:
            continue
        any_enabled = True
        settings = dict(settings)
        settings["_log_label"] = f"publish[{settings['method']}]" if array_syntax else "publish"
        try:
            count, should_fail, message = _publish_one(settings, output, date, generated)
        except Exception as exc:  # each publisher must remain isolated
            count, should_fail, message = _publish_failure(settings, date, 1, exc)
        total += count
        if should_fail and fail_message is None:
            fail_message = message
    if fail_message is not None:
        raise MonitorError(fail_message)
    return total if any_enabled else None


def perform_catch_up(
    config: dict[str, Any],
    *,
    limit: int | None = None,
    only: Sequence[str] = (),
    no_publish: bool = False,
) -> list[DailyRecord]:
    """Fill missing recent slots, returning records from the last catch-up."""
    schedule = config.get("schedule", {})
    latest_slot = latest_due_slot(now_local(), schedule)
    conn = connect_db(_db_path())
    try:
        maximum = max(1, int(schedule.get("catch_up_max_slots", 1)))
        slots, skipped_older = _missing_catch_up_slots(conn, latest_slot, maximum)
    finally:
        conn.close()
    slot_text = iso_time(latest_slot)
    if not slots:
        _monitor_log_line(f"槽位 {slot_text} 已有数据，跳过补跑")
        return []
    note = f"补跑 {len(slots)} 个槽位，跳过 {skipped_older} 个更早的槽位"
    last_records: list[DailyRecord] = []
    last_report: Path | None = None
    for slot in slots:
        records, _report = run_collection(
            config,
            limit=limit,
            only=only,
            slot_end=slot,
            note=note,
        )
        last_records = records
        last_report = _report
        slot_value = iso_time(slot)
        ok = sum(record.status in {"ok", "no_change"} for record in records)
        failed = sum(record.status == "error" for record in records)
        _monitor_log_line(f"补跑完成 slot={slot_value} ok={ok} failed={failed}", slot.date().isoformat())
    if skipped_older:
        _monitor_log_line(note, latest_slot.date().isoformat())
    if last_report is not None:
        publish_report(config, last_report, report_date=slots[-1].date().isoformat(), no_publish=no_publish)
    return last_records


def _backfill_slots(config: dict[str, Any], days: int, to_date: str | None) -> list[dt.datetime]:
    if days <= 0:
        raise MonitorError("backfill --days 必须是正整数")
    schedule = config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {}
    if to_date:
        try:
            end_date = dt.date.fromisoformat(to_date)
        except ValueError as exc:
            raise MonitorError(f"日期格式必须是 YYYY-MM-DD：{to_date}") from exc
    else:
        end_date = now_local().date() - dt.timedelta(days=1)
    hour, minute = _parse_schedule_time(str(schedule.get("time", DEFAULT_SCHEDULE["time"])))
    tz = now_local().tzinfo
    return [
        dt.datetime.combine(end_date - dt.timedelta(days=offset), dt.time(hour, minute, tzinfo=tz))
        for offset in range(days - 1, -1, -1)
    ]


def _purge_backfill_data(slots: Sequence[dt.datetime], repo_names: Sequence[str]) -> None:
    dates = list(dict.fromkeys(slot.date().isoformat() for slot in slots))
    names = list(dict.fromkeys(str(name) for name in repo_names if str(name)))
    conn = connect_db(_db_path())
    try:
        for date in dates:
            if names:
                placeholders = ",".join("?" for _ in names)
                params: list[Any] = [date, *names]
                conn.execute(
                    f"DELETE FROM repo_daily_files WHERE date=? AND repo IN ({placeholders})",
                    params,
                )
                conn.execute(
                    f"DELETE FROM repo_daily WHERE date=? AND repo IN ({placeholders})",
                    params,
                )
            else:
                conn.execute("DELETE FROM repo_daily_files WHERE date=?", (date,))
                conn.execute("DELETE FROM repo_daily WHERE date=?", (date,))
        conn.commit()
    finally:
        conn.close()

    raw_root = _data_dir() / "raw"
    for date in dates:
        raw_dir = raw_root / date
        if raw_dir.is_dir():
            shutil.rmtree(raw_dir)
    print(f"purge: dates={','.join(dates)} repos={len(names) if names else 'all'}")


def command_backfill(config: dict[str, Any], args: argparse.Namespace) -> int:
    slots = _backfill_slots(config, args.days, args.to_date)
    print("backfill slots:")
    for slot in slots:
        print(f"  {iso_time(slot)}")
    if args.dry_run:
        return 0

    selected = _selected_repo_configs(config, args.limit, args.only or ())
    if not selected:
        raise MonitorError("没有可采集的 enabled 仓库")
    if args.purge:
        _purge_backfill_data(slots, [str(item["name"]) for item in selected])

    note = f"历史补跑 {len(slots)} 个槽位"
    last_report: Path | None = None
    for slot in slots:
        print(f"backfill slot={iso_time(slot)}")
        records, report_path = run_collection(
            config,
            limit=args.limit,
            only=args.only or (),
            slot_end=slot,
            note=note,
        )
        last_report = report_path
        _print_records(records, report_path)
    if last_report is not None:
        publish_report(
            config,
            last_report,
            report_date=slots[-1].date().isoformat(),
            no_publish=bool(getattr(args, "no_publish", False)),
        )
    return 0


def clean_raw(date: str, keep_days: int) -> None:
    raw_root = _data_dir() / "raw"
    if keep_days < 0 or not raw_root.exists():
        return
    cutoff = dt.date.fromisoformat(date) - dt.timedelta(days=keep_days)
    for directory in raw_root.iterdir():
        if not directory.is_dir():
            continue
        try:
            if dt.date.fromisoformat(directory.name) < cutoff:
                shutil.rmtree(directory)
        except ValueError:
            continue


def run_collection(
    config: dict[str, Any],
    *,
    limit: int | None = None,
    only: Sequence[str] = (),
    date: str | None = None,
    dry_run: bool = False,
    slot_end: dt.datetime | None = None,
    note: str = "",
) -> tuple[list[DailyRecord], Path | None]:
    started_at = now_local()
    schedule = config.get("schedule", {})
    mode = str(schedule.get("window_mode", "slot")).lower()
    effective_slot = slot_end
    if effective_slot is None and mode != "rolling":
        effective_slot = latest_due_slot(started_at, schedule)
    run_date = date or (effective_slot.date().isoformat() if effective_slot else started_at.date().isoformat())
    try:
        dt.date.fromisoformat(run_date)
    except ValueError as exc:
        raise MonitorError(f"日期格式必须是 YYYY-MM-DD：{run_date}") from exc
    selected = _selected_repo_configs(config, limit, only)
    if not selected:
        raise MonitorError("没有可采集的 enabled 仓库")
    run_id = uuid.uuid4().hex
    latest_records: dict[str, DailyRecord] = {}
    successful_tips: dict[str, str] = {}
    if not dry_run:
        conn = connect_db(_db_path())
        try:
            latest_records, successful_tips = _latest_tips(conn)
            generated = iso_time(started_at)
            sync_repos(conn, config, generated)
            conn.commit()
        finally:
            conn.close()

    concurrency = max(1, int(config.get("runtime", {}).get("concurrency", 4)))
    records: list[DailyRecord] = []
    with ThreadPoolExecutor(max_workers=min(concurrency, len(selected))) as executor:
        futures = {
            executor.submit(
                collect_repository,
                config,
                repo_config,
                date=run_date,
                now=started_at,
                run_id=run_id,
                previous_tip=(
                    latest_records[str(repo_config.get("name"))].tip_commit
                    if str(repo_config.get("name")) in latest_records
                    else None
                ),
                previous_ok_tip=successful_tips.get(str(repo_config.get("name"))),
                previous_record=latest_records.get(str(repo_config.get("name"))),
                slot_end=effective_slot,
            ): repo_config
            for repo_config in selected
        }
        for future in as_completed(futures):
            repo_config = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:  # a single repository must never abort the round
                name = str(repo_config.get("name", ""))
                paths = effective_paths(config, repo_config)
                fallback = _record_base(
                    repo_config,
                    run_date,
                    started_at,
                    started_at - dt.timedelta(hours=float(schedule.get("window_hours", 24))),
                    run_id,
                    window_end=effective_slot or started_at,
                    slot_end=effective_slot,
                    paths=paths,
                )
                fallback.reason = f"未捕获异常：{exc}"
                records.append(fallback)
    records.sort(key=lambda record: record.repo.lower())

    if dry_run:
        return records, None

    finished_at = now_local()
    conn = connect_db(_db_path())
    try:
        for record in records:
            upsert_repo_daily(conn, record)
        window_hours = float(schedule.get("window_hours", 24))
        upsert_run(
            conn,
            {
                "run_id": run_id,
                "started_at": iso_time(started_at),
                "finished_at": iso_time(finished_at),
                "slot_end": iso_time(effective_slot) if effective_slot else None,
                "window_start": iso_time((effective_slot or started_at) - dt.timedelta(hours=window_hours)),
                "window_end": iso_time(effective_slot or started_at),
                "window_hours": window_hours,
                "repo_total": len(records),
                "repo_ok": sum(record.status in {"ok", "no_change"} for record in records),
                "repo_changed": sum(record.status == "ok" for record in records),
                "repo_failed": sum(record.status == "error" for record in records),
                "exit_code": 0,
                "note": note,
            },
        )
        conn.commit()
    finally:
        conn.close()
    clean_raw(run_date, int(config.get("runtime", {}).get("keep_raw_days", 30)))
    report_path = generate_report(config, days=int(config.get("report", {}).get("default_days", 7)))
    return records, report_path


def _format_number(value: Any) -> str:
    if value is None:
        return "—"
    return str(int(value))


def _short_sha(value: str | None) -> str:
    return value[:7] if value else "—"


def _short_commit_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _url_label(url: str) -> str:
    return re.sub(r"^https?://", "", url).rstrip("/")


def _status_label(status: str) -> str:
    return {"ok": "有变更", "no_change": "无变更", "error": "失败", "skipped": "未采集"}.get(status, status)


def _load_paths(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    return ensure_list(parsed)


def _sparkline(repo: str, values: Sequence[tuple[str, int]]) -> str:
    width, height = 96, 24
    title = "每日变化量（新增字数） " + " | ".join(f"{day[5:]}: {_format_number(value)}" for day, value in values)
    if not values:
        return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img"><title>{html.escape(title)}</title></svg>'
    numbers = [value for _, value in values]
    low, high = min(numbers), max(numbers)
    points: list[tuple[float, float]] = []
    for index, value in enumerate(numbers):
        x = 3.0 if len(numbers) == 1 else 3 + index * (90 / (len(numbers) - 1))
        y = 3.0 if high == low else 21 - ((value - low) / (high - low)) * 18
        points.append((x, y))
    points_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    circles = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#1f6feb"/>' for x, y in points)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'
        f"<title>{html.escape(title)}</title>"
        f'<polyline fill="none" stroke="#1f6feb" stroke-width="1.6" points="{points_text}"/>{circles}</svg>'
    )



REPORT_CSS = r"""
:root{
  color-scheme:light;
  --bg:#f1f5f9; --surface:#fff; --surface-soft:#f7f9fb; --surface-subtle:#e8eef5;
  --line:#d6dee7; --line-soft:#e7edf3; --text:#182230; --text-soft:#334155; --muted:#536273;
  --accent:#0b63ce; --accent-soft:#e7f0fb; --focus:#0b63ce;
  --ok:#176b3a; --error:#a63832; --big:#b45309; --none:#6b7280;
  --note-bg:#fffbeb; --note-border:#e6d8ae; --note-text:#5c4b1f;
  --rail:264px; --entry-min:638px;
  --data-font:ui-monospace, "Cascadia Mono", Consolas, "Liberation Mono", monospace;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif}
.app{display:grid;grid-template-columns:var(--rail) minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;align-self:start;height:100vh;overflow:auto;
  background:var(--surface);border-right:1px solid var(--line);padding:16px 14px 26px}
.side h1{font-size:14.5px;margin:0 0 3px}
.side .sub{color:var(--muted);font-size:11px;margin-bottom:14px}
.lbl{font-size:10px;letter-spacing:.12em;color:var(--muted);margin:0 0 8px;padding-top:12px;border-top:1px solid var(--line)}
.lbl.first{border-top:0;padding-top:0}
.ctrl{display:flex;flex-wrap:wrap;gap:6px}
select,button{font:inherit;font-size:12px;padding:4px 8px;border:1px solid var(--line);border-radius:5px;background:#fff;color:var(--text);cursor:pointer}
button:hover{background:var(--surface-soft);border-color:#cdd3da}
button:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.stack{display:flex;height:9px;border-radius:5px;overflow:hidden;margin-bottom:9px;border:1px solid var(--line)}
.stack i{display:block}
.legend{list-style:none;margin:0;padding:0}
.legend li{display:grid;grid-template-columns:1fr auto;gap:8px;font-size:12px;padding:2.5px 0}
.legend .sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:7px;vertical-align:-1px;border:1px solid rgba(15,23,42,.14)}
.legend .cnt{font:12px var(--data-font);color:var(--muted)}
.legend button{border:0;background:none;padding:0;text-align:left;color:inherit;font-size:12px}
.legend button:hover{background:none;text-decoration:underline}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:9px 10px}
.side-meta{display:none}
.side-meta.active{display:block}
.kpi .v{font:600 17px/1.15 var(--data-font);font-variant-numeric:tabular-nums}
.kpi .k{font-size:10.5px;color:var(--muted)}
.side-day{display:none}
.side-day.active{display:block}
.rank{margin:7px 0 0;padding-left:18px}
.rank li{padding:2px 0;font-size:11.5px}
.rank .rank-name{display:inline-block;max-width:140px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom;white-space:nowrap}
.rank .rank-value{float:right;font:11.5px var(--data-font);font-variant-numeric:tabular-nums;color:var(--muted)}
.main{padding:16px 20px 48px;min-width:0;overflow:hidden}
.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:14px}
.top .t{font-size:15px;font-weight:600}
.top .m{color:var(--muted);font-size:11.5px}
section.day{display:none}
section.day.active{display:block}
.grp{margin-bottom:18px}
.grphead{display:flex;align-items:center;gap:10px;padding:7px 10px;background:var(--surface-subtle);
  border:1px solid var(--line);border-radius:7px;margin-bottom:6px}
.grphead .gsw{width:11px;height:11px;border-radius:3px;align-self:center;border:1px solid rgba(15,23,42,.14)}
.grphead .gn{font-size:12px;letter-spacing:.1em;font-weight:600}
.grphead .gc{font:11.5px var(--data-font);color:var(--muted)}
.grphead .gw{margin-left:auto;font:11.5px var(--data-font);color:var(--muted)}
.grpbody{min-width:0;overflow-x:auto;overscroll-behavior-x:contain}
.row{display:grid;align-items:center;gap:10px;grid-template-columns:minmax(150px,1fr) 106px 122px 138px 66px;padding:6px 8px;min-width:var(--entry-min);min-height:34px}
.entry{background:var(--surface);border:1px solid var(--line);border-radius:7px;margin-bottom:6px;overflow:hidden;min-width:var(--entry-min)}
.entry:hover{border-color:#cdd3da}
.entry .row{border-bottom:0}
.entry.flash{border-color:var(--accent);background:#f5f9ff}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
a.name{color:inherit;text-decoration:underline;text-decoration-color:#c3cfdb;text-underline-offset:2px;text-decoration-thickness:1px}
a.name:hover{color:var(--accent);text-decoration-color:var(--accent)}
a.name:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.br{font:10px var(--data-font);color:var(--muted);margin-left:6px}
.num{text-align:right;font:12px var(--data-font);font-variant-numeric:tabular-nums;white-space:nowrap}
.num.big{font-weight:600}
.cells{display:flex;gap:2px}
.cells i{width:11px;height:14px;border-radius:2px;background:#eef1f4;border:1px solid #dfe4ea}
.cells i.h1{background:#bfdbfe;border-color:#a5c9fa}
.cells i.h2{background:#60a5fa;border-color:#3b82f6}
.cells i.h3{background:#2563eb;border-color:#1d4ed8}
.cells i.h4{background:#f97316;border-color:#ea580c}
.state{font-size:11.5px;display:flex;align-items:center;gap:5px;color:var(--muted);white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;flex:0 0 auto}
.dot.ok{background:var(--ok)} .dot.none{background:var(--none)} .dot.err{background:var(--error)}
.tgl{border:0;background:none;color:var(--accent);padding:2px 4px;font-size:12px}
.tgl:hover{background:#eaf2fe}
.detail{padding:10px 12px 12px;background:var(--surface-soft);border-top:1px solid var(--line)}
.detail[hidden]{display:none}
.dkv{font-size:11.5px;color:var(--muted);margin-bottom:5px}
.dkv b{color:var(--text);font-family:var(--data-font);font-weight:600}
.commit-subject{display:inline-block;max-width:46ch;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom;white-space:nowrap}
table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:12.5px;margin-top:6px;border:1px solid var(--line);border-radius:6px}
table th:nth-child(1),table td:nth-child(1){width:auto}
table th:nth-child(2),table td:nth-child(2){width:96px;white-space:nowrap}
table th:nth-child(n+3),table td:nth-child(n+3){width:66px}
th{text-align:left;font-size:11.5px;letter-spacing:.05em;color:var(--text-soft);font-weight:600;padding:7px 10px;background:var(--surface-subtle);border-bottom:1px solid var(--line)}
td{padding:5px 10px;border-bottom:1px solid var(--line-soft)}
td.n,th.n{text-align:right;font-family:var(--data-font);font-variant-numeric:tabular-nums;white-space:nowrap}
td.p{font-family:var(--data-font);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:var(--muted);font-size:11.5px;padding:2px 0 4px}
.legendbar{margin-top:16px;padding:9px 12px;background:var(--note-bg);border:1px solid var(--note-border);border-radius:7px;font-size:11.5px;color:var(--note-text)}
details.meta-block{margin-top:16px;padding:9px 12px;background:var(--surface);border:1px solid var(--line);border-radius:7px;min-width:0}
details.meta-block summary{cursor:pointer;color:var(--muted);font-size:11.5px}
details.meta-block summary:hover{color:var(--accent)}
details.meta-block summary:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
details.meta-block[open]{border-color:#a9c1dd}
pre.config-json{margin:8px 0 0;max-width:100%;max-height:360px;overflow:auto;padding:10px;background:var(--surface-soft);border:1px solid var(--line-soft);border-radius:5px;color:var(--text-soft);font:11.5px/1.5 var(--data-font);white-space:pre}
.spark-baseline{display:none}
@media (max-width:760px){
  .app{grid-template-columns:220px minmax(0,1fr);--entry-min:514px}
  .side{padding-inline:10px}
  .main{padding-inline:12px}
  .row{grid-template-columns:minmax(130px,1fr) 88px 92px 116px 52px;gap:6px;padding-inline:6px}
  .cells{gap:1px}.cells i{width:8px}
}
@media print{.side{display:none}.app{grid-template-columns:1fr}body{background:#fff}.detail[hidden]{display:block}}
@media (prefers-reduced-motion: reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

REPORT_JS = r"""
function $$(sel, root){ return [...(root||document).querySelectorAll(sel)]; }
function activeScope(){ return document.querySelector('section.day.active') || document; }
function groups(root){ return $$('[data-group]', root); }
function setOpen(btn, open){
  var group=btn.closest('[data-group]');
  var t=group ? group.querySelector('.detail') : document.getElementById(btn.dataset.toggle);
  if(!t) return;
  t.hidden=!open;
  btn.textContent=open?'收起':'展开';
  btn.setAttribute('aria-expanded',open?'true':'false');
}
function setAll(open){
  groups(activeScope()).forEach(function(g){ $$('[data-toggle]',g).forEach(function(b){setOpen(b,open);}); });
}
function sortGroups(mode){
  $$('[data-group-container]').forEach(function(box){
    $$('[data-group]',box).sort(function(a,b){
      if(mode==='name') return a.dataset.name.toLowerCase()<b.dataset.name.toLowerCase()?-1:1;
      return (+b.dataset.word)-(+a.dataset.word);
    }).forEach(function(g){box.appendChild(g);});
  });
}
function showDay(v){
  var found=false;
  $$('section.day').forEach(function(s){var on=s.id==='day-'+v;s.classList.toggle('active',on);if(on)found=true;});
  $$('[data-side-meta]').forEach(function(s){s.classList.toggle('active',s.dataset.sideMeta===v);});
  $$('[data-side-day]').forEach(function(s){s.classList.toggle('active',s.dataset.sideDay===v);});
  var sort=document.getElementById('sortSel');
  if(sort) sortGroups(sort.value);
  setAll(false);
  if(!found) console.warn('没有该日期的报告: '+v);
}
function gotoGroup(name){
  var scope=activeScope();
  var el=$$('[data-grp]',scope).find(function(group){return group.dataset.grp===name;});
  if(!el && scope!==document) el=$$('[data-grp]',document).find(function(group){return group.dataset.grp===name;});
  if(!el) return;
  el.scrollIntoView({block:'start',behavior:'smooth'});
  el.classList.add('flash');
  setTimeout(function(){el.classList.remove('flash');},1400);
}
function bindUI(){
  $$('[data-toggle]').forEach(function(b){b.addEventListener('click',function(){setOpen(b,b.getAttribute('aria-expanded')!=='true');});});
  var day=document.getElementById('daySel');
  if(day) day.addEventListener('change',function(){showDay(day.value);});
  var sort=document.getElementById('sortSel');
  if(sort) sort.addEventListener('change',function(){sortGroups(sort.value);});
  $$('[data-jump-group]').forEach(function(b){b.addEventListener('click',function(){gotoGroup(b.dataset.jumpGroup);});});
  var on=document.querySelector('[data-all="open"]'),off=document.querySelector('[data-all="close"]');
  if(on) on.addEventListener('click',function(){setAll(true);});
  if(off) off.addEventListener('click',function(){setAll(false);});
}
document.addEventListener('DOMContentLoaded',bindUI);
"""


def _day_rows(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM repo_daily WHERE date=? ORDER BY repo COLLATE NOCASE", (day,)))


def _recent_days(conn: sqlite3.Connection, anchor: str | None, days: int) -> list[str]:
    all_dates = [row[0] for row in conn.execute("SELECT DISTINCT date FROM repo_daily ORDER BY date DESC")]
    if anchor and anchor in all_dates:
        all_dates = [day for day in all_dates if day <= anchor]
    return all_dates[: max(1, days)]


def _file_rows(conn: sqlite3.Connection, day: str, repo: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM repo_daily_files WHERE date=? AND repo=? ORDER BY path COLLATE NOCASE ASC, COALESCE(path_new, '') COLLATE NOCASE ASC",
            (day, repo),
        )
    )


def _render_commit(commit: str | None, commit_time: str | None, subject: str | None) -> str:
    if not commit:
        return "<span class=\"mono\">—</span>"
    rendered = [f'<span class="mono">{html.escape(_short_sha(commit))}</span>']
    if commit_time:
        rendered.append(f'<span class="sub-inline">{html.escape(_short_commit_time(commit_time))}</span>')
    if subject:
        subject_text = html.escape(subject, quote=True)
        rendered.append(f'<div class="sub" title="{subject_text}">{subject_text}</div>')
    return " ".join(rendered)


def _empty_detail_message(row: sqlite3.Row) -> str:
    status = str(row["status"] or "error")
    if status == "no_change":
        return "该分支在窗口起点之后没有新提交（基线 = 最新提交）。"
    if status == "error":
        return html.escape(row["reason"] or "采集失败，未生成文件明细。")
    whole_word = int(row["whole_word_delta"] or 0)
    matched_files = int(row["matched_files"] or 0)
    if status == "ok" and matched_files == 0 and whole_word > 0:
        paths = json.dumps(_load_paths(row["effective_paths"]), ensure_ascii=False)
        whole_files = row["whole_matched_files"]
        whole_suffix = f" / {int(whole_files)} 个文件" if whole_files is not None else ""
        return (
            f"窗口内有新提交，但生效路径 {html.escape(paths)} 下没有文件级中文变更"
            f"（全仓口径 {_format_number(whole_word)} 字{whole_suffix}）。"
            '若该仓库文档不在此路径下，请在 monitor.config.json 里为它设置 paths_filter，或改成 paths_mode: "replace"。'
        )
    if status == "ok" and whole_word == 0:
        return "窗口内的提交只改动了非文档内容，生效路径下与全仓都没有中文字数变化。"
    return "窗口内有新提交，但文件级明细未保存。"


REPORT_GROUPS = (
    ("大量", 10000, "#f97316", "#b45309", "≥ 10,000 字"),
    ("中等", 1000, "#22c55e", "#15803d", "1,000 – 9,999 字"),
    ("少量", 1, "#3b82f6", "#2563eb", "1 – 999 字"),
    ("无变化", 0, "#94a3b8", "#5f6b76", "窗口内 0 字"),
    ("失败", -1, "#ef4444", "#b42318", "采集失败，请手动查看"),
)
REPORT_STATUS = {
    "ok": ("有变更", "ok"),
    "no_change": ("无变化", "none"),
    "error": ("失败", "err"),
}


def _report_bucket(row: sqlite3.Row) -> str:
    status = str(row["status"] or "error")
    word = int(row["word_delta"] or 0)
    if status == "error":
        return "失败"
    if word <= 0:
        return "无变化"
    if word >= 10000:
        return "大量"
    if word >= 1000:
        return "中等"
    return "少量"


def _report_heat_level(value: int | None) -> int:
    value = int(value or 0)
    if value <= 0:
        return 0
    if value < 1000:
        return 1
    if value < 5000:
        return 2
    if value < 10000:
        return 3
    return 4


def _report_state_label(value: str | None) -> str:
    return str(value or "").strip().strip("[]").strip() or "—"


def _report_image_note(row: sqlite3.Row) -> str:
    images = int(row["image_delta"] or 0)
    if not images:
        return ""
    text_only = int(row["word_delta"] or 0) - 200 * images
    return f"含 {images} 张图（按 200 字/张折算），纯文字 {_format_number(text_only)} 字"


def _render_files(conn: sqlite3.Connection, row: sqlite3.Row, row_id: str) -> str:
    del row_id
    files = _file_rows(conn, row["date"], row["repo"])
    if not files:
        return f'<div class="empty">{_empty_detail_message(row)}</div>'

    body: list[str] = []
    for file_row in files:
        path = str(file_row["path"] or "")
        path_new = str(file_row["path_new"] or "")
        display_path = f"{path} → {path_new}" if path_new else path
        display_path_html = html.escape(display_path, quote=True)
        body.append(
            f'<tr><td class="p" title="{display_path_html}">{display_path_html}</td>'
            f'<td>{html.escape(_report_state_label(file_row["state"]))}</td>'
            f'<td class="n">{_format_number(file_row["changed_sentences"])}</td>'
            f'<td class="n">{_format_number(file_row["added_chars"])}</td>'
            f'<td class="n">{_format_number(file_row["added_images"])}</td>'
            f'<td class="n">{_format_number(file_row["deleted_lines"])}</td></tr>'
        )

    subtotal = _totals_from_files(
        FileChange(
            path=file_row["path"],
            path_new=file_row["path_new"],
            changed_sentences=int(file_row["changed_sentences"] or 0),
            added_chars=int(file_row["added_chars"] or 0),
            added_images=int(file_row["added_images"] or 0),
            deleted_lines=int(file_row["deleted_lines"] or 0),
            state=file_row["state"] or "",
        )
        for file_row in files
    )
    body.append(
        f'<tr class="subtotal"><td>限定路径小计（{len(files)} 个文件）</td><td>—</td>'
        f'<td class="n">{_format_number(subtotal.changed_sentences)}</td>'
        f'<td class="n">{_format_number(subtotal.added_chars)}</td>'
        f'<td class="n">{_format_number(subtotal.added_images)}</td>'
        f'<td class="n">{_format_number(subtotal.deleted_lines)}</td></tr>'
    )
    return (
        '<table class="feed-files"><thead><tr><th>文件</th><th>状态</th>'
        '<th class="n">句</th><th class="n">新增字</th><th class="n">图</th>'
        f'<th class="n">删行</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def _render_day(
    conn: sqlite3.Connection,
    day: str,
    recent_days: Sequence[str],
    top_n: int,
    active: bool,
    generated: str,
) -> tuple[str, str, str]:
    rows = _day_rows(conn, day)
    by_group: dict[str, list[sqlite3.Row]] = {group[0]: [] for group in REPORT_GROUPS}
    for row in rows:
        by_group[_report_bucket(row)].append(row)

    history: dict[str, dict[str, int]] = {}
    by_repo_recent: dict[str, int] = {}
    for recent_day in recent_days:
        for item in _day_rows(conn, recent_day):
            if str(item["status"] or "error") == "error":
                continue
            value = int(item["word_delta"] or 0)
            history.setdefault(str(item["repo"]), {})[recent_day] = value
            by_repo_recent[str(item["repo"])] = by_repo_recent.get(str(item["repo"]), 0) + value

    ranked = sorted(
        ((name, value) for name, value in by_repo_recent.items() if value > 0),
        key=lambda item: (-item[1], item[0].lower()),
    )[: max(0, top_n)]
    if ranked:
        rank_html = "".join(
            f'<li><span class="rank-name" title="{html.escape(name, quote=True)}">{html.escape(name)}</span>'
            f'<span class="rank-value">{_format_number(value)}</span></li>'
            for name, value in ranked
        )
    else:
        rank_html = '<li class="empty">暂无正向变化数据。</li>'

    display_days: list[str | None] = [None] * max(0, 7 - len(recent_days)) + list(recent_days[-7:])
    display_days = display_days[-7:]

    def heat_cells(repo: str) -> str:
        cells: list[str] = []
        for slot in display_days:
            value = history.get(repo, {}).get(slot) if slot else None
            if slot is None or value is None:
                cells.append('<i title="待累积"></i>')
                continue
            level = _report_heat_level(value)
            klass = f' class="h{level}"' if level else ""
            title = f"{slot} · {_format_number(value)} 字 · 档位 {level}/4"
            cells.append(f'<i{klass} title="{html.escape(title, quote=True)}"></i>')
        return f'<div class="cells" aria-hidden="true">{"".join(cells)}</div>'

    group_sections: list[str] = []
    detail_index = 0
    for group_name, _threshold, fill, ink, description in REPORT_GROUPS:
        group_rows = by_group[group_name]
        entries: list[str] = []
        group_total = sum(int(item["word_delta"] or 0) for item in group_rows)
        for index, item in enumerate(group_rows):
            repo = str(item["repo"] or "")
            branch = str(item["branch"] or "—")
            repo_url = str(item["repo_url"] or "")
            status = str(item["status"] or "error")
            status_text, status_class = REPORT_STATUS.get(status, ("失败", "err"))
            word = int(item["word_delta"] or 0)
            repo_title = f"{repo} · {branch}（打开仓库远端）"
            repo_link = (
                f'<a class="name" href="{html.escape(repo_url, quote=True)}" target="_blank" rel="noopener" '
                f'title="{html.escape(repo_title, quote=True)}">{html.escape(repo)}</a>'
            )
            base_subject = html.escape(str(item["base_commit_subject"] or "—"), quote=True)
            tip_subject = html.escape(str(item["commit_subject"] or "—"), quote=True)
            base_detail = (
                f'<b>{html.escape(_short_sha(item["base_commit"]))}</b> '
                f'{html.escape(_short_commit_time(item["base_commit_time"]))} · '
                f'<span class="commit-subject" title="{base_subject}">{base_subject}</span>'
            )
            tip_detail = (
                f'<b>{html.escape(_short_sha(item["tip_commit"]))}</b> '
                f'{html.escape(_short_commit_time(item["commit_time"]))} · '
                f'<span class="commit-subject" title="{tip_subject}">{tip_subject}</span>'
            )
            paths = _load_paths(item["effective_paths"])
            path_text = "全仓" if not paths else "、".join(paths)
            image_detail = _report_image_note(item)
            image_suffix = f' · {html.escape(image_detail)}' if image_detail else ""
            detail_id = f'detail-{day.replace("-", "")}-{detail_index}'
            detail_index += 1
            entries.append(
                f'<div class="entry" data-group data-name="{html.escape(repo, quote=True)}" data-word="{word}">'
                f'<div class="row"><div><span class="repo-name">{repo_link}</span>'
                f'<span class="br">{html.escape(branch)}</span></div>{heat_cells(repo)}'
                f'<div class="num big">{_format_number(word)}</div>'
                f'<div class="state"><span class="dot {status_class}"></span>{html.escape(status_text)} · '
                f'{_format_number(item["matched_files"])} 文件</div>'
                f'<div style="text-align:right"><button class="tgl" type="button" data-toggle="{detail_id}" '
                f'aria-expanded="false" aria-controls="{detail_id}">展开</button></div></div>'
                f'<div class="detail" id="{detail_id}" hidden>'
                f'<div class="dkv">基线 {base_detail}</div>'
                f'<div class="dkv">最新 {tip_detail}</div>'
                f'<div class="dkv">{_format_number(word)} 字（{_format_number(item["sentence_delta"])} 句 / '
                f'{_format_number(item["deleted_lines"])} 删行）{image_suffix} · 生效路径 '
                f'<b>{html.escape(path_text)}</b></div>'
                f'{_render_files(conn, item, detail_id)}</div></div>'
            )
        group_sections.append(
            f'<section class="grp" data-grp="{html.escape(group_name, quote=True)}"><div class="grphead">'
            f'<span class="gsw" style="background:{fill}"></span><span class="gn" style="color:{ink}">'
            f'{html.escape(group_name)}</span><span class="gc">{html.escape(description)} · {len(group_rows)} 个仓库</span>'
            f'<span class="gw">合计 {_format_number(group_total)} 字</span></div>'
            f'<div class="grpbody" data-group-container>{"".join(entries)}</div></section>'
        )

    total_repos = len(rows) or 1
    stack = "".join(
        f'<i style="width:{100.0 * len(by_group[group[0]]) / total_repos:.1f}%;background:{group[2]}" '
        f'title="{html.escape(group[0])} {len(by_group[group[0]])} 个仓库"></i>'
        for group in REPORT_GROUPS
        if by_group[group[0]]
    )
    legend = "".join(
        f'<li><span><span class="sw" style="background:{group[2]}"></span>'
        f'<button type="button" data-jump-group="{html.escape(group[0], quote=True)}">{html.escape(group[0])}</button>'
        f'<span style="color:var(--muted);margin-left:6px">{html.escape(group[4])}</span></span>'
        f'<span class="cnt">{len(by_group[group[0]])}</span></li>'
        for group in REPORT_GROUPS
    )
    daily_total = sum(int(item["word_delta"] or 0) for item in rows)
    changed = sum(str(item["status"] or "") == "ok" for item in rows)
    matched_files = sum(int(item["matched_files"] or 0) for item in rows)
    images = sum(int(item["image_delta"] or 0) for item in rows)
    side_meta_class = "side-meta active" if active else "side-meta"
    side_meta = (
        f'<div class="{side_meta_class}" data-side-meta="{html.escape(day, quote=True)}">'
        f'{html.escape(day)} · 生成于 {html.escape(generated)}</div>'
    )
    side_day_class = "side-day active" if active else "side-day"
    side_day = (
        f'<div class="{side_day_class}" data-side-day="{html.escape(day, quote=True)}">'
        f'<div class="sect"><div class="lbl">改动档位分布（{len(rows)} 仓）</div><div class="stack">{stack}</div>'
        f'<ul class="legend">{legend}</ul></div>'
        f'<div class="sect"><div class="lbl">当日概览</div><div class="kpis">'
        f'<div class="kpi"><div class="v">{_format_number(daily_total)}</div><div class="k">新增字数</div></div>'
        f'<div class="kpi"><div class="v">{changed}<span style="font-size:12px;color:var(--muted)">/{len(rows)}</span></div><div class="k">有变更</div></div>'
        f'<div class="kpi"><div class="v">{_format_number(matched_files)}</div><div class="k">命中文件</div></div>'
        f'<div class="kpi"><div class="v">{_format_number(images)}</div><div class="k">新增图片</div></div>'
        f'</div></div>'
        f'<div class="sect"><div class="lbl">近 7 天变化量排行（前 {max(0, top_n)}）</div>'
        f'<ol class="rank">{rank_html}</ol></div></div>'
    )
    section_class = "day active" if active else "day"
    main_day = (
        f'<section class="{section_class}" id="day-{html.escape(day, quote=True)}">'
        f'<div class="top"><div class="t">今日变更流</div>'
        f'<div class="m">按改动档位分组 · 每行 7 格 = 近 7 天热力（已累积 {len(recent_days)} 天）· '
        f'点「展开」看文件级明细</div><span class="spark-baseline" aria-hidden="true"></span></div>'
        f'{"".join(group_sections)}'
        f'<div class="legendbar"><b>口径</b>：当日新增字数 = 窗口内生效路径下的中文净增字数，每张新增图片按 200 字折算；'
        f'「大量 ≥ 10,000 / 中等 1,000–9,999 / 少量 1–999 / 无变化 0 / 失败」。趋势是<b>变化量</b>，不是仓库中文总量。</div>'
        f'</section>'
    )
    return side_meta, side_day, main_day


_REPORT_HIDDEN_PATH = "[本机绝对路径已隐藏]"


def _report_safe_text(value: Any) -> str:
    """Keep report metadata portable by redacting machine-local paths."""
    raw = "" if value is None else str(value).strip()
    normalized = raw.replace("\\", "/")
    if (
        raw.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:/", normalized)
        or "/mnt/" in normalized.lower()
        or "/home/" in normalized.lower()
    ):
        return _REPORT_HIDDEN_PATH
    return raw


def _report_safe_paths(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        raw = "" if item is None else str(item).strip()
        if not raw:
            continue
        safe = _report_safe_text(raw)
        if safe != _REPORT_HIDDEN_PATH:
            safe = normalize_path(raw)
        if safe and safe not in result:
            result.append(safe)
    return result


def _report_public_effective_paths(config: dict[str, Any], repo_config: dict[str, Any]) -> list[str]:
    common = _report_safe_paths(config.get("common_paths_filter"))
    local = _report_safe_paths(repo_config.get("paths_filter"))
    mode = str(repo_config.get("paths_mode", "union")).lower()
    paths = local if mode == "replace" else common + local
    result: list[str] = []
    for path in paths:
        if path and path not in result:
            result.append(path)
    return result


def _report_day_snapshot(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    rows = _day_rows(conn, day)

    def first_value(field: str) -> str:
        for row in rows:
            value = row[field]
            if value:
                return str(value)
        return ""

    return {
        "date": day,
        "slot_end": first_value("slot_end"),
        "window_start": first_value("window_start"),
        "window_end": first_value("window_end"),
        "repos_total": len(rows),
        "repos_ok": sum(str(row["status"] or "") == "ok" for row in rows),
        "repos_no_change": sum(str(row["status"] or "") == "no_change" for row in rows),
        "repos_error": sum(str(row["status"] or "") == "error" for row in rows),
    }


def _report_config_json(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    dates: Sequence[str],
    generated: str,
) -> str:
    report_config = config.get("report", {}) if isinstance(config.get("report"), dict) else {}
    schedule = config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {}
    try:
        default_days = int(report_config.get("default_days", DEFAULT_REPORT["default_days"]))
    except (TypeError, ValueError):
        default_days = DEFAULT_REPORT["default_days"]
    try:
        top_n_changed = int(report_config.get("top_n_changed", DEFAULT_REPORT["top_n_changed"]))
    except (TypeError, ValueError):
        top_n_changed = DEFAULT_REPORT["top_n_changed"]
    try:
        window_hours = int(schedule.get("window_hours", DEFAULT_SCHEDULE["window_hours"]))
    except (TypeError, ValueError):
        window_hours = DEFAULT_SCHEDULE["window_hours"]

    repos: list[dict[str, Any]] = []
    configured_repos = config.get("repos", [])
    if isinstance(configured_repos, list):
        for repo_config in configured_repos:
            if not isinstance(repo_config, dict):
                continue
            effective = _report_public_effective_paths(config, repo_config)
            repos.append(
                {
                    "name": _report_safe_text(repo_config.get("name", "")),
                    "branch": _report_safe_text(repo_config.get("branch", "auto") or "auto"),
                    "enabled": bool(repo_config.get("enabled", True)),
                    "paths_mode": str(repo_config.get("paths_mode", "union")).lower(),
                    "paths_filter": _report_safe_paths(repo_config.get("paths_filter")),
                    "effective_paths": "全仓" if not effective else "、".join(effective),
                }
            )

    payload = {
        "report": {
            "generated_at": generated,
            "default_days": default_days,
            "top_n_changed": top_n_changed,
            "image_char_count": 200,
            "window_mode": str(schedule.get("window_mode", DEFAULT_SCHEDULE["window_mode"])),
            "window_hours": window_hours,
            "schedule_time": str(schedule.get("time", DEFAULT_SCHEDULE["time"])),
            "common_paths_filter": _report_safe_paths(config.get("common_paths_filter")),
        },
        "days": [_report_day_snapshot(conn, day) for day in dates],
        "repos": repos,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).replace("<", "\\u003c")


def generate_report(
    config: dict[str, Any],
    *,
    db_path: Path | str | None = None,
    days: int | None = None,
    selected_date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    out_path: Path | str | None = None,
    update_latest: bool = True,
) -> Path:
    database = Path(db_path) if db_path is not None else _db_path()
    conn = connect_db(database)
    try:
        report_config = config.get("report", {}) if isinstance(config.get("report"), dict) else {}
        configured_days = int(days or report_config.get("default_days", DEFAULT_REPORT["default_days"]))
        all_dates = [row[0] for row in conn.execute("SELECT DISTINCT date FROM repo_daily ORDER BY date DESC")]
        if from_date or to_date:
            lower = from_date or min(all_dates, default=to_date or "")
            upper = to_date or max(all_dates, default=from_date or "")
            dates = sorted(day for day in all_dates if lower <= day <= upper)
        else:
            anchor = selected_date if selected_date in all_dates else (all_dates[0] if all_dates else None)
            dates = list(reversed(_recent_days(conn, anchor, configured_days)))
        newest = selected_date if selected_date in dates else (dates[-1] if dates else None)
        generated = display_time(now_local())
        title = str(config.get("report", {}).get("title", DEFAULT_REPORT["title"]))
        options = []
        # The newest actual DB date is the default report date, as required by
        # the preview interaction.  Range reports still expose the selected
        # date when one was requested.
        for day in reversed(dates):
            label = f"{html.escape(day)}（最近一次生成）" if day == newest else html.escape(day)
            selected_attr = " selected" if day == newest else ""
            options.append(f'<option value="{html.escape(day)}"{selected_attr}>{label}</option>')
        if from_date or to_date:
            section_windows = {day: [item for item in dates if item <= day] for day in dates}
        else:
            section_windows = {day: list(reversed(_recent_days(conn, day, configured_days))) for day in dates}
        rendered_days = [
            _render_day(
                conn,
                day,
                section_windows[day],
                int(report_config.get("top_n_changed", DEFAULT_REPORT["top_n_changed"])),
                day == newest,
                generated,
            )
            for day in dates
        ]
        side_meta = "".join(side_meta for side_meta, _side_day, _main_day in rendered_days)
        side_days = "".join(side_day for _side_meta, side_day, _main_day in rendered_days)
        main_days = "".join(main_day for _side_meta, _side_day, main_day in rendered_days)
        side = (
            '<aside class="side"><h1>分支字数变化 · 变更流</h1>'
            f'{side_meta}'
            '<div class="sect"><div class="lbl first">报告日期</div><div class="ctrl">'
            f'<select id="daySel">{"".join(options)}</select>'
            '<select id="sortSel" aria-label="排序方式"><option value="word" selected>按变化量降序</option>'
            '<option value="name">按名称升序(档位内)</option></select></div>'
            '<div class="ctrl" style="margin-top:6px"><button type="button" data-all="open">全部展开</button>'
            '<button type="button" data-all="close">全部收起</button></div></div>'
            f'{side_days}</aside>'
        )
        config_json = _report_config_json(conn, config, dates, generated)
        config_block = (
            '<details class="meta-block"><summary>本次采集参数（JSON）</summary>'
            f'<pre class="config-json">{config_json}</pre></details>'
        )
        main = f'<main class="main">{main_days}{config_block}</main>'
        markup = [
            '<!doctype html>',
            '<html lang="zh-CN"><head><meta charset="utf-8">',
            f'<title>{html.escape(title)} · 报表</title>',
            '<style>',
            REPORT_CSS,
            f'</style></head><body><div class="app">{side}{main}</div>',
            f'<script>{REPORT_JS}</script>',
            '</body></html>',
        ]
        content = "\n".join(markup)
    finally:
        conn.close()
    reports = _data_dir() / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if out_path is not None:
        output = Path(out_path)
        if not output.is_absolute():
            output = ROOT / output
    elif from_date or to_date:
        output = reports / f"{from_date or dates[0]}_{to_date or dates[-1]}.html"
    else:
        output = reports / f"{newest or now_local().date().isoformat()}.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8", newline="\n")
    if update_latest and out_path is None:
        shutil.copyfile(output, reports / "latest.html")
    return output.resolve()


def _config_for_repo(config: dict[str, Any], name: str) -> dict[str, Any]:
    for item in config.get("repos", []):
        if isinstance(item, dict) and str(item.get("name")) == name:
            return item
    raise MonitorError(f"配置中没有仓库：{name}")


def _init_config(config: dict[str, Any]) -> Path:
    repos_root = Path(str(config.get("repos_root", _default_repos_root()))).expanduser()
    if not repos_root.is_absolute():
        repos_root = ROOT / repos_root
    generated_repos: list[dict[str, Any]] = []
    if repos_root.exists():
        for repo_path in sorted(repos_root.iterdir(), key=lambda item: item.name.lower()):
            if not _repo_has_git(repo_path):
                continue
            url = repo_remote_url(repo_path)
            branch = resolve_remote_branch(repo_path, url) or "auto"
            generated_repos.append(
                {
                    "name": repo_path.name,
                    "url": url,
                    "branch": branch,
                    "paths_filter": [],
                    "paths_mode": "union",
                    "enabled": True,
                }
            )
    else:
        print(f"警告：仓库根目录不存在：{repos_root}")
    generated = dict(config)
    generated["repos_root"] = str(repos_root.resolve())
    generated["repos"] = generated_repos
    target = CONFIG_PATH if not CONFIG_PATH.exists() else ROOT / "monitor.config.generated.json"
    write_json(target, generated)
    print(f"已写入配置：{target.resolve()}（仓库 {len(generated_repos)} 个）")
    return target


def _check_config(config: dict[str, Any]) -> int:
    repos_root = Path(str(config.get("repos_root", ""))).expanduser()
    if not repos_root.is_absolute():
        repos_root = ROOT / repos_root
    schedule = config.get("schedule", {}) if isinstance(config.get("schedule"), dict) else {}
    print(
        "schedule.clamp_tip_to_window_end="
        f"{str(bool(schedule.get('clamp_tip_to_window_end', True))).lower()}"
        f"（窗口末端早于当前 {TIP_CLAMP_GRACE_MINUTES} 分钟时启用）"
    )
    publish = _publish_settings(config)
    publishers, array_syntax = _publishers(config)
    if array_syntax:
        methods = ",".join(item["method"] for item in publishers)
        targets = ",".join(item["target"] for item in publishers)
        print(f"publish: enabled={str(publish['enabled']).lower()} method={methods} target={targets}")
    else:
        print(
            f"publish: enabled={str(publish['enabled']).lower()} "
            f"method={publish['method']} target={publish['target']}"
        )
    delays = publish["retry_delays_s"]
    retry_text = "/".join(f"{delay:g}s" for delay in delays) if delays else "关闭"
    print(f"publish 重试: {retry_text}（失败后依次等待重试，共 {len(delays) + 1} 次尝试）")
    output_rows: list[dict[str, str]] = []
    problems = 0
    for item in publishers:
        if item["method"] not in {"scp", "copy", "git"}:
            problems += 1
            print(f"[失败] publish: 不支持的 method {item['method']}")
        if item["enabled"] and not item["target"]:
            problems += 1
            print("[失败] publish: enabled=true 但 target 为空")
    for item in config.get("repos", []):
        if not isinstance(item, dict) or not item.get("name") or not item.get("enabled", True):
            continue
        name = str(item["name"])
        repo_path = repos_root / name
        paths = effective_paths(config, item)
        if not _repo_has_git(repo_path):
            problems += 1
            output_rows.append({"repo": name, "path": "", "branch": "", "status": "error", "reason": "仓库缺失"})
            print(f"[失败] {name}: 仓库缺失")
            continue
        branch = str(item.get("branch") or config.get("branch_default", "auto"))
        if branch.lower() == "auto":
            branch = resolve_remote_branch(repo_path, str(item.get("url", ""))) or ""
            if not branch:
                problems += 1
                output_rows.append({"repo": name, "path": "", "branch": "", "status": "error", "reason": "远端默认分支解析失败"})
                print(f"[失败] {name}: 远端默认分支解析失败")
                continue
        if not paths:
            output_rows.append({"repo": name, "path": "(全仓)", "branch": branch, "status": "ok", "reason": ""})
            print(f"[通过] {name}: 分支 {branch}，全仓口径")
            continue
        ref = _branch_ref(repo_path, branch)
        for path in paths:
            result = git(repo_path, ["cat-file", "-e", f"{ref}:{path}"], timeout=60)
            status = "ok" if result.returncode == 0 else "error"
            reason = "" if status == "ok" else "路径不存在"
            problems += status == "error"
            output_rows.append({"repo": name, "path": path, "branch": branch, "status": status, "reason": reason})
            print(f"[{('通过' if status == 'ok' else '失败')}] {name}: {branch}:{path} {reason}".rstrip())
    output = _data_dir() / "config-check.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["repo", "path", "branch", "status", "reason"])
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"检查结果：{output.resolve()}，问题 {problems} 项")
    return 1 if problems else 0


def _print_records(records: Sequence[DailyRecord], report_path: Path | None) -> None:
    for record in records:
        if record.status == "error":
            print(f"{record.repo}: error ({record.reason})")
        else:
            print(
                f"{record.repo}: {record.status} word={record.word_delta} "
                f"sentences={record.sentence_delta} images={record.image_delta} files={record.matched_files}"
            )
    if report_path:
        print(f"report: {report_path}")


def _is_all_paths_argument(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    return stripped in {"", '""', "''"} or stripped.lower() in {"all", "*"}


def _parse_paths(value: str | None) -> list[str]:
    if _is_all_paths_argument(value):
        return []
    return [normalize_path(part) for part in value.split(",") if normalize_path(part)]


def command_collect(config: dict[str, Any], args: argparse.Namespace) -> int:
    repo_config = _config_for_repo(config, args.repo)
    repo_root = Path(str(config.get("repos_root", ""))).expanduser()
    if not repo_root.is_absolute():
        repo_root = ROOT / repo_root
    repo_path = repo_root / args.repo
    if not _repo_has_git(repo_path):
        raise MonitorError("仓库目录不存在")
    raw_paths = getattr(args, "paths", None)
    paths = _parse_paths(raw_paths)
    outcome = run_counter(config, repo_path, args.repo, args.base, args.target, paths, now_local().date().isoformat())
    if not _is_all_paths_argument(raw_paths) and not outcome.no_change and outcome.matched_files == 0:
        print(f"warn: 生效路径 {json.dumps(paths, ensure_ascii=False)} 未匹配任何文件")
    if outcome.no_change:
        print("word=0 sentences=0 images=0 deleted=0 files=0")
    else:
        print(
            f"word={outcome.totals.added_chars} sentences={outcome.totals.changed_sentences} "
            f"images={outcome.totals.added_images} deleted={outcome.totals.deleted_lines} files={outcome.matched_files}"
        )
    return 0


def _parse_schedule_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise MonitorError(f"schedule.time 格式错误：{value}")
    return int(match.group(1)), int(match.group(2))


def _pid_path() -> Path:
    return _data_dir() / "daemon.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _acquire_pid() -> None:
    path = _pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old_pid = int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and _pid_alive(old_pid) and old_pid != os.getpid():
            raise MonitorError(f"daemon 已在运行（PID {old_pid}）")
    path.write_text(str(os.getpid()), encoding="ascii")


def daemon_loop(config: dict[str, Any], run_now: bool = False) -> int:
    _acquire_pid()
    pid_path = _pid_path()
    try:
        if bool(config.get("schedule", {}).get("catch_up_on_start", True)):
            try:
                perform_catch_up(config)
            except Exception as exc:
                error_log = ROOT / "logs" / "monitor.err.log"
                error_log.parent.mkdir(parents=True, exist_ok=True)
                with error_log.open("a", encoding="utf-8") as handle:
                    handle.write(f"{display_time(now_local())} startup catch-up: {exc}\n")
        force_run = run_now
        while True:
            now = now_local()
            hour, minute = _parse_schedule_time(str(config.get("schedule", {}).get("time", "19:00")))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= scheduled:
                grace = int(config.get("schedule", {}).get("missed_run_grace_minutes", 180))
                due = now <= scheduled + dt.timedelta(minutes=grace)
                if force_run or due:
                    today = now.date().isoformat()
                    conn = connect_db(_db_path())
                    try:
                        if str(config.get("schedule", {}).get("window_mode", "slot")).lower() == "rolling":
                            already = conn.execute("SELECT 1 FROM runs WHERE started_at LIKE ? LIMIT 1", (today + "%",)).fetchone()
                        else:
                            already = conn.execute("SELECT 1 FROM runs WHERE slot_end=? LIMIT 1", (iso_time(scheduled),)).fetchone()
                    finally:
                        conn.close()
                    if force_run or already is None:
                        run_log = ROOT / "logs" / f"monitor-{today}.log"
                        run_log.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            with run_log.open("a", encoding="utf-8") as handle, contextlib.redirect_stdout(handle), contextlib.redirect_stderr(handle):
                                records, report = run_collection(config)
                                publish_report(config, report, report_date=today)
                                _print_records(records, report)
                        except Exception as exc:
                            error_log = ROOT / "logs" / "monitor.err.log"
                            error_log.parent.mkdir(parents=True, exist_ok=True)
                            with error_log.open("a", encoding="utf-8") as handle:
                                handle.write(f"{display_time(now_local())} scheduled run: {exc}\n")
                        force_run = False
                        continue
            tomorrow = scheduled + dt.timedelta(days=1) if now >= scheduled else scheduled
            remaining = max(1.0, (tomorrow - now).total_seconds() if now >= scheduled else (scheduled - now).total_seconds())
            time.sleep(min(60.0, remaining))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        error_log = ROOT / "logs" / "monitor.err.log"
        error_log.parent.mkdir(parents=True, exist_ok=True)
        with error_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{display_time(now_local())} {exc}\n")
        return 1
    finally:
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except (OSError, ValueError):
            pass


def command_status() -> int:
    pid_path = _pid_path()
    pid = pid_path.read_text(encoding="ascii").strip() if pid_path.exists() else ""
    alive = False
    if pid:
        try:
            alive = _pid_alive(int(pid))
        except ValueError:
            pass
    print(f"daemon: {'running' if alive else 'stopped'}" + (f" PID={pid}" if pid else ""))
    db = _db_path()
    if db.exists():
        conn = connect_db(db)
        try:
            row = conn.execute("SELECT started_at, finished_at, repo_ok, repo_failed FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row:
            print(f"last run: {row['started_at']} -> {row['finished_at']} ok={row['repo_ok']} failed={row['repo_failed']}")
    logs = sorted((ROOT / "logs").glob("monitor-*.log")) if (ROOT / "logs").exists() else []
    if logs:
        print("log tail:")
        print("\n".join(logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-10:]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分支中文字数变化监控")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--once", action="store_true", help="采集一次后退出")
    parser.add_argument("--daemon", action="store_true", help="进入常驻调度")
    parser.add_argument("--now", action="store_true", help="daemon 启动后立即采集一次")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-config", help="扫描仓库生成配置")
    sub.add_parser("check-config", help="检查仓库、分支和路径")
    sub.add_parser("status", help="显示 daemon 与最近运行状态")
    run = sub.add_parser("run", help="采集并入库")
    run.add_argument("--limit", type=int)
    run.add_argument("--only", action="extend", nargs="+")
    run.add_argument("--date")
    run.add_argument("--catch-up", action="store_true", help="先检查并补跑最近缺失槽位")
    run.add_argument("--slot", help="手动指定所属槽位（ISO8601）")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-publish", action="store_true", help="跳过报表发布")
    backfill = sub.add_parser("backfill", help="按日期顺序补跑历史槽位")
    backfill.add_argument("--days", type=int, required=True)
    backfill.add_argument("--to", dest="to_date")
    backfill.add_argument("--only", action="extend", nargs="+")
    backfill.add_argument("--limit", type=int)
    backfill.add_argument("--purge", action="store_true")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--no-publish", action="store_true", help="跳过报表发布")
    collect = sub.add_parser("collect", help="单仓单窗口调试")
    collect.add_argument("--repo", required=True)
    collect.add_argument("--base", required=True)
    collect.add_argument("--target", required=True)
    collect.add_argument("--paths", help="路径过滤，逗号分隔；不传或传 all = 全仓")
    report = sub.add_parser("report", help="从数据库生成 HTML 报表")
    report.add_argument("--days", type=int)
    report.add_argument("--date")
    report.add_argument("--from", dest="from_date")
    report.add_argument("--to", dest="to_date")
    report.add_argument("--out")
    report.add_argument("--no-publish", action="store_true", help="跳过报表发布")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.daemon:
        config = load_config()
        return daemon_loop(config, args.now)
    if args.once:
        config = load_config()
        perform_catch_up(config)
        return 0
    if args.command == "init-config":
        _init_config(load_config())
        return 0
    if args.command == "check-config":
        return _check_config(load_config())
    if args.command == "status":
        return command_status()
    config = load_config()
    if args.command == "run":
        if args.catch_up:
            perform_catch_up(config, limit=args.limit, only=args.only or (), no_publish=args.no_publish)
            return 0
        records, report = run_collection(
            config,
            limit=args.limit,
            only=args.only or (),
            date=args.date,
            dry_run=args.dry_run,
            slot_end=parse_slot(args.slot),
        )
        publish_report(
            config,
            report,
            report_date=args.date or (args.slot and parse_slot(args.slot).date().isoformat()),
            no_publish=args.no_publish,
        )
        _print_records(records, report)
        return 0
    if args.command == "backfill":
        return command_backfill(config, args)
    if args.command == "collect":
        return command_collect(config, args)
    if args.command == "report":
        path = generate_report(
            config,
            days=args.days,
            selected_date=args.date,
            from_date=args.from_date,
            to_date=args.to_date,
            out_path=args.out,
            update_latest=args.out is None,
        )
        publish_report(
            config,
            path,
            report_date=args.date or args.to_date,
            no_publish=args.no_publish,
        )
        print(f"report: {path}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MonitorError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
