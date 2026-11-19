"""Unit regression tests for PR #45 round-5 findings (P1 split-index/abort, P2 nested gitlink, P2 delete/prune)."""

import asyncio
import hashlib
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.clock import now_iso_utc
from mesh.core.db import transaction
from mesh.daemon import merge, recovery
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from tests.gitfixture import GitFixture
from tests.unit.test_batch7_pr45_r4 import _ctx, _setup_home_and_db

MESH_PROC_LOCK = "shared"


def _pipeline(fx, repo, run_id, task_id, branch, pre_head, test_command="none", test_argv=None):
    rdir = os.path.join(fx.base, run_id)
    os.makedirs(rdir)
    job = {
        "run_id": run_id, "task_id": task_id, "title": task_id,
        "integration": repo, "integration_branch": "main", "worktree": repo,
        "branch": branch, "pre_head": pre_head,
        "test_command": test_command, "test_timeout_s": 30, "kill_grace_s": 1, "run_dir": rdir,
    }
    if test_argv is not None:
        job["test_argv"] = test_argv
    return merge.run_merge_pipeline(job)


def _index_with_ext(sig: bytes, payload: bytes = b"") -> bytes:
    body = b"DIRC" + struct.pack(">II", 2, 0) + sig + struct.pack(">I", len(payload)) + payload
    return body + hashlib.sha1(body).digest()


class TestR5SplitIndexAndAbort(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_split_index_skip_worktree_is_dirty_not_merged(self):
        fx = self.fx
        repo = fx.make_k0("SplitBypass")
        base = fx.head(repo, "main")
        fx.git(repo, "checkout", "-b", "mesh/prj_1/tsk_2a", base)
        with open(os.path.join(repo, "f2a.txt"), "w") as f:
            f.write("2a\n")
        fx.git(repo, "add", "f2a.txt")
        fx.git(repo, "commit", "-q", "-m", "2a")
        fx.git(repo, "checkout", "-q", "main")
        time.sleep(1.1)
        fx.git(repo, "status")
        script = (
            "import subprocess\n"
            "subprocess.run(['git', 'status'], check=True, stdout=subprocess.DEVNULL)\n"
            "open('calc.py', 'w').write('# CORRUPTED VIA SPLIT INDEX\\n')\n"
            "subprocess.run(['git', 'update-index', '--skip-worktree', 'calc.py'], check=True)\n"
            "subprocess.run(['git', 'update-index', '--split-index'], check=True)\n"
        )
        out = _pipeline(fx, repo, "run_1", "tsk_2a", "mesh/prj_1/tsk_2a", base, "custom",
                        [sys.executable, "-c", script])
        self.assertNotEqual(out["outcome"], "merged", out)
        self.assertEqual(fx.head(repo, "main"), base)
        with open(os.path.join(repo, "calc.py")) as f:
            self.assertNotIn("CORRUPTED", f.read())
        gitdir = os.path.join(repo, ".git")
        self.assertEqual([n for n in os.listdir(gitdir) if n.startswith("sharedindex.")], [])
        flags, _m = merge._parse_index_flags(os.path.join(gitdir, "index"))
        self.assertEqual(flags, {})

    def test_corrupt_index_from_test_is_restored(self):
        fx = self.fx
        repo = fx.make_k0("CorruptIndex")
        base = fx.head(repo, "main")
        fx.git(repo, "checkout", "-b", "mesh/prj_1/tsk_ci", base)
        with open(os.path.join(repo, "f1.txt"), "w") as f:
            f.write("1\n")
        fx.git(repo, "add", "f1.txt")
        fx.git(repo, "commit", "-q", "-m", "f1")
        fx.git(repo, "checkout", "-q", "main")
        out = _pipeline(fx, repo, "run_ci", "tsk_ci", "mesh/prj_1/tsk_ci", base, "custom",
                        [sys.executable, "-c", "open('.git/index', 'wb').write(b'corrupt')"])
        self.assertNotEqual(out["outcome"], "merged")
        self.assertTrue(out["details"].get("restored"), out)
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")))
        self.assertEqual(fx.head(repo, "main"), base)
        # The next normal task still merges.
        fx.git(repo, "checkout", "-q", "-b", "mesh/prj_1/tsk_n", base)
        with open(os.path.join(repo, "n.txt"), "w") as f:
            f.write("n\n")
        fx.git(repo, "add", "n.txt")
        fx.git(repo, "commit", "-q", "-m", "n")
        fx.git(repo, "checkout", "-q", "main")
        out2 = _pipeline(fx, repo, "run_n", "tsk_n", "mesh/prj_1/tsk_n", base)
        self.assertEqual(out2["outcome"], "merged", out2)

    def test_parse_fails_closed_on_mandatory_ext_and_bad_framing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "index")
            with open(p, "wb") as f:
                f.write(_index_with_ext(b"sdir"))
            self.assertEqual(merge._parse_index_flags(p), (None, False))
            with open(p, "wb") as f:
                f.write(_index_with_ext(b"TREE", b"x" * 3))
            self.assertEqual(merge._parse_index_flags(p), ({}, False))
            # Extension size running past the trailer.
            body = b"DIRC" + struct.pack(">II", 2, 0) + b"TREE" + struct.pack(">I", 99) + b"xx"
            with open(p, "wb") as f:
                f.write(body + hashlib.sha1(body).digest())
            self.assertEqual(merge._parse_index_flags(p), (None, False))
            # link to a missing shared index.
            with open(p, "wb") as f:
                f.write(_index_with_ext(b"link", b"\x11" * 20))
            self.assertEqual(merge._parse_index_flags(p), (None, False))

    def test_split_index_shared_flags_are_reported(self):
        fx = self.fx
        repo = fx.make_k0("SplitRead")
        fx.git(repo, "update-index", "--skip-worktree", "calc.py")
        fx.git(repo, "update-index", "--split-index")
        idx = os.path.join(repo, ".git", "index")
        flags, _m = merge._parse_index_flags(idx)
        self.assertIsNotNone(flags)
        self.assertTrue(flags.get("calc.py", (False, False, False))[1])
        # Clearing in place is refused on a split index (callers restore pre_merge.index).
        self.assertEqual(merge._parse_index_flags(idx, clear_hide_flags=True), (None, False))
        # A plain split index with no hide flags is clean, not "corrupt".
        fx.git(repo, "update-index", "--no-skip-worktree", "calc.py")
        fx.git(repo, "update-index", "--split-index")
        flags2, _m = merge._parse_index_flags(idx)
        self.assertEqual(flags2, {})

    def test_split_index_matches_git_ls_files(self):
        fx = self.fx
        repo = fx.make_k0("SplitFuzz")
        for i in range(70):
            with open(os.path.join(repo, f"f{i:02d}.txt"), "w") as f:
                f.write(f"{i}\n")
        fx.git(repo, "add", "-A")
        fx.git(repo, "commit", "-q", "-m", "many")
        fx.git(repo, "update-index", "--skip-worktree", "f03.txt")
        fx.git(repo, "update-index", "--split-index")
        # Post-split changes become replacement / deleted / new entries in the main index.
        fx.git(repo, "update-index", "--assume-unchanged", "f65.txt")
        fx.git(repo, "update-index", "--no-skip-worktree", "f03.txt")
        fx.git(repo, "update-index", "--skip-worktree", "f10.txt")
        fx.git(repo, "rm", "-q", "--cached", "f20.txt")
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("n\n")
        fx.git(repo, "add", "new.txt")
        fx.git(repo, "update-index", "--skip-worktree", "new.txt")
        idx = os.path.join(repo, ".git", "index")
        flags, _m = merge._parse_index_flags(idx)
        self.assertIsNotNone(flags)
        import subprocess
        ls = subprocess.run(["git", "ls-files", "-v", "-z"], cwd=repo, capture_output=True, check=True).stdout
        want = {}
        for rec in ls.split(b"\0"):
            if not rec:
                continue
            tag, name = rec[:1].decode(), rec[2:].decode()
            au, sw = tag.islower(), tag.upper() == "S"
            if au or sw:
                want[name] = (au, sw)
        got = {k: (v[0], v[1]) for k, v in flags.items() if v[0] or v[1]}
        self.assertEqual(got, want)
        self.assertIn("f10.txt", got)
        self.assertIn("f65.txt", got)
        self.assertNotIn("f03.txt", got)


class TestR5NestedGitlinkParent000(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()

    def tearDown(self):
        for root, dirs, _files in os.walk(self.fx.base):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o755)
                except OSError:
                    pass
        self.fx.cleanup()

    def _branch_ok(self, repo, base, name):
        fx = self.fx
        fx.git(repo, "checkout", "-b", f"mesh/prj_1/{name}", base)
        with open(os.path.join(repo, "ok.txt"), "w") as f:
            f.write("ok\n")
        fx.git(repo, "add", "ok.txt")
        fx.git(repo, "commit", "-q", "-m", "ok")
        fx.git(repo, "checkout", "-q", "main")

    def test_populated_nested_submodule_modes_untouched(self):
        fx = self.fx
        repo = fx.make_k0("PopulatedNested")
        base = fx.head(repo, "main")
        fx.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},deps/nested_sub")
        nested = os.path.join(repo, "deps", "nested_sub")
        os.makedirs(os.path.join(nested, "ro_sub"), exist_ok=True)
        with open(os.path.join(nested, "ro_sub", "keep.txt"), "w") as f:
            f.write("keep\n")
        os.chmod(os.path.join(nested, "ro_sub"), 0o555)
        os.chmod(nested, 0o555)
        fx.git(repo, "commit", "-q", "-m", "add populated nested_sub")
        base = fx.head(repo, "main")
        self._branch_ok(repo, base, "tsk_pns")
        _pipeline(fx, repo, "run_pns", "tsk_pns", "mesh/prj_1/tsk_pns", base, "custom",
                  [sys.executable, "-c", "import os; os.chmod('deps', 0o000)"])
        os.chmod(os.path.join(repo, "deps"), 0o755)
        self.assertEqual(os.stat(nested).st_mode & 0o777, 0o555)
        self.assertEqual(os.stat(os.path.join(nested, "ro_sub")).st_mode & 0o777, 0o555)

    def test_empty_nested_gitlink_litter_cleaned_and_merged(self):
        fx = self.fx
        repo = fx.make_k0("EmptyNested")
        base = fx.head(repo, "main")
        fx.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},deps/nested_sub")
        os.makedirs(os.path.join(repo, "deps", "nested_sub"), exist_ok=True)
        fx.git(repo, "commit", "-q", "-m", "add empty nested_sub")
        base = fx.head(repo, "main")
        self._branch_ok(repo, base, "tsk_ens")
        out = _pipeline(
            fx, repo, "run_ens", "tsk_ens", "mesh/prj_1/tsk_ens", base, "custom",
            [sys.executable, "-c",
             "import os; open('deps/nested_sub/leaked.txt', 'w').write('x'); os.chmod('deps', 0o000)"],
        )
        self.assertEqual(out["outcome"], "merged", out)
        self.assertFalse(os.path.exists(os.path.join(repo, "deps", "nested_sub", "leaked.txt")))


class _Srv:
    def track_merge_job(self, job):
        pass


class TestR5WorkspaceDeleteAndPrune(unittest.TestCase):
    def _setup_task(self, tmp, task_id):
        home, conn = _setup_home_and_db(tmp)
        now = now_iso_utc()
        ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", task_id)
        os.makedirs(ws_dir, mode=0o700)
        with open(os.path.join(ws_dir, "keep.txt"), "w") as f:
            f.write("important\n")
        with transaction(conn):
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, created_by, created_at, updated_at) "
                "VALUES (?, 'prj_1', 'Del', 'S', 'code', 'accepted', 'dev', 600, 3, ?, ?, 'usr_1', ?, ?);",
                (task_id, ws_dir, f"mesh/prj_1/{task_id}", now, now),
            )
            apr = create_or_get_pending_approval(
                conn=conn, category="delete", op="workspace.delete", requested_by="agt_lead",
                params={"task_id": task_id}, summary="Del", project_id="prj_1", target_id=task_id,
                testing_mode=True,
            )
            handle_approval_approve(_ctx(
                conn, home, "approval.approve",
                {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                k_user=b"\x01" * 32,
            ))
        return home, conn, ws_dir, apr

    def _delete(self, conn, home, task_id, apr, request_id):
        return asyncio.run(run_handlers.run_workspace_delete(
            _Srv(),
            _ctx(conn, home, "workspace.delete", {"task_id": task_id},
                 principal_id="agt_lead", actor_kind="agent", via="token",
                 approval=apr["approval_id"], request_id=request_id),
        ))

    def _approval_status(self, conn, apr):
        return conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()[0]

    def _deleted_events(self, conn, task_id):
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = ?;", (task_id,)
        ).fetchone()[0]

    def test_tasks_dir_000_is_reopened_and_really_deleted(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5-d1-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_d000")
            tasks_dir = os.path.dirname(ws_dir)
            orig = run_handlers._delete_workspace_dir_sync

            def _chmod_000(*a, **kw):
                os.chmod(tasks_dir, 0o000)
                return orig(*a, **kw)

            try:
                with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_chmod_000):
                    res = self._delete(conn, home, "tsk_d000", apr, "req_d1")
            finally:
                os.chmod(tasks_dir, 0o700)
            self.assertTrue(res.data["deleted"], res.data)
            self.assertFalse(os.path.lexists(ws_dir))
            self.assertEqual(self._approval_status(conn, apr), "consumed")
            self.assertEqual([n for n in os.listdir(os.path.join(home, "run")) if n.startswith("ws_del_")], [])
            conn.close()

    def test_unreadable_target_never_consumes_approval(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5-d2-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_dacc")
            tasks_dir = os.path.dirname(ws_dir)
            orig = run_handlers._delete_workspace_dir_sync

            def _chmod_000(*a, **kw):
                os.chmod(tasks_dir, 0o000)
                return orig(*a, **kw)

            try:
                with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_chmod_000), \
                        mock.patch.object(run_handlers, "make_owned_ancestors_writable", return_value=False):
                    with self.assertRaises(BaseException):
                        self._delete(conn, home, "tsk_dacc", apr, "req_d2")
            finally:
                os.chmod(tasks_dir, 0o700)
            self.assertTrue(os.path.isfile(os.path.join(ws_dir, "keep.txt")))
            self.assertEqual(self._approval_status(conn, apr), "approved")
            self.assertEqual(self._deleted_events(conn, "tsk_dacc"), 0)
            conn.close()

    def _reserve_delete(self, conn, home, ws_dir, task_id, apr, marker):
        import json
        doc = {
            "kind": "delete", "task_id": task_id, "project_id": "prj_1",
            "path": ws_dir, "stripped": ws_dir, "real": os.path.realpath(ws_dir),
            "matched_root": os.path.join(home, "workspaces"),
            "branch": f"mesh/prj_1/{task_id}", "approval_id": apr["approval_id"],
            "requested_by": "agt_lead", "request_id": "req_rec",
        }
        if marker is not None:
            doc["rmtree_marker"] = marker
        with transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?);",
                         (f"ws_res:task:{task_id}", json.dumps(doc)))

    def test_recovery_without_rmtree_marker_rolls_back(self):
        import shutil
        with tempfile.TemporaryDirectory(prefix="mh-r5-d3-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_nomark")
            # Crashed right after reserve; the dir later vanished for unrelated reasons.
            shutil.rmtree(ws_dir)
            self._reserve_delete(conn, home, ws_dir, "tsk_nomark", apr,
                                 os.path.join(home, "run", "ws_del_tsk_nomark_x.json"))
            counts = {}
            recovery._step_ws_reservations(conn, home, counts)
            self.assertEqual(counts.get("workspace_deletes_rolled_back"), 1, counts)
            self.assertEqual(self._approval_status(conn, apr), "approved")
            self.assertEqual(self._deleted_events(conn, "tsk_nomark"), 0)
            conn.close()

    def test_recovery_tasks_dir_000_is_unknown_not_deleted(self):
        import json
        with tempfile.TemporaryDirectory(prefix="mh-r5-d4-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_rec000")
            marker = os.path.join(home, "run", "ws_del_tsk_rec000_x.json")
            with open(marker, "w") as f:
                json.dump({"rmtree_started": True}, f)
            self._reserve_delete(conn, home, ws_dir, "tsk_rec000", apr, marker)
            tasks_dir = os.path.dirname(ws_dir)
            os.chmod(tasks_dir, 0o000)
            try:
                counts = {}
                recovery._step_ws_reservations(conn, home, counts)
            finally:
                os.chmod(tasks_dir, 0o700)
            self.assertNotEqual(counts.get("workspace_deletes_finalized"), 1, counts)
            self.assertTrue(os.path.isfile(os.path.join(ws_dir, "keep.txt")))
            self.assertEqual(self._approval_status(conn, apr), "approved")
            self.assertEqual(self._deleted_events(conn, "tsk_rec000"), 0)
            self.assertFalse(os.path.exists(marker))
            conn.close()

    def test_recovery_with_marker_and_enoent_finalizes(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory(prefix="mh-r5-d5-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_done")
            marker = os.path.join(home, "run", "ws_del_tsk_done_x.json")
            with open(marker, "w") as f:
                json.dump({"rmtree_started": True}, f)
            shutil.rmtree(ws_dir)
            self._reserve_delete(conn, home, ws_dir, "tsk_done", apr, marker)
            counts = {}
            recovery._step_ws_reservations(conn, home, counts)
            self.assertEqual(counts.get("workspace_deletes_finalized"), 1, counts)
            self.assertEqual(self._approval_status(conn, apr), "consumed")
            self.assertFalse(os.path.exists(marker))
            conn.close()

    def test_prune_reconcile_without_active_list_ignores_history(self):
        doc = {"kind": "prune", "project_id": "prj_1", "projects": ["prj_1"],
               "planned": [{"task_id": "tsk_old", "project_id": "prj_1", "status": "merged",
                            "path": "/nonexistent/mh-r5/tsk_old", "branch": "",
                            "repo_path": None, "integration_branch": None, "matched_root": None}]}
        for ckpt in (None, {}, {"root": "/x", "completed": False, "removed": [], "skipped": []},
                     {"active_planned_ids": "bogus"}):
            rec = run_handlers.reconcile_prune_checkpoint_from_data(["/nonexistent"], "/nonexistent", {}, doc, ckpt)
            self.assertEqual(rec["removed"], [], ckpt)
            self.assertFalse(rec["completed"], ckpt)
        # With the worker's list the same task is reconciled as before.
        rec = run_handlers.reconcile_prune_checkpoint_from_data(
            ["/nonexistent"], "/nonexistent", {}, doc, {"active_planned_ids": ["tsk_old"]})
        self.assertEqual([r["task_id"] for r in rec["removed"]], ["tsk_old"])
        self.assertTrue(rec["completed"])


if __name__ == "__main__":
    unittest.main()
