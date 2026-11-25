"""Full-codebase review r4 (fix/full-review-r1 @ 56f8564) regressions."""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from mesh.core.redact import LINE_CAP, StreamRedactor, redact

MESH_PROC_LOCK = "shared"  # reads /proc (refresh_run_caches)


def both(raw: str, exact=None) -> str:
    exact = list(exact or [])
    out = redact(raw, exact, jsonl=True)
    sr = StreamRedactor(exact, jsonl=True)
    streamed = "".join(sr.feed(raw[i:i + 997]) for i in range(0, len(raw), 997)) + sr.flush()
    assert streamed == out, (streamed[:200], out[:200])
    return out


class TestR4P1_2EscapeForms(unittest.TestCase):
    def test_ascii_escaped_value_across_json_syntax(self) -> None:
        out = both(json.dumps(["café", "bar123"]) + "\n", [("S", 'café", "bar123')])
        self.assertNotIn("bar123", out)

    def test_escaped_slash_across_json_syntax(self) -> None:
        out = both('["foo\\/bar","baz123"]\n', [("S", 'foo/bar","baz123')])
        self.assertNotIn("baz123", out)

    def test_surrogate_pair_value_in_a_non_json_line(self) -> None:
        line = "INFO " + json.dumps({"x": "\U0001F511secret123"}) + "\n"
        self.assertIn("\\ud83d\\udd11", line)
        out = both(line, [("E", "\U0001F511secret123")])
        self.assertNotIn("secret123", out)

    def test_fallback_line_never_keeps_an_escaped_value(self) -> None:
        raw = json.dumps({"a": "aaa", "c": "bbb", "e": "\U0001F511_my_emoji_secret"}) + "\n"
        out = both(raw, [("S1", 'aaa", "c": "bbb'), ("S2", "\U0001F511_my_emoji_secret")])
        self.assertNotIn("bbb", out)
        self.assertNotIn("_my_emoji_secret", out)


class TestR4P1_3LoneSurrogate(unittest.TestCase):
    def test_redacted_line_stays_encodable_json(self) -> None:
        out = both('{"msg": "bad \\ud83d", "api_key": "secret123"}\n')
        out.encode("utf-8")  # no UnicodeEncodeError
        self.assertNotIn("secret123", out)
        self.assertEqual(json.loads(out)["api_key"], "[REDACTED]")


class TestR4P1_4Linear(unittest.TestCase):
    def test_pathological_lines_are_fast(self) -> None:
        cases = [
            "sk-" * 21840,
            '{"a": "' + "[REDACTED]" * 2000 + " " + "sk-" * 15000 + '"}',
            "secret=" * 9360,
            "password: " * 6500,
            "bearer " * 9000,
            "x" * 1000 + ("sk-" + "a" * 17 + " ") * 2900,
        ]
        for raw in cases:
            self.assertLessEqual(len(raw), LINE_CAP)
            t0 = time.monotonic()
            both(raw + "\n")
            self.assertLess(time.monotonic() - t0, 2.0, raw[:40])

    def test_keyword_at_a_run_end_still_redacts_the_next_value(self) -> None:
        out = both("password=x;secret= hunter22zz\n")
        self.assertNotIn("hunter22zz", out)


class TestR4P1_5Arming(unittest.TestCase):
    def test_empty_credential_field_keeps_the_prompt_armed(self) -> None:
        for mid in ('"api_key": ""', '"api_key": []'):
            out = both('{"prompt": "Enter password:", ' + mid + ', "input": "my_custom_pass_123"}\n')
            self.assertNotIn("my_custom_pass_123", out)
            json.loads(out)

    def test_a_key_named_bearer_does_not_arm(self) -> None:
        raw = '{"bearer": false, "name": "my-service-name"}\n'
        self.assertEqual(both(raw), raw)

    def test_json_event_after_an_over_long_line_stays_valid(self) -> None:
        ev = '{"type": "result", "result": "done"}\n'
        out = both("y" * (LINE_CAP + 10) + "\n" + ev)
        self.assertTrue(out.endswith(ev), out[-80:])

    def test_armed_value_on_the_next_json_line(self) -> None:
        out = both('password:\n{"v": "my_custom_pass_123"}\n')
        self.assertNotIn("my_custom_pass_123", out)
        json.loads(out.splitlines()[1])
        out = both('{"prompt": "password:"}\n{"v": "hunter2zz"}\nhunter3zz\n')
        self.assertNotIn("hunter2zz", out)
        out = both('{"prompt": "password:"}\nhunter3zz\n')
        self.assertNotIn("hunter3zz", out)

    def test_non_json_credential_members(self) -> None:
        for raw in ('INFO {"secret key": ["hunter2zz"]}', 'INFO {"secret:v1": ["hunter2zz"]}',
                    'INFO {"password": token_hunter2zz}', 'INFO {"password": nullhunter2zz}'):
            self.assertNotIn("hunter2zz", both(raw + "\n"), raw)
        raw = 'INFO {"password": null, "n": -1.5e3}\n'
        self.assertEqual(both(raw), raw)


class TestR4P1_6InboxLeadWake(unittest.TestCase):
    def setUp(self) -> None:
        from mesh.core.db import init_db

        self.tmp = tempfile.mkdtemp(prefix="mh-r4i-")
        self.conn = init_db(os.path.join(self.tmp, "mesh.db"))
        c = self.conn
        c.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1','user','u','now');")
        c.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('lead1','agent','lead1','now');")
        c.execute("INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                  "VALUES ('lead1','none','launch',1,1,'now','now');")
        c.execute("INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, "
                  "updated_at) VALUES ('prj_1','P','G','active','usr_1','direct',1,'now','now');")
        c.execute("INSERT INTO events (seq, id, ts, type, project_id, actor_id, actor_kind, via, prev_hash, hash) "
                  "VALUES (1, 'evt_1', 'now', 'project.created', 'prj_1', 'usr_1', 'user', 'direct', 'x', 'y');")
        for mid, ch in (("msg_w", "lead_wake"), ("msg_i", "inbox")):
            c.execute("INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, type, "
                      "payload_json, status, attempts, next_attempt_at, created_at) "
                      "VALUES (?, 1, 'lead1', ?, ?, 'prj_1', 'project.created', '{}', 'pending', 0, 'now', 'now');",
                      (mid, ch, "d_" + mid))

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def ctx(self, cmd: str, args: dict):
        from mesh.daemon.handlers.base import HandlerContext

        return mock.Mock(spec=HandlerContext, conn=self.conn, cmd=cmd, args=args, dry_run=False,
                         principal={"id": "lead1", "kind": "agent", "name": "lead1"}, actor_kind="agent",
                         via="token", approval=None, request_id="req_1", effects=[], testing_mode=True)

    def status(self, mid: str) -> str:
        return self.conn.execute("SELECT status FROM outbox WHERE id = ?;", (mid,)).fetchone()[0]

    def test_agent_token_never_touches_lead_wake(self) -> None:
        import mesh.constants as c
        from mesh.daemon.handlers.inbox import handle_inbox_ack, handle_inbox_list
        from mesh.errors import MeshError

        res = handle_inbox_list(self.ctx("inbox.list", {}))
        ids = [m["id"] for m in (res.data.get("messages") or [])]
        self.assertNotIn("msg_w", ids)
        self.assertEqual(self.status("msg_w"), "pending")
        with self.assertRaises(MeshError) as cm:
            handle_inbox_ack(self.ctx("inbox.ack", {"messages": ["msg_w"]}))
        self.assertEqual(cm.exception.code, c.NOT_FOUND)
        self.assertEqual(self.status("msg_w"), "pending")


class TestR4P2_1ReusedPid(unittest.TestCase):
    def test_pid_with_another_starttime_is_not_the_run(self) -> None:
        from mesh.core.proc import get_proc_stat
        from mesh.daemon.server import DaemonServer

        pid = os.getpid()
        st = int(get_proc_stat(pid)["starttime"])
        fake = mock.Mock()
        fake.db_conn.execute.return_value.fetchall.return_value = [
            {"id": "run_old", "wrapper_pid": pid, "proc_starttime": st + 1, "status": "running", "run_dir": ""},
            {"id": "run_live", "wrapper_pid": pid, "proc_starttime": st, "status": "running", "run_dir": ""},
            {"id": "run_unknown", "wrapper_pid": pid, "proc_starttime": None, "status": "running", "run_dir": ""},
        ]
        fake.known_run_ids, fake.active_runs = set(), {}
        DaemonServer.refresh_run_caches(fake)
        self.assertNotIn("run_old", fake.active_runs)
        self.assertEqual(fake.active_runs.get("run_live"), pid)
        self.assertEqual(fake.active_runs.get("run_unknown"), pid)  # unknown: fail closed


class TestR4P2_2DisabledAssignee(unittest.TestCase):
    from tests.unit.test_scheduler import TestSchedulerUnit as _S

    setUp = _S.setUp
    tearDown = _S.tearDown

    def test_disabled_inbox_agent_does_not_time_out(self) -> None:
        from mesh.daemon.scheduler import check_queued_tasks_t6

        self.conn.execute("UPDATE agents SET ack_timeout_s = 60 WHERE id = 'agt-ibx';") if any(
            r[1] == "ack_timeout_s" for r in self.conn.execute("PRAGMA table_info(agents);")) else None
        self.conn.execute("UPDATE tasks SET status = 'queued', queued_at = '2000-01-01T00:00:00Z', "
                          "offered_at = '2000-01-01T00:00:00Z' WHERE id = 'tsk-1';")
        self.conn.execute("UPDATE principals SET disabled_at = '2000-01-01T00:00:00Z' WHERE id = 'agt-ibx';")
        self.assertEqual(check_queued_tasks_t6(self.conn), [])
        row = self.conn.execute("SELECT status, offered_at FROM tasks WHERE id = 'tsk-1';").fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertNotEqual(row["offered_at"], "2000-01-01T00:00:00Z")
        self.conn.execute("UPDATE principals SET disabled_at = NULL WHERE id = 'agt-ibx';")
        self.conn.execute("UPDATE tasks SET offered_at = '2000-01-01T00:00:00Z' WHERE id = 'tsk-1';")
        self.assertEqual(check_queued_tasks_t6(self.conn), ["tsk-1"])  # the clock runs again


class TestR4P2_3ForeignWorktree(unittest.TestCase):
    def git(self, cwd: str, *args: str) -> str:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_claude_edit_denies_the_paths_themselves(self) -> None:
        from mesh.adapters.claude import EDIT_DENY

        for rule in ("Edit(./.git)", "Edit(./.git/**)", "Edit(./.claude)", "Edit(./.claude/**)"):
            self.assertIn(rule, EDIT_DENY.split(","))

    def test_git_info_refuses_a_worktree_of_another_repository(self) -> None:
        from mesh.core.repos import submission_git_info

        tmp = tempfile.mkdtemp(prefix="mh-r4g-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo, other, ws = (os.path.join(tmp, n) for n in ("repo", "other", "ws"))
        for r in (repo, other):
            os.makedirs(r)
            self.git(r, "init", "-q", "-b", "main")
            self.git(r, "commit", "-q", "--allow-empty", "-m", "init")
        base = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "worktree", "add", "-q", "-b", "mesh/t", ws)
        good = submission_git_info(ws, "mesh/t", base, repo_path=repo)
        self.assertNotIn("error", good)
        self.git(other, "worktree", "add", "-q", "-b", "x", os.path.join(tmp, "ow"))
        ws_git = os.path.join(ws, ".git")
        if os.path.isfile(ws_git):
            os.unlink(ws_git)
        with open(ws_git, "w", encoding="utf-8") as f:  # the task rewrites it
            f.write("gitdir: " + os.path.join(other, ".git", "worktrees", "ow") + "\n")
        bad = submission_git_info(ws, "mesh/t", base, repo_path=repo)
        self.assertEqual(bad["error"]["reason"], "foreign_repository")
        self.assertNotIn("head", bad)

class TestR4P1_1LeadActionRollback(unittest.TestCase):
    def test_rolled_back_actions_keep_their_intents(self) -> None:
        from mesh.daemon import lead_turns

        tmp = tempfile.mkdtemp(prefix="mh-r4l-")
        self.addCleanup(shutil.rmtree, tmp, True)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("BEGIN;")

        def actions(*_a, **_k):
            lead_turns._PENDING_INTENT_UNLINKS.append(os.path.join(tmp, "prj.json"))
            raise RuntimeError("action pass broke after a deliver")

        class Stop(Exception):
            pass

        run = {"id": "run_1", "kind": "lead_turn", "project_id": "prj", "run_dir": tmp, "adapter": "none"}
        del lead_turns._PENDING_INTENT_UNLINKS[:]
        with mock.patch.object(lead_turns, "execute_lead_actions", side_effect=actions), \
                mock.patch("mesh.daemon.supervisor.vendor_failure_meaning", return_value=None), \
                mock.patch.object(lead_turns, "_release_messages", side_effect=Stop):
            with self.assertRaises(Stop):
                lead_turns.finish_lead_turn(conn, tmp, run, {"exit_code": 0, "signal": None})
        self.assertEqual(lead_turns._PENDING_INTENT_UNLINKS, [])


if __name__ == "__main__":
    unittest.main()
