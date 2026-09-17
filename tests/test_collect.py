from datetime import datetime, timedelta, timezone
from io import StringIO
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
import sys
from unittest.mock import patch

import monitor


TZ8 = timezone(timedelta(hours=8))


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=command_env, check=True,
        text=True, capture_output=True,
    )
    return result.stdout.strip()


def _history_repo(root: Path, commit_times: list[str]) -> tuple[Path, list[str]]:
    origin = root / "origin.git"
    work_root = root / "repos"
    work = work_root / "demo"
    work_root.mkdir()
    _git(root, "init", "--bare", str(origin))
    _git(root, "clone", str(origin), str(work))
    _git(work, "checkout", "-b", "main")
    _git(work, "config", "user.email", "tests@example.invalid")
    _git(work, "config", "user.name", "Report Tests")
    hashes: list[str] = []
    document = work / "docs" / "zh" / "history.md"
    document.parent.mkdir(parents=True)
    for index, commit_time in enumerate(commit_times):
        document.write_text(f"第 {index} 次提交\n", encoding="utf-8")
        commit_env = {
            "GIT_AUTHOR_DATE": commit_time,
            "GIT_COMMITTER_DATE": commit_time,
        }
        _git(work, "add", ".")
        _git(work, "commit", "-m", f"history {index}", env=commit_env)
        hashes.append(_git(work, "rev-parse", "HEAD"))
    _git(work, "push", "-u", "origin", "main")
    return work, hashes


def _slot_config(repo_root: Path, *, mode: str = "slot") -> dict:
    return {
        "repos_root": str(repo_root),
        "schedule": {
            "time": "19:00",
            "window_hours": 24,
            "window_mode": mode,
            "clamp_tip_to_window_end": True,
        },
        "runtime": {"concurrency": 1, "keep_raw_days": -1},
        "report": {"default_days": 2, "top_n_changed": 10, "title": "测试"},
        "repos": [{"name": "demo", "url": "origin", "branch": "main", "paths_filter": []}],
    }


class CollectTests(unittest.TestCase):
    def test_remote_mirror_resolves_bare_branch_tip_and_commit_info(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _work, hashes = _history_repo(
                root,
                [
                    "2026-09-07T18:00:00+08:00",
                    "2026-09-08T18:00:00+08:00",
                    "2026-09-09T18:27:00+08:00",
                ],
            )
            config = _slot_config(root / "repos")
            config.update(
                {
                    "repo_source": "remote",
                    "mirrors_root": str(root / "mirrors"),
                    "tool_cache_root": str(root / "toolcache"),
                }
            )
            repo_config = {
                "name": "demo",
                "url": str(root / "origin.git"),
                "branch": "main",
                "paths_filter": [],
            }
            mirror = monitor._ensure_remote_mirror(config, repo_config)
            self.assertEqual(monitor._branch_ref(mirror, "main", repo_source="remote"), "main")
            self.assertEqual(monitor._tip(mirror, "main", repo_source="remote"), hashes[-1])
            self.assertEqual(
                monitor._tip(
                    mirror,
                    "main",
                    before=datetime(2026, 9, 9, 19, tzinfo=TZ8),
                    repo_source="remote",
                ),
                hashes[-1],
            )
            self.assertEqual(monitor._commit_info(mirror, hashes[-1]), ("2026-09-09T18:27:00+08:00", "history 2"))

    def test_run_counter_remote_uses_network_route_and_local_keeps_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_path, hashes = _history_repo(root, ["2026-09-07T18:00:00+08:00", "2026-09-10T18:00:00+08:00"])
            config = _slot_config(root / "repos")
            config.update({"tool_cache_root": str(root / "toolcache")})
            seen: list[tuple[list[str], Path]] = []

            def fake_counter(args, **kwargs):
                cwd = Path(kwargs["cwd"])
                seen.append((list(args), cwd))
                (cwd / "demo_compare_test.csv").write_text(
                    "docs/zh/history.md,1,2,0,3,modified\nTOTAL,1,2,0,3\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args, 0, b"", b"")

            with patch.object(monitor.subprocess, "run", side_effect=fake_counter):
                remote = monitor.run_counter(
                    {**config, "repo_source": "remote"},
                    repo_path,
                    "demo",
                    hashes[0],
                    hashes[1],
                    [],
                    "2099-01-01",
                    repo_url=str(root / "origin.git"),
                    repo_source="remote",
                )
                local = monitor.run_counter(
                    config,
                    repo_path,
                    "demo",
                    hashes[0],
                    hashes[1],
                    [],
                    "2099-01-02",
                )

            remote_args, remote_cwd = seen[0]
            local_args, local_cwd = seen[1]
            self.assertEqual(remote.totals.added_chars, 2)
            self.assertIn("--git-url", remote_args)
            self.assertIn(str(root / "origin.git"), remote_args)
            self.assertIn("--config-root", remote_args)
            self.assertIn(str(root / "toolcache"), remote_args)
            self.assertNotIn("--git-url", local_args)
            self.assertNotIn("--config-root", local_args)
            self.assertEqual(remote_cwd.parent, Path(tempfile.gettempdir()))
            self.assertTrue(str(local_cwd).startswith(str(repo_path) + os.sep))
            self.assertFalse(remote_cwd.exists())
            self.assertFalse(local_cwd.exists())

    def test_check_config_remote_uses_mirror_and_prints_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _history_repo(root, ["2026-09-07T18:00:00+08:00"])
            config = _slot_config(root / "must-not-be-read")
            config.update(
                {
                    "repo_source": "remote",
                    "mirrors_root": str(root / "mirrors"),
                    "repos": [
                        {
                            "name": "demo",
                            "url": str(root / "origin.git"),
                            "branch": "main",
                            "paths_filter": ["docs/zh"],
                        }
                    ],
                }
            )
            output = StringIO()
            with patch.object(monitor, "ROOT", root), patch.object(sys, "stdout", output):
                self.assertEqual(monitor._check_config(config), 0)
            text = output.getvalue()
            self.assertIn(f"repo_source=remote mirrors_root={root / 'mirrors'}", text)
            self.assertIn("[通过] demo: main:docs/zh", text)

    def test_historical_tip_is_clamped_to_window_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_path, hashes = _history_repo(
                root,
                [
                    "2026-09-07T18:00:00+08:00",
                    "2026-09-08T18:00:00+08:00",
                    "2026-09-09T18:27:00+08:00",
                    "2026-09-10T18:00:00+08:00",
                ],
            )
            outcome = monitor.CounterOutcome(
                monitor.Totals(changed_sentences=579, added_chars=9321),
                monitor.Totals(changed_sentences=579, added_chars=9321),
                [], 25,
            )
            slot = datetime(2026, 9, 9, 19, tzinfo=TZ8)
            with patch.object(monitor, "run_counter", return_value=outcome) as counter:
                result = monitor.collect_repository(
                    _slot_config(root / "repos"),
                    {"name": "demo", "url": "origin", "branch": "main", "paths_filter": []},
                    date="2026-09-09",
                    now=datetime(2026, 9, 16, 12, tzinfo=TZ8),
                    run_id="historical",
                    slot_end=slot,
                )
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.tip_commit, hashes[2])
            self.assertEqual(result.base_commit, hashes[1])
            self.assertEqual(result.word_delta, 9321)
            self.assertEqual(counter.call_args.args[4], hashes[2])
            self.assertNotEqual(counter.call_args.args[4], hashes[3])
            self.assertEqual(result.commit_time, "2026-09-09T18:27:00+08:00")

    def test_realtime_mode_keeps_current_branch_tip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_path, hashes = _history_repo(
                root,
                ["2026-09-07T18:00:00+08:00", "2026-09-10T18:00:00+08:00"],
            )
            with patch.object(monitor, "_tip", wraps=monitor._tip) as tip:
                result = monitor.collect_repository(
                    _slot_config(root / "repos", mode="rolling"),
                    {"name": "demo", "url": "origin", "branch": "main", "paths_filter": []},
                    date="2026-09-16",
                    now=datetime(2026, 9, 16, 19, tzinfo=TZ8),
                    run_id="realtime",
                )
            self.assertEqual(result.tip_commit, hashes[-1])
            self.assertEqual(tip.call_args.args, (repo_path, "main"))

    def test_historical_window_without_prior_commit_is_no_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _history_repo(root, ["2026-09-10T18:00:00+08:00"])
            slot = datetime(2026, 9, 9, 19, tzinfo=TZ8)
            with patch.object(monitor, "run_counter") as counter:
                result = monitor.collect_repository(
                    _slot_config(root / "repos"),
                    {"name": "demo", "url": "origin", "branch": "main", "paths_filter": []},
                    date="2026-09-09",
                    now=datetime(2026, 9, 16, 12, tzinfo=TZ8),
                    run_id="empty-history",
                    slot_end=slot,
                )
            self.assertEqual(result.status, "no_change")
            self.assertIsNone(result.tip_commit)
            self.assertEqual(result.word_delta, 0)
            counter.assert_not_called()

    def test_backfill_purge_is_ordered_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parsed = monitor.build_parser().parse_args([
                "backfill", "--days", "2", "--to", "2026-09-09",
                "--only", "demo", "--limit", "1", "--purge", "--dry-run",
            ])
            self.assertEqual(
                (parsed.days, parsed.to_date, parsed.only, parsed.limit, parsed.purge, parsed.dry_run),
                (2, "2026-09-09", ["demo"], 1, True, True),
            )
            _history_repo(
                root,
                [
                    "2026-09-07T18:00:00+08:00",
                    "2026-09-08T18:00:00+08:00",
                    "2026-09-09T18:27:00+08:00",
                    "2026-09-10T18:00:00+08:00",
                ],
            )
            config = _slot_config(root / "repos")
            args = SimpleNamespace(
                days=2, to_date="2026-09-09", only=(), limit=None,
                purge=True, dry_run=False,
            )
            fake_outcome = monitor.CounterOutcome(
                monitor.Totals(changed_sentences=1, added_chars=10),
                monitor.Totals(changed_sentences=1, added_chars=10),
                [], 1,
            )
            data_dir = root / "data"
            output = StringIO()
            dry_args = SimpleNamespace(
                days=2, to_date="2026-09-09", only=(), limit=None,
                purge=True, dry_run=True,
            )
            with patch.object(monitor, "_data_dir", return_value=data_dir), patch.object(
                monitor, "now_local", return_value=datetime(2026, 9, 16, 12, tzinfo=TZ8)
            ), patch.object(monitor, "run_counter", return_value=fake_outcome), patch.object(
                monitor, "generate_report", return_value=None
            ), patch.object(monitor, "prune_detail_rows") as prune, patch("sys.stdout", output):
                self.assertEqual(monitor.command_backfill(config, dry_args), 0)
                self.assertNotIn("purge:", output.getvalue())
                self.assertEqual(monitor.command_backfill(config, args), 0)
                self.assertEqual(monitor.command_backfill(config, args), 0)
                prune.assert_not_called()
            conn = monitor.connect_db(data_dir / "monitor.db")
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM repo_daily").fetchone()[0], 2)
                self.assertEqual(
                    [row[0] for row in conn.execute("SELECT date FROM repo_daily ORDER BY date")],
                    ["2026-09-08", "2026-09-09"],
                )
            finally:
                conn.close()
            output_text = output.getvalue()
            self.assertLess(output_text.index("2026-09-08T19:00:00+08:00"), output_text.index("2026-09-09T19:00:00+08:00"))

    def test_only_repeated_arguments_are_deduplicated_in_request_order_and_warn_unknown(self):
        parser = monitor.build_parser()
        args = parser.parse_args([
            "run", "--only", "B", "--only", "A", "B", "--only", "missing",
        ])
        self.assertEqual(args.only, ["B", "A", "B", "missing"])
        config = {
            "repos": [
                {"name": "A", "enabled": True},
                {"name": "B", "enabled": True},
                {"name": "C", "enabled": True},
            ]
        }
        output = StringIO()
        with patch("sys.stdout", output):
            selected = monitor._selected_repo_configs(config, None, args.only)
        self.assertEqual([item["name"] for item in selected], ["B", "A"])
        self.assertIn("warn: --only 指定的仓库不在配置中：missing", output.getvalue())

    def test_decode_output_handles_utf8_cp936_and_invalid_bytes(self):
        self.assertEqual(monitor._decode_output("中文".encode("utf-8")), "中文")
        with patch.object(monitor.locale, "getpreferredencoding", return_value="gbk"):
            self.assertEqual(monitor._decode_output("算法".encode("gbk")), "算法")
            self.assertIsInstance(monitor._decode_output(b"\xad\x80"), str)
        self.assertEqual(monitor._decode_output(None), "")
        self.assertEqual(monitor._decode_output(b""), "")

    def test_run_decodes_cp936_stdout_and_invalid_stderr_without_raising(self):
        script = (
            "import sys; "
            "sys.stdout.buffer.write('算法'.encode('gbk')); "
            "sys.stderr.buffer.write(b'\\xad\\x80')"
        )
        result = monitor._run([sys.executable, "-c", script])
        self.assertEqual(result.returncode, 0)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)

    def test_same_slot_reuses_completed_row_instead_of_zeroing_it(self):
        slot = datetime(2026, 9, 15, 19, tzinfo=TZ8)
        previous = monitor.DailyRecord(
            date="2026-09-15",
            repo="demo",
            repo_url="https://example.invalid/demo",
            branch="main",
            slot_end=monitor.iso_time(slot),
            window_start="2026-09-14T19:00:00+08:00",
            window_end=monitor.iso_time(slot),
            effective_paths=[],
            status="ok",
            word_delta=120,
            text_only_delta=120,
            image_delta=0,
            sentence_delta=4,
            deleted_lines=1,
            matched_files=2,
            base_commit="a" * 40,
            base_commit_time="2026-09-14T17:00:00+08:00",
            base_commit_subject="基线提交",
            tip_commit="b" * 40,
            commit_subject="已有变更",
            commit_time="2026-09-15T18:00:00+08:00",
            generated_at="2026-09-15T19:05:00+08:00",
            whole_word_delta=120,
            files=[monitor.FileChange("docs/zh/a.md", None, 4, 120, 0, 1, "内容变更")],
        )
        with tempfile.TemporaryDirectory() as directory:
            conn = monitor.connect_db(Path(directory) / "monitor.db")
            monitor.upsert_repo_daily(conn, previous)
            conn.commit()
            latest, _ = monitor._latest_tips(conn)
            loaded = latest["demo"]
            with patch.object(monitor, "_repo_has_git", return_value=True), patch.object(
                monitor, "git", return_value=SimpleNamespace(returncode=0)
            ), patch.object(monitor, "_tip", return_value=loaded.tip_commit), patch.object(
                monitor, "_commit_info", return_value=(loaded.commit_time, loaded.commit_subject)
            ), patch.object(monitor, "run_counter") as counter:
                result = monitor.collect_repository(
                    {"repos_root": directory, "schedule": {"time": "19:00", "window_hours": 24, "window_mode": "slot"}},
                    {"name": "demo", "url": loaded.repo_url, "branch": "main", "paths_filter": []},
                    date="2026-09-15",
                    now=datetime(2026, 9, 16, 10, tzinfo=TZ8),
                    run_id="rerun",
                    previous_tip=loaded.tip_commit,
                    previous_record=loaded,
                    slot_end=slot,
                )
            self.assertEqual((result.status, result.word_delta, result.base_commit, result.tip_commit), ("ok", 120, "a" * 40, "b" * 40))
            self.assertEqual(result.files[0].path, "docs/zh/a.md")
            counter.assert_not_called()
            conn.close()

    def test_paths_sentinels_mean_whole_repository(self):
        for value in (None, "", "  ", '""', "''", "all", "ALL", "*"):
            with self.subTest(value=value):
                self.assertEqual(monitor._parse_paths(value), [])
        self.assertEqual(monitor._parse_paths(" docs/zh, skills "), ["docs/zh", "skills"])

    def test_collect_warns_for_explicit_path_with_no_matches(self):
        config = {"repos_root": "/tmp/not-used", "repos": [{"name": "demo"}]}
        args = SimpleNamespace(repo="demo", base="old", target="new", paths="missing")
        outcome = monitor.CounterOutcome(monitor.Totals(), monitor.Totals(), [], 0)
        output = StringIO()
        with patch.object(monitor, "_repo_has_git", return_value=True), patch.object(monitor, "run_counter", return_value=outcome), patch(
            "sys.stdout", output
        ):
            self.assertEqual(monitor.command_collect(config, args), 0)
        self.assertIn('warn: 生效路径 ["missing"] 未匹配任何文件', output.getvalue())

    def test_no_change_same_ref_and_fast_skip(self):
        self.assertTrue(monitor.should_no_change("same", "same"))
        self.assertTrue(monitor.should_no_change("old", "new", "new"))
        self.assertFalse(monitor.should_no_change("old", "new", "other"))

    def test_latest_due_slot_boundaries(self):
        cases = [
            ("2026-09-16T10:00:00+08:00", "2026-09-15T19:00:00+08:00"),
            ("2026-09-16T19:00:00+08:00", "2026-09-16T19:00:00+08:00"),
            ("2026-09-16T18:59:00+08:00", "2026-09-15T19:00:00+08:00"),
            ("2026-09-17T00:30:00+08:00", "2026-09-16T19:00:00+08:00"),
        ]
        for raw_now, expected in cases:
            with self.subTest(raw_now=raw_now):
                self.assertEqual(monitor.iso_time(monitor.latest_due_slot(datetime.fromisoformat(raw_now), "19:00")), expected)

    def test_slot_completeness_and_catch_up_limit(self):
        latest = datetime(2026, 9, 16, 19, tzinfo=TZ8)
        previous = latest - timedelta(days=2)
        with tempfile.TemporaryDirectory() as directory:
            conn = monitor.connect_db(Path(directory) / "monitor.db")
            self.assertFalse(monitor.slot_data_complete(conn, latest))
            monitor.upsert_run(conn, {"run_id": "old", "started_at": "2026-09-14T19:00:00+08:00", "finished_at": "2026-09-14T19:01:00+08:00", "slot_end": monitor.iso_time(previous), "window_start": "", "window_end": monitor.iso_time(previous), "window_hours": 24, "repo_total": 1, "repo_ok": 1, "repo_changed": 1, "repo_failed": 0, "exit_code": 0, "note": ""})
            conn.commit()
            selected, skipped = monitor._missing_catch_up_slots(conn, latest, 1)
            self.assertEqual([monitor.iso_time(item) for item in selected], ["2026-09-16T19:00:00+08:00"])
            self.assertEqual(skipped, 1)
            monitor.upsert_run(conn, {"run_id": "latest", "started_at": "2026-09-16T19:00:00+08:00", "finished_at": "2026-09-16T19:01:00+08:00", "slot_end": monitor.iso_time(latest), "window_start": "", "window_end": monitor.iso_time(latest), "window_hours": 24, "repo_total": 1, "repo_ok": 1, "repo_changed": 1, "repo_failed": 0, "exit_code": 0, "note": "补跑 1 个槽位，跳过 1 个更早的槽位"})
            conn.commit()
            self.assertEqual(conn.execute("SELECT note FROM runs WHERE run_id='latest'").fetchone()[0], "补跑 1 个槽位，跳过 1 个更早的槽位")
            self.assertFalse(monitor.slot_data_complete(conn, latest))
            record = monitor.DailyRecord(date="2026-09-16", repo="demo", repo_url="", branch="main", slot_end=monitor.iso_time(latest), window_start="", window_end=monitor.iso_time(latest), effective_paths=[], status="no_change", word_delta=0, text_only_delta=0, image_delta=0, sentence_delta=0, deleted_lines=0, matched_files=0)
            monitor.upsert_repo_daily(conn, record)
            conn.commit()
            self.assertTrue(monitor.slot_data_complete(conn, latest))


if __name__ == "__main__":
    unittest.main()
