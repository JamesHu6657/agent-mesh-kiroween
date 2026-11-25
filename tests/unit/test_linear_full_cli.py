"""Linear full-codebase review of main 1f78a4f, part C (mesh/cli, mesh/adapters), unit level.

C6 kiro keeps the complete streamed text over a truncated finalText.
C7 smoke evidence for the probed version wins over a later capture of another version.
C8 keepalive on a superseded fence (a retry started) exits 9, not 0.
C9 adapters run the normalized --binary path.
C10 a MESH_HOME holding % does not break --help."""

import argparse
import json
import os
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.adapters import smoke_schema
from mesh.adapters.base import RunRequest
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.cli.commands import task as task_cmd
from mesh.errors import MeshError


class TestKiroTruncatedFinalText(unittest.TestCase):
    def test_complete_stream_wins(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            full = "Part one of the answer. Part two, the important end."
            lines = [
                {"type": "metadata", "data": {"sessionId": "s1"}},
                {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                              "content": {"type": "text", "text": full}}}},
                {"type": "runFinished", "data": {"finalText": "Part one of the answer.", "status": "ok"}},
            ]
            with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(x) + "\n" for x in lines))
            self.assertEqual(KiroAdapter().parse(d).final_text, full)

    def test_complete_final_text_kept(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            lines = [
                {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                              "content": {"type": "text", "text": "abc"}}}},
                {"type": "runFinished", "data": {"finalText": "abc def", "status": "ok"}},
            ]
            with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(x) + "\n" for x in lines))
            self.assertEqual(KiroAdapter().parse(d).final_text, "abc def")


class TestSmokeEvidenceVersion(unittest.TestCase):
    def test_probed_version_wins(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name in ("kiro-2.0.0-2026-11-01.json", "kiro-1.0.0-2026-11-10.json"):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write("{}")
            self.assertEqual(os.path.basename(smoke_schema.load_latest(d, "kiro", "2.0.0")),
                             "kiro-2.0.0-2026-11-01.json")
            # Without a probed version (or none for it) the newest capture still wins.
            self.assertEqual(os.path.basename(smoke_schema.load_latest(d, "kiro")), "kiro-1.0.0-2026-11-10.json")
            self.assertEqual(os.path.basename(smoke_schema.load_latest(d, "kiro", "3.0.0")),
                             "kiro-1.0.0-2026-11-10.json")


class _Client:
    def __init__(self, run, task):
        self.run, self.task = run, task

    def execute_cmd(self, args, cmd, payload):
        return {"data": self.run if cmd == "run.show" else self.task}


class TestKeepaliveSuperseded(unittest.TestCase):
    def _args(self):
        return argparse.Namespace(task_id="tsk_1", run="run_1", fence=1, home="/tmp/x", format="json")

    def test_newer_fence_exits_9(self) -> None:
        client = _Client({"status": "ended", "end_reason": "worker_failed", "ended_at": "t"},
                         {"status": "running", "fence_seq": 2})
        exc = MeshError(c.FENCE_MISMATCH, exit_code=c.EXIT_STALE, message="fence")
        with mock.patch.object(task_cmd, "_keepalive_args", lambda a: a):
            with self.assertRaises(MeshError) as cm:
                task_cmd._keepalive_end(client, self._args(), exc, 3)
        self.assertEqual(cm.exception.exit_code, c.EXIT_STALE)

    def test_normal_end_same_fence_exits_0(self) -> None:
        client = _Client({"status": "ended", "end_reason": "submitted", "ended_at": "t"},
                         {"status": "submitted", "fence_seq": 1})
        exc = MeshError(c.FENCE_MISMATCH, exit_code=c.EXIT_STALE, message="fence")
        with mock.patch.object(task_cmd, "_keepalive_args", lambda a: a):
            res = task_cmd._keepalive_end(client, self._args(), exc, 3)
        self.assertEqual(res["ended"], "run_ended")


class TestBinaryNormalized(unittest.TestCase):
    def test_tilde_and_relative_binary_are_absolute(self) -> None:
        cases = [(DroidAdapter, "claude-opus-5-5", "high"), (AgyAdapter, "claude-sonnet-4-6", None),
                 (KiroAdapter, "claude-sonnet-4.5", None), (VibeAdapter, "devstral-2", None)]
        with tempfile.TemporaryDirectory() as d:
            prompt = os.path.join(d, "p.md")
            with open(prompt, "w", encoding="utf-8") as f:
                f.write("x")
            for cls, model, effort in cases:
                for raw in ("~/bin/tool", "bin/tool"):
                    req = RunRequest(prompt_path=prompt, workspace=d, model=model, effort=effort, profile="edit",
                                     resume_session_id=None, timeout_s=60,
                                     options={"run_id": "run_1", "binary": raw})
                    try:
                        spec = cls().build(req)
                    except MeshError:
                        continue  # model/effort combination refused: not what this test is about
                    self.assertTrue(os.path.isabs(spec.argv[0]), (cls.__name__, spec.argv[0]))
                    self.assertNotIn("~", spec.argv[0])


class TestPercentHome(unittest.TestCase):
    def test_help_with_percent_home(self) -> None:
        from mesh.cli.main import create_parent_parser
        with mock.patch.dict(os.environ, {"MESH_HOME": "/tmp/mesh%home"}):
            p = argparse.ArgumentParser(parents=[create_parent_parser()])
            self.assertIn("/tmp/mesh%home", p.format_help())


if __name__ == "__main__":
    unittest.main()
