"""Integration tests for `task merge` with test_command none (plan §10.5, §6.2 T14–T17; m2-r5).

Every repo is a K0 fixture under /tmp/mh-*-git and meshd runs with the fixture's hermetic git
env (GitMeshHarness). Projects use `--test-command none`; the executor is the inbox agent `ibx`,
and the task commits are made by the test in the task worktree. Nothing is ever pushed.
The harness teardown runs `doctor --check-invariants`, so I9 is checked live after every
merge here (A8).

ACC: K4 (merge clause), K6, K8, K9, K11 (partial: remote refs unchanged).
"""

import json
import os
import shutil
import socket
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


class TestMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)  # doctor --check-invariants (I9 live), then fixture removal
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_agent(self.harness, "ibx")
        self.tok_plain = create_agent(self.harness, "plain")
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        self.repo, self.prj = self.make_project("R")
        self.main = self.fixture.head(self.repo, "main")
        self.remote_before = self.fixture.remote_refs(self.repo)

    # --- helpers -----------------------------------------------------------------

    def make_project(self, name: str, identity: bool = True, test_command: str = "none") -> Tuple[str, str]:
        repo = self.fixture.make_k0(name, identity=identity)
        if not hasattr(self, "repo_remote_refs"):
            self.repo_remote_refs = {}
        self.repo_remote_refs[repo] = self.fixture.remote_refs(repo)
        rc, resp = self.harness.cli("intake", "submit", "--title", f"Code {name}", "--goal", "g", "--criterion", "c1",
                                    "--repo", repo, "--base-branch", "main", "--test-command", test_command,
                                    "--lead", "lead1", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return repo, resp["data"]["id"]

    def integ_branch(self, prj: str) -> str:
        return f"mesh/{prj}/integration"

    def integ_wt(self, prj: str) -> str:
        return os.path.join(self.harness.home, "workspaces", prj, "integration")

    def show(self, task_id: str) -> Dict[str, Any]:
        rc, resp = self.harness.cli("task", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]

    def start_task(self, prj: str, title: str) -> Tuple[str, Dict[str, Any]]:
        rc, resp = self.harness.cli("task", "create", "--project", prj, "--title", title, "--spec", "Work",
                                    "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        return tsk, st["data"]

    def commit_file(self, ws: str, path: str, content: str, message: str) -> None:
        with open(os.path.join(ws, path), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(ws, "add", path)
        # `-c` identity: the K8 repo has no identity at all, and Mesh never sets one.
        self.fixture.git(ws, "commit", "-m", message, identity=True)

    def submit_accept(self, tsk: str, st: Dict[str, Any]) -> None:
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st["run_id"], "--fence", str(st["fence"]),
                                   "--summary", "done", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        rc, acc = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, acc)

    def accepted_task(self, prj: str, title: str, path: Optional[str], content: str = "x\n") -> Tuple[str, str]:
        tsk, st = self.start_task(prj, title)
        if path:
            self.commit_file(st["workspace"], path, content, title)
        self.submit_accept(tsk, st)
        return tsk, st["workspace"]

    def merge(self, tsk: str, token: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        return self.harness.cli("task", "merge", tsk, token=token or self.tok_lead1, timeout=120.0)

    def q(self, sql: str, params: tuple = ()) -> List[Any]:
        conn = self.harness.db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def lead1_merge_failed(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self.q(
            "SELECT o.payload_json FROM outbox o JOIN events e ON o.event_seq = e.seq "
            "WHERE e.task_id = ? AND o.type = 'merge.failed' AND o.recipient_id = "
            "(SELECT id FROM principals WHERE name = 'lead1');",
            (task_id,),
        )
        return [json.loads(r[0]) for r in rows]

    def merge_runs(self, task_id: str) -> List[Any]:
        return self.q("SELECT * FROM runs WHERE task_id = ? AND kind = 'merge_test' ORDER BY rowid;", (task_id,))

    def assert_no_merge_lease(self, prj: str) -> None:
        self.assertEqual(self.q("SELECT key FROM leases WHERE key = ?;", (f"merge:{prj}",)), [])

    def assert_integration_restored(self, repo: str, prj: str, pre_head: str) -> None:
        wt = self.integ_wt(prj)
        self.assertEqual(self.fixture.head(repo, self.integ_branch(prj)), pre_head)
        self.assertEqual(self.fixture.head(wt), pre_head)
        self.assertTrue(self.fixture.is_clean(wt))
        self.assertEqual(self.fixture.git(wt, "status", "--porcelain").stdout, "")
        self.assertFalse(self.fixture.has_merge_head(wt))

    def assert_remote_unchanged(self) -> None:
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)
        for repo, before in self.repo_remote_refs.items():
            self.assertEqual(self.fixture.remote_refs(repo), before, repo)

    def raw_request(self, cmd: str, args: Dict[str, Any], key: str, token: str) -> Dict[str, Any]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(120.0)
        try:
            s.connect(os.path.join(self.harness.home, "run", "mesh.sock"))
            s.sendall((json.dumps({"v": 1, "cmd": cmd, "auth": {"type": "token", "token": token},
                                   "args": args, "idempotency_key": key}) + "\n").encode("utf-8"))
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            s.close()
        return json.loads(raw.decode("utf-8"))

    def doctor_ok(self) -> None:
        rc, resp = self.harness.cli("doctor", "--check-invariants", user=True, timeout=60.0)
        self.assertEqual(rc, 0, resp)

    # --- clean merge (T14 → T15) ----------------------------------------------------

    def test_clean_merge_records_merge_commit(self) -> None:
        """ACC: K6 (merge A clause), K11 (partial) - a clean accepted task merges with --no-ff; I9 holds."""
        tsk, _ = self.accepted_task(self.prj, "Add feature", "feature.txt", "feature\n")
        branch = f"mesh/{self.prj}/{tsk}"
        tip = self.fixture.head(self.repo, branch)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        data = resp["data"]
        self.assertEqual(data["status"], "merged", data)
        self.assertIs(data["noop"], False, data)
        self.assertEqual(data["tests"], "none", data)
        self.assertEqual(data["pre_head"], self.main, data)

        integ = self.integ_branch(self.prj)
        mc = self.fixture.head(self.repo, integ)
        self.assertEqual(data["merge_commit"], mc)
        # A real merge commit: parents are pre_head and the task tip; the message names the task.
        parents = self.fixture.git(self.repo, "rev-list", "--parents", "-n", "1", mc).stdout.split()
        self.assertEqual(parents[1:], [self.main, tip])
        msg = self.fixture.git(self.repo, "log", "-1", "--format=%s", mc).stdout.strip()
        self.assertEqual(msg, f"Merge mesh task {tsk}: Add feature")
        self.assertTrue(self.fixture.is_clean(self.integ_wt(self.prj)))

        d = self.show(tsk)
        self.assertEqual(d["status"], "merged")
        merge = d["merge"]
        self.assertEqual(merge["merge_commit"], mc, merge)
        self.assertEqual(merge["pre_head"], self.main, merge)
        self.assertEqual(merge["tests"], "none", merge)
        self.assertTrue(merge["mrg"].startswith("mrg_"), merge)
        # The merge_test run (Q5): closed, run_dir under $MESH_HOME/merges/<mrg>/, lease released.
        runs = self.merge_runs(tsk)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "exited")
        self.assertEqual(runs[0]["end_reason"], "merged")
        self.assertIsNone(runs[0]["wrapper_pid"])
        self.assertEqual(runs[0]["run_dir"], os.path.join(self.harness.home, "merges", merge["mrg"]))
        self.assert_no_merge_lease(self.prj)
        # The task branch and worktree are kept (§10.7: nothing is deleted automatically).
        self.assertEqual(self.fixture.head(self.repo, branch), tip)
        self.assertTrue(os.path.isdir(d["workspace_path"]))
        # A merged task cannot be merged again.
        rc, again = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_CONFLICT, again)
        self.assertEqual(again["error"]["code"], c.INVALID_TRANSITION)
        self.doctor_ok()  # I9 live
        self.assert_remote_unchanged()

    def test_noop_merge(self) -> None:
        """ACC: K6 (noop clause of §10.5 step 2) - a branch already in integration is merged with noop:true."""
        tsk, _ = self.accepted_task(self.prj, "Nothing", None)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "merged")
        self.assertIs(resp["data"]["noop"], True, resp)
        self.assertEqual(self.fixture.head(self.repo, self.integ_branch(self.prj)), self.main)
        merge = self.show(tsk)["merge"]
        self.assertIs(merge["noop"], True, merge)
        self.assertEqual(merge["merge_commit"], self.main, merge)
        self.assert_no_merge_lease(self.prj)
        self.doctor_ok()
        self.assert_remote_unchanged()

    # --- K6 ------------------------------------------------------------------------

    def test_k6_conflict_restores_integration(self) -> None:
        """ACC: K6 - A and B change the same line; A merges, B is rc 14 MERGE_CONFLICT and integration is restored."""
        a, _ = self.accepted_task(self.prj, "A", "calc.py", "def add(a, b):\n    return a + b + 1\n")
        b, _ = self.accepted_task(self.prj, "B", "calc.py", "def add(a, b):\n    return a + b + 2\n")
        rc, ra = self.merge(a)
        self.assertEqual(rc, 0, ra)
        self.assertEqual(ra["data"]["status"], "merged")
        pre_head = self.fixture.head(self.repo, self.integ_branch(self.prj))
        a_content = self.fixture.git(self.repo, "show", f"{pre_head}:calc.py").stdout

        rc, rb = self.merge(b)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, rb)
        err = rb["error"]
        self.assertEqual(err["code"], c.MERGE_CONFLICT, err)
        self.assertEqual(err["details"]["reason"], "conflict")
        self.assertIn("calc.py", err["details"]["files"], err)
        self.assertEqual(err["details"]["pre_head"], pre_head)
        self.assertIs(err["details"]["restored"], True, err)
        self.assert_integration_restored(self.repo, self.prj, pre_head)
        with open(os.path.join(self.integ_wt(self.prj), "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), a_content)
        self.assertIn("a + b + 1", a_content)

        d = self.show(b)
        self.assertEqual(d["status"], "merge_failed")
        self.assertEqual(d["merge"]["reason"], "conflict", d["merge"])
        self.assertEqual(self.show(a)["status"], "merged")
        msgs = self.lead1_merge_failed(b)
        self.assertEqual(len(msgs), 1, msgs)
        runs = self.merge_runs(b)
        self.assertEqual([r["status"] for r in runs], ["exited"])
        self.assertEqual(runs[0]["end_reason"], "conflict")
        self.assert_no_merge_lease(self.prj)
        self.doctor_ok()
        self.assert_remote_unchanged()

    # --- K4 (merge clause) + T17 -----------------------------------------------------

    def test_k4_dirty_worktree_then_t17_retry(self) -> None:
        """ACC: K4 (merge clause) - an uncommitted file makes merge rc 14 WORKTREE_DIRTY; after cleanup T17 merges."""
        tsk, st = self.start_task(self.prj, "Dirty")
        ws = st["workspace"]
        self.commit_file(ws, "done.txt", "done\n", "done")
        with open(os.path.join(ws, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("not committed\n")
        self.submit_accept(tsk, st)
        git = self.show(tsk)["deliverable"]["git"]
        self.assertIs(git["dirty"], True, git)
        self.assertIn("notes.txt", git["dirty_files"], git)

        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.WORKTREE_DIRTY, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "worktree_dirty")
        self.assertIn("notes.txt", resp["error"]["details"]["dirty_files"])
        self.assert_integration_restored(self.repo, self.prj, self.main)
        d = self.show(tsk)
        self.assertEqual(d["status"], "merge_failed")
        self.assertEqual(d["merge"]["reason"], "worktree_dirty")
        self.assertEqual(len(self.lead1_merge_failed(tsk)), 1)
        # Mesh did not touch the worktree.
        self.assertTrue(os.path.exists(os.path.join(ws, "notes.txt")))

        # T17: the lead cleans the worktree and retries.
        os.remove(os.path.join(ws, "notes.txt"))
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["status"], "merged")
        self.assertEqual(self.show(tsk)["status"], "merged")
        types = [r["type"] for r in self.q(
            "SELECT type FROM events WHERE task_id = ? AND type IN ('task.merging','merge.failed','task.merged') "
            "ORDER BY seq;", (tsk,))]
        self.assertEqual(types, ["task.merging", "merge.failed", "task.merging", "task.merged"])
        self.assertEqual(len(self.merge_runs(tsk)), 2)
        self.doctor_ok()
        self.assert_remote_unchanged()

    def test_idempotent_replay_rc14_and_rc0(self) -> None:
        """ACC: B5, K4 (merge clause) - §13.4: rc 14 and rc 0 merge responses replay with idempotent_replay; no second pipeline."""
        tsk, st = self.start_task(self.prj, "Idem")
        ws = st["workspace"]
        self.commit_file(ws, "idem.txt", "idem\n", "idem")
        with open(os.path.join(ws, "dirty.txt"), "w", encoding="utf-8") as f:
            f.write("dirty\n")
        self.submit_accept(tsk, st)

        # Raw socket: the CLI's error output does not carry `idempotent_replay`; the wire does.
        first = self.raw_request("task.merge", {"task_id": tsk}, "mk1", self.tok_lead1)
        self.assertEqual(first["exit_code"], c.EXIT_MERGE_FAILED, first)
        self.assertEqual(first["error"]["code"], c.WORKTREE_DIRTY)
        again = self.raw_request("task.merge", {"task_id": tsk}, "mk1", self.tok_lead1)
        self.assertEqual(again["exit_code"], c.EXIT_MERGE_FAILED, again)
        self.assertIs(again.get("idempotent_replay"), True, again)
        self.assertEqual(again["request_id"], first["request_id"])  # the stored envelope
        self.assertEqual(again["error"]["code"], c.WORKTREE_DIRTY)
        self.assertEqual(again["error"]["details"]["merge_id"], first["error"]["details"]["merge_id"])
        self.assertEqual(len(self.merge_runs(tsk)), 1)
        self.assertEqual(len(self.lead1_merge_failed(tsk)), 1)
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        stored = self.q("SELECT exit_code FROM idempotency WHERE key = 'mk1';")
        self.assertEqual([r[0] for r in stored], [c.EXIT_MERGE_FAILED])

        os.remove(os.path.join(ws, "dirty.txt"))
        rc, ok = self.harness.cli("task", "merge", tsk, "--idempotency-key", "mk2",
                                  token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, 0, ok)
        rc, ok2 = self.harness.cli("task", "merge", tsk, "--idempotency-key", "mk2",
                                   token=self.tok_lead1, timeout=120.0)
        self.assertEqual(rc, 0, ok2)
        self.assertIs(ok2.get("idempotent_replay"), True, ok2)
        self.assertEqual(ok2["data"]["merge_commit"], ok["data"]["merge_commit"])
        self.assertEqual(len(self.merge_runs(tsk)), 2)
        merged = self.q("SELECT COUNT(*) FROM events WHERE task_id = ? AND type = 'task.merged';", (tsk,))
        self.assertEqual(merged[0][0], 1)
        self.assertEqual(self.show(tsk)["status"], "merged")
        self.assert_remote_unchanged()

    def test_integration_dirty(self) -> None:
        """ACC: K4 (integration clause of §10.5 step 1) - a dirty integration worktree is rc 14 INTEGRATION_DIRTY."""
        tsk, _ = self.accepted_task(self.prj, "F", "f.txt")
        stray = os.path.join(self.integ_wt(self.prj), "stray.txt")
        with open(stray, "w", encoding="utf-8") as f:
            f.write("stray\n")
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        self.assertEqual(resp["error"]["code"], c.INTEGRATION_DIRTY, resp)
        self.assertEqual(self.fixture.head(self.repo, self.integ_branch(self.prj)), self.main)
        self.assertTrue(os.path.exists(stray))
        self.assertEqual(self.show(tsk)["status"], "merge_failed")
        os.remove(stray)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)

    # --- K8 ------------------------------------------------------------------------

    def test_k8_no_identity_commit_failed(self) -> None:
        """ACC: K8 - no git identity: merge_failed reason commit_failed with stderr; integration unchanged; no identity set."""
        repo, prj = self.make_project("R8", identity=False)
        main = self.fixture.head(repo, "main")
        tsk, _ = self.accepted_task(prj, "Needs identity", "id.txt")
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_MERGE_FAILED, resp)
        err = resp["error"]
        self.assertEqual(err["code"], c.GIT_COMMIT_FAILED, err)
        self.assertEqual(err["details"]["reason"], "commit_failed")
        self.assertTrue(err["details"]["stderr"].strip(), err)
        d = self.show(tsk)
        self.assertEqual(d["status"], "merge_failed")
        self.assertEqual(d["merge"]["reason"], "commit_failed", d["merge"])
        self.assertTrue(d["merge"]["details"]["stderr"].strip(), d["merge"])
        self.assert_integration_restored(repo, prj, main)
        for where in (repo, self.integ_wt(prj)):
            cp = self.fixture.git(where, "config", "--get", "user.name", check=False)
            self.assertEqual(cp.stdout.strip(), "", cp)
            self.assertNotEqual(cp.returncode, 0, cp)
        self.assertEqual(len(self.lead1_merge_failed(tsk)), 1)
        self.assert_no_merge_lease(prj)
        self.assert_remote_unchanged()

    # --- K9 + guards -----------------------------------------------------------------

    def test_k9_guards(self) -> None:
        """ACC: K9 - merge on a submitted task is rc 6 INVALID_TRANSITION; by [@plain] rc 4; other guards in order."""
        tsk, st = self.start_task(self.prj, "Sub")
        self.commit_file(st["workspace"], "s.txt", "s\n", "s")
        # Still running: rc 6 INVALID_TRANSITION (status check comes before the lease check).
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_TRANSITION)
        rc, sub = self.harness.cli("task", "submit", tsk, "--run", st["run_id"], "--fence", str(st["fence"]),
                                   "--summary", "done", token=self.tok_ibx)
        self.assertEqual(rc, 0, sub)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_TRANSITION)
        self.assertEqual(resp["error"]["details"]["from_state"], "submitted")
        rc, acc = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, acc)
        rc, resp = self.merge(tsk, token=self.tok_plain)
        self.assertEqual(rc, c.EXIT_FORBIDDEN, resp)
        self.assertEqual(self.show(tsk)["status"], "accepted")
        rc, resp = self.merge("tsk_0000000000aaaaaaaaaaaaaaaa")
        self.assertEqual(rc, c.EXIT_NOT_FOUND, resp)
        self.assertEqual(self.merge_runs(tsk), [])
        self.assertEqual(self.fixture.head(self.repo, self.integ_branch(self.prj)), self.main)
        self.assert_remote_unchanged()

    def test_general_task_and_test_command_guards(self) -> None:
        """ACC: K9 (guards), K5 - kind=general is rc 2 param kind; a test-command project merges (r6 removed the merge_tests stub)."""
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "G", "--spec", "s",
                                    "--assignee", "ibx", "--kind", "general", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        g = resp["data"]["id"]
        rc, resp = self.merge(g)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["details"]["param"], "kind")

        repo2, prj2 = self.make_project("R2", test_command='["python3","-m","unittest"]')
        tsk, _ = self.accepted_task(prj2, "T", "t.txt")
        rc, resp = self.merge(tsk)
        # B11-style conversion (m2-r6): the r5 stub (rc 2 NOT_IMPLEMENTED_IN_M1 `merge_tests`)
        # is gone; the K0 suite passes, so the task merges with `tests:"passed"`.
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["tests"], "passed", resp)
        after = self.show(tsk)
        self.assertEqual(after["status"], "merged")
        self.assertEqual(len(self.merge_runs(tsk)), 1)
        self.assert_no_merge_lease(prj2)
        self.assertEqual(self.fixture.head(repo2, self.integ_branch(prj2)), resp["data"]["merge_commit"])
        self.assert_remote_unchanged()

    def test_dry_run_changes_nothing(self) -> None:
        """ACC: K6 (guard) - --dry-run reports T14 and changes nothing."""
        tsk, _ = self.accepted_task(self.prj, "D", "d.txt")
        rc, resp = self.harness.cli("task", "merge", tsk, "--dry-run", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp.get("dry_run"), resp)
        self.assertEqual(self.show(tsk)["status"], "accepted")
        self.assertEqual(self.merge_runs(tsk), [])
        self.assert_no_merge_lease(self.prj)
        self.assertEqual(self.fixture.head(self.repo, self.integ_branch(self.prj)), self.main)


if __name__ == "__main__":
    unittest.main()
