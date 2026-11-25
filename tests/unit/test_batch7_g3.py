"""Unit regression tests for batch7 g3 findings (secrets, redaction, token files, isolation)."""

import ast
import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

if sys.platform != "win32":
    import posix
else:
    posix = None  # type: ignore[assignment]

import mesh.constants as c
from mesh.client import MeshClient
from mesh.core import db, logfile
from mesh.core.auth import generate_run_token
from mesh.core.clock import now_iso_utc
from mesh.core.events import fan_out_event, record_event
from mesh.core.init import init_mesh_home
from mesh.core.passphrase import ENV_PASSPHRASE, ENV_PASSPHRASE_FILE, read_passphrase
from mesh.core.paths import (
    check_token_file_secure,
    get_install_root,
    get_mesh_bin_dir,
    is_site_packages_dir,
    read_token_file_secure,
)
from mesh.core.redact import (
    StreamRedactor,
    redact,
    register_secret,
    unregister_secret,
)
from mesh.core.token_roots import configured_roots
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.event import _list_events, handle_event_list, handle_event_tail_live
from mesh.daemon.handlers.inbox import handle_inbox_list, handle_inbox_wait
from mesh.daemon.handlers.project import handle_project_complete, handle_project_set_lead
from mesh.errors import DbIntegrityError, MeshError

PASS = "correcthorsebatterystaple"
USER = {"id": "usr_1", "name": "user"}


def _tok(prefix: str = "sk-", char: str = "a", length: int = 24) -> str:
    return prefix + (char * length)


class _FakeReader:
    def __init__(self, eof: bool = False) -> None:
        self._eof = eof

    def at_eof(self) -> bool:
        return self._eof


class _FakeWriter:
    def __init__(self, stop_after_write: bool = False) -> None:
        self.chunks: list[bytes] = []
        self._closing = False
        self._stop_after_write = stop_after_write

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing or (self._stop_after_write and bool(self.chunks))

    def lines(self) -> list[dict]:
        raw = b"".join(self.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


class _FakeServer:
    def __init__(self) -> None:
        self.notify_cond = asyncio.Condition()
        self.active_waits = 0
        self.active_streams = 0
        self.stopping = False


def _seed_project_with_leads(conn: sqlite3.Connection, delivery: str = "inbox") -> None:
    row = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
    if row is None:
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','user','now');")
        user_id = "usr_1"
    else:
        user_id = row[0]
    for aid, name, caps in (
        ("ag_orig", "origin", '["intake"]'),
        ("ag_1", "lead1", '["intake"]'),
        ("ag_2", "lead2", '["intake"]'),
    ):
        conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, 'now');", (aid, name))
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at) "
            "VALUES (?, 'none', ?, 1, ?, 1, 'now', 'now');",
            (aid, delivery, caps),
        )
    conn.execute(
        "INSERT INTO projects (id, name, goal, status, origin_principal_id, created_by, created_via, version, created_at, updated_at) "
        "VALUES ('prj_1','Proj','Goal','active','ag_orig',?,'direct',1,'now','now');",
        (user_id,),
    )
    conn.execute(
        "INSERT INTO acceptance_criteria (id, project_id, ordinal, text, created_at) "
        "VALUES ('crt_1','prj_1',1,'Criterion 1','now');"
    )
    conn.execute(
        "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
        "VALUES ('asg_1','prj_1','ag_1',?,'direct','now');",
        (user_id,),
    )
    conn.commit()


class TestBatch7G3Redaction(unittest.TestCase):
    """Finding 1: persistence redaction in complete/set-lead and read-time redaction in inbox/event."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-g3-f1-")
        self.conn = db.init_db(os.path.join(self.tmp.name, "mesh.db"))
        _seed_project_with_leads(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _ctx(self, cmd: str, args: dict) -> HandlerContext:
        return HandlerContext(
            conn=self.conn,
            principal=dict(USER),
            actor_kind="user",
            via="direct",
            request_id="req-g3",
            home=self.tmp.name,
            cmd=cmd,
            args=args,
        )

    def test_project_complete_redacts_completion_report_json(self) -> None:
        exact = _tok("exact_g3_", "z", 20)
        pat_sec = "completepatsecret99"
        register_secret("g3_f1_complete", exact)
        try:
            report = {
                "summary": f"done with {exact}",
                "criteria": [{"id": "crt_1", "met": True, "evidence": f"api_key={pat_sec}"}],
            }
            handle_project_complete(self._ctx("project.complete", {"project_id": "prj_1", "report": report}))
            raw = self.conn.execute("SELECT completion_report_json FROM projects WHERE id = 'prj_1';").fetchone()[0]
            self.assertIn("[REDACTED]", raw)
            self.assertNotIn(exact, raw)
            self.assertNotIn(pat_sec, raw)
        finally:
            unregister_secret("g3_f1_complete")

    def test_project_set_lead_redacts_handover_and_lead_changed_outbox(self) -> None:
        exact = _tok("exact_sl_", "q", 20)
        pat_sec = "setleadpwsecret88"
        goal_sec = "goalkeysecret77"
        register_secret("g3_f1_setlead", exact)
        try:
            self.conn.execute(
                "UPDATE projects SET goal = ? WHERE id = 'prj_1';",
                (f"Ship api_key={goal_sec} with {exact}",),
            )
            self.conn.commit()
            reason = f"password={pat_sec} {exact}"
            handle_project_set_lead(
                self._ctx("project.set_lead", {"project_id": "prj_1", "agent": "lead2", "reason": reason})
            )
            rows = self.conn.execute(
                "SELECT type, payload_json FROM outbox WHERE type IN ('project.handover', 'project.lead_changed') ORDER BY type;"
            ).fetchall()
            by_type = {r["type"]: r["payload_json"] for r in rows}
            self.assertEqual(set(by_type), {"project.handover", "project.lead_changed"})
            for mtype, payload_json in by_type.items():
                self.assertIn("[REDACTED]", payload_json, mtype)
                self.assertNotIn(exact, payload_json, mtype)
                self.assertNotIn(pat_sec, payload_json, mtype)
                self.assertNotIn(goal_sec, payload_json, mtype)
        finally:
            unregister_secret("g3_f1_setlead")

    def test_inbox_list_and_wait_redact_raw_db_and_post_registered_secrets(self) -> None:
        post_secret = _tok("post_ibx_", "m", 20)
        pat_sec = "rawoutboxkey777"
        self.conn.execute(
            "INSERT INTO events (id, ts, type, actor_id, actor_kind, via, project_id, payload_json, prev_hash, hash) "
            "VALUES ('evt_raw_1', 'now', 'project.note', 'usr_1', 'user', 'direct', 'prj_1', '{}', 'genesis', 'h1');"
        )
        seq = self.conn.execute("SELECT seq FROM events WHERE id = 'evt_raw_1';").fetchone()[0]
        raw_payload = json.dumps({"note": f"api_key={pat_sec}", "extra": post_secret})
        self.conn.execute(
            "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, status, attempts, next_attempt_at, created_at) "
            "VALUES ('msg_1', ?, 'usr_1', 'inbox', 'dk1', 'prj_1', 'project.note', ?, 'pending', 0, 'now', 'now');",
            (seq, raw_payload),
        )
        self.conn.commit()

        # Secret registered AFTER persistence must still be redacted on read.
        register_secret("g3_f1_post_ibx", post_secret)
        try:
            listed = handle_inbox_list(self._ctx("inbox.list", {"user": True, "peek": True})).data["items"]
            self.assertEqual(len(listed), 1)
            dumped_list = json.dumps(listed[0]["payload"])
            self.assertIn("[REDACTED]", dumped_list)
            self.assertNotIn(pat_sec, dumped_list)
            self.assertNotIn(post_secret, dumped_list)

            srv = _FakeServer()
            waited = asyncio.run(
                handle_inbox_wait(srv, self._ctx("inbox.wait", {"timeout": "1s", "user": True}), _FakeReader())
            ).data["items"]
            self.assertEqual(len(waited), 1)
            dumped_wait = json.dumps(waited[0]["payload"])
            self.assertIn("[REDACTED]", dumped_wait)
            self.assertNotIn(pat_sec, dumped_wait)
            self.assertNotIn(post_secret, dumped_wait)
        finally:
            unregister_secret("g3_f1_post_ibx")

    def test_event_list_and_tail_redact_reason_and_payload_on_read(self) -> None:
        post_secret = _tok("post_evt_", "k", 20)
        reason_pat = "evtreasonpw55"
        payload_pat = "evtpaysecret66"
        self.conn.execute(
            "INSERT INTO events (id, ts, type, actor_id, actor_kind, via, project_id, reason, payload_json, prev_hash, hash) "
            "VALUES ('evt_leak_1', 'now', 'project.note', 'usr_1', 'user', 'direct', 'prj_1', ?, ?, 'genesis', 'h1');",
            (f"password={reason_pat} {post_secret}", json.dumps({"msg": f"secret={payload_pat}", "tok": post_secret})),
        )
        self.conn.commit()

        register_secret("g3_f1_post_evt", post_secret)
        try:
            for res in (
                handle_event_list(self._ctx("event.list", {"limit": 10})),
                _list_events(self._ctx("event.list", {"limit": 10}), "event.list"),
            ):
                items = [e for e in res.data["items"] if e["id"] == "evt_leak_1"]
                self.assertEqual(len(items), 1)
                dumped = json.dumps(items[0])
                self.assertIn("[REDACTED]", items[0]["reason"])
                self.assertNotIn(reason_pat, dumped)
                self.assertNotIn(payload_pat, dumped)
                self.assertNotIn(post_secret, dumped)

            srv = _FakeServer()
            writer = _FakeWriter(stop_after_write=True)
            asyncio.run(
                handle_event_tail_live(
                    srv,
                    self._ctx("event.tail", {"since": 0, "timeout": "1s"}),
                    _FakeReader(),
                    writer,
                )
            )
            streamed = [e for e in writer.lines() if e["id"] == "evt_leak_1"]
            self.assertEqual(len(streamed), 1)
            dumped_s = json.dumps(streamed[0])
            self.assertIn("[REDACTED]", streamed[0]["reason"])
            self.assertNotIn(reason_pat, dumped_s)
            self.assertNotIn(payload_pat, dumped_s)
            self.assertNotIn(post_secret, dumped_s)
        finally:
            unregister_secret("g3_f1_post_evt")


class TestBatch7G3RuntimeIsolation(unittest.TestCase):
    """Finding 2: entrypoint -P -s isolation, site-packages PYTHONPATH filtering, umask 0o077."""

    def test_entry_module_imports_only_posix_and_sys(self) -> None:
        entry_path = os.path.join(get_install_root(), "mesh", "cli", "entry.py")
        with open(entry_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=entry_path)
        imported = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    imported.update(alias.name for alias in sub.names)
                elif isinstance(sub, ast.ImportFrom) and sub.module:
                    imported.add(sub.module)
        self.assertTrue(imported.issubset({"posix", "nt", "_winapi", "sys"}), imported)
        expected_active = {"posix", "sys"} if sys.platform != "win32" else {"nt", "_winapi", "sys"}
        self.assertTrue(expected_active.issubset(imported), imported)
        for name in expected_active:
            self.assertIn(name, sys.builtin_module_names, f"non-builtin import {name} in entry.py")

    def test_console_entrypoint_isolates_pythonpath_safe_path_user_site_and_umask(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f2-exec-") as d:
            cwd_dir = os.path.join(d, "hostile_cwd")
            evil_dir = os.path.join(d, "evil_pp")
            home = os.path.join(d, "mesh_home")
            pf = os.path.join(d, "pw")
            os.makedirs(cwd_dir, mode=0o700)
            os.makedirs(evil_dir, mode=0o700)
            with open(pf, "w", encoding="utf-8") as f:
                f.write(PASS + "\n")
            os.chmod(pf, 0o600)

            for bad_dir in (cwd_dir, evil_dir):
                with open(os.path.join(bad_dir, "json.py"), "w", encoding="utf-8") as f:
                    f.write("raise RuntimeError('hostile json.py imported!')\n")

            repo_root = os.path.realpath(get_install_root())
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(home, PASS)

            env = dict(os.environ)
            env.pop("_MESH_ORIG_PYTHONPATH", None)
            env.pop("PYTHONSAFEPATH", None)
            env["PYTHONNOUSERSITE"] = "0"
            env["MESH_TESTING"] = "1"
            env["PYTHONPATH"] = f"{evil_dir}{os.pathsep}{repo_root}"

            script = (
                "import os, sys; "
                + ("os.umask(0o022); " if sys.platform != "win32" else "")
                + "from mesh.cli.entry import main; "
                "sys.exit(main())"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script, "--home", home, "doctor"],
                cwd=cwd_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            data = json.loads(proc.stdout)
            self.assertEqual(data["data"]["status"], "healthy")

            import mesh.cli.entry as entry

            exec_calls = []
            fake_flags = SimpleNamespace(safe_path=0, no_user_site=0)
            sp_dir = os.path.join(d, "lib", "python3.11", "site-packages")
            os.makedirs(sp_dir, exist_ok=True)
            if sys.platform == "win32":
                test_env = {"PYTHONPATH": f"{sp_dir}{os.pathsep}{evil_dir}"}
                with mock.patch.object(sys, "flags", fake_flags), \
                     mock.patch.object(sys, "argv", ["mesh", "whoami"]), \
                     mock.patch.object(entry.nt, "environ", test_env), \
                     mock.patch.object(entry._winapi, "CreateProcess", side_effect=lambda *a: (exec_calls.append(a), (1, 2, 3, 4))[1]), \
                     mock.patch.object(entry._winapi, "WaitForSingleObject", return_value=0), \
                     mock.patch.object(entry._winapi, "GetExitCodeProcess", return_value=0), \
                     mock.patch.object(entry._winapi, "CloseHandle"):
                    with self.assertRaises(SystemExit):
                        entry.ensure_isolated_process()
                self.assertEqual(len(exec_calls), 1)
                exe, cmdline = exec_calls[0][0], exec_calls[0][1]
                new_env = exec_calls[0][6]
                self.assertEqual(exe, sys.executable)
                self.assertIn("-P -s -m mesh whoami", cmdline)
                self.assertEqual(new_env.get("_MESH_ORIG_PYTHONPATH"), f"{sp_dir}{os.pathsep}{evil_dir}")
                self.assertNotIn("site-packages", new_env.get("PYTHONPATH", ""))
            else:
                # Also verify ensure_isolated_process builds exact [-P, -s, -m, mesh] execve args.
                old_umask = posix.umask(0o022)
                try:
                    test_env = {b"PYTHONPATH": f"{sp_dir}:{evil_dir}".encode("utf-8")}
                    with mock.patch.object(sys, "flags", fake_flags), \
                         mock.patch.object(sys, "argv", ["mesh", "whoami"]), \
                         mock.patch.object(posix, "environ", test_env), \
                         mock.patch.object(posix, "execve", side_effect=lambda *a: exec_calls.append(a)):
                        entry.ensure_isolated_process()
                    current = posix.umask(0o077)
                    self.assertEqual(current, 0o077)
                finally:
                    posix.umask(old_umask)
                self.assertEqual(len(exec_calls), 1)
                exe, argv, new_env = exec_calls[0]
                self.assertEqual(exe, sys.executable)
                self.assertEqual(argv[:4], [sys.executable, "-P", "-s", "-m"])
                self.assertEqual(argv[4:], ["mesh", "whoami"])
                self.assertEqual(new_env.get(b"_MESH_ORIG_PYTHONPATH"), f"{sp_dir}:{evil_dir}".encode("utf-8"))
                self.assertNotIn(b"site-packages", new_env.get(b"PYTHONPATH", b""))

    def test_site_packages_excluded_from_pythonpath_in_all_helpers(self) -> None:
        from mesh.cli.commands import setup as setup_cmd
        from mesh.daemon import merge, supervisor

        sp = "/opt/pipx/venvs/agent-mesh/lib/python3.11/site-packages"
        with mock.patch("site.getsitepackages", return_value=[sp, "/usr/lib/python3/dist-packages"]):
            self.assertTrue(is_site_packages_dir(sp))
            self.assertTrue(is_site_packages_dir("/usr/lib/python3/dist-packages"))
            self.assertFalse(is_site_packages_dir(get_install_root()))
        self.assertFalse(is_site_packages_dir("/opt/custom/site-packages"))

        with tempfile.TemporaryDirectory(prefix="mh-g3-f2-sp-") as d:
            home = os.path.join(d, "h")
            os.makedirs(home, mode=0o700)
            env_key = "LOCALAPPDATA" if sys.platform == "win32" else "XDG_CACHE_HOME"
            with mock.patch("mesh.core.paths.get_install_root", return_value=sp), \
                 mock.patch("site.getsitepackages", return_value=[sp]), \
                 mock.patch.dict(os.environ, {env_key: d}):
                bdir = get_mesh_bin_dir()
                shim_file = os.path.join(bdir, "mesh.cmd" if sys.platform == "win32" else "mesh")
                with open(shim_file, "r", encoding="utf-8") as f:
                    shim = f.read()
                if sys.platform == "win32":
                    self.assertIn('set "PYTHONPATH="', shim)
                else:
                    self.assertIn("unset PYTHONPATH", shim)
                self.assertNotIn(sp, shim)

            base_env = {"PATH": "/usr/bin", "PYTHONPATH": sp}
            with mock.patch("mesh.cli.commands.setup.get_install_root", return_value=sp), \
                 mock.patch("site.getsitepackages", return_value=[sp]), \
                 mock.patch.dict(os.environ, base_env, clear=True):
                menv = setup_cmd._mesh_env()
                self.assertNotIn("PYTHONPATH", menv)

            with mock.patch("mesh.daemon.supervisor.get_install_root", return_value=sp), \
                 mock.patch("site.getsitepackages", return_value=[sp]):
                wenv = supervisor.build_child_env(base_env, "tok", "run_1", 1, "tsk_1", "prj_1", "/tmp/s.sock")
                lenv = supervisor.build_lead_child_env(base_env, "tok", "run_1", "prj_1", "/tmp/s.sock")
                self.assertNotIn("PYTHONPATH", wenv)
                self.assertNotIn("PYTHONPATH", lenv)

            with mock.patch("mesh.daemon.merge.get_install_root", return_value=sp), \
                 mock.patch("site.getsitepackages", return_value=[sp]), \
                 mock.patch.dict(os.environ, {"PATH": "/usr/bin", "PYTHONPATH": sp}, clear=True):
                tenv = merge._test_env("run_m1")
                self.assertNotIn("PYTHONPATH", tenv)

    @unittest.skipUnless(sys.platform != "win32", "POSIX umask(0o077) and S_IMODE permission-bit checks")
    def test_cli_and_daemon_run_server_keep_0077_umask(self) -> None:
        from mesh import client as client_mod
        from mesh.cli import main as cli_main
        from mesh.daemon.server import DaemonServer

        with tempfile.TemporaryDirectory(prefix="mh-g3-f2-um-") as d:
            pf = os.path.join(d, "pw")
            with open(pf, "w", encoding="utf-8") as f:
                f.write(PASS + "\n")
            os.chmod(pf, 0o600)
            nested = os.path.join(d, "a", "b", "mesh_home")

            prev_explicit = client_mod.EXPLICIT_HOME
            old = os.umask(0o022)
            try:
                with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    rc = cli_main.main(["--home", nested, "--passphrase-file", pf, "init"])
                self.assertEqual(rc, 0)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "a")).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "a", "b")).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(nested).st_mode), 0o700)
                self.assertEqual(os.umask(0o022), 0o022)
            finally:
                os.umask(old)
                client_mod.EXPLICIT_HOME = prev_explicit

            # DaemonServer.run_server must keep 0o077 active for its whole lifetime, not just bind().
            h2 = os.path.join(d, "srv_home")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(h2, PASS)
            srv = DaemonServer(h2, testing_mode=True)
            seen_umask = []
            created_during_run = os.path.join(d, "during_run", "nested")

            async def check_and_stop():
                u = os.umask(0o077)
                os.umask(u)
                seen_umask.append(u)
                os.makedirs(created_during_run, exist_ok=True)
                srv.stop_event.set()

            old2 = os.umask(0o022)
            try:
                with mock.patch.object(srv, "setup_subreaper"), \
                     mock.patch.object(srv, "_scheduler_loop", side_effect=check_and_stop):
                    asyncio.run(srv.run_server())
                restored = os.umask(0o077)
                os.umask(restored)
                self.assertEqual(seen_umask, [0o077])
                self.assertEqual(stat.S_IMODE(os.stat(created_during_run).st_mode), 0o700)
                self.assertEqual(restored, 0o022)
            finally:
                os.umask(old2)


class TestBatch7G3TokenFileSecurity(unittest.TestCase):
    """Finding 3: MESH_SOCKET home derivation, PermissionError fail-closed, FIFO/S_ISREG/uid."""

    def test_mesh_socket_derives_home_and_rejects_target_workspace_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f3-sock-") as d:
            target = os.path.join(d, "srv_home")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(target, PASS)
            ws_tok = os.path.join(target, "workspaces", "prj_1", "w.tok")
            os.makedirs(os.path.dirname(ws_tok), mode=0o700, exist_ok=True)
            with open(ws_tok, "w", encoding="utf-8") as f:
                f.write(_tok("agent_"))
            os.chmod(ws_tok, 0o600)

            default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
            sock_path = os.path.join(target, "run", "mesh.sock")
            env_patch = {"MESH_SOCKET": sock_path}
            with mock.patch("mesh.client.EXPLICIT_HOME", False), \
                 mock.patch.dict(os.environ, env_patch, clear=False):
                os.environ.pop("MESH_HOME", None)
                client = MeshClient(default_home)
                self.assertEqual(os.path.realpath(client.home), os.path.realpath(target))
                with self.assertRaises(MeshError) as cm:
                    client._resolve_auth_uncached(
                        SimpleNamespace(token_file=ws_tok, user=False, passphrase_fd=None, passphrase_file=None)
                    )
                self.assertEqual(cm.exception.code, c.TOKEN_FILE_INSECURE)

            with mock.patch("mesh.client.EXPLICIT_HOME", False), \
                 mock.patch.dict(os.environ, {"MESH_SOCKET": "/tmp/custom.sock"}, clear=False):
                os.environ.pop("MESH_HOME", None)
                c2 = MeshClient(default_home)
                self.assertEqual(os.path.realpath(c2.home), default_home)

    def test_configured_roots_fails_closed_on_permission_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f3-roots-") as d:
            with mock.patch("mesh.core.token_roots.os.lstat", side_effect=PermissionError("denied")):
                with self.assertRaises(MeshError) as cm:
                    configured_roots(d)
                self.assertEqual(cm.exception.code, c.TOKEN_FILE_INSECURE)
                self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)

            with mock.patch("mesh.core.token_roots.os.lstat", side_effect=FileNotFoundError("nope")):
                self.assertEqual(configured_roots(d), (None, []))

    @unittest.skipUnless(hasattr(os, "mkfifo") and sys.platform != "win32", "POSIX mkfifo/symlink/fstat UID token-file checks; Win32 DACL checks in test_platform_locks_perms.py")
    def test_token_file_checks_reject_fifo_symlink_and_foreign_uid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f3-tf-") as d:
            home = os.path.join(d, "h")
            tok_dir = os.path.join(d, "toks")
            os.makedirs(home, mode=0o700)
            os.makedirs(tok_dir, mode=0o700)

            fifo = os.path.join(tok_dir, "fifo.tok")
            os.mkfifo(fifo, 0o600)
            for fn in (check_token_file_secure, read_token_file_secure):
                with self.assertRaises(MeshError) as cm:
                    fn(fifo, home)
                self.assertEqual(cm.exception.code, c.TOKEN_FILE_INSECURE)

            real_tok = os.path.join(tok_dir, "real.tok")
            tok_val = _tok("agent_ok_", "b", 20)
            with open(real_tok, "w", encoding="utf-8") as f:
                f.write(tok_val + "\n")
            os.chmod(real_tok, 0o600)

            link_tok = os.path.join(tok_dir, "link.tok")
            os.symlink(real_tok, link_tok)
            with self.assertRaises(MeshError) as cm:
                read_token_file_secure(link_tok, home)
            self.assertEqual(cm.exception.code, c.TOKEN_FILE_INSECURE)

            real_st = os.stat(real_tok)
            foreign_st = os.stat_result(
                (real_st.st_mode, real_st.st_ino, real_st.st_dev, real_st.st_nlink,
                 os.getuid() + 1000, real_st.st_gid, real_st.st_size,
                 real_st.st_atime, real_st.st_mtime, real_st.st_ctime)
            )
            with mock.patch("mesh.core.paths.os.fstat", return_value=foreign_st):
                with self.assertRaises(MeshError) as cm:
                    read_token_file_secure(real_tok, home)
                self.assertEqual(cm.exception.code, c.TOKEN_FILE_INSECURE)

            self.assertEqual(read_token_file_secure(real_tok, home), tok_val)


class TestBatch7G3TripwireEnv(unittest.TestCase):
    """Finding 4: build_external_env preserves _TRIPWIRE_FILE/_SUITE_TRIPWIRE_FILE."""

    def test_build_external_env_preserves_tripwire_vars(self) -> None:
        env = c.build_external_env(
            {
                "PATH": "/usr/bin",
                "_TRIPWIRE_FILE": "/tmp/tw.log",
                "_SUITE_TRIPWIRE_FILE": "/tmp/stw.log",
                "MESH_TRIPWIRE_FILE": "/tmp/legacy.log",
                "MESH_TOKEN": _tok("run_"),
            }
        )
        self.assertEqual(env.get("_TRIPWIRE_FILE"), "/tmp/tw.log")
        self.assertEqual(env.get("_SUITE_TRIPWIRE_FILE"), "/tmp/stw.log")
        self.assertNotIn("MESH_TRIPWIRE_FILE", env)
        self.assertNotIn("MESH_TOKEN", env)


class TestBatch7G3RunwrapDrainAndUtf8(unittest.TestCase):
    """Finding 5: runwrap flushes StreamRedactor/decoder on drain timeout and avoids splitting UTF-8."""

    def test_utf8_safe_prefix_and_capped_multibyte_output(self) -> None:
        from mesh import runwrap

        sample = "ab\u00e9\U0001f680cd".encode("utf-8")
        for limit in range(len(sample) + 1):
            pref = runwrap._utf8_safe_prefix(sample, limit)
            self.assertLessEqual(len(pref), limit)
            pref.decode("utf-8", errors="strict")

        with tempfile.TemporaryDirectory(prefix="mh-g3-f5-utf8-") as d:
            spec = {
                "argv": [sys.executable, "-c", "import sys; sys.stdout.write('\\U0001f680' * 20); sys.stdout.flush()"],
                "cwd": d,
                "stdin": "devnull",
                "log_cap_bytes": 25,
            }
            with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
                json.dump(spec, f)
            rc = runwrap.run(d)
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "stdout.log"), "rb") as f:
                raw = f.read()
            body = raw.split(b"\n[runwrap]")[0]
            self.assertLessEqual(len(body), 25)
            decoded = raw.decode("utf-8", errors="strict")
            self.assertTrue(decoded.startswith("\U0001f680" * 6))

    def test_runwrap_flushes_multiline_secret_and_partial_line_past_drain_grace(self) -> None:
        from mesh import runwrap

        multi_secret = "FIRST_LINE_SECRET_VAL\nSECOND_LINE_SECRET_VAL\nTHIRD_LINE_SECRET_VAL"
        with tempfile.TemporaryDirectory(prefix="mh-g3-f5-drain-") as d:
            holder_pid_file = os.path.join(d, "holder.pid")
            child_script = (
                "import os, subprocess, sys; "
                f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], close_fds=False); "
                f"open({holder_pid_file!r}, 'w').write(str(p.pid)); "
                "sys.stdout.write(os.environ['G3_MULTI_SECRET'] + '\\npartial-deliverable-line'); "
                "sys.stdout.flush()"
            )
            spec = {
                "argv": [sys.executable, "-c", child_script],
                "cwd": d,
                "stdin": "devnull",
                "redact_env_names": ["G3_MULTI_SECRET"],
            }
            with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as f:
                json.dump(spec, f)

            try:
                with mock.patch.object(runwrap, "PIPE_DRAIN_GRACE_S", 0.2), \
                     mock.patch.dict(os.environ, {"G3_MULTI_SECRET": multi_secret}):
                    rc = runwrap.run(d)
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.exists(os.path.join(d, "exit.json")))
                with open(os.path.join(d, "stdout.log"), "r", encoding="utf-8") as f:
                    out = f.read()
                self.assertIn("[REDACTED", out)
                self.assertIn("partial-deliverable-line", out)
                self.assertNotIn("FIRST_LINE_SECRET_VAL", out)
                self.assertNotIn("SECOND_LINE_SECRET_VAL", out)
                self.assertNotIn("THIRD_LINE_SECRET_VAL", out)
            finally:
                if os.path.exists(holder_pid_file):
                    try:
                        with open(holder_pid_file, encoding="utf-8") as pf:
                            pid = int(pf.read().strip())
                        if sys.platform == "win32":
                            from tests.helpers import _win_terminate_and_wait_pid
                            _win_terminate_and_wait_pid(pid)
                        else:
                            os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


class TestBatch7G3SecurityEventRoutingAndWakeups(unittest.TestCase):
    """Finding 6: security.credential_in_run channel & waiter/scheduler wakeups on auth events."""

    def test_security_credential_in_run_routes_to_lead_wake_for_launch_lead(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f6-fan-") as d:
            conn = db.init_db(os.path.join(d, "mesh.db"))
            try:
                _seed_project_with_leads(conn, delivery="launch")
                ev = record_event(
                    conn,
                    type_="security.credential_in_run",
                    actor_id="ag_1",
                    actor_kind="agent",
                    via="token",
                    project_id="prj_1",
                    payload={"run_id": "run_1", "credential_type": "user"},
                )
                fan_out_event(conn, ev)
                conn.commit()
                rows = conn.execute(
                    "SELECT recipient_id, channel FROM outbox WHERE event_seq = ?;", (ev["seq"],)
                ).fetchall()
                by_rec = {r["recipient_id"]: r["channel"] for r in rows}
                self.assertEqual(by_rec.get("ag_1"), "lead_wake")
                self.assertEqual(by_rec.get("usr_1"), "inbox")
            finally:
                conn.close()

    def test_authenticate_and_authorize_wakes_waiters_and_scheduler(self) -> None:
        from mesh.daemon.server import DaemonServer

        with tempfile.TemporaryDirectory(prefix="mh-g3-f6-srv-") as d:
            home = os.path.join(d, "h")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(home, PASS)
            srv = DaemonServer(home, testing_mode=True)
            srv.db_conn = db.connect_db(os.path.join(home, "mesh.db"))
            try:
                _seed_project_with_leads(srv.db_conn, delivery="launch")
                srv.db_conn.execute(
                    "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, run_dir, started_at) "
                    "VALUES ('run_1','lead_turn','prj_1','ag_1',1,'fake','running','boot_1',?,'now');",
                    (d,),
                )
                srv.db_conn.commit()

                notify_mock = mock.AsyncMock()
                wake_mock = mock.MagicMock()
                with mock.patch.object(srv, "notify_waiters", notify_mock), \
                     mock.patch.object(srv, "wake_scheduler", wake_mock):
                    with mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None):
                        with self.assertRaises(MeshError):
                            asyncio.run(
                                srv.authenticate_and_authorize(
                                    peer_pid=os.getpid(),
                                    auth_info={"type": "user", "passphrase": "wrong-passphrase-value"},
                                    cmd="whoami",
                                    request_id="req-badpw",
                                )
                            )
                    self.assertEqual(notify_mock.await_count, 1)

                    notify_mock.reset_mock()
                    wake_mock.reset_mock()
                    with mock.patch("mesh.daemon.server.classify_peer_run_session", return_value="run_1"):
                        with self.assertRaises(MeshError) as cm:
                            asyncio.run(
                                srv.authenticate_and_authorize(
                                    peer_pid=os.getpid(),
                                    auth_info={"type": "user", "passphrase": PASS},
                                    cmd="whoami",
                                    request_id="req-credrun",
                                )
                            )
                        self.assertEqual(cm.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
                    self.assertEqual(wake_mock.call_count, 1)
                    self.assertEqual(notify_mock.await_count, 1)
            finally:
                srv.db_conn.close()


class TestBatch7G3InboxWaitRecheckAndBoolMax(unittest.TestCase):
    """Finding 7: inbox.wait rechecks check_authz on wakeup and rejects boolean max_count."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-g3-f7-")
        self.conn = db.init_db(os.path.join(self.tmp.name, "mesh.db"))
        _seed_project_with_leads(self.conn, delivery="inbox")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_inbox_wait_rejects_boolean_max(self) -> None:
        srv = _FakeServer()
        ctx = HandlerContext(
            conn=self.conn,
            principal=dict(USER),
            actor_kind="user",
            via="direct",
            request_id="req-bool",
            home=self.tmp.name,
            cmd="inbox.wait",
            args={"timeout": "1s", "max": True, "user": True},
        )
        with self.assertRaises(MeshError) as cm:
            asyncio.run(handle_inbox_wait(srv, ctx, _FakeReader()))
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

    def test_inbox_wait_superseded_lead_raises_and_leaves_pending(self) -> None:
        _tok_s, lookup, secret_hash = generate_run_token()
        scope = {
            "role": "lead",
            "project_id": "prj_1",
            "lead_assignment_id": "asg_1",
            "run_id": "run_l1",
        }
        self.conn.execute(
            "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at, expires_at) "
            "VALUES ('cred_l1','ag_1','run_token',?,?,?,'now','9999-12-31T23:59:59Z');",
            (lookup, secret_hash, json.dumps(scope)),
        )
        self.conn.commit()

        lead_principal = {
            "id": "ag_1",
            "name": "lead1",
            "capabilities": [],
            "credential_id": "cred_l1",
            "credential_kind": "run_token",
            "scope": scope,
            "run_id": "run_l1",
        }
        ctx = HandlerContext(
            conn=self.conn,
            principal=lead_principal,
            actor_kind="agent",
            via="run_token",
            request_id="req-wait-sup",
            home=self.tmp.name,
            cmd="inbox.wait",
            args={"timeout": "5s", "max": 5},
        )
        srv = _FakeServer()

        async def scenario():
            task = asyncio.create_task(handle_inbox_wait(srv, ctx, _FakeReader()))
            while srv.active_waits < 1:
                await asyncio.sleep(0.01)

            # Supersede lead_assignment without revoking the run token yet, then deliver a new message.
            self.conn.execute(
                "UPDATE lead_assignments SET ended_at = ?, ended_reason = 'set_lead' WHERE id = 'asg_1';",
                (now_iso_utc(),),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('asg_2','prj_1','ag_2','usr_1','direct',?);",
                (now_iso_utc(),),
            )
            self.conn.execute(
                "INSERT INTO events (id, ts, type, actor_id, actor_kind, via, project_id, payload_json, prev_hash, hash) "
                "VALUES ('evt_after_sup', 'now', 'project.note', 'usr_1', 'user', 'direct', 'prj_1', '{\"text\":\"secret handover\"}', 'genesis', 'h1');"
            )
            seq = self.conn.execute("SELECT seq FROM events WHERE id = 'evt_after_sup';").fetchone()[0]
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, payload_json, status, attempts, next_attempt_at, created_at) "
                "VALUES ('msg_new', ?, 'ag_1', 'inbox', 'dk_sup', 'prj_1', 'project.note', '{\"text\":\"secret handover\"}', 'pending', 0, 'now', 'now');",
                (seq,),
            )
            self.conn.commit()

            async with srv.notify_cond:
                srv.notify_cond.notify_all()
            with self.assertRaises(MeshError) as cm:
                await task
            return cm.exception

        exc = asyncio.run(scenario())
        self.assertEqual(exc.code, c.LEAD_SUPERSEDED)
        status = self.conn.execute("SELECT status FROM outbox WHERE id = 'msg_new';").fetchone()[0]
        self.assertEqual(status, "pending")


class TestBatch7G3LineRedactorBoundaries(unittest.TestCase):
    """Finding 8: _LineRedactor must not arm on redacted tails or non-boundary suffixes."""

    def test_no_false_arm_on_redacted_tails_or_non_boundary_suffixes(self) -> None:
        cases = [
            ("api_key=my_secret:\nstatus ok\n", "api_key=[REDACTED]\nstatus ok\n"),
            ("password=bearer\nhello world\n", "password=[REDACTED]\nhello world\n"),
            ("ringbearer\nwalked away\n", "ringbearer\nwalked away\n"),
            ("the standard-bearer\nwalked away\n", "the standard-bearer\nwalked away\n"),
            ("nonsecret\n= value\n", "nonsecret\n= value\n"),
            ("password:\nhunter2\n", "password:\n[REDACTED]\n"),
            ("Bearer\nmy_tok_123\n", "Bearer\n[REDACTED]\n"),
            ("bearer\nmy_tok_123\n", "bearer\n[REDACTED]\n"),
            ("db_password=hunter2\n", "db_password=[REDACTED]\n"),
            ("db_password:\nhunter2\n", "db_password:\n[REDACTED]\n"),
            ("OPENAI_API_KEY=\nmy_secret_val\n", "OPENAI_API_KEY=\n[REDACTED]\n"),
            ("client_secret\n= top_secret\n", "client_secret\n= [REDACTED]\n"),
            ("unpassword:\nhello\n", "unpassword:\nhello\n"),
        ]
        for raw, expected in cases:
            self.assertEqual(redact(raw, exact_values=[]), expected, raw)
            sr_whole = StreamRedactor(exact_values=[])
            self.assertEqual(sr_whole.feed(raw) + sr_whole.flush(), expected, raw)
            sr_byte = StreamRedactor(exact_values=[])
            got = "".join(sr_byte.feed(ch) for ch in raw) + sr_byte.flush()
            self.assertEqual(got, expected, raw)

        # Exact value ending in "secret:" also must not arm next line
        exact_cases = [("api_key=secret:\nstatus ok\n", [("K", "secret:")], "api_key=[REDACTED:K]\nstatus ok\n")]
        for raw, exacts, expected in exact_cases:
            self.assertEqual(redact(raw, exact_values=exacts), expected)
            sr = StreamRedactor(exact_values=exacts)
            self.assertEqual("".join(sr.feed(ch) for ch in raw) + sr.flush(), expected)


class TestBatch7G3HardeningMisc(unittest.TestCase):
    """Finding 9: passphrase validation, deliverable --input dict check, init foreign dir, schema version, logfile rotation."""

    @unittest.skipUnless(hasattr(os, "mkfifo") and sys.platform != "win32", "POSIX mkfifo/symlink passphrase checks; Win32 checks in test_platform_locks_perms.py")
    def test_passphrase_rejects_special_files_bad_utf8_and_empty_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f9-pp-") as d:
            fifo = os.path.join(d, "pp.fifo")
            os.mkfifo(fifo, 0o600)
            bad_utf8 = os.path.join(d, "bad.bin")
            with open(bad_utf8, "wb") as f:
                f.write(b"\xff\xfe\xfd\n")
            os.chmod(bad_utf8, 0o600)
            real_pp = os.path.join(d, "real.pp")
            with open(real_pp, "w", encoding="utf-8") as f:
                f.write(PASS + "\n")
            os.chmod(real_pp, 0o600)
            link_pp = os.path.join(d, "link.pp")
            os.symlink(real_pp, link_pp)
            sub_dir = os.path.join(d, "subdir")
            os.makedirs(sub_dir, mode=0o700)

            for bad_path in (fifo, link_pp, sub_dir, bad_utf8, os.path.join(d, "missing")):
                args = SimpleNamespace(passphrase_fd=None, passphrase_file=bad_path)
                with self.assertRaises(MeshError, msg=bad_path) as cm:
                    read_passphrase(args, env={})
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

            for args, env in (
                (SimpleNamespace(passphrase_fd=None, passphrase_file=""), {}),
                (SimpleNamespace(passphrase_fd=None, passphrase_file=None), {ENV_PASSPHRASE_FILE: ""}),
                (SimpleNamespace(passphrase_fd=None, passphrase_file=None), {ENV_PASSPHRASE: ""}),
            ):
                with self.assertRaises(MeshError) as cm:
                    read_passphrase(args, env=env)
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

    def test_task_submit_rejects_non_dict_from_json_deliverable(self) -> None:
        from mesh.cli.commands import task as task_cmd

        for bad_val in ("-", "/etc/hostname", 123, ["a"]):
            args = SimpleNamespace(
                task_id="tsk_1",
                run="run_1",
                fence=1,
                deliverable=bad_val,
                summary=None,
                home="/tmp/x",
                _from_json={"deliverable"},
            )
            with self.assertRaises(MeshError) as cm:
                task_cmd.handle_task_submit(args)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

    @unittest.skipUnless(sys.platform != "win32", "POSIX st_uid/os.chmod ownership check; Win32 SID check in test_platform_locks_perms.py")
    def test_init_mesh_home_foreign_owner_raises_insecure_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-g3-f9-init-") as d:
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
                    init_mesh_home(foreign_dir, PASS)
                self.assertEqual(cm.exception.code, c.INSECURE_PERMISSIONS)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
                self.assertEqual(cm.exception.details.get("owner_uid"), foreign_uid)

    def test_schema_version_strict_validation(self) -> None:
        from mesh.cli.commands import doctor
        from mesh.daemon.server import DaemonServer

        for bad in ("-3", "1.0", "1a", True, None, ""):
            self.assertIsNone(db.parse_schema_version(bad), repr(bad))
        self.assertEqual(db.parse_schema_version(0), 0)
        self.assertEqual(db.parse_schema_version("0"), 0)
        self.assertEqual(db.parse_schema_version(1), 1)
        self.assertEqual(db.parse_schema_version("2"), 2)

        with tempfile.TemporaryDirectory(prefix="mh-g3-f9-schema-") as d:
            for bad_ver in ("0", "abc", "-1"):
                db_path = os.path.join(d, f"bad_{bad_ver}.db")
                conn = db.init_db(db_path)
                conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version';", (bad_ver,))
                conn.commit()
                with self.assertRaises(DbIntegrityError):
                    db.migrate_db(conn)
                with self.assertRaises(MeshError):
                    doctor._strict_schema_version(conn, db_path)
                conn.close()

            h = os.path.join(d, "srv_h")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(h, PASS)
            conn2 = sqlite3.connect(os.path.join(h, "mesh.db"))
            conn2.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version';")
            conn2.commit()
            conn2.close()
            with self.assertRaises(DbIntegrityError):
                DaemonServer(h, testing_mode=True).setup_storage()

    def test_logfile_keeps_five_files_and_refuses_symlinks(self) -> None:
        self.assertEqual(logfile.KEEP, 4)
        with tempfile.TemporaryDirectory(prefix="mh-g3-f9-log-") as d:
            with mock.patch.object(logfile, "ROTATE_BYTES", 20):
                for i in range(10):
                    logfile.append_line(d, f"entry-{i:02d}-" + ("x" * 20))
            log_dir = os.path.join(d, "logs")
            self.assertTrue(os.path.exists(os.path.join(log_dir, "meshd.log")))
            self.assertTrue(os.path.exists(os.path.join(log_dir, "meshd.log.4")))
            self.assertFalse(os.path.exists(os.path.join(log_dir, "meshd.log.5")))

            if sys.platform != "win32":
                d2 = os.path.join(d, "sym_home")
                os.makedirs(os.path.join(d2, "logs"), mode=0o700)
                target = os.path.join(d, "pwned.txt")
                os.symlink(target, os.path.join(d2, "logs", "meshd.log"))
                logfile.append_line(d2, "should not follow symlink")
                self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
