"""Shared constants and data models for the Chinese refresh counter."""

from __future__ import annotations

import re
from dataclasses import dataclass


COUNTED_CHAR_RE = re.compile(
    "["
    "\u2e80-\u2eff"
    "\u2f00-\u2fdf"
    "\u2ff0-\u2fff"
    "\u3000-\u303f"
    "\u3100-\u312f"
    "\u31a0-\u31bf"
    "\u31c0-\u31ef"
    "\u3200-\u32ff"
    "\u3300-\u33ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\ufe30-\ufe4f"
    "\U00020000-\U0002a6df"
    "\U0002a700-\U0002b73f"
    "\U0002b740-\U0002b81f"
    "\U0002b820-\U0002ceaf"
    "\U0002ceb0-\U0002ebef"
    "\U0002ebf0-\U0002ee5f"
    "\U0002f800-\U0002fa1f"
    "\U00030000-\U0003134f"
    "\U00031350-\U000323af"
    "\u2600-\u26ff"
    "\u2700-\u27bf"
    "]"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+")
MARKDOWN_IMAGE_RE = re.compile(r"(?<!\\)!\[([^\]]*)\]\(([^)\n]*)\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
SENTENCE_END_RE = re.compile(r"(……|[。！？])")
REF_DATE_FORMAT = "%Y/%m/%d %H:%M:%S"
ARCHIVE_DATE_FORMAT = "%Y%m%d-%H%M%S"
FILENAME_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"
SUMMARY_OUTPUT_FILENAME_PREFIX = "zh_refresh_workload_summary"
SUMMARY_CSV_FIELDS = ["文件", "修改句数", "新增字数", "新增图片数", "删除行数", "状态"]
IMAGE_FILE_CHAR_COUNT = 200
ANSI_RESET = "\033[0m"
ANSI_STYLES = {
    "label": "\033[1;36m",
    "repo": "\033[1;37m",
    "ref": "\033[36m",
    "path": "\033[32m",
    "number": "\033[1;33m",
    "total": "\033[1;35m",
    "archive": "\033[90m",
}


@dataclass(frozen=True)
class GitRef:
    sha: str
    committed_at: str
    message: str

    @property
    def short_sha(self) -> str:
        return self.sha[:12]

    def label(self, width: int = 92) -> str:
        message_width = max(18, width - len(self.committed_at) - len(self.short_sha) - 8)
        message = self.message.replace("\t", " ").strip() or "(no message)"
        if len(message) > message_width:
            message = message[: message_width - 1] + "…"
        return f"{self.committed_at} - {message} - {self.short_sha}"


@dataclass(frozen=True)
class GitBranch:
    name: str
    committed_at: str
    sha: str

    @property
    def short_sha(self) -> str:
        return self.sha[:12]

    def label(self) -> str:
        return f"{self.committed_at} - {self.name} - {self.short_sha}"


@dataclass(frozen=True)
class SentenceChange:
    tag: str
    path: str
    sentence: str
    count: int


@dataclass(frozen=True)
class FileChange:
    status: str
    old_path: str | None
    new_path: str

    @property
    def display_path(self) -> str:
        if self.old_path and self.old_path != self.new_path:
            return f"{self.old_path} -> {self.new_path}"
        return self.new_path


@dataclass(frozen=True)
class TextLine:
    raw: str
    text: str
    is_code: bool = False


@dataclass(frozen=True)
class FileResult:
    status: str
    old_path: str | None
    new_path: str
    display_path: str
    changed_sentences: int
    added_chars: int
    deleted_lines: int
    sentence_changes: list[SentenceChange]
    added_images: int = 0

@dataclass(frozen=True)
class AbsentEnglishResult:
    chinese_path: str
    english_path: str | None
    status: str
    chinese_chars: int


@dataclass(frozen=True)
class RunResult:
    run_id: str
    generated_at: str
    repo_name: str
    repo_root: str
    old_ref: GitRef
    new_ref: GitRef
    files: list[FileResult]

    @property
    def total_changed_sentences(self) -> int:
        return sum(file.changed_sentences for file in self.files)

    @property
    def total_added_chars(self) -> int:
        return sum(file.added_chars for file in self.files)

    @property
    def total_deleted_lines(self) -> int:
        return sum(file.deleted_lines for file in self.files)

    @property
    def total_added_images(self) -> int:
        return sum(file.added_images for file in self.files)
