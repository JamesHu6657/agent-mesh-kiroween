"""r4e (ACC: N18, A8): a forced seen release that rolls back leaves the hold intact, a
restored persisted hold invalidates a cached usable seen state, and a pending
``run.seen_unusable`` event survives the run closing.

In-process, against a real database and real processes (see test_seen_hold_r4d): the
run is adopted, its wrapper (a session leader) has exited and an env-less member that
ignores SIGTERM is still in the wrapper's process group and session.
"""

import json
import os
import signal
import sqlite3
import time
import types
import unittest
from typing import Any, Dict, List, Tuple
from unittest import mock

from mesh.errors import MeshError
from mesh.daemon import supervisor, terminate
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.run import handle_run_release
from mesh.daemon.server import DaemonServer
from tests.unit.test_seen_hold_r4d import _SeenDb, _same_alive, _wait_for

# Real /proc scans: hold the runner's host-wide /proc lock shared (tests/run_tier.py).
MESH_PROC_LOCK = "shared"


class _Injected(Exception):
    """Injected failure after the handler ran: the request transaction rolls back."""


class _R4eDb(_SeenDb):
    def _forget(self) -> None:
        super()._forget()
        supervisor._SEEN_EVENT_PENDING.clear()

    def _hold_row(self, run_id: str) -> Any:
        return self.conn.execute("SELECT * FROM meta WHERE key = ?;", ("seen_hold:" + run_id,)).fetchone()

    def _events(self, type_: str, run_id: str) -> List[Any]:
        return self.conn.execute("SELECT * FROM events WHERE type = ? AND run_id = ? ORDER BY seq;",
                                 (type_, run_id)).fetchall()

    def _watch(self, n: int = 1) -> None:
        with mock.patch.object(supervisor, "_log_meshd"):
            for _ in range(n):
                supervisor.watch_launch_runs(self.conn, self.home)

    def _signals(self, g: Dict[str, int]) -> Tuple[List[Tuple[str, int, int]], Any, Any]:
        """Patches os.kill/os.killpg recording every signal aimed at the member or its group."""
        sent: List[Tuple[str, int, int]] = []
        real_kill, real_killpg = os.kill, os.killpg

        def kill(pid: int, sig: int) -> None:
            if pid in (g["member"], g["wrapper"]) and sig != 0:
                sent.append(("kill", pid, sig))
            return real_kill(pid, sig)

        def killpg(pgid: int, sig: int) -> None:
            if pgid == g["wrapper"] and sig != 0:
                sent.append(("killpg", pgid, sig))
            return real_killpg(pgid, sig)
        return sent, mock.patch("os.kill", kill), mock.patch("os.killpg", killpg)


class TestForcedReleaseRollback(_R4eDb):
    """r4e item 1 (MAJOR): `run release --force-seen-unusable` changes in-memory and
    termination state only after its transaction committed."""

    def _ctx(self, run_id: str) -> HandlerContext:
        return HandlerContext(conn=self.conn, principal={"id": "usr_1", "kind": "user"}, actor_kind="user",
                              via="direct", request_id="req_r4e", home=self.home, cmd="run.release",
                              args={"run_id": run_id, "force_seen_unusable": True})

    def _request(self, run_id: str, fail: bool) -> HandlerContext:
        """One request as the server runs it: BEGIN, handler, (injected failure ->
        ROLLBACK | COMMIT, then the post-commit effects)."""
        ctx = self._ctx(run_id)
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            with mock.patch.object(supervisor, "_log_meshd"):
                handle_run_release(ctx)
            if fail:
                raise _Injected("COMMIT failed (injected)")
            self.conn.execute("COMMIT;")
        except (_Injected, MeshError):
            self.conn.execute("ROLLBACK;")
            if not fail:
                raise
            return ctx
        errors: List[str] = []
        stub = types.SimpleNamespace(home=self.home, stop_event=None, db_conn=self.conn,
                                     log_error=errors.append)
        with mock.patch.object(supervisor, "_log_meshd"):
            DaemonServer.execute_effects(stub, ctx.effects)
        self.assertEqual(errors, [])
        return ctx

    def test_rolled_back_release_keeps_hold_then_release_closes(self) -> None:
        """ACC: N18, A8 (r4e item 1) - persisted hold, seen.json deleted; a forced release
        rolls back, an immediate retry rolls back too, then watch passes: the run stays
        open, `task:<id>` held, the member alive and not signalled, the meta row kept.
        A committed release then closes the run `lost` and clears everything."""
        g = self._spawn_group()
        self._task("tsk_f")
        run_dir = self._run_row("run_f", "tsk_f", g)
        self._write_seen(run_dir, raw="{truncated")
        self._watch(2)
        self.assertIn(self._row("run_f")["status"], ("starting", "running"))
        self.assertIsNotNone(self._hold_row("run_f"), "no persisted hold")
        os.unlink(os.path.join(run_dir, "seen.json"))
        self._watch(2)
        self.assertIn(self._row("run_f")["status"], ("starting", "running"))

        sent, p_kill, p_killpg = self._signals(g)
        with p_kill, p_killpg:
            self._request("run_f", fail=True)
            self._request("run_f", fail=True)  # immediate retry, before any watch pass
            self._watch(3)
            row = self._row("run_f")
            self.assertIn(row["status"], ("starting", "running"),
                          "run closed after a rolled-back force release")
            self.assertIsNotNone(self._lease("tsk_f"), "lease released after a rolled-back force release")
            self.assertTrue(_same_alive(g["member"], g["mst"]))
            self.assertEqual(sent, [], "a member of an unusable seen state was signalled")
            self.assertIsNotNone(self._hold_row("run_f"), "persisted hold lost by a rolled-back release")
            self.assertEqual(json.loads(self._hold_row("run_f")["value"])["reason"], "invalid_json")
            self.assertEqual(self._events("run.seen_release_forced", "run_f"), [])
            self.assertTrue(supervisor.seen_held(row))

            self._request("run_f", fail=False)
            self._watch(2)
        row = self._row("run_f")
        self.assertEqual(row["status"], "lost")
        self.assertIsNone(self._lease("tsk_f"))
        self.assertIsNone(self._hold_row("run_f"))
        self.assertTrue(_same_alive(g["member"], g["mst"]), "release must not signal any process")
        self.assertEqual(sent, [])
        forced = self._events("run.seen_release_forced", "run_f")
        self.assertEqual(len(forced), 1)
        self.assertEqual(json.loads(forced[0]["payload_json"])["reason"], "invalid_json")
        self.assertNotIn("run_f", supervisor._SEEN)
        self.assertNotIn("run_f", supervisor._SEEN_UNUSABLE)
        self.assertEqual(len(self._events("run.seen_unusable", "run_f")), 1)


class TestRestoredHoldDropsCache(_R4eDb):
    """r4e item 1 (second part): a refresh that restores a persisted reason invalidates
    a usable/empty seen state cached while no hold was known."""

    def test_absent_file_cached_empty_then_hold_restored(self) -> None:
        """ACC: N18, A8 (r4e item 1) - seen.json absent, read while no persisted hold was
        known (cached as "no members"); the persisted hold row is then found by the watch
        pass's refresh: the run stays held while the member lives."""
        g = self._spawn_group()
        self._task("tsk_c")
        self._run_row("run_c", "tsk_c", g)
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertEqual(supervisor.seen_for(self._row("run_c"), self.home), {})
        self.assertIn("run_c", supervisor._SEEN)
        self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?);",
                          ("seen_hold:run_c", json.dumps({"run_id": "run_c", "reason": "invalid_json",
                                                          "since": "t"})))
        sent, p_kill, p_killpg = self._signals(g)
        with p_kill, p_killpg:
            self._watch(3)
        self.assertIn(self._row("run_c")["status"], ("starting", "running"),
                      "run closed although a persisted hold exists")
        self.assertIsNotNone(self._lease("tsk_c"))
        self.assertTrue(_same_alive(g["member"], g["mst"]))
        self.assertEqual(sent, [])
        self.assertIsNotNone(self._hold_row("run_c"))
        # Once the member is gone the run closes normally.
        os.kill(g["member"], signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: not _same_alive(g["member"], g["mst"]), timeout=10))
        self.assertTrue(_wait_for(lambda: (self._watch(1) or
                                           self._row("run_c")["status"] not in ("starting", "running")),
                                  timeout=20))
        self.assertIsNone(self._lease("tsk_c"))


class TestPendingEventSurvivesClose(_R4eDb):
    mesh_serial = True  # host-global /proc scan (dogfood merge failure)

    """r4e item 2 (MINOR): a pending `run.seen_unusable` is not dropped when the run closes."""

    def test_failure_on_close_tick_then_success(self) -> None:
        """ACC: N18 (r4e item 2) - held run, the member dies; on the tick the group is empty
        the event insert fails and the run closes; the next tick stores it: exactly one
        event, recorded after the run closed."""
        g = self._spawn_group()
        self._task("tsk_p")
        run_dir = self._run_row("run_p", "tsk_p", g)
        self._write_seen(run_dir, raw="{truncated")
        with mock.patch.object(supervisor, "_log_meshd"):
            self.assertTrue(supervisor.seen_hold(self._row("run_p")))
        self.assertEqual(self._events("run.seen_unusable", "run_p"), [])
        os.kill(g["member"], signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: not _same_alive(g["member"], g["mst"]), timeout=10))
        real = supervisor.record_event
        failed = {"n": 0}

        def flaky(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("type_") == "run.seen_unusable":
                failed["n"] += 1
                raise sqlite3.OperationalError("disk I/O error")
            return real(*args, **kwargs)

        with mock.patch.object(supervisor, "record_event", flaky):
            self._watch(1)
            # Closing needs a second empty leftover scan at least a poll later, taken
            # by a later tick through the real watch path (no seeded private state).
            time.sleep(terminate.POLL_S)
            self._watch(1)
        self.assertGreaterEqual(failed["n"], 1, "the injected failure did not run")
        self.assertNotIn(self._row("run_p")["status"], ("starting", "running"), "run did not close")
        self.assertEqual(self._events("run.seen_unusable", "run_p"), [])
        self._watch(3)
        ev = self._events("run.seen_unusable", "run_p")
        self.assertEqual(len(ev), 1, ev)
        self.assertEqual(json.loads(ev[0]["payload_json"]), {"run_id": "run_p", "reason": "invalid_json"})
        self.assertEqual(ev[0]["task_id"], "tsk_p")
        self.assertEqual(ev[0]["project_id"], "prj_1")
        self.assertNotIn("run_p", supervisor._SEEN_EVENT_PENDING)


if __name__ == "__main__":
    unittest.main()
