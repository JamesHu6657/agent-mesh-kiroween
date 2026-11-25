"""Direct recovery tests: no existing hold hook covers close COMMIT → merge TX1."""

import json
import os
import tempfile
import unittest
from unittest import mock

from mesh.core.clock import now_iso_utc
from mesh.core.db import connect, init_db, transaction
from mesh.core.events import record_event
from mesh.daemon import lead_turns, supervisor
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.merge import check_merge_request
from mesh.errors import MeshError


class TestQueuedLeadMergeRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mh-queued-recovery-")
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.path = os.path.join(self.home, "mesh.db")
        self.conn = init_db(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.addCleanup(lead_turns.take_queued_lead_merges)
        lead_turns.take_queued_lead_merges()
        patch = mock.patch.object(lead_turns, "_MERGE_RECOVERY_BOOT", None)
        patch.start()
        self.addCleanup(patch.stop)
        self.now = now_iso_utc()
        self.conn.execute("INSERT INTO principals (id,kind,name,created_at) VALUES ('usr','user','u',?);",
                          (self.now,))
        self.conn.execute("INSERT INTO principals (id,kind,name,created_at) VALUES ('agt','agent','a',?);",
                          (self.now,))
        self.conn.execute(
            "INSERT INTO agents (id,adapter,delivery,can_lead,capabilities_json,max_concurrency,"
            "created_at,updated_at) VALUES ('agt','fake','launch',1,'[]',1,?,?);", (self.now, self.now))
        self.conn.execute(
            "INSERT INTO projects (id,name,goal,status,created_by,created_via,created_at,updated_at) "
            "VALUES ('prj','p','g','active','usr','user',?,?);", (self.now, self.now))
        self.conn.execute(
            "INSERT INTO lead_assignments (id,project_id,agent_id,assigned_by,via,started_at) "
            "VALUES ('lead','prj','agt','usr','user',?);", (self.now,))
        self.conn.execute(
            "INSERT INTO tasks (id,project_id,title,spec,kind,status,profile,timeout_s,max_attempts,"
            "created_by,created_at,updated_at) "
            "VALUES ('tsk','prj','t','s','code','accepted','edit',600,3,'usr',?,?);", (self.now, self.now))
        self.run_dir = os.path.join(self.home, "run")
        os.mkdir(self.run_dir)
        self.conn.execute(
            "INSERT INTO runs (id,kind,project_id,agent_id,fence,adapter,status,boot_id,run_dir,"
            "lead_assignment_id) VALUES ('run','lead_turn','prj','agt',0,'fake','exited','boot',?,'lead');",
            (self.run_dir,))
        scope = {"role": "lead", "run_id": "run", "project_id": "prj", "lead_assignment_id": "lead"}
        self.conn.execute(
            "INSERT INTO credentials (id,principal_id,kind,lookup,secret_hash,scope_json,created_at,revoked_at) "
            "VALUES ('cred','agt','run_token','lookup',?,?,?,?);",
            (b"hash", json.dumps(scope), self.now, self.now))

    def event(self, request_id, type_="lead.action_result", **overrides):
        payload = {"cmd": "task.merge", "ok": True, "status": "queued", "index": 0,
                   "args": {"task_id": "tsk"}, "action_args": {"task": "tsk"}}
        payload.update(overrides)
        record_event(self.conn, type_=type_, actor_id="agt", actor_kind="agent", via="lead_turn",
                     project_id="prj", run_id="run", request_id=request_id, payload=payload)

    def test_committed_queue_survives_connection_and_memory_loss(self):
        """Queued lead merges survive the close commit, recover once per boot, and retain scope."""
        with transaction(self.conn):
            self.event("req_pending")
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        self.conn.close()
        self.conn = connect(self.path)
        with mock.patch.object(lead_turns, "recover_queued_lead_merges",
                               wraps=lead_turns.recover_queued_lead_merges) as recover:
            supervisor.watch_launch_runs(self.conn, self.home)
            items = lead_turns.take_queued_lead_merges()
            self.assertEqual(recover.call_count, 1)
            supervisor.watch_launch_runs(self.conn, self.home)
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
            self.assertEqual(recover.call_count, 1)
            with mock.patch.object(lead_turns, "_boot_id", return_value="next-boot"):
                lead_turns.recover_queued_lead_merges_once(self.conn)
            self.assertEqual(recover.call_count, 2)
            self.assertEqual(lead_turns.take_queued_lead_merges(), items)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["request_id"], "req_pending")
        self.assertEqual(item["args"], {"task_id": "tsk"})
        self.assertEqual(item["action_args"], {"task": "tsk"})
        self.assertEqual(item["principal"]["id"], "agt")
        self.assertEqual(item["principal"]["credential_id"], "cred")
        self.assertEqual(item["principal"]["scope"]["lead_assignment_id"], "lead")
        # Closing the turn revoked its credential; recovery preserves that original
        # internal principal, not user authority. TX1 must reject a superseded lead.
        self.conn.execute("UPDATE lead_assignments SET ended_at = ? WHERE id = 'lead';", (self.now,))
        ctx = HandlerContext(conn=self.conn, principal=item["principal"], actor_kind="agent",
                             via="run_token", request_id=item["request_id"], home=self.home,
                             cmd="task.merge", args=item["args"], testing_mode=True)
        with self.assertRaises(MeshError) as error:
            check_merge_request(ctx, "tsk")
        self.assertEqual(error.exception.code, "LEAD_SUPERSEDED")
        self.assertEqual(error.exception.exit_code, 9)

    def test_started_failed_and_rolled_back_merges_are_not_requeued(self):
        """Recover only successful queued results lacking same-request start/failure evidence."""
        with transaction(self.conn):
            self.event("req_pending")
            self.event("req_started")
            self.event("req_started", type_="task.merging")
            self.event("req_failed")
            self.event("req_failed", phase="merge_start", ok=False)
            self.event("req_not_merge", cmd="task.accept")
            self.event("req_not_ok", ok=False)
            self.event("req_not_queued", status="done")
        self.conn.execute("BEGIN;")
        self.event("req_rolled_back")
        self.conn.execute("ROLLBACK;")
        self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        items = lead_turns.take_queued_lead_merges()
        self.assertEqual([i["request_id"] for i in items], ["req_pending"])

    def test_pre_r11_queued_event_recovers_original_stdout_action(self):
        """Existing r10b queue events can be recovered without newly persisted args."""
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"task.merge","args":{"task":"tsk"}}]\n```\n')
        with transaction(self.conn):
            self.event("req_legacy", args=None, action_args=None)
        self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        item = lead_turns.take_queued_lead_merges()[0]
        self.assertEqual(item["args"], {"task_id": "tsk"})
        self.assertEqual(item["action_args"], {"task": "tsk"})

    def running_lead(self):
        self.conn.execute(
            "INSERT INTO runs (id,kind,project_id,agent_id,fence,adapter,status,boot_id,run_dir,"
            "lead_assignment_id,wrapper_pid) "
            "VALUES ('live','lead_turn','prj','agt',0,'fake','running','boot',?,'lead',123);",
            (self.run_dir,))
        self.conn.execute(
            "INSERT INTO leases (key,run_id,holder_kind,holder,agent_id,fence,acquired_at,"
            "expires_at,heartbeat_at) VALUES ('leadturn:prj','live','meshd','boot','agt',0,?,?,?);",
            (self.now, "2000-01-01T00:00:00Z", "2000-01-01T00:00:00Z"))

    def assert_live_lease_renewed(self):
        lease = self.conn.execute("SELECT * FROM leases WHERE run_id = 'live';").fetchone()
        self.assertGreater(lease["heartbeat_at"], "2000-01-01T00:00:00Z")
        self.assertGreater(lease["expires_at"], self.now)

    def test_missing_legacy_stdout_does_not_block_watch_or_retry(self):
        """A bad legacy row cannot starve good merges or this tick's lease renewal."""
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "stdout.log")))
        with transaction(self.conn):
            self.event("req_missing", args=None, action_args=None)
            self.event("req_good")
            self.running_lead()
        with mock.patch.object(supervisor, "wrapper_alive", return_value=True), \
                mock.patch.object(lead_turns, "recover_queued_lead_merges",
                                  wraps=lead_turns.recover_queued_lead_merges) as recover:
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home), [])
            self.assert_live_lease_renewed()
            items = lead_turns.take_queued_lead_merges()
            self.assertEqual([item["request_id"] for item in items], ["req_good"])
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home), [])
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
            self.assertEqual(recover.call_count, 1)
        # A subsequent boot must also skip the durable failure, not merely rely
        # on the in-memory once-per-boot guard.
        self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        results = self.conn.execute(
            "SELECT * FROM events WHERE request_id = 'req_missing' ORDER BY seq;").fetchall()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[-1]["reason"], "RECOVERY_FAILED")
        payload = json.loads(results[-1]["payload_json"])
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["phase"], "merge_start")
        self.assertEqual(payload["reason"], "RECOVERY_FAILED")
        self.assertEqual(payload["error_code"], "RECOVERY_FAILED")
        with open(os.path.join(self.home, "logs", "meshd.log"), encoding="utf-8") as f:
            self.assertIn("req_missing", f.read())

    def test_malformed_payload_missing_index_and_truncated_stdout_fail_once(self):
        # Produce the good event through the actual action dispatch and close TX,
        # not through the hand-built historical-event helper.
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"task.merge","args":{"task":"tsk"}}]\n```\n')
        self.conn.execute("UPDATE credentials SET revoked_at = NULL WHERE id = 'cred';")
        self.conn.execute("UPDATE runs SET status = 'running' WHERE id = 'run';")
        self.conn.execute("UPDATE projects SET repo_path = ? WHERE id = 'prj';", (self.home,))
        self.conn.execute("UPDATE tasks SET branch = 'task/tsk', workspace_path = ? WHERE id = 'tsk';",
                          (self.home,))
        run = self.conn.execute("SELECT * FROM runs WHERE id = 'run';").fetchone()
        with mock.patch.object(lead_turns, "generate_id", return_value="req_good"):
            with transaction(self.conn):
                self.assertEqual(lead_turns.finish_lead_turn(
                    self.conn, self.home, run, {"exit_code": 0}, testing_mode=True), "acked")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE id = 'run';").fetchone()[0],
                         "exited")
        committed = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.action_result' AND request_id = 'req_good';"
        ).fetchone()
        payload = json.loads(committed["payload_json"])
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["args"], {"task_id": "tsk"})
        self.assertEqual(payload["action_args"], {"task": "tsk"})
        lost_queue = lead_turns.take_queued_lead_merges()
        self.assertEqual(len(lost_queue), 1)
        self.assertEqual(lost_queue[0]["args"], payload["args"])
        self.assertEqual(lost_queue[0]["action_args"], payload["action_args"])
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"task.merge","args":')
        with transaction(self.conn):
            self.event("req_truncated", args=None, action_args=None)
            # Insert a damaged historical row without modifying append-only events.
            self.conn.execute(
                "INSERT INTO events (id,ts,type,project_id,run_id,actor_id,actor_kind,via,"
                "request_id,payload_json,prev_hash,hash) "
                "VALUES ('evt_corrupt',?,'lead.action_result','prj','run','agt','agent',"
                "'lead_turn','req_corrupt',?,'genesis','corrupt');",
                (self.now, '{"cmd":"task.merge","ok":true,"status":"queued","index":'))
            self.conn.execute(
                "INSERT INTO events (id,ts,type,project_id,run_id,actor_id,actor_kind,via,"
                "request_id,payload_json,prev_hash,hash) "
                "VALUES ('evt_nonfinite',?,'lead.action_result','prj','run','agt','agent',"
                "'lead_turn','req_nonfinite',?,'genesis','corrupt');",
                (self.now, '{"cmd":"task.merge","ok":true,"status":"queued","index":0,'
                           '"args":{"task_id":"tsk","extra":NaN}}'))
            record_event(
                self.conn, type_="lead.action_result", actor_id="agt", actor_kind="agent",
                via="lead_turn", project_id="prj", run_id="run", request_id="req_no_index",
                payload={"cmd": "task.merge", "ok": True, "status": "queued",
                         "args": {"task_id": "tsk"}})
        for _ in range(2):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
            recovered = lead_turns.take_queued_lead_merges()
            self.assertEqual([item["request_id"] for item in recovered], ["req_good"])
            self.assertEqual(recovered, lost_queue)
        failures = self.conn.execute(
            "SELECT request_id,payload_json FROM events WHERE reason = 'RECOVERY_FAILED' "
            "ORDER BY seq;").fetchall()
        self.assertEqual([row["request_id"] for row in failures],
                         ["req_truncated", "req_corrupt", "req_nonfinite", "req_no_index"])
        for row in failures:
            payload = json.loads(row["payload_json"])
            self.assertIs(payload["ok"], False)
            self.assertEqual(payload["phase"], "merge_start")
            source = self.conn.execute(
                "SELECT seq FROM events WHERE request_id = ? ORDER BY seq LIMIT 1;",
                (row["request_id"],)).fetchone()
            self.assertEqual(payload["source_seq"], source["seq"])
        self.assertFalse(self.conn.in_transaction)

    def test_legacy_final_block_never_falls_back_to_an_earlier_task(self):
        first = '```mesh-actions\n[{"cmd":"task.merge","args":{"task":"tsk"}}]\n```\n'
        endings = [
            '```mesh-actions\n[{"cmd":"task.merge","args":{"task":"other"}}',
            '```mesh-actions\n[{"cmd":"task.merge","args":{"task":"other"}}]\n',
            '```mesh-actions\ninvalid\n```\n',
            '```mesh-actions\n{}\n```\n',
            '```mesh-actions\n[{"cmd":"task.accept","args":{"task":"other"}}]\n```\n',
            '```mesh-actions\n[{"cmd":"task.merge","args":{"task":[]}}]\n```\n',
        ]
        for i, ending in enumerate(endings):
            with self.subTest(ending=ending):
                with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                    f.write(first + ending)
                request_id = f"req_final_{i}"
                self.event(request_id, args=None, action_args=None, task_id="tsk")
                for _ in range(2):
                    self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
                    self.assertEqual(lead_turns.take_queued_lead_merges(), [])
                failures = self.conn.execute(
                    "SELECT * FROM events WHERE request_id = ? AND reason = 'RECOVERY_FAILED';",
                    (request_id,)).fetchall()
                self.assertEqual(len(failures), 1)

    def test_legacy_stdout_task_must_agree_with_every_stored_task(self):
        with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write('```mesh-actions\n[{"cmd":"task.merge","args":{"task":"tsk"}}]\n```\n')
        evidence = [
            {"task_id": "other"}, {"task": "other"},
            {"action_args": {"task": "other"}},
            {"request": {"args": {"task_id": "other"}}},
        ]
        for i, stored in enumerate(evidence):
            with self.subTest(stored=stored):
                self.event(f"req_mismatch_{i}", **dict(
                    {"args": None, "action_args": None}, **stored))
        record_event(self.conn, type_="lead.action_result", actor_id="agt", actor_kind="agent",
                     via="lead_turn", project_id="prj", run_id="run", task_id="other",
                     request_id="req_column_mismatch",
                     payload={"cmd": "task.merge", "ok": True, "status": "queued", "index": 0})
        for _ in range(2):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE reason = 'RECOVERY_FAILED';").fetchone()[0], 5)

    def test_unidentifiable_invalid_json_is_skipped_and_logged_once(self):
        corrupt = [
            "{", '{"cmd":"task.accept","ok":true,"status":"queued","index":',
            '{"cmd":"task.merge","ok":false,"status":"queued","index":',
            '{"cmd":"task.merge","ok":true,"status":"done","index":',
            '{"message":{"cmd":"task.merge","ok":true,"status":"queued"},"index":',
            '{"cmd":"task.merge","ok":true,"status":"queued","cmd":"task.accept","index":',
            "NaN", "Infinity", "[NaN]",
            '{"cmd":"task.accept","ok":true,"status":"queued","index":NaN}',
            '{"cmd":"project.note","value":' + "[" * 1100,
        ]
        for i, payload in enumerate(corrupt):
            self.conn.execute(
                "INSERT INTO events (id,ts,type,run_id,actor_id,actor_kind,via,request_id,"
                "payload_json,prev_hash,hash) VALUES (?,?,'lead.action_result','run','agt',"
                "'agent','lead_turn',?,?,'genesis','corrupt');",
                (f"evt_unknown_{i}", self.now, f"req_unknown_{i}", payload))
        with mock.patch.object(lead_turns, "_log_queued_merge_recovery_error") as log:
            for _ in range(2):
                self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
                self.assertEqual(lead_turns.take_queued_lead_merges(), [])
            self.conn.close()
            self.conn = connect(self.path)
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
        self.assertEqual(log.call_count, len(corrupt))
        self.assertTrue(all("skip" in call.args[1].lower() for call in log.call_args_list))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE reason = 'RECOVERY_FAILED';").fetchone()[0], 0)

    def test_null_request_failures_match_only_the_source_event(self):
        self.event(None, args=None, action_args=None)
        lead_turns.recover_queued_lead_merges(self.conn)
        self.event(None, args=None, action_args=None)
        for _ in range(2):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        rows = self.conn.execute(
            "SELECT * FROM events WHERE reason = 'RECOVERY_FAILED' ORDER BY seq;").fetchall()
        self.assertEqual(len(rows), 2)
        sources = self.conn.execute(
            "SELECT seq FROM events WHERE reason IS NULL ORDER BY seq;").fetchall()
        self.assertEqual([json.loads(row["payload_json"])["source_seq"] for row in rows],
                         [row["seq"] for row in sources])

    def test_missing_run_records_detached_terminal_failure_once(self):
        self.event("req_missing_run")
        self.conn.execute("DELETE FROM runs WHERE id = 'run';")
        # Emulate a stricter FK deployment: failure evidence must not need a run.
        self.conn.execute(
            "CREATE TRIGGER require_event_run BEFORE INSERT ON events WHEN NEW.run_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM runs WHERE id = NEW.run_id) "
            "BEGIN SELECT RAISE(ABORT, 'missing run'); END;")
        for _ in range(2):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
            self.assertEqual(lead_turns.take_queued_lead_merges(), [])
        rows = self.conn.execute(
            "SELECT * FROM events WHERE reason = 'RECOVERY_FAILED';").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["run_id"])
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["run_id"], "run")
        self.assertEqual(payload["source_seq"], self.conn.execute(
            "SELECT seq FROM events WHERE request_id = 'req_missing_run' ORDER BY seq LIMIT 1;"
        ).fetchone()[0])

    def test_unexpected_row_exception_does_not_stop_recovery(self):
        with transaction(self.conn):
            self.event("req_exception")
            self.event("req_good")
        principal = lead_turns._lead_turn_principal(self.conn, self.conn.execute(
            "SELECT * FROM runs WHERE id = 'run';").fetchone())
        with mock.patch.object(lead_turns, "_lead_turn_principal",
                               side_effect=[RuntimeError("principal unavailable"), principal]):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        self.assertEqual([item["request_id"] for item in lead_turns.take_queued_lead_merges()],
                         ["req_good"])
        failure = self.conn.execute(
            "SELECT reason FROM events WHERE request_id = 'req_exception' ORDER BY seq DESC LIMIT 1;"
        ).fetchone()
        self.assertEqual(failure["reason"], "RECOVERY_FAILED")

    def test_merge_tx1_with_its_own_run_id_prevents_recovery(self):
        with transaction(self.conn):
            self.event("req_started")
            record_event(
                self.conn, type_="task.merging", actor_id="agt", actor_kind="agent",
                via="run_token", project_id="prj", task_id="tsk", run_id="merge_run",
                request_id="req_started", payload={"task_id": "tsk", "run_id": "merge_run"})
        self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 0)
        self.assertEqual(lead_turns.take_queued_lead_merges(), [])

    def test_failure_recording_and_logging_errors_do_not_stop_other_rows(self):
        with transaction(self.conn):
            self.event("req_missing", args=None, action_args=None)
            self.event("req_good")
        with mock.patch.object(lead_turns, "record_event", side_effect=RuntimeError("event unavailable")), \
                mock.patch.object(supervisor, "_log_meshd", side_effect=OSError("log unavailable")):
            self.assertEqual(lead_turns.recover_queued_lead_merges(self.conn), 1)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual([item["request_id"] for item in lead_turns.take_queued_lead_merges()],
                         ["req_good"])

    def test_recovery_failure_marks_boot_and_watch_still_renews_leases(self):
        self.running_lead()
        with mock.patch.object(lead_turns, "recover_queued_lead_merges",
                               side_effect=RuntimeError("recovery unavailable")) as recover, \
                mock.patch.object(supervisor, "wrapper_alive", return_value=True):
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home), [])
            self.assert_live_lease_renewed()
            self.assertEqual(lead_turns._MERGE_RECOVERY_BOOT, lead_turns._boot_id(self.conn))
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home), [])
            self.assertEqual(recover.call_count, 1)

    def test_watch_survives_recovery_wrapper_and_logging_failure(self):
        self.running_lead()
        with mock.patch.object(lead_turns, "recover_queued_lead_merges_once",
                               side_effect=RuntimeError("recovery wrapper unavailable")), \
                mock.patch.object(supervisor, "_log_meshd", side_effect=OSError("log unavailable")), \
                mock.patch.object(supervisor, "wrapper_alive", return_value=True):
            self.assertEqual(supervisor.watch_launch_runs(self.conn, self.home), [])
            self.assert_live_lease_renewed()

    def test_l5_all_close_paths_capture_sessions_without_erasing_last_id(self):
        """ACC: L5 - successful, killed and lost task/lead closes all persist captured ids."""
        with mock.patch.object(supervisor, "_testing_mode", return_value=True):
            for kind in ("task", "lead_turn"):
                for status in ("exited", "killed", "lost"):
                    run_id = f"run_{kind}_{status}"
                    session = f"session_{kind}_{status}"
                    with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                        f.write(json.dumps({"session_id": session}) + "\n")
                    self.conn.execute(
                        "INSERT INTO runs (id,kind,task_id,project_id,agent_id,fence,adapter,status,boot_id,"
                        "run_dir,lead_assignment_id) VALUES (?,?,?,'prj','agt',0,'fake','running','boot',?,?);",
                        (run_id, kind, "tsk" if kind == "task" else None, self.run_dir,
                         "lead" if kind == "lead_turn" else None))
                    run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
                    with transaction(self.conn):
                        supervisor._close_run(self.conn, run, status, status, None, {})
                    self.assertEqual(self.conn.execute("SELECT session_id FROM runs WHERE id = ?;",
                                                       (run_id,)).fetchone()[0], session)
                    table, column, owner = (("tasks", "last_session_id", "tsk") if kind == "task"
                                            else ("lead_assignments", "session_id", "lead"))
                    self.assertEqual(self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?;",
                                                       (owner,)).fetchone()[0], session)
                    # Re-closing an old run must not overwrite the owner's latest session.
                    with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                        f.write('{"session_id":"stale"}\n')
                    with transaction(self.conn):
                        supervisor._close_run(self.conn, run, status, status, None, {})
                    self.assertEqual(self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?;",
                                                       (owner,)).fetchone()[0], session)
                with open(os.path.join(self.run_dir, "stdout.log"), "w", encoding="utf-8") as f:
                    f.write("No session emitted\n")
                run_id = f"run_{kind}_no_session"
                self.conn.execute(
                    "INSERT INTO runs (id,kind,task_id,project_id,agent_id,fence,adapter,status,boot_id,"
                    "run_dir,lead_assignment_id) VALUES (?,?,?,'prj','agt',0,'fake','running','boot',?,?);",
                    (run_id, kind, "tsk" if kind == "task" else None, self.run_dir,
                     "lead" if kind == "lead_turn" else None))
                run = self.conn.execute("SELECT * FROM runs WHERE id = ?;", (run_id,)).fetchone()
                with transaction(self.conn):
                    supervisor._close_run(self.conn, run, "exited", "exited", 0, {})
                self.assertIsNone(self.conn.execute("SELECT session_id FROM runs WHERE id = ?;",
                                                   (run_id,)).fetchone()[0])
                self.assertEqual(self.conn.execute(f"SELECT {column} FROM {table} WHERE id = ?;",
                                                   (owner,)).fetchone()[0], session)
