"""Regression tests for batch7 PR44 round 1 findings."""

import asyncio
import argparse
import contextlib
import io
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import venv
from typing import Any, Dict, List
from unittest import mock

import mesh.constants as c
from mesh.cli import entry
from mesh.cli.commands import setup as setup_cmd
from mesh.cli.commands import task as task_cmd
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db
from mesh.core.events import record_event
from mesh.core.init import init_mesh_home
from mesh.core.lead_prompt import render_lead_prompt
from mesh.core.paths import get_install_root, get_mesh_bin_dir, is_site_packages_dir
from mesh.core.redact import (
    StreamRedactor,
    clear_registered_secrets,
    redact,
    register_secret,
)
from mesh.daemon import lead_turns, merge, supervisor
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.inbox import handle_inbox_wait
from mesh.daemon.handlers.project import (
    handle_project_leads,
    handle_project_set_lead,
    handle_project_show,
    handle_project_wait,
)
from mesh.daemon.handlers.task import handle_task_wait
from mesh.errors import MeshError


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _subproc_env(extra: Dict[str, str]) -> Dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for k in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(extra)
    return env


class _FakeReader:
    def at_eof(self) -> bool:
        return False


class _FakeServer:
    def __init__(self) -> None:
        self.notify_cond = asyncio.Condition()
        self.active_waits = 0
        self.shutting_down = False


class TestPr44R1P1LineRedactorPrefix(unittest.TestCase):
    """P1-1: multi-line redaction must match underscore/hyphen-prefixed keys."""

    def test_multiline_prefixed_keys_redacted_and_true_words_preserved(self) -> None:
        cases = [
            ("db_password:\nhunter2\n", "db_password:\n[REDACTED]\n"),
            ("OPENAI_API_KEY=\nmy_secret_val\n", "OPENAI_API_KEY=\n[REDACTED]\n"),
            ("client_secret\n= top_secret\n", "client_secret\n= [REDACTED]\n"),
            ("svc_passwd:\nhunter2\n", "svc_passwd:\n[REDACTED]\n"),
            ("x-api-key:\nhunter2\n", "x-api-key:\n[REDACTED]\n"),
            ("v2_secret\n: s3cr3t\n", "v2_secret\n: [REDACTED]\n"),
            # Word-prefix non-secret identifiers remain unredacted
            ("unpassword:\nhello\n", "unpassword:\nhello\n"),
            ("word2secret:\nhello\n", "word2secret:\nhello\n"),
            ("nonsecret\n= value\n", "nonsecret\n= value\n"),
            ("ringbearer\nwalked away\n", "ringbearer\nwalked away\n"),
            ("the standard-bearer\nwalked away\n", "the standard-bearer\nwalked away\n"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw, exact_values=[]), expected)
                sr = StreamRedactor(exact_values=[])
                self.assertEqual(sr.feed(raw) + sr.flush(), expected)
                # Also verify byte-by-byte streaming equivalence
                sr_char = StreamRedactor(exact_values=[])
                chunked = "".join(sr_char.feed(ch) for ch in raw) + sr_char.flush()
                self.assertEqual(chunked, expected)


class TestPr44R1SitePackagesIsolation(unittest.TestCase):
    """P1-2: only global site.getsitepackages() may be stripped from PYTHONPATH."""

    def test_is_site_packages_dir_rejects_custom_and_user_site(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-sp-") as d:
            custom_sp = os.path.join(d, "custom", "site-packages")
            custom_dp = os.path.join(d, "custom", "dist-packages")
            user_sp = os.path.join(d, "userbase", "lib", "python3.11", "site-packages")
            global_sp = os.path.join(d, "venv", "lib", "python3.11", "site-packages")
            os.makedirs(custom_sp)
            os.makedirs(custom_dp)
            os.makedirs(user_sp)
            os.makedirs(global_sp)

            with mock.patch("site.getsitepackages", return_value=[global_sp]), \
                 mock.patch("site.getusersitepackages", return_value=user_sp):
                self.assertTrue(is_site_packages_dir(global_sp))
                self.assertFalse(is_site_packages_dir(user_sp))
                self.assertFalse(is_site_packages_dir(custom_sp))
                self.assertFalse(is_site_packages_dir(custom_dp))

    def test_helpers_keep_pythonpath_for_non_global_site_packages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-helpers-") as d:
            d = os.path.realpath(d)
            custom_sp = os.path.join(d, "custom", "site-packages")
            os.makedirs(custom_sp)
            cache_dir = os.path.join(d, "cache")
            os.makedirs(cache_dir)

            with mock.patch("site.getsitepackages", return_value=["/usr/lib/python3/dist-packages"]):
                with mock.patch("mesh.core.paths.get_install_root", return_value=custom_sp), \
                     mock.patch.dict(os.environ, {"LOCALAPPDATA": cache_dir, "XDG_CACHE_HOME": cache_dir}):
                    bdir = get_mesh_bin_dir()
                    if sys.platform == "win32":
                        with open(os.path.join(bdir, "mesh.cmd"), "r", encoding="utf-8") as fh:
                            wrapper_cmd = fh.read()
                        self.assertIn(f'set "PYTHONPATH={os.path.realpath(custom_sp)}"', wrapper_cmd)
                    with open(os.path.join(bdir, "mesh"), "r", encoding="utf-8") as fh:
                        wrapper = fh.read()
                self.assertIn(f"export PYTHONPATH={shlex.quote(os.path.realpath(custom_sp))}", wrapper)

                with mock.patch("mesh.cli.commands.setup.get_install_root", return_value=custom_sp), \
                     mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
                    senv = setup_cmd._mesh_env()
                self.assertEqual(senv.get("PYTHONPATH"), os.path.realpath(custom_sp))

                with mock.patch("mesh.daemon.supervisor.get_install_root", return_value=custom_sp):
                    child_env = supervisor.build_child_env(
                        {"PATH": "/usr/bin"}, "tok", "run_1", 1, "tsk_1", "prj_1", os.path.join(d, "s.sock")
                    )
                    lead_env = supervisor.build_lead_child_env(
                        {"PATH": "/usr/bin"}, "tok", "run_2", "prj_1", os.path.join(d, "s.sock")
                    )
                self.assertEqual(child_env.get("PYTHONPATH"), os.path.realpath(custom_sp))
                self.assertEqual(lead_env.get("PYTHONPATH"), os.path.realpath(custom_sp))

                with mock.patch("mesh.daemon.merge.get_install_root", return_value=custom_sp), \
                     mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
                    menv = merge._test_env("/usr/bin")
                self.assertEqual(menv.get("PYTHONPATH"), os.path.realpath(custom_sp))

    def _copy_mesh_pkg(self, target_dir: str) -> None:
        shutil.copytree(
            os.path.join(REPO_ROOT, "mesh"),
            os.path.join(target_dir, "mesh"),
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    def test_custom_target_site_packages_runs_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-target-") as d:
            target_sp = os.path.join(d, "custom", "site-packages")
            self._copy_mesh_pkg(target_sp)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": target_sp,
            }
            proc = subprocess.run(
                [sys.executable, "-m", "mesh", "--version"],
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"expected exit 0, got {proc.returncode}; stdout={proc.stdout!r} stderr={proc.stderr!r}",
            )

    def test_user_site_packages_runs_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-userbase-") as d:
            user_site_proc = subprocess.run(
                [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
                env=_subproc_env({"PYTHONUSERBASE": d}),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            user_sp = user_site_proc.stdout.strip()
            self._copy_mesh_pkg(user_sp)

            # Simulate a `pip install --user` console script invoked with python3 (without -s/-P)
            console_script = os.path.join(d, "mesh-user")
            with open(console_script, "w", encoding="utf-8") as fh:
                fh.write(
                    "import sys\n"
                    "from mesh.cli.entry import main\n"
                    "sys.exit(main())\n"
                )
            env = _subproc_env({"PYTHONUSERBASE": d})
            proc = subprocess.run(
                [sys.executable, console_script, "--version"],
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"expected exit 0, got {proc.returncode}; stdout={proc.stdout!r} stderr={proc.stderr!r}",
            )

    def test_real_venv_site_packages_strips_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-venv-") as d:
            vdir = os.path.join(d, "venv")
            venv.create(vdir, with_pip=False, symlinks=(sys.platform != "win32"))
            vpy = os.path.join(vdir, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(vdir, "bin", "python")
            vsp = subprocess.run(
                [vpy, "-c", "import site; print(site.getsitepackages()[0])"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            self._copy_mesh_pkg(vsp)

            probe_env = _subproc_env({})
            res = subprocess.run(
                [
                    vpy,
                    "-c",
                    "import os, sys, mesh.cli.entry as e; "
                    "from mesh.core.paths import is_site_packages_dir; "
                    "print(is_site_packages_dir(e._mesh_install_root()), e._is_site_packages_dir(e._mesh_install_root()))",
                ],
                cwd=d,
                env=probe_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), "True True")

            stale_pp = os.path.join(d, "stale")
            run_res = subprocess.run(
                [vpy, "-m", "mesh", "--version"],
                cwd=d,
                env=_subproc_env({"PYTHONPATH": stale_pp}),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                run_res.returncode, 0,
                f"venv run failed: stdout={run_res.stdout!r} stderr={run_res.stderr!r}",
            )


class TestPr44R1PreIsolatedPythonPath(unittest.TestCase):
    """P2-1: preset PYTHONSAFEPATH=1 + PYTHONNOUSERSITE=1 or -P -s must still scrub PYTHONPATH."""

    def _make_evil_dir(self, base: str) -> str:
        evil = os.path.join(base, "evil")
        os.makedirs(evil, exist_ok=True)
        with open(os.path.join(evil, "json.py"), "w", encoding="utf-8") as fh:
            fh.write("raise SystemExit(99)\n")
        return evil

    def test_preset_env_flags_still_sanitize_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-p21-") as d:
            evil = self._make_evil_dir(d)
            pp = f"{evil}{os.pathsep}{REPO_ROOT}"
            env = _subproc_env({
                "PYTHONSAFEPATH": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": pp,
            })
            proc = subprocess.run(
                [sys.executable, "-m", "mesh", "--version"],
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"poisoned PYTHONPATH bypassed isolation (rc={proc.returncode}): stderr={proc.stderr!r}",
            )

    def test_direct_dash_p_s_still_sanitizes_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-p21ps-") as d:
            evil = self._make_evil_dir(d)
            pp = f"{evil}{os.pathsep}{REPO_ROOT}"
            env = _subproc_env({
                "PYTHONPATH": pp,
            })
            proc = subprocess.run(
                [sys.executable, "-P", "-s", "-m", "mesh", "--version"],
                cwd=d,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"poisoned PYTHONPATH bypassed -P -s isolation (rc={proc.returncode}): stderr={proc.stderr!r}",
            )

    def test_already_sanitized_returns_without_reexec(self) -> None:
        fake_flags = argparse.Namespace(safe_path=True, no_user_site=True)
        if sys.platform == "win32":
            fake_env = {
                "PYTHONPATH": REPO_ROOT,
                "_MESH_ORIG_PYTHONPATH": "__UNSET__",
            }
            with mock.patch.object(entry._winapi, "CreateProcess", side_effect=AssertionError("must not re-exec")), \
                 mock.patch.object(entry.sys, "flags", fake_flags), \
                 mock.patch.object(entry.nt, "environ", fake_env):
                entry.ensure_isolated_process()
            return
        with mock.patch.object(entry.posix, "execve", side_effect=AssertionError("must not re-exec")), \
             mock.patch.object(entry.posix, "umask") as umask_mock:
            expected = REPO_ROOT.encode(sys.getfilesystemencoding() or "utf-8", "surrogateescape")
            fake_env = {
                b"PYTHONPATH": expected,
                b"_MESH_ORIG_PYTHONPATH": b"__UNSET__",
            }
            with mock.patch.object(entry.sys, "flags", fake_flags), \
                 mock.patch.object(entry.posix, "environ", fake_env):
                entry.ensure_isolated_process()
            umask_mock.assert_called_once_with(0o077)


class TestPr44R1WaitRevokedCapability(unittest.TestCase):
    """P2-2: wait wakeups must refresh agent capabilities from DB, not use stale principal cache."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-pr44-p22-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        now = now_iso_utc()
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'relayer', ?);",
            (now,),
        )
        self.conn.execute(
            """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json,
                                   max_concurrency, created_at, updated_at)
               VALUES ('agt_1', 'fake', 'inbox', 1, '["intake","relay"]', 1, ?, ?);""",
            (now, now),
        )
        self.conn.execute(
            """INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, created_at)
               VALUES ('cred_1', 'agt_1', 'agent_token', 'tok_1', X'00', ?);""",
            (now,),
        )
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, origin_principal_id,
                                     created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_1', 'P', 'G', 'active', 'agt_1', 'usr_1', 'direct', 1, ?, ?);""",
            (now, now),
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, profile,
                                  timeout_s, max_attempts, created_by, version, created_at, updated_at)
               VALUES ('tsk_1', 'prj_1', 'T', 'Spec', 'general', 'queued', 'edit',
                       60, 1, 'usr_1', 1, ?, ?);""",
            (now, now),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def _stale_principal(self) -> Dict[str, Any]:
        return {
            "id": "agt_1",
            "kind": "agent",
            "name": "relayer",
            "capabilities": ["intake", "relay"],
        }

    def _ctx(self, cmd: str, args: Dict[str, Any]) -> HandlerContext:
        return HandlerContext(
            conn=self.conn,
            principal=self._stale_principal(),
            actor_kind="agent",
            via="relayed",
            request_id="req_wait",
            home=self.temp_dir.name,
            cmd=cmd,
            args=args,
            relayed_by="usr_1",
            credential_id="cred_1",
        )

    def _run_wait_and_revoke(self, coro) -> None:
        srv = _FakeServer()

        async def _drive() -> None:
            task = asyncio.create_task(coro(srv))
            await asyncio.sleep(0.05)
            # Revoke the relay capability in DB (credential remains active)
            self.conn.execute(
                "UPDATE agents SET capabilities_json = '[\"intake\"]' WHERE id = 'agt_1';"
            )
            self.conn.commit()
            async with srv.notify_cond:
                srv.notify_cond.notify_all()
            await task

        asyncio.run(_drive())

    def test_inbox_wait_catches_revoked_relay_capability_on_wakeup(self) -> None:
        ctx = self._ctx("inbox.wait", {"timeout": "2s"})
        with self.assertRaises(MeshError) as cm:
            self._run_wait_and_revoke(lambda srv: handle_inbox_wait(srv, ctx, _FakeReader()))
        self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)

    def test_task_wait_catches_revoked_relay_capability_on_wakeup(self) -> None:
        ctx = self._ctx("task.wait", {"task_id": "tsk_1", "until": "submitted", "timeout": "2s"})
        with self.assertRaises(MeshError) as cm:
            self._run_wait_and_revoke(lambda srv: handle_task_wait(srv, ctx, _FakeReader()))
        self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)

    def test_project_wait_catches_revoked_relay_capability_on_wakeup(self) -> None:
        ctx = self._ctx("project.wait", {"project_id": "prj_1", "until": "completed", "timeout": "2s"})
        with self.assertRaises(MeshError) as cm:
            self._run_wait_and_revoke(lambda srv: handle_project_wait(srv, ctx, _FakeReader()))
        self.assertEqual(cm.exception.code, c.RELAY_NOT_ALLOWED)
        self.assertEqual(cm.exception.exit_code, c.EXIT_FORBIDDEN)


class TestPr44R1SetLeadAndLeadTurnsRedaction(unittest.TestCase):
    """P2-3: project.set_lead writes & reads must redact reason; lead_turns must redact on read."""

    def setUp(self) -> None:
        clear_registered_secrets()
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-pr44-p23-")
        self.db_path = os.path.join(self.temp_dir.name, "mesh.db")
        self.conn = init_db(self.db_path)
        now = now_iso_utc()
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        for aid in ("lead_a", "lead_b"):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);",
                (aid, aid, now),
            )
            self.conn.execute(
                """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json,
                                       allowed_models_json, max_concurrency, created_at, updated_at)
                   VALUES (?, 'fake', 'launch', 1, '["lead","coordinate"]', '["fake-model"]', 1, ?, ?);""",
                (aid, now, now),
            )
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_1', 'Proj', 'Goal', 'active', 'usr_1', 'direct', 1, ?, ?);""",
            (now, now),
        )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, reason, started_at)
               VALUES ('asg_0', 'prj_1', 'lead_a', 'usr_1', 'direct', 'init', ?);""",
            (now,),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        clear_registered_secrets()
        self.conn.close()
        self.temp_dir.cleanup()

    def test_set_lead_redacts_reason_and_ended_reason_in_db_and_reads(self) -> None:
        secret_tok = "Bearer pr44_set_lead_secret_tok_987654"
        ctx = HandlerContext(
            conn=self.conn,
            principal={"id": "usr_1", "kind": "user", "name": "user"},
            actor_kind="user",
            via="direct",
            request_id="req_lead",
            home=self.temp_dir.name,
            cmd="project.set_lead",
            args={"project_id": "prj_1", "agent": "lead_b", "reason": f"handoff {secret_tok}"},
            testing_mode=True,
        )
        handle_project_set_lead(ctx)

        rows = self.conn.execute(
            "SELECT id, reason, ended_reason FROM lead_assignments WHERE project_id = 'prj_1' ORDER BY started_at ASC;"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("pr44_set_lead_secret_tok_987654", rows[0]["ended_reason"] or "")
        self.assertIn("[REDACTED]", rows[0]["ended_reason"] or "")
        self.assertNotIn("pr44_set_lead_secret_tok_987654", rows[1]["reason"] or "")
        self.assertIn("[REDACTED]", rows[1]["reason"] or "")

        # Legacy unredacted / dynamically registered secret also redacted on read
        dyn_secret = "dyn-lead-hist-" + "sec" + "ret-xyz987654"  # split: synthetic fixture
        self.conn.execute(
            "UPDATE lead_assignments SET reason = ? WHERE id = 'asg_0';",
            (f"legacy {dyn_secret}",),
        )
        register_secret("dyn_hist", dyn_secret)

        leads_res = handle_project_leads(
            HandlerContext(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id="req_leads",
                home=self.temp_dir.name,
                cmd="project.leads",
                args={"project_id": "prj_1"},
            )
        )
        for item in leads_res.data["items"]:
            self.assertNotIn(dyn_secret, item.get("reason") or "")
            self.assertNotIn("pr44_set_lead_secret_tok_987654", item.get("reason") or "")
            self.assertNotIn("pr44_set_lead_secret_tok_987654", item.get("ended_reason") or "")

        show_res = handle_project_show(
            HandlerContext(
                conn=self.conn,
                principal={"id": "usr_1", "kind": "user", "name": "user"},
                actor_kind="user",
                via="direct",
                request_id="req_show",
                home=self.temp_dir.name,
                cmd="project.show",
                args={"project_id": "prj_1", "history": True},
            )
        )
        for item in show_res.data.get("lead_history", []):
            self.assertNotIn(dyn_secret, item.get("reason") or "")
            self.assertNotIn("pr44_set_lead_secret_tok_987654", item.get("reason") or "")
            self.assertNotIn("pr44_set_lead_secret_tok_987654", item.get("ended_reason") or "")

    def test_lead_turns_build_prompt_input_redacts_on_read(self) -> None:
        now = now_iso_utc()
        # Simulate pre-existing / unredacted rows or a secret registered AFTER storage
        dyn = "run_tok_post_registered_secret_123456"
        self.conn.execute(
            "UPDATE projects SET name = ?, goal = ? WHERE id = 'prj_1';",
            (f"Proj {dyn}", f"Goal password=hunter_proj {dyn}"),
        )
        self.conn.execute(
            """INSERT INTO acceptance_criteria (id, project_id, ordinal, text, created_at)
               VALUES ('ac_1', 'prj_1', 1, ?, ?);""",
            (f"criterion {dyn}", now),
        )
        self.conn.execute(
            """INSERT INTO tasks (id, project_id, title, spec, kind, status, profile,
                                  timeout_s, max_attempts, created_by, version, created_at, updated_at)
               VALUES ('tsk_1', 'prj_1', ?, 'Spec', 'general', 'queued', 'edit',
                       60, 1, 'usr_1', 1, ?, ?);""",
            (f"task {dyn}", now, now),
        )
        self.conn.execute(
            """INSERT INTO approvals (id, project_id, category, op_type, op_json, op_hash, summary,
                                      requested_by, requested_at, expires_at, status)
               VALUES ('apr_1', 'prj_1', 'deploy', 'op', '{}', 'h1', ?, 'usr_1', ?, ?, 'approved');""",
            (f"summary {dyn}", now, now),
        )
        ev1 = record_event(
            self.conn,
            "project.note",
            actor_id="usr_1",
            actor_kind="user",
            via="direct",
            project_id="prj_1",
            reason=f"note reason {dyn}",
            payload={"note": f"note body {dyn} Bearer pr44_msg_bearer_secret_99"},
        )
        self.conn.execute(
            """INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id,
                                   type, payload_json, status, next_attempt_at, created_at)
               VALUES ('obx_1', ?, 'lead_b', 'lead_wake', 'dk_1', 'prj_1',
                       'project.lead_changed', ?, 'pending', ?, ?);""",
            (ev1["seq"], f'{{"reason": "wake {dyn} api_key=pr44_inline_key"}}', now, now),
        )
        self.conn.commit()

        # Register dynamic secret after DB insert
        register_secret("post_secret", dyn)

        msgs = self.conn.execute("SELECT * FROM outbox WHERE id = 'obx_1';").fetchall()
        inp = lead_turns.build_prompt_input(self.conn, "prj_1", msgs, 100_000)
        rendered = render_lead_prompt(inp)

        for secret_val in (dyn, "hunter_proj", "pr44_msg_bearer_secret_99", "pr44_inline_key"):
            self.assertNotIn(secret_val, rendered.text)


class TestPr44R1InputNullDeliverable(unittest.TestCase):
    """P3-1: --input with {\"deliverable\": null} must raise INVALID_ARGUMENT, not become {}."""

    def test_handle_task_submit_rejects_null_deliverable_from_input_json(self) -> None:
        args = argparse.Namespace(
            task_id="tsk_1",
            run="run_1",
            fence=1,
            deliverable=None,
            summary=None,
            home=os.path.join(temp_root() if "temp_root" in globals() else tempfile.gettempdir(), "x"),
            _from_json={"deliverable", "run", "fence"},
        )
        called: List[str] = []

        class _NeverClient:
            def __init__(self, *a, **k):
                pass

            def execute_cmd(self, *a, **k):
                called.append("called")
                return {"data": {}}

        with mock.patch.object(task_cmd, "MeshClient", _NeverClient):
            with self.assertRaises(MeshError) as cm:
                task_cmd.handle_task_submit(args)
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertEqual(called, [])

    def test_cli_input_json_null_deliverable_exits_usage(self) -> None:
        from mesh.cli.main import main as cli_main

        with tempfile.TemporaryDirectory(prefix="mh-pr44-p31-") as d:
            inp = os.path.join(d, "submit.json")
            with open(inp, "w", encoding="utf-8") as fh:
                fh.write('{"deliverable": null, "run": "run_1", "fence": 1}')

            with mock.patch.object(task_cmd, "MeshClient", side_effect=AssertionError("must not hit daemon")), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = cli_main(["task", "submit", "tsk_1", "--home", d, "--input", inp])
        self.assertEqual(rc, c.EXIT_USAGE)


@unittest.skipUnless(sys.platform != "win32", "POSIX st_uid/os.chmod ownership check; Win32 SID check in test_platform_locks_perms.py")
class TestPr44R1InitForeignOwner(unittest.TestCase):
    """P3-2: init_mesh_home foreign-owner check must inspect st_uid via os.stat on 0o700 dir."""

    def test_foreign_owner_with_0700_mode_raises_with_owner_uid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-p32-") as d:
            foreign_dir = os.path.join(d, "foreign_home")
            os.makedirs(foreign_dir, mode=0o700)
            real_stat = os.stat
            st = real_stat(foreign_dir)
            foreign_uid = os.getuid() + 1000
            foreign_st = os.stat_result(
                (stat.S_IFDIR | 0o700, st.st_ino, st.st_dev, st.st_nlink,
                 foreign_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
            )
            target_real = os.path.realpath(foreign_dir)

            def _selective_stat(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)) and os.path.realpath(os.fspath(path)) == target_real:
                    return foreign_st
                return real_stat(path, *args, **kwargs)

            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}), \
                 mock.patch("mesh.core.init.os.chmod", side_effect=PermissionError("not owner")), \
                 mock.patch("mesh.core.paths.os.stat", side_effect=_selective_stat):
                with self.assertRaises(MeshError) as cm:
                    init_mesh_home(foreign_dir, "passphrase-for-test-123456")
            self.assertEqual(cm.exception.code, c.INSECURE_PERMISSIONS)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual(cm.exception.details.get("owner_uid"), foreign_uid)

    def test_subdir_chmod_permission_error_raises_insecure_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-pr44-p32sub-") as d:
            home_dir = os.path.join(d, "owned_home")
            os.makedirs(home_dir, mode=0o700)
            orig_chmod = os.chmod

            def _fail_subdir_chmod(path, mode, *args, **kwargs):
                if os.path.realpath(os.fspath(path)) != os.path.realpath(home_dir):
                    raise PermissionError("subdir not writable")
                return orig_chmod(path, mode, *args, **kwargs)

            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}), \
                 mock.patch("mesh.core.init.os.chmod", side_effect=_fail_subdir_chmod):
                with self.assertRaises(MeshError) as cm:
                    init_mesh_home(home_dir, "passphrase-for-test-123456")
            self.assertEqual(cm.exception.code, c.INSECURE_PERMISSIONS)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
