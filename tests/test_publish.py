from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import monitor


REPORT_DATE = "2026-09-16"


def _report_fixture(root: Path) -> Path:
    reports = root / "reports"
    reports.mkdir()
    (reports / "latest.html").write_text("latest report", encoding="utf-8")
    (reports / f"{REPORT_DATE}.html").write_text("dated report", encoding="utf-8")
    return reports / f"{REPORT_DATE}.html"


def _publish_config(target: Path, **overrides: object) -> dict:
    publish = {
        "enabled": True,
        "method": "copy",
        "target": str(target),
        "files": ["latest", "dated"],
        "timeout_s": 180,
        "on_error": "warn",
        "extra_args": [],
    }
    publish.update(overrides)
    return {"publish": publish}


def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    ).stdout


def _git_pages_fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "pages.git"
    worktree = root / "pages"
    _git(root, "init", "--bare", str(remote))
    worktree.mkdir()
    _git(worktree, "init")
    _git(worktree, "checkout", "-b", "main")
    _git(worktree, "config", "user.email", "publish-tests@example.invalid")
    _git(worktree, "config", "user.name", "Publish Tests")
    _git(worktree, "remote", "add", "origin", str(remote))
    return worktree, remote


def _remote_bytes(root: Path, remote: Path, *args: str) -> bytes:
    return _git(root, "--git-dir", str(remote), *args)


class PublishTests(unittest.TestCase):
    def test_copy_publishes_latest_and_dated_with_identical_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "published"
            target.mkdir()
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(_publish_config(target), report, report_date=REPORT_DATE), 2)
            self.assertEqual((target / "latest.html").read_text(encoding="utf-8"), "latest report")
            self.assertEqual((target / f"{REPORT_DATE}.html").read_text(encoding="utf-8"), "dated report")
            self.assertIn("publish: ok 2 files ->", log_line.call_args.args[0])

    def test_files_latest_only_does_not_copy_dated_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "published"
            target.mkdir()
            with patch.object(monitor, "_monitor_log_line"):
                self.assertEqual(
                    monitor.publish_report(
                        _publish_config(target, files=["latest"]), report, report_date=REPORT_DATE
                    ),
                    1,
                )
            self.assertEqual([path.name for path in target.iterdir()], ["latest.html"])

    def test_disabled_publish_is_a_noop_without_output_or_target_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "published"
            target.mkdir()
            sentinel = target / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output), patch.object(monitor, "_run") as run, patch.object(
                monitor, "_monitor_log_line"
            ) as log_line:
                self.assertIsNone(
                    monitor.publish_report(
                        _publish_config(target, enabled=False), report, report_date=REPORT_DATE
                    )
                )
            self.assertEqual([path.name for path in target.iterdir()], ["sentinel.txt"])
            self.assertEqual(output.getvalue(), "")
            run.assert_not_called()
            log_line.assert_not_called()

    def test_copy_failure_warns_or_fails_according_to_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            missing_target = root / "does-not-exist"
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(
                    monitor.publish_report(
                        _publish_config(missing_target, on_error="warn"), report, report_date=REPORT_DATE
                    ),
                    0,
                )
                self.assertIn("publish: failed (exit 1):", log_line.call_args.args[0])
            with patch.object(monitor, "_monitor_log_line"):
                with self.assertRaises(monitor.MonitorError):
                    monitor.publish_report(
                        _publish_config(missing_target, on_error="fail"), report, report_date=REPORT_DATE
                    )

    def test_no_publish_flag_skips_enabled_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "published"
            target.mkdir()
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertIsNone(
                    monitor.publish_report(
                        _publish_config(target), report, report_date=REPORT_DATE, no_publish=True
                    )
                )
            self.assertEqual(list(target.iterdir()), [])
            log_line.assert_not_called()
            self.assertTrue(monitor.build_parser().parse_args(["run", "--no-publish"]).no_publish)
            self.assertTrue(monitor.build_parser().parse_args(["report", "--no-publish"]).no_publish)
            self.assertTrue(monitor.build_parser().parse_args(["backfill", "--days", "1", "--no-publish"]).no_publish)

    def test_check_config_prints_publish_status_and_flags_empty_enabled_target(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            output = StringIO()
            config = {"repos": [], "publish": {"enabled": True, "method": "copy", "target": ""}}
            with patch.object(monitor, "_data_dir", return_value=data_dir), redirect_stdout(output):
                self.assertEqual(monitor._check_config(config), 1)
            text = output.getvalue()
            self.assertIn("publish: enabled=true method=copy target=", text)
            self.assertIn("enabled=true 但 target 为空", text)

    def test_git_publishes_latest_to_local_bare_remote_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            latest_bytes = b"binary\r\nreport\x00\xff"
            report.parent.joinpath("latest.html").write_bytes(latest_bytes)
            worktree, remote = _git_pages_fixture(root)
            config = _publish_config(
                worktree,
                method="git",
                files=["latest"],
                remote="origin",
                branch="main",
            )
            with patch.object(monitor, "_monitor_log_line"):
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 1)
            self.assertEqual(_remote_bytes(root, remote, "show", "main:latest.html"), latest_bytes)
            self.assertEqual(_remote_bytes(root, remote, "rev-list", "--count", "main").strip(), b"1")

    def test_git_skips_commit_when_latest_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            worktree, remote = _git_pages_fixture(root)
            config = _publish_config(worktree, method="git", files=["latest"])
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 1)
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 0)
            self.assertEqual(_remote_bytes(root, remote, "rev-list", "--count", "main").strip(), b"1")
            self.assertIn("unchanged", log_line.call_args_list[-1].args[0])

    def test_git_push_failure_warns_or_fails_without_uncaught_warn_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            worktree, _remote = _git_pages_fixture(root)
            config = _publish_config(
                worktree, method="git", files=["latest"], remote="missing", retry_delays_s=[]
            )
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 0)
                self.assertIn("publish: failed", log_line.call_args.args[0])
            report.parent.joinpath("latest.html").write_text("changed", encoding="utf-8")
            with patch.object(monitor, "_monitor_log_line"):
                with self.assertRaises(monitor.MonitorError):
                    monitor.publish_report(
                        _publish_config(
                            worktree,
                            method="git",
                            files=["latest"],
                            remote="missing",
                            on_error="fail",
                            retry_delays_s=[],
                        ),
                        report,
                        report_date=REPORT_DATE,
                    )

    def test_git_commit_message_replaces_date_and_generated_at(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            worktree, _remote = _git_pages_fixture(root)
            config = _publish_config(
                worktree,
                method="git",
                files=["latest"],
                commit_message="report: {date} ({generated_at})",
            )
            with patch.object(monitor, "_monitor_log_line"):
                monitor.publish_report(
                    config,
                    report,
                    report_date=REPORT_DATE,
                    generated_at="2026-09-16 19:01:02+08:00",
                )
            self.assertEqual(
                _git(worktree, "log", "-1", "--format=%s").decode().strip(),
                "report: 2026-09-16 (2026-09-16 19:01:02+08:00)",
            )

    def test_publishers_array_runs_copy_and_git_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            copy_target = root / "caddy"
            copy_target.mkdir()
            worktree, remote = _git_pages_fixture(root)
            config = {
                "publish": {
                    "enabled": True,
                    "on_error": "warn",
                    "publishers": [
                        {"method": "copy", "target": str(copy_target), "files": ["latest"]},
                        {"method": "git", "target": str(worktree), "files": ["latest"]},
                    ],
                }
            }
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 2)
            self.assertEqual((copy_target / "latest.html").read_bytes(), (report.parent / "latest.html").read_bytes())
            self.assertEqual(_remote_bytes(root, remote, "show", "main:latest.html"), b"latest report")
            self.assertEqual(len(log_line.call_args_list), 2)
            self.assertIn("publish[copy]: ok", log_line.call_args_list[0].args[0])
            self.assertIn("publish[git]: ok", log_line.call_args_list[1].args[0])

    def test_publishers_failure_does_not_stop_later_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            good_target = root / "good"
            good_target.mkdir()
            config = {
                "publish": {
                    "enabled": True,
                    "on_error": "warn",
                    "publishers": [
                        {"method": "copy", "target": str(root / "missing")},
                        {"method": "copy", "target": str(good_target), "files": ["latest"]},
                    ],
                }
            }
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 1)
            self.assertTrue((good_target / "latest.html").exists())
            self.assertIn("publish[copy]: failed", log_line.call_args_list[0].args[0])
            self.assertIn("publish[copy]: ok", log_line.call_args_list[1].args[0])

    def test_legacy_single_publisher_syntax_keeps_unbracketed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "legacy"
            target.mkdir()
            with patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(_publish_config(target, files=["latest"]), report, report_date=REPORT_DATE), 1)
            self.assertTrue((target / "latest.html").exists())
            self.assertIn("publish: ok 1 files", log_line.call_args.args[0])
            self.assertNotIn("publish[", log_line.call_args.args[0])

    def test_retry_delays_are_normalized_and_can_be_disabled(self):
        normalize = monitor._normalize_publish_settings
        self.assertEqual(normalize({"retry_delays_s": [60, 180]})["retry_delays_s"], [60.0, 180.0])
        self.assertEqual(normalize({"retry_delays_s": ["30", -5, "x", 0, 7]})["retry_delays_s"], [30.0, 7.0])
        self.assertEqual(normalize({"retry_delays_s": []})["retry_delays_s"], [])
        self.assertEqual(normalize({"retry_delays_s": "garbage"})["retry_delays_s"], [60.0, 180.0])
        self.assertEqual(normalize({})["retry_delays_s"], [60.0, 180.0])
        self.assertEqual(
            len(normalize({"retry_delays_s": [1, 2, 3, 4, 5, 6, 7]})["retry_delays_s"]),
            monitor.MAX_PUBLISH_RETRIES,
        )

    def test_git_push_retries_transient_failure_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            worktree, remote = _git_pages_fixture(root)
            config = _publish_config(worktree, method="git", files=["latest"], retry_delays_s=[60, 180])
            real_git = monitor.git
            calls: list[str] = []
            sleeps: list[float] = []

            def flaky_git(repo_path, args, *, timeout=None):
                name = list(args)[0] if args else ""
                calls.append(name)
                if name == "push" and calls.count("push") <= 2:
                    return subprocess.CompletedProcess(
                        list(args), 128, "", "fatal: unable to access 'https://...': TLS handshake failed"
                    )
                return real_git(repo_path, args, timeout=timeout)

            with patch.object(monitor, "git", flaky_git), patch.object(
                monitor.time, "sleep", lambda seconds: sleeps.append(seconds)
            ), patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 1)
            self.assertEqual(calls.count("push"), 3)
            self.assertEqual(sleeps, [60.0, 180.0])
            self.assertEqual(_remote_bytes(root, remote, "show", "main:latest.html"), b"latest report")
            # retry wraps the push only: the publisher body runs once (no second diff/commit)
            self.assertEqual(calls.count("diff"), 1)
            self.assertEqual(calls.count("commit"), 1)
            self.assertEqual(len([c for c in log_line.call_args_list if "后重试" in c.args[0]]), 2)
            self.assertIn("publish: ok 1 files", log_line.call_args_list[-1].args[0])

    def test_git_push_retries_exhausted_reports_failure_without_hiding_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            worktree, remote = _git_pages_fixture(root)
            config = _publish_config(worktree, method="git", files=["latest"], retry_delays_s=[5, 10])
            sleeps: list[float] = []
            pushes: list[int] = []
            real_git = monitor.git

            def failing_git(repo_path, args, *, timeout=None):
                if list(args)[:1] == ["push"]:
                    pushes.append(1)
                    return subprocess.CompletedProcess(list(args), 128, "", "fatal: schannel: failed to receive handshake")
                return real_git(repo_path, args, timeout=timeout)

            with patch.object(monitor, "git", failing_git), patch.object(
                monitor.time, "sleep", lambda seconds: sleeps.append(seconds)
            ), patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(monitor.publish_report(config, report, report_date=REPORT_DATE), 0)
                self.assertIn("publish: failed", log_line.call_args_list[-1].args[0])
            self.assertEqual(len(pushes), 3)
            self.assertEqual(sleeps, [5.0, 10.0])
            # the commit stays local and the remote stays empty — failure is not masked as "unchanged"
            self.assertEqual(_git(worktree, "rev-list", "--count", "main").strip(), b"1")
            self.assertEqual(_remote_bytes(root, remote, "rev-list", "--all", "--count").strip(), b"0")

    def test_copy_publish_retries_transient_transfer_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = _report_fixture(root)
            target = root / "retry-copy"
            target.mkdir()
            real_copy = monitor._copy_bytes
            sleeps: list[float] = []
            attempts: list[int] = []

            def flaky_copy(source, destination):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("transient copy failure")
                return real_copy(source, destination)

            with patch.object(monitor, "_copy_bytes", flaky_copy), patch.object(
                monitor.time, "sleep", lambda seconds: sleeps.append(seconds)
            ), patch.object(monitor, "_monitor_log_line") as log_line:
                self.assertEqual(
                    monitor.publish_report(
                        _publish_config(target, files=["latest"], retry_delays_s=[1]), report, report_date=REPORT_DATE
                    ),
                    1,
                )
            self.assertEqual(len(attempts), 2)
            self.assertEqual(sleeps, [1.0])
            self.assertEqual((target / "latest.html").read_text(encoding="utf-8"), "latest report")
            self.assertIn("后重试", log_line.call_args_list[0].args[0])
            self.assertIn("publish: ok 1 files", log_line.call_args_list[-1].args[0])

    def test_check_config_prints_retry_delays(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            output = StringIO()
            config = {
                "repos": [],
                "publish": {"enabled": False, "method": "git", "target": "x", "retry_delays_s": [60, 180]},
            }
            with patch.object(monitor, "_data_dir", return_value=data_dir), redirect_stdout(output):
                monitor._check_config(config)
            self.assertIn("publish 重试: 60s/180s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
