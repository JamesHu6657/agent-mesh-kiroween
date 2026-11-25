"""Full review r5 (Gemini r5 on a3f0c4e): redaction depth/fail-closed/control escapes,
worktree ownership, adapter/vibe redaction, deliver intent reads, offered_at, op_json."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

MESH_PROC_LOCK = "shared"

CROSS = ("CROSS", 'foo", "bar')
PW = "my_super_secret_pw"


def _nest(d: int) -> str:
    s = '{"password":"hunter2-secret"}'
    for _ in range(d):
        s = '["' + s.replace("\\", "\\u005c").replace('"', "\\u0022") + '"]'
    return s


class TestR5Redact(unittest.TestCase):
    def test_p0_1_nested_json_strings_are_bounded(self) -> None:
        from mesh.core.redact import redact, LINE_CAP

        # GitHub windows-latest runners (2-4 vCPUs, MESH_TEST_JOBS=6) can hit ~2.3s on
        # d=110 vs ~0.5s idle on Linux; 6.0s still proves O(depth)-bounded runtime.
        bound_s = 6.0 if sys.platform == "win32" else 2.0
        for d in (14, 21, 49, 110):
            line = _nest(d)
            self.assertLess(len(line), LINE_CAP)
            t = time.monotonic()
            out = redact(line, [], jsonl=True)
            self.assertLess(time.monotonic() - t, bound_s, d)
            self.assertLessEqual(len(out), LINE_CAP)
            self.assertNotIn("hunter2", out)
            json.loads(out)
            self.assertEqual(redact(out, [], jsonl=True), out)  # idempotent

    def test_p0_1_shallow_nesting_still_redacts_inside(self) -> None:
        from mesh.core.redact import redact

        out = redact(_nest(2), [], jsonl=True)
        self.assertNotIn("hunter2", out)
        self.assertNotEqual(out, '"[REDACTED]"')  # kept as the event, not dropped

    def test_p1_1_doubt_never_falls_back_to_line_rules(self) -> None:
        from mesh.core.redact import redact

        lines = [
            '{"cross": ["foo", "bar"], "arr": ["password:", "%s"]}' % PW,
            '{"cross": ["foo", "bar"], "password_' + "x" * 70 + '": "%s"}' % PW,
            '{"cross": ["foo", "bar"], "credentials": {"user": "%s"}}' % PW,
        ]
        for line in lines:
            out = redact(line, [CROSS], jsonl=True)
            self.assertNotIn(PW, out)
            json.loads(out)
        self.assertNotIn(PW, redact('password:\n{"cross": ["foo", "bar"], "v": "%s"}' % PW, [CROSS], jsonl=True))
        self.assertNotIn(PW, redact('{"cross": ["foo", "bar"], "prompt": "password:"}\n%s\n' % PW,
                                    [CROSS], jsonl=True))

    def test_p1_2_control_escapes_match_real_characters(self) -> None:
        from mesh.core.redact import redact

        out = redact('INFO payload={"hdr": "abc1234\\ndef5678"}', [("K", "abc1234\ndef5678")], jsonl=True)
        self.assertNotIn("def5678", out)
        line = '{"type": "tool_result", "content": "resp={\\"hdr\\": \\"session_id\\\\tsecret999\\"}"}'
        out = redact(line, [("K", "session_id\tsecret999")], jsonl=True)
        self.assertNotIn("secret999", out)
        json.loads(out)
        keep = 'x {"a": "alpha\\nbeta\\ngamma"}'
        self.assertEqual(redact(keep, [("K", "alpha beta gamma")], jsonl=True), keep)


class TestR5Worktree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r5w-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@x")
        self.git(self.tmp, "init", "-q", "-b", "main", self.repo)
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        self.git(self.repo, "add", "a.txt")
        self.git(self.repo, "commit", "-qm", "a")
        self.wt = os.path.join(self.tmp, "wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "t1", self.wt)

    def git(self, cwd: str, *args: str) -> str:
        return subprocess.run(["git"] + list(args), cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def _forge(self) -> str:
        """Re-point wt/.git at a task-made repo with core.fsmonitor set; returns the marker."""
        fake = os.path.join(self.wt, "sub")
        self.git(self.tmp, "init", "-q", fake)
        marker = os.path.join(self.tmp, "PWNED")
        if sys.platform == "win32":
            self.git(fake, "config", "core.fsmonitor",
                     f'"{sys.executable}" -c "open({marker!r}, \'w\').close()"')
        else:
            hook = os.path.join(self.tmp, "fsm.sh")
            with open(hook, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\ntouch %s\n" % marker)
            os.chmod(hook, 0o755)
            self.git(fake, "config", "core.fsmonitor", hook)
        wt_git = os.path.join(self.wt, ".git")
        if os.path.isfile(wt_git):
            os.unlink(wt_git)
        with open(wt_git, "w") as f:
            f.write("gitdir: sub/.git\n")
        return marker

    def test_p1_3_registered_worktree_check(self) -> None:
        from mesh.core.repos import is_registered_worktree

        self.assertTrue(is_registered_worktree(self.wt, self.repo))
        self._forge()
        self.assertFalse(is_registered_worktree(self.wt, self.repo))
        self.assertFalse(is_registered_worktree(os.path.join(self.tmp, "nope"), self.repo))

    def test_p1_3_meshd_git_never_runs_fsmonitor(self) -> None:
        from mesh.core.gitops import run_git

        marker = self._forge()
        run_git(["status", "--porcelain"], cwd=self.wt)
        self.assertFalse(os.path.exists(marker))

    def test_p1_3_lead_repo_refresh_skips_a_repointed_worktree(self) -> None:
        from mesh.daemon import lead_turns

        marker = self._forge()
        plan = {"repo": self.repo, "project_id": "prj", "ws_root": self.tmp, "agent_id": "agt", "path": self.wt}
        from mesh.core import gitops

        real = gitops.run_git
        calls = []

        def spy(args, cwd=None, **k):
            calls.append((list(args), cwd))
            if cwd == self.wt:
                os.environ.pop("GIT_CONFIG_COUNT", None)
                raise AssertionError("git ran in the re-pointed worktree")
            return real(args, cwd=cwd, **k)

        with mock.patch.object(gitops, "run_git", spy), \
                mock.patch("mesh.core.repos.run_git", spy), \
                mock.patch.object(lead_turns, "_log"):
            self.assertEqual(lead_turns.run_lead_repo_refresh(self.tmp, plan), self.wt)
        self.assertFalse([c for c in calls if c[1] == self.wt])
        self.assertFalse(os.path.exists(marker))

    def test_p2_4_forged_commondir_is_foreign(self) -> None:
        from mesh.core.repos import submission_git_info

        fake = os.path.join(self.tmp, "fakegit")
        os.makedirs(fake)
        real = os.path.join(self.repo, ".git", "worktrees", "wt")
        for name in ("HEAD", "index"):
            shutil.copy(os.path.join(real, name), os.path.join(fake, name))
        with open(os.path.join(fake, "commondir"), "w") as f:
            f.write(os.path.join(self.repo, ".git") + "\n")
        wt_git = os.path.join(self.wt, ".git")
        if os.path.isfile(wt_git):
            os.unlink(wt_git)
        with open(wt_git, "w") as f:
            f.write("gitdir: %s\n" % fake)
        info = submission_git_info(self.wt, "t1", None, repo_path=self.repo)
        self.assertEqual(info["error"]["reason"], "foreign_repository")

    def test_p2_4_repository_errors_never_raise(self) -> None:
        from mesh.core import repos

        with mock.patch.object(repos, "run_git", side_effect=subprocess.TimeoutExpired("git", 1)):
            info = repos.submission_git_info(self.wt, "t1", None, repo_path=self.repo)
        self.assertIn("error", info)


class TestR5Adapters(unittest.TestCase):
    def test_p1_4_vibe_output_dir_is_redacted_after_parse(self) -> None:
        from mesh.adapters.vibe import VibeAdapter

        run_dir = tempfile.mkdtemp(prefix="mh-r5v-")
        self.addCleanup(shutil.rmtree, run_dir, True)
        os.makedirs(os.path.join(run_dir, "vibe-out"))
        secret = "sk-ant-" + "a1B2c3D4" * 6
        with open(os.path.join(run_dir, "vibe-out", "export.json"), "w") as f:
            json.dump({"session_id": "s1", "outcome": "success", "usage": {"input_tokens": 3},
                       "messages": [{"content": "key " + secret}]}, f)
        with open(os.path.join(run_dir, "vibe-out", "notes.txt"), "w") as f:
            f.write("token=" + secret + "\n")
        res = VibeAdapter().parse(run_dir)
        self.assertEqual(res.session_id, "s1")
        for name in os.listdir(os.path.join(run_dir, "vibe-out")):
            with open(os.path.join(run_dir, "vibe-out", name)) as f:
                self.assertNotIn(secret, f.read())

    def test_p1_4_adapter_show_redacts_probe_details(self) -> None:
        from mesh.daemon.handlers import adapter as h

        secret = "sk-ant-" + "Z9y8X7w6" * 6
        probe = mock.Mock()
        probe.to_dict.return_value = {"status": "error", "details": {"stderr": "OPENAI_API_KEY=" + secret}}
        probe.binary_path = None
        ad = mock.Mock(id="droid", kind="vendor")
        ctx = mock.Mock(dry_run=False, _prepared_adapter=("show", (ad, probe)))
        with mock.patch.object(h, "_authorize_adapter_cmd"), \
                mock.patch.object(h, "_extract_binary_arg"), \
                mock.patch.object(h, "_extract_adapter_id", return_value="droid"), \
                mock.patch.object(h, "get_adapter", return_value=ad), \
                mock.patch.object(h, "record_adapter_probe"), \
                mock.patch.object(h, "effective_launch"), \
                mock.patch.object(h, "apply_to_probe", side_effect=lambda d, a, e: dict(d)):
            res = h.handle_adapter_show(ctx, False)
        self.assertNotIn(secret, json.dumps(res.data))


class TestR5Intents(unittest.TestCase):
    def test_p2_1_a_read_error_keeps_the_intent(self) -> None:
        from mesh.daemon import recovery

        home = tempfile.mkdtemp(prefix="mh-r5i-")
        self.addCleanup(shutil.rmtree, home, True)
        from mesh.core.repos import deliver_intent_path

        path = deliver_intent_path(home, "prj_x")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"project_id": "prj_x"}, f)
        real_open = open

        def emfile(p, *a, **k):
            if p == path:
                raise OSError(24, "Too many open files")
            return real_open(p, *a, **k)

        with mock.patch("builtins.open", side_effect=emfile):
            with self.assertRaises(recovery.DeliverIntentUnresolved):
                recovery.settle_deliver_intent(mock.Mock(), home, "prj_x")
            recovery._step_deliver_intents(mock.Mock(), home, {})
        self.assertTrue(os.path.exists(path))

    def test_p2_1_git_timeout_is_unresolved(self) -> None:
        import sqlite3
        from mesh.daemon import recovery

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE projects (id TEXT, repo_path TEXT, base_branch TEXT);")
        d = tempfile.mkdtemp(prefix="mh-r5g-")
        self.addCleanup(shutil.rmtree, d, True)
        conn.execute("INSERT INTO projects VALUES ('prj_x', ?, 'main');", (d,))
        with mock.patch("mesh.core.gitops.run_git", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaises(recovery.DeliverIntentUnresolved):
                recovery.reconcile_deliver_intent(conn, {"project_id": "prj_x", "old": "a" * 40, "new": "b" * 40})


class TestR5OfferedAt(unittest.TestCase):
    from tests.unit.test_scheduler import TestSchedulerUnit as _S

    setUp = _S.setUp
    tearDown = _S.tearDown

    def test_p2_3_enable_restarts_the_no_response_window(self) -> None:
        from mesh.daemon.handlers import agent as h
        from mesh.daemon.scheduler import check_queued_tasks_t6

        self.conn.execute("UPDATE tasks SET status = 'queued', queued_at = '2000-01-01T00:00:00Z', "
                          "offered_at = '2000-01-01T00:00:00Z' WHERE id = 'tsk-1';")
        self.conn.execute("UPDATE principals SET disabled_at = '2000-01-01T00:00:00Z' WHERE id = 'agt-ibx';")
        ctx = mock.Mock(actor_kind="user", via="direct", args={"name": "ibx"}, conn=self.conn,
                        principal={"id": "usr-1"})
        with mock.patch.object(h, "insert_event"):
            h.handle_agent_enable(ctx)
        self.assertEqual(check_queued_tasks_t6(self.conn), [])


class TestR5OpJson(unittest.TestCase):
    def test_p2_5_stored_op_json_verifies_near_line_cap(self) -> None:
        from mesh.core.approvals import stored_op_json, verify_stored_op_json
        from mesh.core.redact import LINE_CAP

        spec = "x" * (LINE_CAP - 12) + " password: a"
        self.assertLessEqual(len(spec), LINE_CAP)
        op = {"title": "t", "spec": spec}
        stored = stored_op_json(op, "sha256:" + "0" * 64)
        row = {"op_json": stored, "op_type": "task.create"}
        with mock.patch("mesh.core.approvals._op_doc_matches_row", return_value=True):
            self.assertEqual(verify_stored_op_json(row), "sha256:" + "0" * 64)


class TestR5Runwrap(unittest.TestCase):
    def test_p2_6_hung_pump_is_a_capture_error(self) -> None:
        from mesh import runwrap

        run_dir = tempfile.mkdtemp(prefix="mh-r5r-")
        self.addCleanup(shutil.rmtree, run_dir, True)
        with open(os.path.join(run_dir, "spec.json"), "w") as f:
            json.dump({"argv": [sys.executable, "-c", "pass"], "cwd": run_dir, "stdin": "devnull"}, f)
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(runwrap, "PIPE_DRAIN_GRACE_S", 0.1), \
                mock.patch.object(runwrap, "_pump_stream", lambda *a, **k: release.wait(20)):
            runwrap.run(run_dir)
        with open(os.path.join(run_dir, "exit.json")) as f:
            doc = json.load(f)
        self.assertEqual(doc["exit_code"], 0)
        self.assertIn("capture_error", doc)


if __name__ == "__main__":
    unittest.main()
