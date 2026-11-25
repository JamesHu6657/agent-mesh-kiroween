"""PR #38 Linear review P3: text tables measure and cut by user-perceived character, so
emoji sequences (VS16, ZWJ, skin tones, flags) count 2 columns and are never split."""

import unittest

from mesh.cli.render import align_rows, clusters, display_width, fit, pad


class TestGraphemeWidths(unittest.TestCase):
    def test_widths(self) -> None:
        cases = {
            "\u2764\ufe0f": 2,                     # red heart, emoji presentation
            "\u2764": 1,                            # text heart
            "\u2764\ufe0e": 1,                     # explicit text presentation
            "\U0001F600": 2,                        # grinning face (wide)
            "\U0001F44D\U0001F3FD": 2,              # thumbs up, skin tone
            "\U0001F468\u200d\U0001F469\u200d\U0001F467": 2,  # family ZWJ
            "\U0001F3F3\ufe0f\u200d\U0001F308": 2,  # rainbow flag
            "\U0001F1E8\U0001F1F3": 2,              # flag CN (regional pair)
            "1\ufe0f\u20e3": 2,                    # keycap
            "e\u0301": 1,                          # combining accent
            "\u4e2d\u6587": 4,                     # CJK
            "abc": 3,
            "x\u200d": 1,                          # stray ZWJ is zero width (PR #38 re-review)
            "\u4fee\u200d\u590d": 4,               # CJK around ZWJ keeps both widths
            "a\u200db": 2,
            "\u00a9\u200d": 1,                     # narrow pictographic + dangling ZWJ
            "\u2764\u200d": 1,                     # text heart + dangling ZWJ
            "\u2764\u200d\u2764": 2,               # two text hearts joined -> one emoji
            "\U0001F44D\u200d": 2,                  # trailing ZWJ after emoji
            "\u2764\ufe0f\u200d\U0001F525": 2,     # heart on fire (ZWJ after VS16)
            "": 0,
        }
        for s, w in cases.items():
            with self.subTest(s=s):
                self.assertEqual(display_width(s), w)

    def test_clusters_keep_sequences_whole(self) -> None:
        fam = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        self.assertEqual(clusters("a" + fam + "\u2764\ufe0f" + "\U0001F1E8\U0001F1F3\U0001F1FA\U0001F1F8"),
                         ["a", fam, "\u2764\ufe0f", "\U0001F1E8\U0001F1F3", "\U0001F1FA\U0001F1F8"])
        self.assertEqual("".join(clusters("x\u200d")), "x\u200d")  # trailing ZWJ, no crash

    def test_fit_cuts_emoji_runs_without_splitting(self) -> None:
        heart = "\u2764\ufe0f"
        out = fit(heart * 30, 48)
        self.assertLessEqual(display_width(out), 48)
        self.assertTrue(out.endswith("..."))
        body = out[:-3]
        self.assertEqual(body, heart * (len(body) // 2))  # whole hearts only
        fam = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        out = fit(fam * 10, 9)
        self.assertEqual(out, fam * 3 + "...")
        self.assertEqual(fit("ok", 48), "ok")
        out = fit("a\u200d" * 60, 48)  # non-emoji ZWJ text is cut by its real width
        self.assertTrue(out.endswith("..."))
        self.assertLessEqual(display_width(out), 48)
        self.assertEqual(clusters("\u4fee\u200d\u590d"), ["\u4fee\u200d", "\u590d"])

    def test_alignment_with_emoji(self) -> None:
        rows = [["id", "title"], ["t1", "\u2764\ufe0f fix"], ["t2", "plain"]]
        lines = align_rows([r + ["end"] for r in rows])
        self.assertEqual(len({display_width(l[:l.index("end")]) for l in lines}), 1)
        self.assertEqual(display_width(pad("\u2764\ufe0f", 4)), 4)


if __name__ == "__main__":
    unittest.main()
