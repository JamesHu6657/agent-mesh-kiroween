"""Regression tests for full2-r3.md Group H findings (P1-12, P2-10 setup.py, P2-11)."""

import argparse
import asyncio
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import setup as setup_cli
from mesh.cli.main import main as cli_main
from mesh.client import MeshClient
from mesh.core import gitops, init as mesh_init, passphrase as mesh_passphrase, settings, workspaces
from mesh.core.db import init_db, transaction
from mesh.core.ids import generate_id
from mesh.daemon.handlers import agent as agent_handlers
from mesh.daemon.handlers import config as config_handlers
from mesh.daemon.handlers import dep as dep_handlers
from mesh.daemon.handlers import inbox as inbox_handlers
from mesh.daemon.handlers import task as task_handlers
from mesh.daemon.handlers.base import HandlerContext
from mesh.errors import MeshError
from mesh.platform import perms as plat_perms
from tests.gitfixture import GitFixture
from tests.helpers import restore_accessible_for_test, set_world_readable_for_test


PASS = "correct-horse-battery-staple"


def _make_db(tmp: str):
    home = os.path.join(tmp, "home")
    for sub in ("logs", "run", "runs", "workspaces"):
        os.makedirs(os.path.join(home, sub), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    ts = "2026-01-01T00:00:00.000Z"
    with transaction(conn):
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (ts,))
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);", (ts,))
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, '[\"intake\"]', 2, 'edit', ?, ?);",
            (ts, ts),
        )
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ibx', 'agent', 'ibx1', ?);", (ts,))
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, "
            "default_profile, created_at, updated_at) "
            "VALUES ('agt_ibx', 'none', 'inbox', 0, '[\"intake\"]', 2, 'edit', ?, ?);",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at) "
            "VALUES ('prj_1', 'Proj1', 'Goal1', 'active', 'usr_1', 'direct', 1, ?, ?);",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (ts,),
        )
    return home, conn


def _ctx(
    conn,
    home: str,
    cmd: str,
    args: dict,
    *,
    principal: dict | None = None,
    actor_kind: str = "user",
    via: str = "direct",
) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal=principal or {"id": "usr_1", "kind": "user", "name": "user"},
        actor_kind=actor_kind,
        via=via,
        request_id=generate_id("req"),
        home=home,
        cmd=cmd,
        args=args,
        testing_mode=True,
    )


class TestP1_12CliBooleanOptionalAndHandlerArgValidation(unittest.TestCase):
    """P1-12: BooleanOptionalAction --input validation and handler arg/type/bound checks."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3h-p112-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.conn = _make_db(self.tmp)
        self.addCleanup(self.conn.close)

    def test_cli_input_boolean_optional_action_can_lead_rejects_non_bool(self) -> None:
        for bad_json in ('{"can_lead": "false"}', '{"can_lead": 1}', '{"can_lead": ["x"]}'):
            buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(bad_json)), \
                    mock.patch.object(MeshClient, "execute_cmd", side_effect=AssertionError("must not call daemon")), \
                    redirect_stdout(buf):
                rc = cli_main(["--home", self.home, "agent", "update", "ibx1", "--input", "-", "--user"])
            env = json.loads(buf.getvalue())
            self.assertEqual(rc, c.EXIT_USAGE, (bad_json, env))
            self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT, (bad_json, env))
            self.assertEqual(env["error"]["details"].get("param"), "can_lead", (bad_json, env))

        seen = []

        def fake_exec(_self, _args, cmd, cmd_args=None, auth_override=None):
            seen.append((cmd, cmd_args))
            return {"ok": True, "data": {}}

        with mock.patch.object(MeshClient, "execute_cmd", fake_exec):
            for val in (False, True):
                buf = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(json.dumps({"can_lead": val}))), redirect_stdout(buf):
                    rc = cli_main(["--home", self.home, "agent", "update", "ibx1", "--input", "-", "--user"])
                self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(seen[0][1]["can_lead"], False)
        self.assertEqual(seen[1][1]["can_lead"], True)

    def test_task_handlers_reject_invalid_types_and_int64_overflow(self) -> None:
        ts = "2026-01-01T00:00:00.000Z"
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_f', 'prj_1', 'T', 'S', 'general', 'failed', 'agt_ibx', 'edit', 60, 2, 'usr_1', ?, ?);",
                (ts, ts),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, created_by, created_at, updated_at) "
                "VALUES ('tsk_r', 'prj_1', 'T', 'S', 'general', 'running', 'agt_ibx', 'edit', 60, 2, 'usr_1', ?, ?);",
                (ts, ts),
            )
            self.conn.execute(
                "INSERT INTO runs (id, kind, task_id, project_id, agent_id, attempt, fence, adapter, status, "
                "boot_id, run_dir) VALUES ('run_r', 'external', 'tsk_r', 'prj_1', 'agt_ibx', 1, 1, 'none', "
                "'running', 'boot_1', '/tmp/none');"
            )
            self.conn.execute(
                "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, acquired_at, "
                "expires_at, heartbeat_at) VALUES ('task:tsk_r', 'run_r', 'agent', 'agt_ibx', 'agt_ibx', "
                "1, '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z', '2026-01-01T00:00:00Z');"
            )

        bad_create_cases = [
            {"project": ["prj_1"], "title": "T", "spec": "S"},
            {"project": "prj_1", "title": ["T"], "spec": "S"},
            {"project": "prj_1", "title": "T", "spec": 123},
            {"project": "prj_1", "title": "T", "spec": "S", "priority": True},
            {"project": "prj_1", "title": "T", "spec": "S", "priority": 1.5},
            {"project": "prj_1", "title": "T", "spec": "S", "priority": 10**20},
            {"project": "prj_1", "title": "T", "spec": "S", "priority": -(10**20)},
            {"project": "prj_1", "title": "T", "spec": "S", "timeout": True},
            {"project": "prj_1", "title": "T", "spec": "S", "idle_timeout": True},
            {"project": "prj_1", "title": "T", "spec": "S", "max_attempts": True},
            {"project": "prj_1", "title": "T", "spec": "S", "model": ["bad"]},
            {"project": "prj_1", "title": "T", "spec": "S", "effort": ["bad"]},
            {"project": "prj_1", "title": "T", "spec": "S", "profile": ["bad"]},
            {"project": "prj_1", "title": "T", "spec": "S", "assignee": ["ibx1"]},
            {"project": "prj_1", "title": "T", "spec": "S", "assignee": "ibx1", "model": ["bad"]},
        ]
        for args in bad_create_cases:
            with self.subTest(cmd="task.create", args=args):
                with self.assertRaises(MeshError) as cm:
                    task_handlers.handle_task_create(_ctx(self.conn, self.home, "task.create", args))
                self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for bad_reason in (["x"], 123):
            with self.assertRaises(MeshError) as cm_close:
                task_handlers.handle_task_close(
                    _ctx(self.conn, self.home, "task.close",
                         {"task_id": "tsk_f", "superseded_by": "tsk_r", "reason": bad_reason})
                )
            self.assertEqual((cm_close.exception.code, cm_close.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

            with self.assertRaises(MeshError) as cm_retry:
                task_handlers.handle_task_retry(
                    _ctx(self.conn, self.home, "task.retry", {"task_id": "tsk_f", "reason": bad_reason})
                )
            self.assertEqual((cm_retry.exception.code, cm_retry.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

            with self.assertRaises(MeshError) as cm_fail:
                task_handlers.handle_task_fail(
                    _ctx(self.conn, self.home, "task.fail",
                         {"task_id": "tsk_r", "run": "run_r", "fence": 1, "reason": bad_reason})
                )
            self.assertEqual((cm_fail.exception.code, cm_fail.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

            with self.assertRaises(MeshError) as cm_prog:
                task_handlers.handle_task_progress(
                    _ctx(self.conn, self.home, "task.progress",
                         {"task_id": "tsk_r", "run": "run_r", "fence": 1, "message": bad_reason})
                )
            self.assertEqual((cm_prog.exception.code, cm_prog.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for bad_session in (123, ["s"], True, "x" * 257, "bad\x00ses"):
            with self.assertRaises(MeshError) as cm_hb:
                task_handlers.handle_task_heartbeat(
                    _ctx(self.conn, self.home, "task.heartbeat",
                         {"task_id": "tsk_r", "run": "run_r", "fence": 1, "session": bad_session})
                )
            self.assertEqual((cm_hb.exception.code, cm_hb.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for bad_fence in (True, 1.5, 10**20, ["1"]):
            with self.assertRaises(MeshError) as cm_fence:
                task_handlers.handle_task_heartbeat(
                    _ctx(self.conn, self.home, "task.heartbeat",
                         {"task_id": "tsk_r", "run": "run_r", "fence": bad_fence})
                )
            self.assertEqual((cm_fence.exception.code, cm_fence.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

    def test_task_start_bad_session_leaves_no_orphan_worktree_or_branch(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(fixture.git_env_vars())

        repo = fixture.make_k0("R3H_START")
        integ = "mesh/prj_code/integration"
        fixture.git(repo, "branch", integ, "main")
        ts = "2026-01-01T00:00:00.000Z"
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
                "created_by, created_via, version, created_at, updated_at) "
                "VALUES ('prj_code', 'CodeP', 'Goal', 'active', ?, 'main', ?, 'usr_1', 'direct', 1, ?, ?);",
                (repo, integ, ts, ts),
            )
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, queued_at, created_by, created_at, updated_at) "
                "VALUES ('tsk_code1', 'prj_code', 'T', 'S', 'code', 'queued', 'agt_ibx', 'edit', 60, 2, ?, 'usr_1', ?, ?);",
                (ts, ts, ts),
            )

        for bad_session in (123, ["ses"], "s" * 257, "bad\x00s"):
            ctx = _ctx(
                self.conn,
                self.home,
                "task.start",
                {"task_id": "tsk_code1", "session": bad_session},
                principal={"id": "agt_ibx", "kind": "agent", "name": "ibx1"},
                actor_kind="agent",
                via="token",
            )
            asyncio.run(task_handlers.prepare_task_start_workspace(ctx))
            self.assertIsNone(getattr(ctx, "prepared_workspace", None))
            with self.assertRaises(MeshError) as cm:
                task_handlers.handle_task_start(ctx)
            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))
            ws_dir = os.path.join(self.home, "workspaces", "prj_code", "tasks", "tsk_code1")
            self.assertFalse(os.path.exists(ws_dir), f"orphan worktree left at {ws_dir}")
            self.assertNotIn("mesh/prj_code/tsk_code1", fixture.branches(repo))

    def test_inbox_config_settings_and_agent_handlers_reject_bad_args(self) -> None:
        class _FakeServer:
            active_waits = 0

        for bad_max in (10**20, 10001, 1.5, True, 0):
            with self.assertRaises(MeshError) as cm_m:
                asyncio.run(inbox_handlers.handle_inbox_wait(
                    _FakeServer(),
                    _ctx(self.conn, self.home, "inbox.wait", {"timeout": "1s", "max": bad_max}),
                ))
            self.assertEqual((cm_m.exception.code, cm_m.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for bad_key in (["x"], {"k": 1}, 123, ""):
            with self.assertRaises(MeshError) as cm_cg:
                config_handlers.handle_config_get(_ctx(self.conn, self.home, "config.get", {"key": bad_key}))
            self.assertEqual((cm_cg.exception.code, cm_cg.exception.exit_code), (c.INVALID_SETTING, c.EXIT_USAGE))
            with self.assertRaises(MeshError) as cm_vs:
                settings.validate_setting(bad_key, 1, self.home)
            self.assertEqual((cm_vs.exception.code, cm_vs.exception.exit_code), (c.INVALID_SETTING, c.EXIT_USAGE))

        for bad_name in ("   ", "a b", "a\nb", "a\x00b", ["x"], 123, ""):
            with self.assertRaises(MeshError) as cm_ac:
                agent_handlers.handle_agent_create(
                    _ctx(self.conn, self.home, "agent.create",
                         {"name": bad_name, "adapter": "none", "delivery": "inbox"})
                )
            self.assertEqual((cm_ac.exception.code, cm_ac.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        with self.assertRaises(MeshError) as cm_cl:
            agent_handlers.handle_agent_create(
                _ctx(self.conn, self.home, "agent.create",
                     {"name": "ok_cl", "adapter": "none", "delivery": "inbox", "can_lead": "false"})
            )
        self.assertEqual((cm_cl.exception.code, cm_cl.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        with self.assertRaises(MeshError) as cm_clu:
            agent_handlers.handle_agent_update(
                _ctx(self.conn, self.home, "agent.update", {"name": "ibx1", "can_lead": "false"})
            )
        self.assertEqual((cm_clu.exception.code, cm_clu.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for fn, cmd in (
            (agent_handlers.handle_agent_show, "agent.show"),
            (agent_handlers.handle_agent_disable, "agent.disable"),
            (agent_handlers.handle_agent_enable, "agent.enable"),
            (agent_handlers.handle_agent_token_issue, "agent.token.issue"),
            (agent_handlers.handle_agent_token_list, "agent.token.list"),
        ):
            for bad_id in (["x"], 123, "", "   "):
                with self.assertRaises(MeshError) as cm_fn:
                    fn(_ctx(self.conn, self.home, cmd, {"name": bad_id}))
                self.assertEqual((cm_fn.exception.code, cm_fn.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

        for bad_cred in (["x"], 123, "", "   "):
            with self.assertRaises(MeshError) as cm_rev:
                agent_handlers.handle_agent_token_revoke(
                    _ctx(self.conn, self.home, "agent.token.revoke", {"credential": bad_cred})
                )
            self.assertEqual((cm_rev.exception.code, cm_rev.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))


class TestP2_10SetupLeadNameRegex(unittest.TestCase):
    """P2-10: setup._LEAD_NAME_RE must anchor with \\A...\\Z to reject trailing newline."""

    def test_lead_name_with_trailing_newline_rejected(self) -> None:
        self.assertIsNone(setup_cli._LEAD_NAME_RE.match("lead\n"))
        with tempfile.TemporaryDirectory(prefix="mh-r3h-p210-") as tmp:
            ns = argparse.Namespace(
                home=os.path.join(tmp, "h"),
                dry_run=False,
                no_start=True,
                no_lead=False,
                lead_name="lead\n",
                token_dir=os.path.join(tmp, "tok"),
                approval_mode=None,
                passphrase_fd=None,
                passphrase_file=None,
            )
            with mock.patch.object(setup_cli, "_run", side_effect=AssertionError("must not run")):
                with self.assertRaises(MeshError) as cm:
                    setup_cli.handle_setup(ns)
            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))


class TestP2_11BoundaryAndClientHardening(unittest.TestCase):
    """P2-11: workspaces symlinks/orphan branch, gitops env, init file, passphrase limit, setup ancestor, client, dep, worker script."""

    def test_ensure_workspace_and_run_dir_reject_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-ws-") as tmp:
            tmp_real = os.path.realpath(tmp)
            victim = os.path.join(tmp_real, "victim")
            os.makedirs(victim, mode=0o755)
            os.chmod(victim, 0o755)
            sym_ws = os.path.join(tmp_real, "ws_sym")
            os.symlink(victim, sym_ws)

            with self.assertRaises(OSError):
                workspaces.ensure_workspace_dir(sym_ws)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o755)

            with self.assertRaises(OSError):
                workspaces.ensure_run_dir(sym_ws)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o755)

    def test_first_start_reattaches_orphaned_branch_when_db_branch_is_none(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(fixture.git_env_vars())

        repo = fixture.make_k0("R3H_ORPHAN_BR")
        integ = "mesh/prj_o/integration"
        fixture.git(repo, "branch", integ, "main")
        branch = workspaces.task_branch_name("prj_o", "tsk_o1")
        fixture.git(repo, "branch", branch, integ)
        ws_path = fixture.path("ws_orphan", "prj_o", "tasks", "tsk_o1")
        plan = {
            "task_id": "tsk_o1",
            "project_id": "prj_o",
            "repo": repo,
            "integration": integ,
            "workspace_path": ws_path,
            "branch": None,
            "base_commit": None,
            "fence_seq": 0,
        }
        res = workspaces.run_code_workspace_plan(plan)
        self.assertEqual(res["branch"], branch)
        self.assertEqual(res["base_commit"], fixture.head(repo, integ))
        self.assertTrue(os.path.exists(os.path.join(ws_path, ".git")))

    def test_gitops_drops_incomplete_git_config_without_value(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("R3H_GITCFG")
        env = dict(os.environ)
        for k in [k for k in env if k.startswith("GIT_")]:
            del env[k]
        env.update(fixture.git_env_vars())
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "user.name"
        env.pop("GIT_CONFIG_VALUE_0", None)
        with mock.patch.dict(os.environ, env, clear=True):
            cp = gitops.run_git(["rev-parse", "HEAD"], cwd=repo)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_init_mesh_home_regular_file_raises_insecure_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-init-") as tmp:
            fpath = os.path.join(tmp, "not_a_dir")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("hello")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                with self.assertRaises(MeshError) as cm:
                    mesh_init.init_mesh_home(fpath, PASS)
            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INSECURE_PERMISSIONS, c.EXIT_USAGE))

    def test_passphrase_read_file_and_fd_bound_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-pp-") as tmp:
            big = os.path.join(tmp, "big.pp")
            with open(big, "w", encoding="utf-8") as f:
                f.write("a" * (mesh_passphrase.MAX_PASSPHRASE_BYTES + 100))
            plat_perms.set_private_file_acl(big)
            with self.assertRaises(MeshError) as cm_f:
                mesh_passphrase.read_passphrase(SimpleNamespace(passphrase_fd=None, passphrase_file=big), env={})
            self.assertEqual((cm_f.exception.code, cm_f.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

            fd = os.open(big, os.O_RDONLY)
            try:
                with self.assertRaises(MeshError) as cm_fd:
                    mesh_passphrase.read_passphrase(SimpleNamespace(passphrase_fd=fd, passphrase_file=None), env={})
                self.assertEqual((cm_fd.exception.code, cm_fd.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))
            finally:
                os.close(fd)

    def test_setup_check_token_destination_walks_to_existing_ancestor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-tok-") as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home, mode=0o700)
            plat_perms.set_private_dir_acl(home)
            world_dir = os.path.join(tmp, "world")
            os.makedirs(world_dir, mode=0o777)
            if sys.platform == "win32":
                plat_perms.grant_everyone_write_for_test(world_dir)
                self.addCleanup(lambda: os.path.isdir(world_dir) and restore_accessible_for_test(world_dir))
            else:
                os.chmod(world_dir, 0o777)
            planned_nonexistent = os.path.join(world_dir, "sub", "nested", "lead.token")
            with self.assertRaises(MeshError) as cm:
                setup_cli._check_token_destination(home, planned_nonexistent)
            self.assertEqual((cm.exception.code, cm.exception.exit_code), (c.INVALID_ARGUMENT, c.EXIT_USAGE))

            priv_dir = os.path.join(tmp, "priv")
            os.makedirs(priv_dir, mode=0o700)
            plat_perms.set_private_dir_acl(priv_dir)
            setup_cli._check_token_destination(home, os.path.join(priv_dir, "sub", "lead.token"))

    @unittest.skipIf(sys.platform == "win32", "AF_UNIX socket length/socket.socket mocks are POSIX-only")
    def test_client_checks_socket_len_handles_oserror_and_closes_fds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-cli-") as tmp:
            long_home = os.path.join(tmp, "x" * 120)
            client_long = MeshClient(long_home)
            with self.assertRaises(MeshError) as cm_len:
                client_long.send_request({"v": 1, "cmd": "status"})
            self.assertEqual((cm_len.exception.code, cm_len.exception.exit_code), (c.SOCKET_PATH_TOO_LONG, c.EXIT_USAGE))

            args = argparse.Namespace(user=False, token_file=None, timeout=None, request_id="req_1", dry_run=False)
            with mock.patch.object(client_long, "resolve_auth", return_value={"type": "none"}):
                with self.assertRaises(MeshError) as cm_slen:
                    client_long.stream_cmd(args, "event.tail", {})
            self.assertEqual((cm_slen.exception.code, cm_slen.exception.exit_code), (c.SOCKET_PATH_TOO_LONG, c.EXIT_USAGE))

            # stream_cmd connect OSError must close socket and raise DAEMON_UNAVAILABLE
            client_ok = MeshClient(tmp)
            fake_sock = mock.MagicMock()
            fake_sock.connect.side_effect = PermissionError("denied")
            with mock.patch("socket.socket", return_value=fake_sock), \
                    mock.patch.object(client_ok, "resolve_auth", return_value={"type": "none"}):
                with self.assertRaises(MeshError) as cm_sc:
                    client_ok.stream_cmd(args, "event.tail", {})
            self.assertEqual((cm_sc.exception.code, cm_sc.exception.exit_code), (c.DAEMON_UNAVAILABLE, c.EXIT_DAEMON_UNAVAILABLE))
            fake_sock.close.assert_called_once()

            # send_request OSError during sendall/readline must raise DAEMON_UNAVAILABLE and close file/socket
            sock_dir = os.path.join(tmp, "run")
            os.makedirs(sock_dir, mode=0o700)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(os.path.join(sock_dir, "mesh.sock"))
            srv.listen(1)

            def _drop_conn() -> None:
                c_sock, _ = srv.accept()
                c_sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                c_sock.close()

            t = threading.Thread(target=_drop_conn, daemon=True)
            t.start()
            try:
                with self.assertRaises(MeshError) as cm_sr:
                    client_ok.send_request({"v": 1, "cmd": "status"})
                self.assertEqual((cm_sr.exception.code, cm_sr.exception.exit_code),
                                 (c.DAEMON_UNAVAILABLE, c.EXIT_DAEMON_UNAVAILABLE))
            finally:
                t.join(timeout=2)
                srv.close()

    def test_dep_add_blocks_post_queue_states_and_checks_authz_first(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3h-dep-") as tmp:
            home, conn = _make_db(tmp)
            try:
                ts = "2026-01-01T00:00:00.000Z"
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                        "max_attempts, created_by, created_at, updated_at) "
                        "VALUES ('tsk_unfin', 'prj_1', 'U', 'S', 'general', 'created', 'edit', 60, 1, 'usr_1', ?, ?);",
                        (ts, ts),
                    )
                    for st in ("running", "submitted", "accepted", "merging", "merged", "cancelling", "cancelled"):
                        conn.execute(
                            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                            "max_attempts, created_by, created_at, updated_at) "
                            "VALUES (?, 'prj_1', 'T', 'S', 'code', ?, 'edit', 60, 1, 'usr_1', ?, ?);",
                            (f"tsk_{st}", st, ts, ts),
                        )

                for st in ("running", "submitted", "accepted", "merging", "merged", "cancelling", "cancelled"):
                    with self.assertRaises(MeshError) as cm:
                        dep_handlers.handle_dep_add(
                            _ctx(conn, home, "dep.add", {"task_id": f"tsk_{st}", "on": "tsk_unfin"})
                        )
                    self.assertEqual((cm.exception.code, cm.exception.exit_code),
                                     (c.INVALID_TRANSITION, c.EXIT_CONFLICT), st)

                # Non-lead agent probing dep.add / dep.remove with nonexistent --on must get FORBIDDEN, not NOT_FOUND
                worker_principal = {"id": "agt_ibx", "kind": "agent", "name": "ibx1", "capabilities": ["intake"]}
                for fn, cmd in ((dep_handlers.handle_dep_add, "dep.add"), (dep_handlers.handle_dep_remove, "dep.remove")):
                    with self.assertRaises(MeshError) as cm_a:
                        fn(_ctx(conn, home, cmd, {"task_id": "tsk_unfin", "on": "tsk_does_not_exist"},
                                principal=worker_principal, actor_kind="agent", via="token"))
                    self.assertEqual(cm_a.exception.exit_code, c.EXIT_FORBIDDEN, (cmd, cm_a.exception.code))
            finally:
                conn.close()

    @unittest.skipIf(sys.platform == "win32", "scripts/inbox-worker-example.sh requires POSIX bash")
    def test_inbox_worker_script_dash_summary_and_non_offer_wait_rounds(self) -> None:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                              "scripts", "inbox-worker-example.sh")
        with tempfile.TemporaryDirectory(prefix="mh-r3h-sh-") as d:
            os.makedirs(os.path.join(d, "ws"))
            tok = os.path.join(d, "w.token")
            with open(tok, "w", encoding="utf-8") as f:
                f.write("dummy-tok\n")
            os.chmod(tok, 0o600)
            stub = os.path.join(d, "mesh")
            stub_src = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
# Reject leading-dash --summary if passed as two tokens (`--summary`, `- leading`)
for i, a in enumerate(argv):
    if a == "--summary" and i + 1 < len(argv) and argv[i + 1].startswith("-"):
        sys.stderr.write("error: argument --summary: expected one argument\n")
        sys.exit(2)
d = os.environ["STUB_DIR"]
with open(os.path.join(d, "calls.log"), "a", encoding="utf-8") as lf:
    lf.write(" ".join(argv) + "\n")
cmd = " ".join(argv[:2])
if cmd == "inbox wait":
    cnt_p = os.path.join(d, "wait_count")
    n = int(open(cnt_p).read()) if os.path.exists(cnt_p) else 0
    open(cnt_p, "w").write(str(n + 1))
    if n == 0:
        print(json.dumps({"ok": True, "data": {"items": [
            {"id": "msg_notice", "type": "task.rejected", "payload": {"task_id": "tsk_0"}}
        ]}}))
    else:
        print(json.dumps({"ok": True, "data": {"items": [
            {"id": "msg_offer", "type": "task.offered", "payload": {"task_id": "tsk_1"}}
        ]}}))
elif cmd == "task start":
    print(json.dumps({"ok": True, "data": {
        "run_id": "run_1", "fence": 1, "workspace": os.path.join(d, "ws"),
        "branch": "mesh/p/t", "session": "ses_1", "heartbeat_every_s": 60, "lease_ttl_s": 120,
    }}))
else:
    print(json.dumps({"ok": True, "data": {}}))
"""
            with open(stub, "w", encoding="utf-8") as f:
                f.write(stub_src)
            os.chmod(stub, 0o755)
            env = dict(
                os.environ,
                MESH_BIN=stub,
                MESH_TOKEN_FILE=tok,
                STUB_DIR=d,
                WAIT_TIMEOUT="1s",
                WAIT_ROUNDS="1",
                SUMMARY="- leading dash summary",
            )
            proc = subprocess.run(
                ["bash", script, "--", "true"],
                capture_output=True, text=True, env=env, timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(d, "calls.log"), encoding="utf-8") as cf:
                calls = cf.read()
            self.assertIn("--summary=- leading dash summary", calls)


if __name__ == "__main__":
    unittest.main()
