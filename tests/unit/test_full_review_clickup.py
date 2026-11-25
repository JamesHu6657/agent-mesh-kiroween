import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import mesh.constants as c
from mesh.core import db, invariants
from mesh.core.approvals import (
    compute_user_approval_mac,
    stored_op_json,
    verify_stored_op_json,
)
from mesh.core.canonical import compute_op_hash
from mesh.core.events import record_event
from mesh.core.redact import LINE_CAP, StreamRedactor, redact
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.audit import handle_audit_verify
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.project import handle_project_deliver
from mesh.errors import MeshError


def _make_ctx(conn, home: str, cmd: str, args: dict, k_user: bytes = b"k" * 32) -> HandlerContext:
    return HandlerContext(
        conn=conn,
        principal={"id": "user", "kind": "user"},
        actor_kind="user",
        via="direct",
        request_id="req_test",
        home=home,
        cmd=cmd,
        args=args,
        k_user=k_user,
    )



def _seed_project_and_task(conn, repo: str = "", base_sha: str = "", status: str = "cancelled") -> None:
    ts = "2026-01-01T00:00:00Z"
    conn.execute(
        "INSERT INTO principals (id, kind, name, created_at) VALUES (?, ?, ?, ?);",
        ("lead_1", "agent", "Lead", ts),
    )
    conn.execute(
        "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?);",
        ("lead_1", "codex", "inbox", 1, 1, ts, ts),
    )
    conn.execute(
        "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, "
        "created_by, created_via, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        ("proj_1", "p", "g", "active", repo or None, "main", "mesh/proj_1/integration",
         "user", "direct", ts, ts),
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
        "branch, base_commit, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        ("task_1", "proj_1", "t", "s", "code", status, "edit", 60, 1,
         "task_br", base_sha or None, "user", ts, ts),
    )
    conn.commit()


def _seed_legacy_consumed_approval(conn, k_user: bytes, *, include_op_hash_in_decided: bool = False) -> str:
    aid = "apr_legacy_1"
    op_doc, op_hash = compute_op_hash(
        op="task.cancel",
        requested_by="lead_1",
        params={"task_id": "task_1"},
        project_id="proj_1",
        target_id="task_1",
        category="delete",
    )
    op_json_str = stored_op_json(op_doc, op_hash)
    decided_at = "2026-01-01T00:01:00Z"
    user_mac = compute_user_approval_mac(k_user, aid, op_hash, "approved", decided_at)

    conn.execute(
        "INSERT INTO approvals (id, project_id, task_id, category, op_type, op_hash, op_json, "
        "summary, requested_by, requested_at, expires_at, status, decided_by, decided_via, "
        "decided_at, user_mac, consumed_at, consumed_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
        (
            aid, "proj_1", "task_1", "delete", "task.cancel", op_hash, op_json_str,
            "cancel task_1", "lead_1", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z",
            "consumed", "user", "direct", decided_at, user_mac,
            "2026-01-01T00:02:00Z", "lead_1",
        ),
    )
    record_event(
        conn,
        event_type="approval.requested",
        actor_id="lead_1",
        actor_kind="agent",
        via="run_token",
        project_id="proj_1",
        task_id="task_1",
        approval_id=aid,
        payload={"op_hash": op_hash, "op": "task.cancel", "category": "delete"},
        ts="2026-01-01T00:00:00Z",
    )
    dec_payload = {"decision": "approved"}
    if include_op_hash_in_decided:
        dec_payload["op_hash"] = op_hash
    record_event(
        conn,
        event_type="approval.decided",
        actor_id="user",
        actor_kind="user",
        via="direct",
        project_id="proj_1",
        task_id="task_1",
        approval_id=aid,
        payload=dec_payload,
        k_user=k_user,
        ts=decided_at,
    )
    conn.commit()
    return aid, op_hash


class TestFullReviewClickUp(unittest.TestCase):
    # --- P1-1 ---
    def test_p1_1_common_credential_formats_redacted(self):
        cases = [
            ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
            ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
            ("STRIPE_SECRET_KEY=sk_live_1234567890abcdef", "sk_live_1234567890abcdef"),
            ("STRIPE_RESTRICTED=rk_live_1234567890abcdef", "rk_live_1234567890abcdef"),
            ("GITHUB_TOKEN=github_pat_11ABCDEFG0123456789_abcdefghijklmnop", "github_pat_"),
            ("GITLAB_TOKEN=glpat-1234567890abcdefghij", "glpat-"),
            ("HF_TOKEN=hf_1234567890abcdefghijklmnop", "hf_1234567890"),
            ("NPM_TOKEN=npm_1234567890abcdefghijklmnop", "npm_1234567890"),
            ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ('{"access_token": "ya29.abcdef123", "refresh_token": "1//0gabcdef123"}', "ya29.abcdef123"),
        ]
        for raw, secret in cases:
            out = redact(raw, (), jsonl=True)
            self.assertNotIn(secret, out, f"Secret leaked in: {out!r}")
            self.assertIn("[REDACTED]", out)

        # idempotency_key must not be falsely redacted
        safe_json = '{"idempotency_key": "act:run1:0"}'
        self.assertEqual(redact(safe_json, (), jsonl=True), safe_json)

    def test_p1_1_stored_op_json_fixed_point_and_linearity(self):
        op_doc, op_hash = compute_op_hash(
            op="task.create",
            requested_by="lead_1",
            params={
                "cmd": "Authorization: Basic dXNlcjpwYXNz",
                "env": {"access_token": "ya29.secret_value_123"},
            },
            project_id="proj_1",
            target_id=None,
        )
        stored = stored_op_json(op_doc, op_hash)
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

        # 64KB worst-case linearity (< 0.5s) and output <= LINE_CAP
        for line in [
            "a" * LINE_CAP,
            ("sk-" * (LINE_CAP // 3))[:LINE_CAP],
            ("secret= " * (LINE_CAP // 8))[:LINE_CAP],
        ]:
            t0 = time.monotonic()
            out = redact(line, (), jsonl=True)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertLessEqual(len(out), LINE_CAP)

    # --- P2-1 ---
    def test_p2_1_redaction_boundaries_stay_fail_closed(self):
        # sk- false positives on words like "risk-assessment-framework" are kept on
        # purpose: a left boundary or casing heuristic lets glued real keys leak
        # (test_blind_review_unit property tests), so redaction stays fail-closed.
        self.assertEqual(redact("OPENAI_API_KEY=sk-1234567890abcdef", (), jsonl=False),
                         "OPENAI_API_KEY=[REDACTED]")

        # Non-boundary suffixes and false-arm words must be preserved
        for raw in ("unpassword:\nhello\n", "word2secret:\nhello\n", "nonsecret\n= value\n"):
            self.assertEqual(redact(raw, (), jsonl=False), raw)

        # Concatenated bearer after an exact secret still redacts the bearer token
        concat = "word plainsecretvalueBearer " + ("X" * 40)
        out_concat = redact(concat, [("PLAIN", "plainsecretvalue")], jsonl=False)
        self.assertNotIn("XXXXXXXX", out_concat)

        # Keyword concatenated after non-separator characters still redacts assignment (fail closed)
        glued = "VVVVVVVVpassword  :  " + ("V" * 40)
        out_glued = redact(glued, (), jsonl=False)
        self.assertNotIn("V" * 40, out_glued)

    # --- P2-2 ---
    def test_p2_2_legacy_consumed_approval_accepted_and_modern_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.init_db(os.path.join(tmp, "mesh.db"))
            try:
                k_user = b"k" * 32
                _seed_project_and_task(conn)
                aid, op_hash = _seed_legacy_consumed_approval(conn, k_user, include_op_hash_in_decided=False)

                res = handle_audit_verify(_make_ctx(conn, tmp, "audit.verify", {}, k_user=k_user))
                self.assertEqual(res.data["approvals_verified"], 1)

                with self.assertRaises(MeshError) as cm:
                    handle_approval_approve(_make_ctx(conn, tmp, "approval.approve", {"approval_id": aid, "op_hash": op_hash}, k_user=k_user))
                self.assertEqual(cm.exception.code, c.APPROVAL_CONSUMED)
            finally:
                conn.close()

        # Modern approval (op_hash in approval.decided) missing approval.consumed must still fail audit verify
        with tempfile.TemporaryDirectory() as tmp2:
            conn2 = db.init_db(os.path.join(tmp2, "mesh.db"))
            try:
                k_user = b"k" * 32
                _seed_project_and_task(conn2)
                _seed_legacy_consumed_approval(conn2, k_user, include_op_hash_in_decided=True)
                with self.assertRaises(MeshError) as cm2:
                    handle_audit_verify(_make_ctx(conn2, tmp2, "audit.verify", {}, k_user=k_user))
                self.assertEqual(cm2.exception.code, c.AUDIT_MISMATCH)
            finally:
                conn2.close()

    # --- P2-3 ---
    def test_p2_3_reverse_i9_fails_closed_on_cap_or_git_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)

            def git(*args):
                cp = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
                return cp.stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.com")
            Path(repo, "f.txt").write_text("base\n")
            git("add", "f.txt")
            git("commit", "-q", "-m", "base")
            base_sha = git("rev-parse", "HEAD")

            git("checkout", "-q", "-b", "mesh/proj_1/integration")
            for i in range(4):
                b = f"other_{i}"
                git("checkout", "-q", "-b", b, base_sha)
                Path(repo, f"o_{i}.txt").write_text(f"{i}\n")
                git("add", f"o_{i}.txt")
                git("commit", "-q", "-m", f"other {i}")
                git("checkout", "-q", "mesh/proj_1/integration")
                git("merge", "-q", "--no-ff", b, "-m", f"merge {i}")

            git("checkout", "-q", "-b", "task_br", base_sha)
            Path(repo, "t.txt").write_text("task work\n")
            git("add", "t.txt")
            git("commit", "-q", "-m", "task work")

            conn = db.init_db(os.path.join(tmp, "mesh.db"))
            try:
                _seed_project_and_task(conn, repo=repo, base_sha=base_sha)

                # git error / timeout must fail closed as unverifiable and block project deliver
                with mock.patch("mesh.core.gitops.run_git", side_effect=RuntimeError("git timeout")):
                    viols_err = invariants.check_i9_integrated_unmerged_tasks(conn, "proj_1")
                    self.assertEqual(len(viols_err), 1)
                    self.assertEqual(viols_err[0].details["reason"], "unverifiable")
                    with self.assertRaises(MeshError) as cm:
                        handle_project_deliver(_make_ctx(conn, tmp, "project.deliver", {"project_id": "proj_1"}))
                    self.assertEqual(cm.exception.code, c.INVALID_TRANSITION)

                # Clean stale unmerged branch with integration merges must allow project deliver
                viols = invariants.check_i9_integrated_unmerged_tasks(conn, "proj_1")
                self.assertEqual(viols, [])
                res = handle_project_deliver(_make_ctx(conn, tmp, "project.deliver", {"project_id": "proj_1"}))
                self.assertEqual(res.data["project_id"], "proj_1")
            finally:
                conn.close()

    # --- P2-4 ---
    def test_p2_4_multiline_exact_value_linear_performance(self):
        key_500 = "\n".join(f"line_{i:04d}_secret_chunk_abcdefgh" for i in range(500))
        log_2mb = ("hello world " * 80 + "\n") * 2000
        t0 = time.monotonic()
        out = redact(log_2mb, [("JWT_PRIVATE_KEY", key_500)], jsonl=True)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, f"2MB redaction with 500-line key took {elapsed:.2f}s")
        self.assertEqual(out, log_2mb)

        # Individual lines of the 500-line key are still redacted, and StreamRedactor matches redact
        sample = "leak: line_0250_secret_chunk_abcdefgh\n"
        r_batch = redact(sample, [("JWT_PRIVATE_KEY", key_500)], jsonl=True)
        self.assertNotIn("line_0250_secret_chunk_abcdefgh", r_batch)
        sr = StreamRedactor([("JWT_PRIVATE_KEY", key_500)], jsonl=True)
        r_stream = sr.feed(sample[:12]) + sr.feed(sample[12:]) + sr.flush()
        self.assertEqual(r_stream, r_batch)


if __name__ == "__main__":
    unittest.main()
