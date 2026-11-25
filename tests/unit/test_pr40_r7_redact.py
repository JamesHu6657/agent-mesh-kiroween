"""PR #40 Linear r7: multi-line exact secrets ending in a newline must not leak at EOF
(P1), and a withheld over-long line holding a secret's last piece must not release that
piece in its kept head (P2). Batch (redact) and stream (StreamRedactor) agree."""

import unittest

from mesh.core.redact import LINE_CAP, UNBROKEN, StreamRedactor, redact


def stream(text, exact, step):
    r = StreamRedactor(list(exact))
    out = "".join(r.feed(text[i:i + step]) for i in range(0, len(text), step))
    return out + r.flush()


class TestTrailingNewlineSecret(unittest.TestCase):
    SECRET = "p4Q7w2X" + "\n" + "Z9r1V6m" + "\n"
    EXACT = [("MULTI_SECRET", SECRET)]

    def check(self, text):
        outs = [redact(text, self.EXACT)] + [stream(text, self.EXACT, s) for s in (1, 5, 4096)]
        for out in outs:
            self.assertNotIn("p4Q7w2X", out)
            self.assertNotIn("Z9r1V6m", out)
            self.assertIn("[REDACTED:MULTI_SECRET]", out)
        return outs

    def test_exact_text_at_eof(self) -> None:
        self.check(self.SECRET)

    def test_followed_by_more_output(self) -> None:
        for out in self.check("x " + "p4Q7w2X\nZ9r1V6m\nafter\n"):
            self.assertIn("after", out)

    def test_leading_newline_value(self) -> None:
        exact = [("LEAD", "\n" + "abc12345" + "\n" + "def67890")]
        for out in (redact("abc12345\ndef67890\n", exact), stream("abc12345\ndef67890\n", exact, 3)):
            self.assertNotIn("abc12345", out)
            self.assertNotIn("def67890", out)


class TestWithheldLastPiece(unittest.TestCase):
    EXACT = [("TWO_LINE", "alpha12" + "\n" + "beta34")]

    def test_kept_head_does_not_release_last_piece(self) -> None:
        text = "before alpha12\nbeta34 " + "x" * (LINE_CAP + 1) + "\n"
        outs = [redact(text, self.EXACT)] + [stream(text, self.EXACT, s) for s in (1000, 70000)]
        for out in outs:
            self.assertNotIn("beta34", out)
            self.assertNotIn("alpha12", out)
            self.assertIn(UNBROKEN, out)
            self.assertIn("before", out)

    def test_unrelated_withheld_line_keeps_safe_head(self) -> None:
        text = "hello there " + "y" * (LINE_CAP + 1) + "\n"
        out = redact(text, self.EXACT)
        self.assertIn("hello there", out)
        self.assertIn(UNBROKEN, out)


class TestSecretStartingOnWithheldLine(unittest.TestCase):
    """PR #40 r8: a multi-line value whose first piece ends a withheld line."""
    SECRET = "first77" + "\n" + "secnd77" + "\n" + "third88"
    EXACT = [("CONFIG_SECRET", SECRET)]

    def test_later_pieces_redacted(self) -> None:
        text = "x" * LINE_CAP + self.SECRET + "\n" + "after\n"
        outs = [redact(text, self.EXACT)] + [stream(text, self.EXACT, s) for s in (999, 70000)]
        for out in outs:
            for frag in ("first77", "secnd77", "third88"):
                self.assertNotIn(frag, out)
            self.assertIn(UNBROKEN, out)
            self.assertIn("after", out)

    def test_unrelated_lines_after_withheld_line_kept(self) -> None:
        text = "x" * (LINE_CAP + 5) + "\nsecnd77 is not alone\nplain\n"
        out = redact(text, self.EXACT)
        self.assertIn(" is not alone", out)  # only the first token is armed after a withheld line
        self.assertIn("plain", out)


if __name__ == "__main__":
    unittest.main()
