"""Unit regression tests for batch7 PR #46 round-1 review findings."""

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
from mesh.adapters import evidence, smoke_schema
from mesh.adapters.base import AdapterNotInstalled, parse_version_output
from mesh.cli.commands import agent as agent_cli
from mesh.cli.commands import setup as setup_cli
from mesh.cli.main import main as cli_main
from mesh.client import MeshClient
from mesh.core.db import init_db, transaction
from mesh.daemon.handlers.agent import handle_agent_create, handle_agent_token_issue
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.supervisor import _vendor_nonzero_failure
from mesh.errors import MeshError
from tests import tierlib


def _user_ctx(conn, home: str, cmd: str, args: dict, *, testing_mode: bool = True) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_unit_pr46_r1",
        home=home,
        cmd=cmd,
        args=args,
        testing_mode=testing_mode,
        credential_id="cred_1",
    )


def _setup_home_db(tmp: str):
    home = os.path.join(tmp, "home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) "
            "VALUES ('usr_1', 'user', 'user', '2026-01-01T00:00:00.000Z');"
        )
    return home, conn


class TestPr46R1Finding1ReplayOnlyUnit(unittest.TestCase):
    """P1: agent token issue / onboard pre-flight replay_only propagation and refusal."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46-f1-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.tok_dir = os.path.join(self.tmp, "tok")
        os.makedirs(self.home, mode=0o700)
        os.makedirs(self.tok_dir, mode=0o700)
        self.existing = os.path.join(self.tok_dir, "existing.tok")
        with open(self.existing, "w", encoding="utf-8") as f:
            f.write("EXISTING_SECRET\n")
        os.chmod(self.existing, 0o600)

    def test_client_execute_cmd_emits_replay_only_only_when_flagged(self) -> None:
        client = MeshClient(home=self.home)
        sent = []

        def fake_send(payload):
            sent.append(payload)
            return {"ok": True, "data": {}}

        with mock.patch.object(client, "resolve_auth", return_value={"type": "user"}), \
                mock.patch.object(client, "send_request", side_effect=fake_send):
            ns_plain = argparse.Namespace(idempotency_key="k1", dry_run=False, request_id="r1")
            client.execute_cmd(ns_plain, "agent.create", {"name": "a1"})
            self.assertNotIn("replay_only", sent[-1])

            ns_replay = argparse.Namespace(idempotency_key="k1", dry_run=False, request_id="r2", _replay_only=True)
            client.execute_cmd(ns_replay, "agent.create", {"name": "a1"})
            self.assertIs(sent[-1].get("replay_only"), True)

    def test_token_issue_dry_run_with_idem_refuses_existing_out_before_rpc(self) -> None:
        called = []

        class _NeverClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                called.append(cmd)
                return {"data": {}}

        args = argparse.Namespace(
            home=self.home, name="w1", expires_in=None, out=self.existing,
            force=False, idempotency_key="k1", dry_run=True,
        )
        with mock.patch.object(agent_cli, "MeshClient", _NeverClient):
            with self.assertRaises(MeshError) as cm:
                agent_cli.handle_agent_token_issue(args)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual(called, [])

    def test_token_issue_existing_out_with_idem_sets_replay_only_and_translates_miss(self) -> None:
        seen = []

        class _FakeClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                seen.append((cmd, getattr(args, "_replay_only", False), dict(payload)))
                raise MeshError(
                    c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="No matching idempotent execution found for replay_only request",
                    details={"param": "idempotency_key", "reason": "replay_only_miss"},
                )

        args = argparse.Namespace(
            home=self.home, name="w1", expires_in=None, out=self.existing,
            force=False, idempotency_key="fresh-k", dry_run=False,
        )
        with mock.patch.object(agent_cli, "MeshClient", _FakeClient):
            with self.assertRaises(MeshError) as cm:
                agent_cli.handle_agent_token_issue(args)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual(cm.exception.details.get("param"), "out")
        self.assertIn("already exists", cm.exception.message)
        self.assertEqual(seen, [("agent.token.issue", True, {"name": "w1", "expires_in": None})])
        self.assertFalse(getattr(args, "_replay_only", False))
        with open(self.existing, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "EXISTING_SECRET\n")

    def test_token_issue_defense_in_depth_if_client_ignores_replay_only(self) -> None:
        class _DumbClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                return {"data": {"token": "mesh_at_leak_value_1234567890", "credential_id": "cred_x"}}

        args = argparse.Namespace(
            home=self.home, name="w1", expires_in=None, out=self.existing,
            force=False, idempotency_key="fresh-k", dry_run=False,
        )
        with mock.patch.object(agent_cli, "MeshClient", _DumbClient):
            with self.assertRaises(MeshError) as cm:
                agent_cli.handle_agent_token_issue(args)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        with open(self.existing, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "EXISTING_SECRET\n")

    def test_onboard_existing_out_with_idem_sets_replay_only_on_create_and_issue(self) -> None:
        calls = []

        class _MissCreateClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                calls.append((cmd, getattr(args, "_replay_only", False)))
                raise MeshError(
                    c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="miss",
                    details={"param": "idempotency_key", "reason": "replay_only_miss"},
                )

        args = argparse.Namespace(
            home=self.home, name="ghost", adapter="none", out=self.existing,
            force=False, idempotency_key="fresh-ob", dry_run=False,
            max_concurrency="1", capability=None, no_intake=False, ack_timeout=None, expires_in=None,
        )
        with mock.patch.object(agent_cli, "MeshClient", _MissCreateClient):
            with self.assertRaises(MeshError) as cm:
                agent_cli.handle_agent_onboard(args)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual(cm.exception.details.get("param"), "out")
        self.assertEqual(calls, [("agent.create", True)])

        # Dry run must refuse before RPC too
        calls.clear()
        dry_args = argparse.Namespace(**vars(args))
        dry_args.dry_run = True
        with mock.patch.object(agent_cli, "MeshClient", _MissCreateClient):
            with self.assertRaises(MeshError) as cm_dry:
                agent_cli.handle_agent_onboard(dry_args)
        self.assertEqual(cm_dry.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(calls, [])

        # If step 1 replays, step 2 (token issue) must ALSO send replay_only=True
        calls2 = []

        class _ReplayCreateMissIssueClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                calls2.append((cmd, getattr(args, "_replay_only", False), args.idempotency_key))
                if cmd == "agent.create":
                    args._idempotent_replay = True
                    return {"data": {"id": "agt_1", "name": "ghost"}}
                raise MeshError(
                    c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="miss",
                    details={"param": "idempotency_key", "reason": "replay_only_miss"},
                )

        args2 = argparse.Namespace(**vars(args))
        with mock.patch.object(agent_cli, "MeshClient", _ReplayCreateMissIssueClient):
            with self.assertRaises(MeshError) as cm2:
                agent_cli.handle_agent_onboard(args2)
        self.assertEqual(cm2.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm2.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual([c_item[:2] for c_item in calls2], [("agent.create", True), ("agent.token.issue", True)])


class TestPr46R1Finding2CliInputValidation(unittest.TestCase):
    """P2: CLI --input empty paths, optional field types, and nargs='+' list checks return exit 2."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46-f2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)

    def _run_json(self, argv, stdin_text="{}"):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch.object(MeshClient, "execute_cmd", side_effect=AssertionError("must not call daemon")), \
                mock.patch.object(MeshClient, "stream_cmd", side_effect=AssertionError("must not call daemon")), \
                mock.patch.object(setup_cli, "handle_setup", side_effect=AssertionError("must not run setup")), \
                redirect_stdout(buf):
            rc = cli_main(["--home", self.home, *argv])
        return rc, json.loads(buf.getvalue())

    def test_empty_and_blank_input_path_rejected_with_usage(self) -> None:
        for argv in (
            ["config", "set", "--input", ""],
            ["config", "set", "--input="],
            ["config", "set", "--input", "   "],
            ["config", "set", "--inp="],
            ["status", "--input", ""],
            ["agent", "create", "--input", "   "],
        ):
            rc, env = self._run_json(argv)
            self.assertEqual(rc, c.EXIT_USAGE, (argv, env))
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT)

    def test_optional_field_and_nargs_plus_bad_types_rejected_with_usage(self) -> None:
        cases = [
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "binary": 123}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "default_profile": 1}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "ack_timeout": true}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "allowed_models": [123]}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "allowed_models": "m1"}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "options": 123}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "options": [123]}'),
            (["setup", "--input", "-"], '{"lead_name": 123}'),
            (["setup", "--input", "-"], '{"token_dir": []}'),
            (["task", "create", "--input", "-"], '{"project": ["a"], "title": "t", "spec": "s"}'),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": 123, "spec": "s"}'),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": "t", "spec": {"x": 1}}'),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": "t", "spec": "s", "idle_timeout": true}'),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": "t", "spec": "s", "idle_timeout": 1.5}'),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": "t", "spec": "s", "depends_on": [123]}'),
            (["inbox", "ack", "--input", "-"], '{"messages": 123}'),
            (["inbox", "ack", "--input", "-"], '{"messages": "msg_1"}'),
            (["inbox", "ack", "--input", "-"], '{"messages": []}'),
            (["inbox", "ack", "--input", "-"], '{"messages": [123]}'),
            (["inbox", "ack", "--input", "-"], '{"messages": null}'),
            (["task", "close", "tsk_1", "--input", "-"], '{"superseded_by": "tsk_2", "reason": 123}'),
            (["agent", "token", "issue", "a1", "--input", "-"], '{"expires_in": 123}'),
            (["task", "list", "--input", "-"], '{"project": 123}'),
            (["event", "list", "--input", "-"], '{"project": ["prj_1"]}'),
        ]
        for argv, stdin_text in cases:
            rc, env = self._run_json(argv, stdin_text=stdin_text)
            self.assertEqual(rc, c.EXIT_USAGE, (argv, stdin_text, env))
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT, (argv, stdin_text, env))

    def test_valid_special_json_types_preserved(self) -> None:
        recorded = []

        def fake_exec(self, args, cmd, cmd_args=None, auth_override=None):
            recorded.append((cmd, cmd_args))
            return {"ok": True, "data": {}}

        good_cases = [
            (
                ["task", "create", "--input", "-"],
                '{"project": "prj_1", "title": "T", "spec": "S", "timeout": 120, "idle_timeout": 60}',
            ),
            (
                ["agent", "create", "--input", "-"],
                '{"name": "a1", "adapter": "none", "delivery": "inbox", "max_concurrency": 2, "ack_timeout": 900, "options": {}}',
            ),
            (
                ["agent", "update", "a1", "--input", "-"],
                '{"allowed_efforts": [], "default_effort": null}',
            ),
            (
                ["config", "set", "--input", "-"],
                '{"key": "lead.default_agent", "value": null}',
            ),
        ]
        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            for argv, stdin_text in good_cases:
                buf = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
                    rc = cli_main(["--home", self.home, *argv])
                self.assertEqual(rc, 0, (argv, stdin_text, buf.getvalue()))

    def test_handler_token_issue_rejects_bad_expires_in_with_usage_error(self) -> None:
        home, conn = _setup_home_db(os.path.join(self.tmp, "db-h"))
        self.addCleanup(conn.close)
        with transaction(conn):
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'w1', '2026-01-01T00:00:00.000Z');"
            )
            conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_1', 'none', 'inbox', 0, 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
            )
        for bad_exp in ("bogus", 123, "0s", ""):
            with self.assertRaises(MeshError, msg=repr(bad_exp)) as cm:
                handle_agent_token_issue(_user_ctx(conn, home, "agent.token.issue", {"name": "w1", "expires_in": bad_exp}))
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)


class TestPr46R1Finding3SymlinkAndEvidence(unittest.TestCase):
    """P2: smoke_schema._has_symlink checks allowlist break before os.path.islink, and evidence passes root."""

    def test_has_symlink_allows_symlinked_system_roots(self) -> None:
        real_islink = smoke_schema.os.path.islink
        with mock.patch("mesh.adapters.smoke_schema.os.path.islink",
                        side_effect=lambda p: True if p in ("/tmp", "/var", "/home") else real_islink(p)):
            self.assertIsNone(smoke_schema._has_symlink("/tmp/mh-test/results/vibe-3.1.0-2026-11-15.json"))
            self.assertIsNone(smoke_schema._has_symlink("/var/folders/xx/results/vibe-3.1.0-2026-11-15.json"))
            self.assertIsNone(smoke_schema._has_symlink("/home/u/repo/tests/smoke/results/vibe-3.1.0-2026-11-15.json"))
            self.assertIsNone(smoke_schema._has_symlink(
                "/var/folders/xx/results/vibe-3.1.0-2026-11-15.json",
                root="/var/folders/xx/results",
            ))

    def test_load_evidence_under_symlinked_tmp_succeeds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr46-f3-", dir="/tmp") as stage:
            fx_rel = "droid/1.6.0/smoke-S-X-1-stdout.jsonl"
            fx_bytes = b'{"ok": true}\n'
            tierlib.write_capture(stage, fx_rel, fx_bytes, ["droid"])
            fx_rel6 = "droid/1.6.0/smoke-S-X-6-stdout.jsonl"
            tierlib.write_capture(stage, fx_rel6, fx_bytes, ["droid"])
            doc = {
                "schema": smoke_schema.SCHEMA,
                "adapter": "droid",
                "binary": "/usr/local/bin/droid",
                "version": "1.6.0",
                "captured_at_utc": "2026-11-15T00:00:00Z",
                "mesh_commit": "abc1234",
                "model": "m1",
                "effort": None,
                "profile": "readonly",
                "timeout_s": 30,
                "consent_ref": "unit",
                "cases": [
                    {
                        "id": "S-X-1", "status": "pass", "reason": None, "rc": 0, "duration_s": 0.1,
                        "fixture": smoke_schema.fixture_rel("droid", "1.6.0", "smoke-S-X-1-stdout.jsonl"),
                        "fixture_sha256": tierlib.sha256_bytes(fx_bytes),
                        "observation": None, "evaluated_from": None, "carried_from": None,
                    },
                    {
                        "id": "S-X-6", "status": "pass", "reason": None, "rc": 0, "duration_s": 0.1,
                        "fixture": smoke_schema.fixture_rel("droid", "1.6.0", "smoke-S-X-6-stdout.jsonl"),
                        "fixture_sha256": tierlib.sha256_bytes(fx_bytes),
                        "observation": None, "evaluated_from": None, "carried_from": None,
                    },
                ],
            }
            tierlib.write_capture(stage, "results/droid-1.6.0-2026-11-15.json", json.dumps(doc).encode("utf-8"), ["smoke"])

            # Still reject actual symlinked directory INSIDE root
            sym_sub = os.path.join(stage, "results", "symdir")
            os.symlink(os.path.join(stage, "results"), sym_sub)
            self.assertIsNotNone(smoke_schema._has_symlink(os.path.join(sym_sub, "f.json"), root=os.path.join(stage, "results")))

            # With /tmp mocked as a symlink (like macOS /tmp -> private/tmp), load_evidence passes
            real_islink = os.path.islink
            env = {
                "MESH_TEST_SMOKE_RESULTS_DIR": os.path.join(stage, "results"),
                "MESH_TEST_SMOKE_FIXTURES_DIR": stage,
            }
            with mock.patch("os.path.islink", side_effect=lambda p: True if p == "/tmp" else real_islink(p)):
                ev = evidence.load_evidence("droid", testing_mode=True, probed_version="1.6.0", env=env)
            self.assertTrue(ev.ok, (ev.reason, ev.errors))


class TestPr46R1Finding4VersionRegex(unittest.TestCase):
    """P2: parse_version_output handles sentence-ending periods and prerelease trailing periods."""

    def test_trailing_periods_and_paths(self) -> None:
        self.assertEqual(parse_version_output("droid version 1.6.0."), "1.6.0")
        self.assertEqual(parse_version_output("version 3.1.0."), "3.1.0")
        self.assertEqual(parse_version_output("version 2.10.3.4-rc1."), "2.10.3.4-rc1")
        self.assertEqual(parse_version_output("v1.2.3-beta.1."), "1.2.3-beta.1")
        self.assertEqual(parse_version_output("1.6.0.1"), "1.6.0.1")
        self.assertEqual(parse_version_output("/opt/python3.11/bin/droid 1.6.0"), "1.6.0")
        self.assertEqual(parse_version_output("tool/1.6.0."), "1.6.0")
        self.assertIsNone(parse_version_output("/opt/python3.11/bin/droid"))


class TestPr46R1Finding5VendorNonzeroStderr(unittest.TestCase):
    """P2: _vendor_nonzero_failure must not promote tool_denied and must bound/redact stderr."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46-f5-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_tool_denied_stays_exit_nonzero_and_bounds_redacts_unbroken_and_multiline_stderr(self) -> None:
        fake_sk = "sk-" + "A" * 24
        fake_mesh = "mesh_at_" + "B" * 24

        # Case 1: long unbroken 150KB line after [denied]
        rdir1 = os.path.join(self.tmp, "r1")
        os.makedirs(rdir1)
        with open(os.path.join(rdir1, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("[denied] tool permission approval is not supported in non-interactive mode\n")
            f.write("x" * 150_000 + "\n")
            f.write(f"Error: connection reset by peer token={fake_sk} {fake_mesh}\n")

        reason1, extra1 = _vendor_nonzero_failure(rdir1, "kiro", 1, testing_mode=True)
        self.assertEqual(reason1, "exit_nonzero")
        self.assertIn("stderr_error", extra1)
        self.assertEqual(extra1["stderr_error"]["reason"], "tool_denied")
        dumped1 = json.dumps(extra1)
        self.assertLessEqual(len(extra1["stderr_error"]["details"]["stderr"]), 2000)
        self.assertNotIn(fake_sk, dumped1)
        self.assertNotIn(fake_mesh, dumped1)

        # Case 2: many multi-line 200KB log after [denied]
        rdir2 = os.path.join(self.tmp, "r2")
        os.makedirs(rdir2)
        with open(os.path.join(rdir2, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("[denied] tool permission approval is not supported in non-interactive mode\n")
            for i in range(4000):
                f.write(f"trace line {i:05d} doing network stuff\n")
            f.write(f"fatal auth leak: {fake_sk} {fake_mesh}\n")

        reason2, extra2 = _vendor_nonzero_failure(rdir2, "kiro", 1, testing_mode=True)
        self.assertEqual(reason2, "exit_nonzero")
        dumped2 = json.dumps(extra2)
        self.assertLessEqual(len(extra2["stderr_error"]["details"]["stderr"]), 2000)
        self.assertNotIn(fake_sk, dumped2)
        self.assertNotIn(fake_mesh, dumped2)

    def test_model_or_effort_rejected_still_promotes_and_bounds_redacts(self) -> None:
        fake_sk = "sk-" + "C" * 24
        rdir = os.path.join(self.tmp, "r-eff")
        os.makedirs(rdir)
        with open(os.path.join(rdir, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("failed to set effort: model does not support additional fields\n")
            for i in range(2500):
                f.write(f"noise line {i:05d} with secret {fake_sk}\n")
        reason, extra = _vendor_nonzero_failure(rdir, "kiro", 1, testing_mode=True)
        self.assertEqual(reason, "model_or_effort_rejected")
        self.assertIn("note", extra)
        self.assertLessEqual(len(extra["stderr"]), 2000)
        self.assertNotIn(fake_sk, json.dumps(extra))


class TestPr46R1Finding6And7AgentCreateValidation(unittest.TestCase):
    """P2/P3: pure arg checks run before is_installed(), and falsy non-lists are not swallowed."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46-f67-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.conn = _setup_home_db(self.tmp)
        self.addCleanup(self.conn.close)

    def test_invalid_model_fails_before_is_installed_when_cli_absent(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(MeshError) as cm:
                handle_agent_create(_user_ctx(
                    self.conn, self.home, "agent.create",
                    {
                        "name": "d_no_bin",
                        "adapter": "droid",
                        "allowed_models": ["--skip-permissions-unsafe"],
                        "default_model": "--skip-permissions-unsafe",
                    },
                ))
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

        # Valid args still raise ADAPTER_NOT_INSTALLED when binary is absent
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(AdapterNotInstalled):
                handle_agent_create(_user_ctx(
                    self.conn, self.home, "agent.create",
                    {
                        "name": "d_valid_no_bin",
                        "adapter": "droid",
                        "allowed_models": ["m1"],
                        "default_model": "m1",
                    },
                ))

        # testing_mode=False with adapter="fake" still returns TEST_DOUBLE_DISABLED first
        with self.assertRaises(MeshError) as cm_fake:
            handle_agent_create(
                _user_ctx(self.conn, self.home, "agent.create", {"name": "f1", "adapter": "fake"}, testing_mode=False)
            )
        self.assertEqual(cm_fake.exception.code, c.TEST_DOUBLE_DISABLED)

    def test_falsy_non_lists_not_swallowed_by_or_fallback(self) -> None:
        bad_payloads = [
            {"name": "b1", "adapter": "fake", "delivery": "inbox", "allowed_models": False},
            {"name": "b2", "adapter": "fake", "delivery": "inbox", "allowed_models": 0},
            {"name": "b3", "adapter": "fake", "delivery": "inbox", "allowed_models": ""},
            {"name": "b4", "adapter": "fake", "delivery": "inbox", "allowed_models": {}},
            {"name": "b5", "adapter": "fake", "delivery": "inbox", "allowed_model": False},
            {"name": "b6", "adapter": "fake", "delivery": "inbox", "allowed_efforts": False},
            {"name": "b7", "adapter": "fake", "delivery": "inbox", "allowed_efforts": 0},
            {"name": "b8", "adapter": "fake", "delivery": "inbox", "allowed_efforts": ""},
            {"name": "b9", "adapter": "fake", "delivery": "inbox", "allowed_efforts": {}},
            {"name": "b10", "adapter": "fake", "delivery": "inbox", "allowed_effort": False},
            {"name": "b11", "adapter": "fake", "delivery": "inbox", "capabilities": False},
            {"name": "b12", "adapter": "fake", "delivery": "inbox", "capabilities": 0},
            {"name": "b13", "adapter": "fake", "delivery": "inbox", "capabilities": {}},
            {"name": "b14", "adapter": "fake", "delivery": "inbox", "capabilities": ""},
        ]
        for args in bad_payloads:
            with self.assertRaises(MeshError, msg=args) as cm:
                handle_agent_create(_user_ctx(self.conn, self.home, "agent.create", args))
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT, (args, cm.exception))
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        cnt = self.conn.execute("SELECT COUNT(*) FROM agents;").fetchone()[0]
        self.assertEqual(cnt, 0)


if __name__ == "__main__":
    unittest.main()
