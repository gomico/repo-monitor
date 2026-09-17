from collections import Counter
from pathlib import Path
import json
import re
import tempfile
import unittest

import monitor


def _report_config() -> dict:
    return {
        "common_paths_filter": [],
        "schedule": {"window_hours": 24},
        "report": {"default_days": 7, "top_n_changed": 10, "title": "测试报表"},
        "repos": [],
    }


def _seed_003_fixture(conn: object, date: str = "2026-09-16") -> None:
    words = [12000, 11000] + list(range(1000, 1008)) + list(range(1, 18)) + [0] * 19
    for index, word in enumerate(words):
        repo = f"repo-{index:02d}"
        file_count = 16 if index < 6 else 15 if index < 28 else 0
        files = [
            monitor.FileChange(
                f"docs/{repo}/file-{file_index:02d}.md",
                None,
                1 if file_index == 0 else 0,
                word if file_index == 0 else 0,
                1 if file_index == 0 and index == 0 else 0,
                0,
                "[新增文件]" if file_index == 0 and index == 0 else "内容变更",
            )
            for file_index in range(file_count)
        ]
        status = "ok" if word else "no_change"
        monitor.upsert_repo_daily(
            conn,
            monitor.DailyRecord(
                date=date,
                repo=repo,
                repo_url=f"https://example.invalid/{repo}",
                branch="main",
                slot_end=f"{date}T19:00:00+08:00",
                window_start=f"{date}T18:00:00+08:00",
                window_end=f"{date}T19:00:00+08:00",
                effective_paths=[],
                status=status,
                word_delta=word,
                text_only_delta=word,
                image_delta=1 if index == 0 else 0,
                sentence_delta=1 if word else 0,
                deleted_lines=0,
                matched_files=file_count,
                base_commit="a" * 40,
                base_commit_time=f"{date}T18:00:00+08:00",
                base_commit_subject="baseline subject",
                tip_commit="b" * 40,
                commit_subject="latest subject",
                commit_time=f"{date}T19:00:00+08:00",
                generated_at=f"{date}T19:01:00+08:00",
                whole_word_delta=word,
                whole_matched_files=file_count,
                files=files,
            ),
        )


class ReportTests(unittest.TestCase):
    def test_html_numbers_have_no_thousands_separator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            output = root / "report.html"
            conn = monitor.connect_db(database)
            monitor.upsert_repo_daily(
                conn,
                monitor.DailyRecord(
                    date="2026-09-16", repo="large", repo_url="", branch="main",
                    slot_end="2026-09-16T19:00:00+08:00",
                    window_start="2026-09-15T19:00:00+08:00",
                    window_end="2026-09-16T19:00:00+08:00",
                    effective_paths=[], status="ok", word_delta=67715,
                    text_only_delta=67715, image_delta=0, sentence_delta=1,
                    deleted_lines=0, matched_files=1, whole_word_delta=67715,
                    whole_matched_files=1,
                    files=[monitor.FileChange("docs/a.md", None, 1, 67715, 0, 0, "内容变更")],
                ),
            )
            conn.commit()
            conn.close()
            path = monitor.generate_report(_report_config(), db_path=database, days=1, out_path=output, update_latest=False)
            content = path.read_text(encoding="utf-8")
            self.assertIn("67715", content)
            self.assertNotIn("67,715", content)

    def test_report_contains_003_dom_contract_and_repo_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            output = root / "report.html"
            conn = monitor.connect_db(database)
            _seed_003_fixture(conn)
            conn.commit()
            conn.close()

            content = monitor.generate_report(
                _report_config(), db_path=database, days=1, out_path=output, update_latest=False
            ).read_text(encoding="utf-8")
            entries = re.findall(r'<div class="entry" data-group data-name="[^"]+" data-word="[^"]+">', content)
            groups = re.findall(r'<section class="grp" data-grp="([^"]+)">', content)
            id_counts = Counter(re.findall(r'\bid="([^"]+)"', content))
            duplicate_ids = {value: count for value, count in id_counts.items() if count > 1}
            self.assertFalse(duplicate_ids, f"duplicate HTML ids: {duplicate_ids}")
            jump_groups = re.findall(r'data-jump-group="([^"]+)"', content)
            expanders = re.findall(
                r'<button class="tgl"[^>]*aria-expanded="false"[^>]*aria-controls="[^"]+">展开</button>',
                content,
            )
            toggle_ids = re.findall(r'data-toggle="([^"]+)"', content)
            aria_ids = re.findall(r'aria-controls="([^"]+)"', content)
            detail_ids = re.findall(r'<div class="detail" id="([^"]+)"', content)
            links = re.findall(r'<a class="name" href="([^"]+)" target="_blank" rel="noopener"', content)

            self.assertEqual(46, len(entries))
            self.assertEqual(["大量", "中等", "少量", "无变化", "失败"], groups)
            self.assertEqual(["大量", "中等", "少量", "无变化", "失败"], jump_groups)
            for group in jump_groups:
                self.assertRegex(content, rf'<section class="grp" data-grp="{re.escape(group)}">')
            self.assertEqual(46, len(expanders))
            self.assertEqual(46, len(toggle_ids))
            self.assertEqual(46, len(set(toggle_ids)))
            self.assertEqual(toggle_ids, aria_ids)
            self.assertEqual(set(toggle_ids), set(detail_ids))
            self.assertTrue(all(content.count(f'id="{detail_id}"') == 1 for detail_id in toggle_ids))
            group_blocks = re.findall(r'<section class="grp" data-grp="[^"]+">.*?</section>', content, re.S)
            self.assertEqual(5, len(group_blocks))
            populated_group_blocks = [block for block in group_blocks if 'data-toggle="' in block]
            self.assertEqual(4, len(populated_group_blocks))
            for group_block in populated_group_blocks:
                group_toggle = re.search(r'data-toggle="([^"]+)"', group_block).group(1)
                group_detail = re.search(r'<div class="detail" id="([^"]+)"', group_block).group(1)
                self.assertEqual(group_toggle, group_detail)
            self.assertEqual(46, len(links))
            self.assertTrue(all(url.startswith("http") for url in links))
            self.assertEqual(46, content.count('target="_blank"'))
            self.assertEqual(426, content.count('<tr><td class="p"'))
            self.assertEqual(28, content.count('<tr class="subtotal">'))
            self.assertIn('<select id="daySel">', content)
            self.assertIn('<select id="sortSel" aria-label="排序方式">', content)
            self.assertIn('<option value="word" selected>按变化量降序</option>', content)
            self.assertIn('<option value="name">按名称升序(档位内)</option>', content)
            self.assertIn('<div class="lbl">近 7 天变化量排行（前 10）</div><ol class="rank">', content)
            self.assertNotIn('<details class="rankbox">', content)
            self.assertNotIn('前 10 名（默认收起）', content)
            self.assertIn("大量", content)
            self.assertIn("中等", content)
            self.assertNotIn("巨量", content)
            self.assertNotIn("可观", content)
            self.assertIn("大量 ≥ 10,000 / 中等 1,000–9,999 / 少量 1–999 / 无变化 0 / 失败", content)
            self.assertIn('data-all="open"', content)
            self.assertIn('data-all="close"', content)
            self.assertIn('aria-expanded="false" aria-controls=', content)
            self.assertIn('<th>文件</th><th>状态</th><th class="n">句</th><th class="n">新增字</th>', content)
            self.assertIn('<td>新增文件</td>', content)
            self.assertNotIn('<td>[新增文件]</td>', content)
            self.assertIn('class="spark-baseline"', content)
            self.assertIn('--rail:264px', content)
            self.assertIn('.side{position:sticky', content)
            self.assertIn('.grpbody{min-width:0;overflow-x:auto', content)
            self.assertIn('--entry-min:638px', content)
            self.assertIn('--entry-min:514px', content)
            self.assertIn("btn.closest('[data-group]')", content)
            self.assertIn('white-space:nowrap', content)
            self.assertIn('@media print', content)
            self.assertIn('@media (prefers-reduced-motion: reduce)', content)
            self.assertIn('a.name:focus-visible', content)
            self.assertIn('a.name:hover{color:var(--accent);text-decoration-color:var(--accent)}', content)
            styles = "\n".join(re.findall(r"<style>(.*?)</style>", content, re.S))
            scripts = "\n".join(re.findall(r"<script>(.*?)</script>", content, re.S))
            self.assertNotRegex(styles + scripts, r"https?://")

    def test_report_restores_safe_config_json_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            output = root / "report.html"
            conn = monitor.connect_db(database)
            _seed_003_fixture(conn)
            conn.commit()
            conn.close()

            config = _report_config()
            config["repos_root"] = "/mnt/private/repos"
            config["python_exe"] = r"D:\Python\python.exe"
            config["counter_script"] = "/home/alice/counter.py"
            config["common_paths_filter"] = ["docs/<shared>"]
            config["repos"] = [
                {
                    "name": "<repo>",
                    "branch": "main",
                    "enabled": True,
                    "paths_mode": "replace",
                    "paths_filter": ["/home/alice/docs", "docs/<local>"],
                }
            ]

            content = monitor.generate_report(
                config, db_path=database, days=1, out_path=output, update_latest=False
            ).read_text(encoding="utf-8")
            details_match = re.search(r'<details class="meta-block">(.*?)</details>', content, re.S)
            self.assertIsNotNone(details_match)
            details = details_match.group(0)
            self.assertIn("<summary>本次采集参数（JSON）</summary>", details)
            details_tag = re.search(r'<details class="meta-block"[^>]*>', details).group(0)
            self.assertNotIn(" open", details_tag)
            json_text = re.search(r'<pre class="config-json">(.*?)</pre>', details, re.S).group(1)
            payload = json.loads(json_text)
            self.assertEqual({"report", "days", "repos"}, set(payload.keys()))
            self.assertEqual("<repo>", payload["repos"][0]["name"])
            self.assertEqual("[本机绝对路径已隐藏]、docs/<local>", payload["repos"][0]["effective_paths"])
            self.assertIn(r"\u003c", json_text)
            self.assertNotRegex(json_text, r"/mnt/|D:\\|/home/")
            self.assertEqual(46, payload["days"][0]["repos_total"])
            self.assertEqual(1, content.count('<details class="meta-block">'))
            self.assertLess(content.rfind('<div class="legendbar">'), content.index('<details class="meta-block">'))
            self.assertIn("max-height:360px", content)

    def test_report_keeps_multi_day_switch_and_heat_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            output = root / "report.html"
            conn = monitor.connect_db(database)
            _seed_003_fixture(conn, "2026-09-15")
            _seed_003_fixture(conn, "2026-09-16")
            conn.commit()
            conn.close()

            content = monitor.generate_report(
                _report_config(), db_path=database, days=2, out_path=output, update_latest=False
            ).read_text(encoding="utf-8")
            self.assertEqual(1, content.count('<select id="daySel">'))
            self.assertEqual(2, len(re.findall(r'<section class="day(?: active)?" id="day-', content)))
            self.assertEqual(1, content.count('<section class="day active" id="day-'))
            self.assertIn('data-side-day="2026-09-15"', content)
            self.assertIn('data-side-day="2026-09-16"', content)
            self.assertIn("已累积 2 天", content)
            self.assertIn("function showDay(v)", content)

    def test_empty_detail_messages_cover_status_and_path_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.db"
            output = root / "report.html"
            conn = monitor.connect_db(database)
            records = [
                monitor.DailyRecord(
                    date="2026-09-16", repo="no-change", repo_url="", branch="main",
                    window_start="", window_end="", effective_paths=[], status="no_change",
                    word_delta=0, whole_word_delta=0, matched_files=0,
                ),
                monitor.DailyRecord(
                    date="2026-09-16", repo="filtered", repo_url="", branch="main",
                    window_start="", window_end="", effective_paths=["docs/zh"], status="ok",
                    word_delta=0, whole_word_delta=156, whole_matched_files=8, matched_files=0,
                ),
                monitor.DailyRecord(
                    date="2026-09-16", repo="non-doc", repo_url="", branch="main",
                    window_start="", window_end="", effective_paths=["docs/zh"], status="ok",
                    word_delta=0, whole_word_delta=0, whole_matched_files=0, matched_files=0,
                ),
                monitor.DailyRecord(
                    date="2026-09-16", repo="failed", repo_url="", branch="main",
                    window_start="", window_end="", effective_paths=[], status="error",
                    reason="fetch 失败：网络不可用",
                ),
            ]
            for record in records:
                monitor.upsert_repo_daily(conn, record)
            conn.commit()
            conn.close()
            content = monitor.generate_report(
                _report_config(), db_path=database, days=1, out_path=output, update_latest=False
            ).read_text(encoding="utf-8")
            self.assertIn("该分支在窗口起点之后没有新提交（基线 = 最新提交）。", content)
            self.assertIn('窗口内有新提交，但生效路径 [&quot;docs/zh&quot;] 下没有文件级中文变更（全仓口径 156 字 / 8 个文件）。', content)
            self.assertIn('若该仓库文档不在此路径下，请在 monitor.config.json 里为它设置 paths_filter，或改成 paths_mode: "replace"。', content)
            self.assertIn("窗口内的提交只改动了非文档内容，生效路径下与全仓都没有中文字数变化。", content)
            self.assertIn("fetch 失败：网络不可用", content)
            self.assertIn("采集失败，请手动查看", content)


if __name__ == "__main__":
    unittest.main()
