"""batch6: meshd shutdown drains in-flight merges before it releases the lock and DB
(Nov 14 full review P1, daemon-rest); PR41 r2 P3s: parse_deliverable by position,
no symlinked /tmp/gitleaks fallback."""
import asyncio
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unittest

from mesh.core.prompts import parse_deliverable
from mesh.daemon.server import DaemonServer

F = "`" * 3
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestShutdownDrain(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mb6-")
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(os.path.join(self.home, "run"), mode=0o700)

    def make_server(self, order):
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:")
        conn = srv.db_conn

        async def idle():
            await asyncio.Event().wait()

        srv.setup_storage = lambda: None
        srv.acquire_lock = lambda: None
        srv.record_boot = lambda: None
        srv.setup_subreaper = lambda: None
        srv.run_recovery = lambda: None
        srv._scheduler_loop = idle
        srv._termination_loop = idle
        srv.log_error = lambda msg: order.append("log:" + msg)
        srv.log_request = lambda *a, **k: None
        srv.record_stop = lambda: order.append("record_stop")
        srv.release_lock = lambda: order.append("release_lock")
        self.conn = conn
        return srv

    def test_lock_and_db_outlive_inflight_merge(self):
        order = []
        srv = self.make_server(order)
        conn = self.conn
        seen = {}

        async def merge_like():
            # The worker thread is still running git/tests when the stop arrives.
            await asyncio.to_thread(time.sleep, 0.6)
            order.append("merge_thread_done")
            # The final TX needs the open connection.
            seen["db_ok"] = conn.execute("SELECT 1;").fetchone()[0] == 1
            order.append("merge_final_tx")

        async def lead_merge_like():
            await asyncio.sleep(0.3)
            order.append("lead_merge_done")

        async def main():
            run = asyncio.create_task(srv.run_server())
            for _ in range(200):
                if srv.server is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(srv.server)
            job = asyncio.ensure_future(merge_like())
            srv.track_merge_job(job)
            lm = asyncio.ensure_future(lead_merge_like())
            srv._lead_merge_tasks.add(lm)
            lm.add_done_callback(srv._lead_merge_tasks.discard)
            # A connected client that never sends its request (a long poll stand-in).
            if sys.platform == "win32":
                from mesh.platform import ipc

                sock = await asyncio.to_thread(
                    ipc.connect_endpoint, ipc.daemon_endpoint_path(self.home), 5.0
                )
            else:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                await asyncio.get_running_loop().sock_connect(sock, os.path.join(self.home, "run", "mesh.sock"))
            for _ in range(200):
                if srv._client_tasks:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(srv._client_tasks), 1)
            srv.stop_event.set()
            await asyncio.wait_for(run, timeout=30)
            sock.close()

        asyncio.run(main())
        self.assertTrue(seen.get("db_ok"))
        self.assertLess(order.index("merge_final_tx"), order.index("record_stop"))
        self.assertLess(order.index("lead_merge_done"), order.index("record_stop"))
        self.assertLess(order.index("record_stop"), order.index("release_lock"))
        self.assertIsNone(srv.db_conn)
        self.assertEqual(srv._client_tasks, set())
        if sys.platform == "win32":
            from mesh.platform import ipc

            self.assertFalse(ipc.probe_daemon_endpoint(self.home, timeout=0.2))
        else:
            self.assertFalse(os.path.exists(os.path.join(self.home, "run", "mesh.sock")))

    def test_no_new_lead_merge_once_stopping(self):
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:")
        self.addCleanup(srv.db_conn.close)
        srv._stopping = True
        from unittest import mock
        with mock.patch("mesh.daemon.lead_turns.take_queued_lead_merges") as take:
            srv.start_queued_lead_merges()
        take.assert_not_called()

    def test_drain_rolls_back_open_transaction(self):
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(srv.db_conn.close)
        srv.log_error = lambda msg: None
        srv.db_conn.execute("BEGIN;")
        asyncio.run(srv._drain_for_shutdown(grace_s=0.01))
        self.assertFalse(srv.db_conn.in_transaction)
        self.assertTrue(srv._stopping)

    def test_merge_handler_not_cancelled_and_gets_response(self):
        # PR42 r1: a handler parked on a merge (cmd task.merge) outlives the grace and
        # is awaited, not cancelled; a long poll and an unread request are cancelled.
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(srv.db_conn.close)
        srv.log_error = lambda msg: None
        got = {}

        async def main():
            job = asyncio.ensure_future(asyncio.sleep(0.4))
            srv.track_merge_job(job)

            async def handler(cmd, coro):
                me = asyncio.current_task()
                srv._client_tasks.add(me)
                srv._client_cmds[me] = cmd
                try:
                    await coro
                    got[cmd] = "done"
                except asyncio.CancelledError:
                    got[cmd] = "cancelled"
                    raise
                finally:
                    srv._client_tasks.discard(me)
                    srv._client_cmds.pop(me, None)

            tasks = [asyncio.ensure_future(handler("task.merge", asyncio.shield(job))),
                     asyncio.ensure_future(handler("task.start", asyncio.sleep(0.3))),
                     asyncio.ensure_future(handler("inbox.wait", asyncio.sleep(60))),
                     asyncio.ensure_future(handler("unknown", asyncio.sleep(60)))]
            await asyncio.sleep(0)
            await srv._drain_for_shutdown(grace_s=0.05)
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(main())
        self.assertEqual(got, {"task.merge": "done", "task.start": "done",
                               "inbox.wait": "cancelled", "unknown": "cancelled"})

    def test_late_poll_handler_is_cancelled(self):
        # PR42 r2: a handler that registers after the grace snapshot as a long poll is
        # still cancelled, and shutdown does not wait for its timeout.
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(srv.db_conn.close)
        srv.log_error = lambda msg: None
        got = {}

        async def main():
            job = asyncio.ensure_future(asyncio.sleep(0.3))
            srv.track_merge_job(job)

            async def late():
                await asyncio.sleep(0.1)
                me = asyncio.current_task()
                srv._client_tasks.add(me)
                srv._client_cmds[me] = "task.wait"
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    got["late"] = "cancelled"
                    raise
                finally:
                    srv._client_tasks.discard(me)

            t = asyncio.ensure_future(late())
            start = time.monotonic()
            await srv._drain_for_shutdown(grace_s=0.01)
            got["elapsed"] = time.monotonic() - start
            await asyncio.gather(t, return_exceptions=True)

        asyncio.run(main())
        self.assertEqual(got.get("late"), "cancelled")
        self.assertLess(got["elapsed"], 10)

    def test_no_scheduler_tick_while_stopping(self):
        with open(os.path.join(ROOT, "mesh", "daemon", "server.py"), encoding="utf-8") as f:
            src = f.read()
        sites = [m.start() for m in re.finditer(r"run_scheduler_tick\(self\.db_conn", src)]
        loop_site = src.index("async def _scheduler_loop")
        post = [i for i in sites if i > loop_site + 2000]
        self.assertEqual(len(post), 2)
        for i in post:
            self.assertIn("_stopping", src[i - 400:i])

    def test_non_reading_client_cannot_block_shutdown(self):
        # PR42 Linear r1: a handler stuck in drain() on a client that never reads is
        # aborted after settle_s once no merge is left.
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(srv.db_conn.close)
        srv.log_error = lambda msg: None
        got = {}

        async def main():
            unblock = asyncio.Event()

            class FakeTransport:
                def abort(self):
                    got["aborted"] = True
                    unblock.set()

            class FakeWriter:
                transport = FakeTransport()

            async def stuck():
                me = asyncio.current_task()
                srv._client_tasks.add(me)
                srv._client_cmds[me] = "run.log"
                srv._client_writers[me] = FakeWriter()
                try:
                    await unblock.wait()  # stands in for a drain() that never returns
                finally:
                    srv._client_tasks.discard(me)

            t = asyncio.ensure_future(stuck())
            await asyncio.sleep(0)
            start = time.monotonic()
            await asyncio.wait_for(srv._drain_for_shutdown(grace_s=0.01, settle_s=0.2), timeout=20)
            got["elapsed"] = time.monotonic() - start
            await t

        asyncio.run(main())
        self.assertTrue(got.get("aborted"))
        self.assertLess(got["elapsed"], 10)

    def test_cancelled_poll_with_stuck_cleanup_is_aborted(self):
        # PR42 Linear r2: an event.tail whose cleanup blocks until its transport is
        # aborted does not hold shutdown.
        srv = DaemonServer(self.home, testing_mode=True)
        srv.db_conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(srv.db_conn.close)
        srv.log_error = lambda msg: None
        got = {}

        async def main():
            closed = asyncio.Event()

            class FakeTransport:
                def abort(self):
                    got["aborted"] = True
                    closed.set()

            class FakeWriter:
                transport = FakeTransport()

            async def tail():
                me = asyncio.current_task()
                srv._client_tasks.add(me)
                srv._client_cmds[me] = "event.tail"
                srv._client_writers[me] = FakeWriter()
                try:
                    await asyncio.sleep(60)
                finally:
                    await closed.wait()  # stands in for wait_closed() on a stuck socket
                    srv._client_tasks.discard(me)

            t = asyncio.ensure_future(tail())
            await asyncio.sleep(0)
            await asyncio.wait_for(srv._drain_for_shutdown(grace_s=0.01, settle_s=0.2), timeout=20)
            await asyncio.gather(t, return_exceptions=True)

        asyncio.run(main())
        self.assertTrue(got.get("aborted"))

    def test_cli_sees_lock_held(self):
        from mesh.cli.commands.daemon import _meshd_lock_held
        self.assertFalse(_meshd_lock_held(self.home))
        path = os.path.join(self.home, "run", "meshd.lock")
        if sys.platform == "win32":
            from mesh.platform import locks

            fd = locks.open_lock_file(path)
            self.addCleanup(os.close, fd)
            self.assertFalse(_meshd_lock_held(self.home))
            locks.acquire_lock(fd, exclusive=True, blocking=False)
            self.assertTrue(_meshd_lock_held(self.home))
            locks.release_lock(fd)
            self.assertFalse(_meshd_lock_held(self.home))
        else:
            import fcntl

            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, fd)
            self.assertFalse(_meshd_lock_held(self.home))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(_meshd_lock_held(self.home))
            fcntl.flock(fd, fcntl.LOCK_UN)
            self.assertFalse(_meshd_lock_held(self.home))

    def test_cli_stop_reports_stopping_while_lock_held(self):
        import argparse
        from unittest import mock
        from mesh.cli.commands import daemon as dcmd
        from mesh.errors import MeshError

        class FakeClient:
            def __init__(self, home=None, timeout=None):
                self.home = home

            def execute_cmd(self, *a, **k):
                return {"data": {"status": "stopping"}}

            def send_request(self, *a, **k):
                raise ConnectionRefusedError()

        args = argparse.Namespace(home=self.home, dry_run=False)
        with mock.patch.object(dcmd, "MeshClient", FakeClient), \
                mock.patch.object(dcmd, "_meshd_lock_held", return_value=True), \
                mock.patch.object(dcmd.time, "monotonic", side_effect=[0.0, 0.0, 10.0, 10.0]), \
                mock.patch.object(dcmd.time, "sleep"):
            with self.assertRaises(MeshError) as cm:
                dcmd.handle_daemon_stop(args)
        self.assertEqual(cm.exception.details.get("status"), "stopping")
        with mock.patch.object(dcmd, "MeshClient", FakeClient), \
                mock.patch.object(dcmd, "_meshd_lock_held", return_value=False):
            self.assertEqual(dcmd.handle_daemon_stop(args)["status"], "stopped")

    def test_cli_start_refuses_while_old_meshd_holds_lock(self):
        import argparse
        from unittest import mock
        from mesh.cli.commands import daemon as dcmd
        from mesh.errors import MeshError

        class FakeClient:
            def __init__(self, home=None, timeout=None):
                self.home = home

            def execute_cmd(self, *a, **k):
                raise ConnectionRefusedError()

        args = argparse.Namespace(home=self.home, dry_run=False, user=False)
        with mock.patch.object(dcmd, "MeshClient", FakeClient), \
                mock.patch.object(dcmd, "_meshd_lock_held", return_value=True), \
                mock.patch.object(dcmd.subprocess, "Popen") as popen:
            with self.assertRaises(MeshError) as cm:
                dcmd.handle_daemon_start(args)
        popen.assert_not_called()
        self.assertEqual(cm.exception.details.get("status"), "stopping")

    def test_merge_registers_its_job(self):
        with open(os.path.join(ROOT, "mesh", "daemon", "merge.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("job = asyncio.ensure_future(_pipeline_and_finish())")
        self.assertIn("track_merge_job", src[i:i + 400])


class TestDeliverableByPosition(unittest.TestCase):
    def test_draft_standalone_then_final_with_fence_on_json_line(self):
        draft = F + "mesh-deliverable\n{\"summary\": \"draft\"}\n" + F + "\n"
        final = F + "mesh-deliverable\n{\"summary\": \"final\"}" + F + "\n"
        self.assertEqual(parse_deliverable(draft + "more work\n" + final), {"summary": "final"})

    def test_regex_only_then_later_standalone(self):
        first = F + "mesh-deliverable\n{\"summary\": \"first\"}" + F + "\n"
        last = F + "mesh-deliverable\n{\"summary\": \"last\"}\n" + F + "\n"
        self.assertEqual(parse_deliverable(first + "x\n" + last), {"summary": "last"})

    def test_later_invalid_block_falls_back_to_earlier_valid(self):
        good = F + "mesh-deliverable\n{\"summary\": \"good\"}\n" + F + "\n"
        bad = F + "mesh-deliverable\nnot json" + F + "\n"
        self.assertEqual(parse_deliverable(good + bad), {"summary": "good"})

    def test_inline_fence_in_json_string_still_wins(self):
        body = '{"summary": "x ' + F + 'mesh-deliverable y", "artifacts": ["a.py"]}'
        out = parse_deliverable("pre\n" + F + "mesh-deliverable\n" + body + "\n" + F + "\n")
        self.assertEqual(out.get("artifacts"), ["a.py"])

    def test_crlf(self):
        text = "a\r\n" + F + "mesh-deliverable\r\n{\"summary\": \"d\"}\r\n" + F + "\r\n"
        self.assertEqual(parse_deliverable(text), {"summary": "d"})

    def test_regex_inside_valid_standalone_block_is_body(self):
        body = '{"summary": "outer", "notes": "x"}'
        text = F + "mesh-deliverable\n" + body + "\n" + F + "\n"
        self.assertEqual(parse_deliverable(text), {"summary": "outer", "notes": "x"})

    def test_quoted_example_in_other_fence_is_ignored(self):
        real = F + "mesh-deliverable\n{\"summary\": \"real\", \"artifacts\": [\"a.py\"]}\n" + F + "\n"
        doc = ("Docs:\n" + "````markdown\n" + F + "mesh-deliverable\n{\"summary\": \"example\"}\n"
               + F + "\n" + "````\n")
        self.assertEqual(parse_deliverable(real + doc), {"summary": "real", "artifacts": ["a.py"]})

    def test_example_inside_invalid_deliverable_block_is_ignored(self):
        real = F + "mesh-deliverable\n{\"summary\": \"real\"}\n" + F + "\n"
        bad = "````mesh-deliverable\nnot json\n" + F + "mesh-deliverable\n{\"summary\": \"ex\"}\n" + F + "\n````\n"
        self.assertEqual(parse_deliverable(real + bad), {"summary": "real"})

    def test_unclosed_draft_does_not_hide_final(self):
        text = (F + "mesh-deliverable\n{\"summary\": \"draft\"}\n\n"
                + F + "mesh-deliverable\n{\"summary\": \"real\"}\n" + F)
        self.assertEqual(parse_deliverable(text), {"summary": "real"})
        text2 = (F + "mesh-deliverable\n{\"summary\": \"draft\"}\nsome prose\n"
                 + F + "mesh-deliverable\n{\"summary\": \"real\"}\n" + F + "\n")
        self.assertEqual(parse_deliverable(text2), {"summary": "real"})

    def test_unbalanced_earlier_fence_does_not_hide_deliverable(self):
        real = F + "mesh-deliverable\n{\"summary\": \"real\", \"artifacts\": [\"a.py\"]}\n" + F + "\n"
        nested = (F + "markdown\n# README\n" + F + "bash\nmake\n" + F + "\nmore\n" + F + "\n")
        self.assertEqual(parse_deliverable(nested + "done\n" + real), {"summary": "real", "artifacts": ["a.py"]})
        unclosed = F + "text\nlog line\n"
        self.assertEqual(parse_deliverable(unclosed + real), {"summary": "real", "artifacts": ["a.py"]})

    def test_many_fences_parse_in_linear_time(self):
        unit = ("````md\nx\n````\n" + F + "mesh-deliverable\n{\"summary\": \"s\"}\n" + F + "\n")
        text = unit * 20000
        start = time.monotonic()
        self.assertEqual(parse_deliverable(text), {"summary": "s"})
        self.assertLess(time.monotonic() - start, 5.0)

    def test_deeply_nested_earlier_candidate_is_skipped(self):
        deep = "[" * 100000 + "]" * 100000
        text = (F + "mesh-deliverable\n" + deep + "\n" + F + "\n"
                + F + "mesh-deliverable\n{\"summary\": \"final\"}\n" + F + "\n")
        self.assertEqual(parse_deliverable(text), {"summary": "final"})
        early = (F + "mesh-deliverable\n{\"a\": " + deep + "}\n" + F + "\n")
        self.assertEqual(parse_deliverable(early + "x\n" + F + "mesh-deliverable\n{\"summary\": \"f2\"}\n" + F),
                         {"summary": "f2"})

    def test_no_block_is_fallback(self):
        self.assertEqual(parse_deliverable("plain")["structured"], False)


class TestTmpGitleaksNotSymlink(unittest.TestCase):
    SCRIPTS = ("scripts/coord/sync-github.sh", "scripts/coord/push-review.sh",
               ".kiro/hooks/scripts/secret-scan.sh")

    def test_fallback_refuses_symlink(self):
        for rel in self.SCRIPTS:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            lines = [ln for ln in src.splitlines()
                     if "GL=/tmp/gitleaks" in ln and not ln.lstrip().startswith("#")]
            self.assertEqual(len(lines), 1, rel)
            self.assertIn("[ ! -L /tmp/gitleaks ]", lines[0], rel)
            self.assertIn("[ -O /tmp/gitleaks ]", lines[0], rel)
            self.assertTrue(re.search(r'GL="\$\{GITLEAKS:-', src), rel)

    @unittest.skipUnless(sys.platform != "win32", "POSIX bash/symlink test expression check")
    def test_condition_behaviour(self):
        # The same test expression, against a temp path standing in for /tmp/gitleaks.
        d = tempfile.mkdtemp(prefix="mb6gl-")
        self.addCleanup(shutil.rmtree, d, True)
        real = os.path.join(d, "real")
        with open(real, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(real, 0o755)
        link = os.path.join(d, "link")
        os.symlink(real, link)
        import subprocess
        cond = '[ ! -L "$1" ] && [ -f "$1" ] && [ -x "$1" ] && [ -O "$1" ]'
        rc = lambda p: subprocess.run(["bash", "-c", cond, "x", p]).returncode
        self.assertEqual(rc(real), 0)
        self.assertNotEqual(rc(link), 0)
        self.assertNotEqual(rc(os.path.join(d, "missing")), 0)


if __name__ == "__main__":
    unittest.main()
