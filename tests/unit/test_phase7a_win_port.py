"""Phase 7a (T11) cross-platform and Windows port tests.

Covers every new Win32 branch in ``mesh/core/{repos,workspaces}.py``,
``mesh/daemon/{merge,handlers/run}.py``, ``mesh/adapters/{vibe,devin}.py``, and
``mesh/platform/{proc,_win32}.py`` via mocked Win32 primitives on Linux, plus real
Windows Named Job Object / NTFS junction / handle-wait tests under ``skipUnless``.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, List, Optional, Set
from unittest import mock

import mesh.constants as c
from mesh.adapters import devin as devin_mod
from mesh.adapters import vibe as vibe_mod
from mesh.adapters.base import AdapterUnsupported, RunRequest
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.proc import ScanResult
from mesh.core import repos as repos_mod
from mesh.core import workspaces as workspaces_mod
from mesh.daemon import children, merge as merge_mod, supervisor, terminate
from mesh.daemon.handlers import run as run_handler
from mesh.daemon.handlers.base import HandlerContext
from mesh.errors import MeshError
from mesh.platform import _posix as plat_posix, _win32 as plat_win32, paths as plat_paths, proc as plat_proc
from tests.win_sim import SIMULATED_WIN32_PRELUDE

MESH_PROC_LOCK = "shared"


def _reset_daemon_state() -> None:
    terminate._active.clear()
    terminate._WIN_MY_SID = None
    supervisor._WRAPPERS.clear()
    supervisor._SPAWN_ABORTS.clear()
    with supervisor._RUN_JOBS_LOCK:
        for job in supervisor._RUN_JOBS.values():
            try:
                job.close()
            except Exception:
                pass
        supervisor._RUN_JOBS.clear()
        supervisor._RUN_HOMES.clear()
        supervisor._PGID_RUN_INFO.clear()


def _seed_merge_db(
    conn: sqlite3.Connection,
    run_id: str,
    run_dir: str,
    *,
    wrapper_pid: Optional[int] = None,
    pgid: Optional[int] = None,
    proc_starttime: Optional[int] = None,
    with_lease: bool = False,
) -> str:
    now = now_iso_utc()
    with transaction(conn):
        conn.execute(
            "INSERT INTO projects(id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES('prj_1', 'P', 'G', 'active', 'user', 'cli', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO tasks(id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
            "created_by, created_at, updated_at, version) "
            "VALUES('tsk_1', 'prj_1', 'T', 'spec', 'code', 'merging', 'dev', 60, 1, 'user', ?, ?, 1);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO runs(id, kind, task_id, project_id, fence, adapter, status, boot_id, wrapper_pid, "
            "pgid, proc_starttime, run_dir) "
            "VALUES(?, 'merge_test', 'tsk_1', 'prj_1', 1, 'merge_test', 'running', 'boot_1', ?, ?, ?, ?);",
            (run_id, wrapper_pid, pgid, proc_starttime, run_dir),
        )
        if with_lease:
            conn.execute(
                "INSERT INTO leases(key, run_id, holder_kind, holder, fence, acquired_at, expires_at, heartbeat_at) "
                "VALUES('merge:prj_1', ?, 'meshd', 'meshd', 1, ?, ?, ?);",
                (run_id, now, now, now),
            )
    return now


class _FakeJobHandle:
    def __init__(self, run_id: str, name: str = "job", *, kill_on_close: bool = False) -> None:
        self.run_id = run_id
        self.name = name
        self.kill_on_close = kill_on_close
        self.pids: Set[int] = set()
        self.closed = False
        self.close_calls = 0
        self.terminated_codes: List[int] = []

    def query_pids(self) -> List[int]:
        if self.closed:
            raise OSError(errno.EBADF, "Job closed")
        return sorted(self.pids)

    def terminate(self, exit_code: int = 137) -> None:
        if self.closed:
            raise OSError(errno.EBADF, "Job closed")
        self.terminated_codes.append(int(exit_code))
        self.pids.clear()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _FakePopen:
    def __init__(self, pid: int = 43210, handle: int = 0x1234) -> None:
        self.pid = pid
        self._handle = handle
        self.returncode: Optional[int] = None
        self.kill_calls = 0
        self.terminate_calls = 0
        self.poll_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = 1

    def terminate(self) -> None:
        self.terminate_calls += 1

    def poll(self) -> Optional[int]:
        self.poll_calls += 1
        return self.returncode


class _FakeK32Wait:
    def __init__(self, wait_rc: int = 0, last_error: int = 0) -> None:
        self.wait_rc = wait_rc
        self.last_error = last_error
        self.calls: List[tuple[int, int]] = []
        self.closed_handles: List[int] = []

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        self.calls.append((int(handle), int(timeout_ms)))
        return self.wait_rc

    def CloseHandle(self, handle: int) -> bool:
        self.closed_handles.append(int(handle))
        return True


def _fake_stat(mode: int, *, file_attributes: int = 0, size: int = 16) -> Any:
    ns = types.SimpleNamespace(
        st_mode=mode,
        st_size=size,
        st_uid=0,
        st_gid=0,
        st_file_attributes=file_attributes,
    )
    return ns


class SimulatedWin32ImportHygieneTests(unittest.TestCase):
    def test_t11_modules_import_under_win32_without_fcntl_or_posix(self) -> None:
        script = (
            "import asyncio, shutil, subprocess, tempfile\n"
            + SIMULATED_WIN32_PRELUDE
            + "import mesh.core.repos\n"
            + "import mesh.core.workspaces\n"
            + "import mesh.daemon.merge\n"
            + "import mesh.daemon.handlers.run\n"
            + "import mesh.adapters.vibe\n"
            + "import mesh.adapters.devin\n"
            + "import mesh.platform.proc\n"
            + "print('OK')\n"
        )
        cp = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(cp.returncode, 0, f"stderr={cp.stderr}\nstdout={cp.stdout}")
        self.assertIn("OK", cp.stdout)


class PlatformExitedUnreapedWinTests(unittest.TestCase):
    def test_wait_single_object_checks_without_closing_handle(self) -> None:
        proc = _FakePopen(pid=5555, handle=0xBEEF)

        k_running = _FakeK32Wait(wait_rc=0x102)  # WAIT_TIMEOUT
        self.assertFalse(plat_win32.is_popen_exited_unreaped_win(proc, k32=k_running))
        self.assertEqual(k_running.calls, [(0xBEEF, 0)])
        self.assertEqual(k_running.closed_handles, [])
        self.assertIsNone(proc.returncode)

        k_exited = _FakeK32Wait(wait_rc=0)  # WAIT_OBJECT_0
        self.assertTrue(plat_win32.is_popen_exited_unreaped_win(proc, k32=k_exited))
        self.assertEqual(k_exited.calls, [(0xBEEF, 0)])
        self.assertEqual(k_exited.closed_handles, [])
        self.assertIsNone(proc.returncode)

        k_invalid = _FakeK32Wait(wait_rc=0xFFFFFFFF, last_error=6)
        self.assertTrue(
            plat_win32.is_popen_exited_unreaped_win(
                proc, k32=k_invalid, get_last_error_fn=lambda: k_invalid.last_error
            )
        )

        k_err = _FakeK32Wait(wait_rc=0xFFFFFFFF, last_error=5)
        with self.assertRaises(OSError):
            plat_win32.is_popen_exited_unreaped_win(
                proc, k32=k_err, get_last_error_fn=lambda: k_err.last_error
            )

        proc.returncode = 0
        k_never = _FakeK32Wait(wait_rc=0x102)
        self.assertTrue(plat_win32.is_popen_exited_unreaped_win(proc, k32=k_never))
        self.assertEqual(k_never.calls, [])

    def test_merge_exited_unreaped_routes_via_win32_platform_branch(self) -> None:
        proc = _FakePopen(pid=777, handle=0x9001)
        with (
            mock.patch.object(plat_proc.sys, "platform", "win32"),
            mock.patch.object(plat_win32, "is_popen_exited_unreaped_win", return_value=False) as m_wait,
        ):
            self.assertFalse(merge_mod._exited_unreaped(proc))
            m_wait.assert_called_once_with(proc)


class ReposWinPortTests(unittest.TestCase):
    def test_validate_repo_accepts_same_path_across_drive_case_and_slashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = os.path.realpath(os.path.join(tmp, "repo"))
            os.makedirs(repo_dir)
            fake_toplevel = repo_dir.upper() if os.name == "nt" else repo_dir.swapcase()

            def _fake_git(args: List[str], cwd: str, timeout_s: float = 30.0) -> Any:
                if args[:2] == ["rev-parse", "--show-toplevel"]:
                    return types.SimpleNamespace(returncode=0, stdout=fake_toplevel + "\n", stderr="")
                if args[:2] == ["show-ref", "--verify"]:
                    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
                if args[:2] == ["rev-parse", "--verify"]:
                    return types.SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
                raise AssertionError(f"unexpected git args: {args}")

            with (
                mock.patch("mesh.core.repos.run_git", side_effect=_fake_git),
                mock.patch.object(plat_paths, "same_path", side_effect=lambda a, b: a.lower() == b.lower()),
            ):
                res = repos_mod.validate_repo(repo_dir, "main")
            self.assertEqual(res["repo"], repo_dir)
            self.assertEqual(res["base_branch"], "main")

    def test_is_registered_worktree_rejects_reparse_and_matches_case_folded_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            wt = os.path.join(root, "wt")
            repo = os.path.join(root, "repo")
            wt_gitdir = os.path.join(repo, ".git", "worktrees", "wt")
            os.makedirs(wt)
            os.makedirs(wt_gitdir)
            dot_git = os.path.join(wt, ".git")
            with open(dot_git, "w", encoding="utf-8") as f:
                f.write(f"gitdir: {wt_gitdir.replace(os.sep, '/')}\n")
            with open(os.path.join(wt_gitdir, "commondir"), "w", encoding="utf-8") as f:
                f.write("../..\n")
            with open(os.path.join(wt_gitdir, "gitdir"), "w", encoding="utf-8") as f:
                f.write(f"{dot_git}\n")

            cp_common = types.SimpleNamespace(returncode=0, stdout=(os.path.join(repo, ".git")).upper() + "\n", stderr="")
            with (
                mock.patch("mesh.core.repos.run_git", return_value=cp_common),
                mock.patch.object(plat_paths, "same_path", side_effect=lambda a, b: os.path.normpath(a).lower() == os.path.normpath(b).lower()),
                mock.patch.object(plat_paths, "is_reparse_link", return_value=False),
            ):
                self.assertTrue(repos_mod.is_registered_worktree(wt, repo))

            with (
                mock.patch("mesh.core.repos.run_git", return_value=cp_common),
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.basename(p) == "commondir",
                ),
            ):
                self.assertFalse(repos_mod.is_registered_worktree(wt, repo))

    def test_ignored_paths_overwritten_blocks_windows_reparse_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = os.path.realpath(tmp)
            parent = os.path.join(wt, "ignored_junc")
            os.makedirs(parent)
            cp = types.SimpleNamespace(returncode=0, stdout="A\0ignored_junc/sub\0", stderr="")

            with (
                mock.patch("mesh.core.repos.run_git", return_value=cp),
                mock.patch.object(plat_paths, "is_reparse_stat", return_value=True),
            ):
                hits = repos_mod._ignored_paths_overwritten(wt, wt, "old", "new")
            self.assertEqual(hits, ["ignored_junc/sub"])

    def test_write_deliver_intent_uses_win32_acl_and_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            with (
                mock.patch.object(repos_mod.sys, "platform", "win32"),
                mock.patch.object(repos_mod._plat_perms, "set_private_dir_acl") as m_dir_acl,
                mock.patch.object(repos_mod._plat_perms, "set_private_file_acl") as m_file_acl,
                mock.patch.object(repos_mod._plat_fs, "atomic_replace", side_effect=os.replace) as m_replace,
            ):
                out = repos_mod.write_deliver_intent(home, {"project_id": "prj_1", "ok": True})
            self.assertTrue(os.path.isfile(out))
            m_dir_acl.assert_called_once()
            m_file_acl.assert_called_once()
            m_replace.assert_called_once()


class WorkspacesWinPortTests(unittest.TestCase):
    def test_makedirs_parent_and_fresh_branch_strip_mixed_seps_on_win32(self) -> None:
        win_ws = r"C:\mesh_ws\prj_1\tasks\tsk_1.a2/"
        win_parent = r"C:\mesh_ws\prj_1\tasks"
        plan = {"project_id": "prj_1", "task_id": "tsk_1"}
        with (
            mock.patch.object(plat_paths, "strip_trailing_seps", side_effect=plat_paths.strip_trailing_seps_win),
            mock.patch("mesh.core.paths.resolve_trusted_path", return_value=win_ws.rstrip("/\\")),
            mock.patch.object(os.path, "dirname", side_effect=lambda p: p.rsplit("\\", 1)[0]),
            mock.patch("mesh.core.workspaces.ensure_workspace_dir") as m_ensure,
        ):
            workspaces_mod._makedirs_parent(plan, win_ws)
            m_ensure.assert_called_once_with(win_parent)

        with mock.patch.object(plat_paths, "strip_trailing_seps", side_effect=plat_paths.strip_trailing_seps_win):
            self.assertEqual(workspaces_mod._fresh_branch(plan, r"C:\ws\prj\tasks\tsk_1.a2\\"), "mesh/prj_1/tsk_1-a2")
            self.assertEqual(workspaces_mod._fresh_branch(plan, r"C:\ws\prj\tasks\tsk_1.a3/"), "mesh/prj_1/tsk_1-a3")

    def test_reuse_compares_git_toplevel_with_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.realpath(os.path.join(tmp, "ws"))
            os.makedirs(ws)
            with open(os.path.join(ws, ".git"), "w", encoding="utf-8") as f:
                f.write("gitdir: ../repo/.git/worktrees/ws\n")
            plan = {"repo": os.path.join(tmp, "repo"), "task_id": "tsk_1", "project_id": "prj_1"}
            cp_top = types.SimpleNamespace(returncode=0, stdout=ws.upper() + "\n", stderr="")
            cp_branch = types.SimpleNamespace(returncode=0, stdout="refs/heads/mesh/prj_1/tsk_1\n", stderr="")

            def _git_side(_plan, _step, args, _cwd, **_kw):
                if args[:2] == ["rev-parse", "--show-toplevel"]:
                    return cp_top
                if args[:2] == ["rev-parse", "--symbolic-full-name"]:
                    return cp_branch
                raise AssertionError(args)

            with (
                mock.patch("mesh.core.workspaces._git", side_effect=_git_side),
                mock.patch("mesh.core.repos.is_registered_worktree", return_value=True),
                mock.patch.object(plat_paths, "same_path", side_effect=lambda a, b: a.lower() == b.lower()),
            ):
                got = workspaces_mod._reuse(plan, ws + "/", "mesh/prj_1/tsk_1")
            self.assertEqual(got, "mesh/prj_1/tsk_1")


class RunHandlerWinPortTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_state()

    def tearDown(self) -> None:
        _reset_daemon_state()

    def test_checkpoint_and_reparse_and_root_strip_helpers_on_win32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            tasks_dir = os.path.join(root, "prj_1", "tasks")
            ws_dir = os.path.join(tasks_dir, "tsk_1")
            os.makedirs(ws_dir)

            # Re-parse target refused in _task_workspace_entry & _remove_worktree_and_branch_with_roots
            row = {"id": "tsk_1", "project_id": "prj_1", "status": "done", "workspace_path": ws_dir + "/", "branch": "b", "base_commit": "0" * 40}
            with mock.patch.object(plat_paths, "is_reparse_link", side_effect=lambda p: p == ws_dir):
                entry = run_handler._task_workspace_entry(row, [root])
                self.assertFalse(entry["exists"])
                reason = run_handler._remove_worktree_and_branch_with_roots([root], row, root)
                self.assertEqual(reason, "outside_workspaces_root")

            # Root path should not count as non-empty stripped path
            with (
                mock.patch.object(plat_paths, "strip_trailing_seps", return_value="C:\\"),
                mock.patch.object(plat_paths, "is_fs_root", return_value=True),
            ):
                self.assertIsNone(run_handler._match_task_workspace_with_roots([root], "prj_1", "tsk_1", "C:\\"))

            # Checkpoint write + read routes through Win32 private ACL + atomic_replace + read_bytes_shared
            ckpt_path = os.path.join(root, "ws_prune", "ckpt.json")
            with (
                mock.patch.object(run_handler.sys, "platform", "win32"),
                mock.patch.object(run_handler._plat_perms, "set_private_file_acl") as m_acl,
                mock.patch.object(run_handler._plat_fs, "atomic_replace", side_effect=os.replace) as m_rep,
                mock.patch.object(run_handler._plat_fs, "read_bytes_shared", side_effect=plat_posix.read_bytes_shared) as m_rbs,
            ):
                run_handler._write_ws_prune_checkpoint(ckpt_path, {"ok": True})
                loaded = run_handler._read_ws_prune_checkpoint(ckpt_path)
            self.assertEqual(loaded, {"ok": True})
            m_acl.assert_called_once()
            m_rep.assert_called_once()
            m_rbs.assert_called_once_with(ckpt_path)

    def test_release_merge_held_closes_run_job_on_win32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            db_path = os.path.join(home, "mesh.db")
            conn = init_db(db_path)
            try:
                _seed_merge_db(conn, "run_m1", os.path.join(home, "merges", "m1"))
                fake_job = _FakeJobHandle("run_m1")
                supervisor.register_run_job("run_m1", fake_job, home=home)
                run_row = conn.execute("SELECT * FROM runs WHERE id = 'run_m1';").fetchone()
                ctx = HandlerContext(
                    home=home,
                    conn=conn,
                    principal={"id": "user", "kind": "user"},
                    actor_kind="user",
                    via="cli",
                    cmd="run.release",
                    args={"force_merge_held": True},
                    request_id="req_1",
                )
                with (
                    mock.patch.object(run_handler.sys, "platform", "win32"),
                    mock.patch.object(merge_mod, "is_merge_run_active", return_value=False),
                    mock.patch.object(run_handler, "_merge_wrapper_alive", return_value=False),
                ):
                    with transaction(conn):
                        res = run_handler._release_merge_held(ctx, run_row)
                self.assertTrue(res.data["released"])
                self.assertIsNone(supervisor.get_run_job("run_m1"))
                self.assertTrue(fake_job.closed)
            finally:
                conn.close()

    def test_workspace_delete_lstat_rejects_ntfs_junction_with_s_isdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            ws_root = os.path.join(home, "workspaces")
            ws_dir = os.path.join(ws_root, "prj_1", "tasks", "tsk_1")
            os.makedirs(ws_dir)
            reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            junc_stat = _fake_stat(stat.S_IFDIR | 0o755, file_attributes=reparse_attr)
            orig_lstat = os.lstat

            # 1. _delete_workspace_dir_impl: leaf swapped to NTFS junction right before lstat
            def _swapped_lstat_after_validate(p: str) -> Any:
                if os.path.normpath(p) == os.path.normpath(ws_dir):
                    return junc_stat
                return orig_lstat(p)

            with (
                mock.patch.object(run_handler.sys, "platform", "win32"),
                mock.patch.object(run_handler, "_validate_delete_target_ancestors", return_value=ws_dir),
                mock.patch.object(run_handler, "make_owned_ancestors_writable", return_value=False),
                mock.patch.object(run_handler.os, "lstat", side_effect=_swapped_lstat_after_validate),
                mock.patch.object(run_handler, "rmtree_allow_readonly") as m_rmtree,
            ):
                with self.assertRaises(MeshError) as cm_impl:
                    run_handler._delete_workspace_dir_impl(ws_dir, ws_dir, ws_root, "prj_1", "tsk_1", None)
            self.assertEqual(cm_impl.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm_impl.exception.details.get("reason"), "not_a_directory")
            m_rmtree.assert_not_called()

            # 2. _validate_workspace_delete: target inspected via lstat (after make_owned_ancestors_writable) is NTFS junction with S_IFDIR
            conn = init_db(os.path.join(home, "mesh.db"))
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects(id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES('prj_1', 'P', 'G', 'active', 'user', 'cli', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks(id, project_id, title, spec, kind, status, profile, timeout_s, "
                        "max_attempts, workspace_path, created_by, created_at, updated_at, version) "
                        "VALUES('tsk_1', 'prj_1', 'T', 'spec', 'code', 'done', 'dev', 60, 1, ?, 'user', ?, ?, 1);",
                        (ws_dir, now, now),
                    )
                ctx = HandlerContext(
                    home=home,
                    conn=conn,
                    principal={"id": "user", "kind": "user"},
                    actor_kind="user",
                    via="direct",
                    cmd="workspace.delete",
                    args={"task_id": "tsk_1"},
                    request_id="req_ws_del",
                )
                lstat_calls = {"n": 0}

                def _validate_lstat(p: str) -> Any:
                    if os.path.normpath(p) == os.path.normpath(ws_dir):
                        lstat_calls["n"] += 1
                        if lstat_calls["n"] == 1:
                            raise PermissionError(errno.EACCES, "unreadable tasks parent")
                        return junc_stat
                    return orig_lstat(p)

                with (
                    mock.patch.object(run_handler.sys, "platform", "win32"),
                    mock.patch.object(run_handler, "_task_workspace_match", return_value=(ws_dir, ws_dir, ws_root)),
                    mock.patch.object(run_handler, "_validate_delete_target_ancestors", return_value=ws_dir),
                    mock.patch.object(run_handler, "make_owned_ancestors_writable", return_value=True) as m_mkw,
                    mock.patch.object(run_handler.os, "lstat", side_effect=_validate_lstat),
                ):
                    with self.assertRaises(MeshError) as cm_val:
                        run_handler._validate_workspace_delete(ctx, consume_approval=False)
                self.assertEqual(lstat_calls["n"], 2)
                m_mkw.assert_called_once()
                self.assertEqual(cm_val.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(cm_val.exception.details.get("reason"), "not_a_directory")
            finally:
                conn.close()


class AdapterWinPortTests(unittest.TestCase):
    def test_vibe_redact_output_dir_removes_junctions_and_uses_platform_fs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            out_dir = os.path.join(root, "vibe-out")
            os.makedirs(out_dir)
            secret_dir = os.path.join(root, "outside_secret")
            os.makedirs(secret_dir)
            secret_file = os.path.join(secret_dir, "keep.txt")
            with open(secret_file, "w", encoding="utf-8") as f:
                f.write("SECRET")

            # Simulated empty junction subdir inside out_dir where os.unlink raises PermissionError on Win32
            junc = os.path.join(out_dir, "junc_dir")
            os.makedirs(junc)
            export_path = os.path.join(out_dir, "export.json")
            with open(export_path, "w", encoding="utf-8") as f:
                f.write('{"token": "sk-test-abcdefghijklmnopqrstuvwxyz123456"}\n')

            orig_unlink = os.unlink

            def _win_unlink(p: str) -> None:
                if os.path.normpath(p) == os.path.normpath(junc):
                    raise PermissionError(errno.EACCES, "cannot unlink dir junction")
                orig_unlink(p)

            with (
                mock.patch.object(vibe_mod.sys, "platform", "win32"),
                mock.patch.object(os, "unlink", side_effect=_win_unlink),
                mock.patch("mesh.adapters.base.open_regular_nofollow", side_effect=plat_posix.open_regular_nofollow),
                mock.patch.object(vibe_mod._plat_fs, "open_regular_nofollow", side_effect=plat_posix.open_regular_nofollow),
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.normpath(p) == os.path.normpath(junc),
                ),
                mock.patch.object(plat_paths, "is_reparse_stat", return_value=False),
                mock.patch.object(vibe_mod._plat_perms, "set_private_dir_acl") as m_dacl,
                mock.patch.object(vibe_mod._plat_perms, "chmod_private") as m_fchmod,
                mock.patch.object(vibe_mod._plat_fs, "atomic_replace", side_effect=os.replace) as m_rep,
            ):
                vibe_mod._redact_output_dir(out_dir + "\\")

            self.assertFalse(os.path.exists(junc))
            self.assertGreaterEqual(m_dacl.call_count, 1)
            self.assertGreaterEqual(m_fchmod.call_count, 1)
            m_rep.assert_called_once()
            with open(secret_file, encoding="utf-8") as sf:
                self.assertEqual(sf.read(), "SECRET")

            # Fallback path when redact/unlink fails truncates using platform open_regular_nofollow
            bad_file = os.path.join(out_dir, "ununlinkable.json")
            with open(bad_file, "w", encoding="utf-8") as f:
                f.write("secret-content")
            with (
                mock.patch.object(vibe_mod.sys, "platform", "win32"),
                mock.patch.object(vibe_mod, "open_run_file_nofollow", side_effect=OSError(errno.EACCES, "denied")),
                mock.patch.object(vibe_mod, "_unlink_nonregular", side_effect=OSError(errno.EACCES, "denied")),
                mock.patch.object(vibe_mod._plat_perms, "set_private_dir_acl"),
                mock.patch.object(vibe_mod._plat_perms, "chmod_private"),
                mock.patch.object(
                    vibe_mod._plat_fs, "open_regular_nofollow", wraps=plat_posix.open_regular_nofollow
                ) as m_trunc_open,
            ):
                vibe_mod._redact_output_dir(out_dir)
            self.assertTrue(
                any(c_args.args[0] == bad_file for c_args in m_trunc_open.call_args_list),
                m_trunc_open.call_args_list,
            )
            with open(bad_file, encoding="utf-8") as bf:
                self.assertEqual(bf.read(), "")

            # _read_export refuses reparse parent and fs root
            with mock.patch.object(
                plat_paths,
                "is_reparse_link",
                side_effect=lambda p: os.path.normpath(p) == os.path.normpath(out_dir),
            ):
                self.assertIsNone(vibe_mod._read_export(export_path))
            with (
                mock.patch.object(plat_paths, "strip_trailing_seps", return_value="C:\\"),
                mock.patch.object(plat_paths, "is_fs_root", return_value=True),
            ):
                self.assertIsNone(vibe_mod._read_export("C:\\"))

    def test_devin_skill_scan_prunes_reparse_dirs_and_uses_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            ws = os.path.join(root, "ws")
            outside = os.path.join(root, "outside_docs")
            os.makedirs(os.path.join(ws, ".git"))
            junc_skills = os.path.join(ws, ".devin", "skills", "linked_dir")
            os.makedirs(junc_skills)
            # File inside linked_dir would exceed budget / grant if traversed without pruning
            with open(os.path.join(junc_skills, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nallowed-tools: [exec]\n---\n")
            os.makedirs(os.path.join(ws, ".devin", "skills", "sub_named", "SKILL.md"))
            os.makedirs(outside)

            # Re-parse subdir must be recorded as `linked_dir` ONLY (pruned, inner SKILL.md NOT scanned)
            with (
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.basename(p) == "linked_dir",
                ),
                mock.patch.object(devin_mod._plat_fs, "open_regular_nofollow", wraps=plat_posix.open_regular_nofollow) as m_open,
            ):
                found = devin_mod._skill_grants_found(ws)
            self.assertIn(junc_skills, found)
            self.assertNotIn(os.path.join(junc_skills, "SKILL.md"), found)
            # Directory literally named SKILL.md must be refused cleanly via open_regular_nofollow raising EINVAL/EISDIR -> _skill_grants True
            self.assertIn(os.path.join(ws, ".devin", "skills", "sub_named", "SKILL.md"), found)
            self.assertGreaterEqual(m_open.call_count, 1)

            # User folder equivalence with same_path skips ~/.devin & ~/.cognition even if drive case differs
            os.makedirs(os.path.join(ws, ".cognition"))
            with (
                mock.patch.object(
                    os.path,
                    "expanduser",
                    side_effect=lambda p: os.path.join(ws.upper(), p[2:]) if p.startswith("~/") else p,
                ),
                mock.patch.object(plat_paths, "same_path", side_effect=lambda a, b: a.lower() == b.lower()),
            ):
                self.assertEqual(devin_mod._legacy_dirs_found(ws), [])
                self.assertNotIn(os.path.join(ws, ".devin", "skills", "sub_named", "SKILL.md"), devin_mod._skill_grants_found(ws))

    def test_vibe_unlink_nonregular_clears_readonly_on_win32_without_chmod_on_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            out_dir = os.path.join(root, "vibe-out")
            os.makedirs(out_dir)
            ro_file = os.path.join(out_dir, "ro_secret.json")
            with open(ro_file, "w", encoding="utf-8") as f:
                f.write('{"token": "secret"}')
            junc = os.path.join(out_dir, "junc_dir")
            os.makedirs(junc)

            writable: Set[str] = set()
            chmod_calls: List[tuple[str, int]] = []
            orig_unlink = os.unlink
            orig_chmod = os.chmod

            def _win_unlink(p: str) -> None:
                norm = os.path.normpath(p)
                if norm == os.path.normpath(junc):
                    raise PermissionError(errno.EACCES, "cannot unlink dir junction")
                if norm == os.path.normpath(ro_file) and norm not in writable:
                    raise PermissionError(errno.EACCES, "FILE_ATTRIBUTE_READONLY")
                orig_unlink(p)

            def _track_chmod(p: str, mode: int) -> None:
                chmod_calls.append((p, mode))
                if os.path.normpath(p) == os.path.normpath(ro_file) and (mode & stat.S_IWRITE):
                    writable.add(os.path.normpath(p))
                orig_chmod(p, mode)

            with (
                mock.patch.object(vibe_mod.sys, "platform", "win32"),
                mock.patch.object(os, "unlink", side_effect=_win_unlink),
                mock.patch.object(os, "chmod", side_effect=_track_chmod),
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.normpath(p) == os.path.normpath(junc),
                ),
                mock.patch.object(plat_paths, "is_reparse_stat", return_value=False),
                mock.patch.object(vibe_mod, "open_run_file_nofollow", side_effect=OSError(errno.EACCES, "denied")),
                mock.patch.object(vibe_mod._plat_perms, "set_private_dir_acl"),
                mock.patch.object(vibe_mod._plat_perms, "chmod_private"),
            ):
                vibe_mod._redact_output_dir(out_dir)

            self.assertFalse(os.path.lexists(ro_file))
            self.assertFalse(os.path.lexists(junc))
            self.assertIn((ro_file, stat.S_IWRITE | stat.S_IREAD), chmod_calls)
            self.assertFalse(any(os.path.normpath(p) == os.path.normpath(junc) for p, _m in chmod_calls))


class MergeWinPortTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_state()

    def tearDown(self) -> None:
        _reset_daemon_state()

    def test_merge_path_and_symlink_restore_guards_on_win32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            integ = os.path.join(root, "integ")
            target_dir = os.path.join(integ, "real_dir")
            os.makedirs(target_dir)

            # _restore_single_tracked_path 120000 detects directory target + formats WinError 1314
            blob_cp = types.SimpleNamespace(returncode=0, stdout=b"real_dir", stderr=b"")
            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch("mesh.daemon.merge.run_git", return_value=blob_cp),
                mock.patch("os.symlink") as m_symlink,
            ):
                merge_mod._restore_single_tracked_path(integ, integ, "HEAD", "dir_link", "120000")
                m_symlink.assert_called_once_with(
                    "real_dir",
                    os.path.join(integ, "dir_link"),
                    target_is_directory=True,
                )

            priv_err = OSError(errno.EACCES, "privilege not held")
            priv_err.winerror = 1314  # type: ignore[attr-defined]
            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch("mesh.daemon.merge.run_git", return_value=blob_cp),
                mock.patch("os.symlink", side_effect=priv_err),
            ):
                with self.assertRaises(OSError) as cm:
                    merge_mod._restore_single_tracked_path(integ, integ, "HEAD", "dir_link", "120000")
            self.assertIn("SeCreateSymbolicLinkPrivilege", str(cm.exception))
            self.assertIn("WinError 1314", str(cm.exception))

            # Regular file restore routes through open_regular_nofollow
            file_blob_cp = types.SimpleNamespace(returncode=0, stdout=b"hello\n", stderr=b"")
            with (
                mock.patch("mesh.daemon.merge.run_git", return_value=file_blob_cp),
                mock.patch.object(merge_mod._plat_fs, "open_regular_nofollow", wraps=plat_posix.open_regular_nofollow) as m_open,
            ):
                merge_mod._restore_single_tracked_path(integ, integ, "HEAD", "f.txt", "100644")
            m_open.assert_called_once()

            # _safe_unlink_untracked unlinks directory junction via rmdir fallback without touching target dir
            junc = os.path.join(integ, "junc")
            os.makedirs(junc)
            keep = os.path.join(target_dir, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("ok")
            orig_unlink = os.unlink

            def _win_unlink(p: str) -> None:
                if os.path.normpath(p) == os.path.normpath(junc):
                    raise PermissionError(errno.EACCES, "junction dir")
                orig_unlink(p)

            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch.object(os, "unlink", side_effect=_win_unlink),
                mock.patch.object(plat_paths, "is_reparse_link", side_effect=lambda p: os.path.normpath(p) == os.path.normpath(junc)),
            ):
                merge_mod._safe_unlink_untracked(integ, "junc")
            self.assertFalse(os.path.exists(junc))
            self.assertTrue(os.path.isfile(keep))

    def test_kill_test_proc_direct_uses_win32_stop_event_and_job_terminate(self) -> None:
        proc = _FakePopen(pid=8888, handle=0x4444)
        job = _FakeJobHandle("run_m1")
        proc._mesh_job = job  # type: ignore[attr-defined]

        # Exits gracefully during grace window -> TerminateJobObject is NOT called
        with (
            mock.patch.object(merge_mod.sys, "platform", "win32"),
            mock.patch.object(os, "killpg", side_effect=AssertionError("killpg called on win32"), create=True),
            mock.patch.object(os, "getpgrp", side_effect=AssertionError("getpgrp called on win32"), create=True),
            mock.patch.object(merge_mod, "_exited_unreaped", return_value=False),
            mock.patch.object(merge_mod, "_wait_exit_unreaped", return_value=True),
            mock.patch.object(merge_mod._plat_proc, "signal_if_same", return_value=True) as m_sig,
            mock.patch.object(merge_mod._plat_proc, "terminate_job") as m_term_job,
            mock.patch.object(merge_mod._plat_proc, "kill_if_same") as m_kill_same,
        ):
            merge_mod._kill_test_proc_direct(
                proc, 1.0, force_group_kill=False, run_id="run_m1", home="/h", wrapper_starttime=999
            )
        m_sig.assert_called_once_with(8888, 999, merge_mod._plat_proc.SIGTERM, run_id="run_m1", home="/h")
        m_term_job.assert_not_called()
        m_kill_same.assert_not_called()

        # Survives grace -> hard kills Named Job + kill_if_same + proc.kill fallback
        with (
            mock.patch.object(merge_mod.sys, "platform", "win32"),
            mock.patch.object(merge_mod, "_exited_unreaped", side_effect=[False, False, False]),
            mock.patch.object(merge_mod, "_wait_exit_unreaped", return_value=False),
            mock.patch.object(merge_mod._plat_proc, "signal_if_same", return_value=True),
            mock.patch.object(merge_mod._plat_proc, "terminate_job") as m_term_job,
            mock.patch.object(merge_mod._plat_proc, "kill_if_same", return_value=True) as m_kill_same,
        ):
            merge_mod._kill_test_proc_direct(
                proc, 0.1, force_group_kill=False, run_id="run_m1", home="/h", wrapper_starttime=999
            )
        m_term_job.assert_called_once_with(job, 137)
        m_kill_same.assert_called_once_with(8888, 999)
        self.assertEqual(proc.kill_calls, 1)

    def test_run_test_step_creates_and_registers_named_run_job_and_final_tx_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            db_path = os.path.join(home, "mesh.db")
            conn = init_db(db_path)
            try:
                run_dir = os.path.join(home, "merges", "mrg_1")
                integ = os.path.join(home, "ws", "integ")
                os.makedirs(integ)
                now = _seed_merge_db(conn, "run_mrg", run_dir)

                fake_job = _FakeJobHandle("run_mrg")
                fake_proc = _FakePopen(pid=6543, handle=0x5555)
                plan = {
                    "run_id": "run_mrg",
                    "run_dir": run_dir,
                    "task_id": "tsk_1",
                    "project_id": "prj_1",
                    "merge_id": "mrg_1",
                    "started_at": now,
                    "integration": integ,
                    "test_argv": [sys.executable, "-c", "pass"],
                    "test_timeout_s": 5.0,
                    "pre_head": "a" * 40,
                }

                def _fake_popen(kind: str, argv: List[str], **kw: Any) -> _FakePopen:
                    self.assertEqual(kind, "merge_test")
                    self.assertIs(kw.get("job"), fake_job)
                    with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as ef:
                        json.dump({"exit_code": 0, "signal": None}, ef)
                    fake_proc.returncode = 0
                    return fake_proc

                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch.object(merge_mod._plat_perms, "set_private_dir_acl"),
                    mock.patch.object(merge_mod._plat_perms, "set_private_file_acl"),
                    mock.patch.object(merge_mod._plat_fs, "atomic_replace", side_effect=os.replace),
                    mock.patch.object(merge_mod._plat_fs, "read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(merge_mod._plat_proc, "create_run_job", return_value=fake_job) as m_create,
                    mock.patch.object(children, "popen_registered", side_effect=_fake_popen),
                    mock.patch.object(children, "unregister"),
                    mock.patch.object(children, "unhold"),
                    mock.patch.object(merge_mod, "get_proc_stat", return_value={"starttime": 123456, "pid": 6543}),
                    mock.patch.object(terminate, "run_targets_or_recheck", return_value=ScanResult({})),
                ):
                    test_res = merge_mod.run_test_step(plan)

                m_create.assert_called_once_with(home, "run_mrg", kill_on_close=False)
                with open(os.path.join(run_dir, "spec.json"), encoding="utf-8") as sf:
                    spec_on_disk = json.load(sf)
                self.assertEqual(spec_on_disk["home"], home)
                self.assertEqual(spec_on_disk["run_id"], "run_mrg")
                self.assertTrue(test_res["passed"])
                # Named Run Job stays open across _run_test_step so close gate can inspect it, then _final_tx closes it.
                self.assertIs(supervisor.get_run_job("run_mrg"), fake_job)
                self.assertFalse(fake_job.closed)

                pipeline_res = {
                    "outcome": "merged",
                    "merge_commit": "b" * 40,
                    "pre_head": "a" * 40,
                    "noop": False,
                    "tests": "passed",
                    "test_log": test_res["log_path"],
                    "test": test_res,
                }
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch.object(merge_mod, "_check_live_merge_close"),
                ):
                    with transaction(conn):
                        merge_mod._final_tx(conn, plan, pipeline_res)
                self.assertIsNone(supervisor.get_run_job("run_mrg"))
                self.assertTrue(fake_job.closed)
            finally:
                conn.close()

    def test_run_test_step_win32_survives_concurrent_watch_launch_runs(self) -> None:
        _reset_daemon_state()
        self.addCleanup(_reset_daemon_state)
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            conn = init_db(os.path.join(home, "mesh.db"))
            conn_empty = init_db(os.path.join(home, "empty.db"))
            close_calls: List[int] = []
            fake_k32 = types.SimpleNamespace(CloseHandle=lambda h: (close_calls.append(int(h)) or 1))
            real_job = plat_win32.JobHandle(
                1234, name=r"Local\mesh-job-run_mrg_race", ephemeral=False, k32=fake_k32
            )
            try:
                run_dir = os.path.join(home, "merges", "mrg_race")
                integ = os.path.join(home, "ws", "integ")
                os.makedirs(integ)
                now = _seed_merge_db(conn, "run_mrg_race", run_dir)

                fake_proc = _FakePopen(pid=6544, handle=0x5556)
                plan = {
                    "run_id": "run_mrg_race",
                    "run_dir": run_dir,
                    "task_id": "tsk_1",
                    "project_id": "prj_1",
                    "merge_id": "mrg_1",
                    "started_at": now,
                    "integration": integ,
                    "test_argv": [sys.executable, "-c", "pass"],
                    "test_timeout_s": 5.0,
                    "pre_head": "a" * 40,
                }

                def _fake_popen_raced(kind: str, argv: List[str], **kw: Any) -> _FakePopen:
                    self.assertEqual(kind, "merge_test")
                    self.assertIs(kw.get("job"), real_job)
                    # Simulate watch_launch_runs firing between register_run_job() and spawn_in_job_win(),
                    # both with the real DB and with an empty DB before a merge_test row is visible.
                    supervisor.watch_launch_runs(conn, home, testing_mode=True)
                    supervisor.watch_launch_runs(conn_empty, home, testing_mode=True)
                    h_job, _, _, _ = plat_win32._resolve_job_deps(
                        kw["job"], k32=fake_k32, get_last_error_fn=lambda: 0
                    )
                    self.assertEqual(h_job, 1234)
                    self.assertIsNotNone(real_job.handle)
                    with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as ef:
                        json.dump({"exit_code": 0, "signal": None}, ef)
                    fake_proc.returncode = 0
                    return fake_proc

                with (
                    mock.patch.object(sys, "platform", "win32"),
                    mock.patch.object(merge_mod._plat_perms, "set_private_dir_acl"),
                    mock.patch.object(merge_mod._plat_perms, "set_private_file_acl"),
                    mock.patch.object(merge_mod._plat_fs, "atomic_replace", side_effect=os.replace),
                    mock.patch.object(
                        merge_mod._plat_fs, "read_bytes_shared", side_effect=plat_posix.read_bytes_shared
                    ),
                    mock.patch.object(merge_mod._plat_proc, "create_run_job", return_value=real_job),
                    mock.patch.object(children, "popen_registered", side_effect=_fake_popen_raced),
                    mock.patch.object(children, "unregister"),
                    mock.patch.object(children, "unhold"),
                    mock.patch.object(merge_mod, "get_proc_stat", return_value={"starttime": 123456, "pid": 6544}),
                    mock.patch.object(terminate, "run_targets_or_recheck", return_value=ScanResult({})),
                ):
                    test_res = merge_mod.run_test_step(plan)
                    self.assertTrue(test_res["passed"])
                    self.assertNotIn("run_mrg_race", merge_mod._ACTIVE_TEST_JOB_RUNS)
                    # Between run_test_step() and _final_tx(), the open merge_test DB row still protects it.
                    supervisor.watch_launch_runs(conn, home, testing_mode=True)
                    self.assertIs(supervisor.get_run_job("run_mrg_race"), real_job)
                    self.assertEqual(real_job.handle, 1234)

                pipeline_res = {
                    "outcome": "merged",
                    "merge_commit": "b" * 40,
                    "pre_head": "a" * 40,
                    "noop": False,
                    "tests": "passed",
                    "test_log": test_res["log_path"],
                    "test": test_res,
                }
                with (
                    mock.patch.object(sys, "platform", "win32"),
                    mock.patch.object(merge_mod, "_check_live_merge_close"),
                ):
                    with transaction(conn):
                        merge_mod._final_tx(conn, plan, pipeline_res)
                self.assertIsNone(supervisor.get_run_job("run_mrg_race"))
                self.assertIsNone(real_job.handle)
                self.assertEqual(close_calls, [1234])
            finally:
                real_job.close()
                conn_empty.close()
                conn.close()

    def test_recover_merge_runs_and_close_gate_adopt_win32_job_and_exit_members(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            db_path = os.path.join(home, "mesh.db")
            conn = init_db(db_path)
            try:
                now = now_iso_utc()
                run_dir = os.path.join(home, "merges", "mrg_rec")
                os.makedirs(run_dir)
                with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as ef:
                    json.dump(
                        {
                            "exit_code": 0,
                            "signal": None,
                            "job_members_at_exit": [{"pid": 9001, "starttime": 777}],
                        },
                        ef,
                    )
                _seed_merge_db(
                    conn,
                    "run_rec",
                    run_dir,
                    wrapper_pid=5000,
                    pgid=5000,
                    proc_starttime=111,
                    with_lease=True,
                )
                run_row = conn.execute("SELECT * FROM runs WHERE id = 'run_rec';").fetchone()
                adopted_job = _FakeJobHandle("run_rec")

                # 1. merge_close_gate holds if Win32 exit.json job_members_at_exit is live
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(supervisor, "_identity_live", side_effect=lambda p, s: (p, s) == (9001, 777)),
                ):
                    with self.assertRaises(merge_mod.MergeRunHeld):
                        merge_mod.merge_close_gate(conn, run_row, None, None)

                # 2. merge_close_gate holds via Win32 Named Run Job even when wrapper PID was reused
                adopted_job.pids = {9002}
                supervisor.register_run_job("run_rec", adopted_job, home=home)
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(supervisor, "_identity_live", return_value=False),
                    mock.patch.object(supervisor, "group_still_ours", return_value=False),
                    mock.patch.object(merge_mod._plat_proc, "query_job_pids", return_value=[9002]),
                ):
                    with self.assertRaises(merge_mod.MergeRunHeld):
                        merge_mod.merge_close_gate(conn, run_row, 5000, 111)
                _reset_daemon_state()

                # 3. recover_merge_runs opens Named Job, folds live exit member, steps termination, and closes job
                fresh_job = _FakeJobHandle("run_rec")
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(merge_mod, "_live_wrapper", return_value=(None, None)),
                    mock.patch.object(merge_mod._plat_proc, "open_run_job", return_value=fresh_job) as m_open,
                    mock.patch.object(supervisor, "_identity_live", side_effect=lambda p, s: (p, s) == (9001, 777)),
                    mock.patch.object(terminate, "run_targets_or_recheck", return_value=ScanResult({})),
                    mock.patch.object(terminate, "begin", return_value=types.SimpleNamespace(initial_pids={9001})) as m_begin,
                    mock.patch.object(merge_mod, "_step_terminations", return_value={"run_rec": {"targets": 1}}),
                    mock.patch.object(merge_mod, "merge_close_gate"),
                ):
                    out = merge_mod.recover_merge_runs(conn, [run_row])

                self.assertEqual(out["run_rec"], "killed")
                m_open.assert_called_once_with(home, "run_rec", terminate=True)
                self.assertEqual(m_begin.call_args.kwargs["seen"], {9001: 777})
                self.assertTrue(fresh_job.closed)
                self.assertIsNone(supervisor.get_run_job("run_rec"))
            finally:
                conn.close()

    def test_safe_unlink_untracked_preserves_posix_backslash_and_splits_win32_seps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            integ = os.path.realpath(tmp)
            if sys.platform != "win32":
                lit_bs = os.path.join(integ, "foo\\")
                plain_foo = os.path.join(integ, "foo")
                with open(lit_bs, "w", encoding="utf-8") as f:
                    f.write("bs")
                with open(plain_foo, "w", encoding="utf-8") as f:
                    f.write("plain")
                merge_mod._safe_unlink_untracked(integ, "foo\\")
                self.assertFalse(os.path.lexists(lit_bs))
                self.assertTrue(os.path.isfile(plain_foo))

            junc_dir = os.path.join(integ, "junc")
            os.makedirs(junc_dir)
            junc_secret = os.path.join(junc_dir, "secret.txt")
            with open(junc_secret, "w", encoding="utf-8") as f:
                f.write("KEEP")
            escape_file = os.path.join(integ, "escape.txt")
            with open(escape_file, "w", encoding="utf-8") as f:
                f.write("KEEP")
            sub_dir = os.path.join(integ, "sub")
            os.makedirs(sub_dir)
            sub_clean = os.path.join(sub_dir, "clean.txt")
            with open(sub_clean, "w", encoding="utf-8") as f:
                f.write("DROP")

            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.normpath(p) == os.path.normpath(junc_dir),
                ),
            ):
                merge_mod._safe_unlink_untracked(integ, r"junc\secret.txt")
                self.assertTrue(os.path.isfile(junc_secret))

                merge_mod._safe_unlink_untracked(integ, r"a\..\escape.txt")
                self.assertTrue(os.path.isfile(escape_file))

                with mock.patch.object(
                    merge_mod, "_unlink_nonregular", side_effect=AssertionError("should not unlink")
                ):
                    merge_mod._safe_unlink_untracked(integ, "C:foo")
                    merge_mod._safe_unlink_untracked(integ, r"\foo")

                merge_mod._safe_unlink_untracked(integ, r"sub\clean.txt")
                self.assertFalse(os.path.lexists(sub_clean))
                self.assertFalse(os.path.lexists(sub_dir))

    def test_merge_unlink_nonregular_clears_readonly_on_win32_and_skips_reparse_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            integ = os.path.realpath(tmp)
            ro_file = os.path.join(integ, "ro.txt")
            with open(ro_file, "w", encoding="utf-8") as f:
                f.write("ro")
            junc = os.path.join(integ, "junc")
            os.makedirs(junc)

            writable: Set[str] = set()
            chmod_calls: List[tuple[str, int]] = []
            orig_unlink = os.unlink
            orig_chmod = os.chmod

            def _win_unlink(p: str) -> None:
                norm = os.path.normpath(p)
                if norm == os.path.normpath(junc):
                    raise PermissionError(errno.EACCES, "junction dir")
                if norm == os.path.normpath(ro_file) and norm not in writable:
                    raise PermissionError(errno.EACCES, "FILE_ATTRIBUTE_READONLY")
                orig_unlink(p)

            def _track_chmod(p: str, mode: int) -> None:
                chmod_calls.append((p, mode))
                if os.path.normpath(p) == os.path.normpath(ro_file) and (mode & stat.S_IWRITE):
                    writable.add(os.path.normpath(p))
                orig_chmod(p, mode)

            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch.object(os, "unlink", side_effect=_win_unlink),
                mock.patch.object(os, "chmod", side_effect=_track_chmod),
                mock.patch.object(
                    plat_paths,
                    "is_reparse_link",
                    side_effect=lambda p: os.path.normpath(p) == os.path.normpath(junc),
                ),
            ):
                merge_mod._safe_unlink_untracked(integ, "ro.txt")
                merge_mod._safe_unlink_untracked(integ, "junc")

            self.assertFalse(os.path.lexists(ro_file))
            self.assertFalse(os.path.lexists(junc))
            self.assertIn((ro_file, stat.S_IWRITE | stat.S_IREAD), chmod_calls)
            self.assertFalse(any(os.path.normpath(p) == os.path.normpath(junc) for p, _m in chmod_calls))

    def test_kill_test_proc_direct_without_wrapper_starttime(self) -> None:
        proc1 = _FakePopen(pid=8889, handle=0x4445)
        job1 = _FakeJobHandle("run_m1")
        proc1._mesh_job = job1  # type: ignore[attr-defined]
        with (
            mock.patch.object(merge_mod.sys, "platform", "win32"),
            mock.patch.object(merge_mod, "get_proc_stat", side_effect=OSError(errno.EACCES, "denied")),
            mock.patch.object(merge_mod, "_exited_unreaped", return_value=False),
            mock.patch.object(merge_mod, "_wait_exit_unreaped", return_value=True) as m_wait,
            mock.patch.object(merge_mod._plat_proc, "signal_run_stop_event", return_value=True) as m_stop,
            mock.patch.object(merge_mod._plat_proc, "terminate_job") as m_term_job,
        ):
            merge_mod._kill_test_proc_direct(
                proc1, 1.5, force_group_kill=False, run_id="run_m1", home="/h", wrapper_starttime=None
            )
        m_stop.assert_called_once_with("/h", "run_m1")
        m_wait.assert_called_once_with(proc1, 1.5)
        m_term_job.assert_not_called()

        proc2 = _FakePopen(pid=8890, handle=0x4446)
        job2 = _FakeJobHandle("run_m2")
        proc2._mesh_job = job2  # type: ignore[attr-defined]
        with (
            mock.patch.object(merge_mod.sys, "platform", "win32"),
            mock.patch.object(merge_mod, "get_proc_stat", side_effect=OSError(errno.EACCES, "denied")),
            mock.patch.object(merge_mod, "_exited_unreaped", return_value=False),
            mock.patch.object(merge_mod, "_wait_exit_unreaped", return_value=True) as m_wait2,
            mock.patch.object(merge_mod._plat_proc, "signal_run_stop_event", return_value=False) as m_stop2,
            mock.patch.object(merge_mod._plat_proc, "terminate_job") as m_term_job2,
        ):
            merge_mod._kill_test_proc_direct(
                proc2, 5.0, force_group_kill=False, run_id="run_m2", home="/h", wrapper_starttime=None
            )
        m_stop2.assert_called_once_with("/h", "run_m2")
        m_wait2.assert_called_once_with(proc2, terminate.KILL_WAIT_S)
        m_term_job2.assert_called_once_with(job2, 137)
        self.assertEqual(proc2.kill_calls, 1)

    def test_merge_close_gate_preserves_db_wrapper_identity_on_win32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            conn = init_db(os.path.join(home, "mesh.db"))
            try:
                run_dir_job = os.path.join(home, "merges", "mrg_job")
                os.makedirs(run_dir_job)
                _seed_merge_db(
                    conn,
                    "run_job_only",
                    run_dir_job,
                    wrapper_pid=None,
                    pgid=None,
                    proc_starttime=None,
                    with_lease=True,
                )
                row_job = conn.execute("SELECT * FROM runs WHERE id = 'run_job_only';").fetchone()
                adopted_job = _FakeJobHandle("run_job_only")
                adopted_job.pids = {9002}
                supervisor.register_run_job("run_job_only", adopted_job, home=home)
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(supervisor, "_identity_live", return_value=False),
                    mock.patch.object(supervisor, "group_still_ours", return_value=False),
                    mock.patch.object(merge_mod._plat_proc, "query_job_pids", return_value=[9002]),
                ):
                    with self.assertRaises(merge_mod.MergeRunHeld) as cm_job:
                        merge_mod.merge_close_gate(conn, row_job, None, None)
                self.assertEqual(cm_job.exception.pgid, 9002)
                updated_job = conn.execute(
                    "SELECT * FROM runs WHERE id = 'run_job_only';"
                ).fetchone()
                self.assertIsNone(updated_job["wrapper_pid"])
                self.assertIsNone(updated_job["pgid"])
                self.assertIsNone(updated_job["proc_starttime"])
                with mock.patch("mesh.core.proc.get_proc_stat", side_effect=AssertionError("should not probe proc")):
                    self.assertFalse(run_handler._merge_wrapper_alive(updated_job))

                # 2. exit.json live member with wrapper.json present and DB columns NULL persists wrapper identity
                run_dir_exit = os.path.join(home, "merges", "mrg_exit")
                os.makedirs(run_dir_exit)
                with open(os.path.join(run_dir_exit, "wrapper.json"), "w", encoding="utf-8") as wf:
                    json.dump({"pid": 5000, "starttime": 111}, wf)
                with open(os.path.join(run_dir_exit, "exit.json"), "w", encoding="utf-8") as ef:
                    json.dump({"exit_code": 0, "signal": None, "job_members_at_exit": [{"pid": 9001, "starttime": 777}]}, ef)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO runs(id, kind, task_id, project_id, fence, adapter, status, boot_id, "
                        "wrapper_pid, pgid, proc_starttime, run_dir) "
                        "VALUES('run_exit_wrap', 'merge_test', 'tsk_1', 'prj_1', 2, 'merge_test', 'running', 'boot_1', NULL, NULL, NULL, ?);",
                        (run_dir_exit,),
                    )
                row_exit = conn.execute("SELECT * FROM runs WHERE id = 'run_exit_wrap';").fetchone()
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(supervisor, "_identity_live", side_effect=lambda p, s: (p, s) == (9001, 777)),
                ):
                    with self.assertRaises(merge_mod.MergeRunHeld) as cm_exit:
                        merge_mod.merge_close_gate(conn, row_exit, None, None)
                self.assertEqual(cm_exit.exception.pgid, 5000)
                updated_exit = conn.execute(
                    "SELECT wrapper_pid, pgid, proc_starttime FROM runs WHERE id = 'run_exit_wrap';"
                ).fetchone()
                self.assertEqual(updated_exit["wrapper_pid"], 5000)
                self.assertEqual(updated_exit["pgid"], 5000)
                self.assertEqual(updated_exit["proc_starttime"], 111)

                # 3. exit.json live member without wrapper.json leaves DB wrapper_pid NULL
                run_dir_nowr = os.path.join(home, "merges", "mrg_nowr")
                os.makedirs(run_dir_nowr)
                with open(os.path.join(run_dir_nowr, "exit.json"), "w", encoding="utf-8") as ef:
                    json.dump({"exit_code": 0, "signal": None, "job_members_at_exit": [{"pid": 9003, "starttime": 888}]}, ef)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO runs(id, kind, task_id, project_id, fence, adapter, status, boot_id, "
                        "wrapper_pid, pgid, proc_starttime, run_dir) "
                        "VALUES('run_exit_nowr', 'merge_test', 'tsk_1', 'prj_1', 3, 'merge_test', 'running', 'boot_1', NULL, NULL, NULL, ?);",
                        (run_dir_nowr,),
                    )
                row_nowr = conn.execute("SELECT * FROM runs WHERE id = 'run_exit_nowr';").fetchone()
                with (
                    mock.patch.object(merge_mod.sys, "platform", "win32"),
                    mock.patch("mesh.platform.fs.read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                    mock.patch.object(supervisor, "_identity_live", side_effect=lambda p, s: (p, s) == (9003, 888)),
                ):
                    with self.assertRaises(merge_mod.MergeRunHeld) as cm_nowr:
                        merge_mod.merge_close_gate(conn, row_nowr, None, None)
                self.assertEqual(cm_nowr.exception.pgid, 9003)
                updated_nowr = conn.execute(
                    "SELECT wrapper_pid, pgid, proc_starttime FROM runs WHERE id = 'run_exit_nowr';"
                ).fetchone()
                self.assertIsNone(updated_nowr["wrapper_pid"])
                self.assertIsNone(updated_nowr["pgid"])
                self.assertIsNone(updated_nowr["proc_starttime"])
            finally:
                conn.close()


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class RealWindowsPhase7aTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_daemon_state()

    def tearDown(self) -> None:
        _reset_daemon_state()

    def _create_junction(self, link_path: str, target_path: str) -> None:
        os.makedirs(target_path, exist_ok=True)
        cp = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target_path],
            capture_output=True,
            text=True,
        )
        if cp.returncode != 0:
            self.skipTest(f"mklink /J unavailable: {cp.stderr or cp.stdout}")

    def test_real_ntfs_junctions_blocked_across_vibe_devin_merge_and_run_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            secret_dir = os.path.join(root, "secret_target")
            os.makedirs(secret_dir)
            secret_file = os.path.join(secret_dir, "SKILL.md")
            with open(secret_file, "w", encoding="utf-8") as f:
                f.write("---\nallowed-tools: [exec]\n---\nKEEP\n")

            # 1. merge._safe_unlink_untracked deletes junction without touching secret_target contents
            integ = os.path.join(root, "integ")
            os.makedirs(integ)
            junc_merge = os.path.join(integ, "junc")
            self._create_junction(junc_merge, secret_dir)
            self.assertTrue(plat_paths.is_reparse_link(junc_merge))
            merge_mod._safe_unlink_untracked(integ, r"junc\SKILL.md")
            self.assertTrue(os.path.isfile(secret_file))
            merge_mod._safe_unlink_untracked(integ, "junc")
            self.assertFalse(os.path.lexists(junc_merge))
            self.assertTrue(os.path.isfile(secret_file))

            # 2. vibe._redact_output_dir unlinks top-level / nested junction without wiping target
            vibe_out = os.path.join(root, "vibe-out")
            os.makedirs(vibe_out)
            junc_vibe = os.path.join(vibe_out, "nested_junc")
            self._create_junction(junc_vibe, secret_dir)
            vibe_mod._redact_output_dir(vibe_out)
            self.assertFalse(os.path.lexists(junc_vibe))
            with open(secret_file, encoding="utf-8") as sf:
                self.assertIn("KEEP", sf.read())

            # 3. devin._skill_grants_found records junction without walking secret_file inside it
            ws = os.path.join(root, "devin_ws")
            os.makedirs(os.path.join(ws, ".git"))
            junc_devin = os.path.join(ws, ".devin", "skills", "junc")
            os.makedirs(os.path.dirname(junc_devin))
            self._create_junction(junc_devin, secret_dir)
            found = devin_mod._skill_grants_found(ws)
            self.assertIn(junc_devin, found)
            self.assertNotIn(os.path.join(junc_devin, "SKILL.md"), found)

            # 4. run handler rejects NTFS junction task workspace (including lstat S_ISDIR guard)
            ws_root = os.path.join(root, "workspaces")
            junc_task = os.path.join(ws_root, "prj_1", "tasks", "tsk_1")
            os.makedirs(os.path.dirname(junc_task))
            self._create_junction(junc_task, secret_dir)
            self.assertEqual(
                run_handler._remove_worktree_and_branch_with_roots(
                    [ws_root],
                    {"id": "tsk_1", "project_id": "prj_1", "workspace_path": junc_task, "branch": "b"},
                    integ,
                ),
                "outside_workspaces_root",
            )
            with mock.patch.object(run_handler, "_validate_delete_target_ancestors", return_value=junc_task):
                with self.assertRaises(MeshError) as cm_impl:
                    run_handler._delete_workspace_dir_impl(junc_task, junc_task, ws_root, "prj_1", "tsk_1", None)
                self.assertEqual(cm_impl.exception.details.get("reason"), "not_a_directory")
            self.assertTrue(os.path.isfile(secret_file))

            conn = init_db(os.path.join(root, "mesh.db"))
            try:
                now = now_iso_utc()
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO projects(id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES('prj_1', 'P', 'G', 'active', 'user', 'cli', ?, ?);",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks(id, project_id, title, spec, kind, status, profile, timeout_s, "
                        "max_attempts, workspace_path, created_by, created_at, updated_at, version) "
                        "VALUES('tsk_1', 'prj_1', 'T', 'spec', 'code', 'done', 'dev', 60, 1, ?, 'user', ?, ?, 1);",
                        (junc_task, now, now),
                    )
                ctx = HandlerContext(
                    home=root,
                    conn=conn,
                    principal={"id": "user", "kind": "user"},
                    actor_kind="user",
                    via="direct",
                    cmd="workspace.delete",
                    args={"task_id": "tsk_1"},
                    request_id="req_real_del",
                )
                with (
                    mock.patch.object(run_handler, "_task_workspace_match", return_value=(junc_task, junc_task, ws_root)),
                    mock.patch.object(run_handler, "_validate_delete_target_ancestors", return_value=junc_task),
                ):
                    with self.assertRaises(MeshError) as cm_val:
                        run_handler._validate_workspace_delete(ctx, consume_approval=False)
                    self.assertEqual(cm_val.exception.details.get("reason"), "not_a_directory")
            finally:
                conn.close()

    def test_real_win32_readonly_unlink_backslash_split_and_stop_event(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            integ = os.path.join(root, "integ")
            os.makedirs(integ)
            ro_merge = os.path.join(integ, "ro_merge.txt")
            with open(ro_merge, "w", encoding="utf-8") as f:
                f.write("ro")
            os.chmod(ro_merge, stat.S_IREAD)
            merge_mod._safe_unlink_untracked(integ, "ro_merge.txt")
            self.assertFalse(os.path.lexists(ro_merge))

            sub_dir = os.path.join(integ, "sub")
            os.makedirs(sub_dir)
            leaf = os.path.join(sub_dir, "leaf.txt")
            with open(leaf, "w", encoding="utf-8") as f:
                f.write("leaf")
            merge_mod._safe_unlink_untracked(integ, r"sub\leaf.txt")
            self.assertFalse(os.path.lexists(leaf))

            keep = os.path.join(integ, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("keep")
            merge_mod._safe_unlink_untracked(integ, r"a\..\keep.txt")
            merge_mod._safe_unlink_untracked(integ, "C:keep.txt")
            self.assertTrue(os.path.isfile(keep))

            vibe_out = os.path.join(root, "vibe-out")
            os.makedirs(vibe_out)
            ro_vibe = os.path.join(vibe_out, "ro_vibe.json")
            with open(ro_vibe, "w", encoding="utf-8") as f:
                f.write("{}")
            os.chmod(ro_vibe, stat.S_IREAD)
            vibe_mod._unlink_nonregular(ro_vibe)
            self.assertFalse(os.path.lexists(ro_vibe))

            ev = plat_proc.create_stop_event(root, "run_real_stop")
            try:
                self.assertTrue(plat_proc.signal_run_stop_event(root, "run_real_stop"))
                self.assertEqual(w32.get_kernel32().WaitForSingleObject(int(ev.fileno()), 0), 0)
            finally:
                ev.close()
            self.assertFalse(plat_proc.signal_run_stop_event(root, "run_missing_stop"))

    def test_real_win32_exited_unreaped_and_named_run_job_step(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = __import__("time").monotonic() + 5.0
            while not merge_mod._exited_unreaped(proc) and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
            self.assertTrue(merge_mod._exited_unreaped(proc))
            self.assertIsNone(proc.returncode)
            self.assertNotEqual(int(proc._handle), 0)
            self.assertEqual(proc.wait(timeout=2.0), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            run_dir = os.path.join(home, "merges", "mrg_real")
            integ = os.path.join(home, "integ")
            os.makedirs(integ)
            plan = {
                "run_id": "run_real_mrg",
                "run_dir": run_dir,
                "task_id": "tsk_r",
                "project_id": "prj_r",
                "merge_id": "mrg_real",
                "integration": integ,
                "test_argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                "test_timeout_s": 0.4,
                "kill_grace_s": 0.3,
            }
            res = merge_mod.run_test_step(plan)
            try:
                self.assertTrue(res["timeout"])
                self.assertFalse(res["passed"])
            finally:
                supervisor.close_run_job("run_real_mrg")


class TestFinalReviewPhase7aFixes(unittest.TestCase):
    """Regression tests for R13 P1-3 and P3-3."""

    def test_p1_3_run_test_step_normalizes_signal_exit_code_win32_and_linux(self) -> None:
        for raw_code, exp_sig in ((137, 9), (143, 15)):
            for target_plat in ("win32", "linux"):
                with tempfile.TemporaryDirectory() as tmp:
                    home = os.path.realpath(tmp)
                    conn = init_db(os.path.join(home, "mesh.db"))
                    try:
                        run_id = f"run_p13_{target_plat}_{raw_code}"
                        run_dir = os.path.join(home, "merges", f"mrg_{target_plat}_{raw_code}")
                        integ = os.path.join(home, "ws", "integ")
                        os.makedirs(integ)
                        now = _seed_merge_db(conn, run_id, run_dir)
                        fake_job = _FakeJobHandle(run_id)
                        fake_proc = _FakePopen(pid=6543, handle=0x5555)

                        def _fake_popen(kind: str, argv: List[str], _rc: int = raw_code, **kw: Any) -> _FakePopen:
                            with open(os.path.join(run_dir, "exit.json"), "w", encoding="utf-8") as ef:
                                json.dump({"exit_code": _rc, "signal": None}, ef)
                            fake_proc.returncode = _rc
                            return fake_proc

                        plan = {
                            "run_id": run_id,
                            "run_dir": run_dir,
                            "task_id": "tsk_1",
                            "project_id": "prj_1",
                            "merge_id": "mrg_1",
                            "started_at": now,
                            "integration": integ,
                            "test_argv": [sys.executable, "-c", "pass"],
                            "test_timeout_s": 5.0,
                            "pre_head": "a" * 40,
                        }
                        try:
                            with (
                                mock.patch.object(merge_mod.sys, "platform", target_plat),
                                mock.patch.object(merge_mod._plat_perms, "set_private_dir_acl"),
                                mock.patch.object(merge_mod._plat_perms, "set_private_file_acl"),
                                mock.patch.object(merge_mod._plat_fs, "atomic_replace", side_effect=os.replace),
                                mock.patch.object(merge_mod._plat_fs, "read_bytes_shared", side_effect=plat_posix.read_bytes_shared),
                                mock.patch.object(merge_mod._plat_proc, "create_run_job", return_value=fake_job),
                                mock.patch.object(children, "popen_registered", side_effect=_fake_popen),
                                mock.patch.object(children, "unregister"),
                                mock.patch.object(children, "unhold"),
                                mock.patch.object(merge_mod, "get_proc_stat", return_value={"starttime": 123456, "pid": 6543}),
                                mock.patch.object(terminate, "run_targets_or_recheck", return_value=ScanResult({})),
                            ):
                                res = merge_mod.run_test_step(plan)
                        finally:
                            supervisor.close_run_job(run_id)

                        self.assertFalse(res["passed"])
                        if target_plat == "win32":
                            self.assertIsNone(res["exit_code"])
                            self.assertEqual(res["signal"], exp_sig)
                        else:
                            self.assertEqual(res["exit_code"], raw_code)
                            self.assertIsNone(res["signal"])
                    finally:
                        conn.close()

    def test_p3_3_restore_head_and_ref_preserves_linux_crlf_and_win32_read_bytes_shared(self) -> None:
        pre_head = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            integ = os.path.realpath(tmp)
            head_path = os.path.join(integ, "HEAD")
            with open(head_path, "wb") as hf:
                hf.write(b"ref: refs/heads/main\r\n")

            if sys.platform != "win32":
                with (
                    mock.patch.object(merge_mod, "_git_path", return_value=head_path),
                    mock.patch.object(merge_mod, "_head_symref", return_value="refs/heads/main"),
                    mock.patch.object(merge_mod, "_head", return_value=pre_head),
                    mock.patch.object(merge_mod._plat_paths, "is_reparse_link", return_value=False),
                    mock.patch.object(merge_mod._plat_fs, "read_bytes_shared") as m_shared,
                    mock.patch.object(merge_mod, "_atomic_write_bytes") as m_write,
                ):
                    ok = merge_mod._restore_head_and_ref(
                        integ, pre_head, pre_symref="refs/heads/main"
                    )
                self.assertTrue(ok)
                m_shared.assert_not_called()
                m_write.assert_not_called()

            def _read_raw(p: str) -> bytes:
                with open(p, "rb") as fp:
                    return fp.read()

            # Under mocked win32, _restore_head_and_ref reads raw bytes via read_bytes_shared
            with (
                mock.patch.object(merge_mod.sys, "platform", "win32"),
                mock.patch.object(merge_mod, "_git_path", return_value=head_path),
                mock.patch.object(merge_mod, "_head_symref", return_value="refs/heads/main"),
                mock.patch.object(merge_mod, "_head", return_value=pre_head),
                mock.patch.object(merge_mod._plat_paths, "is_reparse_link", return_value=False),
                mock.patch.object(
                    merge_mod._plat_fs,
                    "read_bytes_shared",
                    side_effect=_read_raw,
                ) as m_shared_win,
                mock.patch.object(merge_mod, "_atomic_write_bytes") as m_write_win,
            ):
                ok_win = merge_mod._restore_head_and_ref(
                    integ, pre_head, pre_symref="refs/heads/main"
                )
            self.assertTrue(ok_win)
            m_shared_win.assert_called_once_with(head_path)
            m_write_win.assert_called_once_with(
                head_path, b"ref: refs/heads/main\n", mode=0o644
            )

    def test_proc_posix_mod_resolves_lazily_when_none(self) -> None:
        fake_proc = _FakePopen(pid=6544, handle=0x5556)
        fake_proc.returncode = 0
        with mock.patch.object(merge_mod._plat_proc, "_posix", None), \
                mock.patch.object(merge_mod._plat_proc.sys, "platform", "linux"):
            self.assertTrue(merge_mod._plat_proc.is_popen_exited_unreaped(fake_proc))


if __name__ == "__main__":
    unittest.main()
