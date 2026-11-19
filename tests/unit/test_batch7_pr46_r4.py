"""Unit regression tests for batch7 PR #46 round-4 review findings."""

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import task as task_cli
from mesh.cli.main import SYNONYMS, main as cli_main
from mesh.client import MeshClient
from mesh.errors import MeshError

MESH_PROC_LOCK = "shared"


class TestPr46R4Finding1MergeAllAuthOnce(unittest.TestCase):
    """P2: handle_task_merge_all resolves auth once before building per-call args."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r4-f1-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)

    def _merge_all_args(self, fmt: str = "json") -> argparse.Namespace:
        return argparse.Namespace(
            home=self.home,
            task_id=None,
            all_accepted=True,
            project="prj_1",
            user=True,
            passphrase_fd=None,
            passphrase_file=None,
            token_file=None,
            format=fmt,
            idempotency_key=None,
            dry_run=False,
            quiet=False,
            relayed=False,
            approval=None,
            if_version=None,
            timeout=None,
            request_id="req_top",
        )

    def test_merge_all_two_accepted_tasks_reads_passphrase_once(self) -> None:
        sent_payloads = []

        def fake_send_request(client_self, payload):
            sent_payloads.append(payload)
            if payload["cmd"] == "task.list":
                return {
                    "ok": True,
                    "data": {
                        "items": [
                            {"id": "tsk_1", "kind": "code", "created_at": "2026-01-01T00:00:00Z"},
                            {"id": "tsk_2", "kind": "code", "created_at": "2026-01-01T00:00:01Z"},
                        ]
                    },
                }
            if payload["cmd"] == "task.merge":
                tid = payload["args"]["task_id"]
                return {"ok": True, "data": {"status": "merged", "merge_commit": f"commit_{tid}"}}
            raise AssertionError(f"Unexpected cmd: {payload}")

        args = self._merge_all_args(fmt="json")
        with mock.patch("mesh.core.passphrase.read_passphrase", return_value="passphrase-unit-test") as read_mock, \
                mock.patch.object(MeshClient, "send_request", fake_send_request):
            result = task_cli.handle_task_merge(args)

        self.assertEqual(read_mock.call_count, 1)
        self.assertEqual([m["task_id"] for m in result["merged"]], ["tsk_1", "tsk_2"])
        self.assertEqual([p["cmd"] for p in sent_payloads], ["task.list", "task.merge", "task.merge"])
        for p in sent_payloads:
            self.assertEqual(p["auth"], {"type": "user", "passphrase": "passphrase-unit-test"})
        self.assertEqual(len({p["request_id"] for p in sent_payloads}), 3)

    def test_merge_all_text_format_two_tasks_reads_passphrase_once(self) -> None:
        sent_payloads = []

        def fake_send_request(client_self, payload):
            sent_payloads.append(payload)
            if payload["cmd"] == "task.list":
                return {
                    "ok": True,
                    "data": {
                        "items": [
                            {"id": "tsk_1", "kind": "code", "created_at": "2026-01-01T00:00:00Z"},
                            {"id": "tsk_2", "kind": "code", "created_at": "2026-01-01T00:00:01Z"},
                        ]
                    },
                }
            if payload["cmd"] == "task.merge":
                tid = payload["args"]["task_id"]
                return {"ok": True, "data": {"status": "merged", "merge_commit": f"commit_{tid}"}}
            if payload["cmd"] == "task.show":
                return {"ok": True, "data": {"status": "merged"}}
            raise AssertionError(f"Unexpected cmd: {payload}")

        args = self._merge_all_args(fmt="text")
        with mock.patch("mesh.core.passphrase.read_passphrase", return_value="passphrase-text-test") as read_mock, \
                mock.patch.object(MeshClient, "send_request", fake_send_request), \
                mock.patch("sys.stderr", io.StringIO()):
            result = task_cli.handle_task_merge(args)

        self.assertEqual(read_mock.call_count, 1)
        self.assertIn("merged 2 accepted task(s)", result["text"])
        for p in sent_payloads:
            self.assertEqual(p["auth"], {"type": "user", "passphrase": "passphrase-text-test"})

        # Empty queue also resolves passphrase exactly once.
        empty_payloads = []

        def fake_empty_send(client_self, payload):
            empty_payloads.append(payload)
            return {"ok": True, "data": {"items": []}}

        empty_args = self._merge_all_args(fmt="json")
        with mock.patch("mesh.core.passphrase.read_passphrase", return_value="passphrase-empty-test") as empty_read, \
                mock.patch.object(MeshClient, "send_request", fake_empty_send):
            empty_res = task_cli.handle_task_merge(empty_args)

        self.assertEqual(empty_read.call_count, 1)
        self.assertEqual(empty_res["merged"], [])
        self.assertEqual(len(empty_payloads), 2)
        for p in empty_payloads:
            self.assertEqual(p["auth"], {"type": "user", "passphrase": "passphrase-empty-test"})


class TestPr46R4Finding2JsonDuplicateSynonyms(unittest.TestCase):
    """P3: --input JSON must reject synonym + canonical key (or multiple synonyms) with DUPLICATE_PARAM."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r4-f2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)

    def _run_cli_no_rpc(self, argv, stdin_text):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch.object(MeshClient, "execute_cmd", side_effect=AssertionError("must not call daemon")), \
                mock.patch.object(MeshClient, "stream_cmd", side_effect=AssertionError("must not call daemon")), \
                redirect_stdout(buf):
            rc = cli_main(["--home", self.home, *argv])
        return rc, json.loads(buf.getvalue())

    def test_intake_submit_repro_criteria_and_criterion_rejected(self) -> None:
        # Repro 1: invalid-type synonym + valid canonical key must fail DUPLICATE_PARAM (exit 2),
        # regardless of JSON key order.
        for payload in (
            {"title": "T", "goal": "G", "lead": "lead1", "criteria": 123, "criterion": ["C1"]},
            {"title": "T", "goal": "G", "lead": "lead1", "criterion": ["C1"], "criteria": 123},
            {"title": "T", "goal": "G", "depends_on_projects": ["p1"], "depends_on_project": ["p2"]},
        ):
            rc, env = self._run_cli_no_rpc(["intake", "submit", "--input", "-"], json.dumps(payload))
            self.assertEqual(rc, c.EXIT_USAGE, (payload, env))
            self.assertFalse(env["ok"], env)
            self.assertEqual(env["error"]["code"], c.DUPLICATE_PARAM, (payload, env))

        # Control: single synonym with bad type is rejected with INVALID_ARGUMENT, and valid single
        # synonym passes validation and sends the exact validated value.
        rc_bad, env_bad = self._run_cli_no_rpc(
            ["intake", "submit", "--input", "-"],
            json.dumps({"title": "T", "goal": "G", "criteria": 123}),
        )
        self.assertEqual(rc_bad, c.EXIT_USAGE, env_bad)
        self.assertEqual(env_bad["error"]["code"], c.INVALID_ARGUMENT, env_bad)

    def test_agent_create_repro_allowed_models_and_allowed_model_rejected(self) -> None:
        # Repro 2: synonym + canonical key in JSON previously validated the canonical key while
        # agent.py read and sent the unvalidated synonym attribute.
        cases = [
            (
                ["agent", "create", "--input", "-"],
                {
                    "name": "a_syn",
                    "adapter": "fake",
                    "delivery": "launch",
                    "allowed_models": ["m1"],
                    "allowed_model": ["m2"],
                    "default_model": "m2",
                },
            ),
            (
                ["agent", "create", "--input", "-"],
                {
                    "name": "a_syn",
                    "adapter": "fake",
                    "delivery": "launch",
                    "allowed_model": ["m2"],
                    "allowed_models": ["m1"],
                    "default_model": "m2",
                },
            ),
            (
                ["agent", "create", "--input", "-"],
                {
                    "name": "a_syn",
                    "adapter": "fake",
                    "delivery": "launch",
                    "allowed_efforts": ["low"],
                    "allowed_effort": ["high"],
                },
            ),
            (
                ["agent", "create", "--input", "-"],
                {
                    "name": "a_syn",
                    "adapter": "fake",
                    "delivery": "launch",
                    "capabilities": ["intake"],
                    "capability": ["relay"],
                },
            ),
            (
                ["agent", "create", "--input", "-"],
                {
                    "name": "a_syn",
                    "adapter": "fake",
                    "delivery": "launch",
                    "options": {"a": 1},
                    "option": ["b=2"],
                },
            ),
            (
                ["agent", "update", "a_syn", "--input", "-"],
                {
                    "allowed_models": [123],
                    "allowed_model": ["m2"],
                },
            ),
            (
                ["agent", "onboard", "a_syn", "--out", os.path.join(self.tmp, "w.tok"), "--input", "-"],
                {
                    "capabilities": ["intake"],
                    "capability": ["relay"],
                },
            ),
            (
                ["project", "create", "--input", "-"],
                {
                    "title": "T",
                    "goal": "G",
                    "criteria": ["C1"],
                    "criterion": ["C2"],
                },
            ),
            (
                ["project", "update", "prj_1", "--input", "-"],
                {
                    "goal": "G2",
                    "criteria": 123,
                    "criterion": ["C1"],
                },
            ),
            (
                ["dep", "add", "tsk_2", "--input", "-"],
                {
                    "depends_on": "tsk_1",
                    "on": "tsk_3",
                },
            ),
        ]
        for argv, payload in cases:
            rc, env = self._run_cli_no_rpc(argv, json.dumps(payload))
            self.assertEqual(rc, c.EXIT_USAGE, (argv, payload, env))
            self.assertFalse(env["ok"], (argv, payload, env))
            self.assertEqual(env["error"]["code"], c.DUPLICATE_PARAM, (argv, payload, env))

    def test_two_synonyms_of_same_param_rejected_and_single_synonym_validated(self) -> None:
        with mock.patch.dict(SYNONYMS, {"model_allowlist": "allowed_model"}):
            rc, env = self._run_cli_no_rpc(
                ["agent", "create", "--input", "-"],
                json.dumps({
                    "name": "a_syn2",
                    "adapter": "fake",
                    "delivery": "launch",
                    "allowed_models": ["m1"],
                    "model_allowlist": ["m2"],
                }),
            )
        self.assertEqual(rc, c.EXIT_USAGE, env)
        self.assertEqual(env["error"]["code"], c.DUPLICATE_PARAM, env)

        # Single synonym succeeds and sends the validated value; task create depends_on still works.
        sent = []

        def fake_exec(client_self, args, cmd, cmd_args=None, auth_override=None):
            sent.append((cmd, cmd_args))
            return {"ok": True, "data": {}}

        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            for argv, payload in (
                (
                    ["agent", "create", "--input", "-"],
                    {"name": "a_ok", "adapter": "fake", "delivery": "launch", "allowed_models": ["m1"], "default_model": "m1"},
                ),
                (
                    ["task", "create", "--input", "-"],
                    {"project": "prj_1", "title": "T", "spec": "S", "depends_on": ["tsk_0"]},
                ),
            ):
                buf = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(buf):
                    rc_ok = cli_main(["--home", self.home, *argv])
                self.assertEqual(rc_ok, 0, (argv, buf.getvalue()))
        self.assertEqual(sent[0][0], "agent.create")
        self.assertEqual(sent[0][1]["allowed_models"], ["m1"])
        self.assertEqual(sent[1][0], "task.create")
        self.assertEqual(sent[1][1]["depends_on"], ["tsk_0"])


if __name__ == "__main__":
    unittest.main()
