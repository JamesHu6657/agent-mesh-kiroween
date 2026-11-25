"""Focused unit regression tests for batch7 g5 findings (adapters and CLI)."""

import sys
import argparse
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mesh.constants as c
from mesh.adapters import smoke_schema
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.base import (
    RunRequest,
    parse_version_output,
    validate_adapter_options,
)
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.cli.commands import adapter as adapter_cli
from mesh.cli.commands import doctor as doctor_cli
from mesh.cli.commands import setup as setup_cli
from mesh.cli.commands import task as task_cli
from mesh.cli.main import main as cli_main
from mesh.client import MeshClient
from mesh.core.db import connect, init_db, transaction
from mesh.core.invariants import check_invariants
from mesh.daemon.handlers.adapter import handle_adapter_probe
from mesh.daemon.handlers.agent import handle_agent_create
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.supervisor import _vendor_nonzero_failure
from mesh.errors import MeshError
from tests import tierlib
from tests.smoke import harness
from tests.smoke import reevaluate as ev


def _user_ctx(conn, home: str, cmd: str, args: dict, *, testing_mode: bool = True) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_unit_g5",
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


class TestBatch7G5Unit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-g5-u-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _exe(self, rel_path: str, script: str, win_cmd: str = "@echo off\r\necho ok\r\n") -> str:
        if sys.platform == "win32":
            p = os.path.join(self.tmp, rel_path + ".cmd")
            os.makedirs(os.path.dirname(p), mode=0o755, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(win_cmd)
            return p
        p = os.path.join(self.tmp, rel_path)
        os.makedirs(os.path.dirname(p), mode=0o755, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(p, 0o755)
        return p

    # F1: Unvalidated adapter_options types and leading-dash values -----------------
    def test_f1_rejects_bad_adapter_options_and_leading_dash_values(self) -> None:
        prompt = os.path.join(self.tmp, "prompt.md")
        with open(prompt, "w", encoding="utf-8") as f:
            f.write("hello\n")

        # Adapter.build() validation for options types and leading '-'
        for adapter, bad_opts in (
            (KiroAdapter(), {"agent": True}),
            (KiroAdapter(), {"agent": "--trust-all-tools"}),
            (AgyAdapter(), {"agent": 123}),
            (AgyAdapter(), {"agent": "--skip-permissions-unsafe"}),
            (AgyAdapter(), {"sandbox": "true"}),
            (VibeAdapter(), {"max_turns": None}),
            (VibeAdapter(), {"max_turns": True}),
            (VibeAdapter(), {"max_turns": 0}),
            (VibeAdapter(), {"max_tokens": "--auto-approve"}),
            (VibeAdapter(), {"max_price_usd": -5}),
            (VibeAdapter(), {"trust_workdir": "yes"}),
        ):
            bin_path = self._exe(f"bin-{adapter.id}/{adapter.id}", "#!/bin/sh\necho ok\n")
            opts = dict(bad_opts, binary=bin_path, run_id="run_1")
            req = RunRequest(
                prompt_path=prompt,
                workspace=self.tmp,
                model="m1",
                effort=None,
                profile="readonly",
                timeout_s=30,
                options=opts,
            )
            with self.assertRaises(MeshError, msg=(adapter.id, bad_opts)) as cm:
                adapter.build(req)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

        # Leading '-' on model, effort, and resume_session_id must be rejected by build()
        for field_kw in (
            {"model": "--skip-permissions-unsafe"},
            {"effort": "--high"},
            {"resume_session_id": "--auto-approve"},
        ):
            for cls in (DroidAdapter, KiroAdapter, AgyAdapter, VibeAdapter):
                ad = cls()
                bin_path = self._exe(f"bin2-{ad.id}/{ad.id}", "#!/bin/sh\necho ok\n")
                kw = dict(
                    prompt_path=prompt,
                    workspace=self.tmp,
                    model="m1",
                    effort=None,
                    profile="readonly",
                    timeout_s=30,
                    options={"binary": bin_path, "run_id": "run_1"},
                )
                kw.update(field_kw)
                req = RunRequest(**kw)
                with self.assertRaises(MeshError, msg=(ad.id, field_kw)):
                    ad.build(req)

        # parse() ignores leading-dash session IDs emitted by vendor streams/exports
        for cls, fname, content in (
            (DroidAdapter, "stdout.jsonl", json.dumps({"type": "system", "subtype": "init", "session_id": "--auto-approve"}) + "\n"),
            (KiroAdapter, "stdout.jsonl", json.dumps({"type": "runStarted", "sessionId": "--trust-all-tools"}) + "\n"),
            (AgyAdapter, "stdout.jsonl", json.dumps({"event": "system", "conversation_id": "--yolo"}) + "\n"),
            (VibeAdapter, "stdout.jsonl", json.dumps({"type": "message", "role": "assistant", "sessionId": "--auto-approve", "content": [{"type": "text", "text": "ok"}]}) + "\n"),
        ):
            rdir = os.path.join(self.tmp, f"parse-{cls.id}")
            os.makedirs(rdir, exist_ok=True)
            with open(os.path.join(rdir, fname), "w", encoding="utf-8") as f:
                f.write(content)
            parsed = cls().parse(rdir)
            self.assertIsNone(parsed.session_id, cls.id)

        # handle_agent_create validates options and allowed_models
        home, conn = _setup_home_db(self.tmp)
        self.addCleanup(conn.close)
        bad_creates = [
            {"name": "k1", "adapter": "kiro", "option": ["agent=true"], "allowed_models": ["m1"], "default_model": "m1"},
            {"name": "k2", "adapter": "kiro", "option": ["agent=--trust-all-tools"], "allowed_models": ["m1"], "default_model": "m1"},
            {"name": "d1", "adapter": "droid", "allowed_models": ["--skip-permissions-unsafe"], "default_model": "--skip-permissions-unsafe"},
            {"name": "v1", "adapter": "vibe", "option": ["max_turns=null", "max_tokens=--auto-approve"], "allowed_models": ["m1"], "default_model": "m1"},
        ]
        for args in bad_creates:
            with self.assertRaises(MeshError, msg=args) as cm:
                handle_agent_create(_user_ctx(conn, home, "agent.create", args))
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

        # Valid adapter options pass cleanly
        clean = validate_adapter_options("vibe", {"trust_workdir": True, "max_turns": 5, "max_price_usd": 1.5, "max_tokens": 2000})
        self.assertEqual(clean["max_turns"], 5)

    # F3: adapter probe normalized --binary & require adapter_id with --binary --
    def test_f3_probe_binary_requires_adapter_id_and_normalizes_path(self) -> None:
        droid_bin = self._exe(
            "relbin/droid",
            "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'droid 1.6.0'; fi\necho 'Usage: droid'\n",
            "@echo off\r\nif \"%~1\"==\"--version\" echo droid 1.6.0\r\necho Usage: droid\r\n",
        )
        rel_bin = os.path.relpath(droid_bin, self.tmp)
        home, conn = _setup_home_db(self.tmp)
        self.addCleanup(conn.close)

        # CLI rejects --binary without adapter_id
        with self.assertRaises(MeshError) as cm:
            adapter_cli.handle_adapter_probe(
                argparse.Namespace(home=home, adapter_id=None, binary=droid_bin, if_version=None, user=True)
            )
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

        # Handler rejects --binary without adapter_id
        with self.assertRaises(MeshError) as cm:
            handle_adapter_probe(_user_ctx(conn, home, "adapter.probe", {"binary": droid_bin}), testing_mode=True)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

        # Relative --binary is normalized to abspath when stored in DB
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            res = handle_adapter_probe(
                _user_ctx(conn, home, "adapter.probe", {"adapter": "droid", "binary": os.path.join(".", rel_bin)}),
                testing_mode=True,
            )
        finally:
            os.chdir(old_cwd)
        row = conn.execute("SELECT binary_path FROM adapter_probes WHERE adapter = 'droid';").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["binary_path"], os.path.abspath(droid_bin))
        self.assertEqual(res.data["binary_path"], os.path.abspath(droid_bin))

    # F4: parse_version_output preserves 4+ component versions ---------------------
    def test_f4_parse_version_output_preserves_four_part_versions(self) -> None:
        self.assertEqual(parse_version_output("droid 1.6.0.1\n"), "1.6.0.1")
        self.assertEqual(parse_version_output("version 2.10.3.4-rc1"), "2.10.3.4-rc1")
        self.assertEqual(parse_version_output("v1.2.3.4.5"), "1.2.3.4.5")
        self.assertEqual(parse_version_output("droid 1.6.0"), "1.6.0")

    # F5: Droid/Kiro subcommand --help failures & verification='none' -------------
    def test_f5_probe_subcommand_help_failure_is_error_and_resets_verification(self) -> None:
        droid_bad_exec = self._exe(
            "b5/droid",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'droid 1.6.0'; exit 0; fi\n"
            "if [ \"$1\" = \"exec\" ]; then echo 'broken exec' >&2; exit 2; fi\n"
            "echo 'Usage: droid'\nexit 0\n",
            "@echo off\r\n"
            "if \"%~1\"==\"--version\" (echo droid 1.6.0& exit /b 0)\r\n"
            "if \"%~1\"==\"exec\" (echo broken exec>&2& exit /b 2)\r\n"
            "echo Usage: droid\r\nexit /b 0\r\n",
        )
        p_d = DroidAdapter().probe(binary_path=droid_bad_exec)
        self.assertEqual(p_d.status, "error")
        self.assertTrue(all(v["verification"] == "none" for v in p_d.capabilities.values()))

        kiro_bad_chat = self._exe(
            "b5/kiro-cli",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'kiro-cli 3.1.0'; exit 0; fi\n"
            "if [ \"$1\" = \"chat\" ]; then echo 'broken chat' >&2; exit 2; fi\n"
            "echo 'Usage: kiro-cli'\nexit 0\n",
            "@echo off\r\n"
            "if \"%~1\"==\"--version\" (echo kiro-cli 3.1.0& exit /b 0)\r\n"
            "if \"%~1\"==\"chat\" (echo broken chat>&2& exit /b 2)\r\n"
            "echo Usage: kiro-cli\r\nexit /b 0\r\n",
        )
        p_k = KiroAdapter().probe(binary_path=kiro_bad_chat)
        self.assertEqual(p_k.status, "error")
        self.assertTrue(all(v["verification"] == "none" for v in p_k.capabilities.values()))

        droid_bad_help = self._exe(
            "b5h/droid",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'droid 1.6.0'; exit 0; fi\n"
            "echo 'help failed' >&2; exit 1\n",
            "@echo off\r\n"
            "if \"%~1\"==\"--version\" (echo droid 1.6.0& exit /b 0)\r\n"
            "echo help failed>&2\r\nexit /b 1\r\n",
        )
        p_h = DroidAdapter().probe(binary_path=droid_bad_help)
        self.assertEqual(p_h.status, "error")
        self.assertTrue(all(v["verification"] == "none" for v in p_h.capabilities.values()))

    # F6: setup token dir perms, non-NOT_FOUND abort, default_agent preserved -----
    def test_f6_setup_checks_token_dir_not_found_and_preserves_default_agent(self) -> None:
        home = os.path.join(self.tmp, "setup-home")
        os.makedirs(home, mode=0o700)
        world_dir = os.path.join(self.tmp, "world-tok")
        os.makedirs(world_dir)
        if sys.platform == "win32":
            from mesh.platform import perms as plat_perms
            from tests.helpers import restore_accessible_for_test
            plat_perms.grant_everyone_write_for_test(world_dir)
            self.addCleanup(restore_accessible_for_test, world_dir)
        else:
            os.chmod(world_dir, 0o777)
        with self.assertRaises(MeshError) as cm:
            setup_cli._check_token_destination(home, os.path.join(world_dir, "lead.token"))
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

        # Wrong passphrase / non-NOT_FOUND agent show error must NOT proceed to agent create
        priv_home, _pconn = _setup_home_db(os.path.join(self.tmp, "init-parent"))
        _pconn.close()
        tok_dir = os.path.join(self.tmp, "priv-tok")
        os.makedirs(tok_dir, mode=0o700)

        calls = []

        def fake_run_wrong_pass(_home, argv, _passphrase=None):
            calls.append(argv)
            if argv[:2] == ["daemon", "status"]:
                return 0, {"ok": True, "data": {"running": True}}
            if argv[:2] == ["agent", "show"]:
                return c.EXIT_UNAUTHENTICATED, {"ok": False, "error": {"code": c.PASSPHRASE_INVALID, "message": "bad pass"}}
            return 0, {"ok": True, "data": {}}

        args = argparse.Namespace(
            home=priv_home,
            lead_name="lead",
            no_lead=False,
            no_start=False,
            token_dir=tok_dir,
            approval_mode=None,
            dry_run=False,
            passphrase_fd=None,
            passphrase_file=None,
        )
        with mock.patch("mesh.core.passphrase.read_passphrase", return_value="wrong-passphrase-123"), \
                mock.patch.object(setup_cli, "_run", side_effect=fake_run_wrong_pass):
            with self.assertRaises(MeshError) as cm:
                setup_cli.handle_setup(args)
        self.assertEqual(cm.exception.code, c.PASSPHRASE_INVALID)
        self.assertFalse(any(a[:2] == ["agent", "create"] for a in calls))

        # Re-run setup skips lead.default_agent overwrite and passes testing_mode to environment_checks
        calls2 = []

        def fake_run_preserve(_home, argv, _passphrase=None):
            calls2.append(argv)
            if argv[:2] == ["daemon", "status"]:
                return 0, {"ok": True, "data": {"running": True}}
            if argv[:2] == ["agent", "show"]:
                return 0, {"ok": True, "data": {"name": "lead"}}
            if argv[:3] == ["config", "get", "lead.default_agent"]:
                return 0, {"ok": True, "data": {"key": "lead.default_agent", "value": "custom-lead"}}
            return 0, {"ok": True, "data": {}}

        seen_testing = []

        def fake_env_checks(_home, _db_present, **kwargs):
            seen_testing.append(kwargs.get("testing_mode"))
            return []

        with mock.patch("mesh.core.passphrase.read_passphrase", return_value="correct-pass-123"), \
                mock.patch.object(setup_cli, "_run", side_effect=fake_run_preserve), \
                mock.patch.object(setup_cli, "_token_works", return_value=True), \
                mock.patch("mesh.core.settings.is_testing_mode", return_value=True), \
                mock.patch("mesh.cli.commands.doctor_env.environment_checks", side_effect=fake_env_checks):
            out = setup_cli.handle_setup(args)
        self.assertFalse(any(a[:3] == ["config", "set", "lead.default_agent"] for a in calls2))
        self.assertTrue(any("custom-lead" in s for s in out["skipped"]))
        self.assertEqual(seen_testing, [True])

    # F7: doctor --check-invariants read snapshot & invariants I1/I3/I8 ----------
    def test_f7_doctor_snapshot_and_invariants_i1_i3_i8(self) -> None:
        home, conn = _setup_home_db(self.tmp)
        now = "2026-01-01T00:00:00.000Z"
        with transaction(conn):
            conn.execute(
                "INSERT INTO credentials (id, principal_id, kind, secret_hash, kdf_params, created_at) "
                "VALUES ('cred_u1', 'usr_1', 'passphrase', X'00', "
                "'{\"n\":32768,\"r\":8,\"p\":1,\"dklen\":64,\"maxmem\":67108864}', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ibx', 'agent', 'ibx1', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_ibx', 'none', 'inbox', 0, 1, ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lnch', 'agent', 'lnch1', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lnch', 'fake', 'launch', 0, 1, ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P1', 'G1', 'active', 'usr_1', 'user', ?, ?);",
                (now, now),
            )
            # 1) I8: inbox task stuck in cancelling without active/ending run or lease
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, profile, timeout_s, max_attempts, assignee_agent_id, status, created_by, created_at, updated_at) "
                "VALUES ('tsk_i8', 'prj_1', 'I8', 'spec', 'general', 'ro', 60, 1, 'agt_ibx', 'cancelling', 'usr_1', ?, ?);",
                (now, now),
            )
            # 2) I1: running task whose lease points to wrong run_id + I3 NULL slot cols on task:* lease
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, profile, timeout_s, max_attempts, assignee_agent_id, status, created_by, created_at, updated_at) "
                "VALUES ('tsk_i1', 'prj_1', 'I1', 'spec', 'code', 'edit', 60, 1, 'agt_lnch', 'running', 'usr_1', ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, fence, status, boot_id, run_dir) "
                "VALUES ('run_live', 'task', 'tsk_i1', 'prj_1', 'agt_lnch', 'fake', 1, 'running', 'b1', '/tmp/r1');"
            )
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, fence, status, boot_id, run_dir) "
                "VALUES ('run_old', 'task', 'tsk_i1', 'prj_1', 'agt_lnch', 'fake', 0, 'exited', 'b1', '/tmp/r0');"
            )
            conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, "
                "slots_in_use_at_acquire, limit_at_acquire, acquired_at, expires_at, heartbeat_at) "
                "VALUES ('task:tsk_i1', 'run_old', 'agent', 'run_old', NULL, 0, NULL, NULL, ?, '2099-01-01T00:00:00.000Z', ?);",
                (now, now),
            )
            # 3) I1: active run whose leases row was deleted
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, profile, timeout_s, max_attempts, assignee_agent_id, status, created_by, created_at, updated_at) "
                "VALUES ('tsk_i1_nl', 'prj_1', 'I1NL', 'spec', 'code', 'edit', 60, 1, 'agt_lnch', 'running', 'usr_1', ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, adapter, fence, status, boot_id, run_dir) "
                "VALUES ('run_nolease', 'task', 'tsk_i1_nl', 'prj_1', 'agt_lnch', 'fake', 1, 'running', 'b1', '/tmp/rnl');"
            )

        viols = check_invariants(conn)
        conn.close()
        by_inv = [(v.invariant, v.message) for v in viols]
        self.assertTrue(any(inv == "I8" and "tsk_i8" in msg for inv, msg in by_inv), by_inv)
        self.assertTrue(any(inv == "I1" and "points to run run_old" in msg for inv, msg in by_inv), by_inv)
        self.assertTrue(any(inv == "I3" and "missing concurrency snapshot columns" in msg for inv, msg in by_inv), by_inv)
        self.assertTrue(any(inv == "I1" and "run_nolease" in msg and "has no task lease" in msg for inv, msg in by_inv), by_inv)

        # Verify handle_doctor wraps --check-invariants reads in BEGIN DEFERRED ... ROLLBACK
        executed_sql = []
        real_connect = connect

        class _SpyConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=()):
                executed_sql.append(sql)
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with mock.patch.object(doctor_cli, "connect", side_effect=lambda p, **kw: _SpyConn(real_connect(p, **kw))):
            with self.assertRaises(MeshError) as cm_doc:
                doctor_cli.handle_doctor(argparse.Namespace(home=home, fix_perms=False, check_invariants=True, quiet=False))
        self.assertGreaterEqual(executed_sql.count("BEGIN DEFERRED;"), 2)
        self.assertGreaterEqual(executed_sql.count("ROLLBACK;"), 2)
        self.assertGreaterEqual(len(cm_doc.exception.details["violations"]), 4)

    # F8 & F13: CLI --input validation, --, prefix flags, event tail, subcommand help
    def test_f8_and_f13_cli_input_validation_and_missing_subcommand_json(self) -> None:
        home = os.path.join(self.tmp, "cli-home")
        os.makedirs(home, mode=0o700)

        # F13: missing verb prints JSON error in json mode and subcommand help in text mode
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--home", home, "--format", "json", "task"])
        self.assertEqual(rc, c.EXIT_USAGE)
        env = json.loads(buf.getvalue())
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = cli_main(["--home", home, "--format", "text", "agent", "token"])
        self.assertEqual(rc2, c.EXIT_USAGE)
        self.assertIn("issue", buf2.getvalue())

        # F8: missing required keys in --input JSON exit 2 (NOT INTERNAL_ERROR 1)
        for argv, stdin_data in (
            (["--home", home, "config", "set", "--input", "-"], "{}"),
            (["--home", home, "config", "set", "--input", "-"], '{"key": 123, "value": "x"}'),
            (["--home", home, "agent", "onboard", "--input", "-"], "{}"),
            (["--home", home, "event", "tail", "--input", "-"], '{"timeout": true}'),
            (["--home", home, "project", "complete", "prj_1", "--input", "-"], '{"report": 123}'),
            (["--home", home, "agent", "create", "--input", "-"], '{"name": "a1", "adapter": "none", "capabilities": ["bogus"]}'),
        ):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(stdin_data)), redirect_stdout(buf):
                rc = cli_main(argv)
            self.assertEqual(rc, c.EXIT_USAGE, (argv, stdin_data, buf.getvalue()))
            err = json.loads(buf.getvalue())["error"]
            self.assertEqual(err["code"], c.INVALID_ARGUMENT)

        # F8: event tail --input with {"timeout": "5s"} and -- stops CLI flag scanning,
        # and --inp prefix triggers JSON input cleanly
        captured_calls = []

        def fake_stream(self, args, cmd, cmd_args=None, auth_override=None):
            captured_calls.append((cmd, cmd_args, getattr(args, "timeout", None), getattr(args, "title", None)))
            return {"ok": True}

        with mock.patch.object(MeshClient, "stream_cmd", fake_stream):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO('{"timeout": "5s"}')), redirect_stdout(buf):
                rc = cli_main(["--home", home, "event", "tail", "--input", "-"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(captured_calls[-1][0], "event.tail")
        self.assertEqual(captured_calls[-1][1]["timeout"], "5s")
        self.assertEqual(captured_calls[-1][2], "5s")

        captured_exec = []

        def fake_exec(self, args, cmd, cmd_args=None, auth_override=None):
            captured_exec.append((cmd, cmd_args))
            return {"ok": True, "data": {"id": "tsk_1"}}

        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO('{"reason": "boom", "run": "run_1", "fence": 1}')), redirect_stdout(buf):
                rc = cli_main(["--home", home, "task", "fail", "--inp", "-", "--", "--project"])
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertEqual(captured_exec[-1][1]["task_id"], "--project")
            self.assertEqual(captured_exec[-1][1]["reason"], "boom")
            self.assertEqual(captured_exec[-1][1]["run"], "run_1")
            self.assertEqual(captured_exec[-1][1]["fence"], 1)

            # List-typed append choices (capabilities: []) & structured report dict accepted cleanly
            buf_cap = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO('{"name": "a1", "adapter": "none", "capabilities": []}')), redirect_stdout(buf_cap):
                rc_cap = cli_main(["--home", home, "agent", "create", "--input", "-"])
            self.assertEqual(rc_cap, 0, buf_cap.getvalue())
            self.assertEqual(captured_exec[-1][1]["capabilities"], [])

            buf_rep = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO('{"report": {"summary": "ok", "criteria": []}}')), redirect_stdout(buf_rep):
                rc_rep = cli_main(["--home", home, "project", "complete", "prj_1", "--input", "-"])
            self.assertEqual(rc_rep, 0, buf_rep.getvalue())
            self.assertEqual(captured_exec[-1][1]["report"], {"summary": "ok", "criteria": []})

        # -- stops relax_required_actions from treating trailing --input as an option
        err_buf = io.StringIO()
        out_buf = io.StringIO()
        with mock.patch("sys.stderr", err_buf), redirect_stdout(out_buf):
            rc_after_dd = cli_main(["--home", home, "task", "fail", "--", "--input"])
        self.assertEqual(rc_after_dd, c.EXIT_USAGE)

    # F9: MeshClient timeout validation & scoping ---------------------------------
    def test_f9_client_timeout_validation_and_scoping(self) -> None:
        home = os.path.join(self.tmp, "client-home")
        os.makedirs(home, mode=0o700)

        for cmd_name, bad_t in (("status", "bogus"), ("status", "0s"), ("event.tail", "nope")):
            client = MeshClient(home=home, timeout=30.0)
            args = argparse.Namespace(user=True, token_file=None, timeout=bad_t, request_id="req_1", dry_run=False)
            with mock.patch.object(client, "resolve_auth", return_value={"kind": "user"}), \
                    mock.patch.object(client, "send_request", return_value={"ok": True, "data": {}}):
                with self.assertRaises(MeshError) as cm:
                    if cmd_name == "event.tail":
                        client.stream_cmd(args, cmd_name, {"timeout": bad_t})
                    else:
                        client.execute_cmd(args, cmd_name, {})
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

        # status --timeout 2s lowers client timeout to 2.0 (not clamped to 30.0)
        client_fast = MeshClient(home=home, timeout=30.0)
        args_fast = argparse.Namespace(user=True, token_file=None, timeout="2s", request_id="req_2", dry_run=False)
        with mock.patch.object(client_fast, "resolve_auth", return_value={"kind": "user"}), \
                mock.patch.object(client_fast, "send_request", return_value={"ok": True, "data": {}}):
            client_fast.execute_cmd(args_fast, "status", {})
        self.assertEqual(client_fast.timeout, 2.0)

        # task.create with run --timeout 7d preserves client's 30s socket timeout
        client_create = MeshClient(home=home, timeout=30.0)
        args_create = argparse.Namespace(user=True, token_file=None, timeout=None, request_id="req_3", dry_run=False)
        with mock.patch.object(client_create, "resolve_auth", return_value={"kind": "user"}), \
                mock.patch.object(client_create, "send_request", return_value={"ok": True, "data": {}}):
            client_create.execute_cmd(args_create, "task.create", {"timeout": "7d"})
        self.assertEqual(client_create.timeout, 30.0)

        # *.wait adds +15.0 slack
        client_wait = MeshClient(home=home, timeout=30.0)
        args_wait = argparse.Namespace(user=True, token_file=None, timeout="5s", request_id="req_4", dry_run=False)
        with mock.patch.object(client_wait, "resolve_auth", return_value={"kind": "user"}), \
                mock.patch.object(client_wait, "send_request", return_value={"ok": True, "data": {}}):
            client_wait.execute_cmd(args_wait, "task.wait", {"timeout": "5s"})
        self.assertEqual(client_wait.timeout, 20.0)

    # F10: keepalive short retry sleep & preserves unset every ---------------------
    def test_f10_keepalive_preserves_unset_every_and_short_retry_sleep(self) -> None:
        home = os.path.join(self.tmp, "ka-home")
        os.makedirs(home, mode=0o700)
        sleeps = []
        hb_calls = [0]

        def fake_exec(_self, _args, cmd, _body=None, auth_override=None):
            if cmd == "task.heartbeat":
                hb_calls[0] += 1
                if hb_calls[0] == 1:
                    raise MeshError(c.DAEMON_UNAVAILABLE, exit_code=c.EXIT_DAEMON_UNAVAILABLE, message="down")
                return {"ok": True, "data": {"expires_at": "2099-01-01T00:00:15Z"}}
            if cmd == "task.show":
                return {
                    "ok": True,
                    "data": {
                        "status": "running",
                        "live_run": {"run_id": "run_1", "fence": 1, "lease_ttl_s": 15, "heartbeat_every_s": 5},
                    },
                }
            raise AssertionError(f"unexpected cmd {cmd}")

        def fake_sleep(secs):
            sleeps.append((hb_calls[0], secs))
            if hb_calls[0] >= 2:
                raise KeyboardInterrupt()

        args = argparse.Namespace(
            home=home,
            task_id="tsk_1",
            run="run_1",
            fence=1,
            every=None,
            dry_run=False,
            user=False,
            token_file="/tmp/none.tok",
            format="json",
            quiet=False,
            idempotency_key=None,
            relayed=False,
            approval=None,
            if_version=None,
            timeout=None,
        )
        with mock.patch.object(MeshClient, "execute_cmd", fake_exec), \
                mock.patch("time.sleep", side_effect=fake_sleep), \
                mock.patch.object(task_cli, "_with_session", lambda d: dict(d, session_token="tok_s")), \
                mock.patch("sys.stderr", io.StringIO()):
            try:
                res = task_cli.handle_task_keepalive(args)
            except KeyboardInterrupt:
                res = {"beats": 1}
        self.assertGreaterEqual(hb_calls[0], 2)
        beat1_sleep = sum(sec for b, sec in sleeps if b == 1)
        beat2_sleeps = [sec for b, sec in sleeps if b == 2]
        self.assertLessEqual(beat1_sleep, 2.0)  # transient failure retried quickly, NOT 40s
        self.assertTrue(beat2_sleeps and beat2_sleeps[0] <= 5.0)
        self.assertEqual(res["beats"], 1)

    # F14: smoke_schema symlinks/pass fixtures + reevaluate stderr/vibe export -----
    @unittest.skipUnless(sys.platform != "win32", "Smoke harness (tests/smoke/harness.py) is POSIX/Linux-only")
    def test_f14_smoke_symlinks_pass_fixtures_stderr_vibe_export_and_reeval(self) -> None:
        vibe_bin = self._exe(
            "bin-v14/vibe",
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']: print('vibe 3.1.0'); sys.exit(0)\n"
            "odir = args[args.index('--output-dir') + 1]\n"
            "os.makedirs(odir, exist_ok=True)\n"
            "print(json.dumps({'type':'message','role':'assistant','sessionId':'vs-1','content':[{'type':'text','text':'MESH_SMOKE_OK'}]}))\n"
            "with open(os.path.join(odir, 'export.json'), 'w') as f:\n"
            "    json.dump({'outcome':'error','stop_reason':'error','error':None,'session_id':'vs-1','exit_code':0,'schema_version':1,'config':{'active_model':'m1'}}, f)\n",
        )
        out = os.path.join(self.tmp, "out14")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MESH_SMOKE": "1",
            "MESH_SMOKE_ADAPTERS": "vibe",
            "MESH_SMOKE_CONSENT": "vibe",
            "MESH_SMOKE_MAX_RUNS": "2",
            "MESH_SMOKE_MODEL_VIBE": "m1",
            "MESH_SMOKE_TIMEOUT_S": "10",
            "MESH_SMOKE_OUT": out,
            "MESH_SMOKE_CONSENT_REF": "unit",
        }
        sess = harness.SmokeSession(env, "vibe", vibe_bin, grace_s=1, version="3.1.0", mesh_commit="abc")
        self.addCleanup(sess.cleanup)
        o1 = sess.run("S-X-1", "hi")
        parsed1 = sess.parse(o1)
        st1, rsn1, obs1 = ev.eval_sx1(o1.stdout, o1.rc, o1.timed_out, parsed1)
        self.assertEqual((st1, rsn1), ("fail", "vendor_error"))
        sess.record("S-X-1", st1, reason=rsn1, outcome=o1, observation=obs1)

        # Also stage a passing S-X-6 case AND an S-X-5 case with rc=2 and stderr naming 'effort'
        fx6_data = b'{"type":"message","sessionId":"vs-6"}\n'
        fx6_path = tierlib.write_capture(out, "vibe/3.1.0/smoke-S-X-6-stdout.jsonl", fx6_data, ["vibe"])
        fx5_data = b'{"type":"message","sessionId":"vs-5"}\n'
        fx5_path = tierlib.write_capture(out, "vibe/3.1.0/smoke-S-X-5-stdout.jsonl", fx5_data, ["vibe"])
        tierlib.write_capture(out, "vibe/3.1.0/smoke-S-X-5-stderr.txt", b"error: effort not allowed\n", ["vibe"])
        sess.cases.append({
            "id": "S-X-6", "status": "pass", "reason": None, "rc": -15, "duration_s": 0.5,
            "fixture": smoke_schema.fixture_rel("vibe", "3.1.0", os.path.basename(fx6_path)),
            "fixture_sha256": tierlib.sha256_bytes(fx6_data), "observation": None,
            "evaluated_from": None, "carried_from": None,
        })
        sess.cases.append({
            "id": "S-X-5", "status": "fail", "reason": "rc_nonzero", "rc": 2, "duration_s": 0.5,
            "fixture": smoke_schema.fixture_rel("vibe", "3.1.0", os.path.basename(fx5_path)),
            "fixture_sha256": tierlib.sha256_bytes(fx5_data), "observation": {"requested_effort": "low"},
            "evaluated_from": None, "carried_from": None,
        })
        res_path = sess.finish()

        # Re-evaluating preserves S-X-1 fail(vendor_error) from vibe-export.json AND reclassifies S-X-5 via stderr!
        rout = os.path.join(self.tmp, "reeval14")
        new_res = ev.reevaluate(res_path, rout)
        with open(new_res, encoding="utf-8") as f:
            new_doc = json.load(f)
        by_cid = {x["id"]: x for x in new_doc["cases"]}
        self.assertEqual((by_cid["S-X-1"]["status"], by_cid["S-X-1"]["reason"]), ("fail", "vendor_error"))
        self.assertEqual((by_cid["S-X-5"]["status"], by_cid["S-X-5"]["reason"]), ("not_applicable", "model_or_effort_rejected"))

        # S-X-6 passing with fixture=None is rejected
        bad_doc = json.loads(json.dumps(new_doc))
        for item in bad_doc["cases"]:
            if item["id"] == "S-X-6":
                item["fixture"] = None
                item["fixture_sha256"] = None
        self.assertTrue(any("needs a fixture" in e for e in smoke_schema.validate(bad_doc, self.tmp, fixture_root=out)))

        # Symlinked results file, fixture file, and intermediate directories are rejected
        sym_res = os.path.join(out, "results", "vibe-3.1.0-2099-01-01.json")
        os.symlink(res_path, sym_res)
        with open(res_path, "rb") as f:
            raw_res = f.read()
        with open(sym_res + ".meta.json", "wb") as f:
            f.write(tierlib.sidecar_bytes(["test"], raw_res))
        self.assertIsNotNone(smoke_schema._check_sidecar(sym_res))
        with self.assertRaises(tierlib.TierError):
            tierlib.read_sidecar(sym_res)
        self.assertNotEqual(smoke_schema.load_latest(os.path.join(out, "results"), "vibe"), sym_res)

        sym_root = os.path.join(self.tmp, "sym-fx-root")
        os.symlink(out, sym_root)
        self.assertTrue(smoke_schema.validate(new_doc, self.tmp, fixture_root=sym_root))

        # Missing version or unknown adapter in reevaluate raises ReevaluateError (not KeyError)
        missing_ver_path = os.path.join(self.tmp, "badver", "results", "droid-1.6.0-2026-11-20.json")
        bad_ver_doc = json.loads(json.dumps(new_doc))
        bad_ver_doc["adapter"] = "droid"
        bad_ver_doc.pop("version")
        tierlib.write_capture(os.path.join(self.tmp, "badver"), "results/droid-1.6.0-2026-11-20.json", json.dumps(bad_ver_doc).encode(), ["test"])
        with self.assertRaises(ev.ReevaluateError):
            ev.reevaluate(missing_ver_path, os.path.join(self.tmp, "rout-err"))

    # F15: Kiro ellipsis finalText, abs prompt_path, supervisor nonzero failure
    def test_f15_kiro_ellipsis_final_text_abs_prompt_path_and_vendor_exit(self) -> None:
        rdir = os.path.join(self.tmp, "k15")
        os.makedirs(rdir)
        lines = [
            json.dumps({"type": "metadata", "data": {"sessionId": "k-1"}}),
            json.dumps({"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Thinking... "}}}}),
            json.dumps({"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read", "kind": "read"}}}),
            json.dumps({"type": "runFinished", "data": {"finalText": "...", "status": "COMPLETED", "stopReason": "END_TURN"}}),
        ]
        with open(os.path.join(rdir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        res = KiroAdapter().parse(rdir)
        self.assertEqual(res.final_text, "...")
        self.assertEqual(res.session_id, "k-1")

        # Relative prompt_path normalized to abspath in >100KB pointer sentence
        rel_prompt = "big_prompt.md"
        abs_prompt = os.path.join(self.tmp, rel_prompt)
        with open(abs_prompt, "w", encoding="utf-8") as f:
            f.write("x" * 200)
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            for cls in (AgyAdapter, KiroAdapter):
                ad = cls()
                ad.max_argv_prompt_bytes = 20
                bpath = self._exe(f"bin15-{ad.id}/{ad.id}", "#!/bin/sh\necho ok\n")
                req = RunRequest(
                    prompt_path=rel_prompt,
                    workspace=self.tmp,
                    model="m1",
                    effort=None,
                    profile="edit",
                    timeout_s=30,
                    options={"binary": bpath, "run_id": "r15"},
                )
                spec = ad.build(req)
                prompt_arg = spec.argv[spec.argv.index("--print") + 1] if ad.id == "agy" else spec.argv[-1]
                self.assertIn(abs_prompt, prompt_arg, ad.id)
        finally:
            os.chdir(old_cwd)

        # Supervisor maps vibe exit 3 -> timeout and Kiro stderr effort rejection -> model_or_effort_rejected
        v_dir = os.path.join(self.tmp, "v15")
        os.makedirs(v_dir)
        reason_v, _ = _vendor_nonzero_failure(v_dir, "vibe", 3, testing_mode=True)
        self.assertEqual(reason_v, "timeout")

        k_dir = os.path.join(self.tmp, "k15-err")
        os.makedirs(k_dir)
        with open(os.path.join(k_dir, "stderr.log"), "w", encoding="utf-8") as f:
            f.write("failed to set effort: model does not support additional fields\n")
        reason_k, _ = _vendor_nonzero_failure(k_dir, "kiro", 2, testing_mode=True)
        self.assertEqual(reason_k, "model_or_effort_rejected")


if __name__ == "__main__":
    unittest.main()
