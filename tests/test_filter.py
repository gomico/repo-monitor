import csv
from pathlib import Path
import tempfile
import unittest

import monitor


def _rows(prefix: str, count: int, sentences: int, chars: int, deleted: int):
    for index in range(count):
        yield [
            f"{prefix}/file-{index}.md",
            sentences - (count - 1) if index == 0 else 1,
            chars - (count - 1) if index == 0 else 1,
            0,
            deleted - (count - 1) if index == 0 else 1,
            "内容变更",
        ]


class FilterTests(unittest.TestCase):
    def test_fixture_b_totals_are_recomputed_from_file_rows(self):
        rows = list(_rows("docs/zh", 43, 325, 7203, 248))
        rows += list(_rows("skills", 2, 2, 59, 2))
        rows += list(_rows("other", 15, 28, 793, 29))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(["文件 (old - new)", "修改句数", "新增字数", "新增图片数", "删除行数", "状态"])
                writer.writerows(rows)
                writer.writerow(["TOTAL", 355, 8055, 0, 279, ""])
            whole = monitor.parse_summary_csv(path, [])
            docs = monitor.parse_summary_csv(path, ["docs/zh"])
            union = monitor.parse_summary_csv(path, ["docs/zh", "skills"])
            self.assertEqual((whole.matched_files, whole.totals.changed_sentences, whole.totals.added_chars, whole.totals.added_images, whole.totals.deleted_lines), (60, 355, 8055, 0, 279))
            self.assertEqual((docs.matched_files, docs.totals.changed_sentences, docs.totals.added_chars, docs.totals.added_images, docs.totals.deleted_lines), (43, 325, 7203, 0, 248))
            self.assertEqual((union.matched_files, union.totals.changed_sentences, union.totals.added_chars, union.totals.added_images, union.totals.deleted_lines), (45, 327, 7262, 0, 250))


if __name__ == "__main__":
    unittest.main()
