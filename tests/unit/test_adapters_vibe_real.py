"""VibeAdapter.parse() on the real vibe 3.1.0 capture (m3-vibeparse).

Inputs are copied into a fresh ``/tmp/mh-vibeparse-*`` run dir as ``stdout.log`` and
``vibe-out/export.json`` (the layout VibeAdapter.build() asks vibe for). Real fixtures are
read-only and checked against their sidecar sha256 before use. No vendor process is started.
"""

import json
import os
import shutil
import tempfile
import unittest

from mesh.adapters.base import RunResult
from mesh.adapters.vibe import VibeAdapter
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "real")
STREAM = "vibe/3.1.0/streaming-terminated.jsonl"
EXPORT = "vibe/3.1.0/export-terminated.json"
SESSION = "d76f6107-47ca-badd-301b-f7348b4930a2"


def real_fixture(rel: str) -> str:
    """Path of a real fixture after checking its sha256 against the .meta.json sidecar."""
    path = os.path.join(REAL_ROOT, rel)
    tierlib.read_sidecar(path)  # raises TierError on a missing sidecar or sha256 mismatch
    return path


def expected_final_text(stream_path: str) -> str:
    """Independent reading of the fixture: pick the last assistant message line first, then
    take its text parts (empty text stays "", no fallback to an older message)."""
    with open(stream_path, "r", encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    assistant = [e for e in events if e.get("type") == "message" and e.get("role") == "assistant"]
    if not assistant:
        return ""
    content = assistant[-1].get("content")
    if not isinstance(content, list):
        return ""
    return "".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text").strip()


class VibeParseBase(unittest.TestCase):
    def setUp(self):
        self.vibe = VibeAdapter()

    def run_dir(self, stdout_bytes=None, export_bytes=None) -> str:
        d = tempfile.mkdtemp(prefix="mh-vibeparse-", dir="/tmp")
        self.assertTrue(d.startswith("/tmp/mh-"), d)
        self.addCleanup(shutil.rmtree, d, True)
        if stdout_bytes is not None:
            with open(os.path.join(d, "stdout.log"), "wb") as f:
                f.write(stdout_bytes)
        if export_bytes is not None:
            os.makedirs(os.path.join(d, "vibe-out"))
            with open(os.path.join(d, "vibe-out", "export.json"), "wb") as f:
                f.write(export_bytes)
        return d

    @staticmethod
    def read(rel: str) -> bytes:
        with open(real_fixture(rel), "rb") as f:
            return f.read()


class TestVibeParseReal(VibeParseBase):
    def test_fixture_hashes(self):
        for rel in (STREAM, EXPORT):
            meta = tierlib.read_sidecar(os.path.join(REAL_ROOT, rel))
            self.assertEqual(len(meta["sha256"]), 64)

    def test_terminated_with_export(self):
        res = self.vibe.parse(self.run_dir(self.read(STREAM), self.read(EXPORT)))
        self.assertIsInstance(res, RunResult)
        self.assertEqual(res.session_id, SESSION)
        self.assertEqual(res.vendor_exit_meaning, "terminated")
        self.assertAlmostEqual(res.usage["cost_usd"], 0.0094254, places=9)
        self.assertEqual(res.usage["steps"], 6)
        self.assertEqual(res.usage["outcome"], "terminated")
        self.assertEqual(res.usage["stop_reason"], "interrupted")
        self.assertEqual(res.usage["exit_code"], 3)
        self.assertEqual(res.usage["model"], "ds-flash")
        self.assertEqual(res.usage["input_tokens"], 60614)
        self.assertEqual(res.usage["output_tokens"], 381)
        self.assertEqual(res.usage["cached_input_tokens"], 38400)
        self.assertEqual(res.usage["total_tokens"], 60995)

    def test_final_text_is_last_assistant_message(self):
        stream = real_fixture(STREAM)
        res = self.vibe.parse(self.run_dir(self.read(STREAM), self.read(EXPORT)))
        expected = expected_final_text(stream)
        self.assertTrue(expected)
        self.assertEqual(res.final_text, expected)
        self.assertIn("find the r1 worktree", res.final_text)
        # never the user prompt, never an effect (tool) line
        self.assertNotIn("Common block for every M2 run", res.final_text)
        self.assertNotIn("file_system.", res.final_text)

    def test_without_export_session_from_stream(self):
        res = self.vibe.parse(self.run_dir(self.read(STREAM)))
        self.assertEqual(res.session_id, SESSION)
        self.assertEqual(res.final_text, expected_final_text(real_fixture(STREAM)))
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_export_only(self):
        res = self.vibe.parse(self.run_dir(None, self.read(EXPORT)))
        self.assertEqual(res.session_id, SESSION)
        self.assertEqual(res.final_text, "")
        self.assertEqual(res.vendor_exit_meaning, "terminated")

    def test_truncated_last_line(self):
        data = self.read(STREAM).rstrip(b"\n")
        cut = data[: len(data) - 200]  # last line (an effect) is cut mid-JSON
        res = self.vibe.parse(self.run_dir(cut))
        self.assertEqual(res.session_id, SESSION)
        self.assertIn("find the r1 worktree", res.final_text)

    def test_truncated_export(self):
        exp = self.read(EXPORT)
        res = self.vibe.parse(self.run_dir(self.read(STREAM), exp[: len(exp) // 2]))
        self.assertEqual(res.session_id, SESSION)
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)


class TestVibeParseRobust(VibeParseBase):
    def test_empty_run_dir(self):
        res = self.vibe.parse(self.run_dir())
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.session_id)
        self.assertEqual(res.usage, {"error": "no export"})
        self.assertEqual(res.vendor_exit_meaning, "run_error")

    def test_missing_run_dir(self):
        res = self.vibe.parse("/tmp/mh-vibeparse-does-not-exist-xyz")
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.session_id)
        self.assertEqual(res.usage, {"error": "no export"})
        self.assertEqual(res.vendor_exit_meaning, "run_error")

    def test_empty_files(self):
        res = self.vibe.parse(self.run_dir(b"", b""))
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.session_id)
        self.assertEqual(res.usage, {"error": "no export"})
        self.assertEqual(res.vendor_exit_meaning, "run_error")

    def test_garbage(self):
        garbage = b"not json\n\x00\xff\xfe{{{\n[1,2,3]\n\"str\"\n42\nnull\n{\"type\":\"message\",\"role\":\"assistant\",\"content\":7}\n"
        res = self.vibe.parse(self.run_dir(garbage, b"[1, 2]"))
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.session_id)
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_export_wrong_types(self):
        exp = json.dumps({"session_id": 5, "usage": "x", "config": [], "outcome": 1, "error": None}).encode()
        line = json.dumps({"type": "message", "role": "assistant", "sessionId": "s-1",
                           "content": [{"type": "text", "text": "hi"}, {"type": "image"}, "x"]}).encode()
        res = self.vibe.parse(self.run_dir(line + b"\n", exp))
        self.assertEqual(res.session_id, "s-1")
        self.assertEqual(res.final_text, "hi")
        self.assertEqual(res.usage, {"outcome": 1})
        self.assertIsNone(res.vendor_exit_meaning)

    def test_non_user_role_after_last_assistant(self):
        # A "system" line with text after the last assistant message must not become
        # final_text (guards the role check against a `!= "user"` mutation).
        lines = [
            {"type": "message", "role": "user", "sessionId": "s-2", "content": [{"type": "text", "text": "do it"}]},
            {"type": "message", "role": "assistant", "sessionId": "s-2", "content": [{"type": "text", "text": "done"}]},
            {"type": "message", "role": "system", "sessionId": "s-2", "content": [{"type": "text", "text": "sys note"}]},
        ]
        data = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
        res = self.vibe.parse(self.run_dir(data))
        self.assertEqual(res.final_text, "done")
        self.assertEqual(res.session_id, "s-2")


class TestVibeExitMeaning(VibeParseBase):
    def meaning(self, **export) -> str:
        return self.vibe.parse(self.run_dir(b"", json.dumps(export).encode())).vendor_exit_meaning

    def test_outcomes(self):
        self.assertIsNone(self.meaning(outcome="completed", error=None))
        self.assertEqual(self.meaning(outcome="turn_limit", error=None), "turn_limit")
        self.assertEqual(self.meaning(outcome="terminated", error=None), "terminated")
        self.assertEqual(self.meaning(outcome="interrupted", error=None), "terminated")
        self.assertIsNone(self.meaning(outcome="something_new", error=None))

    def test_error_wins(self):
        self.assertEqual(self.meaning(outcome="completed", error="boom"), "run_error")
        self.assertEqual(self.meaning(outcome="terminated", error={"message": "x"}), "run_error")
        self.assertEqual(self.meaning(outcome="something_new", stop_reason="limit", error="boom"), "run_error")

    def test_unknown_outcome_stop_reason_interrupted(self):
        self.assertEqual(self.meaning(outcome="something_new", stop_reason="interrupted", error=None), "terminated")

    def test_unknown_outcome_stop_reason_limit(self):
        # generic "limit", not "turn_limit": --max-price / --max-tokens stops also say limit
        self.assertEqual(self.meaning(outcome="something_new", stop_reason="limit", error=None), "limit")

    def test_unknown_outcome_other_stop_reason(self):
        self.assertIsNone(self.meaning(outcome="something_new", stop_reason="end_turn", error=None))
        self.assertIsNone(self.meaning(outcome="something_new", stop_reason=7, error=None))
        self.assertIsNone(self.meaning(outcome="something_new", error=None))

    def test_missing_or_non_string_outcome_uses_stop_reason(self):
        self.assertEqual(self.meaning(stop_reason="interrupted", error=None), "terminated")
        self.assertEqual(self.meaning(outcome=1, stop_reason="limit", error=None), "limit")

    def test_known_outcome_ignores_stop_reason(self):
        self.assertIsNone(self.meaning(outcome="completed", stop_reason="interrupted", error=None))
        self.assertEqual(self.meaning(outcome="turn_limit", stop_reason="limit", error=None), "turn_limit")
        self.assertEqual(self.meaning(outcome="terminated", stop_reason="limit", error=None), "terminated")

    def test_outcome_error_without_error_field(self):
        self.assertEqual(self.meaning(outcome="error", error=None), "run_error")
        self.assertEqual(self.meaning(outcome="error"), "run_error")


def _assistant(text_parts, sid="s-1"):
    content = [{"type": "text", "text": t} for t in text_parts]
    return json.dumps({"type": "message", "role": "assistant", "sessionId": sid, "content": content})


class TestVibeFinalTextLast(VibeParseBase):
    def test_last_assistant_empty_text(self):
        stream = (_assistant(["old"]) + "\n" + _assistant([""]) + "\n").encode()
        d = self.run_dir(stream)
        res = self.vibe.parse(d)
        self.assertEqual(res.final_text, "")
        self.assertEqual(expected_final_text(os.path.join(d, "stdout.log")), "")

    def test_last_assistant_no_text_parts(self):
        stream = (_assistant(["old"]) + "\n" + _assistant([]) + "\n").encode()
        self.assertEqual(self.vibe.parse(self.run_dir(stream)).final_text, "")

    def test_effect_after_last_assistant_ignored(self):
        effect = json.dumps({"type": "effect", "title": "file_system.bash", "sessionId": "s-1"})
        stream = (_assistant(["old"]) + "\n" + _assistant(["new"]) + "\n" + effect + "\n").encode()
        self.assertEqual(self.vibe.parse(self.run_dir(stream)).final_text, "new")

    def test_export_session_id_wins_over_stream(self):
        stream = (_assistant(["hi"], sid="stream-session") + "\n").encode()
        exp = json.dumps({"session_id": "export-session", "outcome": "completed", "error": None}).encode()
        res = self.vibe.parse(self.run_dir(stream, exp))
        self.assertEqual(res.session_id, "export-session")
        self.assertEqual(self.vibe.parse(self.run_dir(stream)).session_id, "stream-session")


class TestVibeParsePolish2(VibeParseBase):
    def test_no_export_no_assistant_is_run_error(self):
        """Missing/unreadable export and no assistant message -> run_error, usage error no export."""
        res = self.vibe.parse(self.run_dir(b'{"type":"effect","title":"file_system.bash"}\n'))
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], "no export")
        self.assertNotIn("stderr_tail", res.usage)

    def test_no_export_no_assistant_stderr_tail(self):
        d = self.run_dir(b"")
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("\nignored blank\nfirst complaint\nlast non-empty line\n\n")
        res = self.vibe.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage, {"error": "no export", "stderr_tail": "last non-empty line"})

    def test_assistant_without_export_keeps_none(self):
        """With an assistant message but no export, usage and meaning stay None."""
        stream = (_assistant(["hi"]) + "\n").encode()
        res = self.vibe.parse(self.run_dir(stream))
        self.assertEqual(res.final_text, "hi")
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)

        # empty last assistant text still counts as an assistant message
        stream = (_assistant(["old"]) + "\n" + _assistant([""]) + "\n").encode()
        res = self.vibe.parse(self.run_dir(stream))
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_export_error_false_or_zero_not_error(self):
        """Not an error only for None, "", false, 0, {} and []."""
        for err in (None, "", False, 0, {}, []):
            exp = json.dumps({"outcome": "completed", "error": err}).encode()
            res = self.vibe.parse(self.run_dir(b"", exp))
            self.assertIsNone(res.vendor_exit_meaning, repr(err))

    def test_export_error_true_is_error(self):
        exp = json.dumps({"outcome": "completed", "error": True}).encode()
        res = self.vibe.parse(self.run_dir(b"", exp))
        self.assertEqual(res.vendor_exit_meaning, "run_error")

    def test_export_error_one_is_error(self):
        exp = json.dumps({"outcome": "completed", "error": 1}).encode()
        res = self.vibe.parse(self.run_dir(b"", exp))
        self.assertEqual(res.vendor_exit_meaning, "run_error")

    def test_export_error_other_values_are_errors(self):
        for err, label in (("boom", "str"), ({"message": "x"}, "dict"), (["x"], "list"),
                           (2, "int 2"), (-1, "int -1"), (0.5, "float")):
            exp = json.dumps({"outcome": "completed", "error": err}).encode()
            res = self.vibe.parse(self.run_dir(b"", exp))
            self.assertEqual(res.vendor_exit_meaning, "run_error", label)

    def test_utf8_sig_bom_on_export_and_stream(self):
        stream = b"\xef\xbb\xbf" + (_assistant(["bom text"], sid="bom-sid") + "\n").encode()
        exp_obj = {"session_id": "exp-bom", "outcome": "completed", "error": None,
                   "usage": {"input_tokens": 3}, "config": {"active_model": "m"}}
        exp = b"\xef\xbb\xbf" + json.dumps(exp_obj).encode()
        res = self.vibe.parse(self.run_dir(stream, exp))
        self.assertEqual(res.final_text, "bom text")
        self.assertEqual(res.session_id, "exp-bom")
        self.assertEqual(res.usage["input_tokens"], 3)
        self.assertEqual(res.usage["model"], "m")
        self.assertIsNone(res.vendor_exit_meaning)

    def test_nan_infinity_export_numbers_become_none(self):
        raw = (
            '{"outcome":"completed","error":null,"usage":{"input_tokens":NaN,'
            '"output_tokens":Infinity,"total_tokens":-Infinity},"cost_usd":NaN}'
        )
        res = self.vibe.parse(self.run_dir(b"", raw.encode()))
        self.assertIsNone(res.usage["input_tokens"])
        self.assertIsNone(res.usage["output_tokens"])
        self.assertIsNone(res.usage["total_tokens"])
        self.assertIsNone(res.usage["cost_usd"])
        self.assertEqual(res.usage["outcome"], "completed")

    def test_nan_infinity_nested_in_usage_become_none(self):
        raw = (
            '{"outcome":"completed","error":null,"usage":{"input_tokens":1,'
            '"detail":{"a":NaN,"list":[1,Infinity,{"b":-Infinity}]}}}'
        )
        res = self.vibe.parse(self.run_dir(b"", raw.encode()))
        self.assertEqual(res.usage["input_tokens"], 1)
        self.assertEqual(res.usage["detail"], {"a": None, "list": [1, None, {"b": None}]})

    def test_nan_deep_nesting_bounded_does_not_raise(self):
        from mesh.adapters.vibe import _NONFINITE_MAX_DEPTH
        depth = 400
        inner = "NaN"
        for _ in range(depth):
            inner = '{"x":' + inner + '}'
        raw = '{"outcome":"completed","error":null,"usage":{"deep":' + inner + '}}'
        res = self.vibe.parse(self.run_dir(b"", raw.encode()))
        self.assertEqual(res.usage["outcome"], "completed")
        # Below the bound the value is left as-is (still NaN, not None)
        node = res.usage["deep"]
        for _ in range(depth):
            node = node["x"]
        self.assertIsInstance(node, float)
        self.assertNotEqual(node, node)  # NaN
        self.assertLess(_NONFINITE_MAX_DEPTH, depth)

    def test_stderr_tail_capped_keeps_end(self):
        d = self.run_dir(b"")
        long_line = "A" * 600 + "END-OF-ERROR"
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("first\n" + long_line + "\n")
        res = self.vibe.parse(d)
        self.assertEqual(len(res.usage["stderr_tail"]), 500)
        self.assertTrue(res.usage["stderr_tail"].endswith("END-OF-ERROR"))
        self.assertEqual(res.usage["stderr_tail"], long_line[-500:])

    def test_non_object_export_labelled(self):
        for raw in (b"[1, 2]", b'"text"', b"42", b"null"):
            res = self.vibe.parse(self.run_dir(b"", raw))
            self.assertEqual(res.vendor_exit_meaning, "run_error", raw)
            self.assertEqual(res.usage, {"error": "export not an object"}, raw)
        # invalid JSON is still "no export"
        res = self.vibe.parse(self.run_dir(b"", b"{not json"))
        self.assertEqual(res.usage, {"error": "no export"})
        # with an assistant message a non-object export keeps None, like a missing export
        res = self.vibe.parse(self.run_dir((_assistant(["hi"]) + "\n").encode(), b"[1]"))
        self.assertEqual(res.final_text, "hi")
        self.assertIsNone(res.usage)
        self.assertIsNone(res.vendor_exit_meaning)

    def test_first_stream_session_id_wins(self):
        lines = [
            json.dumps({"type": "effect", "sessionId": "first-sid"}),
            json.dumps({"type": "message", "role": "assistant", "sessionId": "second-sid",
                        "content": [{"type": "text", "text": "hi"}]}),
        ]
        data = "".join(x + "\n" for x in lines).encode()
        res = self.vibe.parse(self.run_dir(data))
        self.assertEqual(res.session_id, "first-sid")
        self.assertEqual(res.final_text, "hi")

    def test_thinking_content_part_not_in_final_text(self):
        line = json.dumps({
            "type": "message", "role": "assistant", "sessionId": "s-t",
            "content": [
                {"type": "thinking", "text": "SECRET THOUGHT"},
                {"type": "text", "text": "visible"},
            ],
        }).encode() + b"\n"
        res = self.vibe.parse(self.run_dir(line))
        self.assertEqual(res.final_text, "visible")
        self.assertNotIn("SECRET THOUGHT", res.final_text)

    def test_deep_nesting_line_does_not_raise(self):
        deep = ("{\"a\":" * 2000) + "1" + ("}" * 2000) + "\n"
        line = _assistant(["ok"]) + "\n"
        res = self.vibe.parse(self.run_dir((deep + line).encode()))
        self.assertEqual(res.final_text, "ok")
        self.assertEqual(res.session_id, "s-1")

    def test_truncated_last_assistant_falls_back_to_previous(self):
        """A truncated LAST assistant line is skipped; previous complete assistant is kept."""
        good = _assistant(["keep me"], sid="s-tr")
        # Cut mid-JSON so the last assistant line is unparseable.
        bad = '{"type":"message","role":"assistant","sessionId":"s-tr","content":[{"type":"text","text":"LOST'
        data = (good + "\n" + bad).encode()
        res = self.vibe.parse(self.run_dir(data))
        self.assertEqual(res.final_text, "keep me")
        self.assertEqual(res.session_id, "s-tr")
        self.assertNotIn("LOST", res.final_text)


if __name__ == "__main__":
    unittest.main()
