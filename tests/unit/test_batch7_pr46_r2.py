"""Unit regression tests for batch7 PR #46 round-2 review findings and same-class bugs."""

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import agent as agent_cli
from mesh.cli.commands import setup as setup_cli
from mesh.cli.commands import task as task_cli
from mesh.cli.main import main as cli_main
from mesh.client import MeshClient
from mesh.core.db import init_db, transaction
from mesh.daemon.handlers.adapter import _extract_adapter_id
from mesh.daemon.handlers.agent import handle_agent_create, handle_agent_update
from mesh.daemon.handlers.approval import handle_approval_request
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.inbox import handle_inbox_ack
from mesh.daemon.handlers.project import (
    handle_project_create,
    handle_project_update,
    handle_project_wait,
)
from mesh.daemon.handlers.task import handle_task_create, handle_task_wait
from mesh.errors import MeshError


def _user_ctx(conn, home: str, cmd: str, args: dict, *, testing_mode: bool = True) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_unit_pr46_r2",
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


class TestPr46R2Finding1JsonNullAndDefaults(unittest.TestCase):
    """P2: --input JSON null for non-None defaults + setup & task/project.wait guards."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-f1-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        if os.name == "nt":
            from mesh.platform import perms
            perms.set_private_dir_acl(self.tmp, 0o700)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)

    def _run_cli_blocked(self, argv, stdin_text="{}"):
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch.object(MeshClient, "execute_cmd", side_effect=AssertionError("must not call daemon")), \
                mock.patch.object(MeshClient, "stream_cmd", side_effect=AssertionError("must not call daemon")), \
                mock.patch.object(setup_cli, "handle_setup", side_effect=AssertionError("must not run setup")), \
                redirect_stdout(buf):
            rc = cli_main(["--home", self.home, *argv])
        return rc, json.loads(buf.getvalue())

    def test_json_null_for_non_none_default_rejected_with_usage(self) -> None:
        out_tok = os.path.join(self.tmp, "tok.file")
        cases = [
            (["setup", "--input", "-"], '{"lead_name": null}', "lead_name"),
            (["setup", "--input", "-"], '{"no_lead": null}', "no_lead"),
            (["setup", "--input", "-"], '{"no_start": null}', "no_start"),
            (["task", "wait", "tsk_1", "--input", "-"], '{"until": null, "timeout": "1s"}', "until"),
            (["project", "wait", "prj_1", "--input", "-"], '{"until": null, "timeout": "1s"}', "until"),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "delivery": null}', "delivery"),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "allowed_models": null}', "allowed_model"),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "allowed_efforts": null}', "allowed_effort"),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "options": null}', "option"),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "can_lead": null}', "can_lead"),
            (["agent", "onboard", "a1", "--out", out_tok, "--input", "-"], '{"adapter": null}', "adapter"),
            (["agent", "onboard", "a1", "--out", out_tok, "--input", "-"], '{"force": null}', "force"),
            (["event", "list", "--input", "-"], '{"limit": null}', "limit"),
            (["inbox", "wait", "--input", "-"], '{"timeout": "1s", "max": null}', "max"),
            (["task", "create", "--input", "-"], '{"project": "p1", "title": "t", "spec": "s", "depends_on": null}', "depends_on"),
            (["project", "update", "prj_1", "--input", "-"], '{"goal": "g2", "criteria": null}', "criterion"),
            (["project", "create", "--input", "-"], '{"title": "T", "goal": "G", "criterion": null}', "criterion"),
        ]
        for argv, stdin_text, expected_param in cases:
            rc, env = self._run_cli_blocked(argv, stdin_text=stdin_text)
            self.assertEqual(rc, c.EXIT_USAGE, (argv, stdin_text, env))
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT, (argv, stdin_text, env))
            self.assertEqual(env["error"]["details"].get("param"), expected_param, (argv, stdin_text, env))

    def test_explicit_nullable_fields_and_default_none_filters_still_allowed(self) -> None:
        recorded = []

        def fake_exec(self, args, cmd, cmd_args=None, auth_override=None):
            recorded.append((cmd, cmd_args))
            if cmd == "agent.token.issue":
                return {"ok": True, "data": {"token": "msh_ag_fake_0001", "credential_id": "cred_1"}}
            return {"ok": True, "data": {}}

        ok_cases = [
            (["config", "set", "--input", "-"], '{"key": "lead.default_agent", "value": null}'),
            (["agent", "update", "a1", "--input", "-"], '{"default_model": null, "default_effort": null}'),
            (["task", "list", "--input", "-"], '{"project": null, "status": null}'),
            (["agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "max_concurrency": null}'),
            (["agent", "update", "a1", "--input", "-"], '{"max_concurrency": null}'),
            (["agent", "onboard", "a1", "--out", os.path.join(self.tmp, "ob.tok"), "--input", "-"], '{"max_concurrency": null}'),
        ]
        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            for argv, stdin_text in ok_cases:
                buf = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
                    rc = cli_main(["--home", self.home, *argv])
                self.assertEqual(rc, 0, (argv, stdin_text, buf.getvalue()))
        self.assertIsNone(recorded[3][1]["max_concurrency"])
        self.assertIn("max_concurrency", recorded[4][1])
        self.assertIsNone(recorded[4][1]["max_concurrency"])
        self.assertIsNone(recorded[5][1]["max_concurrency"])
        self.assertEqual(recorded[6][0], "agent.token.issue")

        # task submit {"deliverable": null} must still fail INVALID_ARGUMENT in task.py (not daemon)
        rc_sub, env_sub = self._run_cli_blocked(
            ["task", "submit", "tsk_1", "--input", "-"],
            stdin_text='{"deliverable": null, "run": "run_1", "fence": 1}',
        )
        self.assertEqual(rc_sub, c.EXIT_USAGE, env_sub)
        self.assertEqual(env_sub["error"]["code"], c.INVALID_ARGUMENT)

    def test_setup_direct_rejects_none_lead_name_and_bad_token_dir_before_side_effects(self) -> None:
        fresh_home = os.path.join(self.tmp, "fresh-home")
        bad_cases = [
            {"lead_name": None, "no_lead": False, "token_dir": os.path.join(self.tmp, "t")},
            {"lead_name": "lead", "no_lead": False, "token_dir": ""},
            {"lead_name": "lead", "no_lead": False, "token_dir": "   "},
            {"lead_name": "lead", "no_lead": False, "token_dir": "bad\x00dir"},
            {"lead_name": "lead", "no_lead": True, "token_dir": ""},
            {"lead_name": "lead", "no_lead": True, "token_dir": "bad\x00dir"},
        ]
        for overrides in bad_cases:
            ns = argparse.Namespace(
                home=fresh_home,
                dry_run=False,
                no_start=False,
                approval_mode=None,
                passphrase_fd=None,
                passphrase_file=None,
                **overrides,
            )
            with mock.patch.object(setup_cli, "_run", side_effect=AssertionError("must not spawn subprocess")), \
                    mock.patch("mesh.core.passphrase.read_passphrase", side_effect=AssertionError("must not prompt")):
                with self.assertRaises(MeshError, msg=repr(overrides)) as cm:
                    setup_cli.handle_setup(ns)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
            self.assertFalse(os.path.exists(os.path.join(fresh_home, "mesh.db")))

    def test_daemon_wait_handlers_reject_non_str_until_with_usage(self) -> None:
        import asyncio

        home, conn = _setup_home_db(os.path.join(self.tmp, "wait-db"))
        self.addCleanup(conn.close)
        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, origin_principal_id, created_by, created_via, version, created_at, updated_at) "
                "VALUES ('prj_1', 'P1', 'G1', 'active', 'usr_1', 'usr_1', 'direct', 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, created_by, version, created_at, updated_at) "
                "VALUES ('tsk_1', 'prj_1', 'T1', 'S1', 'general', 'created', 'edit', 60, 1, 'usr_1', 1, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');"
            )

        class _FakeServer:
            active_waits = 0

        for bad_until in (None, 123, ["submitted"], False):
            with self.assertRaises(MeshError, msg=repr(bad_until)) as cm_t:
                asyncio.run(handle_task_wait(
                    _FakeServer(),
                    _user_ctx(conn, home, "task.wait", {"task_id": "tsk_1", "until": bad_until, "timeout": "1s"}),
                ))
            self.assertEqual(cm_t.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_t.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual((cm_t.exception.details or {}).get("param"), "until")

            with self.assertRaises(MeshError, msg=repr(bad_until)) as cm_p:
                asyncio.run(handle_project_wait(
                    _FakeServer(),
                    _user_ctx(conn, home, "project.wait", {"project_id": "prj_1", "until": bad_until, "timeout": "1s"}),
                ))
            self.assertEqual(cm_p.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_p.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual((cm_p.exception.details or {}).get("param"), "until")


class TestPr46R2Finding2OnboardExpiresInPrecheck(unittest.TestCase):
    """P2: agent onboard & agent token issue share client-side expires_in pre-check."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-f2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        if os.name == "nt":
            from mesh.platform import perms
            perms.set_private_dir_acl(self.tmp, 0o700)
        self.home = os.path.join(self.tmp, "home")
        self.tok_dir = os.path.join(self.tmp, "tok")
        os.makedirs(self.home, mode=0o700)
        os.makedirs(self.tok_dir, mode=0o700)

    def test_onboard_and_token_issue_reject_bad_expires_in_before_any_rpc(self) -> None:
        called = []

        class _NeverClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                called.append(cmd)
                return {"data": {}}

        out_tok = os.path.join(self.tok_dir, "w.tok")
        for bad_exp in ("bogus", "0s", "-5s", "", "   ", 123, True):
            called.clear()
            ob_args = argparse.Namespace(
                home=self.home, name="w1", out=out_tok, force=False, adapter="none",
                max_concurrency="1", capability=None, no_intake=False, ack_timeout=None,
                expires_in=bad_exp, idempotency_key=None, dry_run=False,
            )
            with mock.patch.object(agent_cli, "MeshClient", _NeverClient):
                with self.assertRaises(MeshError, msg=f"onboard {bad_exp!r}") as cm_o:
                    agent_cli.handle_agent_onboard(ob_args)
            self.assertEqual(cm_o.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_o.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual((cm_o.exception.details or {}).get("param"), "expires_in")
            self.assertEqual(called, [])
            self.assertFalse(os.path.exists(out_tok))

            ti_args = argparse.Namespace(
                home=self.home, name="w1", expires_in=bad_exp, out=None,
                force=False, idempotency_key=None, dry_run=False,
            )
            with mock.patch.object(agent_cli, "MeshClient", _NeverClient):
                with self.assertRaises(MeshError, msg=f"token_issue {bad_exp!r}") as cm_t:
                    agent_cli.handle_agent_token_issue(ti_args)
            self.assertEqual(cm_t.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_t.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual((cm_t.exception.details or {}).get("param"), "expires_in")
            self.assertEqual(called, [])


class TestPr46R2Finding3AdapterProbeTruthiness(unittest.TestCase):
    """P2: adapter probe --binary '' and empty adapter_id not dropped by truthiness."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-f3-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)

    def test_probe_binary_empty_without_adapter_id_exits_usage_before_rpc(self) -> None:
        for argv in (
            ["--home", self.home, "adapter", "probe", "--binary", ""],
            ["--home", self.home, "adapter", "probe", "--binary="],
            ["--home", self.home, "adapter", "probe", "--input", "-"],
        ):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO('{"binary": ""}')), \
                    mock.patch.object(MeshClient, "execute_cmd", side_effect=AssertionError("must not call daemon")), \
                    redirect_stdout(buf):
                rc = cli_main(argv)
            env = json.loads(buf.getvalue())
            self.assertEqual(rc, c.EXIT_USAGE, (argv, env))
            self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(env["error"]["details"].get("param"), "adapter_id")

    def test_probe_passes_empty_binary_and_empty_adapter_id_to_server(self) -> None:
        sent = []

        def fake_exec(self, args, cmd, cmd_args=None, auth_override=None):
            sent.append((cmd, dict(cmd_args or {})))
            return {"ok": True, "data": {}}

        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            buf1 = io.StringIO()
            with redirect_stdout(buf1):
                rc1 = cli_main(["--home", self.home, "adapter", "probe", "agy", "--binary", ""])
            self.assertEqual(rc1, 0)
            self.assertEqual(sent[-1], ("adapter.probe", {"adapter_id": "agy", "binary": ""}))

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                rc2 = cli_main(["--home", self.home, "adapter", "probe", ""])
            self.assertEqual(rc2, 0)
            self.assertEqual(sent[-1], ("adapter.probe", {"adapter_id": ""}))

    def test_daemon_extract_adapter_id_rejects_empty_and_bool(self) -> None:
        home, conn = _setup_home_db(os.path.join(self.tmp, "ap-db"))
        self.addCleanup(conn.close)
        for args in ({"adapter_id": ""}, {"adapter_id": False}, {"adapter": ""}, {"id": ""}):
            for req_flag in (False, True):
                ctx = _user_ctx(conn, home, "adapter.probe", args)
                with self.assertRaises(MeshError, msg=(args, req_flag)) as cm:
                    _extract_adapter_id(ctx, required=req_flag)
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)


class TestPr46R2Finding4AgentUpdateValidation(unittest.TestCase):
    """P3: daemon handle_agent_update type validation & same-class create/update parity."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-f4-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.conn = _setup_home_db(self.tmp)
        self.addCleanup(self.conn.close)
        with transaction(self.conn):
            handle_agent_create(_user_ctx(
                self.conn, self.home, "agent.create",
                {"name": "ibx1", "adapter": "none", "delivery": "inbox"},
            ))

    def test_agent_update_rejects_bad_types_with_usage_error(self) -> None:
        bad_payloads = [
            {"name": "ibx1", "capabilities": False},
            {"name": "ibx1", "capabilities": 123},
            {"name": "ibx1", "capabilities": [{}]},
            {"name": "ibx1", "capabilities": {"intake": False}},
            {"name": "ibx1", "capability": {"intake": False}},
            {"name": "ibx1", "default_profile": {}},
            {"name": "ibx1", "default_profile": []},
            {"name": "ibx1", "default_profile": 123},
            {"name": "ibx1", "default_profile": "high"},
            {"name": "ibx1", "max_unapproved_profile": {}},
            {"name": "ibx1", "max_unapproved_profile": []},
            {"name": "ibx1", "max_unapproved_profile": True},
            {"name": "ibx1", "if_version": True},
            {"name": "ibx1", "if_version": False},
            {"name": "ibx1", "if_version": 1.5},
            {"name": "ibx1", "default_model": 123},
            {"name": "ibx1", "default_model": {}},
            {"name": "ibx1", "default_effort": 123},
            {"name": "ibx1", "default_effort": {}},
            {"name": ["ibx1"], "max_concurrency": 2},
        ]
        for args in bad_payloads:
            with self.assertRaises(MeshError, msg=repr(args)) as cm:
                handle_agent_update(_user_ctx(self.conn, self.home, "agent.update", args))
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT, args)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE, args)

        with self.assertRaises(MeshError) as cm_mc:
            handle_agent_update(_user_ctx(self.conn, self.home, "agent.update", {"name": "ibx1", "max_concurrency": None}))
        self.assertEqual(cm_mc.exception.code, c.INVALID_CONCURRENCY)
        self.assertEqual(cm_mc.exception.exit_code, c.EXIT_USAGE)

        # Valid update still works normally
        with transaction(self.conn):
            res = handle_agent_update(_user_ctx(
                self.conn, self.home, "agent.update",
                {
                    "name": "ibx1",
                    "if_version": 1,
                    "capabilities": ["intake", "relay"],
                    "default_profile": "dev",
                    "max_unapproved_profile": "elevated",
                    "default_model": "",
                    "default_effort": None,
                },
            ))
        self.assertEqual(res.data["capabilities"], ["intake", "relay"])
        self.assertEqual(res.data["default_profile"], "dev")
        self.assertEqual(res.data["max_unapproved_profile"], "elevated")
        self.assertEqual(res.data["version"], 2)


class TestPr46R2SameClassAudit(unittest.TestCase):
    """Additional same-class truthiness and direct-RPC validation bugs."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-pr46r2-sc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.conn = _setup_home_db(self.tmp)
        self.addCleanup(self.conn.close)

    def test_agent_create_cli_preserves_empty_or_bad_profile_and_binary(self) -> None:
        sent = []

        class _CapClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                sent.append(dict(payload))
                return {"data": {}}

        def _base_ns(**overrides):
            d = dict(
                home=self.home, name="a1", adapter="none", delivery="inbox",
                default_model=None, allowed_models=None, allowed_model=[],
                default_effort=None, allowed_efforts=None, allowed_effort=[],
                max_concurrency=None, can_lead=False, capabilities=None,
                capability=None, default_profile=None, max_unapproved_profile=None,
                binary=None, options=None, option=[], strict_models=False,
                no_intake=False, ack_timeout=None, _from_json=set(),
            )
            d.update(overrides)
            return SimpleNamespace(**d)

        with mock.patch.object(agent_cli, "MeshClient", _CapClient):
            # Omitted manually -> CLI omits default_profile so meshd applies adapter-aware safe default
            agent_cli.handle_agent_create(_base_ns())
            self.assertNotIn("default_profile", sent[-1])
            self.assertEqual(sent[-1]["max_unapproved_profile"], "dev")

            # Empty / JSON null must be preserved so daemon rejects
            agent_cli.handle_agent_create(_base_ns(default_profile="", max_unapproved_profile=""))
            self.assertEqual(sent[-1]["default_profile"], "")
            self.assertEqual(sent[-1]["max_unapproved_profile"], "")

            agent_cli.handle_agent_create(_base_ns(_from_json={"default_profile", "max_unapproved_profile"}))
            self.assertIsNone(sent[-1]["default_profile"])
            self.assertIsNone(sent[-1]["max_unapproved_profile"])

            # Bad binaries must not crash with ValueError or get /cwd prefix
            for bad_bin in ("bad\x00bin", "   ", "--skip"):
                agent_cli.handle_agent_create(_base_ns(binary=bad_bin))
                self.assertEqual(sent[-1]["binary"], bad_bin)

    def test_task_merge_empty_project_rejected_without_rpc(self) -> None:
        ns = argparse.Namespace(home=self.home, all_accepted=False, task_id="tsk_1", project="")
        with mock.patch.object(task_cli, "MeshClient", side_effect=AssertionError("must not call daemon")):
            with self.assertRaises(MeshError) as cm:
                task_cli.handle_task_merge(ns)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual((cm.exception.details or {}).get("param"), "project")

    def test_daemon_handlers_reject_falsy_and_nonlist_direct_rpc_args(self) -> None:
        with transaction(self.conn):
            handle_agent_create(_user_ctx(
                self.conn, self.home, "agent.create",
                {"name": "lead1", "adapter": "none", "delivery": "inbox", "can_lead": True},
            ))
            pres = handle_project_create(_user_ctx(
                self.conn, self.home, "project.create",
                {"title": "P", "goal": "G", "criterion": ["C1"], "lead": "lead1"},
            ))
            pid = pres.data["id"]

        # approval.request falsy expires_in & unhashable category
        for bad_exp in ("", 0, False, 123):
            with self.assertRaises(MeshError, msg=repr(bad_exp)) as cm_a:
                handle_approval_request(_user_ctx(
                    self.conn, self.home, "approval.request",
                    {"category": "spend_money", "summary": "S", "operation": {"k": 1}, "expires_in": bad_exp},
                ))
            self.assertEqual(cm_a.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_a.exception.exit_code, c.EXIT_USAGE)
        for bad_cat in ({"k": 1}, [1]):
            with self.assertRaises(MeshError, msg=repr(bad_cat)) as cm_cat:
                handle_approval_request(_user_ctx(
                    self.conn, self.home, "approval.request",
                    {"category": bad_cat, "summary": "S", "operation": {"k": 1}},
                ))
            self.assertEqual(cm_cat.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_cat.exception.exit_code, c.EXIT_USAGE)

        # task.create depends_on
        for bad_deps in (123, [{}], False, "", [""]):
            with self.assertRaises(MeshError, msg=repr(bad_deps)) as cm_td:
                handle_task_create(_user_ctx(
                    self.conn, self.home, "task.create",
                    {"project": pid, "title": "T", "spec": "S", "depends_on": bad_deps},
                ))
            self.assertEqual(cm_td.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_td.exception.exit_code, c.EXIT_USAGE)

        # project.create depends_on_project falsy
        for bad_pdeps in (False, "", [{}]):
            with self.assertRaises(MeshError, msg=repr(bad_pdeps)) as cm_pd:
                handle_project_create(_user_ctx(
                    self.conn, self.home, "project.create",
                    {"title": "P2", "goal": "G2", "criterion": ["C1"], "lead": "lead1", "depends_on_project": bad_pdeps},
                ))
            self.assertEqual(cm_pd.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_pd.exception.exit_code, c.EXIT_USAGE)

        # project.update goal + falsy non-list criterion
        for bad_crit in (False, 0, [{}]):
            with self.assertRaises(MeshError, msg=repr(bad_crit)) as cm_pu:
                handle_project_update(_user_ctx(
                    self.conn, self.home, "project.update",
                    {"project_id": pid, "goal": "G_new", "criterion": bad_crit},
                ))
            self.assertEqual(cm_pu.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_pu.exception.exit_code, c.EXIT_USAGE)

        # inbox.ack non-list/non-str messages
        for bad_msgs in (123, [{}]):
            with self.assertRaises(MeshError, msg=repr(bad_msgs)) as cm_ia:
                handle_inbox_ack(_user_ctx(
                    self.conn, self.home, "inbox.ack",
                    {"messages": bad_msgs},
                ))
            self.assertEqual(cm_ia.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_ia.exception.exit_code, c.EXIT_USAGE)

    def test_nul_byte_path_and_setup_approval_mode_guards(self) -> None:
        from mesh.cli.commands import intake as intake_cli
        from mesh.cli.commands import project as project_cli
        from mesh.core.repos import validate_repo

        # validate_repo & CLI abspath guards on NUL bytes
        with self.assertRaises(MeshError) as cm_r:
            validate_repo("/tmp/bad\x00repo", "main")
        self.assertEqual(cm_r.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm_r.exception.exit_code, c.EXIT_USAGE)

        self.assertEqual(project_cli._abs_repo("bad\x00repo"), "bad\x00repo")
        self.assertEqual(project_cli._abs_repo("   "), "   ")

        captured = []

        class _CapClient:
            def __init__(self, home=None):
                pass

            def execute_cmd(self, args, cmd, payload):
                captured.append(dict(payload))
                return {"data": {}}

        with mock.patch.object(intake_cli, "MeshClient", _CapClient):
            intake_cli.handle_intake_submit(SimpleNamespace(home=self.home, repo="bad\x00repo"))
        self.assertEqual(captured[-1]["repo"], "bad\x00repo")

        # _check_out_path NUL byte guard
        with self.assertRaises(MeshError) as cm_o:
            agent_cli._check_out_path(self.home, "bad\x00out", False)
        self.assertEqual(cm_o.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm_o.exception.exit_code, c.EXIT_USAGE)

        # task submit --deliverable with NUL byte raises exit 2 instead of ValueError
        with mock.patch.object(task_cli, "MeshClient", side_effect=AssertionError("must not call daemon")):
            with self.assertRaises(MeshError) as cm_s:
                task_cli.handle_task_submit(SimpleNamespace(
                    home=self.home, task_id="tsk_1", run="run_1", fence=1,
                    deliverable="bad\x00path", summary=None, _from_json=set(),
                ))
        self.assertEqual(cm_s.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm_s.exception.exit_code, c.EXIT_USAGE)

        # setup --approval-mode invalid rejected before side effects
        ns = argparse.Namespace(
            home=os.path.join(self.tmp, "am-home"), dry_run=False, no_start=False,
            no_lead=True, lead_name="lead", token_dir=None, approval_mode="bogus",
            passphrase_fd=None, passphrase_file=None,
        )
        with mock.patch.object(setup_cli, "_run", side_effect=AssertionError("must not spawn subprocess")), \
                mock.patch("mesh.core.passphrase.read_passphrase", side_effect=AssertionError("must not prompt")):
            with self.assertRaises(MeshError) as cm_am:
                setup_cli.handle_setup(ns)
        self.assertEqual(cm_am.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm_am.exception.exit_code, c.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
