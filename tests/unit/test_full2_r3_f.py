"""Regression tests for full2-r3 Group F (P1-9, P1-10, P2-11 vibe.py)."""

import errno
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from mesh import runwrap
from mesh.adapters.agy import AgyAdapter
from mesh.adapters import base as adapter_base
from mesh.adapters.claude import ClaudeAdapter
from mesh.adapters.codex import CodexAdapter
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.fake import FakeAdapter
from mesh.adapters.kiro import KiroAdapter
from mesh.adapters.vibe import VibeAdapter
from mesh.core.paths import make_owned_ancestors_writable, rmtree_allow_readonly


def _run_with_timeout(fn, timeout_s: float = 2.0):
    """Run ``fn()`` in a daemon thread so a blocking FIFO open fails fast instead of hanging."""
    result = []
    err = []

    def _target():
        try:
            result.append(fn())
        except BaseException as exc:
            err.append(exc)

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        raise TimeoutError(f"Operation blocked past {timeout_s}s (likely FIFO open hang)")
    if err:
        raise err[0]
    return result[0]


@unittest.skipUnless(sys.platform != "win32", "POSIX 0o000/0o500 dir chmod and POSIX symlink rmtree tests; Win32 READONLY/junction tests in test_platform_paths_fs.py")
class TestP1_9PathsTraversalAndRmtree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3f-p19-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_make_owned_ancestors_writable_stops_at_symlink_hidden_by_000_dir(self) -> None:
        bound = os.path.join(self.tmp, "bound")
        outside = os.path.join(self.tmp, "outside_victim")
        ro_dir = os.path.join(outside, "ro_dir")
        os.makedirs(bound, mode=0o700)
        os.makedirs(ro_dir, mode=0o700)
        with open(os.path.join(ro_dir, "leaf.txt"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.chmod(ro_dir, 0o500)
        self.addCleanup(lambda: os.path.exists(ro_dir) and os.chmod(ro_dir, 0o700))

        dir_a = os.path.join(bound, "a")
        os.makedirs(dir_a, mode=0o700)
        sym = os.path.join(dir_a, "sym")
        os.symlink(outside, sym)
        os.chmod(dir_a, 0o000)
        self.addCleanup(lambda: os.path.exists(dir_a) and os.chmod(dir_a, 0o700))

        target = os.path.join(sym, "ro_dir", "leaf.txt")
        res = make_owned_ancestors_writable(target, bound)
        self.assertFalse(res)
        self.assertEqual(stat.S_IMODE(os.stat(ro_dir).st_mode), 0o500)

    def test_make_owned_ancestors_writable_legit_000_chain_and_nondir_and_oob(self) -> None:
        bound = os.path.join(self.tmp, "bound2")
        os.makedirs(os.path.join(bound, "x", "y", "z"), mode=0o700)
        x = os.path.join(bound, "x")
        y = os.path.join(x, "y")
        z = os.path.join(y, "z")
        self.addCleanup(lambda: [os.path.exists(p) and os.chmod(p, 0o700) for p in (bound, x, y, z)])
        os.chmod(z, 0o500)
        os.chmod(y, 0o000)
        os.chmod(x, 0o000)
        os.chmod(bound, 0o500)

        self.assertTrue(make_owned_ancestors_writable(os.path.join(z, "leaf.txt"), bound))
        self.assertEqual(stat.S_IMODE(os.stat(x).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(y).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(z).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(bound).st_mode), 0o500)
        os.chmod(bound, 0o700)

        # Non-directory ancestor hidden behind 0o000 stops cleanly.
        d = os.path.join(bound, "d")
        os.makedirs(d, mode=0o700)
        reg = os.path.join(d, "reg")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("file\n")
        os.chmod(d, 0o000)
        self.addCleanup(lambda: os.path.exists(d) and os.chmod(d, 0o700))
        self.assertFalse(make_owned_ancestors_writable(os.path.join(reg, "sub"), bound))

        # Out-of-bounds and .. traversal return False.
        outside = os.path.join(self.tmp, "outside2")
        os.makedirs(os.path.join(outside, "sub"), mode=0o500)
        self.addCleanup(lambda: os.path.exists(os.path.join(outside, "sub")) and os.chmod(os.path.join(outside, "sub"), 0o700))
        self.assertFalse(make_owned_ancestors_writable(os.path.join(outside, "sub", "f"), bound))
        self.assertFalse(make_owned_ancestors_writable(os.path.join(bound, "x", "..", "..", "outside2", "sub", "f"), bound))

    def test_rmtree_allow_readonly_rejects_intermediate_symlink_and_oob_and_toctou(self) -> None:
        bound = os.path.join(self.tmp, "bound_rm")
        outside = os.path.join(self.tmp, "outside_rm")
        sub = os.path.join(outside, "sub")
        os.makedirs(bound, mode=0o700)
        os.makedirs(sub, mode=0o700)
        victim_file = os.path.join(sub, "victim.txt")
        with open(victim_file, "w", encoding="utf-8") as f:
            f.write("do not delete\n")

        sym = os.path.join(bound, "sym")
        os.symlink(outside, sym)

        # Intermediate symlink ancestor must fail without touching outside/sub.
        with self.assertRaises(OSError) as cm:
            rmtree_allow_readonly(os.path.join(sym, "sub"), boundary_real=bound)
        self.assertEqual(cm.exception.errno, errno.EINVAL)
        self.assertTrue(os.path.isfile(victim_file))

        # Symlink target with trailing slash must also fail.
        with self.assertRaises(OSError) as cm_sl:
            rmtree_allow_readonly(sym + os.sep, boundary_real=bound)
        self.assertEqual(cm_sl.exception.errno, errno.EINVAL)
        self.assertTrue(os.path.isfile(victim_file))

        # Out-of-bounds target must fail.
        with self.assertRaises(OSError) as cm_oob:
            rmtree_allow_readonly(sub, boundary_real=bound)
        self.assertEqual(cm_oob.exception.errno, errno.EINVAL)
        self.assertTrue(os.path.isfile(victim_file))

        # Legitimate read-only subtree inside boundary is removed.
        ro_tree = os.path.join(bound, "ro_tree")
        ro_sub = os.path.join(ro_tree, "nested")
        os.makedirs(ro_sub, mode=0o700)
        ro_file = os.path.join(ro_sub, "f.txt")
        with open(ro_file, "w", encoding="utf-8") as f:
            f.write("ok\n")
        os.chmod(ro_file, 0o400)
        os.chmod(ro_sub, 0o500)
        os.chmod(ro_tree, 0o500)
        rmtree_allow_readonly(ro_tree, boundary_real=bound)
        self.assertFalse(os.path.lexists(ro_tree))

        # Post-rmtree symlink swap check catches escape.
        real_dir = os.path.join(bound, "swap_dir")
        os.makedirs(real_dir, mode=0o700)

        def fake_rmtree(p):
            os.rmdir(p)
            os.symlink(outside, p)

        with mock.patch("mesh.core.paths.shutil.rmtree", side_effect=fake_rmtree):
            with self.assertRaises(OSError) as cm_post:
                rmtree_allow_readonly(real_dir, boundary_real=bound)
            self.assertEqual(cm_post.exception.errno, errno.EINVAL)


@unittest.skipUnless(hasattr(os, "mkfifo") and sys.platform != "win32", "POSIX mkfifo and file-symlink nofollow checks in adapters and runwrap")
class TestP1_10AdaptersAndRunwrap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-r3f-p110-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_open_run_file_nofollow_rejects_symlink_fifo_dir_oversized(self) -> None:
        open_run_file_nofollow = adapter_base.open_run_file_nofollow
        reg = os.path.join(self.tmp, "regular.log")
        with open(reg, "w", encoding="utf-8") as f:
            f.write("hello\n")
        with open_run_file_nofollow(reg, max_bytes=100) as f:
            self.assertEqual(f.read(), "hello\n")

        # Symlink (and trailing slash on symlink) rejected
        sym = os.path.join(self.tmp, "sym.log")
        os.symlink(reg, sym)
        with self.assertRaises(OSError):
            open_run_file_nofollow(sym)
        with self.assertRaises(OSError):
            open_run_file_nofollow(sym + os.sep)

        # FIFO rejected without blocking
        fifo = os.path.join(self.tmp, "pipe.log")
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            _run_with_timeout(lambda: open_run_file_nofollow(fifo))

        # Directory rejected
        sub = os.path.join(self.tmp, "adir")
        os.makedirs(sub)
        with self.assertRaises(OSError):
            open_run_file_nofollow(sub)

        # Oversized file rejected
        with self.assertRaises(OSError) as cm_big:
            open_run_file_nofollow(reg, max_bytes=3)
        self.assertEqual(cm_big.exception.errno, errno.EFBIG)

        # Default bound accepts runwrap's 50 MiB cap + marker and rejects larger files
        capped = os.path.join(self.tmp, "capped.log")
        with open(capped, "wb") as f:
            f.truncate(50 * 1024 * 1024 + 43)
        with open_run_file_nofollow(capped) as f:
            self.assertEqual(f.read(1), "\x00")
        over_cap = os.path.join(self.tmp, "over_cap.log")
        with open(over_cap, "wb") as f:
            f.truncate(adapter_base.DEFAULT_MAX_RUN_FILE_BYTES + 1)
        with self.assertRaises(OSError) as cm_cap:
            open_run_file_nofollow(over_cap)
        self.assertEqual(cm_cap.exception.errno, errno.EFBIG)

    def test_all_adapters_parse_rejects_symlinks_on_logs_and_fallbacks(self) -> None:
        outside_stdout = os.path.join(self.tmp, "outside_stdout.txt")
        outside_stderr = os.path.join(self.tmp, "outside_stderr.txt")

        cases = [
            (
                "droid_stdout",
                DroidAdapter(),
                "stdout.log",
                json.dumps({"type": "system", "subtype": "init", "session_id": "LEAK_DROID"}) + "\n"
                + json.dumps({"type": "completion", "finalText": "LEAK_DROID_TEXT"}) + "\n",
                "",
            ),
            (
                "droid_fallback",
                DroidAdapter(),
                "exec-init-event-opus55-high.json",
                json.dumps({"type": "system", "subtype": "init", "session_id": "LEAK_DROID_FB"}) + "\n",
                "",
            ),
            (
                "kiro_stdout",
                KiroAdapter(),
                "stdout.log",
                json.dumps({"type": "metadata", "data": {"sessionId": "LEAK_KIRO"}}) + "\n"
                + json.dumps({"type": "runFinished", "data": {"finalText": "LEAK_KIRO_TEXT", "status": "success"}}) + "\n",
                "",
            ),
            (
                "kiro_fallback",
                KiroAdapter(),
                "effort-high-run-metadata.jsonl",
                json.dumps({"type": "metadata", "data": {"sessionId": "LEAK_KIRO_FB"}}) + "\n",
                "",
            ),
            (
                "agy_stdout",
                AgyAdapter(),
                "stdout.log",
                json.dumps({"event": "init", "conversation_id": "LEAK_AGY", "init": {"model": "m"}}) + "\n"
                + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "LEAK_AGY_TEXT"}}) + "\n",
                "",
            ),
            (
                "agy_stderr",
                AgyAdapter(),
                "stderr.log",
                "",
                'AGY_ERROR: {"short_error": "LEAK_AGY_ERR", "retryable": false}\n',
            ),
            (
                "vibe_stdout",
                VibeAdapter(),
                "stdout.log",
                json.dumps({"type": "message", "role": "assistant", "sessionId": "LEAK_VIBE",
                            "content": [{"type": "text", "text": "LEAK_VIBE_TEXT"}]}) + "\n",
                "",
            ),
            (
                "vibe_stderr",
                VibeAdapter(),
                "stderr.log",
                "",
                "LEAK_VIBE_ERR\n",
            ),
            (
                "fake_stdout",
                FakeAdapter(),
                "stdout.log",
                json.dumps({"session_id": "LEAK_FAKE"}) + "\n",
                "",
            ),
            (
                "codex_stdout",
                CodexAdapter(),
                "stdout.log",
                json.dumps({"type": "thread.started", "thread_id": "LEAK_CODEX"}) + "\n"
                + json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "LEAK_CODEX_TEXT"}}) + "\n"
                + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}) + "\n",
                "",
            ),
            (
                "claude_stdout",
                ClaudeAdapter(),
                "stdout.log",
                json.dumps({"type": "system", "subtype": "init", "session_id": "LEAK_CLAUDE"}) + "\n"
                + json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "LEAK_CLAUDE_TEXT"}) + "\n",
                "",
            ),
        ]

        for label, adapter, rel_name, out_body, err_body in cases:
            with self.subTest(case=label):
                with open(outside_stdout, "w", encoding="utf-8") as f:
                    f.write(out_body)
                with open(outside_stderr, "w", encoding="utf-8") as f:
                    f.write(err_body)
                rdir = os.path.join(self.tmp, f"run_{label}")
                os.makedirs(rdir, mode=0o700)
                if rel_name == "stderr.log":
                    os.symlink(outside_stderr, os.path.join(rdir, rel_name))
                else:
                    os.symlink(outside_stdout, os.path.join(rdir, rel_name))

                res = adapter.parse(rdir)
                self.assertIsNone(res.session_id, label)
                self.assertEqual(res.final_text, "", label)
                self.assertNotIn("LEAK_", json.dumps(res.usage or {}), label)

    def test_all_adapters_parse_rejects_fifo_without_hanging(self) -> None:
        cases = [
            ("droid_stdout", DroidAdapter(), "stdout.log"),
            ("droid_fallback", DroidAdapter(), "exec-init-event-opus55-high.json"),
            ("kiro_stdout", KiroAdapter(), "stdout.log"),
            ("kiro_fallback", KiroAdapter(), "effort-high-run-metadata.jsonl"),
            ("agy_stdout", AgyAdapter(), "stdout.log"),
            ("agy_stderr", AgyAdapter(), "stderr.log"),
            ("vibe_stdout", VibeAdapter(), "stdout.log"),
            ("vibe_stderr", VibeAdapter(), "stderr.log"),
            ("fake_stdout", FakeAdapter(), "stdout.log"),
            ("codex_stdout", CodexAdapter(), "stdout.log"),
            ("claude_stdout", ClaudeAdapter(), "stdout.log"),
        ]
        for label, adapter, rel_name in cases:
            with self.subTest(case=label):
                rdir = os.path.join(self.tmp, f"fifo_{label}")
                os.makedirs(rdir, mode=0o700)
                os.mkfifo(os.path.join(rdir, rel_name))
                res = _run_with_timeout(lambda ad=adapter, d=rdir: ad.parse(d), timeout_s=2.0)
                self.assertEqual(res.final_text, "", label)
                self.assertIsNone(res.session_id, label)

    def test_runwrap_atomic_write_json_rejects_predictable_pid_symlink(self) -> None:
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("KEEP_ME\n")

        # 1. Direct atomic_write_json call with predictable <path>.tmp.<pid> symlink
        target = os.path.join(self.tmp, "wrapper.json")
        os.symlink(victim, f"{target}.tmp.{os.getpid()}")
        runwrap.atomic_write_json(target, {"pid": os.getpid()})
        with open(victim, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "KEEP_ME\n")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

        # 2. Child inside runwrap.run() placing exit.json.tmp.<ppid> symlink
        rdir = os.path.join(self.tmp, "rw_child_sym")
        os.makedirs(rdir, mode=0o700)
        child_script = (
            "import os, sys\n"
            "rdir, victim = sys.argv[1], sys.argv[2]\n"
            "os.symlink(victim, os.path.join(rdir, f'exit.json.tmp.{os.getppid()}'))\n"
            "print('child done')\n"
        )
        with open(os.path.join(rdir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": [sys.executable, "-c", child_script, rdir, victim], "cwd": rdir, "stdin": "devnull"}, f)

        rc = runwrap.run(rdir)
        self.assertEqual(rc, 0)
        with open(victim, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "KEEP_ME\n")
        with open(os.path.join(rdir, "exit.json"), "r", encoding="utf-8") as f:
            exit_doc = json.loads(f.read())
        self.assertEqual(exit_doc["exit_code"], 0)

    def test_runwrap_rejects_symlink_and_fifo_logs_and_activity_nofollow(self) -> None:
        victim_log = os.path.join(self.tmp, "victim_log.txt")
        with open(victim_log, "w", encoding="utf-8") as f:
            f.write("DO_NOT_TRUNCATE\n")

        # 1. Symlinked stdout.log must not truncate target, and records capture_error
        rdir_sym = os.path.join(self.tmp, "rw_log_sym")
        os.makedirs(rdir_sym, mode=0o700)
        os.symlink(victim_log, os.path.join(rdir_sym, "stdout.log"))
        with open(os.path.join(rdir_sym, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": [sys.executable, "-c", "print('hello')"], "cwd": rdir_sym, "stdin": "devnull"}, f)

        self.assertEqual(runwrap.run(rdir_sym), 0)
        with open(victim_log, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "DO_NOT_TRUNCATE\n")
        with open(os.path.join(rdir_sym, "exit.json"), "r", encoding="utf-8") as f:
            exit_sym = json.loads(f.read())
        self.assertIn("capture_error", exit_sym)

        # 2. FIFO stdout.log must not hang, and records capture_error
        rdir_fifo = os.path.join(self.tmp, "rw_log_fifo")
        os.makedirs(rdir_fifo, mode=0o700)
        os.mkfifo(os.path.join(rdir_fifo, "stdout.log"))
        with open(os.path.join(rdir_fifo, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": [sys.executable, "-c", "print('hello')"], "cwd": rdir_fifo, "stdin": "devnull"}, f)

        rc_fifo = _run_with_timeout(lambda: runwrap.run(rdir_fifo), timeout_s=5.0)
        self.assertEqual(rc_fifo, 0)
        with open(os.path.join(rdir_fifo, "exit.json"), "r", encoding="utf-8") as f:
            exit_fifo = json.loads(f.read())
        self.assertIn("capture_error", exit_fifo)

        # 3. Symlinked activity file must not be touched/followed in _pump_stream or run()
        victim_act = os.path.join(self.tmp, "victim_act.txt")
        with open(victim_act, "w", encoding="utf-8") as f:
            f.write("act\n")
        old_ts = 1_500_000_000.0
        os.utime(victim_act, (old_ts, old_ts))

        rdir_act = os.path.join(self.tmp, "rw_act_sym")
        os.makedirs(rdir_act, mode=0o700)
        act_link = os.path.join(rdir_act, "activity")
        os.symlink(victim_act, act_link)
        # Child replaces activity or pre-existing symlink is checked by _pump_stream utime(follow_symlinks=False)
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"line1\n")
        os.close(w_fd)
        with os.fdopen(r_fd, "rb") as pipe_r:
            runwrap._pump_stream(
                pipe_r,
                os.path.join(rdir_act, "stdout.log"),
                [],
                act_link,
                1024 * 1024,
            )
        self.assertAlmostEqual(os.stat(victim_act).st_mtime, old_ts, delta=1.0)

        with open(os.path.join(rdir_act, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": [sys.executable, "-c", "pass"], "cwd": rdir_act, "stdin": "devnull"}, f)
        with self.assertRaises(OSError):
            runwrap.run(rdir_act)

        # 4. Normal existing regular activity (append preserves) & existing regular log (truncates)
        rdir_norm = os.path.join(self.tmp, "rw_norm")
        os.makedirs(rdir_norm, mode=0o700)
        with open(os.path.join(rdir_norm, "activity"), "w", encoding="utf-8") as f:
            f.write("keep\n")
        with open(os.path.join(rdir_norm, "stdout.log"), "w", encoding="utf-8") as f:
            f.write("old_stale_log\n")
        with open(os.path.join(rdir_norm, "spec.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": [sys.executable, "-c", "print('fresh_out')"], "cwd": rdir_norm, "stdin": "devnull"}, f)
        self.assertEqual(runwrap.run(rdir_norm), 0)
        with open(os.path.join(rdir_norm, "activity"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep\n")
        with open(os.path.join(rdir_norm, "stdout.log"), "r", encoding="utf-8") as f:
            out_txt = f.read()
        self.assertIn("fresh_out", out_txt)
        self.assertNotIn("old_stale_log", out_txt)


class TestP2_11VibeTrustWorkdirContract(unittest.TestCase):
    def test_vibe_trust_workdir_n15_opt_in_contract_preserved(self) -> None:
        from mesh.adapters.base import RunRequest

        with tempfile.TemporaryDirectory(prefix="mh-r3f-vibe-") as tmp:
            pfile = os.path.join(tmp, "prompt.md")
            with open(pfile, "w", encoding="utf-8") as f:
                f.write("task\n")
            vibe = VibeAdapter()
            for profile in ("readonly", "edit", "dev", "unrestricted"):
                spec_default = vibe.build(
                    RunRequest(prompt_path=pfile, workspace=tmp, model="m", effort=None, profile=profile)
                )
                self.assertNotIn("--trust", spec_default.argv)
                spec_opt_in = vibe.build(
                    RunRequest(
                        prompt_path=pfile,
                        workspace=tmp,
                        model="m",
                        effort=None,
                        profile=profile,
                        options={"trust_workdir": True},
                    )
                )
                self.assertIn("--trust", spec_opt_in.argv)


if __name__ == "__main__":
    unittest.main()
