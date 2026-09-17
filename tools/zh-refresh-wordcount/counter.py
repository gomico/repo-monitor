"""Document normalization, sentence diffing, and workload counting."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from git_utils import (
    is_excluded_path,
    is_image_path,
    is_markdown_path,
    list_all_documents,
    list_changed_files,
    prefetch_changed_file_blobs,
    read_git_file,
)
from models import (
    COUNTED_CHAR_RE,
    HTML_IMAGE_RE,
    IMAGE_FILE_CHAR_COUNT,
    MARKDOWN_IMAGE_RE,
    MARKDOWN_LINK_RE,
    SENTENCE_END_RE,
    FileChange,
    FileResult,
    GitRef,
    SentenceChange,
    TextLine,
    URL_RE,
)


def strip_non_counted_tokens(text: str) -> str:
    image_tokens: list[str] = []

    def keep_image_text(match: re.Match[str]) -> str:
        image_tokens.append(f"{match.group(1)} {match.group(2)}")
        return f"__ZH_IMAGE_TOKEN_{len(image_tokens) - 1}__"

    text = MARKDOWN_IMAGE_RE.sub(keep_image_text, text)
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    text = URL_RE.sub("", text)
    for index, image_text in enumerate(image_tokens):
        text = text.replace(f"__ZH_IMAGE_TOKEN_{index}__", image_text)
    return text


def strip_markdown_markup(line: str) -> str:
    line = strip_non_counted_tokens(line)
    line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
    line = re.sub(r"^\s*=+\s+(.+?)\s*=*\s*$", r"\1", line)
    line = re.sub(r"^\s*[-+*]\s+", "", line)
    line = re.sub(r"^\s*\d+[.)]\s+", "", line)
    line = re.sub(r"^\s*>\s?", "", line)
    line = line.replace("\\", "")
    return re.sub(r"\s+", " ", line).strip()


def is_fence_start(line: str) -> str | None:
    match = re.match(r"^\s*(```+|~~~+)", line)
    if match:
        return match.group(1)[:3]
    return None


def is_asciidoc_block_delimiter(line: str) -> bool:
    stripped = line.strip()
    return stripped in {"----", "....", "++++"}


def normalize_document(text: str) -> list[TextLine]:
    lines: list[TextLine] = []
    in_fence = False
    fence_marker: str | None = None
    in_asciidoc_block = False

    def code_text(line: str) -> str:
        return line.strip() if COUNTED_CHAR_RE.search(line) else ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        fence_start = is_fence_start(line)

        if fence_start:
            if in_fence and fence_marker == fence_start:
                in_fence = False
                fence_marker = None
            elif not in_fence:
                in_fence = True
                fence_marker = fence_start
            lines.append(TextLine(raw=line, text=""))
            continue

        if in_fence:
            lines.append(TextLine(raw=line, text=code_text(line), is_code=True))
            continue

        if is_asciidoc_block_delimiter(line):
            in_asciidoc_block = not in_asciidoc_block
            lines.append(TextLine(raw=line, text=""))
            continue

        if in_asciidoc_block:
            lines.append(TextLine(raw=line, text=code_text(line), is_code=True))
            continue

        stripped = strip_markdown_markup(line)
        lines.append(TextLine(raw=line, text=stripped))

    return lines


def extract_image_references(text: str) -> list[str]:
    references: list[str] = []
    for line in normalize_document(text):
        if line.is_code:
            continue
        matches = [
            (match.start(), match.group(0))
            for pattern in (MARKDOWN_IMAGE_RE, HTML_IMAGE_RE)
            for match in pattern.finditer(line.raw)
        ]
        references.extend(value for _start, value in sorted(matches))
    return references


def count_added_image_references(old_text: str, new_text: str) -> int:
    old_references = extract_image_references(old_text)
    new_references = extract_image_references(new_text)
    matcher = difflib.SequenceMatcher(None, old_references, new_references, autojunk=False)
    return sum(
        new_end - new_start
        for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes()
        if tag in {"insert", "replace"}
    )


def is_title_line(line: str) -> bool:
    return bool(re.match(r"^\s{0,3}#{1,6}\s+", line) or re.match(r"^\s*=+\s+\S", line))


def is_list_line(line: str) -> bool:
    return bool(re.match(r"^\s*([-+*]|\d+[.)])\s+", line))


def split_unescaped_pipes(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            continue
        if char == "|":
            cells.append("".join(current))
            current.clear()
            continue
        current.append(char)

    cells.append("".join(current))
    return cells


def table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []

    cells = split_unescaped_pipes(stripped)
    if stripped.startswith("|"):
        cells = cells[1:]
    if stripped.endswith("|") and cells:
        cells = cells[:-1]

    cells = [strip_markdown_markup(cell).strip() for cell in cells]
    if len(cells) < 2:
        return []
    return cells


def is_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def split_sentence_text(text: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for match in SENTENCE_END_RE.finditer(text):
        end = match.end()
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end

    tail = text[start:].strip()
    if tail and COUNTED_CHAR_RE.search(tail):
        sentences.append(tail)
    return sentences


def split_table_sentences(cells: list[str]) -> list[str]:
    if is_table_separator(cells):
        return []

    sentences: list[str] = []
    for cell in cells:
        if not COUNTED_CHAR_RE.search(cell):
            continue
        if SENTENCE_END_RE.search(cell):
            sentences.extend(split_sentence_text(cell))
        else:
            sentences.append(cell)
    return sentences


def split_sentences(text: str) -> list[str]:
    normalized_lines = normalize_document(text)
    sentences: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        merged = " ".join(paragraph)
        sentences.extend(split_sentence_text(merged))
        paragraph.clear()

    for line in normalized_lines:
        if not line.text:
            flush_paragraph()
            continue

        if line.is_code:
            flush_paragraph()
            sentences.append(line.text)
            continue

        cells = table_cells(line.raw)
        if cells:
            flush_paragraph()
            sentences.extend(split_table_sentences(cells))
            continue

        if is_title_line(line.raw):
            flush_paragraph()
            if COUNTED_CHAR_RE.search(line.text):
                sentences.append(line.text)
            continue

        if is_list_line(line.raw):
            flush_paragraph()
            sentences.extend(split_sentence_text(line.text))
            continue

        paragraph.append(line.text)

    flush_paragraph()
    return sentences


def count_chinese_chars(sentence: str) -> int:
    sentence = strip_non_counted_tokens(sentence)
    return len(COUNTED_CHAR_RE.findall(sentence))


def diff_sentences(old_sentences: list[str], new_sentences: list[str], path: str) -> list[SentenceChange]:
    matcher = difflib.SequenceMatcher(None, old_sentences, new_sentences, autojunk=False)
    changes: list[SentenceChange] = []

    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"equal", "delete"}:
            continue
        for sentence in new_sentences[new_start:new_end]:
            count = count_chinese_chars(sentence)
            if count:
                changes.append(SentenceChange(tag=tag, path=path, sentence=sentence, count=count))

    return changes


def count_deleted_lines(old_text: str, new_text: str) -> int:
    matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(), autojunk=False)
    return sum(
        old_end - old_start
        for tag, old_start, old_end, _new_start, _new_end in matcher.get_opcodes()
        if tag in {"delete", "replace"}
    )


def count_file(repo_root: Path, old_ref: str, new_ref: str, file_change: FileChange) -> FileResult:
    if is_image_path(file_change.new_path) or (
        file_change.old_path is not None and is_image_path(file_change.old_path)
    ):
        added_chars = (
            IMAGE_FILE_CHAR_COUNT
            if file_change.status != "D" and not file_change.status.startswith("R")
            else 0
        )
        return FileResult(
            status=file_change.status,
            old_path=file_change.old_path,
            new_path=file_change.new_path,
            display_path=file_change.display_path,
            changed_sentences=0,
            added_chars=added_chars,
            deleted_lines=0,
            sentence_changes=[],
        )

    old_text = read_git_file(repo_root, old_ref, file_change.old_path) if file_change.old_path else ""
    new_text = read_git_file(repo_root, new_ref, file_change.new_path)
    changes = diff_sentences(
        split_sentences(old_text),
        split_sentences(new_text),
        file_change.display_path,
    )
    return FileResult(
        status=file_change.status,
        old_path=file_change.old_path,
        new_path=file_change.new_path,
        display_path=file_change.display_path,
        changed_sentences=len(changes),
        added_chars=sum(change.count for change in changes),
        deleted_lines=count_deleted_lines(old_text, new_text),
        sentence_changes=changes,
        added_images=count_added_image_references(old_text, new_text),
    )


def calculate(
    repo_root: Path,
    old_ref: GitRef,
    new_ref: GitRef,
    *,
    prefetch_missing: bool = False,
) -> list[FileResult]:
    results: list[FileResult] = []
    changes = list_changed_files(repo_root, old_ref.sha, new_ref.sha)
    if prefetch_missing:
        prefetch_changed_file_blobs(repo_root, old_ref.sha, new_ref.sha, changes)
    for change in changes:
        result = count_file(repo_root, old_ref.sha, new_ref.sha, change)
        if result.added_chars or result.added_images or result.deleted_lines or change.status.startswith("R"):
            results.append(result)
    return results


def count_all_documents(repo_root: Path) -> list[tuple[str, int, int]]:
    results: list[tuple[str, int, int]] = []
    for relative_path in list_all_documents(repo_root):
        count, images = count_document_stats(repo_root / relative_path)
        if count or images:
            results.append((relative_path, count, images))
    return results


def count_document_chars(path: Path) -> int:
    return count_document_stats(path)[0]


def count_document_stats(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return (
        sum(count_chinese_chars(sentence) for sentence in split_sentences(text)),
        len(extract_image_references(text)),
    )


def count_requested_documents(paths: list[Path]) -> list[tuple[str, int | None, int | None]]:
    results: list[tuple[str, int | None, int | None]] = []
    for requested_path in paths:
        path = requested_path.expanduser()
        display_path = str(path)
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved_path = path.resolve()
        if resolved_path.is_dir():
            documents = sorted(
                candidate
                for candidate in resolved_path.rglob("*")
                if candidate.is_file()
                and is_markdown_path(str(candidate))
                and not is_excluded_path(str(candidate))
            )
            for document in documents:
                relative_path = document.relative_to(resolved_path)
                document_display_path = str(Path(display_path) / relative_path)
                count, images = count_document_stats(document)
                results.append((document_display_path, count, images))
            continue
        if not resolved_path.is_file():
            results.append((display_path, None, None))
            continue
        count, images = count_document_stats(resolved_path)
        results.append((display_path, count, images))
    return results
