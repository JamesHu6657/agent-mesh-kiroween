"""AgyAdapter.parse() on real agy 1.5.3 stream-json captures (m3-agyparse; ACC: N6).

Every input is copied into a fresh ``/tmp/mh-agyparse-*`` run dir as ``stdout.log``
(and ``stderr.log`` where a stderr capture exists). Real fixtures are read-only and checked
against their ``.meta.json`` sidecar sha256 before use. No vendor process is started.
"""

import json
import os
import shutil
import tempfile
import unittest

from mesh.adapters.agy import AgyAdapter, _scan_stream
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_ROOT = os.path.join(REPO_ROOT, "tests", "fixtures", "real")
VERSION_DIR = "agy/1.5.3"

STEP1 = "stream-step1-denied.jsonl"
FIXC = "stream-fixC-denied.jsonl"
STEP6 = "stream-step6-network-error.jsonl"
STEP6_ERR = "stream-step6-network-error-stderr.txt"
STEP1_ERR = "stream-step1-denied-stderr.txt"
TOKEN_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
# Literals from stream-step6-network-error-stderr.txt (not via parse_stderr_error).
STEP6_STDERR_ERROR = {
    "short_error": (
        "agent executor error: generating and executing: request failed: Post "
        '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": '
        "net/http: cannot rewind body after connection loss"
    ),
    "retryable": False,
    "status": "UNKNOWN",
    "error_code": 2,
    "code_kind": "grpc",
}


def real_fixture(name: str) -> str:
    """Path of a real agy 1.5.3 fixture after checking its sidecar sha256."""
    path = os.path.join(REAL_ROOT, VERSION_DIR, name)
    assert os.path.isfile(path + tierlib.SIDECAR_SUFFIX), f"missing sidecar for {name}"
    tierlib.read_sidecar(path)  # raises TierError on any sha256 mismatch
    return path


def read_events(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class AgyParseBase(unittest.TestCase):
    def setUp(self):
        self.agy = AgyAdapter()

    def run_dir(self) -> str:
        d = tempfile.mkdtemp(prefix="mh-agyparse-", dir="/tmp")
        self.assertTrue(d.startswith("/tmp/mh-"), d)
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def stage(self, stdout_src=None, stderr_src=None, stdout_bytes=None) -> str:
        d = self.run_dir()
        if stdout_src is not None:
            shutil.copyfile(stdout_src, os.path.join(d, "stdout.log"))
        if stdout_bytes is not None:
            with open(os.path.join(d, "stdout.log"), "wb") as f:
                f.write(stdout_bytes)
        if stderr_src is not None:
            shutil.copyfile(stderr_src, os.path.join(d, "stderr.log"))
        return d

    def events(self, name):
        events = read_events(real_fixture(name))
        init = next(e for e in events if e.get("event") == "init")
        result = [e for e in events if e.get("event") == "result"][-1]["result"]
        return events, init, result

    def assert_success_run(self, name):
        events, init, result = self.events(name)
        res = self.agy.parse(self.stage(real_fixture(name), real_fixture(STEP1_ERR)))
        self.assertEqual(res.session_id, init["conversation_id"])
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertEqual(res.usage["status"], "SUCCESS")
        self.assertEqual(res.usage["source"], "result")
        for key in TOKEN_KEYS:
            self.assertEqual(res.usage[key], result["usage"][key], key)
        self.assertEqual(res.usage["duration_seconds"], result["duration_seconds"])
        self.assertEqual(res.usage["num_turns"], result["num_turns"])
        self.assertEqual(res.usage["model"], init["init"]["model"])
        self.assertEqual(res.usage["denied_actions"], len(result["denied_actions"]))
        self.assertNotIn("error", res.usage)
        return events, res


class TestAgyParseReal(AgyParseBase):
    def test_step1_denied_success(self):
        """ACC: N6 - step1-denied: init session id, SUCCESS, tokens from result.usage."""
        _, res = self.assert_success_run(STEP1)
        self.assertEqual(res.session_id, "464ae741-b883-48de-998c-9fc7ba5567c9")
        self.assertEqual(res.usage["input_tokens"], 17263)
        self.assertEqual(res.usage["output_tokens"], 1233)
        self.assertEqual(res.usage["thinking_tokens"], 1135)
        self.assertEqual(res.usage["cache_read_tokens"], 0)
        self.assertEqual(res.usage["model"], "gemini-3.8-flash-high")
        # result.response is "" and 1.5.3 agent_response steps carry no text
        self.assertEqual(res.final_text, "")

    def test_fixc_denied_tool_not_final_text(self):
        """ACC: N6 - fixC-denied: same checks; the ERROR tool step does not become final_text."""
        events, res = self.assert_success_run(FIXC)
        self.assertEqual(res.session_id, "1010b608-f183-4095-9760-28ede28cc378")
        denied = [e["step_update"] for e in events
                  if e.get("event") == "step_update" and e["step_update"].get("state") == "ERROR"]
        self.assertTrue(denied, "fixture must contain a failed tool step")
        for step in denied:
            msg = step["tool_info"]["error"]["message"]
            self.assertNotIn(msg[:40], res.final_text)
        for e in events:
            out = e.get("step_update", {}).get("tool_info", {}).get("output")
            if out:
                self.assertNotIn(out[:40], res.final_text)
        self.assertEqual(res.final_text, "")

    def test_step_usage_sums_match_result(self):
        """Fallback evidence: summed agent_response usage equals result.usage (all 3 captures)."""
        for name in (STEP1, FIXC, STEP6):
            _, _, result = self.events(name)
            scan = _scan_stream(real_fixture(name))
            for key in TOKEN_KEYS:
                self.assertEqual(scan["step_usage"][key], result["usage"][key], f"{name} {key}")
        self.assertEqual(_scan_stream(real_fixture(STEP6))["step_usage"]["cache_read_tokens"], 2131756)

    def test_step6_network_error_run_error(self):
        """ACC: N6 - step6: status ERROR -> run_error; error text kept; stderr AGY_ERROR mapped."""
        _, init, result = self.events(STEP6)
        res = self.agy.parse(self.stage(real_fixture(STEP6), real_fixture(STEP6_ERR)))
        self.assertEqual(res.session_id, init["conversation_id"])
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["status"], "ERROR")
        self.assertEqual(res.usage["error"], result["error"])
        self.assertIn("cannot rewind body after connection loss", res.usage["error"])
        for key in TOKEN_KEYS:
            self.assertEqual(res.usage[key], result["usage"][key], key)
        self.assertEqual(res.final_text, "")
        self.assertEqual(res.usage["stderr_error"]["retryable"], False)

        with open(real_fixture(STEP6_ERR), "r", encoding="utf-8") as f:
            stderr_text = f.read()
        err = AgyAdapter.parse_stderr_error(stderr_text)
        self.assertIsNotNone(err)
        self.assertEqual(err["short_error"], result["error"])
        self.assertIs(err["retryable"], False)
        self.assertEqual(err["status"], "UNKNOWN")
        self.assertEqual(err["error_code"], 2)
        self.assertEqual(err["code_kind"], "grpc")

    def test_parse_stderr_error_negative(self):
        """No AGY_ERROR line, empty, None or broken JSON -> None, never raises."""
        with open(real_fixture(STEP1_ERR), "r", encoding="utf-8") as f:
            self.assertIsNone(AgyAdapter.parse_stderr_error(f.read()))
        for text in ("", None, "error: boom\n", "AGY_ERROR: {not json", "AGY_ERROR: [1, 2]"):
            self.assertIsNone(AgyAdapter.parse_stderr_error(text), repr(text))
        ok = AgyAdapter.parse_stderr_error('x\nAGY_ERROR: {"short_error": "s", "retryable": true}\n')
        self.assertEqual(ok, {"short_error": "s", "retryable": True})

    def test_result_text_wins(self):
        """result.response, when non-empty, is final_text."""
        events = read_events(real_fixture(STEP1))
        events[-1]["result"]["response"] = "DONE: summary"
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.final_text, "DONE: summary")
        self.assertIsNone(res.vendor_exit_meaning)


class TestAgyParseRobust(AgyParseBase):
    def assert_no_result(self, res, error):
        """No result event at all: run_error with the reason, never an exception."""
        self.assertEqual(res.final_text, "")
        self.assertIsNone(res.session_id)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], error)
        self.assertEqual(res.usage["source"], "none")
        self.assertNotIn("stderr_error", res.usage)
        for key in TOKEN_KEYS:
            self.assertEqual(res.usage[key], 0, key)

    def test_missing_file(self):
        self.assert_no_result(self.agy.parse(self.run_dir()), "missing stdout")
        self.assert_no_result(self.agy.parse("/tmp/mh-agyparse-does-not-exist"), "missing stdout")

    def test_empty_and_garbage(self):
        for data in (b"", b"\n\n"):
            self.assert_no_result(self.agy.parse(self.stage(stdout_bytes=data)), "empty stdout")
        for data in (b"not json\n{{{\n", b"[1,2]\n\"str\"\n", b"\xff\xfe\x00garbage"):
            self.assert_no_result(self.agy.parse(self.stage(stdout_bytes=data)),
                                  "no result event (no parseable events)")

    def test_empty_stdout_with_agy_error_stderr(self):
        """Empty stdout + AGY_ERROR-only stderr: run_error and stderr_error is filled."""
        expected = STEP6_STDERR_ERROR
        for data in (b"", b"garbage\n"):
            res = self.agy.parse(self.stage(stderr_src=real_fixture(STEP6_ERR), stdout_bytes=data))
            self.assertEqual(res.vendor_exit_meaning, "run_error")
            self.assertEqual(res.usage["stderr_error"], expected)
            self.assertIn(res.usage["error"], ("empty stdout", "no result event (no parseable events)"))
        # stdout.log missing entirely, stderr.log present
        d = self.stage(stderr_src=real_fixture(STEP6_ERR))
        res = self.agy.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], "missing stdout")
        self.assertEqual(res.usage["stderr_error"], expected)

    def test_truncated_last_line(self):
        """Result line cut mid-way: no exception; init session kept; run_error (no result)."""
        with open(real_fixture(STEP1), "rb") as f:
            data = f.read().rstrip(b"\n")
        cut = data[: len(data) - 40]
        res = self.agy.parse(self.stage(stdout_bytes=cut))
        self.assertEqual(res.session_id, "464ae741-b883-48de-998c-9fc7ba5567c9")
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], "no result event")
        self.assertEqual(res.usage["source"], "steps")
        self.assertEqual(res.usage["malformed_lines"], 1)
        self.assertEqual(res.final_text, "")

    def test_result_usage_missing_key_uses_step_sum(self):
        """result.usage without output_tokens: that key is the step sum, never None."""
        events = read_events(real_fixture(STEP1))
        del events[-1]["result"]["usage"]["output_tokens"]
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.usage["source"], "result")
        self.assertEqual(res.usage["output_tokens"], 1233)  # sum of the agent_response steps
        self.assertEqual(res.usage["input_tokens"], 17263)  # still from result.usage
        for key in TOKEN_KEYS:
            self.assertIsNotNone(res.usage[key], key)

    def test_result_usage_missing_key_no_steps_is_zero(self):
        """result.usage without output_tokens and no agent_response steps: 0, never None."""
        events = [e for e in read_events(real_fixture(STEP1)) if e.get("event") != "step_update"]
        del events[-1]["result"]["usage"]["output_tokens"]
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.usage["source"], "result")
        self.assertEqual(res.usage["output_tokens"], 0)
        self.assertEqual(res.usage["input_tokens"], 17263)

    def test_error_result_without_stderr_log(self):
        """ERROR result and no stderr.log in run_dir: run_error, no stderr_error key."""
        _, _, result = self.events(STEP6)
        d = self.stage(real_fixture(STEP6))
        self.assertFalse(os.path.exists(os.path.join(d, "stderr.log")))
        res = self.agy.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], result["error"])
        self.assertNotIn("stderr_error", res.usage)

    def test_objects_without_event_key(self):
        """JSON objects with no "event" key, no init/result: pinned as run_error "no result event"."""
        data = b'{"foo": 1}\n{"step_update": {"step_type": "agent_response"}}\n{"type": "result"}\n'
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["error"], "no result event")
        self.assertEqual(res.usage["source"], "steps")
        self.assertIsNone(res.session_id)
        self.assertIsNone(res.usage["model"])
        self.assertEqual(res.final_text, "")
        for key in TOKEN_KEYS:
            self.assertEqual(res.usage[key], 0, key)
        self.assertNotIn("malformed_lines", res.usage)

    def test_result_without_status(self):
        events = read_events(real_fixture(STEP1))
        del events[-1]["result"]["status"]
        del events[0]["conversation_id"]
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        # session id falls back to the result event
        self.assertEqual(res.session_id, "464ae741-b883-48de-998c-9fc7ba5567c9")

    def test_init_wins_over_result_conversation_id(self):
        """init conversation_id wins when result.conversation_id differs."""
        events = read_events(real_fixture(STEP1))
        events[-1]["result"]["conversation_id"] = "result-sid-should-lose"
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        # literal copied from stream-step1-denied.jsonl line 1
        self.assertEqual(res.session_id, "464ae741-b883-48de-998c-9fc7ba5567c9")

    def test_success_with_nonempty_error_is_run_error(self):
        """SUCCESS plus a non-empty result.error is still run_error."""
        events = read_events(real_fixture(STEP1))
        self.assertEqual(events[-1]["result"]["status"], "SUCCESS")
        events[-1]["result"]["error"] = "boom despite success"
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["status"], "SUCCESS")
        self.assertEqual(res.usage["error"], "boom despite success")

    def test_several_init_events_first_wins(self):
        """Several init events: first conversation_id and model win."""
        events = read_events(real_fixture(STEP1))
        extra = {
            "event": "init",
            "conversation_id": "second-init-sid",
            "init": {"model": "later-model", "cwd": "/later"},
        }
        events.insert(1, extra)
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        # literals copied from stream-step1-denied.jsonl line 1
        self.assertEqual(res.session_id, "464ae741-b883-48de-998c-9fc7ba5567c9")
        self.assertEqual(res.usage["model"], "gemini-3.8-flash-high")

    def test_first_init_missing_model_second_init_not_used(self):
        """First init without model + second init with model -> model None (first init EVENT wins)."""
        lines = [
            {"event": "init", "conversation_id": "first-sid", "init": {"cwd": "/first"}},
            {"event": "init", "conversation_id": "second-sid",
             "init": {"model": "second-model", "cwd": "/second"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "ok",
                                           "usage": {"input_tokens": 1}}},
        ]
        data = "\n".join(json.dumps(e) for e in lines).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertIsNone(res.usage["model"])
        self.assertEqual(res.session_id, "first-sid")
        self.assertIsNone(res.vendor_exit_meaning)

    def test_init_fields_not_mixed_across_init_events(self):
        """Counterexample to field mixing: first init has model only, second has sid/cwd only.

        session_id must not come from the second init; it falls back to result.conversation_id.
        """
        from mesh.adapters.agy import _scan_stream
        lines = [
            {"event": "init", "init": {"model": "first-model"}},
            {"event": "init", "conversation_id": "second-sid", "init": {"cwd": "/second"}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "ok",
                                           "conversation_id": "result-sid", "usage": {}}},
        ]
        data = "\n".join(json.dumps(e) for e in lines).encode()
        d = self.stage(stdout_bytes=data)
        res = self.agy.parse(d)
        self.assertEqual(res.usage["model"], "first-model")
        self.assertEqual(res.session_id, "result-sid")
        scan = _scan_stream(os.path.join(d, "stdout.log"))
        self.assertIsNone(scan["session_id"])
        self.assertIsNone(scan["cwd"])
        self.assertEqual(scan["model"], "first-model")

        # Without a result conversation_id there is no fallback: None, never "second-sid"
        lines[-1]["result"].pop("conversation_id")
        data = "\n".join(json.dumps(e) for e in lines).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertIsNone(res.session_id)

    def test_first_init_cwd_from_real_fixture(self):
        from mesh.adapters.agy import _scan_stream
        scan = _scan_stream(real_fixture(STEP1))
        self.assertEqual(scan["cwd"], "/workspace/mesh-fresh-v1")

    def test_several_agy_error_lines_last_wins(self):
        """Several AGY_ERROR lines: last parseable one is stderr_error."""
        stderr = (
            'AGY_ERROR: {"short_error": "first", "retryable": true}\n'
            'AGY_ERROR: {"short_error": "second", "retryable": false}\n'
        )
        d = self.stage(stdout_bytes=b"")
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write(stderr)
        res = self.agy.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(res.usage["stderr_error"], {"short_error": "second", "retryable": False})

    def test_tool_step_usage_and_text_not_counted(self):
        """A tool step carrying usage/text keys does not add tokens or become final_text."""
        events = read_events(real_fixture(STEP1))
        # Drop agent_response steps so step sums would be 0 unless the tool step is counted.
        events = [e for e in events if not (
            e.get("event") == "step_update" and e.get("step_update", {}).get("step_type") == "agent_response"
        )]
        del events[-1]["result"]["usage"]["output_tokens"]
        events[-1]["result"]["response"] = ""
        tool = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "DONE",
                "usage": {"output_tokens": 999, "input_tokens": 888},
                "text": "TOOL TEXT MUST NOT APPEAR",
                "response": "also hidden",
            },
        }
        events.insert(-1, tool)
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.usage["output_tokens"], 0)  # missing key, no agent_response steps
        self.assertEqual(res.final_text, "")
        self.assertNotIn("TOOL TEXT MUST NOT APPEAR", res.final_text)

    def test_stderr_only_agy_error_line(self):
        """stderr containing ONLY an AGY_ERROR line (no plain error: line) is parsed."""
        only = 'AGY_ERROR: {"short_error": "solo", "retryable": true, "status": "UNKNOWN"}\n'
        d = self.stage(stdout_bytes=b"")
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write(only)
        res = self.agy.parse(d)
        self.assertEqual(res.vendor_exit_meaning, "run_error")
        self.assertEqual(
            res.usage["stderr_error"],
            {"short_error": "solo", "retryable": True, "status": "UNKNOWN"},
        )
        # The helper itself also accepts a lone AGY_ERROR line
        self.assertEqual(
            AgyAdapter.parse_stderr_error(only),
            {"short_error": "solo", "retryable": True, "status": "UNKNOWN"},
        )

    def test_result_usage_null_key_with_and_without_steps(self):
        """result.usage key explicit JSON null: step sum if steps exist, else 0."""
        events = read_events(real_fixture(STEP1))
        events[-1]["result"]["usage"]["output_tokens"] = None
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.usage["source"], "result")
        self.assertEqual(res.usage["output_tokens"], 1233)  # step sum
        self.assertEqual(res.usage["input_tokens"], 17263)

        events = [e for e in read_events(real_fixture(STEP1)) if e.get("event") != "step_update"]
        events[-1]["result"]["usage"]["output_tokens"] = None
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.usage["source"], "result")
        self.assertEqual(res.usage["output_tokens"], 0)
        self.assertEqual(res.usage["input_tokens"], 17263)

    def test_last_empty_agent_response_text_no_fallback(self):
        """Last agent_response text that is empty -> final_text "" (no older fallback)."""
        events = read_events(real_fixture(STEP1))
        events[-1]["result"]["response"] = ""
        # Inject two agent_response steps with text; the last is empty.
        older = {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "text": "OLDER TEXT"},
        }
        last = {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "text": ""},
        }
        events.insert(-1, older)
        events.insert(-1, last)
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.final_text, "")
        self.assertNotIn("OLDER TEXT", res.final_text)


    def test_last_agent_response_without_text_key(self):
        """Last agent_response with NO text key at all -> final_text "" (no older fallback)."""
        events = read_events(real_fixture(STEP1))
        events[-1]["result"]["response"] = ""
        events.insert(-1, {"event": "step_update",
                           "step_update": {"step_type": "agent_response", "text": "OLDER TEXT"}})
        events.insert(-1, {"event": "step_update",
                           "step_update": {"step_type": "agent_response", "state": "DONE"}})
        data = "\n".join(json.dumps(e) for e in events).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.final_text, "")

    def test_two_result_events_last_wins(self):
        """Two valid result events: response, status and usage all come from the last."""
        lines = [
            {"event": "init", "conversation_id": "sid", "init": {"model": "m"}},
            {"event": "result", "result": {"status": "ERROR", "error": "first failed",
                                           "response": "FIRST",
                                           "usage": {"input_tokens": 1, "output_tokens": 2}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "SECOND",
                                           "usage": {"input_tokens": 10, "output_tokens": 20}}},
        ]
        data = "\n".join(json.dumps(e) for e in lines).encode()
        res = self.agy.parse(self.stage(stdout_bytes=data))
        self.assertEqual(res.final_text, "SECOND")
        self.assertEqual(res.usage["status"], "SUCCESS")
        self.assertEqual(res.usage["input_tokens"], 10)
        self.assertEqual(res.usage["output_tokens"], 20)
        self.assertIsNone(res.vendor_exit_meaning)
        self.assertNotIn("error", res.usage)


if __name__ == "__main__":
    unittest.main()
