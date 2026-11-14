"""Blind Linear review of main ffa471e: unit-level fixes (batch 2).

- runwrap redacted line by line, so a PEM private key (one secret over many lines)
  reached the run logs; output without newlines was read whole (memory) and never
  touched activity; a log write failure killed the capture thread and blocked the child.
- meshd.log took client fields unredacted and unescaped and never rotated.
"""

import io
import json
import os
import shutil
import tempfile
import threading
import unittest

from mesh import runwrap
from mesh.core import logfile
from mesh.core.redact import StreamRedactor

TOKEN = "gh" + "p_" + "A1b2" * 9
KEY_BODY = "QUJD" * 13  # base64-looking, built at runtime (not a real key)
PEM = ("-----BEGIN " + "PRIVATE KEY-----\n" + (KEY_BODY + "\n") * 3 + "-----END " + "PRIVATE KEY-----\n")


class TestStreamRedactor(unittest.TestCase):
    def run_stream(self, text: str, step: int, exact=()) -> str:
        r = StreamRedactor(list(exact))
        out = "".join(r.feed(text[i:i + step]) for i in range(0, len(text), step))
        return out + r.flush()

    def test_pem_split_across_lines_and_chunks(self) -> None:
        for step in (1, 7, 64, 10_000):
            out = self.run_stream("before\n" + PEM + "after\n", step)
            self.assertNotIn(KEY_BODY, out, step)
            self.assertIn("before", out)
            self.assertIn("after", out)

    def test_exact_value_split_across_chunks(self) -> None:
        secret = "s3cr3t-value-" + "x" * 20
        out = self.run_stream("a " + secret + " b\n", 5, [("MY_SECRET", secret)])
        self.assertNotIn(secret, out)

    def test_no_newline_output_is_released_bounded(self) -> None:
        # PR #40 r5: a line past LINE_CAP is withheld whole (fail closed), memory bounded.
        from mesh.core.redact import LINE_CAP, UNBROKEN

        r = StreamRedactor([])
        out = []
        for _ in range(64):
            out.append(r.feed(("y" * 63 + " ") * 1024))
            self.assertLessEqual(len(r.buf), LINE_CAP)
        out.append(r.feed("\nnext\n"))
        self.assertEqual("".join(out), ("y" * 63 + " ") * 4 + UNBROKEN + "\n[REDACTED]\n")
        self.assertEqual(r.withheld, 1)
        # One unbroken token-like run: withheld (redacted), still bounded.
        r = StreamRedactor([])
        for _ in range(64):
            r.feed("y" * 65536)
        self.assertLess(len(r.buf), 200 * 1024)

    def test_unterminated_key_is_cut(self) -> None:
        out = self.run_stream("-----BEGIN " + "PRIVATE KEY-----\n" + (KEY_BODY + "\n") * 10, 13)
        self.assertNotIn(KEY_BODY, out)


class TestPumpStream(unittest.TestCase):
    def setUp(self) -> None:
        self.d = tempfile.mkdtemp()
        self.activity = os.path.join(self.d, "activity")
        open(self.activity, "w").close()

    def tearDown(self) -> None:
        shutil.rmtree(self.d, ignore_errors=True)

    def pump(self, data: bytes, log_path: str, cap: int = 10 * 1024 * 1024, errors=None) -> None:
        r, w = os.pipe()

        def writer() -> None:
            with os.fdopen(w, "wb") as f:
                f.write(data)

        t = threading.Thread(target=writer)
        t.start()
        with os.fdopen(r, "rb") as pipe:
            args = [pipe, log_path, [], self.activity, cap]
            if errors is not None:
                args.append(errors)
            runwrap._pump_stream(*args)
        t.join(10)
        self.assertFalse(t.is_alive(), "child writer blocked: the pipe was not drained")

    def test_pem_redacted_in_log(self) -> None:
        log = os.path.join(self.d, "stdout.log")
        self.pump(("start\n" + PEM + "end " + TOKEN + "\n").encode(), log)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(KEY_BODY, text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("end", text)

    def test_no_newline_output_capped(self) -> None:
        log = os.path.join(self.d, "stdout.log")
        self.pump(b"z" * (4 * 1024 * 1024), log, cap=1024 * 1024)
        self.assertLess(os.path.getsize(log), 1024 * 1024 + 4096)

    def test_capture_failure_recorded_and_drained(self) -> None:
        errors = []
        self.pump(b"q" * (2 * 1024 * 1024), os.path.join(self.d, "missing", "stdout.log"), errors=errors)
        self.assertTrue(errors)


class TestLogFile(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)

    def test_newline_escaped_and_redacted(self) -> None:
        logfile.append_line(self.home, "req=a\nFORGED line " + TOKEN)
        with open(os.path.join(self.home, "logs", "meshd.log"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn(TOKEN, lines[0])

    def test_rotation(self) -> None:
        old = logfile.ROTATE_BYTES
        logfile.ROTATE_BYTES = 1000
        try:
            for i in range(200):
                logfile.append_line(self.home, f"line {i} " + "x" * 50)
        finally:
            logfile.ROTATE_BYTES = old
        names = sorted(os.listdir(os.path.join(self.home, "logs")))
        self.assertIn("meshd.log.1", names)
        self.assertNotIn(f"meshd.log.{logfile.KEEP + 1}", names)
        self.assertLess(os.path.getsize(os.path.join(self.home, "logs", "meshd.log")), 1100)


if __name__ == "__main__":
    unittest.main()


class TestStreamRedactorR1(unittest.TestCase):
    """PR #40 r1: forced cuts and multiline exact values."""

    def test_forced_cut_never_splits_a_pattern_token(self) -> None:
        from mesh.core.redact import StreamRedactor

        for pos in range(65860, 65912, 3):
            text = ("word " * 30000)[:pos] + TOKEN + " " + ("more " * 20000)
            for step in (70000,):
                r = StreamRedactor([])
                out = "".join(r.feed(text[i:i + step]) for i in range(0, len(text), step)) + r.flush()
                self.assertNotIn("A1b2A1b2", out, (pos, step))

    def test_multiline_exact_value_split_mid_chunk(self) -> None:
        from mesh.core.redact import StreamRedactor

        secret = "first-part-of-secret\nsecond-part-of-secret"
        text = "pre " + secret + " post\nnext line\n"
        for step in range(1, len(text) + 1):
            r = StreamRedactor([("MULTI_SECRET", secret)])
            out = "".join(r.feed(text[i:i + step]) for i in range(0, len(text), step)) + r.flush()
            self.assertNotIn("first-part-of-secret", out, step)
            self.assertNotIn("second-part-of-secret", out, step)


class TestStreamRedactorR2(unittest.TestCase):
    """PR #40 r2: forced cuts never split a contextual pattern."""

    def run_chunks(self, text: str, step: int = 65536) -> str:
        r = StreamRedactor([])
        return "".join(r.feed(text[i:i + step]) for i in range(0, len(text), step)) + r.flush()

    def test_bearer_prefix_and_body_across_forced_cut(self) -> None:
        text = "word " * 12280 + "Bearer " + "A" * 40
        text += " " * (65537 - len(text))
        self.assertNotIn("A" * 16, self.run_chunks(text))

    def test_password_assignment_with_whitespace(self) -> None:
        for pad in range(0, 12):
            text = "word " * 13100 + "x" * pad + " password = " + "hunter2secretvalue "
            for step in (65536, 65540, 65530):
                self.assertNotIn("hunter2secretvalue", self.run_chunks(text, step), (pad, step))

    def test_bearer_across_newline(self) -> None:
        self.assertNotIn("A" * 16, self.run_chunks("auth: Bearer\n" + "A" * 40 + "\n", 13))


class TestStreamRedactorR3(unittest.TestCase):
    """PR #40 r3: streaming redaction equals whole-string redaction for any chunking."""

    @staticmethod
    def stream(text: str, steps, exact=()) -> str:
        from mesh.core.redact import StreamRedactor

        r = StreamRedactor(list(exact))
        out, i, k = [], 0, 0
        while i < len(text):
            step = steps[k % len(steps)]
            out.append(r.feed(text[i:i + step]))
            i += step
            k += 1
        return "".join(out) + r.flush()

    def test_token_three_chars_before_forced_cut(self) -> None:
        from mesh.core import redact as rd

        body = "sk" + "-" + "A" * 24
        window = 4096  # r5: line-based; cuts near chunk edges still never split a token
        for total in (65537, 2 * window + 1, 2 * window + 5000):
            for back in (1, 2, 3, 4, 10):
                cut = total - window
                pos = cut - back
                text = "y " * (pos // 2) + "y" * (pos % 2) + "\n" + body + "\n" + "y" * 10
                for steps in ([65536], [total - 1], [4096], [1000, 7]):
                    out = self.stream(text, steps)
                    self.assertNotIn("A" * 16, out, (total, back, steps))
                    self.assertEqual(out, rd.redact(text, []), (total, back, steps))

    def test_keyword_gap_is_consistent(self) -> None:
        from mesh.core.redact import redact

        for gap in (1, 63, 64, 65, 256, 300):
            text = "Bearer" + " " * gap + "\n" + "T" * 40 + "\n"
            for steps in ([1], [7], [gap + 7], [len(text)]):
                self.assertEqual(self.stream(text, steps), redact(text, []), (gap, steps))
        text = "Bearer" + " " * 30 + "\n" + "T" * 40 + "\n"
        self.assertNotIn("T" * 16, self.stream(text, [1]))

    def test_random_chunkings_equal_whole_string(self) -> None:
        import random

        from mesh.core.redact import redact

        sec = "first-part-of-secret\nsecond-part"
        exact = [("MULTI", sec), ("PLAIN", "plainsecretvalue"), ("OVL", "secretvalue-tail"), ("PW", "hunter2xyz")]
        begin, end = "-----BEGIN " + "PRIVATE KEY-----\n", "-----END " + "PRIVATE KEY-----\n"
        frags = [TOKEN, "sk" + "-" + "Z" * 24, begin + (KEY_BODY + "\n") * 3 + end, begin + KEY_BODY * 5, end,
                 sec, "plainsecretvalue", "plainsecretvalue-tail", "Bearer " + "A" * 40, "Bearer\n" + "B" * 30,
                 "Bearer" + " " * 100 + "\n" + "C" * 30, "password = hunter2xyz", "password:\n  val123",
                 "secret=", "api_key :", "sk-" + "Q" * 5000, "A" * 70000, "word ", "\n", " " * 70, "x" * 5000]
        rnd = random.Random(40)
        for it in range(120):
            parts = [rnd.choice(frags) for _ in range(rnd.randint(1, 40))]
            if rnd.random() < 0.3:
                parts.append("w " * rnd.randint(10000, 40000))
            rnd.shuffle(parts)
            text = "".join(parts)
            steps = [rnd.choice([1, 3, 17, 500, 4096, 65536, 70000, rnd.randint(1, 100000)]) for _ in range(5)]
            self.assertEqual(self.stream(text, steps, exact), redact(text, exact), (it, steps))

    def test_linear_r3_repro_unbroken_run(self) -> None:
        from mesh.core.redact import redact

        body = "sk" + "-" + "A" * 24
        for pos in range(61430, 61445):
            text = "y" * pos + body + "y" * (65537 - pos - len(body))
            out = self.stream(text, [65536])
            self.assertNotIn("A" * 16, out, pos)
            self.assertEqual(out, redact(text, []), pos)


class TestStreamRedactorR4(unittest.TestCase):
    """PR #40 r4: nothing over a size limit is released; stream == whole still holds."""

    stream = staticmethod(TestStreamRedactorR3.stream)

    def check(self, text: str, banned, exact=()) -> None:
        from mesh.core.redact import redact

        whole = redact(text, list(exact))
        for b in banned:
            self.assertNotIn(b, whole)
        for steps in ([1], [7], [100], [4096], [65536], [len(text) + 1], [3, 70000, 11]):
            out = self.stream(text, steps, exact)
            self.assertEqual(out, whole, steps)

    def test_linear_r4_repros(self) -> None:
        self.check("Bearer" + " " * 65 + "X" * 40 + "\n", ["XXXXXXXX"])
        self.check("Bearer" + " " * 5000 + "\n\n" + "X" * 40 + " ok\n", ["XXXXXXXX"])
        self.check("Bearer " + "A" * 4096 + "TAIL" + "_CREDENTIAL_123456\n", ["TAIL", "AAAAAAAA"])
        body = "QUJD" * 4250  # 17,000 characters
        self.check("pre\n-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----\npost\n",
                   ["QUJDQUJD"])
        self.check("-----BEGIN " + "PRIVATE KEY-----\n" + body * 3, ["QUJDQUJD"])  # never closed
        self.check("sk" + "-" + "Z" * 100000 + "W" + "\n", ["ZZZZZZZZ", "W\n"])
        self.check("password" + " " * 300 + "=" + " " * 300 + "V" * 9000 + "\n", ["VVVVVVVV"])

    def test_random_oversized_never_leak(self) -> None:
        import random

        sec = "first-part-of-secret\nsecond-part"
        exact = [("MULTI", sec), ("PLAIN", "plainsecretvalue")]
        begin, end = "-----BEGIN " + "PRIVATE KEY-----\n", "-----END " + "PRIVATE KEY-----\n"
        frags = [
            "Bearer" + " " * 65 + "X" * 40, "Bearer " + "Y" * 5000, "Bearer\n\n" + "X" * 30,
            "sk" + "-" + "Z" * 6000, "gh" + "p_" + "Z" * 9000, "password  :  " + "V" * 5000,
            "api_key=" + "V" * 20, begin + "QUJD" * 4500 + "\n" + end, begin + "QUJD" * 50,
            sec, "plainsecretvalue", "word ", "\n", " " * 70, "ok:", "e" * 3000, "bearer of news ",
        ]
        banned = ["XXXXXXXX", "YYYYYYYY", "ZZZZZZZZ", "VVVVVVVV", "QUJDQUJD", "first-part", "plainsecret"]
        rnd = random.Random(44)
        for it in range(80):
            parts = [rnd.choice(frags) for _ in range(rnd.randint(1, 30))]
            text = "".join(parts)
            steps = [rnd.choice([1, 5, 99, 4096, 65536, rnd.randint(1, 90000)]) for _ in range(4)]
            from mesh.core.redact import redact

            whole = redact(text, exact)
            self.assertEqual(self.stream(text, steps, exact), whole, (it, steps))
            for b in banned:
                self.assertNotIn(b, whole, (it, b))


class TestLineRedactionR5(unittest.TestCase):
    """PR #40 r5: line-based redaction; stream == batch for any chunking; nothing leaks."""

    stream = staticmethod(TestStreamRedactorR3.stream)

    def test_linear_r5_repros(self) -> None:
        from mesh.core.redact import redact

        text = "password=sk" + "-" + "A" * 16 + ":TAIL" + "_SECRET_VALUE\n"
        for steps in ([1], [3], [7], [len(text)]):
            out = self.stream(text, steps)
            self.assertEqual(out, redact(text, []))
            self.assertNotIn("TAIL", out)
            self.assertNotIn("AAAA", out)
        text = "gh" + "p_" + "a" * 30 + "fk" + "-" + "G" * 16 + "\n"
        for steps in ([1], [3], [7], [len(text)]):
            out = self.stream(text, steps)
            self.assertEqual(out, redact(text, []))
            self.assertNotIn("GGGG", out)
        pem = "-----BEGIN " + "PRIVATE KEY-----\n" + ("QUJD" * 16 + "\n") * 20 + "-----END " + "PRIVATE KEY-----\nok\n"
        for steps in ([1], [3], [5], [7], [11]):
            out = self.stream(pem, steps)
            self.assertEqual(out, redact(pem, []))
            self.assertNotIn("QUJD", out)
            self.assertTrue(out.endswith("ok\n"))

    def test_unbroken_line_withheld_in_both_modes(self) -> None:
        from mesh.core.redact import LINE_CAP, UNBROKEN, redact

        text = "a\n" + "x" * (LINE_CAP + 10) + "\nb\n" + "y" * (LINE_CAP + 1)
        # The next line's first token is redacted too: the withheld line may have ended in a keyword.
        self.assertEqual(redact(text, []), "a\n" + UNBROKEN + "\n[REDACTED]\n" + UNBROKEN)
        for steps in ([1000], [LINE_CAP], [LINE_CAP + 1], [7, 70000]):
            self.assertEqual(self.stream(text, steps), redact(text, []))

    def test_random_property_5000(self) -> None:
        import random

        from mesh.core.redact import LINE_CAP, redact

        sec = "first-part-of-secret\nsecond-part"
        exact = [("MULTI", sec), ("PLAIN", "plainsecretvalue")]
        begin, end = "-----BEGIN " + "PRIVATE KEY-----\n", "-----END " + "PRIVATE KEY-----\n"
        frags = [
            "Bearer" + " " * 65 + "X" * 40, "Bearer " + "Y" * 300, "Bearer\n\n" + "X" * 30, "Bearer\n",
            "sk" + "-" + "Z" * 60, "gh" + "p_" + "Z" * 40, "fk" + "-" + "Z" * 20, "xox" + "b-" + "Z" * 12,
            "password  :  " + "V" * 50, "api_key=" + "V" * 20, "secret: " + "V" * 30, "passwd=", "password\n",
            "password=sk" + "-" + "Z" * 16 + ":VVVVVVVVVV",
            begin + ("QUJD" * 16 + "\n") * 3 + end, begin + "QUJD" * 50, "-----BEGIN ", "PRIVATE KEY-----\n",
            sec, "plainsecretvalue", "first-part-of-secret", "word ", "\n", "\n\n", " " * 70, "ok:", "-", "_",
            ":", "=", "e" * 200, "bearer of news ",
        ]
        banned = ["XXXXXXXX", "YYYYYYYY", "ZZZZZZZZ", "VVVVVVVV", "QUJDQUJD", "first-part", "plainsecret"]
        rnd = random.Random(5)
        for it in range(5000):
            parts = [rnd.choice(frags) for _ in range(rnd.randint(1, 20))]
            if rnd.random() < 0.02:
                big = "Q" * (LINE_CAP + rnd.randint(1, 50))
                cut = rnd.randint(0, len(big))
                big = big[:cut] + rnd.choice(["", "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"]) + big[cut:]
                parts.insert(rnd.randint(0, len(parts)), big)
            text = "".join(parts)
            whole = redact(text, exact)
            for b in banned:
                self.assertNotIn(b, whole, (it, b, text[:300]))
            steps = [rnd.choice([1, 2, 3, 5, 7, 64, 4096, rnd.randint(1, 3000)]) for _ in range(5)]
            self.assertEqual(self.stream(text, steps, exact), whole, (it, steps))


class TestLineRedactionR6(unittest.TestCase):
    """PR #40 r6: a multi-line exact value running into a withheld line is still
    redacted on the lines before it, in both modes."""

    stream = staticmethod(TestStreamRedactorR3.stream)

    def test_multiline_secret_into_withheld_line(self) -> None:
        from mesh.core.redact import LINE_CAP, redact

        for head, tail in (("hunt" + "er2", "A" * (LINE_CAP + 1)), ("pw1", "B" * 10 + " C" * (LINE_CAP // 2)),
                           ("x", "middle\n" + "D" * (LINE_CAP + 5))):
            sec = head + "\n" + tail
            text = "before " + sec + " after\nnext\n"
            exact = [("S", sec)]
            whole = redact(text, exact)
            self.assertNotIn(head, whole.replace("[REDACTED:S]", ""))
            self.assertTrue(whole.endswith("next\n") or whole.endswith("[REDACTED]\n"), whole[-80:])
            for steps in ([1], [7], [4096], [LINE_CAP], [len(text)]):
                self.assertEqual(self.stream(text, steps, exact), whole, steps)
