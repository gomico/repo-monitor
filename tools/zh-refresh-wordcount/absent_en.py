"""Find Markdown documents whose paired English document is absent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from counter import count_chinese_chars, diff_sentences, split_sentences
from git_utils import is_excluded_path, is_markdown_path, read_git_file, run_git
from models import AbsentEnglishResult


def list_markdown_files(repo_root: Path, ref: str) -> list[str]:
    output = run_git(["ls-tree", "-r", "--name-only", "-z", ref], cwd=repo_root)
    return sorted(
        path
        for path in output.split("\0")
        if path and is_markdown_path(path) and not is_excluded_path(path)
    )


def english_candidates(chinese_path: str) -> list[str]:
    parts = chinese_path.split("/")
    if "zh" in parts:
        index = parts.index("zh")
        mapped = list(parts)
        mapped[index] = "en"
        return ["/".join(mapped)]
    stem, extension = chinese_path.rsplit(".", 1)
    if stem.endswith(".zh"):
        return [f"{stem[:-3]}.{extension}"]
    # Ascend 系列仓库根目录惯例 README_EN.md（大写 EN），与 -en/_en 一并作为候选
    return [f"{stem}-en.{extension}", f"{stem}_en.{extension}", f"{stem}_EN.{extension}"]


def _document_char_count(text: str) -> int:
    return sum(count_chinese_chars(sentence) for sentence in split_sentences(text))


def _changed_char_count(old_text: str, new_text: str) -> tuple[bool, int]:
    old_sentences = split_sentences(old_text)
    new_sentences = split_sentences(new_text)
    changes = diff_sentences(old_sentences, new_sentences, "")
    return old_sentences != new_sentences, sum(change.count for change in changes)


def find_absent_english(
    repo_root: Path,
    target_ref: str,
    baseline_ref: str | None = None,
) -> list[AbsentEnglishResult]:
    target_paths = list_markdown_files(repo_root, target_ref)
    target_set = {
        path
        for path in run_git(["ls-tree", "-r", "--name-only", "-z", target_ref], cwd=repo_root).split("\0")
        if path
    }
    baseline_set: set[str] = set()
    if baseline_ref is not None:
        baseline_set = {
            path
            for path in run_git(["ls-tree", "-r", "--name-only", "-z", baseline_ref], cwd=repo_root).split("\0")
            if path
        }

    results: list[AbsentEnglishResult] = []
    for chinese_path in target_paths:
        if not chinese_path.endswith((".zh.md", ".zh.markdown")):
            stem, extension = chinese_path.rsplit(".", 1)
            zh_variant = f"{stem}.zh.{extension}"
            if zh_variant in target_set:
                continue
        candidates = english_candidates(chinese_path)
        if any(candidate in target_set for candidate in candidates):
            continue

        target_text = read_git_file(repo_root, target_ref, chinese_path)
        if baseline_ref is None:
            results.append(
                AbsentEnglishResult(chinese_path, None, "[缺失]", _document_char_count(target_text))
            )
            continue

        baseline_english = next((candidate for candidate in candidates if candidate in baseline_set), None)
        if baseline_english is None:
            results.append(
                AbsentEnglishResult(chinese_path, None, "[缺失]", _document_char_count(target_text))
            )
            continue

        baseline_text = read_git_file(repo_root, baseline_ref, chinese_path)
        changed, chars = _changed_char_count(baseline_text, target_text)
        status = "[基线已有英文，中文有变化]" if changed else "[基线已有英文，中文无变化]"
        results.append(AbsentEnglishResult(chinese_path, baseline_english, status, chars if changed else 0))
    return results
