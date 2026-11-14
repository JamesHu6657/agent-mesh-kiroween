"""Database invariants verification per plan §5.6.

Invariants checked:
- I1: tasks.status='running' <=> active task lease & active run. Leased task must be
      running, cancelling, or submitted (submitted allowed only for launch-mode tasks
      with active run in post-submit grace).
- I2: For a task lease: leases.fence = tasks.fence_seq = runs.fence.
- I3: At acquisition: slots_in_use_at_acquire < limit_at_acquire. Agent concurrency
      oversubscription requires limit_at_acquire > agents.max_concurrency. Same for global cap.
- I4: Every project/task status change has matching event with to_state = current status.
- I5: Each project has at most one active lead; active projects have exactly one.
- I6: approvals.status='consumed' => consumed_at, consumed_by, decided_via non-null.
- I7: Event hash chain is unbroken and continuous.
- I8: Launch-mode task in 'cancelling' holds a lease. No lease exists without an active run.
- I9: For a merged code task, merge_json.merge_commit exists in the repo and is an
      ancestor of the project's integration branch (M2).
"""

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, NamedTuple, Optional

from mesh.core.events import verify_event_chain

# A full SHA-1 (40 hex) or SHA-256 (64 hex) object name; anything else never reaches git argv.
# `\Z` (not `$`) so a trailing newline is rejected too.
_SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?\Z")


class Violation(NamedTuple):
    invariant: str
    message: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invariant": self.invariant,
            "message": self.message,
            "details": self.details,
        }


def check_i1_leases_and_tasks(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I1: task status running vs leases and runs."""
    violations: List[Violation] = []

    # 1. Check all tasks in status 'running'
    cursor = conn.execute("SELECT id, status, assignee_agent_id FROM tasks WHERE status = 'running';")
    for row in cursor.fetchall():
        task_id = row["id"]
        # Must have exactly one lease with key 'task:<id>'
        lease_cursor = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",))
        leases = lease_cursor.fetchall()
        if len(leases) != 1:
            violations.append(
                Violation(
                    invariant="I1",
                    message=f"Task {task_id} in 'running' status has {len(leases)} leases (expected 1)",
                    details={"task_id": task_id, "lease_count": len(leases)},
                )
            )

        # Must have exactly one run in ('starting', 'running')
        run_cursor = conn.execute(
            "SELECT id, status FROM runs WHERE task_id = ? AND status IN ('starting', 'running') AND kind IN ('task', 'external');",
            (task_id,),
        )
        active_runs = run_cursor.fetchall()
        if len(active_runs) != 1:
            violations.append(
                Violation(
                    invariant="I1",
                    message=f"Task {task_id} in 'running' status has {len(active_runs)} active runs (expected 1)",
                    details={"task_id": task_id, "active_run_count": len(active_runs)},
                )
            )

    # 2. Check all leases with key 'task:<id>'
    cursor = conn.execute("SELECT key, run_id FROM leases WHERE key LIKE 'task:%';")
    for row in cursor.fetchall():
        lease_key = row["key"]
        task_id = lease_key.split(":", 1)[1]
        run_id = row["run_id"]

        task_cursor = conn.execute(
            """
            SELECT t.id, t.status, t.assignee_agent_id, a.delivery
            FROM tasks t
            LEFT JOIN agents a ON t.assignee_agent_id = a.id
            WHERE t.id = ?;
            """,
            (task_id,),
        )
        task_row = task_cursor.fetchone()
        if not task_row:
            violations.append(
                Violation(
                    invariant="I1",
                    message=f"Lease {lease_key} references non-existent task {task_id}",
                    details={"lease_key": lease_key, "task_id": task_id},
                )
            )
            continue

        status = task_row["status"]
        if status not in ("running", "cancelling", "submitted"):
            violations.append(
                Violation(
                    invariant="I1",
                    message=f"Lease {lease_key} held for task in illegal status '{status}' (must be running, cancelling, or submitted)",
                    details={"task_id": task_id, "status": status},
                )
            )
        elif status == "submitted":
            # Allowed only for launch-mode tasks whose run is still active (post-submit grace, T7)
            delivery = task_row["delivery"]
            run_cursor = conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,))
            run_row = run_cursor.fetchone()
            run_status = run_row["status"] if run_row else None

            if delivery != "launch" or run_status not in ("starting", "running"):
                violations.append(
                    Violation(
                        invariant="I1",
                        message=f"Lease {lease_key} on submitted task {task_id} invalid: delivery='{delivery}', run_status='{run_status}'",
                        details={"task_id": task_id, "delivery": delivery, "run_status": run_status},
                    )
                )

    return violations


def check_i2_fences(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I2: leases.fence = tasks.fence_seq = runs.fence for task leases."""
    violations: List[Violation] = []

    cursor = conn.execute(
        """
        SELECT l.key, l.fence AS lease_fence, l.run_id,
               t.id AS task_id, t.fence_seq AS task_fence,
               r.fence AS run_fence
        FROM leases l
        JOIN runs r ON l.run_id = r.id
        LEFT JOIN tasks t ON r.task_id = t.id
        WHERE l.key LIKE 'task:%';
        """
    )
    for row in cursor.fetchall():
        task_id = row["task_id"]
        lease_fence = row["lease_fence"]
        task_fence = row["task_fence"]
        run_fence = row["run_fence"]

        if not (lease_fence == task_fence == run_fence):
            violations.append(
                Violation(
                    invariant="I2",
                    message=f"Fence mismatch on task {task_id}: lease.fence={lease_fence}, task.fence_seq={task_fence}, run.fence={run_fence}",
                    details={
                        "task_id": task_id,
                        "lease_fence": lease_fence,
                        "task_fence": task_fence,
                        "run_fence": run_fence,
                    },
                )
            )

    return violations


def check_i3_concurrency_and_limits(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I3: acquisition limits and oversubscription rules."""
    violations: List[Violation] = []

    # 1. At acquisition: slots_in_use_at_acquire < limit_at_acquire
    cursor = conn.execute(
        """
        SELECT key, agent_id, slots_in_use_at_acquire, limit_at_acquire,
               total_at_acquire, total_limit_at_acquire
        FROM leases
        WHERE key LIKE 'task:%';
        """
    )
    for row in cursor.fetchall():
        key = row["key"]
        slots = row["slots_in_use_at_acquire"]
        limit = row["limit_at_acquire"]
        total = row["total_at_acquire"]
        total_limit = row["total_limit_at_acquire"]

        if slots is not None and limit is not None:
            if slots >= limit:
                violations.append(
                    Violation(
                        invariant="I3",
                        message=f"Lease {key} acquired with slots_in_use ({slots}) >= limit ({limit})",
                        details={"lease_key": key, "slots_in_use": slots, "limit": limit},
                    )
                )

        if total is not None and total_limit is not None:
            if total >= total_limit:
                violations.append(
                    Violation(
                        invariant="I3",
                        message=f"Lease {key} acquired with total_at_acquire ({total}) >= total_limit ({total_limit})",
                        details={"lease_key": key, "total": total, "total_limit": total_limit},
                    )
                )

    # 2. Oversubscription rule for agents
    agent_cursor = conn.execute("SELECT id, max_concurrency FROM agents;")
    for agent_row in agent_cursor.fetchall():
        agent_id = agent_row["id"]
        max_concurrency = agent_row["max_concurrency"]

        lease_cursor = conn.execute(
            "SELECT key, limit_at_acquire FROM leases WHERE agent_id = ? AND key LIKE 'task:%';",
            (agent_id,),
        )
        leases = lease_cursor.fetchall()
        count = len(leases)
        if count > max_concurrency:
            diff = count - max_concurrency
            grandfathered = sum(
                1 for l in leases
                if l["limit_at_acquire"] is not None and l["limit_at_acquire"] > max_concurrency
            )
            if grandfathered < diff:
                violations.append(
                    Violation(
                        invariant="I3",
                        message=f"Agent {agent_id} has {count} active leases exceeding max_concurrency {max_concurrency}, but only {grandfathered} were acquired with higher limit (expected >= {diff})",
                        details={
                            "agent_id": agent_id,
                            "active_leases": count,
                            "max_concurrency": max_concurrency,
                            "grandfathered": grandfathered,
                            "required": diff,
                        },
                    )
                )

    # 3. Oversubscription rule for scheduler.max_total_runs
    settings_cursor = conn.execute("SELECT value_json FROM settings WHERE key = 'scheduler.max_total_runs';")
    setting_row = settings_cursor.fetchone()
    if setting_row:
        try:
            max_total = json.loads(setting_row["value_json"])
            if isinstance(max_total, int):
                all_leases_cursor = conn.execute(
                    "SELECT key, total_limit_at_acquire FROM leases WHERE key LIKE 'task:%';"
                )
                all_leases = all_leases_cursor.fetchall()
                total_count = len(all_leases)
                if total_count > max_total:
                    diff = total_count - max_total
                    grandfathered_total = sum(
                        1 for l in all_leases
                        # Linear full review A5: NULL = acquired while unlimited, which
                        # is above any cap set later, so it is grandfathered too.
                        if l["total_limit_at_acquire"] is None or l["total_limit_at_acquire"] > max_total
                    )
                    if grandfathered_total < diff:
                        violations.append(
                            Violation(
                                invariant="I3",
                                message=f"Total active task leases ({total_count}) exceed scheduler.max_total_runs ({max_total}), and grandfathered ({grandfathered_total}) < diff ({diff})",
                                details={
                                    "total_leases": total_count,
                                    "max_total_runs": max_total,
                                    "grandfathered": grandfathered_total,
                                },
                            )
                        )
        except Exception:
            pass

    return violations


def check_i4_status_events(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I4: latest state change event matches current status for projects and tasks."""
    violations: List[Violation] = []

    # 1. Projects
    cursor = conn.execute("SELECT id, status FROM projects;")
    for row in cursor.fetchall():
        project_id = row["id"]
        status = row["status"]

        evt_cursor = conn.execute(
            """
            SELECT to_state FROM events
            WHERE project_id = ? AND task_id IS NULL AND to_state IS NOT NULL
            ORDER BY seq DESC LIMIT 1;
            """,
            (project_id,),
        )
        evt_row = evt_cursor.fetchone()
        if not evt_row:
            violations.append(
                Violation(
                    invariant="I4",
                    message=f"Project {project_id} has current status '{status}' but no state transition events exist",
                    details={"project_id": project_id, "status": status},
                )
            )
        elif evt_row["to_state"] != status:
            violations.append(
                Violation(
                    invariant="I4",
                    message=f"Project {project_id} status is '{status}' but latest event to_state is '{evt_row['to_state']}'",
                    details={"project_id": project_id, "status": status, "event_to_state": evt_row["to_state"]},
                )
            )

    # 2. Tasks
    cursor = conn.execute("SELECT id, status FROM tasks;")
    for row in cursor.fetchall():
        task_id = row["id"]
        status = row["status"]

        evt_cursor = conn.execute(
            """
            SELECT to_state FROM events
            WHERE task_id = ? AND to_state IS NOT NULL
            ORDER BY seq DESC LIMIT 1;
            """,
            (task_id,),
        )
        evt_row = evt_cursor.fetchone()
        if not evt_row:
            violations.append(
                Violation(
                    invariant="I4",
                    message=f"Task {task_id} has current status '{status}' but no state transition events exist",
                    details={"task_id": task_id, "status": status},
                )
            )
        elif evt_row["to_state"] != status:
            violations.append(
                Violation(
                    invariant="I4",
                    message=f"Task {task_id} status is '{status}' but latest event to_state is '{evt_row['to_state']}'",
                    details={"task_id": task_id, "status": status, "event_to_state": evt_row["to_state"]},
                )
            )

    return violations


def check_i5_lead_assignments(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I5: at most one active lead; active projects have exactly one."""
    violations: List[Violation] = []

    cursor = conn.execute("SELECT id, status FROM projects;")
    for row in cursor.fetchall():
        project_id = row["id"]
        status = row["status"]

        lead_cursor = conn.execute(
            "SELECT id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL;",
            (project_id,),
        )
        active_leads = lead_cursor.fetchall()
        count = len(active_leads)

        if count > 1:
            violations.append(
                Violation(
                    invariant="I5",
                    message=f"Project {project_id} has {count} active lead assignments (at most 1 allowed)",
                    details={"project_id": project_id, "active_leads": count},
                )
            )
        elif status == "active" and count != 1:
            violations.append(
                Violation(
                    invariant="I5",
                    message=f"Active project {project_id} has {count} active lead assignments (exactly 1 required)",
                    details={"project_id": project_id, "active_leads": count},
                )
            )

    return violations


def check_i6_consumed_approvals(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I6: approvals.status='consumed' => consumed_at, consumed_by, decided_via non-null."""
    violations: List[Violation] = []

    cursor = conn.execute(
        """
        SELECT id, consumed_at, consumed_by, decided_via
        FROM approvals
        WHERE status = 'consumed';
        """
    )
    for row in cursor.fetchall():
        app_id = row["id"]
        c_at = row["consumed_at"]
        c_by = row["consumed_by"]
        d_via = row["decided_via"]

        if c_at is None or c_by is None or d_via is None:
            violations.append(
                Violation(
                    invariant="I6",
                    message=f"Consumed approval {app_id} has missing fields: consumed_at={c_at}, consumed_by={c_by}, decided_via={d_via}",
                    details={
                        "approval_id": app_id,
                        "consumed_at": c_at,
                        "consumed_by": c_by,
                        "decided_via": d_via,
                    },
                )
            )

    return violations


def check_i7_event_hash_chain(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I7: unbroken and continuous event hash chain."""
    violations: List[Violation] = []
    raw_violations = verify_event_chain(conn)
    for raw in raw_violations:
        violations.append(
            Violation(
                invariant="I7",
                message=raw["message"],
                details=raw,
            )
        )
    return violations


def check_i8_cancelling_and_leases(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I8: cancelling launch tasks hold leases; no lease without active run."""
    violations: List[Violation] = []

    # 1. Launch-mode task in cancelling holds a lease
    cursor = conn.execute(
        """
        SELECT t.id, t.assignee_agent_id, a.delivery
        FROM tasks t
        LEFT JOIN agents a ON t.assignee_agent_id = a.id
        WHERE t.status = 'cancelling';
        """
    )
    for row in cursor.fetchall():
        task_id = row["id"]
        delivery = row["delivery"]
        if delivery == "launch":
            lease_cursor = conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",))
            if not lease_cursor.fetchone():
                violations.append(
                    Violation(
                        invariant="I8",
                        message=f"Launch-mode task {task_id} in status 'cancelling' does not hold a lease",
                        details={"task_id": task_id, "delivery": delivery},
                    )
                )

    # 2. No lease exists without an active run (starting or running)
    cursor = conn.execute(
        """
        SELECT l.key, l.run_id, r.status AS run_status
        FROM leases l
        LEFT JOIN runs r ON l.run_id = r.id;
        """
    )
    for row in cursor.fetchall():
        key = row["key"]
        run_id = row["run_id"]
        run_status = row["run_status"]

        if run_status not in ("starting", "running"):
            violations.append(
                Violation(
                    invariant="I8",
                    message=f"Lease {key} exists on non-active run {run_id} (run status: '{run_status}')",
                    details={"lease_key": key, "run_id": run_id, "run_status": run_status},
                )
            )

    return violations


def check_i9_merged_code_tasks(conn: sqlite3.Connection) -> List[Violation]:
    """Verify I9: a merged code task's merge commit exists and is an ancestor of the integration branch.

    No git process is started when there is no merged code task.
    """
    violations: List[Violation] = []

    cursor = conn.execute(
        """
        SELECT t.id AS task_id, t.merge_json AS merge_json,
               p.id AS project_id, p.repo_path AS repo_path,
               p.integration_branch AS integration_branch
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.kind = 'code' AND t.status = 'merged';
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return violations

    from mesh.core.gitops import run_git

    for row in rows:
        task_id = row["task_id"]
        project_id = row["project_id"]
        repo_path = row["repo_path"]
        integration_branch = row["integration_branch"]

        def _violation(reason: str, **extra: Any) -> None:
            details: Dict[str, Any] = {"task_id": task_id, "project_id": project_id, "reason": reason}
            details.update(extra)
            violations.append(
                Violation(
                    invariant="I9",
                    message=f"Merged code task {task_id}: {reason}",
                    details=details,
                )
            )

        merge_commit = None
        raw_merge = row["merge_json"]
        if not raw_merge:
            _violation("merge_json is missing")
            continue
        try:
            merge_data = json.loads(raw_merge)
        except (TypeError, ValueError):
            _violation("merge_json is not valid JSON")
            continue
        if isinstance(merge_data, dict):
            merge_commit = merge_data.get("merge_commit")
        if not merge_commit or not isinstance(merge_commit, str):
            _violation("merge_json.merge_commit is missing")
            continue
        if not _SHA_RE.match(merge_commit):
            _violation("merge_commit is not a sha", merge_commit=merge_commit)
            continue

        if not repo_path:
            _violation("project has no repo_path", merge_commit=merge_commit)
            continue
        if not integration_branch:
            _violation("project has no integration_branch", merge_commit=merge_commit)
            continue
        if not os.path.isdir(repo_path):
            _violation("repo_path does not exist", repo_path=repo_path, merge_commit=merge_commit)
            continue

        try:
            exists = run_git(["cat-file", "-e", f"{merge_commit}^{{commit}}"], cwd=repo_path)
        except Exception as exc:  # git missing, timeout, disallowed args
            _violation(f"git cat-file failed: {exc}", merge_commit=merge_commit)
            continue
        if exists.returncode != 0:
            _violation("merge_commit does not exist in the repo", merge_commit=merge_commit)
            continue

        try:
            ancestor = run_git(
                ["merge-base", "--is-ancestor", merge_commit, f"refs/heads/{integration_branch}"],
                cwd=repo_path,
            )
        except Exception as exc:
            _violation(f"git merge-base failed: {exc}", merge_commit=merge_commit)
            continue
        if ancestor.returncode == 1:
            _violation(
                "merge_commit is not an ancestor of the integration branch",
                merge_commit=merge_commit,
                integration_branch=integration_branch,
            )
        elif ancestor.returncode != 0:
            stderr_tail = (ancestor.stderr or "").strip().splitlines()[-1:] or [""]
            _violation(
                f"git merge-base failed (rc {ancestor.returncode}): {stderr_tail[0]}",
                merge_commit=merge_commit,
                integration_branch=integration_branch,
            )

    return violations


#: Cap on the merge commits examined per task by the reverse I9 check.
_I9_REVERSE_MERGE_CAP = 200


def _superseded_by_merged(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the task's latest ``task.closed`` event names a superseding task
    (``task close --superseded-by``) and that task is ``merged`` in the same project."""
    ev = conn.execute("SELECT payload_json FROM events WHERE task_id = ? AND type = 'task.closed' "
                      "ORDER BY seq DESC LIMIT 1;", (task_id,)).fetchone()
    if ev is None:
        return False
    try:
        other = json.loads(ev["payload_json"] or "{}").get("superseded_by")
    except (TypeError, ValueError):
        return False
    if not other:
        return False
    row = conn.execute(
        "SELECT 1 FROM tasks o JOIN tasks t ON t.id = ? WHERE o.id = ? AND o.status = 'merged' "
        "AND o.project_id = t.project_id;", (task_id, other)).fetchone()
    return row is not None


def check_i9_integrated_unmerged_tasks(conn: sqlite3.Connection,
                                       project_id: Optional[str] = None) -> List[Violation]:
    """Reverse I9 (Carbon P1-3): a code task that is not ``merged`` but whose own commits are
    already in its project's integration branch.

    A merge commit of ``refs/heads/<integration>`` that came after the task's ``base_commit``
    and whose second parent is a commit of the task's branch (reachable from the branch tip,
    not from ``base_commit``) means the task's work entered integration. If the task is
    anything but ``merged`` (``merging`` is the merge in flight and is skipped, and so is a
    task closed with ``task close --superseded-by`` a ``merged`` task; ``cancelled``
    and ``merge_failed`` are the dangerous ones: ``project deliver`` would hand the work to
    the base branch) that is a violation. No git process starts without such a candidate.
    ``project_id`` limits the check to one project (``project deliver`` uses it as a gate).
    """
    violations: List[Violation] = []
    rows = conn.execute(
        """
        SELECT t.id AS task_id, t.status AS status, t.branch AS branch, t.base_commit AS base_commit,
               p.id AS project_id, p.repo_path AS repo_path, p.integration_branch AS integration_branch
        FROM tasks t JOIN projects p ON t.project_id = p.id
        WHERE t.kind = 'code' AND t.status NOT IN ('merged', 'merging')
          AND t.branch IS NOT NULL AND t.base_commit IS NOT NULL
          AND p.repo_path IS NOT NULL AND p.integration_branch IS NOT NULL
          AND (? IS NULL OR p.id = ?)
        ORDER BY t.id;
        """,
        (project_id, project_id),
    ).fetchall()
    if not rows:
        return violations

    from mesh.core.gitops import run_git

    def git(args: List[str], cwd: str) -> Any:
        return run_git(args, cwd=cwd)

    for row in rows:
        repo = row["repo_path"]
        if not os.path.isdir(repo) or not _SHA_RE.match(row["base_commit"] or ""):
            continue
        if row["status"] == "cancelled" and _superseded_by_merged(conn, row["task_id"]):
            continue  # closed with `task close --superseded-by`: its work came in through that merged task
        try:
            tip = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{row['branch']}^{{commit}}"], repo)
            if tip.returncode != 0:
                continue  # the branch is gone (pruned): nothing to compare
            tip_sha = tip.stdout.strip()
            merges = git(["log", "--merges", "--first-parent", "--format=%H %P", "-n", str(_I9_REVERSE_MERGE_CAP),
                          f"refs/heads/{row['integration_branch']}", f"^{row['base_commit']}"], repo)
            if merges.returncode != 0:
                continue
            for line in merges.stdout.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                for parent in parts[2:]:
                    in_branch = git(["merge-base", "--is-ancestor", parent, tip_sha], repo).returncode == 0
                    in_base = git(["merge-base", "--is-ancestor", parent, row["base_commit"]], repo).returncode == 0
                    if in_branch and not in_base:
                        violations.append(Violation(
                            invariant="I9",
                            message=(f"Code task {row['task_id']} is '{row['status']}' but its work is already in "
                                     f"the integration branch (merge commit {parts[0]})"),
                            details={"task_id": row["task_id"], "project_id": row["project_id"],
                                     "status": row["status"], "reason": "integrated_but_not_merged",
                                     "merge_commit": parts[0], "integration_branch": row["integration_branch"]},
                        ))
                        break
                else:
                    continue
                break
        except Exception:  # git missing or timed out: the forward I9 check reports such repos
            continue
    return violations


def check_invariants(conn: sqlite3.Connection) -> List[Violation]:
    """Run all invariant checks (I1-I9) on the database.

    Returns:
        List of Violation objects (empty if valid).
    """
    violations: List[Violation] = []
    violations.extend(check_i1_leases_and_tasks(conn))
    violations.extend(check_i2_fences(conn))
    violations.extend(check_i3_concurrency_and_limits(conn))
    violations.extend(check_i4_status_events(conn))
    violations.extend(check_i5_lead_assignments(conn))
    violations.extend(check_i6_consumed_approvals(conn))
    violations.extend(check_i7_event_hash_chain(conn))
    violations.extend(check_i8_cancelling_and_leases(conn))
    violations.extend(check_i9_merged_code_tasks(conn))
    violations.extend(check_i9_integrated_unmerged_tasks(conn))
    return violations
