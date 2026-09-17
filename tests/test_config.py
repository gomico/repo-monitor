import json
from pathlib import Path
import tempfile
import unittest

import monitor


class ConfigTests(unittest.TestCase):
    def test_load_config_applies_shallow_local_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            local = root / "monitor.config.local.json"
            main.write_text(
                json.dumps(
                    {
                        "repos_root": "/base",
                        "runtime": {"concurrency": 4, "keep_raw_days": 30},
                        "report": {"title": "base"},
                    }
                ),
                encoding="utf-8",
            )
            local.write_text(
                json.dumps({"repos_root": "/local", "runtime": {"concurrency": 1}}),
                encoding="utf-8",
            )
            config = monitor.load_config(main, local)
            self.assertEqual(config["repos_root"], "/local")
            self.assertEqual(config["runtime"]["concurrency"], 1)
            self.assertEqual(config["runtime"]["keep_raw_days"], monitor.DEFAULT_RUNTIME["keep_raw_days"])
            self.assertEqual(config["report"]["title"], "base")

    def test_only_shared_file_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            main.write_text(json.dumps({"repos_root": "/base"}), encoding="utf-8")
            (root / "monitor.config.local.json").write_text(json.dumps({"repos_root": "/shared"}), encoding="utf-8")
            self.assertEqual(monitor.load_config(main)["repos_root"], "/shared")

    def test_only_current_platform_file_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            main.write_text(json.dumps({"repos_root": "/base"}), encoding="utf-8")
            platform = "windows" if monitor.os.name == "nt" else "linux"
            (root / f"monitor.config.local.{platform}.json").write_text(
                json.dumps({"repos_root": "/platform"}), encoding="utf-8"
            )
            self.assertEqual(monitor.load_config(main)["repos_root"], "/platform")

    def test_platform_file_overrides_shared_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            main.write_text(json.dumps({"repos_root": "/base", "python_exe": "base-python"}), encoding="utf-8")
            (root / "monitor.config.local.json").write_text(
                json.dumps({"repos_root": "/shared", "python_exe": "shared-python"}), encoding="utf-8"
            )
            platform = "windows" if monitor.os.name == "nt" else "linux"
            (root / f"monitor.config.local.{platform}.json").write_text(
                json.dumps({"repos_root": "/platform"}), encoding="utf-8"
            )
            config = monitor.load_config(main)
            self.assertEqual(config["repos_root"], "/platform")
            self.assertEqual(config["python_exe"], "shared-python")

    def test_publish_local_overlay_keeps_base_git_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            local = root / "monitor.config.local.json"
            main.write_text(
                json.dumps(
                    {
                        "publish": {
                            "enabled": True,
                            "method": "git",
                            "target": r"D:\repos\local\repo-monitor-reports",
                            "remote": "origin",
                            "branch": "main",
                        }
                    }
                ),
                encoding="utf-8",
            )
            local.write_text(json.dumps({"publish": {"target": "/mnt/d/repos/local/repo-monitor-reports"}}), encoding="utf-8")
            publish = monitor.load_config(main, local)["publish"]
            self.assertEqual(publish["method"], "git")
            self.assertEqual(publish["target"], "/mnt/d/repos/local/repo-monitor-reports")
            self.assertEqual(publish["remote"], "origin")

    def test_other_platform_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "monitor.config.json"
            main.write_text(json.dumps({"repos_root": "/base"}), encoding="utf-8")
            other = "linux" if monitor.os.name == "nt" else "windows"
            (root / f"monitor.config.local.{other}.json").write_text(
                json.dumps({"repos_root": "/wrong-platform"}), encoding="utf-8"
            )
            self.assertEqual(monitor.load_config(main)["repos_root"], "/base")

    def test_effective_paths_union_and_replace(self):
        config = {"common_paths_filter": ["docs/zh", "shared"]}
        self.assertEqual(
            monitor.effective_paths(config, {"paths_filter": ["skills"], "paths_mode": "union"}),
            ["docs/zh", "shared", "skills"],
        )
        self.assertEqual(
            monitor.effective_paths(config, {"paths_filter": ["skills", "docs/zh"], "paths_mode": "replace"}),
            ["skills", "docs/zh"],
        )


if __name__ == "__main__":
    unittest.main()
