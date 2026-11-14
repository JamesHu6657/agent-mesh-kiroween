"""Sakana review of public PR #4, CLI half, P3-2: text tables truncate and pad by display
width, so East Asian wide characters (CJK, 2 columns each) keep the columns aligned."""

import unittest

from mesh.cli.commands.inbox import inbox_table
from mesh.cli.commands.task import task_list_table
from mesh.cli.render import display_width, fit, pad, render_table


def col_starts(line: str, header: str, names) -> list:
    """Display column where each header name's cell starts in ``line``."""
    starts = []
    for name in names:
        idx = header.index(name)
        starts.append(display_width(header[:idx]))
    return starts


class TestDisplayWidth(unittest.TestCase):
    def test_widths(self) -> None:
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("修复登录"), 8)
        self.assertEqual(display_width("ＡＢ"), 4)            # fullwidth
        self.assertEqual(display_width("e\u0301"), 1)        # combining mark
        self.assertEqual(display_width("ｶﾞ"), 2)             # halfwidth katakana stays 1 each

    def test_fit_and_pad(self) -> None:
        self.assertEqual(fit("短", 10), "短")
        cut = fit("修" * 40, 48)
        self.assertTrue(cut.endswith("..."))
        self.assertLessEqual(display_width(cut), 48)
        self.assertEqual(display_width(cut), 47)  # 22 wide chars + "...": never splits a wide char
        self.assertEqual(fit("x" * 60, 48), "x" * 45 + "...")
        self.assertEqual(display_width(pad("中文", 6)), 6)


class TestTablesAlignWithCJK(unittest.TestCase):
    def assert_aligned(self, lines, cells_per_row) -> None:
        """Every row's cells start at the same display columns as the header's."""
        header = lines[0]
        names = header.split()
        expected = col_starts(header, header, names)
        for line, cells in zip(lines[1:], cells_per_row):
            got, pos = [], 0
            for cell in cells:
                i = line.index(cell, pos)
                got.append(display_width(line[:i]))
                pos = i + len(cell)
            self.assertEqual(got, expected[:len(got)], line)

    def test_task_list_title_truncated_by_display_width(self) -> None:
        long_cjk = "实现登录页面的国际化支持并修复移动端布局错位问题以及补充单元测试覆盖率报告"  # 37 wide chars
        items = [{"id": "tsk_1", "status": "queued", "title": long_cjk, "attempt": 0, "max_attempts": 3},
                 {"id": "tsk_2", "status": "running", "title": "short", "attempt": 1, "max_attempts": 3}]
        out = task_list_table(items, now=0).splitlines()
        title = out[1].split("  ")[-1]
        self.assertLessEqual(display_width(title), 48)
        self.assertTrue(title.endswith("..."))

    def test_inbox_columns_after_a_cjk_title_stay_aligned(self) -> None:
        items = [
            {"seq": 1, "id": "msg_a", "type": "task.failed", "status": "delivered", "project_id": "prj_aaaaaaaa11112222",
             "payload": {"task_id": "tsk_a", "title": "修复登录", "reason": "tests_failed"}},
            {"seq": 2, "id": "msg_b", "type": "task.failed", "status": "delivered", "project_id": "prj_aaaaaaaa11112222",
             "payload": {"task_id": "tsk_b", "title": "fix login page", "reason": "no_response"}},
        ]
        lines = inbox_table(items).splitlines()
        self.assert_aligned(lines, [["1", "msg_a", "task.failed", "delivered", "prj_11112222", "tsk_a", "修复登录",
                                     "tests_failed"],
                                    ["2", "msg_b", "task.failed", "delivered", "prj_11112222", "tsk_b",
                                     "fix login page", "no_response"]])
        reason_col = [display_width(l[:l.index(r)]) for l, r in zip(lines[1:], ("tests_failed", "no_response"))]
        self.assertEqual(reason_col[0], reason_col[1])

    def test_generic_table_aligns_cjk(self) -> None:
        lines = render_table([{"id": "a", "name": "中文名字", "status": "ok"},
                              {"id": "b", "name": "ascii", "status": "ok"}])
        cols = [display_width(l[:l.rindex("ok")]) for l in lines[1:]]
        self.assertEqual(cols[0], cols[1])


if __name__ == "__main__":
    unittest.main()
