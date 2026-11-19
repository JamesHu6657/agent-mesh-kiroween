"""Regression tests for PR #54 round-1 review findings.

1. P1: multi-line exact secrets longer than 64 lines must not drop short (< 8 char)
   lines in redact(), StreamRedactor, JSONL, or escaped views, while staying linear.
2. P2: "Authorization: Basic" at the end of a line must arm the next non-blank line's
   first token (batch, stream, JSON array), keeping non-boundaries intact.
3. P2: reverse I9 check must not fail closed as "unverifiable" solely because integration
   exceeds 200 merges; stale cancelled/failed branches allow deliver, integrated ones
   still block deliver, and real git errors still fail closed.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import mesh.constants as c
from mesh.core import db, invariants
from mesh.core.approvals import stored_op_json, verify_stored_op_json
from mesh.core.canonical import compute_op_hash
from mesh.core.redact import LINE_CAP, UNBROKEN, StreamRedactor, redact
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_deliver
from mesh.errors import MeshError
from tests.gitfixture import GitFixture


def _stream(text: str, exact=(), jsonl: bool = False, steps=(1, 7, 64, 4096)) -> str:
    results = []
    for step in steps:
        sr = StreamRedactor(list(exact), jsonl=jsonl)
        out = "".join(sr.feed(text[i:i + step]) for i in range(0, len(text), step)) + sr.flush()
        results.append(out)
    for out in results[1:]:
        assert out == results[0], "StreamRedactor output differed across chunk sizes"
    return results[0]


def _make_ctx(conn, home: str, cmd: str, args: dict) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "user", "kind": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_pr54_r1",
        home=home,
        cmd=cmd,
        args=args,
        k_user=b"k" * 32,
    )


class TestPR54R1LongMultilineExact(unittest.TestCase):
    """Finding 1 (P1): multi-line exact secrets > 64 lines must never leak short lines."""

    def test_70_line_all_short_secret_redacted_batch_and_stream(self) -> None:
        lines = [f"L{i:02d}" for i in range(70)]  # each line is 3 chars (< MIN_EXACT_VALUE_LEN)
        secret = "\n".join(lines)
        exact = [("LONG_SHORT_KEY", secret)]
        text = "prefix " + secret + " suffix\nafter\n"

        for jsonl in (False, True):
            with self.subTest(jsonl=jsonl):
                out = redact(text, exact, jsonl=jsonl)
                self.assertEqual(_stream(text, exact, jsonl=jsonl), out)
                self.assertEqual(redact(out, exact, jsonl=jsonl), out)
                for ln in lines:
                    self.assertNotIn(ln, out)
                self.assertIn("prefix [REDACTED:LONG_SHORT_KEY]", out)
                self.assertIn(" suffix\nafter\n", out)

    def test_70_line_mixed_short_tail_and_empty_line_redacted(self) -> None:
        lines = [f"chunk_{i:02d}_longsecret" for i in range(64)] + ["", "end", "x", "tail78"]
        secret = "\n".join(lines)
        exact = [("MIXED_70", secret)]
        text = "start " + secret + " done\n"

        out = redact(text, exact, jsonl=False)
        self.assertEqual(_stream(text, exact, jsonl=False), out)
        self.assertNotIn("end", out)
        self.assertNotIn("tail78", out)
        self.assertIn("start [REDACTED:MIXED_70]", out)
        self.assertIn(" done\n", out)

    def test_70_line_short_secret_in_jsonl_and_escaped_text(self) -> None:
        lines = [f"S{i:02d}" for i in range(70)]
        secret = "\n".join(lines)
        exact = [("JSON_70", secret)]

        # 1. Inside a JSONL event string field
        jline = json.dumps({"event": "out", "body": "hdr " + secret + " ftr"}) + "\n"
        jout = redact(jline, exact, jsonl=True)
        self.assertEqual(_stream(jline, exact, jsonl=True), jout)
        doc = json.loads(jout)
        self.assertIn("hdr [REDACTED:JSON_70]", doc["body"])
        for ln in lines:
            self.assertNotIn(ln, jout)

        # 2. Escaped newlines inside a non-JSON line
        esc_line = "INFO dump=" + "\\n".join(lines) + "\n"
        eout = redact(esc_line, exact, jsonl=True)
        self.assertEqual(_stream(esc_line, exact, jsonl=True), eout)
        for ln in lines:
            self.assertNotIn(ln, eout)

        # 3. Spanning JSON syntax via escaped newlines across keys must fail closed
        span_secret = 'k1\n' + '\n'.join(f'k{i:02d}' for i in range(2, 69)) + '", "b": "end'
        span_exact = [("SPAN_70", span_secret)]
        span_raw = json.dumps({"a": 'k1\n' + '\n'.join(f'k{i:02d}' for i in range(2, 69)), "b": "end"}) + "\n"
        sout = redact(span_raw, span_exact, jsonl=True)
        self.assertNotIn("k68", sout)
        self.assertEqual(_stream(span_raw, span_exact, jsonl=True), sout)

    def test_70_line_secret_with_overlong_boundary_and_partial_mismatch(self) -> None:
        lines = [f"P{i:02d}" for i in range(70)]
        secret = "\n".join(lines)
        exact = [("OVER_70", secret)]

        # Partial prefix (first 69 lines without the 70th) must NOT falsely redact short lines
        partial = "\n".join(lines[:69]) + "\nnot_the_70th\n"
        self.assertEqual(redact(partial, exact), partial)
        self.assertEqual(_stream(partial, exact), partial)

        # Starting on an over-long line must redact lines 1..69
        over_start = "x" * (LINE_CAP + 5) + "\n" + "\n".join(lines[1:]) + "\nafter\n"
        out_start = redact(over_start, exact)
        self.assertEqual(_stream(over_start, exact, steps=(1024, 70000)), out_start)
        self.assertIn(UNBROKEN, out_start)
        for ln in lines[1:]:
            self.assertNotIn(ln, out_start)
        self.assertIn("after\n", out_start)

        # Ending on an over-long line must withhold the kept head and redact earlier lines
        over_end = "pre " + "\n".join(lines[:-1]) + "\n" + lines[-1] + " " + "y" * (LINE_CAP + 5) + "\n"
        out_end = redact(over_end, exact)
        self.assertEqual(_stream(over_end, exact, steps=(1024, 70000)), out_end)
        for ln in lines:
            self.assertNotIn(ln, out_end)
        self.assertIn(UNBROKEN, out_end)

    def test_500_line_repeated_secret_stays_linear_and_fast(self) -> None:
        # Adversarial repeating prefix ("aa\n" * 499 + "bb") in 2 MB of "aa\n" and normal logs
        secret = ("aa\n" * 499) + "bb"
        exact = [("REP_500", secret)]

        rep_log = "aa\n" * 60000
        t0 = time.monotonic()
        rep_out = redact(rep_log, exact, jsonl=True)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(rep_out, rep_log)

        # Now actually embed the 500-line secret inside a 2 MB log
        log_2mb = ("hello world " * 80 + "\n") * 2000 + secret + "\ntail\n"
        t1 = time.monotonic()
        out_2mb = redact(log_2mb, exact, jsonl=True)
        self.assertLess(time.monotonic() - t1, 2.0)
        self.assertNotIn("bb\ntail", out_2mb)
        self.assertIn("[REDACTED:REP_500]\ntail\n", out_2mb)

        # 500 distinct short lines (< 8 chars) in a 2 MB log: fast and never leaks any line
        short_500_lines = [f"k{i:03d}" for i in range(500)]
        short_500 = "\n".join(short_500_lines)
        exact_short = [("SHORT_500", short_500)]
        log_short = ("hello world " * 80 + "\n") * 2000 + "pre " + short_500 + " post\n"
        t2 = time.monotonic()
        out_short = redact(log_short, exact_short, jsonl=True)
        self.assertLess(time.monotonic() - t2, 2.0)
        self.assertEqual(_stream(log_short, exact_short, jsonl=True, steps=(4096, 65536)), out_short)
        for ln in short_500_lines:
            self.assertNotIn(ln, out_short)
        self.assertIn("pre [REDACTED:SHORT_500]", out_short)
        self.assertIn(" post\n", out_short)


class TestPR54R1AuthBasicCrossLine(unittest.TestCase):
    """Finding 2 (P2): Authorization: Basic at end of line must arm next line's first token."""

    def test_authorization_basic_armed_across_lines_batch_stream_and_json(self) -> None:
        cases = [
            ("Authorization: Basic\ndXNlcjpwYXNz\n", "Authorization: Basic\n[REDACTED]\n"),
            ("Authorization: Basic   \n\n  dXNlcjpwYXNz rest\n", "Authorization: Basic   \n\n  [REDACTED] rest\n"),
            ("authorization:basic\ndXNlcjpwYXNz", "authorization:basic\n[REDACTED]"),
            ("Authorization=Basic\ndXNlcjpwYXNz\n", "Authorization=Basic\n[REDACTED]\n"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                for jsonl in (False, True):
                    out = redact(raw, (), jsonl=jsonl)
                    self.assertEqual(out, expected)
                    self.assertEqual(_stream(raw, (), jsonl=jsonl), expected)
                    self.assertEqual(redact(out, (), jsonl=jsonl), out)  # fixed point

        # JSON array where "Authorization: Basic" is followed by the token element
        jraw = '["Authorization: Basic", "dXNlcjpwYXNz"]\n'
        jout = redact(jraw, (), jsonl=True)
        self.assertEqual(jout, '["Authorization: Basic", "[REDACTED]"]\n')
        self.assertEqual(_stream(jraw, (), jsonl=True), jout)

    def test_authorization_basic_non_boundaries_and_worst_case_linearity(self) -> None:
        # Non-boundary prefixes and standalone "basic" must not arm
        for safe in (
            "X-Authorization: Basic\nhello\n",
            "myauthorization: Basic\nhello\n",
            "a basic\nhello\n",
            "Authorization: Basic [REDACTED]\nhello\n",
        ):
            with self.subTest(safe=safe):
                self.assertEqual(redact(safe, (), jsonl=True), safe)
                self.assertEqual(_stream(safe, (), jsonl=True), safe)

        # verify_stored_op_json fixed point
        op_doc, op_hash = compute_op_hash(
            op="task.create",
            requested_by="lead_1",
            params={"cmd": "Authorization: Basic\ndXNlcjpwYXNz"},
            project_id="proj_1",
            target_id=None,
        )
        stored = stored_op_json(op_doc, op_hash)
        self.assertNotIn("dXNlcjpwYXNz", stored)
        self.assertEqual(
            verify_stored_op_json({
                "op_json": stored,
                "op_type": "task.create",
                "requested_by": "lead_1",
                "project_id": "proj_1",
                "category": None,
            }),
            op_hash,
        )

        # 64 KB worst-case line performance (< 0.5s)
        for line in (
            ("Authorization: Basic" + " " * LINE_CAP)[:LINE_CAP],
            ("authorization: " * (LINE_CAP // 15))[:LINE_CAP],
        ):
            t0 = time.monotonic()
            out = redact(line, (), jsonl=True)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertLessEqual(len(out), LINE_CAP)


class TestPR54R1ReverseI9LargeHistory(unittest.TestCase):
    """Finding 3 (P2): reverse I9 must not block deliver when integration has > 200 merges."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="mh-pr54-i9-")
        self.fixture = GitFixture()
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[k]
        os.environ.update(self.fixture.git_env_vars())

        self.repo = self.fixture.make_k0("PR54I9")
        self.base_sha = self.fixture.head(self.repo, "main")
        self.integ = "mesh/proj_1/integration"
        self.fixture.git(self.repo, "branch", self.integ, "main")

        self.conn = db.init_db(os.path.join(self.tmp.name, "mesh.db"))
        ts = "2026-01-01T00:00:00Z"
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('lead_1', 'agent', 'Lead', ?);",
            (ts,),
        )
        self.conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
            "VALUES ('lead_1', 'codex', 'inbox', 1, 1, ?, ?);",
            (ts, ts),
        )
        self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
            "created_by, created_via, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            ("proj_1", "p", "g", "active", self.repo, "main", self.integ, "user", "direct", ts, ts),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.fixture.cleanup()
        self.tmp.cleanup()

    def _git_out(self, *args: str) -> str:
        return self.fixture.git(self.repo, *args).stdout.strip()

    def _add_merges(self, count: int, start_from: str, *, prefix: str = "other") -> tuple[str, list[str]]:
        tree = self._git_out("rev-parse", f"{self.base_sha}^{{tree}}")
        cur = start_from
        merges = []
        for i in range(count):
            side = self._git_out("commit-tree", tree, "-p", self.base_sha, "-m", f"{prefix}_side_{i}")
            cur = self._git_out("commit-tree", tree, "-p", cur, "-p", side, "-m", f"{prefix}_merge_{i}")
            merges.append(cur)
        self._git_out("update-ref", f"refs/heads/{self.integ}", cur)
        return cur, merges

    def _insert_task(self, task_id: str, status: str, branch: str, base_commit: str,
                     merge_commit: str = None) -> None:
        ts = "2026-01-01T00:00:00Z"
        mj = json.dumps({"merge_commit": merge_commit, "noop": False}) if merge_commit else None
        self.conn.execute(
            "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
            "branch, base_commit, merge_json, created_by, created_at, updated_at) "
            "VALUES (?, 'proj_1', 't', 's', 'code', ?, 'edit', 60, 1, ?, ?, ?, 'user', ?, ?);",
            (task_id, status, branch, base_commit, mj, ts, ts),
        )
        self.conn.commit()

    def _make_task_branch(self, branch: str, parent_sha: str, filename: str) -> str:
        self._git_out("branch", branch, parent_sha)
        wt = self.fixture.path(f"wt-{branch.replace('/', '_')}")
        self.fixture.git(self.repo, "worktree", "add", wt, branch)
        Path(wt, filename).write_text(f"{filename}\n")
        self.fixture.git(wt, "add", filename)
        self.fixture.git(wt, "commit", "-q", "-m", f"work on {branch}", identity=True)
        tip = self.fixture.head(wt)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        return tip

    def test_stale_cancelled_and_failed_branches_past_200_merges_allow_deliver(self) -> None:
        # 210 unrelated merges on integration (> old _I9_REVERSE_MERGE_CAP of 200)
        integ_tip, merges = self._add_merges(210, self.base_sha)
        for idx, mc in enumerate(merges[:10]):
            self._insert_task(f"tsk_m{idx}", "merged", f"mesh/proj_1/m{idx}", self.base_sha, merge_commit=mc)

        # 1. Stale cancelled branch with unmerged work from base_sha
        self._make_task_branch("mesh/proj_1/stale_cancel", self.base_sha, "stale1.txt")
        self._insert_task("tsk_c1", "cancelled", "mesh/proj_1/stale_cancel", self.base_sha)

        # 2. Stale merge_failed branch that synced integration at merge #10 before failing
        synced_sha = merges[9]
        self._make_task_branch("mesh/proj_1/stale_fail", synced_sha, "stale2.txt")
        self._insert_task("tsk_f1", "merge_failed", "mesh/proj_1/stale_fail", self.base_sha)

        # Detach HEAD from main so update-ref on refs/heads/main succeeds during deliver
        self._git_out("checkout", "-q", "--detach", self.base_sha)

        self.assertEqual(invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1"), [])
        res = handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "proj_1"}))
        self.assertEqual(res.data["new"], integ_tip)
        self.assertEqual(self.fixture.head(self.repo, "main"), integ_tip)

    def test_unmerged_integrated_branch_buried_past_200_merges_is_still_caught(self) -> None:
        # Task branch integrated first, then 210 unrelated merges added on top
        tip = self._make_task_branch("mesh/proj_1/sneaky", self.base_sha, "sneaky.txt")
        integ_wt = self.fixture.path("wt-integ-sneaky")
        self.fixture.git(self.repo, "worktree", "add", integ_wt, self.integ)
        self.fixture.git(integ_wt, "merge", "--no-ff", "-m", "unrecorded merge", "mesh/proj_1/sneaky", identity=True)
        unrecorded_mc = self.fixture.head(integ_wt)
        self.fixture.git(self.repo, "worktree", "remove", integ_wt)

        self._add_merges(210, unrecorded_mc)
        self._insert_task("tsk_sneaky", "cancelled", "mesh/proj_1/sneaky", self.base_sha)

        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
        self.assertEqual(len(viols), 1)
        self.assertEqual(viols[0].details["reason"], "integrated_but_not_merged")
        self.assertEqual(viols[0].details["merge_commit"], unrecorded_mc)
        with self.assertRaises(MeshError) as cm:
            handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "proj_1"}))
        self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)

    def test_fast_forwarded_branch_buried_past_200_merges_is_still_caught(self) -> None:
        tip = self._make_task_branch("mesh/proj_1/ff_sneaky", self.base_sha, "ff.txt")
        self._git_out("update-ref", f"refs/heads/{self.integ}", tip)
        self._add_merges(210, tip)
        self._insert_task("tsk_ff", "merge_failed", "mesh/proj_1/ff_sneaky", self.base_sha)

        viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
        self.assertEqual(len(viols), 1)
        self.assertEqual(viols[0].details["reason"], "integrated_but_not_merged")
        self.assertEqual(viols[0].details["merge_commit"], tip)

    def test_git_failures_still_fail_closed_as_unverifiable(self) -> None:
        self._make_task_branch("mesh/proj_1/t1", self.base_sha, "t1.txt")
        self._insert_task("tsk_1", "cancelled", "mesh/proj_1/t1", self.base_sha)

        with mock.patch("mesh.core.gitops.run_git", side_effect=RuntimeError("git timeout")):
            viols = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
            self.assertEqual(len(viols), 1)
            self.assertEqual(viols[0].details["reason"], "unverifiable")
            with self.assertRaises(MeshError) as cm:
                handle_project_deliver(_make_ctx(self.conn, self.tmp.name, "project.deliver", {"project_id": "proj_1"}))
            self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)

        # Missing integration branch also fails closed as unverifiable
        self.conn.execute("UPDATE projects SET integration_branch = 'mesh/proj_1/missing' WHERE id = 'proj_1';")
        self.conn.commit()
        viols_missing = invariants.check_i9_integrated_unmerged_tasks(self.conn, "proj_1")
        self.assertEqual(len(viols_missing), 1)
        self.assertEqual(viols_missing[0].details["reason"], "unverifiable")


if __name__ == "__main__":
    unittest.main()
