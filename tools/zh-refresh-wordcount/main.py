"""CLI entry point and interactive selectors."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import os
import re
import sys
import time
import unicodedata
import uuid
from pathlib import Path

from absent_en import find_absent_english
from counter import calculate, count_all_documents, count_requested_documents
from git_utils import (
    cleanup_network_cache,
    copy_to_clipboard,
    confirm_pull_current_branch,
    current_branch,
    display_git_url,
    fetch_current_branch,
    find_git_root,
    git_activity_reporter,
    git_url_override,
    list_origin_branches,
    list_local_branches,
    list_refs,
    network_cache_path,
    prepare_cached_repository,
    pull_current_branch,
    resolve_ref,
    repository_name_from_url,
    switch_branch,
)
from models import GitBranch, GitRef, RunResult
from output import (
    archive_paths,
    default_config_root,
    load_config,
    print_all_result,
    print_all_result_csv,
    print_result,
    save_config,
    summary_output_path,
    write_all_summary_output,
    write_csv,
    write_json,
    write_summary_output,
    print_absent_en_result,
    write_absent_en_output,
)


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "用法:", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法:", 1)


def run_numbered_git_url_picker() -> str | None:
    while True:
        git_url = input("当前目录不是 Git 仓库，请输入 Git URL（q 取消）: ").strip()
        if git_url.lower() == "q":
            return None
        if git_url:
            return git_url
        print("Git URL 不能为空。")


def run_git_url_picker() -> str | None:
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame, TextArea
    except ImportError:
        return run_numbered_git_url_picker()

    url_input = TextArea(
        height=1,
        multiline=False,
        focus_on_click=True,
        wrap_lines=False,
        style="class:input",
    )
    error_message = ""
    result: dict[str, str] = {}

    def submit(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal error_message
        git_url = url_input.text.strip()
        if not git_url:
            error_message = "Git URL 不能为空"
            app.invalidate()
            return
        result["git_url"] = git_url
        event.app.exit()

    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        submit(event)

    @bindings.add("escape")
    @bindings.add("c-c")
    def _quit(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit()

    root = HSplit(
        [
            Window(
                FormattedTextControl(
                    HTML("<b>远端 Git 仓库</b>：当前目录不是 Git 仓库，请输入 HTTPS、SSH 或本地 Git URL。")
                ),
                height=1,
            ),
            Frame(url_input, title="Git URL", height=3),
            Window(
                FormattedTextControl(lambda: [("class:error", error_message)]),
                height=1,
            ),
            Window(
                FormattedTextControl(
                    HTML(
                        "<ansiyellow><b>Enter</b></ansiyellow><ansicyan> 获取远端仓库  </ansicyan>"
                        "<ansiyellow><b>Esc/Ctrl-C</b></ansiyellow><ansicyan> 取消</ansicyan>"
                    )
                ),
                height=1,
            ),
        ]
    )
    app = Application(
        layout=Layout(root, focused_element=url_input),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict({"input": "bg:default", "error": "ansired bold"}),
    )
    app.run()
    return result.get("git_url")


def clone_remote_repository_with_progress(
    git_url: str,
    repo_root: Path,
    use_tui: bool,
) -> tuple[str, str | None]:
    safe_git_url = display_git_url(git_url)
    if not use_tui:
        print(f"正在初始化或更新远端仓库缓存: {safe_git_url}")
        return prepare_cached_repository(git_url, repo_root)
    try:
        import asyncio

        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        print(f"正在初始化或更新远端仓库缓存: {safe_git_url}")
        return prepare_cached_repository(git_url, repo_root)

    spinner = "|"
    status_control = FormattedTextControl(
        lambda: HTML(
            f"<ansicyan><b>{spinner}</b></ansicyan> 正在初始化或更新远端仓库缓存，请稍候……"
        )
    )
    root = HSplit(
        [
            Window(FormattedTextControl(HTML("<b>远端 Git 仓库</b>")), height=1),
            Frame(
                HSplit(
                    [
                        Window(status_control, height=1),
                        Window(FormattedTextControl(safe_git_url), height=1),
                    ]
                ),
                title="网络缓存",
                height=5,
            ),
            Window(
                FormattedTextControl("使用持久裸仓库缓存，不会 checkout；完成后将自动进入主菜单。"),
                height=1,
            ),
        ]
    )
    app = Application(
        layout=Layout(root),
        full_screen=True,
        mouse_support=False,
        style=Style.from_dict({"frame.label": "bold"}),
    )

    async def clone_task() -> None:
        nonlocal spinner
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, prepare_cached_repository, git_url, repo_root)
        frames = "|/-\\"
        index = 0
        while not future.done():
            spinner = frames[index % len(frames)]
            index += 1
            app.invalidate()
            await asyncio.sleep(0.1)
        try:
            repository_status = await future
        except Exception as exc:
            app.exit(exception=exc)
            return
        app.exit(result=repository_status)

    def start_clone() -> None:
        app.create_background_task(clone_task())

    return app.run(pre_run=start_clone)


def show_network_cache_warning(message: str, use_tui: bool) -> None:
    if not use_tui:
        print(f"warning: {message}", file=sys.stderr)
        return
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        print(f"warning: {message}", file=sys.stderr)
        input("按 Enter 使用现有缓存继续...")
        return

    bindings = KeyBindings()

    @bindings.add("enter")
    @bindings.add("escape")
    @bindings.add("c-c")
    def _continue(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit()

    root = HSplit(
        [
            Window(FormattedTextControl(HTML("<b>网络缓存警告</b>")), height=1),
            Frame(
                Window(FormattedTextControl(message), wrap_lines=True),
                title="远端更新失败",
            ),
            Window(FormattedTextControl("按 Enter 使用可能过期的缓存继续。"), height=1),
        ]
    )
    Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=True,
        style=Style.from_dict({"frame.label": "bold ansiyellow"}),
    ).run()


def calculate_with_progress(
    repo_root: Path,
    old_ref: GitRef,
    new_ref: GitRef,
    use_tui: bool,
    network_mode: bool = False,
) -> list:
    if not use_tui:
        print(f"正在统计 {old_ref.short_sha}..{new_ref.short_sha} 的中文文档刷新工作量...")
        return calculate(repo_root, old_ref, new_ref, prefetch_missing=network_mode)
    try:
        import asyncio

        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        print(f"正在统计 {old_ref.short_sha}..{new_ref.short_sha} 的中文文档刷新工作量...")
        return calculate(repo_root, old_ref, new_ref, prefetch_missing=network_mode)

    spinner = "|"
    network_active = False
    network_started_at = 0.0
    network_received = 0
    network_messages: list[str] = []

    def pack_storage_size() -> int:
        pack_dir = repo_root / "objects" / "pack"
        try:
            return sum(entry.stat().st_size for entry in pack_dir.iterdir() if entry.is_file())
        except OSError:
            return 0

    def format_download_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KiB"
        return f"{size / (1024 * 1024):.1f} MiB"

    def status_text() -> HTML:
        if network_active:
            elapsed = max(0, int(time.monotonic() - network_started_at))
            received = (
                f"，已接收 {format_download_size(network_received)}"
                if network_received
                else ""
            )
            lines = [
                f"<ansicyan><b>{spinner}</b></ansicyan> "
                f"正在通过网络获取 Git 对象（已等待 {elapsed} 秒{received}）"
            ]
            if network_messages:
                lines.extend(f"  {html.escape(message[:180])}" for message in network_messages[-5:])
            else:
                lines.append("  等待 Git 返回传输进度……")
            return HTML("\n".join(lines))
        return HTML(
            f"<ansicyan><b>{spinner}</b></ansicyan> 正在分析 ref 差异并统计中文文档，请稍候……"
        )

    status_control = FormattedTextControl(status_text)
    root = HSplit(
        [
            Window(FormattedTextControl(HTML("<b>统计中文文档刷新工作量</b>")), height=1),
            Frame(
                HSplit(
                    [
                        Window(status_control, height=6, wrap_lines=False),
                        Window(
                            FormattedTextControl(
                                f"old ref: {old_ref.short_sha}    new ref: {new_ref.short_sha}"
                            ),
                            height=1,
                        ),
                    ]
                ),
                title="处理中",
                height=10,
            ),
            Window(FormattedTextControl("统计完成后将自动显示结果。"), height=1),
        ]
    )
    app = Application(
        layout=Layout(root),
        full_screen=True,
        mouse_support=False,
        style=Style.from_dict({"frame.label": "bold"}),
    )

    async def calculate_task() -> None:
        nonlocal spinner, network_active, network_started_at, network_received
        loop = asyncio.get_running_loop()

        def report_network_activity(message: str) -> None:
            nonlocal network_active, network_started_at
            if message:
                if not network_active:
                    network_active = True
                    network_started_at = time.monotonic()
                network_messages.append(message)
                del network_messages[:-5]
            else:
                network_active = False
            app.invalidate()

        def run_calculation() -> list:
            if network_mode:
                with git_activity_reporter(report_network_activity):
                    return calculate(repo_root, old_ref, new_ref, prefetch_missing=True)
            return calculate(repo_root, old_ref, new_ref)

        initial_pack_size = pack_storage_size() if network_mode else 0
        last_pack_size = initial_pack_size
        future = loop.run_in_executor(None, run_calculation)
        frames = "|/-\\"
        index = 0
        while not future.done():
            spinner = frames[index % len(frames)]
            index += 1
            if network_mode:
                current_pack_size = pack_storage_size()
                if current_pack_size > initial_pack_size and current_pack_size != last_pack_size:
                    if not network_active:
                        network_active = True
                        network_started_at = time.monotonic()
                    network_received = current_pack_size - initial_pack_size
                last_pack_size = current_pack_size
            app.invalidate()
            await asyncio.sleep(0.1)
        try:
            files = await future
        except Exception as exc:
            app.exit(exception=exc)
            return
        app.exit(result=files)

    def start_calculation() -> None:
        app.create_background_task(calculate_task())

    return app.run(pre_run=start_calculation)


def find_absent_en_with_progress(
    repo_root: Path,
    target_ref: GitRef,
    baseline_ref: GitRef | None,
    *,
    use_tui: bool,
    network_mode: bool,
) -> list:
    def run_scan(report=None) -> list:
        if network_mode and report is not None:
            with git_activity_reporter(report):
                return find_absent_english(
                    repo_root,
                    target_ref.sha,
                    baseline_ref.sha if baseline_ref is not None else None,
                )
        return find_absent_english(
            repo_root,
            target_ref.sha,
            baseline_ref.sha if baseline_ref is not None else None,
        )

    if not use_tui:
        print("正在分析英文文档缺失情况...")
        return run_scan()
    try:
        import asyncio

        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        print("正在分析英文文档缺失情况...")
        return run_scan()

    spinner = "|"
    network_active = False
    network_started_at = 0.0
    network_messages: list[str] = []

    def status_text() -> HTML:
        if network_active:
            elapsed = max(0, int(time.monotonic() - network_started_at))
            lines = [
                f"<ansicyan><b>{spinner}</b></ansicyan> 正在通过网络获取 Git 对象（已等待 {elapsed} 秒）"
            ]
            lines.extend(f"  {html.escape(message[:180])}" for message in network_messages[-5:])
            return HTML("\n".join(lines))
        return HTML(f"<ansicyan><b>{spinner}</b></ansicyan> 正在分析英文文档缺失情况，请稍候……")

    app = Application(
        layout=Layout(
            HSplit(
                [
                    Window(FormattedTextControl(HTML("<b>统计英文文档缺失</b>")), height=1),
                    Frame(
                        HSplit(
                            [
                                Window(FormattedTextControl(status_text), height=6),
                                Window(
                                    FormattedTextControl(
                                        f"目标 ref: {target_ref.short_sha}    基线 ref: "
                                        f"{baseline_ref.short_sha if baseline_ref is not None else '未选择'}"
                                    ),
                                    height=1,
                                ),
                            ]
                        ),
                        title="处理中",
                        height=10,
                    ),
                    Window(FormattedTextControl("统计完成后将自动显示结果。"), height=1),
                ]
            )
        ),
        full_screen=True,
        mouse_support=False,
        style=Style.from_dict({"frame.label": "bold"}),
    )

    async def scan_task() -> None:
        nonlocal spinner, network_active, network_started_at
        loop = asyncio.get_running_loop()

        def report_network_activity(message: str) -> None:
            nonlocal network_active, network_started_at
            if message:
                if not network_active:
                    network_active = True
                    network_started_at = time.monotonic()
                network_messages.append(message)
                del network_messages[:-5]
            else:
                network_active = False
            app.invalidate()

        future = loop.run_in_executor(
            None,
            lambda: run_scan(report_network_activity if network_mode else None),
        )
        frames = "|/-\\"
        index = 0
        while not future.done():
            spinner = frames[index % len(frames)]
            index += 1
            app.invalidate()
            await asyncio.sleep(0.1)
        try:
            files = await future
        except Exception as exc:
            app.exit(exception=exc)
            return
        app.exit(result=files)

    def start_scan() -> None:
        app.create_background_task(scan_task())

    return app.run(pre_run=start_scan)


def short_cached_ref(value: str) -> str:
    return value[:12] if value else "未设置"


def cached_ref_hint(cached_old_ref: str, cached_new_ref: str) -> str:
    if cached_old_ref and cached_new_ref:
        return "提示：选择“手动输入”选项进入比较"
    return ""


def run_numbered_mode_picker(
    initial_csv: bool = True,
    cached_old_ref: str = "",
    cached_new_ref: str = "",
    allow_all: bool = True,
    compare_option: str = "当前分支 ref 比较（默认）",
    allow_absent_en: bool = True,
    allow_switch_branch: bool = False,
) -> tuple[int, bool]:
    options = [
        compare_option,
        "列出各分支 ref",
        "手动输入 old ref 和 new ref",
    ]
    if allow_all:
        options.append("全量统计仓库内中文字数（适用于全新仓库）")
    if allow_absent_en:
        options.append("英文文档缺失统计")
    if allow_switch_branch:
        options.append("切换本地分支")

    print("主菜单：")
    cache_line = f"ref 缓存：old={short_cached_ref(cached_old_ref)}  new={short_cached_ref(cached_new_ref)}"
    hint = cached_ref_hint(cached_old_ref, cached_new_ref)
    if hint:
        cache_line += f"    {hint}"
    print(cache_line)
    for index, option in enumerate(options, start=1):
        print(f"{index}. {option}")
    while True:
        selected = input("请输入选项 [1]: ").strip()
        if not selected:
            return 0, initial_csv
        if selected.isdigit() and 1 <= int(selected) <= len(options):
            return int(selected) - 1, initial_csv
        print("请输入有效的菜单编号。")


def run_mode_picker(
    initial_csv: bool = True,
    cached_old_ref: str = "",
    cached_new_ref: str = "",
    allow_all: bool = True,
    compare_option: str = "当前分支 ref 比较（默认）",
    allow_absent_en: bool = True,
    allow_switch_branch: bool = False,
) -> tuple[int, bool]:
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window, WindowAlign
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        return run_numbered_mode_picker(
            initial_csv,
            cached_old_ref,
            cached_new_ref,
            allow_all,
            compare_option,
            allow_absent_en,
            allow_switch_branch,
        )

    options = [
        compare_option,
        "列出各分支 ref",
        "手动输入 old ref 和 new ref",
    ]
    if allow_all:
        options.append("全量统计仓库内中文字数（适用于全新仓库）")
    if allow_absent_en:
        options.append("英文文档缺失统计")
    if allow_switch_branch:
        options.append("切换本地分支")
    selected_index = 0
    csv_enabled = initial_csv
    result: dict[str, tuple[int, bool]] = {}

    def refresh() -> None:
        app.invalidate()

    def select(index: int) -> None:
        nonlocal selected_index
        selected_index = index
        refresh()

    def toggle_csv() -> None:
        nonlocal csv_enabled
        csv_enabled = not csv_enabled
        refresh()

    def option_rows() -> list[tuple]:
        rows: list[tuple] = []
        for index, option in enumerate(options):
            style = "class:selected" if index == selected_index else ""
            handler = lambda event, index=index: select(index) if event.event_type == MouseEventType.MOUSE_UP else None
            rows.append((style, f"{index + 1}. {option}\n", handler))
        return rows

    mode_control = FormattedTextControl(option_rows)
    key_control = FormattedTextControl(
        HTML(
            f"<ansiyellow><b>{'/'.join(str(index) for index in range(1, len(options) + 1))}</b></ansiyellow><ansicyan> 或 </ansicyan>"
            "<ansiyellow><b>↑/↓</b></ansiyellow><ansicyan> 选择  </ansicyan>"
            "<ansiyellow><b>鼠标点击</b></ansiyellow><ansicyan> 选择  </ansicyan>"
            "<ansiyellow><b>Space</b></ansiyellow><ansicyan> 切换CSV输出  </ansicyan>"
            "<ansiyellow><b>Enter</b></ansiyellow><ansicyan> 确认  </ansicyan>"
            "<ansiyellow><b>q/Esc/Ctrl-C</b></ansiyellow><ansicyan> 退出</ansicyan>"
        )
    )
    csv_status_control = FormattedTextControl(
        lambda: [("class:csv-enabled", "CSV输出已开启")] if csv_enabled else [("", "CSV输出已关闭")]
    )
    cache_status_control = FormattedTextControl(
        lambda: [
            (
                "class:cache",
                f"ref 缓存 - old: {short_cached_ref(cached_old_ref)}  new: {short_cached_ref(cached_new_ref)}",
            )
        ]
    )
    cache_hint_control = FormattedTextControl(
        lambda: [("class:cache-hint", cached_ref_hint(cached_old_ref, cached_new_ref))]
        if cached_ref_hint(cached_old_ref, cached_new_ref)
        else []
    )
    bindings = KeyBindings()

    @bindings.add("1")
    def _select_compare(event) -> None:  # type: ignore[no-untyped-def]
        select(0)

    @bindings.add("2")
    def _select_branch_refs(event) -> None:  # type: ignore[no-untyped-def]
        select(1)

    @bindings.add("3")
    def _select_manual(event) -> None:  # type: ignore[no-untyped-def]
        select(2)

    if allow_all:

        @bindings.add("4")
        def _select_all(event) -> None:  # type: ignore[no-untyped-def]
            select(3)

    if allow_absent_en:

        @bindings.add("5" if allow_all else "4")
        def _select_absent_en(event) -> None:  # type: ignore[no-untyped-def]
            select(4 if allow_all else 3)

    if allow_switch_branch:

        @bindings.add(str(len(options)))
        def _select_switch_branch(event) -> None:  # type: ignore[no-untyped-def]
            select(len(options) - 1)

    @bindings.add("up")
    def _up(event) -> None:  # type: ignore[no-untyped-def]
        select(max(0, selected_index - 1))

    @bindings.add("down")
    def _down(event) -> None:  # type: ignore[no-untyped-def]
        select(min(len(options) - 1, selected_index + 1))

    @bindings.add(" ")
    def _toggle_csv(event) -> None:  # type: ignore[no-untyped-def]
        toggle_csv()

    @bindings.add("enter")
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        result["selection"] = (selected_index, csv_enabled)
        event.app.exit()

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("q")
    def _quit(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(exception=KeyboardInterrupt)

    root = HSplit(
        [
            Window(
                FormattedTextControl(HTML("<b>主菜单</b>")),
                height=1,
            ),
            Frame(Window(mode_control, height=len(options) + 1, always_hide_cursor=True), title="主菜单"),
            VSplit(
                [
                    Window(cache_status_control, width=48, height=1),
                    Window(cache_hint_control, height=1, align=WindowAlign.RIGHT),
                ]
            ),
            VSplit(
                [
                    Window(key_control, height=1),
                    Window(csv_status_control, width=20, height=1, align=WindowAlign.RIGHT),
                    Window(FormattedTextControl("  "), width=2, height=1),
                ]
            ),
        ]
    )
    app = Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict(
            {
                "selected": "reverse bold",
                "csv-enabled": "bg:ansigreen ansiblack bold",
                "cache": "ansicyan",
                "cache-hint": "ansiyellow",
            }
        ),
    )
    app.run()

    if "selection" not in result:
        raise KeyboardInterrupt
    return result["selection"]


def run_numbered_manual_ref_picker(
    repo_root: Path,
    initial_old_ref: str = "",
    initial_new_ref: str = "",
) -> tuple[GitRef, GitRef] | None:
    while True:
        old_value = input(f"请输入 old ref（q 返回主菜单）[{initial_old_ref}]: ").strip() or initial_old_ref
        if old_value.lower() == "q":
            return None
        new_value = input(f"请输入 new ref（q 返回主菜单）[{initial_new_ref}]: ").strip() or initial_new_ref
        if new_value.lower() == "q":
            return None
        try:
            old_ref = resolve_ref(repo_root, old_value)
            new_ref = resolve_ref(repo_root, new_value)
        except RuntimeError as exc:
            print(f"ref 无效：{exc}")
            continue
        if old_ref.sha == new_ref.sha:
            print("old ref 和 new ref 指向同一个 commit")
            continue
        return old_ref, new_ref


def run_manual_ref_picker(
    repo_root: Path,
    initial_old_ref: str = "",
    initial_new_ref: str = "",
) -> tuple[GitRef, GitRef] | None:
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame, TextArea
    except ImportError:
        return run_numbered_manual_ref_picker(repo_root, initial_old_ref, initial_new_ref)

    old_input = TextArea(
        text=initial_old_ref,
        height=1,
        multiline=False,
        focus_on_click=True,
        wrap_lines=False,
        style="class:input",
    )
    new_input = TextArea(
        text=initial_new_ref,
        height=1,
        multiline=False,
        focus_on_click=True,
        wrap_lines=False,
        style="class:input",
    )
    error_message = ""
    result: dict[str, tuple[GitRef, GitRef]] = {}

    def show_error(message: str) -> None:
        nonlocal error_message
        error_message = message
        app.invalidate()

    def submit(event) -> None:  # type: ignore[no-untyped-def]
        old_value = old_input.text.strip()
        new_value = new_input.text.strip()
        if not old_value or not new_value:
            show_error("old ref 和 new ref 都不能为空")
            return
        try:
            old_ref = resolve_ref(repo_root, old_value)
            new_ref = resolve_ref(repo_root, new_value)
        except RuntimeError as exc:
            show_error(f"ref 无效：{exc}")
            return
        if old_ref.sha == new_ref.sha:
            show_error("old ref 和 new ref 指向同一个 commit")
            return
        result["refs"] = (old_ref, new_ref)
        event.app.exit()

    bindings = KeyBindings()

    @bindings.add("tab", eager=True)
    def _focus_next(event) -> None:  # type: ignore[no-untyped-def]
        event.app.layout.focus_next()

    @bindings.add("s-tab", eager=True)
    def _focus_previous(event) -> None:  # type: ignore[no-untyped-def]
        event.app.layout.focus_previous()

    @bindings.add("up", eager=True)
    def _focus_old_ref(event) -> None:  # type: ignore[no-untyped-def]
        event.app.layout.focus(old_input)

    @bindings.add("down", eager=True)
    def _focus_new_ref(event) -> None:  # type: ignore[no-untyped-def]
        event.app.layout.focus(new_input)

    @bindings.add("enter", eager=True)
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        submit(event)

    @bindings.add("escape")
    @bindings.add("c-c")
    def _quit(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit()

    error_control = FormattedTextControl(lambda: [("class:error", error_message)])
    status_control = FormattedTextControl(
        HTML(
            "<ansiyellow><b>↑/↓/Tab/Shift-Tab</b></ansiyellow><ansicyan> 切换输入框  </ansicyan>"
            "<ansiyellow><b>鼠标点击</b></ansiyellow><ansicyan> 切换输入框  </ansicyan>"
            "<ansiyellow><b>Enter</b></ansiyellow><ansicyan> 校验并确认  </ansicyan>"
            "<ansiyellow><b>Esc/Ctrl-C</b></ansiyellow><ansicyan> 返回主菜单</ansicyan>"
        )
    )
    root = HSplit(
        [
            Window(
                FormattedTextControl(HTML("<b>手动输入 Git refs</b>：输入分支名、tag 或 commit hash。")),
                height=1,
            ),
            Frame(old_input, title="old ref", height=3),
            Frame(new_input, title="new ref", height=3),
            Window(error_control, height=2, wrap_lines=True),
            Window(status_control, height=1),
        ]
    )
    app = Application(
        layout=Layout(root, focused_element=old_input),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict({"input": "bg:default", "error": "ansired bold"}),
    )
    app.run()
    return result.get("refs")


def run_numbered_single_picker(labels: list[str], title: str) -> int | None:
    print(title)
    for index, label in enumerate(labels):
        print(f"{index:>4}. {label}")
    while True:
        raw = input("请输入序号（q 返回主菜单）: ").strip().lower()
        if raw in {"q", "quit"}:
            return None
        try:
            selected = int(raw)
        except ValueError:
            print("请输入列表中的数字序号，或输入 q 返回主菜单。")
            continue
        if 0 <= selected < len(labels):
            return selected
        print(f"序号范围是 0 到 {len(labels) - 1}。")


def run_single_picker(
    labels: list[str],
    title: str,
    description: str,
    colored_items: list[GitRef | GitBranch | None] | None = None,
) -> int | None:
    if not labels:
        raise RuntimeError(f"{title}：没有可选择的项目")
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        return run_numbered_single_picker(labels, title)

    if colored_items is not None and len(colored_items) != len(labels):
        raise ValueError("colored_items 必须与 labels 数量一致")

    selected_index = 0
    result: dict[str, int] = {}

    def visible_height() -> int:
        try:
            return max(6, app.output.get_size().rows - 5)
        except NameError:
            return 19

    def visible_start() -> int:
        height = visible_height()
        return max(0, min(selected_index - height // 2, max(0, len(labels) - height)))

    def visible_width() -> int:
        try:
            return max(44, app.output.get_size().columns - 5)
        except NameError:
            return 115

    def display_width(text: str) -> int:
        width = 0
        for char in text:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    def fit_ref_message(ref: GitRef) -> str:
        message = ref.message.replace("\t", " ").strip() or "(no message)"
        fixed_width = (
            display_width(ref.committed_at)
            + display_width(" - ") * 2
            + display_width(ref.short_sha)
        )
        available = max(1, visible_width() - fixed_width)
        if display_width(message) <= available:
            return message + " " * (available - display_width(message))

        ellipsis = "…"
        target = max(0, available - display_width(ellipsis))
        fitted: list[str] = []
        current_width = 0
        for char in message:
            char_width = (
                0
                if unicodedata.combining(char)
                else 2
                if unicodedata.east_asian_width(char) in {"F", "W"}
                else 1
            )
            if current_width + char_width > target:
                break
            fitted.append(char)
            current_width += char_width
        return "".join(fitted) + " " * (target - current_width) + ellipsis

    def move(delta: int) -> None:
        nonlocal selected_index
        selected_index = max(0, min(len(labels) - 1, selected_index + delta))
        app.invalidate()

    def select(index: int) -> None:
        nonlocal selected_index
        selected_index = index
        app.invalidate()

    def rows() -> list[tuple]:
        fragments: list[tuple] = []
        start = visible_start()
        for index in range(start, min(len(labels), start + visible_height())):
            style = "class:selected" if index == selected_index else ""
            prefix = ">> " if index == selected_index else "   "

            def mouse_handler(event, index=index) -> None:  # type: ignore[no-untyped-def]
                if event.event_type == MouseEventType.MOUSE_UP:
                    select(index)
                elif event.event_type == MouseEventType.SCROLL_UP:
                    move(-1)
                elif event.event_type == MouseEventType.SCROLL_DOWN:
                    move(1)

            fragments.append((style, prefix, mouse_handler))
            if colored_items is None or colored_items[index] is None:
                label = labels[index]
                date_match = re.search(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", label)
                sha_match = re.search(r"[0-9a-fA-F]{12}(?![0-9a-fA-F])", label)
                if date_match is None or sha_match is None or sha_match.start() <= date_match.end():
                    fragments.append((style, label, mouse_handler))
                else:
                    fragments.extend(
                        [
                            (style, label[: date_match.start()], mouse_handler),
                            (f"{style} class:date", date_match.group(), mouse_handler),
                            (
                                f"{style} class:message",
                                label[date_match.end() : sha_match.start()],
                                mouse_handler,
                            ),
                            (f"{style} class:sha", sha_match.group(), mouse_handler),
                            (style, label[sha_match.end() :], mouse_handler),
                        ]
                    )
            else:
                item = colored_items[index]
                assert item is not None
                label = labels[index]
                date_start = label.find(item.committed_at)
                sha_start = label.rfind(item.short_sha)
                if date_start > 0:
                    fragments.append((style, label[:date_start], mouse_handler))
                fragments.extend(
                    [
                        (f"{style} class:date", item.committed_at, mouse_handler),
                        (
                            f"{style} class:message",
                            f" - {fit_ref_message(item)} - "
                            if isinstance(item, GitRef)
                            else label[date_start + len(item.committed_at) : sha_start],
                            mouse_handler,
                        ),
                        (f"{style} class:sha", item.short_sha, mouse_handler),
                    ]
                )
                sha_end = sha_start + len(item.short_sha)
                if sha_end < len(label):
                    fragments.append((style, label[sha_end:], mouse_handler))
            fragments.append(("", "\n"))
        return fragments

    bindings = KeyBindings()

    @bindings.add("up")
    @bindings.add("w")
    def _up(event) -> None:  # type: ignore[no-untyped-def]
        move(-1)

    @bindings.add("down")
    @bindings.add("s")
    def _down(event) -> None:  # type: ignore[no-untyped-def]
        move(1)

    @bindings.add("pageup")
    def _page_up(event) -> None:  # type: ignore[no-untyped-def]
        move(-max(1, visible_height() // 2))

    @bindings.add("pagedown")
    def _page_down(event) -> None:  # type: ignore[no-untyped-def]
        move(max(1, visible_height() // 2))

    @bindings.add("enter")
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        result["index"] = selected_index
        event.app.exit()

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("q")
    def _quit(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit()

    status = HTML(
        "<ansiyellow><b>↑/↓/W/S/鼠标滚轮</b></ansiyellow><ansicyan> 逐项  </ansicyan>"
        "<ansiyellow><b>PgUp/PgDn</b></ansiyellow><ansicyan> 半页  </ansicyan>"
        "<ansiyellow><b>鼠标点击</b></ansiyellow><ansicyan> 选择  </ansicyan>"
        "<ansiyellow><b>Enter</b></ansiyellow><ansicyan> 确认  </ansicyan>"
        "<ansiyellow><b>q/Esc/Ctrl-C</b></ansiyellow><ansicyan> 返回主菜单</ansicyan>"
    )
    root = HSplit(
        [
            Window(FormattedTextControl(description), height=1),
            Frame(Window(FormattedTextControl(rows), wrap_lines=False, always_hide_cursor=True), title=title),
            Window(FormattedTextControl(status), height=1),
        ]
    )
    app = Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict(
            {
                "selected": "reverse bold",
                "date": "ansicyan",
                "message": "ansidefault",
                "sha": "ansiyellow",
            }
        ),
    )
    app.run()
    return result.get("index")


def run_numbered_ref_cache_target_picker(ref: GitRef) -> str | None:
    print(f"ref {ref.sha}已复制")
    print("填入后请返回主菜单，选择“手动输入 old ref 和 new ref”开始对比。")
    print("1. 填入 old ref")
    print("2. 填入 new ref")
    print("3. 不填入，返回主菜单（默认）")
    while True:
        selected = input("请输入选项 [3]: ").strip()
        if selected == "1":
            return "old"
        if selected == "2":
            return "new"
        if not selected or selected == "3":
            return None
        print("请输入 1、2 或 3。")


def prompt_ref_cache_target(ref: GitRef) -> str | None:
    message = f"ref {ref.sha}已复制"
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application import get_app
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
        from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
        from prompt_toolkit.key_binding.defaults import load_key_bindings
        from prompt_toolkit.layout import HSplit, Layout
        from prompt_toolkit.mouse_events import MouseEventType
        from prompt_toolkit.styles import Style
        from prompt_toolkit.utils import get_cwidth
        from prompt_toolkit.widgets import Button, Dialog, Label
    except ImportError:
        return run_numbered_ref_cache_target_picker(ref)

    class RefCacheButton(Button):
        def _get_text_fragments(self) -> list[tuple]:
            focused = get_app().layout.has_focus(self)
            left_symbol = "▶ " if focused else "  "
            right_symbol = " ◀" if focused else "  "
            text_width = self.width - get_cwidth(left_symbol) - get_cwidth(right_symbol)
            padding = max(0, text_width - get_cwidth(self.text))
            text = " " * (padding // 2) + self.text + " " * (padding - padding // 2)
            focused_style = "class:button.focused" if focused else "class:button.arrow"
            text_style = "class:button.focused" if focused else "class:button.text"

            def handler(mouse_event) -> None:  # type: ignore[no-untyped-def]
                if self.handler is not None and mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self.handler()

            return [
                (focused_style, left_symbol, handler),
                ("[SetCursorPosition]", ""),
                (text_style, text, handler),
                (focused_style, right_symbol, handler),
            ]

    def exit_with(value: str | None):  # type: ignore[no-untyped-def]
        return lambda: get_app().exit(result=value)

    buttons = [
        RefCacheButton("填入 old ref", handler=exit_with("old"), width=18),
        RefCacheButton("填入 new ref", handler=exit_with("new"), width=18),
        RefCacheButton("不填入", handler=exit_with(None), width=18),
    ]
    dialog = Dialog(
        title="复制成功",
        body=HSplit(
            [
                Label(
                    text=(
                        f"{message}\n"
                        "填入后请返回主菜单，选择“手动输入 old ref 和 new ref”开始对比。\n"
                        "请选择是否将该 hash 缓存到 ref 输入框；不填入将返回主菜单。"
                    ),
                    dont_extend_height=True,
                ),
                Label(text="←/→ 选择    Enter 确认", dont_extend_height=True),
            ]
        ),
        buttons=buttons,
        with_background=True,
    )
    bindings = KeyBindings()
    bindings.add("tab")(focus_next)
    bindings.add("right")(focus_next)
    bindings.add("s-tab")(focus_previous)
    bindings.add("left")(focus_previous)

    @bindings.add("escape")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    dialog_style = Style.from_dict(
        {
            "dialog": "bg:default",
            "dialog.body": "bg:#eeeeee #000000",
            "dialog frame.label": "bg:#d7d7d7 #000000 bold",
            "dialog shadow": "bg:#555555",
            "dialog.body shadow": "bg:#555555",
            "button": "bg:#d7d7d7 #000000",
            "button.focused": "bg:#800080 #ffffff bold",
        }
    )
    return Application(
        layout=Layout(dialog),
        key_bindings=merge_key_bindings([load_key_bindings(), bindings]),
        mouse_support=True,
        style=dialog_style,
        full_screen=True,
    ).run()


def browse_origin_branch_refs(repo_root: Path) -> tuple[str, GitRef] | None:
    branches = list_origin_branches(repo_root)
    if not branches:
        raise RuntimeError("仓库中没有 origin 远端分支")
    branch_index = run_single_picker(
        [branch.label() for branch in branches],
        "origin 分支",
        "选择 origin 分支（按最后提交时间从新到旧排序）",
    )
    if branch_index is None:
        return

    branch = branches[branch_index]
    refs = list_refs(repo_root, None, branch.name)
    if not refs:
        raise RuntimeError(f"分支 {branch.name} 中没有可选择的 Git ref")
    ref_index = run_single_picker(
        [ref.label() for ref in refs],
        f"{branch.name} refs",
        f"选择 {branch.name} 中的 ref，按 Enter 复制完整 hash",
        colored_items=refs,
    )
    if ref_index is None:
        return
    selected_ref = refs[ref_index]
    copy_to_clipboard(selected_ref.sha)
    target = prompt_ref_cache_target(selected_ref)
    return (target, selected_ref) if target is not None else None


def choose_branch_to_switch(repo_root: Path, current: str) -> str | None:
    local_branches = list_local_branches(repo_root)
    local_names = {branch.name for branch in local_branches}
    remote_branches = [
        branch
        for branch in list_origin_branches(repo_root)
        if branch.name.removeprefix("origin/") not in local_names
    ]
    branches = [*local_branches, *remote_branches]
    if not branches:
        raise RuntimeError("仓库中没有可切换的本地或 origin 远端分支")

    labels = []
    for branch in branches:
        is_remote = branch.name.startswith("origin/")
        kind = "远端" if is_remote else "本地"
        current_marker = "（当前）" if not is_remote and branch.name == current else ""
        labels.append(f"[{kind}] {branch.label()}{current_marker}")
    selected = run_single_picker(
        labels,
        "切换分支",
        "选择本地分支，或创建并跟踪远端分支",
    )
    if selected is None:
        return None
    return branches[selected].name


def run_absent_en_selection(
    repo_root: Path,
    *,
    network_mode: bool,
    current: str,
    target_absent_ref: str | None = None,
    base_absent_ref: str | None = None,
) -> tuple[GitRef, GitRef | None] | None:
    if target_absent_ref:
        target_ref = resolve_ref(repo_root, target_absent_ref)
    elif network_mode:
        branches = list_origin_branches(repo_root)
        if not branches:
            raise RuntimeError("仓库中没有可选择的 origin 远端分支")
        index = run_single_picker(
            [branch.label() for branch in branches],
            "目标分支",
            "选择英文文档缺失统计的目标分支",
        )
        if index is None:
            return None
        target_ref = resolve_ref(repo_root, branches[index].name)
    else:
        target_ref = resolve_ref(repo_root, current)

    if base_absent_ref:
        return target_ref, resolve_ref(repo_root, base_absent_ref)

    branch_candidates = list_origin_branches(repo_root) if network_mode else [
        *list_local_branches(repo_root),
        *list_origin_branches(repo_root),
    ]
    choices = ["不使用基线"] + [branch.label() for branch in branch_candidates]
    index = run_single_picker(
        choices,
        "基线分支",
        "选择基线分支，或不使用基线，仅比较当前分支",
    )
    if index is None:
        return None
    if index == 0:
        return target_ref, None
    return target_ref, resolve_ref(repo_root, branch_candidates[index - 1].name)


def run_tui(
    refs: list[GitRef],
    default_old: GitRef | None,
    default_new: GitRef | None,
    branch: str | None = None,
    branch_kind: str = "当前分支",
) -> tuple[GitRef, GitRef] | None:
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.mouse_events import MouseEventType
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame
    except ImportError:
        return run_numbered_picker(refs, default_old, default_new, branch, branch_kind)

    if not refs:
        raise RuntimeError("没有可选择的 Git ref")

    branch_label = html.escape(branch or "未指定")
    old_index = refs.index(default_old) if default_old in refs else min(1, len(refs) - 1)
    new_index = refs.index(default_new) if default_new in refs else 0
    active_column = 0
    result: dict[str, tuple[GitRef, GitRef]] = {}

    def visible_height() -> int:
        try:
            rows = app.output.get_size().rows
        except NameError:
            rows = 24
        return max(6, rows - 5)

    def visible_width() -> int:
        try:
            columns = app.output.get_size().columns
        except NameError:
            columns = 120
        return max(44, (columns - 7) // 2)

    def display_width(text: str) -> int:
        width = 0
        for char in text:
            if unicodedata.combining(char):
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    def fit_message(message: str, width: int) -> str:
        if width <= 0:
            return ""
        current_width = display_width(message)
        if current_width <= width:
            return message + " " * (width - current_width)

        ellipsis_width = display_width("…")
        available = max(0, width - ellipsis_width)
        fitted: list[str] = []
        current_width = 0
        for char in message:
            char_width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
            if current_width + char_width > available:
                break
            fitted.append(char)
            current_width += char_width

        return "".join(fitted) + " " * (available - current_width) + "…"

    def visible_start(selected_index: int) -> int:
        height = visible_height()
        half = height // 2
        return max(0, min(selected_index - half, max(0, len(refs) - height)))

    def move_ref(column: int, delta: int) -> None:
        nonlocal active_column, old_index, new_index
        active_column = column
        if column == 0:
            old_index = max(0, min(len(refs) - 1, old_index + delta))
        else:
            new_index = max(0, min(len(refs) - 1, new_index + delta))
        refresh()

    def select_ref(column: int, index: int) -> None:
        nonlocal active_column, old_index, new_index
        active_column = column
        if column == 0:
            old_index = index
        else:
            new_index = index
        refresh()

    def row_fragments(index: int, selected_index: int, active: bool, column: int) -> list[tuple]:
        ref = refs[index]
        prefix = ">>" if index == selected_index and active else "**" if index == selected_index else "  "
        row_style = (
            "class:active-selected"
            if active and index == selected_index
            else "class:inactive-selected"
            if index == selected_index
            else ""
        )
        message = ref.message.replace("\t", " ").strip() or "(no message)"
        fixed_width = (
            display_width(prefix)
            + 1
            + display_width(ref.committed_at)
            + display_width(" - ") * 2
            + display_width(ref.short_sha)
        )
        message = fit_message(message, visible_width() - fixed_width)

        def mouse_handler(event, column=column, index=index) -> None:  # type: ignore[no-untyped-def]
            if event.event_type == MouseEventType.MOUSE_UP:
                select_ref(column, index)
            elif event.event_type == MouseEventType.SCROLL_UP:
                move_ref(column, -1)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                move_ref(column, 1)

        return [
            (row_style, f"{prefix} ", mouse_handler),
            (f"{row_style} class:date", ref.committed_at, mouse_handler),
            (row_style, " - ", mouse_handler),
            (f"{row_style} class:message", message, mouse_handler),
            (row_style, " - ", mouse_handler),
            (f"{row_style} class:sha", ref.short_sha, mouse_handler),
        ]

    def visible_rows(column: int, selected_index: int, active: bool) -> list[tuple]:
        height = visible_height()
        start = visible_start(selected_index)
        rows: list[tuple] = []
        for index in range(start, min(len(refs), start + height)):
            rows.extend(row_fragments(index, selected_index, active, column))
            rows.append(("", "\n"))
        return rows

    old_control = FormattedTextControl(lambda: visible_rows(0, old_index, active_column == 0))
    new_control = FormattedTextControl(lambda: visible_rows(1, new_index, active_column == 1))
    status_control = FormattedTextControl(
        HTML(
            "<ansiyellow><b>Tab/←/→/A/D</b></ansiyellow><ansicyan> 切换列  </ansicyan>"
            "<ansiyellow><b>↑/↓/W/S/鼠标滚轮</b></ansiyellow><ansicyan> 逐项  </ansicyan>"
            "<ansiyellow><b>PgUp/PgDn</b></ansiyellow><ansicyan> 半页  </ansicyan>"
            "<ansiyellow><b>鼠标点击</b></ansiyellow><ansicyan> 选择  </ansicyan>"
            "<ansiyellow><b>Enter</b></ansiyellow><ansicyan> 计算  </ansicyan>"
            "<ansiyellow><b>q/Esc/Ctrl-C</b></ansiyellow><ansicyan> 返回模式选择</ansicyan>"
        )
    )

    def refresh() -> None:
        app.invalidate()

    bindings = KeyBindings()

    @bindings.add("tab")
    def _switch(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal active_column
        active_column = 1 - active_column
        refresh()

    @bindings.add("left")
    @bindings.add("a")
    def _left(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal active_column
        active_column = 0
        refresh()

    @bindings.add("right")
    @bindings.add("d")
    def _right(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal active_column
        active_column = 1
        refresh()

    @bindings.add("up")
    @bindings.add("w")
    def _up(event) -> None:  # type: ignore[no-untyped-def]
        move_ref(active_column, -1)

    @bindings.add("down")
    @bindings.add("s")
    def _down(event) -> None:  # type: ignore[no-untyped-def]
        move_ref(active_column, 1)

    @bindings.add("pageup")
    def _page_up(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal old_index, new_index
        step = max(1, visible_height() // 2)
        if active_column == 0:
            old_index = max(0, old_index - step)
        else:
            new_index = max(0, new_index - step)
        refresh()

    @bindings.add("pagedown")
    def _page_down(event) -> None:  # type: ignore[no-untyped-def]
        nonlocal old_index, new_index
        step = max(1, visible_height() // 2)
        if active_column == 0:
            old_index = min(len(refs) - 1, old_index + step)
        else:
            new_index = min(len(refs) - 1, new_index + step)
        refresh()

    @bindings.add("enter")
    def _enter(event) -> None:  # type: ignore[no-untyped-def]
        result["refs"] = (refs[old_index], refs[new_index])
        event.app.exit()

    @bindings.add("escape")
    @bindings.add("c-c")
    @bindings.add("q")
    def _quit(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit()

    root = HSplit(
        [
            Window(
                FormattedTextControl(
                    HTML(
                        f"<b>选择 Git refs</b>：{html.escape(branch_kind)} <b>{branch_label}</b>。"
                        "左列 old ref，右列 new ref。提交后按句子级口径统计中文刷新字数和新增图片数，图片文件每个折算 200 字。"
                    )
                ),
                height=1,
            ),
            VSplit(
                [
                    Frame(Window(old_control, wrap_lines=False, always_hide_cursor=True), title="old ref"),
                    Frame(Window(new_control, wrap_lines=False, always_hide_cursor=True), title="new ref"),
                ]
            ),
            Window(status_control, height=1),
        ]
    )
    app = Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=True,
        style=Style.from_dict(
            {
                "active-selected": "reverse bold",
                "inactive-selected": "bg:#dddddd #000000",
                "date": "ansicyan",
                "message": "ansidefault",
                "sha": "ansiyellow",
            }
        ),
    )
    app.run()

    if "refs" not in result:
        return None
    return result["refs"]


def run_numbered_picker(
    refs: list[GitRef],
    default_old: GitRef | None,
    default_new: GitRef | None,
    branch: str | None = None,
    branch_kind: str = "当前分支",
) -> tuple[GitRef, GitRef]:
    if not refs:
        raise RuntimeError("没有可选择的 Git ref")

    default_old_index = refs.index(default_old) if default_old in refs else min(1, len(refs) - 1)
    default_new_index = refs.index(default_new) if default_new in refs else 0
    width = min(88, max(len(ref.label(88)) for ref in refs[:40]) if refs else 88)

    print("未安装 prompt_toolkit，使用编号式 ref 选择。安装后可使用全屏双列 TUI：")
    print("python -m pip install prompt_toolkit")
    if branch:
        print(f"{branch_kind}: {branch}")
    print()
    print(f"{'old/new ref 列表':<{width}}  {'序号':>4}")
    print("-" * (width + 8))
    for index, ref in enumerate(refs):
        label = ref.label(width)
        print(f"{label:<{width}}  {index:>4}")

    def ask_index(prompt: str, default_index: int) -> int:
        while True:
            raw = input(f"{prompt} [{default_index}]: ").strip()
            if not raw:
                return default_index
            try:
                selected = int(raw)
            except ValueError:
                print("请输入列表中的数字序号。")
                continue
            if 0 <= selected < len(refs):
                return selected
            print(f"序号范围是 0 到 {len(refs) - 1}。")

    old_index = ask_index("old ref 序号", default_old_index)
    new_index = ask_index("new ref 序号", default_new_index)
    return refs[old_index], refs[new_index]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = ChineseArgumentParser(description="统计 Git 中文 Markdown 文档刷新工作量，或检查缺失的英文文档。")
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"
    parser.add_argument("--all", action="store_true", help="直接统计当前仓库所有中文 Markdown 文档的字数和图片数")
    parser.add_argument("--file", dest="files", action="append", type=Path, metavar="PATH", help="统计指定文件或目录的中文字数和图片数，可重复使用；目录会递归扫描 Markdown 文件")
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument("--csv", dest="csv", action="store_true", default=True, help="以 CSV 格式输出（默认）")
    format_group.add_argument("--txt", dest="csv", action="store_false", help="以文本格式输出")
    parser.add_argument("--git-url", help="在任意目录通过持久裸仓库缓存读取远端 Git URL，不 checkout 工作树")
    parser.add_argument("--base-compare-ref", help="非交互模式：刷新工作量对比的基线 Git ref")
    parser.add_argument("--target-compare-ref", help="非交互模式：刷新工作量对比的目标 Git ref")
    parser.add_argument("--old-ref", dest="base_compare_ref", help=argparse.SUPPRESS)
    parser.add_argument("--new-ref", dest="target_compare_ref", help=argparse.SUPPRESS)
    parser.add_argument("--absent-en", action="store_true", help="统计缺失的英文 Markdown 文档")
    parser.add_argument("--base-absent-ref", help="英文缺失统计的可选基线 Git ref")
    parser.add_argument("--target-absent-ref", help="英文缺失统计的目标 Git ref")
    parser.add_argument("--target-branch", dest="target_absent_ref", help=argparse.SUPPRESS)
    parser.add_argument("--baseline-branch", dest="base_absent_ref", help=argparse.SUPPRESS)
    parser.add_argument(
        "--max-refs",
        type=int,
        default=500,
        help="当前分支比较 TUI 中最多展示的 commit 数，默认 500",
    )
    parser.add_argument("--config-root", type=Path, default=default_config_root(), help="配置和存档根目录")
    return parser.parse_args(argv)


def run_absent_en_report(
    repo_root: Path,
    repo_name: str,
    target_ref: GitRef,
    baseline_ref: GitRef | None,
    csv_output: bool,
    *,
    use_tui: bool = False,
    network_mode: bool = False,
) -> int:
    files = find_absent_en_with_progress(
        repo_root,
        target_ref,
        baseline_ref,
        use_tui=use_tui,
        network_mode=network_mode,
    )
    output_path = summary_output_path(Path.cwd(), repo_name, "absent_en", csv_output=csv_output)
    write_absent_en_output(output_path, files, csv_output, target_ref, baseline_ref)
    print_absent_en_result(files, output_path)
    return 0


def run_repository(args: argparse.Namespace, repo_root: Path, git_url: str | None = None) -> int:
    network_mode = git_url is not None
    if args.absent_en and (args.base_compare_ref or args.target_compare_ref):
        raise RuntimeError("--absent-en 不能与 --base-compare-ref 或 --target-compare-ref 同时使用")
    if args.base_absent_ref and not args.absent_en:
        raise RuntimeError("--base-absent-ref 只能与 --absent-en 一起使用")
    if args.target_absent_ref and not args.absent_en:
        raise RuntimeError("--target-absent-ref 只能与 --absent-en 一起使用")
    interactive_run = not (args.base_compare_ref and args.target_compare_ref)
    repo_name = repository_name_from_url(git_url) if git_url is not None else repo_root.name
    repo_source = display_git_url(git_url) if git_url is not None else str(repo_root)
    config_root = args.config_root.expanduser()
    repo_dir = config_root / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_root / "config.json"
    config = load_config(config_path)
    csv_output = args.csv

    if args.base_compare_ref and args.target_compare_ref:
        old_ref = resolve_ref(repo_root, args.base_compare_ref)
        new_ref = resolve_ref(repo_root, args.target_compare_ref)
    else:
        branch = current_branch(repo_root)
        if network_mode:
            print(f"远端默认分支: {branch}")
        else:
            print(f"当前分支: {branch}")
            fetch_current_branch(repo_root, branch)
            if confirm_pull_current_branch():
                pull_current_branch(repo_root)
        if args.absent_en:
            selection = run_absent_en_selection(
                repo_root,
                network_mode=network_mode,
                current=branch,
                target_absent_ref=args.target_absent_ref,
                base_absent_ref=args.base_absent_ref,
            )
            if selection is None:
                return 0
            target_ref, baseline_ref = selection
            return run_absent_en_report(
                repo_root,
                repo_name,
                target_ref,
                baseline_ref,
                args.csv,
                use_tui=network_mode,
                network_mode=network_mode,
            )
        refs = list_refs(repo_root, args.max_refs, branch)
        last_repo = config.get("repos", {}).get(repo_name, {}) if isinstance(config.get("repos"), dict) else {}
        cached_old_ref = ""
        cached_new_ref = ""
        while True:
            mode, csv_output = run_mode_picker(
                csv_output,
                cached_old_ref,
                cached_new_ref,
                allow_all=not network_mode,
                compare_option=(
                    "默认分支 ref 比较（默认）"
                    if network_mode
                    else f"当前分支 {branch} ref 比较（默认）"
                ),
                allow_absent_en=True,
                allow_switch_branch=not network_mode,
            )
            if mode == 5 and not network_mode:
                selected_branch = choose_branch_to_switch(repo_root, branch)
                if selected_branch is None:
                    continue
                selected_remote_branch = selected_branch.startswith("origin/")
                try:
                    changed = switch_branch(repo_root, selected_branch)
                except RuntimeError as exc:
                    print(f"切换分支失败：{exc}")
                    input("请按 Enter 键继续...")
                    continue
                branch = current_branch(repo_root)
                if changed:
                    print(f"已切换到分支: {branch}")
                if not selected_remote_branch and confirm_pull_current_branch():
                    pull_current_branch(repo_root)
                refs = list_refs(repo_root, args.max_refs, branch)
                continue

            if mode == 3 and not network_mode:
                files = count_all_documents(repo_root)
                last_commit = resolve_ref(repo_root, "HEAD")
                summary_path = summary_output_path(
                    Path.cwd(),
                    repo_name,
                    "full",
                    last_commit_timestamp=last_commit.committed_at,
                    csv_output=csv_output,
                )
                write_all_summary_output(summary_path, repo_root, files, csv_output)
                (
                    print_all_result_csv(files, summary_path)
                    if csv_output
                    else print_all_result(repo_root, files, summary_path)
                )
                return 0

            absent_mode = 4 if not network_mode else 3
            if mode == absent_mode:
                selection = run_absent_en_selection(
                    repo_root,
                    network_mode=network_mode,
                    current=branch,
                )
                if selection is None:
                    continue
                target_ref, baseline_ref = selection
                return run_absent_en_report(
                    repo_root,
                    repo_name,
                    target_ref,
                    baseline_ref,
                    csv_output,
                    use_tui=True,
                    network_mode=network_mode,
                )

            if mode == 2:
                manual_refs = run_manual_ref_picker(repo_root, cached_old_ref, cached_new_ref)
                if manual_refs is None:
                    continue
                old_ref, new_ref = manual_refs
                break

            if mode == 1:
                cache_selection = browse_origin_branch_refs(repo_root)
                if cache_selection is not None:
                    target, selected_ref = cache_selection
                    if target == "old":
                        cached_old_ref = selected_ref.sha
                    else:
                        cached_new_ref = selected_ref.sha
                continue

            default_old = None
            default_new = None
            if isinstance(last_repo, dict):
                last_old = last_repo.get("old_sha")
                last_new = last_repo.get("new_sha")
                default_old = next((ref for ref in refs if ref.sha == last_old), None) if isinstance(last_old, str) else None
                default_new = next((ref for ref in refs if ref.sha == last_new), None) if isinstance(last_new, str) else None
            if default_old is None:
                default_old = refs[min(1, len(refs) - 1)] if refs else None
            if default_new is None:
                default_new = refs[0] if refs else None

            selected_refs = run_tui(
                refs,
                default_old,
                default_new,
                branch,
                branch_kind="默认分支" if network_mode else "当前分支",
            )
            if selected_refs is not None:
                old_ref, new_ref = selected_refs
                break

    if old_ref.sha == new_ref.sha:
        raise RuntimeError("old ref 和 new ref 指向同一个 commit")

    generated = dt.datetime.now().astimezone()
    files = calculate_with_progress(
        repo_root,
        old_ref,
        new_ref,
        use_tui=interactive_run,
        network_mode=network_mode,
    )
    result = RunResult(
        run_id=str(uuid.uuid4()),
        generated_at=generated.isoformat(timespec="seconds"),
        repo_name=repo_name,
        repo_root=repo_source,
        old_ref=old_ref,
        new_ref=new_ref,
        files=files,
    )

    csv_path, json_path = archive_paths(repo_dir, old_ref, new_ref, generated)
    write_csv(csv_path, result)
    write_json(json_path, result)
    summary_path = summary_output_path(
        Path.cwd(),
        repo_name,
        "compare",
        old_timestamp=old_ref.committed_at,
        new_timestamp=new_ref.committed_at,
        csv_output=csv_output,
    )
    write_summary_output(summary_path, result, csv_output)

    repos_config = config.get("repos")
    if not isinstance(repos_config, dict):
        repos_config = {}
    repos_config[repo_name] = {
        "repo_root": repo_source,
        "old_sha": old_ref.sha,
        "new_sha": new_ref.sha,
        "max_refs": args.max_refs,
        "last_run_at": result.generated_at,
    }
    config["last_repo"] = repo_name
    config["repos"] = repos_config
    save_config(config_path, config)

    print_result(result, csv_path, json_path, summary_path)
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    try:
        if args.all and args.files:
            raise RuntimeError("--all 不能与 --file 同时使用")
        if args.absent_en and (args.all or args.files):
            raise RuntimeError("--absent-en 不能与 --all 或 --file 同时使用")
        if args.git_url and args.files:
            raise RuntimeError("--git-url 不能与 --file 同时使用")
        if args.git_url and args.all:
            raise RuntimeError("--git-url 不能与 --all 同时使用；远端裸仓库没有当前工作树")
        if (args.target_absent_ref or args.base_absent_ref) and not args.absent_en:
            raise RuntimeError("--target-absent-ref 和 --base-absent-ref 只能与 --absent-en 一起使用")
        if args.base_compare_ref or args.target_compare_ref:
            if not (args.base_compare_ref and args.target_compare_ref):
                raise RuntimeError("--base-compare-ref 和 --target-compare-ref 必须同时提供")

        if args.files:
            if args.base_compare_ref or args.target_compare_ref:
                raise RuntimeError("--file 不能与 --base-compare-ref 或 --target-compare-ref 同时使用")
            files = count_requested_documents(args.files)
            (print_all_result_csv(files) if args.csv else print_all_result(None, files))
            return 0

        if args.all:
            if args.base_compare_ref or args.target_compare_ref:
                raise RuntimeError("--all 不能与 --base-compare-ref 或 --target-compare-ref 同时使用")
            repo_root = find_git_root(Path.cwd())
            files = count_all_documents(repo_root)
            (print_all_result_csv(files) if args.csv else print_all_result(repo_root, files))
            return 0

        git_url = args.git_url
        repo_root: Path | None = None
        if git_url is None:
            try:
                repo_root = find_git_root(Path.cwd())
            except RuntimeError:
                if args.base_compare_ref or args.target_compare_ref:
                    raise RuntimeError("当前目录不是 Git 仓库；请同时使用 --git-url URL")
                git_url = run_git_url_picker()
                if git_url is None:
                    raise KeyboardInterrupt

        if repo_root is not None:
            return run_repository(args, repo_root)

        if git_url is None:
            raise RuntimeError("Git URL 不能为空")
        config_root = args.config_root.expanduser()
        remote_repo_root = network_cache_path(config_root, git_url)
        cache_root = remote_repo_root.parent
        cleanup_network_cache(cache_root, active_cache=remote_repo_root)
        interactive_network = not (args.base_compare_ref and args.target_compare_ref)
        _default_branch, cache_warning = clone_remote_repository_with_progress(
            git_url,
            remote_repo_root,
            use_tui=interactive_network,
        )
        if cache_warning:
            show_network_cache_warning(cache_warning, interactive_network)
        try:
            with git_url_override(git_url):
                return run_repository(args, remote_repo_root, git_url)
        finally:
            cleanup_network_cache(cache_root)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def should_pause_on_exit() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def pause_on_exit() -> None:
    if not should_pause_on_exit():
        return
    try:
        input("\n按 Enter 键退出...")
    except (EOFError, KeyboardInterrupt):
        return


def entrypoint(argv: list[str]) -> int:
    exit_code = main(argv)
    pause_on_exit()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(entrypoint(sys.argv[1:]))
