from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

import monitor


class DatabaseTests(unittest.TestCase):
    def test_repo_daily_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = monitor.connect_db(Path(directory) / "monitor.db")
            record = monitor.DailyRecord(
                date="2026-09-15",
                repo="demo",
                repo_url="https://example.invalid/demo",
                branch="main",
                slot_end="2026-09-15T19:00:00+08:00",
                window_start="2026-09-14T19:00:00+08:00",
                window_end="2026-09-15T19:00:00+08:00",
                effective_paths=["docs/zh"],
                status="ok",
                word_delta=12,
                text_only_delta=12,
                image_delta=0,
                sentence_delta=2,
                deleted_lines=1,
                matched_files=1,
                base_commit="a" * 40,
                base_commit_time="2026-09-14T18:00:00+08:00",
                base_commit_subject="基线提交",
                tip_commit="b" * 40,
                generated_at="2026-09-15T19:00:01+08:00",
                whole_word_delta=20,
                whole_matched_files=2,
                files=[monitor.FileChange("docs/zh/a.md", None, 2, 12, 0, 1, "内容变更")],
            )
            monitor.upsert_repo_daily(conn, record)
            record.word_delta = 15
            record.files[0].added_chars = 15
            monitor.upsert_repo_daily(conn, record)
            conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repo_daily").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repo_daily_files").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT word_delta FROM repo_daily").fetchone()[0], 15)
            self.assertEqual(conn.execute("SELECT slot_end FROM repo_daily").fetchone()[0], "2026-09-15T19:00:00+08:00")

    def test_old_database_migrates_and_null_base_metadata_renders_as_sha_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            raw = sqlite3.connect(database)
            raw.executescript(
                """
                CREATE TABLE repo_daily (
                  date TEXT NOT NULL, repo TEXT NOT NULL, repo_url TEXT, branch TEXT,
                  slot_end TEXT, window_start TEXT, window_end TEXT,
                  word_delta INTEGER, text_only_delta INTEGER, image_delta INTEGER,
                  sentence_delta INTEGER, deleted_lines INTEGER, matched_files INTEGER,
                  effective_paths TEXT, base_commit TEXT, tip_commit TEXT,
                  commit_subject TEXT, commit_time TEXT, status TEXT, reason TEXT,
                  generated_at TEXT, duration_s REAL, run_id TEXT, whole_word_delta INTEGER,
                  PRIMARY KEY (date, repo)
                );
                INSERT INTO repo_daily (
                  date, repo, repo_url, branch, slot_end, window_start, window_end,
                  word_delta, text_only_delta, image_delta, sentence_delta, deleted_lines,
                  matched_files, effective_paths, base_commit, tip_commit, commit_subject,
                  commit_time, status, generated_at, duration_s, run_id, whole_word_delta
                ) VALUES (
                  '2026-09-16', 'legacy', '', 'main', '2026-09-16T19:00:00+08:00', '', '',
                  5, 5, 0, 1, 0, 1, '[]',
                  'abcdef1234567890abcdef1234567890abcdef12',
                  'fedcba1234567890fedcba1234567890fedcba12',
                  '!1038 merge 26.2.0_version_update into master',
                  '2026-09-14T19:29:46+08:00', 'ok', '2026-09-16T19:01:00+08:00', 1.0,
                  'legacy-run', 5
                );
                """
            )
            raw.commit()
            raw.close()

            conn = monitor.connect_db(database)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(repo_daily)")}
            self.assertIn("base_commit_time", columns)
            self.assertIn("base_commit_subject", columns)
            self.assertIn("whole_matched_files", columns)
            conn.execute(
                """
                INSERT INTO repo_daily_files
                  (date, repo, path, changed_sentences, added_chars, added_images, deleted_lines, state)
                VALUES ('2026-09-16', 'legacy', 'docs/zh/a.md', 1, 5, 0, 0, '内容变更')
                """
            )
            conn.commit()
            conn.close()

            output = root / "report.html"
            path = monitor.generate_report(
                {"common_paths_filter": [], "schedule": {"window_hours": 24}, "report": {"default_days": 7, "title": "旧库"}, "repos": []},
                db_path=database,
                days=1,
                out_path=output,
                update_latest=False,
            )
            content = path.read_text(encoding="utf-8")
            base_cell = re.search(r'<div class="dkv">基线 (.*?)</div>', content, re.S).group(1)
            self.assertIn("abcdef1", base_cell)
            self.assertNotIn("09-14 19:29", base_cell)
            self.assertNotIn("!1038 merge 26.2.0_version_update into master", base_cell)


if __name__ == "__main__":
    unittest.main()
