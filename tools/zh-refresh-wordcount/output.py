"""Configuration, archive, and terminal output helpers."""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import os
import sys
from pathlib import Path

from models import (
    ANSI_RESET,
    ANSI_STYLES,
    ARCHIVE_DATE_FORMAT,
    FILENAME_TIMESTAMP_FORMAT,
    REF_DATE_FORMAT,
    SUMMARY_CSV_FIELDS,
    FileResult,
    GitRef,
    RunResult,
    SentenceChange,
    AbsentEnglishResult,
)


def default_config_root() -> Path:
    return Path.home() / ".config" / "zh-refresh-wordcount"


def load_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config_path: Path, config: dict[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def archive_paths(repo_dir: Path, old_ref: GitRef, new_ref: GitRef, generated_at: dt.datetime) -> tuple[Path, Path]:
    archives_dir = repo_dir / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True)
    base = f"{generated_at.strftime(ARCHIVE_DATE_FORMAT)}_{old_ref.short_sha}_{new_ref.short_sha}"
    return archives_dir / f"{base}.csv", archives_dir / f"{base}.json"


def write_csv(path: Path, result: RunResult) -> None:
    fields = [
        "run_id",
        "generated_at",
        "repo_name",
        "repo_root",
        "old_ref",
        "old_sha",
        "old_time",
        "old_message",
        "new_ref",
        "new_sha",
        "new_time",
        "new_message",
        "old_path",
        "new_path",
        "display_path",
        "changed_sentences",
        "added_chars",
        "added_images",
        "deleted_lines",
        "status",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for file in result.files:
            writer.writerow(
                {
                    "run_id": result.run_id,
                    "generated_at": result.generated_at,
                    "repo_name": result.repo_name,
                    "repo_root": result.repo_root,
                    "old_ref": result.old_ref.short_sha,
                    "old_sha": result.old_ref.sha,
                    "old_time": result.old_ref.committed_at,
                    "old_message": result.old_ref.message,
                    "new_ref": result.new_ref.short_sha,
                    "new_sha": result.new_ref.sha,
                    "new_time": result.new_ref.committed_at,
                    "new_message": result.new_ref.message,
                    "old_path": file.old_path or "",
                    "new_path": file.new_path,
                    "display_path": file.display_path,
                    "changed_sentences": file.changed_sentences,
                    "added_chars": file.added_chars,
                    "added_images": file.added_images,
                    "deleted_lines": file.deleted_lines,
                    "status": file_status_label(file),
                }
            )


def write_json(path: Path, result: RunResult) -> None:
    payload = {
        "run_id": result.run_id,
        "generated_at": result.generated_at,
        "repo": {
            "name": result.repo_name,
            "root": result.repo_root,
        },
        "old_ref": dataclasses.asdict(result.old_ref),
        "new_ref": dataclasses.asdict(result.new_ref),
        "totals": {
            "changed_files": len(result.files),
            "changed_sentences": result.total_changed_sentences,
            "added_chars": result.total_added_chars,
            "added_images": result.total_added_images,
            "deleted_lines": result.total_deleted_lines,
        },
        "files": [dataclasses.asdict(file) for file in result.files],
    }
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


def should_use_color() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    enable_windows_ansi()
    return True


def colorize(text: object, style: str, use_color: bool) -> str:
    value = str(text)
    if not use_color:
        return value
    return f"{ANSI_STYLES[style]}{value}{ANSI_RESET}"


def format_ref_summary(ref: GitRef, use_color: bool) -> str:
    return (
        f"{colorize(ref.committed_at, 'ref', use_color)} - "
        f"{ref.message} - "
        f"{colorize(ref.short_sha, 'number', use_color)}"
    )


def file_status_label(file: FileResult) -> str:
    if file.status == "A":
        return "[新增文件]"
    if file.status == "D":
        return "[删除文件]"
    if file.status.startswith("R"):
        return "[路径更改]"
    return ""


def format_file_result(file: FileResult, use_color: bool) -> str:
    display_path = file.display_path
    if status_label := file_status_label(file):
        display_path += f" {status_label}"
    line = (
        f"{colorize(display_path, 'path', use_color)} - 修改 "
        f"{colorize(file.changed_sentences, 'number', use_color)} 句, 新增 "
        f"{colorize(file.added_chars, 'number', use_color)} 字, 新增图片 "
        f"{colorize(file.added_images, 'number', use_color)} 张"
    )
    if file.deleted_lines:
        line += f", 删除 {colorize(file.deleted_lines, 'number', use_color)} 行"
    return line


def format_sentence_change(change: SentenceChange, index: int, use_color: bool) -> str:
    return (
        f"{colorize(index, 'number', use_color)}. "
        f"{colorize(change.path, 'path', use_color)} - {change.tag}, "
        f"{colorize(change.count, 'number', use_color)} 字: {change.sentence}"
    )


def render_sentence_details(result: RunResult, use_color: bool) -> list[str]:
    lines = [f"{colorize('新增工作量句子:', 'label', use_color)}"]
    index = 1
    for file in result.files:
        for change in file.sentence_changes:
            lines.append(format_sentence_change(change, index, use_color))
            index += 1
    if index == 1:
        lines.append("无新增工作量句子。")
    return lines


def render_summary(result: RunResult, use_color: bool, summary_path: Path | None = None) -> list[str]:
    lines = [
        "-" * 72,
        f"{colorize('当前总结:', 'label', use_color)}",
    ]
    if summary_path is not None:
        lines.append(
            f"{colorize('工作量总结已保存到:', 'label', use_color)} "
            f"{colorize(summary_path, 'archive', use_color)}"
        )
    lines.extend(
        [
            f"{colorize('仓库:', 'label', use_color)} {colorize(result.repo_name, 'repo', use_color)}",
            f"{colorize('Old:', 'label', use_color)} {format_ref_summary(result.old_ref, use_color)}",
            f"{colorize('New:', 'label', use_color)} {format_ref_summary(result.new_ref, use_color)}",
            "",
        ]
    )

    if not result.files:
        lines.append(
            f"{colorize('TOTAL', 'total', use_color)} - 修改 "
            f"{colorize(0, 'number', use_color)} 句, 新增 {colorize(0, 'number', use_color)} 字, "
            f"新增图片 {colorize(0, 'number', use_color)} 张"
        )
    else:
        for file in result.files:
            lines.append(format_file_result(file, use_color))
        lines.append(
            f"{colorize('TOTAL', 'total', use_color)} - 修改 "
            f"{colorize(result.total_changed_sentences, 'number', use_color)} 句, 新增 "
            f"{colorize(result.total_added_chars, 'number', use_color)} 字, 新增图片 "
            f"{colorize(result.total_added_images, 'number', use_color)} 张, 删除 "
            f"{colorize(result.total_deleted_lines, 'number', use_color)} 行"
        )
    return lines


def render_archive_paths(csv_path: Path, json_path: Path, use_color: bool) -> list[str]:
    return [
        f"{colorize('CSV:', 'label', use_color)}  {colorize(csv_path, 'archive', use_color)}",
        f"{colorize('JSON:', 'label', use_color)} {colorize(json_path, 'archive', use_color)}",
    ]


def render_result(
    result: RunResult,
    csv_path: Path,
    json_path: Path,
    *,
    use_color: bool,
    include_archives: bool,
    summary_path: Path | None = None,
) -> str:
    lines = render_sentence_details(result, use_color)
    if include_archives:
        lines.extend(["", *render_archive_paths(csv_path, json_path, use_color)])
    lines.extend(["", *render_summary(result, use_color, summary_path)])
    return "\n".join(lines)


def print_result(result: RunResult, csv_path: Path, json_path: Path, summary_path: Path) -> None:
    use_color = should_use_color()
    print()
    print(
        render_result(
            result,
            csv_path,
            json_path,
            use_color=use_color,
            include_archives=True,
            summary_path=summary_path,
        )
    )


def render_all_result(
    repo_root: Path | None,
    files: list[tuple[str, int | None, int | None]],
    use_color: bool,
) -> str:
    lines: list[str] = []
    if repo_root is not None:
        lines.append(f"{colorize('仓库:', 'label', use_color)} {colorize(repo_root.name, 'repo', use_color)}")
    lines.append(f"{colorize('中文文档字数和图片数:', 'label', use_color)}")
    missing_paths: list[str] = []
    for path, count, images in files:
        if count is None:
            lines.append(f"{colorize(path, 'path', use_color)} - {colorize('不存在', 'number', use_color)}")
            missing_paths.append(path)
            continue
        lines.append(
            f"{colorize(path, 'path', use_color)} - {colorize(count, 'number', use_color)} 字, "
            f"{colorize(images, 'number', use_color)} 张图片"
        )
    total = sum(count for _path, count, _images in files if count is not None)
    total_images = sum(images for _path, _count, images in files if images is not None)
    lines.append(
        f"{colorize('TOTAL', 'total', use_color)} - {colorize(total, 'number', use_color)} 字, "
        f"{colorize(total_images, 'number', use_color)} 张图片"
    )
    if missing_paths:
        lines.append(f"{colorize('不存在文件:', 'label', use_color)}")
        for path in missing_paths:
            lines.append(f"- {colorize(path, 'path', use_color)}")
    return "\n".join(lines)


def print_all_result(
    repo_root: Path | None,
    files: list[tuple[str, int | None, int | None]],
    summary_path: Path | None = None,
) -> None:
    print(render_all_result(repo_root, files, should_use_color()))
    if summary_path is not None:
        print(f"工作量总结已保存到: {summary_path}")


def write_all_result_csv(handle, files: list[tuple[str, int | None, int | None]]) -> None:
    writer = csv.writer(handle, lineterminator="\n")
    writer.writerow(["文件", "中文字数", "图片数"])
    total = 0
    total_images = 0
    for path, count, images in files:
        writer.writerow([path, "不存在" if count is None else count, "不存在" if images is None else images])
        if count is not None:
            total += count
        if images is not None:
            total_images += images
    writer.writerow(["TOTAL", total, total_images])


def print_all_result_csv(
    files: list[tuple[str, int | None, int | None]],
    summary_path: Path | None = None,
) -> None:
    write_all_result_csv(sys.stdout, files)
    if summary_path is not None:
        print(f"工作量总结已保存到: {summary_path}", file=sys.stderr)


def write_all_summary_output(
    path: Path,
    repo_root: Path,
    files: list[tuple[str, int | None, int | None]],
    csv_output: bool = False,
) -> None:
    if csv_output:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            write_all_result_csv(handle, files)
        return
    output = render_all_result(repo_root, files, use_color=False)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(output)
        handle.write("\n")


def write_summary_csv(path: Path, result: RunResult) -> None:
    fields = SUMMARY_CSV_FIELDS
    if hasattr(result, "old_ref") and hasattr(result, "new_ref"):
        fields = [
            f"{SUMMARY_CSV_FIELDS[0]} ({result.old_ref.sha[:7]} - {result.new_ref.sha[:7]})",
            *SUMMARY_CSV_FIELDS[1:],
        ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fields)
        for file in result.files:
            writer.writerow(
                [
                    file.display_path,
                    file.changed_sentences,
                    file.added_chars,
                    file.added_images,
                    file.deleted_lines,
                    file_status_label(file),
                ]
            )
        writer.writerow(
            [
                "TOTAL",
                result.total_changed_sentences,
                result.total_added_chars,
                result.total_added_images,
                result.total_deleted_lines,
                "",
            ]
        )


def _filename_timestamp(committed_at: str) -> str:
    return dt.datetime.strptime(committed_at, REF_DATE_FORMAT).strftime(FILENAME_TIMESTAMP_FORMAT)


def summary_output_path(
    workdir: Path,
    repo_name: str,
    mode: str,
    *,
    old_timestamp: str | None = None,
    new_timestamp: str | None = None,
    last_commit_timestamp: str | None = None,
    csv_output: bool = False,
) -> Path:
    if mode == "absent_en":
        base = f"{repo_name}_absent_en_{dt.datetime.now().strftime(FILENAME_TIMESTAMP_FORMAT)}"
    elif mode == "compare":
        if old_timestamp is None or new_timestamp is None:
            raise ValueError("比较模式需要 old 和 new commit 时间")
        base = f"{repo_name}_compare_{_filename_timestamp(old_timestamp)}-{_filename_timestamp(new_timestamp)}"
    elif mode == "full":
        if last_commit_timestamp is None:
            raise ValueError("全量模式需要最后一个 commit 时间")
        base = f"{repo_name}_full_{_filename_timestamp(last_commit_timestamp)}"
    else:
        raise ValueError(f"不支持的统计模式: {mode}")
    extension = ".csv" if csv_output else ".txt"
    return workdir / f"{base}{extension}"


def write_absent_en_csv(
    path: Path,
    files: list[AbsentEnglishResult],
    target_ref: GitRef | None = None,
    baseline_ref: GitRef | None = None,
) -> None:
    fields = ["中文文档", "英文文档", "状态", "中文缺失/变更字数"]
    if target_ref is not None:
        baseline_sha = baseline_ref.sha[:7] if baseline_ref is not None else "自身英文缺失统计"
        fields[0] = f"中文文档 ({target_ref.sha[:7]} - {baseline_sha})"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fields)
        for file in files:
            writer.writerow([file.chinese_path, file.english_path or "", file.status, file.chinese_chars])
        writer.writerow(["TOTAL", "", "", sum(file.chinese_chars for file in files)])


def render_absent_en(files: list[AbsentEnglishResult]) -> str:
    lines = ["英文文档缺失统计:"]
    for file in files:
        english = file.english_path or "不存在"
        lines.append(
            f"{file.chinese_path} - 英文文档: {english}, {file.status}, {file.chinese_chars} 字"
        )
    lines.append(f"TOTAL - {sum(file.chinese_chars for file in files)} 字")
    return "\n".join(lines)


def write_absent_en_output(
    path: Path,
    files: list[AbsentEnglishResult],
    csv_output: bool = False,
    target_ref: GitRef | None = None,
    baseline_ref: GitRef | None = None,
) -> None:
    if csv_output:
        write_absent_en_csv(path, files, target_ref, baseline_ref)
        return
    path.write_text(render_absent_en(files) + "\n", encoding="utf-8", newline="\n")


def print_absent_en_result(files: list[AbsentEnglishResult], output_path: Path) -> None:
    print(render_absent_en(files))
    print(f"缺失统计结果已保存到: {output_path}")


def write_summary_output(path: Path, result: RunResult, csv_output: bool = False) -> None:
    if csv_output:
        write_summary_csv(path, result)
        return
    output = "\n".join(render_summary(result, use_color=False))
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(output)
        handle.write("\n")
