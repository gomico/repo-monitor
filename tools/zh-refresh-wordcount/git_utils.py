"""Git repository queries used by the counter."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from models import FileChange, GitBranch, GitRef, REF_DATE_FORMAT


_git_activity = threading.local()
_git_url = threading.local()

NETWORK_CACHE_LIMIT = 50 * 1024 * 1024
NETWORK_CACHE_MARKER = ".zh-refresh-wordcount-cache"
IMAGE_EXTENSIONS = frozenset(
    {
        ".apng",
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)


@contextmanager
def git_activity_reporter(reporter: Callable[[str], None]) -> Iterator[None]:
    previous = getattr(_git_activity, "reporter", None)
    _git_activity.reporter = reporter
    try:
        yield
    finally:
        _git_activity.reporter = previous


@contextmanager
def git_url_override(git_url: str) -> Iterator[None]:
    previous = getattr(_git_url, "value", None)
    _git_url.value = git_url
    try:
        yield
    finally:
        _git_url.value = previous


def run_git(
    args: list[str],
    *,
    cwd: Path,
    text: bool = True,
    capture_stdout: bool = True,
) -> str:
    reporter = getattr(_git_activity, "reporter", None)
    git_url = getattr(_git_url, "value", None)
    # Keep UTF-8 path names readable in commands such as diff/ls-files.
    # Git otherwise emits non-ASCII bytes as C-style octal escapes, which
    # then become literal text in the generated CSV files.
    command = ["git", "-c", "core.quotePath=false"]
    if isinstance(git_url, str):
        safe_git_url = display_git_url(git_url)
        if safe_git_url != git_url:
            command.extend(["-c", f"url.{git_url}.insteadOf={safe_git_url}"])
    command.extend(args)
    kwargs: dict[str, object] = {}
    if text:
        kwargs.update({"text": True, "encoding": "utf-8", "errors": "replace"})

    if reporter is not None:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr_parts: list[bytes] = []

        def read_stderr() -> None:
            assert process.stderr is not None
            line = bytearray()
            while True:
                chunk = process.stderr.read(1)
                if not chunk:
                    break
                stderr_parts.append(chunk)
                if chunk in {b"\r", b"\n"}:
                    message = line.decode("utf-8", errors="replace").strip()
                    if message:
                        reporter(message)
                    line.clear()
                else:
                    line.extend(chunk)
            message = line.decode("utf-8", errors="replace").strip()
            if message:
                reporter(message)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        stdout_bytes = process.stdout.read() if process.stdout is not None else b""
        returncode = process.wait()
        stderr_thread.join()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
        if returncode != 0:
            message = stderr.strip() or "git command failed"
            raise RuntimeError(f"git {' '.join(args)}: {message}")
        reporter("")
        if text:
            return stdout_bytes.decode("utf-8", errors="replace")
        return stdout_bytes  # type: ignore[return-value]

    result = subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "git command failed"
        raise RuntimeError(f"git {' '.join(args)}: {message}")
    return result.stdout if capture_stdout else ""


def find_git_root(start: Path) -> Path:
    output = run_git(["rev-parse", "--show-toplevel"], cwd=start)
    return Path(output.strip()).resolve()


def repository_name_from_url(git_url: str) -> str:
    if "://" in git_url:
        candidate_source = urlsplit(git_url).path
    else:
        candidate_source = git_url
    candidate = re.split(r"[/\\:]", candidate_source.rstrip("/\\"))[-1]
    if candidate.lower().endswith(".git"):
        candidate = candidate[:-4]
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate).strip(" .")
    if candidate.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        candidate += "_"
    return candidate or "remote-repository"


def display_git_url(git_url: str) -> str:
    if "://" not in git_url:
        return git_url
    parts = urlsplit(git_url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def network_cache_key(git_url: str) -> str:
    safe_git_url = display_git_url(git_url)
    digest = hashlib.sha256(safe_git_url.encode("utf-8")).hexdigest()[:12]
    return f"{repository_name_from_url(safe_git_url)}-{digest}.git"


def network_cache_path(config_root: Path, git_url: str) -> Path:
    return config_root / "git-cache" / network_cache_key(git_url)


def directory_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += directory_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def cleanup_network_cache(
    cache_root: Path,
    *,
    active_cache: Path | None = None,
    limit: int = NETWORK_CACHE_LIMIT,
) -> list[Path]:
    if not cache_root.is_dir():
        return []
    for path in cache_root.iterdir():
        if path.is_dir() and path.name.startswith(".zh-refresh-cache-"):
            shutil.rmtree(path, ignore_errors=True)
    caches: list[tuple[float, Path, int]] = []
    for path in cache_root.iterdir():
        if not path.is_dir() or not (path / NETWORK_CACHE_MARKER).is_file():
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        caches.append((modified, path, directory_size(path)))
    total = sum(size for _modified, _path, size in caches)
    if total < limit:
        return []
    removed: list[Path] = []
    active_resolved = active_cache.resolve() if active_cache is not None else None
    for _modified, path, size in sorted(caches):
        if total < limit:
            break
        if active_resolved is not None and path.resolve() == active_resolved:
            continue
        shutil.rmtree(path)
        removed.append(path)
        total -= size
    return removed


def _sync_origin_refs(repo_root: Path, default_branch: str) -> None:
    output = run_git(
        ["for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads"],
        cwd=repo_root,
    )
    live_refs: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[0].startswith("refs/heads/"):
            continue
        branch_name = parts[0][len("refs/heads/") :]
        remote_ref = f"refs/remotes/origin/{branch_name}"
        run_git(["update-ref", remote_ref, parts[1]], cwd=repo_root)
        live_refs.add(remote_ref)
    if not live_refs:
        raise RuntimeError("远端仓库没有可用分支")
    existing = run_git(
        ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
        cwd=repo_root,
    )
    for ref in existing.splitlines():
        if ref != "refs/remotes/origin/HEAD" and ref not in live_refs:
            run_git(["update-ref", "-d", ref], cwd=repo_root)
    run_git(
        ["symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default_branch}"],
        cwd=repo_root,
    )


def clone_remote_repository(git_url: str, repo_root: Path) -> str:
    if not git_url.strip():
        raise RuntimeError("Git URL 不能为空")
    run_git(
        ["clone", "--bare", "--filter=blob:none", "--", git_url, str(repo_root)],
        cwd=repo_root.parent,
    )
    try:
        default_branch = run_git(["symbolic-ref", "--short", "HEAD"], cwd=repo_root).strip()
    except RuntimeError as exc:
        raise RuntimeError("远端仓库没有可用的默认分支") from exc
    if not default_branch:
        raise RuntimeError("远端仓库没有可用的默认分支")

    _sync_origin_refs(repo_root, default_branch)
    return default_branch


def prepare_cached_repository(git_url: str, repo_root: Path) -> tuple[str, str | None]:
    safe_git_url = display_git_url(git_url)
    marker = repo_root / NETWORK_CACHE_MARKER
    if marker.is_file():
        try:
            with git_url_override(git_url):
                run_git(["remote", "set-url", "origin", safe_git_url], cwd=repo_root)
                run_git(
                    [
                        "fetch",
                        "--prune",
                        "--prune-tags",
                        "--filter=blob:none",
                        "--update-head-ok",
                        "origin",
                        "+refs/heads/*:refs/heads/*",
                        "+refs/tags/*:refs/tags/*",
                    ],
                    cwd=repo_root,
                )
                default_branch = current_branch(repo_root)
                _sync_origin_refs(repo_root, default_branch)
            os.utime(repo_root)
            return default_branch, None
        except RuntimeError as exc:
            try:
                default_branch = current_branch(repo_root)
                _sync_origin_refs(repo_root, default_branch)
                os.utime(repo_root)
                warning = str(exc).replace(git_url, safe_git_url)
                return default_branch, f"远端更新失败，正在使用可能过期的缓存：{warning}"
            except RuntimeError:
                pass

    repo_root.parent.mkdir(parents=True, exist_ok=True)
    temp_parent = Path(tempfile.mkdtemp(prefix=".zh-refresh-cache-", dir=repo_root.parent))
    temp_repo = temp_parent / "repository.git"
    try:
        with git_url_override(git_url):
            default_branch = clone_remote_repository(git_url, temp_repo)
            run_git(["remote", "set-url", "origin", safe_git_url], cwd=temp_repo)
        (temp_repo / NETWORK_CACHE_MARKER).write_text(safe_git_url + "\n", encoding="utf-8")
        if repo_root.exists():
            shutil.rmtree(repo_root)
        temp_repo.replace(repo_root)
        os.utime(repo_root)
        return default_branch, None
    except RuntimeError as exc:
        raise RuntimeError(str(exc).replace(git_url, safe_git_url)) from exc
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


def resolve_ref(repo_root: Path, ref: str) -> GitRef:
    output = run_git(
        ["show", "-s", f"--date=format:{REF_DATE_FORMAT}", "--format=%H%x09%ad%x09%s", ref],
        cwd=repo_root,
    ).strip()
    sha, committed_at, message = output.split("\t", 2)
    return GitRef(sha=sha, committed_at=committed_at, message=message)


def current_branch(repo_root: Path) -> str:
    branch = run_git(["branch", "--show-current"], cwd=repo_root).strip()
    if not branch:
        raise RuntimeError("当前处于 detached HEAD，交互模式需要先切换到一个本地分支")
    return branch


def upstream_ref(repo_root: Path) -> str | None:
    try:
        upstream = run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo_root,
        ).strip()
    except RuntimeError:
        return None
    return upstream or None


def fetch_current_branch(repo_root: Path, branch: str) -> None:
    upstream = upstream_ref(repo_root)
    print(f"正在获取当前分支 {branch} 的最新远端数据...")
    if upstream and "/" in upstream:
        remote, _remote_branch = upstream.split("/", 1)
        run_git(["fetch", remote, "--prune"], cwd=repo_root)
        return
    run_git(["fetch", "--all", "--prune"], cwd=repo_root)


def is_worktree_clean(repo_root: Path) -> bool:
    return (
        run_git(
            ["status", "--porcelain", "--untracked-files=no"],
            cwd=repo_root,
        )
        == ""
    )


def confirm_pull_current_branch() -> bool:
    while True:
        answer = input("是否执行 git pull --ff-only 更新当前分支（默认更新）？[Y/n]: ").strip().lower()
        if not answer:
            return True
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("请输入 y 或 n。")


def pull_current_branch(repo_root: Path) -> None:
    if not is_worktree_clean(repo_root):
        print("已跟踪文件或暂存区有未提交改动，已跳过 git pull。请先处理本地改动后再更新。")
        input("请按 Enter 键继续...")
        return
    print("正在执行 git pull --ff-only...")
    run_git(["pull", "--ff-only"], cwd=repo_root)


def switch_branch(repo_root: Path, branch: str) -> bool:
    remote_prefix = "origin/"
    local_name = branch.removeprefix(remote_prefix)
    if current_branch(repo_root) == local_name:
        return False
    if not is_worktree_clean(repo_root):
        raise RuntimeError("已跟踪文件或暂存区有未提交改动，请先处理本地改动后再切换分支")
    if branch.startswith(remote_prefix):
        run_git(["switch", "--track", "-c", local_name, branch], cwd=repo_root)
    else:
        run_git(["switch", branch], cwd=repo_root)
    return True


def list_refs(repo_root: Path, max_refs: int | None, branch: str | None = None) -> list[GitRef]:
    args = [
        "log",
        f"--date=format:{REF_DATE_FORMAT}",
        "--format=%H%x09%ad%x09%s",
    ]
    if max_refs is not None:
        args.insert(1, f"--max-count={max_refs}")
    if branch is None:
        args.insert(1, "--all")
    elif branch.startswith("origin/"):
        args.append(f"refs/remotes/{branch}")
    else:
        args.append(f"refs/heads/{branch}")

    output = run_git(args, cwd=repo_root)
    refs: list[GitRef] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, committed_at, message = parts
        if sha in seen:
            continue
        seen.add(sha)
        refs.append(GitRef(sha=sha, committed_at=committed_at, message=message))
    return refs


def list_origin_branches(repo_root: Path) -> list[GitBranch]:
    output = run_git(
        [
            "for-each-ref",
            "--sort=-committerdate",
            f"--format=%(refname)%09%(refname:short)%09%(committerdate:format:{REF_DATE_FORMAT})%09%(objectname)",
            "refs/remotes/origin",
        ],
        cwd=repo_root,
    )
    branches: list[GitBranch] = []
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        full_name, name, committed_at, sha = parts
        if full_name == "refs/remotes/origin/HEAD":
            continue
        branches.append(GitBranch(name=name, committed_at=committed_at, sha=sha))
    return branches


def list_local_branches(repo_root: Path) -> list[GitBranch]:
    output = run_git(
        [
            "for-each-ref",
            "--sort=-committerdate",
            f"--format=%(refname)%09%(refname:short)%09%(committerdate:format:{REF_DATE_FORMAT})%09%(objectname)",
            "refs/heads",
        ],
        cwd=repo_root,
    )
    branches: list[GitBranch] = []
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            _full_name, name, committed_at, sha = parts
            branches.append(GitBranch(name=name, committed_at=committed_at, sha=sha))
    return branches


def copy_to_clipboard(text: str) -> None:
    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["clip"])
    elif sys.platform == "darwin":
        commands.append(["pbcopy"])
    else:
        commands.extend(
            [
                ["clip.exe"],
                ["wl-copy"],
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ]
        )

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        result = subprocess.run(
            command,
            input=text,
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return
    raise RuntimeError("未找到可用的剪贴板工具，无法复制 ref hash")


def read_git_file(repo_root: Path, ref: str, path: str) -> str:
    try:
        return run_git(["show", f"{ref}:{path}"], cwd=repo_root)
    except RuntimeError as exc:
        message = str(exc)
        if "exists on disk, but not in" in message or "does not exist" in message:
            return ""
        if "Path" in message and "does not exist" in message:
            return ""
        raise


def is_markdown_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(".md") or lowered.endswith(".markdown")


def is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def comparison_pathspecs() -> list[str]:
    extensions = (".md", ".markdown", *sorted(IMAGE_EXTENSIONS))
    return [f":(icase)*{extension}" for extension in extensions]


def is_excluded_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        "/en/" in f"/{normalized}/"
        or name.endswith("-en.md")
        or name.endswith("_en.md")
        or name.endswith("-en.markdown")
        or name.endswith("_en.markdown")
    )


def should_count_change(change: FileChange) -> bool:
    paths = [path for path in (change.old_path, change.new_path) if path]
    if not any(is_markdown_path(path) or is_image_path(path) for path in paths):
        return False
    return not any(is_excluded_path(path) for path in paths)


def list_changed_files(repo_root: Path, old_ref: str, new_ref: str) -> list[FileChange]:
    output = run_git(
        [
            "diff",
            "--name-status",
            "-M",
            "--diff-filter=ACMRD",
            old_ref,
            new_ref,
            "--",
            *comparison_pathspecs(),
        ],
        cwd=repo_root,
    )
    files: list[FileChange] = []
    for line in output.splitlines():
        if not line:
            continue

        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            if len(parts) == 3:
                files.append(FileChange(status=status, old_path=parts[1], new_path=parts[2]))
            continue

        if len(parts) != 2:
            continue
        path = parts[1]
        old_path = None if status == "A" else path
        files.append(FileChange(status=status, old_path=old_path, new_path=path))

    return [change for change in files if should_count_change(change)]


def prefetch_changed_file_blobs(
    repo_root: Path,
    old_ref: str,
    new_ref: str,
    changes: list[FileChange],
) -> None:
    paths = sorted(
        {
            path
            for change in changes
            for path in (change.old_path, change.new_path)
            if path is not None and is_markdown_path(path)
        }
    )
    base_args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        old_ref,
        new_ref,
        "--",
    ]
    batch: list[str] = []
    batch_size = sum(len(arg.encode("utf-8")) + 1 for arg in base_args)
    for path in paths:
        path_size = len(path.encode("utf-8")) + 1
        if batch and (len(batch) >= 100 or batch_size + path_size > 24 * 1024):
            run_git([*base_args, *batch], cwd=repo_root, capture_stdout=False)
            batch = []
            batch_size = sum(len(arg.encode("utf-8")) + 1 for arg in base_args)
        batch.append(path)
        batch_size += path_size
    if batch:
        run_git([*base_args, *batch], cwd=repo_root, capture_stdout=False)


def list_all_documents(repo_root: Path) -> list[str]:
    output = run_git(["ls-files", "-z"], cwd=repo_root)
    return [
        path
        for path in output.split("\0")
        if path
        and is_markdown_path(path)
        and not is_excluded_path(path)
        and (repo_root / path).is_file()
    ]
