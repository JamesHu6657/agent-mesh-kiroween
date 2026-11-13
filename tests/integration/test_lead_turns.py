"""Integration tests for lead turns (plan §12.4, §12.6; checklist L1, L3, L6, K13).

Every test drives the real CLI (`bin/mesh` subprocesses) against a `/tmp/mh-*` daemon
started by the harness, with the `fake` adapter as the lead. The fake selects its
behaviour from the first prompt line matching `^FAKE {…}$` (Q11), so each test puts
its spec in the project `--goal`.

The lead `flead` is a launch-mode agent with `--can-lead`; the project names it with
`--lead flead`. `lead.wake_debounce_s` is 2 s and `lead.wake_backoff_s` is set per
test, so no test waits on the production 30/120/600 s backoff (Q9).
"""

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs


def create_agent(
    harness: Any,
    name: str,
    adapter: str = "none",
    delivery: str = "inbox",
    can_lead: bool = False,
    capabilities: Optional[List[str]] = None,
    allowed_models: Optional[List[str]] = None,
    default_model: Optional[str] = None,
    max_concurrency: int = 1,
) -> str:
    """Create an agent and return its token (the M1c helper, unchanged)."""
    if delivery == "launch":
        if allowed_models is None:
            allowed_models = ["fake-model"]
        if default_model is None:
            default_model = "fake-model"

    args = [
        "agent", "create",
        "--name", name,
        "--adapter", adapter,
        "--delivery", delivery,
        "--max-concurrency", str(max_concurrency),
    ]
    if can_lead:
        args.append("--can-lead")
    if capabilities:
        for cap in capabilities:
            args.extend(["--capability", cap])
    if allowed_models:
        for m in allowed_models:
            args.extend(["--allowed-model", m])
    if default_model:
        args.extend(["--default-model", default_model])

    rc, resp = harness.setup_cli(*args, user=True)
    assert rc == 0, f"agent create {name} failed: {resp}"

    rc_tok, tok_resp = harness.setup_cli("agent", "token", "issue", name, user=True)
    assert rc_tok == 0, f"token issue {name} failed: {tok_resp}"
    return tok_resp["data"]["token"]


class LeadTurnCase(unittest.TestCase):
    """Shared harness: one daemon, `flead` (fake, launch, can_lead), `grok` (origin)."""

    #: Set by subclasses that need a git fixture.
    needs_git = False

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.fixture: Optional[GitFixture] = None
        if self.needs_git:
            self.fixture = GitFixture()
            self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True,
                                          extra_path=self.stubs_dir)
        else:
            self.harness = MeshHarness(init_db=True, start_daemon=True,
                                       extra_path=self.stubs_dir)
        self.tok_flead = create_agent(self.harness, "flead", adapter="fake", delivery="launch",
                                      can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", adapter="none", delivery="inbox",
                                     capabilities=["intake"])
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"flead"', user=True)
        self.assertEqual(rc, 0)
        rc, _ = self.harness.cli("config", "set", "lead.wake_debounce_s", "2", user=True)
        self.assertEqual(rc, 0)

    def tearDown(self) -> None:
        # GitMeshHarness.teardown() also cleans the git fixture up.
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    # --- helpers -----------------------------------------------------------

    def _set_backoff(self, values: List[int]) -> None:
        rc, resp = self.harness.cli("config", "set", "lead.wake_backoff_s",
                                    json.dumps(values), user=True)
        self.assertEqual(rc, 0, resp)

    def _create_project(self, goal: str, title: str = "Lead turn project",
                        repo: Optional[str] = None) -> str:
        args = ["intake", "submit", "--title", title, "--goal", goal,
                "--criterion", "C1", "--lead", "flead"]
        if repo is not None:
            args += ["--repo", repo, "--base-branch", "main", "--test-command", "none"]
        rc, resp = self.harness.cli(*args, token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _follow_up(self, project_id: str, message: str) -> None:
        rc, resp = self.harness.cli("intake", "submit", "--project", project_id,
                                    "--message", message, token=self.tok_grok)
        self.assertEqual(rc, 0, resp)

    def _lead_turn_runs(self, project_id: str) -> List[sqlite3.Row]:
        conn = self.harness.db()
        try:
            return conn.execute(
                "SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ? "
                "ORDER BY started_at ASC, id ASC;",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()

    def _lead_wake_rows(self, project_id: str) -> List[sqlite3.Row]:
        conn = self.harness.db()
        try:
            return conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND channel = 'lead_wake' "
                "ORDER BY event_seq ASC, id ASC;",
                (project_id,),
            ).fetchall()
        finally:
            conn.close()

    def _events(self, project_id: str, type_: str) -> List[sqlite3.Row]:
        conn = self.harness.db()
        try:
            return conn.execute(
                "SELECT * FROM events WHERE project_id = ? AND type = ? ORDER BY seq ASC;",
                (project_id, type_),
            ).fetchall()
        finally:
            conn.close()

    def _wait_turn_closed(self, project_id: str, count: int = 1, timeout: float = 40.0) -> List[sqlite3.Row]:
        """Wait until `count` lead turns of the project have ended; return them all."""
        def done() -> bool:
            runs = self._lead_turn_runs(project_id)
            return len(runs) >= count and all(
                r["status"] not in ("starting", "running") for r in runs[:count]
            )

        self.assertTrue(self.harness.wait_for(done, timeout=timeout, step=0.1),
                        f"lead turns did not close: {[dict(r) for r in self._lead_turn_runs(project_id)]}")
        return self._lead_turn_runs(project_id)

    def _wait_turn_running(self, project_id: str, timeout: float = 30.0) -> sqlite3.Row:
        def running() -> bool:
            return any(r["status"] == "running" for r in self._lead_turn_runs(project_id))

        self.assertTrue(self.harness.wait_for(running, timeout=timeout, step=0.05),
                        f"no running lead turn: {[dict(r) for r in self._lead_turn_runs(project_id)]}")
        return [r for r in self._lead_turn_runs(project_id) if r["status"] == "running"][0]


class TestLeadTurnDebounce(LeadTurnCase):
    """ACC: L1 - one lead turn per debounce window, never two concurrent turns."""

    def test_l1_one_turn_per_debounce_window(self) -> None:
        """ACC: L1 - a project creation plus 3 follow-up messages within 1 s are batched
        into exactly one lead turn (the debounce window), which starts at least
        `lead.wake_debounce_s` after the oldest message, and no second turn starts."""
        # Hold the scheduler so the four messages are all pending before any turn can
        # start: the debounce window is then the only thing that decides the batch.
        self.harness.hold_launch_starts()
        try:
            project_id = self._create_project("Lead turn debounce test")
            for i in range(3):
                self._follow_up(project_id, f"follow-up {i}")
            rows = self._lead_wake_rows(project_id)
            self.assertEqual(len(rows), 4, [dict(r) for r in rows])
            self.assertEqual({r["status"] for r in rows}, {"pending"})
            oldest = rows[0]["created_at"]
        finally:
            self.harness.release_launch_starts()

        runs = self._wait_turn_closed(project_id, count=1, timeout=40.0)
        turn = runs[0]
        self.assertEqual(turn["status"], "exited")
        self.assertEqual(turn["exit_code"], 0)
        self.assertIsNotNone(turn["lead_assignment_id"])

        # Exactly one turn, and it carried all four messages.
        self.assertEqual(len(runs), 1, [dict(r) for r in runs])
        rows = self._lead_wake_rows(project_id)
        self.assertEqual(len(rows), 4, [dict(r) for r in rows])
        self.assertEqual({r["delivering_run_id"] for r in rows}, {turn["id"]},
                         [dict(r) for r in rows])
        self.assertEqual({r["status"] for r in rows}, {"acked"})

        # The turn waited for the debounce window (2 s) after the oldest message.
        from mesh.core.clock import parse_utc

        waited = parse_utc(turn["started_at"]).timestamp() - parse_utc(oldest).timestamp()
        self.assertGreaterEqual(waited, 2.0, f"turn started after only {waited:.2f}s")

        # The prompt was rendered and written to the run dir.
        prompt_path = os.path.join(turn["run_dir"], "prompt.md")
        self.assertTrue(os.path.exists(prompt_path))
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("Agent Mesh lead turn", prompt)
        self.assertIn("mesh-actions", prompt)

        # No second turn starts: every message was carried by the first one.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._lead_turn_runs(project_id)) > 1,
                                  timeout=3.0, step=0.1),
            "a second lead turn started after the first carried every message",
        )

        # The lease is released and the run token revoked when the turn ends.
        conn = self.harness.db()
        try:
            lease = conn.execute("SELECT * FROM leases WHERE key = ?;",
                                 (f"leadturn:{project_id}",)).fetchone()
            self.assertIsNone(lease)
            cred = conn.execute(
                "SELECT revoked_at, scope_json FROM credentials WHERE kind = 'run_token' "
                "AND json_extract(scope_json, '$.run_id') = ?;", (turn["id"],)
            ).fetchone()
            self.assertIsNotNone(cred)
            self.assertIsNotNone(cred["revoked_at"])
            scope = json.loads(cred["scope_json"])
        finally:
            conn.close()
        self.assertEqual(scope["role"], "lead")
        self.assertEqual(scope["project_id"], project_id)
        self.assertEqual(scope["lead_assignment_id"], turn["lead_assignment_id"])

    def test_l1_no_overlapping_turns(self) -> None:
        """ACC: L1 - while a turn runs, a second batch of messages waits: the
        `leadturn:<prj>` lease keeps one turn per project at a time."""
        self._set_backoff([1, 1, 1])
        # Debounce off for this test (existing setting, testing range 0..3600), so the
        # second batch is eligible for a turn the moment it is pending: only the lease
        # (and the active-turn check) can keep a second turn from starting
        # (m3-a1-speed fix round, Linear MAJOR).
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "0", user=True)
        self.assertEqual(rc, 0, resp)
        # Held by a release file instead of a 3 s sleep (m3-a1-speed): the first turn is
        # still running through the whole observation window, whatever the load.
        gate = os.path.join(self.harness.fixtures_dir, "l1-overlap-turn")
        project_id = self._create_project(
            'FAKE {"wait_release":{"path":%s}}' % json.dumps(gate))
        first = self._wait_turn_running(project_id)
        carried = {r["id"] for r in self._lead_wake_rows(project_id)}

        # A second batch arrives while the first turn is still running.
        self._follow_up(project_id, "second batch")

        # Wait until the second batch is due (or already taken, which the checks below
        # reject): from then on every scheduler tick (about 1 s) would start a turn if
        # the lease guard let it.
        def second_batch_due() -> bool:
            from mesh.core.clock import now_iso_utc

            now = now_iso_utc()
            return any(r["id"] not in carried
                       and (r["status"] != "pending" or r["next_attempt_at"] <= now)
                       for r in self._lead_wake_rows(project_id))

        self.assertTrue(self.harness.wait_for(second_batch_due, timeout=10.0, step=0.05),
                        f"second batch never became due: {[dict(r) for r in self._lead_wake_rows(project_id)]}")
        # Observe for 3.5 s (at least three scheduler ticks) with the batch eligible.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._lead_turn_runs(project_id)) > 1, timeout=3.5, step=0.1),
            "a second lead turn started while the first was running",
        )
        conn = self.harness.db()
        try:
            lease = conn.execute("SELECT run_id FROM leases WHERE key = ?;",
                                 (f"leadturn:{project_id}",)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["run_id"], first["id"])
        runs = self._lead_turn_runs(project_id)
        self.assertEqual(len(runs), 1, [dict(r) for r in runs])
        self.assertEqual(runs[0]["status"], "running")
        # The second batch is still waiting, not handed to any turn.
        waiting = [r for r in self._lead_wake_rows(project_id) if r["id"] not in carried]
        self.assertTrue(waiting and all(r["status"] == "pending" for r in waiting),
                        [dict(r) for r in waiting])
        with open(gate, "w", encoding="utf-8"):
            pass

        # After the first turn ends, the second batch gets its own turn.
        runs = self._wait_turn_closed(project_id, count=2, timeout=40.0)
        self.assertGreaterEqual(len(runs), 2, [dict(r) for r in runs])
        # No two turns of the project overlap in time.
        spans = [(r["started_at"], r["ended_at"]) for r in runs if r["ended_at"]]
        spans.sort()
        for (_, end_a), (start_b, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end_a, start_b, f"lead turns overlap: {spans}")


class TestLeadTurnBackoff(LeadTurnCase):
    """ACC: L3 - exactly 3 attempts with backoff, then `dead` and one `lead.unreachable`."""

    def test_l3_three_attempts_then_dead(self) -> None:
        """ACC: L3 - a lead turn that always exits non-zero is retried with the §12.4
        backoff (testing-only `lead.wake_backoff_s`), then its messages go `dead` with
        exactly one `lead.unreachable` each to the user and the origin agent, and no
        fourth turn starts."""
        self._set_backoff([1, 1, 1])
        project_id = self._create_project('FAKE {"fail":{"code":3,"stderr":"lead down"}}')

        runs = self._wait_turn_closed(project_id, count=3, timeout=60.0)
        # Exactly three attempts, each a non-zero exit.
        self.assertEqual(len(runs), 3, [dict(r) for r in runs])
        for run in runs:
            self.assertEqual(run["status"], "exited")
            self.assertEqual(run["exit_code"], 3)
        from mesh.core.clock import parse_utc

        for previous, following in zip(runs, runs[1:]):
            delay = (parse_utc(following["started_at"]) - parse_utc(previous["ended_at"])).total_seconds()
            self.assertGreaterEqual(delay, 1.0, [dict(r) for r in runs])

        # The messages are dead after the third attempt.
        def all_dead() -> bool:
            rows = self._lead_wake_rows(project_id)
            return bool(rows) and all(r["status"] == "dead" for r in rows)

        self.assertTrue(self.harness.wait_for(all_dead, timeout=20.0, step=0.1),
                        [dict(r) for r in self._lead_wake_rows(project_id)])
        rows = self._lead_wake_rows(project_id)
        self.assertEqual(len(rows), 1, [dict(r) for r in rows])
        self.assertEqual(rows[0]["attempts"], 3)

        # Exactly one lead.unreachable, fanned out to the user and the origin agent.
        unreachable = self._events(project_id, "lead.unreachable")
        self.assertEqual(len(unreachable), 1, [dict(e) for e in unreachable])
        conn = self.harness.db()
        try:
            recipients = conn.execute(
                "SELECT recipient_id, status FROM outbox WHERE type = 'lead.unreachable' "
                "AND project_id = ? ORDER BY recipient_id ASC;", (project_id,)
            ).fetchall()
            user_id = conn.execute("SELECT id FROM principals WHERE kind = 'user';").fetchone()["id"]
            grok_id = conn.execute("SELECT id FROM principals WHERE name = 'grok';").fetchone()["id"]
        finally:
            conn.close()
        self.assertEqual({r["recipient_id"] for r in recipients}, {user_id, grok_id},
                         [dict(r) for r in recipients])

        # No fourth turn within 5 s.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._lead_turn_runs(project_id)) > 3, timeout=5.0, step=0.1),
            "a fourth lead turn started after the messages went dead",
        )
        self.assertEqual(len(self._lead_turn_runs(project_id)), 3)

    def test_l3_second_episode_reaches_the_user_again(self) -> None:
        """ACC: L3 - a second dead episode in the same project sends the user a second
        `lead.unreachable` message (review m2-r9 finding 5: the dedup subject is the
        run id, not the project)."""
        self._set_backoff([1, 1, 1])
        project_id = self._create_project('FAKE {"fail":{"code":3}}')

        def dead_episodes() -> int:
            return sum(1 for r in self._lead_wake_rows(project_id) if r["status"] == "dead")

        self.assertTrue(self.harness.wait_for(lambda: dead_episodes() >= 1, timeout=60.0, step=0.1),
                        [dict(r) for r in self._lead_wake_rows(project_id)])

        # A new message starts a second episode, which also goes dead.
        self._follow_up(project_id, "second episode")
        self.assertTrue(self.harness.wait_for(lambda: dead_episodes() >= 2, timeout=60.0, step=0.1),
                        [dict(r) for r in self._lead_wake_rows(project_id)])

        conn = self.harness.db()
        try:
            user_id = conn.execute("SELECT id FROM principals WHERE kind = 'user';").fetchone()["id"]
            user_msgs = conn.execute(
                "SELECT id, dedup_key FROM outbox WHERE type = 'lead.unreachable' "
                "AND project_id = ? AND recipient_id = ?;", (project_id, user_id)
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(user_msgs), 2, [dict(r) for r in user_msgs])
        self.assertEqual(len({r["dedup_key"] for r in user_msgs}), 2,
                         [dict(r) for r in user_msgs])


class TestLeadPromptCapInTurn(LeadTurnCase):
    """ACC: L6 - messages that did not fit the prompt stay `pending` for the next turn."""

    def test_l6_overflow_messages_stay_pending(self) -> None:
        """ACC: L6 - 25 pending messages of ~20 KB each do not all fit the lead prompt:
        the turn's `delivering_run_id` rows are a strict subset of the pending ones, the
        rest are still `pending` after the turn, and no `PROMPT_TOO_LONG` is raised."""
        project_id = self._create_project("Lead prompt cap in a real turn")
        body = "x" * 20000
        for i in range(25):
            self._follow_up(project_id, f"msg-{i:02d} {body}")

        runs = self._wait_turn_closed(project_id, count=1, timeout=60.0)
        turn = runs[0]
        self.assertEqual(turn["status"], "exited")
        self.assertEqual(turn["exit_code"], 0)

        rows = self._lead_wake_rows(project_id)
        # 25 follow-ups plus the project.created wake message.
        self.assertEqual(len(rows), 26, len(rows))
        delivered = [r for r in rows if r["delivering_run_id"] == turn["id"]]
        # The rest was left for later: still pending, or (on a loaded host, where the next
        # turn may start before this read) already carried by a LATER turn, never by this one.
        left = [r for r in rows if r["delivering_run_id"] != turn["id"]]
        # A strict subset went into the turn; the rest stayed pending.
        self.assertGreater(len(delivered), 0)
        self.assertLess(len(delivered), len(rows), f"all {len(rows)} messages fit the prompt")
        self.assertGreater(len(left), 0)
        self.assertEqual({r["status"] for r in delivered}, {"acked"})
        for r in left:
            if r["delivering_run_id"] is None:
                self.assertEqual(r["status"], "pending", r)
            else:
                self.assertGreater(r["delivering_run_id"], turn["id"], r)  # ids sort by creation

        # The prompt is within the adapter's limit and names what it omitted.
        prompt_path = os.path.join(turn["run_dir"], "prompt.md")
        with open(prompt_path, "rb") as f:
            prompt_bytes = f.read()
        self.assertLessEqual(len(prompt_bytes), 100_000)
        self.assertIn(b"Omitted:", prompt_bytes)
        self.assertNotIn(b"Omitted: nothing.", prompt_bytes)

        # No PROMPT_TOO_LONG anywhere: the turn ran and no task failed for it.
        conn = self.harness.db()
        try:
            too_long = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE type = 'task.failed' "
                "AND json_extract(payload_json, '$.reason') = 'prompt_too_long';"
            ).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(too_long, 0)

        # A later turn picks up messages the first one left behind.
        first_ids = {r["id"] for r in delivered}
        self.assertTrue(
            self.harness.wait_for(
                lambda: any(r["delivering_run_id"] not in (None, turn["id"])
                            for r in self._lead_wake_rows(project_id)),
                timeout=40.0, step=0.1,
            ),
            "no later turn carried the messages the first one left pending",
        )
        later = [r for r in self._lead_wake_rows(project_id)
                 if r["delivering_run_id"] not in (None, turn["id"])]
        self.assertTrue({r["id"] for r in later} & (first_ids ^ {r["id"] for r in rows}))


class TestLeadRepoWorktree(LeadTurnCase):
    """ACC: K13 - the lead's `repo/` worktree is detached and is never a task's worktree."""

    needs_git = True

    def setUp(self) -> None:
        super().setUp()
        self.tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")

    def test_k13_lead_repo_detached_during_turn(self) -> None:
        """ACC: K13 - during a lead turn of a repo project, `worktree list --porcelain`
        shows `lead/<agent>/repo` detached and not equal to any task worktree; a later
        turn refreshes it to the new integration tip, still detached."""
        assert self.fixture is not None
        repo = self.fixture.make_k0("R13")
        remote_before = self.fixture.remote_refs(repo)
        # The first turn is held by a release file instead of a 4 s sleep (m3-a1-speed);
        # the later turn finds the file and ends at once, as the old sleep let it.
        gate = os.path.join(self.harness.fixtures_dir, "k13-turn")
        project_id = self._create_project(
            'FAKE {"wait_release":{"path":%s}}' % json.dumps(gate), repo=repo)

        # A real code task of an inbox agent, started so its worktree exists.
        rc, resp = self.harness.cli("task", "create", "--project", project_id,
                                    "--title", "Code task", "--spec", "edit",
                                    "--assignee", "ibx", "--kind", "code", user=True)
        self.assertEqual(rc, 0, resp)
        task_id = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(
            lambda: self.harness.cli("task", "show", task_id, user=True)[1]["data"]["status"] == "queued",
            timeout=30.0, step=0.1))
        rc, st = self.harness.cli("task", "start", task_id, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        task_ws = os.path.realpath(st["data"]["workspace"])

        turn = self._wait_turn_running(project_id)
        worktrees = self.fixture.worktrees(repo)
        conn = self.harness.db()
        try:
            agent_id = conn.execute(
                "SELECT agent_id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;",
                (project_id,),
            ).fetchone()["agent_id"]
        finally:
            conn.close()
        lead_path = os.path.realpath(
            os.path.join(self.harness.home, "workspaces", project_id, "lead", agent_id, "repo")
        )
        by_path = {os.path.realpath(w["worktree"]): w for w in worktrees}
        self.assertIn(lead_path, by_path, [w["worktree"] for w in worktrees])
        lead_wt = by_path[lead_path]
        self.assertTrue(lead_wt.get("detached"), lead_wt)
        self.assertNotIn("branch", lead_wt)

        # It is not any task's worktree: the task worktree exists and differs, and the
        # integration worktree is a different, non-detached one.
        self.assertIn(task_ws, by_path, [w["worktree"] for w in worktrees])
        self.assertNotEqual(task_ws, lead_path)
        conn = self.harness.db()
        try:
            task_paths = [r["workspace_path"] for r in conn.execute(
                "SELECT workspace_path FROM tasks WHERE project_id = ?;", (project_id,)
            ).fetchall() if r["workspace_path"]]
        finally:
            conn.close()
        self.assertTrue(task_paths)
        for path in task_paths:
            self.assertNotEqual(os.path.realpath(path), lead_path)
        integration = os.path.realpath(
            os.path.join(self.harness.home, "workspaces", project_id, "integration")
        )
        self.assertIn(integration, by_path)
        self.assertFalse(by_path[integration].get("detached"), by_path[integration])

        # The turn's own cwd is the lead dir, and the run is a lead_turn.
        self.assertEqual(turn["kind"], "lead_turn")
        self.assertTrue(turn["run_dir"].startswith(self.harness.home))

        # After the turn the worktree is still detached.
        self.assertEqual(self._lead_turn_runs(project_id)[0]["status"], "running")
        with open(gate, "w", encoding="utf-8"):
            pass
        self._wait_turn_closed(project_id, count=1, timeout=40.0)
        by_path = {os.path.realpath(w["worktree"]): w for w in self.fixture.worktrees(repo)}
        self.assertIn(lead_path, by_path)
        self.assertTrue(by_path[lead_path].get("detached"), by_path[lead_path])

        # A commit on the integration branch, then a second turn: the refresh moves
        # `repo/` to the new tip and keeps it detached.
        with open(os.path.join(integration, "extra.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        self.fixture.git(integration, "add", "-A", identity=True)
        self.fixture.git(integration, "commit", "-m", "integration tip", identity=True)
        new_tip = self.fixture.head(integration)
        self.assertNotEqual(new_tip, self.fixture.head(repo, "main"))

        self._follow_up(project_id, "second turn")
        self._wait_turn_closed(project_id, count=2, timeout=60.0)
        by_path = {os.path.realpath(w["worktree"]): w for w in self.fixture.worktrees(repo)}
        self.assertIn(lead_path, by_path)
        self.assertTrue(by_path[lead_path].get("detached"), by_path[lead_path])
        self.assertTrue(self.fixture.is_clean(lead_path),
                        self.fixture.git(lead_path, "status", "--porcelain").stdout)
        self.assertEqual(self.fixture.head(lead_path), new_tip)
        # Remote refs are unchanged: nothing was pushed.
        self.assertEqual(self.fixture.remote_refs(repo), remote_before)


class TestLeadTurnLeftovers(LeadTurnCase):
    """ACC: L1 (exit-0 clause) - a turn that exits 0 and leaves an escapee succeeded
    (review m2-r9 finding 2)."""

    def test_l1_leftover_after_exit_zero_acks(self) -> None:
        """ACC: L1 - a lead turn that exits 0 and leaves a setsid escapee is finished
        normally: `run.leftovers_terminated`, the turn `exited` with exit_code 0, its
        messages `acked` with attempts 0, and no second turn."""
        rc, _ = self.harness.cli("config", "set", "kill.grace_s", "2", user=True)
        self.assertEqual(rc, 0)
        project_id = self._create_project(
            'FAKE {"steps": [{"spawn_setsid_grandchild": {"ignore_sigterm": true}}, {"succeed": {}}]}')

        runs = self._wait_turn_closed(project_id, count=1, timeout=60.0)
        turn = runs[0]
        self.assertEqual(turn["status"], "exited")
        self.assertEqual(turn["exit_code"], 0)
        self.assertEqual(turn["end_reason"], "exited")

        # The escapee was terminated and reported.
        self.assertTrue(self.harness.wait_for(
            lambda: bool(self._events(project_id, "run.leftovers_terminated")),
            timeout=20.0, step=0.1), "no run.leftovers_terminated event")

        # Its messages are acked on the first attempt: the turn succeeded.
        rows = self._lead_wake_rows(project_id)
        self.assertTrue(rows)
        self.assertEqual({r["status"] for r in rows}, {"acked"}, [dict(r) for r in rows])
        self.assertEqual({r["attempts"] for r in rows}, {0})
        self.assertEqual({r["delivering_run_id"] for r in rows}, {turn["id"]})

        # No retry: the turn is not re-run.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._lead_turn_runs(project_id)) > 1,
                                  timeout=3.0, step=0.1),
            "a second lead turn started after a successful one",
        )


class TestLeadRunTokenScope(LeadTurnCase):
    """ACC: L1 (lead run-token scope clause) - a lead turn's token acts only for its
    own project (review m2-r9 finding 1)."""

    def test_l1_lead_token_confined_to_its_project(self) -> None:
        """ACC: L1 - with the turn's `MESH_TOKEN` read from `/proc/<pid>/environ` (G18)
        during a sleeping turn: `inbox list` leaves a pending `lead_wake` row `pending`
        and `inbox ack` of it is rc 5, `event list --task <other project's task>` is
        rc 4, and `project list`, `approval list` and `workspace list` (also with the
        relay capability) show only the scope project; `approval show <other>` is rc 4."""
        from mesh.core.proc import get_proc_environ

        # A second project of the same origin agent, with a task of its own.
        other_id = self._create_project("Other project", title="Other")
        rc, resp = self.harness.cli("task", "create", "--project", other_id,
                                    "--title", "Other task", "--spec", "s",
                                    "--profile", "dev", user=True)
        self.assertEqual(rc, 0, resp)
        other_task = resp["data"]["id"]

        # Finish relay/approval setup before starting the timed turn. The own-project
        # approval needs its project ID, so hold launches until both requests exist.
        rc, resp = self.harness.cli("agent", "update", "flead", "--capability", "intake",
                                    "--capability", "relay", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("approval", "request", "--category", "deploy",
                                    "--summary", "Other deploy", "--operation", '{"t": "other"}',
                                    "--project", other_id, user=True)
        self.assertEqual(rc, 0, resp)
        other_approval = resp["data"]["approval_id"] if "approval_id" in resp["data"] else resp["data"]["id"]
        self.harness.hold_launch_starts()
        try:
            # Held by a release file instead of a 30 s sleep (A1 speed pass, m3-a1-speed):
            # the turn stays running for every check below and ends right after them.
            gate = os.path.join(self.harness.fixtures_dir, "l1-scope-turn")
            project_id = self._create_project(
                'FAKE {"wait_release":{"path":%s}}' % json.dumps(gate))
            rc, resp = self.harness.cli("approval", "request", "--category", "deploy",
                                        "--summary", "Own deploy", "--operation", '{"t": "own"}',
                                        "--project", project_id, user=True)
            self.assertEqual(rc, 0, resp)
        finally:
            self.harness.release_launch_starts()
        turn = self._wait_turn_running(project_id)

        env = None

        def token_ready() -> bool:
            nonlocal env
            env = get_proc_environ(turn["wrapper_pid"])
            return bool(env and env.get("MESH_TOKEN"))

        self.assertTrue(self.harness.wait_for(token_ready, timeout=10.0, step=0.05))
        self.assertIsNotNone(env)
        token = env["MESH_TOKEN"]
        self.assertEqual(env.get("MESH_PROJECT_ID"), project_id)
        self.assertNotIn("MESH_TASK_ID", env)

        # During the turn every project row is `delivering`; a follow-up adds a
        # genuinely `pending` lead_wake row for `inbox list` and `inbox ack` to leave alone.
        self._follow_up(project_id, "follow-up during the turn")
        pending = [r for r in self._lead_wake_rows(project_id) if r["status"] == "pending"]
        self.assertTrue(pending, [dict(r) for r in self._lead_wake_rows(project_id)])
        before = {r["id"]: r["status"] for r in self._lead_wake_rows(project_id)}
        rc, resp = self.harness.cli("inbox", "list", token=token)
        self.assertEqual(rc, 0, resp)
        after = {r["id"]: r["status"] for r in self._lead_wake_rows(project_id)}
        self.assertEqual(after, before, "inbox list consumed lead_wake rows")
        self.assertEqual(after[pending[0]["id"]], "pending")
        self.assertEqual({r["channel"] for r in self._lead_wake_rows(project_id)}, {"lead_wake"})
        self.assertNotIn("lead_wake", {i["channel"] for i in resp["data"]["items"]})
        rc, resp = self.harness.cli("inbox", "ack", pending[0]["id"], token=token)
        self.assertEqual(rc, 5, resp)
        self.assertEqual(resp["error"]["code"], "NOT_FOUND")
        after = {r["id"]: r["status"] for r in self._lead_wake_rows(project_id)}
        self.assertEqual(after, before, "inbox ack touched lead_wake rows")

        # Another project's task is out of scope: rc 4 SCOPE_MISMATCH.
        rc, resp = self.harness.cli("event", "list", "--task", other_task, token=token)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "SCOPE_MISMATCH")

        # project list shows only the scope project.
        rc, resp = self.harness.cli("project", "list", token=token)
        self.assertEqual(rc, 0, resp)
        self.assertEqual([p["id"] for p in resp["data"]["items"]], [project_id])

        # r9c N1: with the relay capability too, approval list and workspace list return
        # only the scope project, and another project's approval is invisible.
        rc, resp = self.harness.cli("approval", "list", token=token)
        self.assertEqual(rc, 0, resp)
        items = resp["data"]["items"]
        self.assertTrue(items, resp)
        self.assertEqual({a["project_id"] for a in items}, {project_id})
        rc, resp = self.harness.cli("approval", "list", "--project", other_id, token=token)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "SCOPE_MISMATCH")
        rc, resp = self.harness.cli("approval", "show", other_approval, token=token)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "SCOPE_MISMATCH")
        rc, resp = self.harness.cli("approval", "list", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIn(other_approval, {a["id"] for a in resp["data"]["items"]})

        rc, resp = self.harness.cli("workspace", "list", token=token)
        self.assertEqual(rc, 0, resp)
        self.assertEqual({w["project_id"] for w in resp["data"]["workspaces"]} - {None}, {project_id})
        rc, resp = self.harness.cli("workspace", "list", "--project", other_id, token=token)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "SCOPE_MISMATCH")

        # New intake and project creation are out of scope too.
        rc, resp = self.harness.cli("intake", "submit", "--title", "New", "--goal", "g",
                                    "--criterion", "C1", token=token)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "SCOPE_MISMATCH")

        # Every check ran while the turn was still running; let it end now.
        self.assertEqual(self._lead_turn_runs(project_id)[0]["status"], "running")
        with open(gate, "w", encoding="utf-8"):
            pass
        self._wait_turn_closed(project_id, count=1, timeout=60.0)
