"""Regression tests for Group F findings (P1-8, P2-4, P2-6, P2-8)."""

import sys
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import logfile
from mesh.core.auth import derive_user_keys
from mesh.core.clock import DURATION_PATTERN, now_iso_utc, parse_duration
from mesh.core.db import connect_db, init_db, transaction
from mesh.core.events import compute_event_hash, compute_user_event_mac, record_event, verify_event_chain
from mesh.core.ids import ID_PATTERN, generate_id, is_valid_id, parse_id
from mesh.core.init import init_mesh_home
from mesh.core.repos import deliver_to_base
from mesh.daemon.handlers.agent import handle_agent_token_issue
from mesh.daemon.handlers.audit import handle_audit_verify
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.task import handle_task_start
from mesh.daemon.scheduler import check_queued_tasks_t6
from mesh.errors import MeshError


PASS = "correct-horse-battery-staple"


def _audit_ctx(conn: sqlite3.Connection, home: str, k_user: bytes) -> HandlerContext:
    user_row = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()
    uid = user_row["id"] if user_row else "user"
    return HandlerContext(
        conn=conn,
        principal={"id": uid, "name": "user", "kind": "user"},
        actor_kind="user",
        via="direct",
        request_id=generate_id("req"),
        home=home,
        cmd="audit.verify",
        args={},
        k_user=k_user,
        testing_mode=True,
    )


def _derive_k_user(conn: sqlite3.Connection, passphrase: str = PASS) -> bytes:
    cred = conn.execute("SELECT salt FROM credentials WHERE kind = 'passphrase' LIMIT 1;").fetchone()
    assert cred is not None
    _, k_user, _, _ = derive_user_keys(passphrase, salt=cred["salt"], testing_kdf=True)
    return k_user


def _drop_event_triggers(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS events_no_update;")
    conn.execute("DROP TRIGGER IF EXISTS events_no_delete;")


def _recompute_hashes_from(conn: sqlite3.Connection, start_idx: int = 0) -> None:
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq ASC;").fetchall()]
    prev_hash = "genesis" if start_idx == 0 else rows[start_idx - 1]["hash"]
    for i in range(start_idx, len(rows)):
        row = rows[i]
        row["prev_hash"] = prev_hash
        new_hash = compute_event_hash(row, prev_hash)
        conn.execute(
            "UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?;",
            (prev_hash, new_hash, row["seq"]),
        )
        prev_hash = new_hash


def _recompute_hashes_with_mac(conn: sqlite3.Connection, k_user: bytes, start_idx: int = 0) -> None:
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq ASC;").fetchall()]
    prev_hash = "genesis" if start_idx == 0 else rows[start_idx - 1]["hash"]
    for i in range(start_idx, len(rows)):
        row = rows[i]
        row["prev_hash"] = prev_hash
        new_hash = compute_event_hash(row, prev_hash)
        new_mac = row["user_mac"]
        if row["actor_kind"] == "user" and row["via"] == "direct":
            new_mac = compute_user_event_mac(k_user, new_hash)
        conn.execute(
            "UPDATE events SET prev_hash = ?, hash = ?, user_mac = ? WHERE seq = ?;",
            (prev_hash, new_hash, new_mac, row["seq"]),
        )
        prev_hash = new_hash


class TestP1_8AuditAndEventChain(unittest.TestCase):
    def _init_home_with_events(self, tmp: str):
        home = os.path.join(tmp, "home")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(home, PASS)
        conn = connect_db(os.path.join(home, "mesh.db"))
        k_user = _derive_k_user(conn, PASS)
        user_id = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()["id"]
        with transaction(conn):
            record_event(
                conn,
                type_="config.updated",
                actor_id=user_id,
                actor_kind="user",
                via="direct",
                payload={"key": "inbox.ack_timeout_s", "value": 60},
                k_user=k_user,
            )
            record_event(
                conn,
                type_="project.created",
                actor_id="agt_lead",
                actor_kind="agent",
                via="token",
                project_id="prj_1",
                payload={"name": "P"},
            )
            record_event(
                conn,
                type_="task.created",
                actor_id="agt_lead",
                actor_kind="agent",
                via="token",
                project_id="prj_1",
                task_id="tsk_1",
                payload={"title": "T"},
            )
        return home, conn, k_user

    def test_prefix_middle_and_tail_truncation_detected(self) -> None:
        # 1. Prefix truncation (delete seq=1 system.init, reset seq=2 prev_hash='genesis', recompute hashes)
        with tempfile.TemporaryDirectory(prefix="mh-p18-pfx-") as tmp:
            home, conn, k_user = self._init_home_with_events(tmp)
            try:
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM events WHERE seq = 1;")
                _recompute_hashes_with_mac(conn, k_user, start_idx=0)
                conn.commit()
                self.assertTrue(verify_event_chain(conn))
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_audit_ctx(conn, home, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # 2. Middle deletion (delete seq=2, relink seq=3->seq=1 and recompute hashes)
        with tempfile.TemporaryDirectory(prefix="mh-p18-mid-") as tmp:
            home, conn, k_user = self._init_home_with_events(tmp)
            try:
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM events WHERE seq = 2;")
                _recompute_hashes_with_mac(conn, k_user, start_idx=1)
                conn.commit()
                self.assertTrue(verify_event_chain(conn))
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_audit_ctx(conn, home, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # 3. Prefix truncation with seq renumbered to start at 1 (missing genesis system.init)
        with tempfile.TemporaryDirectory(prefix="mh-p18-renum-") as tmp:
            home, conn, k_user = self._init_home_with_events(tmp)
            try:
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM events WHERE seq <= 2;")
                conn.execute("UPDATE events SET seq = seq - 2;")
                conn.execute("UPDATE sqlite_sequence SET seq = (SELECT MAX(seq) FROM events) WHERE name = 'events';")
                _recompute_hashes_with_mac(conn, k_user, start_idx=0)
                conn.commit()
                self.assertTrue(verify_event_chain(conn))
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_audit_ctx(conn, home, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

        # 4. Tail truncation (delete last event without updating sqlite_sequence)
        with tempfile.TemporaryDirectory(prefix="mh-p18-tail-") as tmp:
            home, conn, k_user = self._init_home_with_events(tmp)
            try:
                _drop_event_triggers(conn)
                conn.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events);")
                conn.commit()
                self.assertTrue(verify_event_chain(conn))
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_audit_ctx(conn, home, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn.close()

    def test_direct_user_only_event_mac_strip_downgrade_detected(self) -> None:
        for ev_type in (
            "system.init",
            "config.updated",
            "agent.created",
            "agent.token_issued",
            "run.seen_release_forced",
            "run.merge_release_forced",
        ):
            with self.subTest(ev_type=ev_type), tempfile.TemporaryDirectory(prefix="mh-p18-mac-") as tmp:
                home, conn, k_user = self._init_home_with_events(tmp)
                try:
                    user_id = conn.execute("SELECT id FROM principals WHERE kind = 'user' LIMIT 1;").fetchone()["id"]
                    if ev_type not in ("system.init", "config.updated"):
                        with transaction(conn):
                            record_event(
                                conn,
                                type_=ev_type,
                                actor_id=user_id,
                                actor_kind="user",
                                via="direct",
                                payload={"test": True},
                                k_user=k_user,
                            )
                    target = conn.execute(
                        "SELECT seq FROM events WHERE type = ? ORDER BY seq ASC LIMIT 1;",
                        (ev_type,),
                    ).fetchone()
                    assert target is not None
                    target_seq = target["seq"]

                    # Downgrade actor_kind/via to system and clear user_mac, recomputing public hashes without k_user
                    _drop_event_triggers(conn)
                    conn.execute(
                        "UPDATE events SET actor_id = 'system', actor_kind = 'system', via = 'system', "
                        "user_mac = NULL WHERE seq = ?;",
                        (target_seq,),
                    )
                    _recompute_hashes_from(conn, start_idx=target_seq - 1)
                    conn.commit()

                    viols = verify_event_chain(conn)
                    self.assertTrue(viols, f"verify_event_chain missed downgraded {ev_type}")
                    with self.assertRaises(MeshError) as cm:
                        handle_audit_verify(_audit_ctx(conn, home, k_user))
                    self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                    self.assertIn(target_seq, cm.exception.details["broken_event_seqs"])
                finally:
                    conn.close()

        # Also verify actor_kind='user' with via!='direct' (e.g. via='relayed', user_mac=NULL) is rejected
        with tempfile.TemporaryDirectory(prefix="mh-p18-via-") as tmp:
            home, conn, k_user = self._init_home_with_events(tmp)
            try:
                _drop_event_triggers(conn)
                conn.execute("UPDATE events SET via = 'relayed', user_mac = NULL WHERE seq = 2;")
                _recompute_hashes_from(conn, start_idx=1)
                conn.commit()
                self.assertTrue(verify_event_chain(conn))
                with self.assertRaises(MeshError) as cm:
                    handle_audit_verify(_audit_ctx(conn, home, k_user))
                self.assertEqual(cm.exception.code, c.AUDIT_MISMATCH)
                self.assertIn(2, cm.exception.details["broken_event_seqs"])
            finally:
                conn.close()


class TestP2_4DeliverHiddenIndexFlags(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mh-p24-")
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(root, "repo")
        self.integ = os.path.join(root, "integration")
        self.integ_branch = "mesh/prj_p24/integration"
        os.makedirs(self.repo)
        self._git(self.repo, "init", "-q", "-b", "main")
        self._git(self.repo, "config", "user.email", "p24@example.invalid")
        self._git(self.repo, "config", "user.name", "p24")
        with open(os.path.join(self.repo, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        self._git(self.repo, "add", "tracked.txt")
        self._git(self.repo, "commit", "-qm", "init")
        self._git(self.repo, "branch", self.integ_branch)
        self._git(self.repo, "worktree", "add", "-q", self.integ, self.integ_branch)
        with open(os.path.join(self.integ, "tracked.txt"), "w", encoding="utf-8") as f:
            f.write("v2\n")
        self._git(self.integ, "add", "tracked.txt")
        self._git(self.integ, "commit", "-qm", "integ update")

    @staticmethod
    def _git(cwd: str, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def test_deliver_rejects_skip_worktree_and_assume_unchanged_with_worktree_dirty(self) -> None:
        main_before = self._git(self.repo, "rev-parse", "refs/heads/main")
        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag=flag):
                self._git(self.repo, "update-index", flag, "tracked.txt")
                with open(os.path.join(self.repo, "tracked.txt"), "w", encoding="utf-8") as f:
                    f.write("local hidden edit\n")
                try:
                    self.assertEqual(self._git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"), "")
                    with self.assertRaises(MeshError) as cm:
                        deliver_to_base(self.repo, "main", self.integ_branch)
                    self.assertEqual(cm.exception.code, c.WORKTREE_DIRTY)
                    self.assertEqual(cm.exception.exit_code, c.EXIT_MERGE_FAILED)
                    self.assertEqual(self._git(self.repo, "rev-parse", "refs/heads/main"), main_before)
                finally:
                    self._git(self.repo, "update-index", "--no-" + flag.lstrip("-"), "tracked.txt")
                    self._git(self.repo, "checkout", "--", "tracked.txt")

        res = deliver_to_base(self.repo, "main", self.integ_branch)
        self.assertEqual(res["method"], "merge_ff_only")


class TestP2_6HomeSubdirAndLogfileSymlinks(unittest.TestCase):
    @staticmethod
    def _dir_link(src: str, dst: str) -> None:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(src, dst)
        else:
            os.symlink(src, dst)

    def test_init_mesh_home_rejects_symlink_and_file_subdirs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-p26-init-") as tmp:
            external = os.path.join(tmp, "external_victim")
            os.makedirs(external, mode=0o755)
            os.chmod(external, 0o755)

            for sub in ("logs", "run", "runs", "workspaces", "merges"):
                with self.subTest(kind="symlink_dir", sub=sub):
                    home = os.path.join(tmp, f"home_sym_{sub}")
                    os.makedirs(home, mode=0o700)
                    self._dir_link(external, os.path.join(home, sub))
                    with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                        with self.assertRaises(MeshError) as cm:
                            init_mesh_home(home, PASS)
                    self.assertEqual(cm.exception.code, c.INSECURE_PERMISSIONS)
                    self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
                    if sys.platform != "win32":
                        self.assertEqual(stat.S_IMODE(os.stat(external).st_mode), 0o755)

            # Regular file and dangling symlink / junction at subdir must also raise INSECURE_PERMISSIONS (rc 2)
            home_file = os.path.join(tmp, "home_file")
            os.makedirs(home_file, mode=0o700)
            with open(os.path.join(home_file, "logs"), "w", encoding="utf-8") as f:
                f.write("not a dir")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                with self.assertRaises(MeshError) as cm_file:
                    init_mesh_home(home_file, PASS)
            self.assertEqual(cm_file.exception.code, c.INSECURE_PERMISSIONS)
            self.assertEqual(cm_file.exception.exit_code, c.EXIT_USAGE)

            home_dangle = os.path.join(tmp, "home_dangle")
            os.makedirs(home_dangle, mode=0o700)
            missing_target = os.path.join(tmp, "missing_target")
            os.makedirs(missing_target, exist_ok=True)
            self._dir_link(missing_target, os.path.join(home_dangle, "logs"))
            os.rmdir(missing_target)
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                with self.assertRaises(MeshError) as cm_d:
                    init_mesh_home(home_dangle, PASS)
            self.assertEqual(cm_d.exception.code, c.INSECURE_PERMISSIONS)
            self.assertEqual(cm_d.exception.exit_code, c.EXIT_USAGE)

    def test_logfile_append_line_rejects_symlink_dirs_and_traversal_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-p26-log-") as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside, mode=0o700)

            # 1. Symlinked / junctioned $home/logs
            h1 = os.path.join(tmp, "h1")
            os.makedirs(h1, mode=0o700)
            self._dir_link(outside, os.path.join(h1, "logs"))
            logfile.append_line(h1, "should not escape logs symlink")
            self.assertFalse(os.path.exists(os.path.join(outside, "meshd.log")))

            # 2. Symlinked / junctioned $home
            real_h = os.path.join(tmp, "real_h")
            os.makedirs(real_h, mode=0o700)
            sym_h = os.path.join(tmp, "sym_h")
            self._dir_link(real_h, sym_h)
            logfile.append_line(sym_h, "should not follow home symlink")
            self.assertFalse(os.path.exists(os.path.join(real_h, "logs", "meshd.log")))

            # 3. Path-traversal or invalid filenames
            h2 = os.path.join(tmp, "h2")
            os.makedirs(h2, mode=0o700)
            for bad_name in ("../escaped.log", "sub/escaped.log", os.path.join(outside, "abs.log"), ".", "..", ""):
                logfile.append_line(h2, "should not write", name=bad_name)
            self.assertFalse(os.path.exists(os.path.join(h2, "escaped.log")))
            self.assertFalse(os.path.exists(os.path.join(outside, "abs.log")))


class TestP2_8SchedulerClockIds(unittest.TestCase):
    def _setup_scheduler_db(self, tmp: str) -> sqlite3.Connection:
        conn = init_db(os.path.join(tmp, "mesh.db"))
        now = now_iso_utc()
        with transaction(conn):
            conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);", (now,))
            conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                "VALUES ('agt_lead', 'none', 'inbox', 1, 1, ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P1', 'G', 'active', 'usr_1', 'direct', ?, ?);",
                (now, now),
            )
            conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES ('la_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
                (now,),
            )
        return conn

    def test_inbox_effort_stays_queued_in_t6_and_rejected_by_task_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-p28-eff-") as tmp:
            conn = self._setup_scheduler_db(tmp)
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ibx', 'agent', 'ibx', ?);", (now,))
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, allowed_models_json, allowed_efforts_json, "
                        "max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_ibx', 'none', 'inbox', 0, '[]', '[\"low\"]', 1, ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, assignee_agent_id, "
                        "effort, profile, timeout_s, max_attempts, queued_at, offered_at, created_by, version, created_at, updated_at) "
                        "VALUES ('tsk_eff1', 'prj_1', 'T1', 'S', 'general', 'queued', 0, 'agt_ibx', 'high', 'edit', 600, 1, ?, ?, 'usr_1', 1, ?, ?);",
                        (now, now, now, now),
                    )

                    conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_vibe', 'agent', 'vibe_ibx', ?);", (now,))
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, allowed_models_json, allowed_efforts_json, "
                        "max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_vibe', 'vibe', 'inbox', 0, '[\"m1\"]', '[\"low\"]', 1, ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, assignee_agent_id, "
                        "model, effort, profile, timeout_s, max_attempts, queued_at, offered_at, created_by, version, created_at, updated_at) "
                        "VALUES ('tsk_eff2', 'prj_1', 'T2', 'S', 'general', 'queued', 0, 'agt_vibe', 'm1', 'low', 'edit', 600, 1, ?, ?, 'usr_1', 1, ?, ?);",
                        (now, now, now, now),
                    )

                failed = check_queued_tasks_t6(conn, home=tmp, testing_mode=True)
                self.assertEqual(failed, [])
                for tid in ("tsk_eff1", "tsk_eff2"):
                    self.assertEqual(
                        conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()["status"],
                        "queued",
                    )

                ctx_ibx = HandlerContext(
                    conn=conn,
                    principal={"id": "agt_ibx", "kind": "agent", "name": "ibx"},
                    actor_kind="agent",
                    via="token",
                    request_id=generate_id("req"),
                    home=tmp,
                    cmd="task.start",
                    args={"task_id": "tsk_eff1"},
                    testing_mode=True,
                )
                with self.assertRaises(MeshError) as cm1:
                    handle_task_start(ctx_ibx)
                self.assertEqual((cm1.exception.code, cm1.exception.exit_code), (c.EFFORT_NOT_ALLOWED, c.EXIT_USAGE))
                self.assertEqual(
                    conn.execute("SELECT status FROM tasks WHERE id = 'tsk_eff1';").fetchone()["status"],
                    "queued",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = 'tsk_eff1';").fetchone()[0],
                    0,
                )

                ctx_vibe = HandlerContext(
                    conn=conn,
                    principal={"id": "agt_vibe", "kind": "agent", "name": "vibe_ibx"},
                    actor_kind="agent",
                    via="token",
                    request_id=generate_id("req"),
                    home=tmp,
                    cmd="task.start",
                    args={"task_id": "tsk_eff2"},
                    testing_mode=True,
                )
                with self.assertRaises(MeshError) as cm2:
                    handle_task_start(ctx_vibe)
                self.assertEqual((cm2.exception.code, cm2.exception.exit_code), (c.EFFORT_UNSUPPORTED, c.EXIT_USAGE))
                self.assertEqual(
                    conn.execute("SELECT status FROM tasks WHERE id = 'tsk_eff2';").fetchone()["status"],
                    "queued",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = 'tsk_eff2';").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_t6_pauses_offered_at_during_ws_reservation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-p28-ws-") as tmp:
            conn = self._setup_scheduler_db(tmp)
            try:
                now = now_iso_utc()
                stale_offered = now_iso_utc(time.time() - 3600)
                with transaction(conn):
                    conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_ibx', 'agent', 'ibx', ?);", (now,))
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_ibx', 'none', 'inbox', 0, 1, ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                        "VALUES ('inbox.ack_timeout_s', '60', ?, 'usr_1');",
                        (now,),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, priority, assignee_agent_id, "
                        "profile, timeout_s, max_attempts, queued_at, offered_at, created_by, version, created_at, updated_at) "
                        "VALUES ('tsk_res', 'prj_1', 'T1', 'S', 'code', 'queued', 0, 'agt_ibx', 'edit', 600, 1, ?, ?, 'usr_1', 1, ?, ?);",
                        (stale_offered, stale_offered, now, now),
                    )
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('ws_res:task:tsk_res', ?);",
                        (json.dumps({"kind": "delete", "task_id": "tsk_res", "project_id": "prj_1"}),),
                    )

                failed = check_queued_tasks_t6(conn, home=tmp, testing_mode=True)
                self.assertEqual(failed, [])
                row = conn.execute("SELECT status, offered_at FROM tasks WHERE id = 'tsk_res';").fetchone()
                self.assertEqual(row["status"], "queued")
                self.assertGreater(row["offered_at"], stale_offered)

                # Once reservation is cleared and offered_at is stale again, T6 times it out
                with transaction(conn):
                    conn.execute("DELETE FROM meta WHERE key = 'ws_res:task:tsk_res';")
                    conn.execute("UPDATE tasks SET offered_at = ? WHERE id = 'tsk_res';", (stale_offered,))
                failed2 = check_queued_tasks_t6(conn, home=tmp, testing_mode=True)
                self.assertEqual(failed2, ["tsk_res"])
                row2 = conn.execute("SELECT status, failure_json FROM tasks WHERE id = 'tsk_res';").fetchone()
                self.assertEqual(row2["status"], "failed")
                self.assertEqual(json.loads(row2["failure_json"])["reason"], "no_response")
            finally:
                conn.close()

    def test_clock_bounds_overflow_and_trailing_newline_rejected(self) -> None:
        max_dur_s = 100 * 365 * 86400
        with self.assertRaises(ValueError):
            parse_duration("9999999999999d")
        with self.assertRaises(ValueError):
            parse_duration(max_dur_s + 1)
        self.assertEqual(parse_duration(max_dur_s), max_dur_s)
        for bad_ts in (1e20, -1e20, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                now_iso_utc(bad_ts)
        self.assertIsNone(DURATION_PATTERN.match("30s\n"))

        # Verify agent.token.issue fails cleanly with INVALID_ARGUMENT (exit 2), not OverflowError
        with tempfile.TemporaryDirectory(prefix="mh-p28-tok-") as tmp:
            conn = init_db(os.path.join(tmp, "mesh.db"))
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_1', 'agent', 'a1', ?);", (now,))
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_1', 'none', 'inbox', 0, 1, ?, ?);",
                        (now, now),
                    )
                ctx = HandlerContext(
                    conn=conn,
                    principal={"id": "usr_1", "kind": "user", "name": "user"},
                    actor_kind="user",
                    via="direct",
                    request_id=generate_id("req"),
                    home=tmp,
                    cmd="agent.token.issue",
                    args={"name": "a1", "expires_in": "9999999999999d"},
                    k_user=b"k" * 32,
                    testing_mode=True,
                )
                with self.assertRaises(MeshError) as cm:
                    handle_agent_token_issue(ctx)
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
            finally:
                conn.close()

    def test_id_pattern_rejects_trailing_newline(self) -> None:
        gid = generate_id("tsk")
        self.assertTrue(is_valid_id(gid))
        self.assertIsNotNone(parse_id(gid))
        self.assertFalse(is_valid_id(gid + "\n"))
        self.assertIsNone(parse_id(gid + "\n"))
        self.assertIsNone(ID_PATTERN.match(gid + "\n"))


if __name__ == "__main__":
    unittest.main()
