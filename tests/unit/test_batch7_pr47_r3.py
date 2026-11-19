"""Regression tests for batch7 PR #47 round-3 review findings (2x P2)."""

import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests import run_tier
from tests.unit.test_inbox_worker_kit import SCRIPT as WORKER_SCRIPT, STUB as WORKER_STUB

# Worker tests inspect /proc; coordinate with other /proc-scanning unit modules.
MESH_PROC_LOCK = "shared"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mode is not None:
        os.chmod(path, mode)


def _supports_leader_zombie():
    """Return True if this host can create a multithreaded process whose main thread exits into Z."""
    probe = (
        "import ctypes, os, threading, time\n"
        "ev = threading.Event()\n"
        "def w():\n"
        "    ev.set()\n"
        "    time.sleep(2)\n"
        "t = threading.Thread(target=w, daemon=True)\n"
        "t.start()\n"
        "ev.wait()\n"
        "libc = ctypes.CDLL(None)\n"
        "if hasattr(libc, 'pthread_exit'):\n"
        "    libc.pthread_exit(None)\n"
    )
    try:
        p = subprocess.Popen(
            [sys.executable, "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                with open(f"/proc/{p.pid}/stat", encoding="utf-8") as fh:
                    raw = fh.read()
                after = raw.rsplit(") ", 1)[1].split()
                if after[0] == "Z" and int(after[17]) > 1:
                    return True
            except (OSError, IndexError, ValueError):
                pass
            if p.poll() is not None:
                return False
            time.sleep(0.02)
        return False
    finally:
        try:
            os.kill(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass


def _leader_zombie_cmd(lockf, pidf, readyf, ignore_term=False):
    term_line = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else ""
    return (
        "import ctypes, fcntl, os, signal, threading, time\n"
        f"{term_line}"
        f"fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "ev = threading.Event()\n"
        "def w():\n"
        "    ev.set()\n"
        "    time.sleep(20)\n"
        "threading.Thread(target=w, daemon=False).start()\n"
        "ev.wait()\n"
        f"with open({pidf!r}, 'w') as pf:\n"
        "    pf.write(str(os.getpid()))\n"
        f"with open({readyf!r}, 'w') as rf:\n"
        "    rf.write('1')\n"
        "devnull = os.open(os.devnull, os.O_RDWR)\n"
        "os.dup2(devnull, 1); os.dup2(devnull, 2)\n"
        "ctypes.CDLL(None).pthread_exit(None)\n"
    )


def _is_flock_held(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def _wait_for_leader_zombie(pidf, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(pidf):
            try:
                with open(pidf, encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                    after = fh.read().rsplit(") ", 1)[1].split()
                if after[0] == "Z" and int(after[17]) > 1:
                    return pid
            except (OSError, ValueError, IndexError):
                pass
        time.sleep(0.02)
    return None


def _kill_proc_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=2)
    except Exception:
        pass


class TestFinding1InboxWorkerLeaderZombieAndNoProcWait(unittest.TestCase):
    """P2: inbox-worker-example.sh must treat Z with live threads as alive and handle missing /proc + interrupted wait."""

    def _setup_worker(self, tmp, hb_every="1", lose=False):
        os.makedirs(os.path.join(tmp, "ws"), mode=0o700)
        stub = os.path.join(tmp, "mesh")
        custom_stub = WORKER_STUB.replace(
            'if [ -f "$D/lose" ]; then echo \'{"ok": false, "error": {"code": "LEASE_EXPIRED"}}\'; exit 9; fi',
            'if [ -f "$D/lose" ]; then echo \'{"ok": false, "error": {"code": "LEASE_EXPIRED"}}\'; exit 9; fi\n'
            '        if [ -f "$D/hb-poll" ]; then sleep 0.05; fi',
        )
        _write(stub, custom_stub, 0o755)
        tok = os.path.join(tmp, "worker.token")
        _write(tok, "tok\n", 0o600)
        _write(os.path.join(tmp, "hb-every"), hb_every)
        if lose:
            _write(os.path.join(tmp, "lose"), "")
        return dict(
            os.environ,
            MESH_BIN=stub,
            MESH_TOKEN_FILE=tok,
            STUB_DIR=tmp,
            WAIT_TIMEOUT="1s",
            WAIT_ROUNDS="1",
            KILL_GRACE_S="1",
        )

    def _kill_pidfile(self, pidf):
        if os.path.exists(pidf):
            try:
                with open(pidf, encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def test_leader_zombie_pthread_exit_killed_on_lease_loss(self):
        if not _supports_leader_zombie():
            self.skipTest("host cannot create multithreaded Z leader with live worker thread")

        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term):
                with tempfile.TemporaryDirectory(prefix="mh-pr47r3-lz-") as tmp:
                    env = self._setup_worker(tmp, hb_every="0", lose=False)
                    _write(os.path.join(tmp, "hb-poll"), "")
                    lockf = os.path.join(tmp, "alive.lock")
                    pidf = os.path.join(tmp, "cmd.pid")
                    readyf = os.path.join(tmp, "cmd.ready")
                    self.addCleanup(self._kill_pidfile, pidf)

                    cmd_py = _leader_zombie_cmd(lockf, pidf, readyf, ignore_term=ignore_term)
                    proc = subprocess.Popen(
                        ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", cmd_py],
                        env=env,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        pid = _wait_for_leader_zombie(pidf, timeout=6.0)
                        self.assertIsNotNone(pid, "child never entered leader-zombie Z state")
                        _write(os.path.join(tmp, "lose"), "")
                        _, err = proc.communicate(timeout=5)
                    except Exception:
                        _kill_proc_group(proc)
                        raise

                    self.assertEqual(proc.returncode, 9, err)
                    self.assertFalse(
                        _is_flock_held(lockf),
                        f"leader-zombie worker thread survived lease loss (ignore_term={ignore_term})",
                    )

    def test_leader_zombie_killed_on_worker_sigterm_and_as_pgid_leftover(self):
        if not _supports_leader_zombie():
            self.skipTest("host cannot create multithreaded Z leader with live worker thread")

        # (a) External SIGTERM to worker must terminate a leader-zombie child (even ignoring SIGTERM).
        with tempfile.TemporaryDirectory(prefix="mh-pr47r3-lzterm-") as tmp:
            env = self._setup_worker(tmp, hb_every="40", lose=False)
            lockf = os.path.join(tmp, "alive.lock")
            pidf = os.path.join(tmp, "cmd.pid")
            readyf = os.path.join(tmp, "cmd.ready")
            self.addCleanup(self._kill_pidfile, pidf)

            cmd_py = _leader_zombie_cmd(lockf, pidf, readyf, ignore_term=True)
            proc = subprocess.Popen(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", cmd_py],
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                pid = _wait_for_leader_zombie(pidf, timeout=6.0)
                self.assertIsNotNone(pid, "child never entered leader-zombie Z state")
                proc.send_signal(signal.SIGTERM)
                _, err = proc.communicate(timeout=5)
            except Exception:
                _kill_proc_group(proc)
                raise

            self.assertEqual(proc.returncode, 143, err)
            self.assertFalse(_is_flock_held(lockf), "leader-zombie survived worker SIGTERM")

        # (b) Normal command exit leaving a leader-zombie background process in PGID must kill it before submit.
        with tempfile.TemporaryDirectory(prefix="mh-pr47r3-lzbg-") as tmp:
            env = self._setup_worker(tmp, hb_every="40", lose=False)
            lockf = os.path.join(tmp, "bg.lock")
            pidf = os.path.join(tmp, "bg.pid")
            readyf = os.path.join(tmp, "bg.ready")
            self.addCleanup(self._kill_pidfile, pidf)

            bg_py = _leader_zombie_cmd(lockf, pidf, readyf, ignore_term=True)
            launcher_py = (
                "import os, subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {bg_py!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                f"deadline = time.monotonic() + 5\n"
                f"while time.monotonic() < deadline and not os.path.exists({readyf!r}): time.sleep(0.02)\n"
                "time.sleep(0.2)\n"
            )
            res = subprocess.run(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", launcher_py],
                env=env,
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(_is_flock_held(lockf), "leader-zombie leftover in PGID survived after command exit")

    def test_fake_proc_tree_decision_logic_for_tgid_pgid_and_pid_is_dead(self):
        with open(WORKER_SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(
            r"(?ms)^(_proc_tgid_alive\(\)|_pgid_has_live\(\)).*?(?=^cleanup\(\))",
            src,
        )
        self.assertIsNotNone(m, "could not locate worker process helper block")
        helper_block = m.group(0)

        with tempfile.TemporaryDirectory(prefix="mh-pr47r3-fproc-") as tmp:
            fake_proc = os.path.join(tmp, "proc")
            patched_helpers = helper_block.replace("/proc", fake_proc)

            def _make_proc_entry(pid, comm, state, pgid, num_threads, status_threads=None, tasks=None):
                stat_line = f"{pid} ({comm}) {state} 1 {pgid} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 {num_threads} 0 12345\n"
                _write(os.path.join(fake_proc, str(pid), "stat"), stat_line)
                if status_threads is not None:
                    _write(
                        os.path.join(fake_proc, str(pid), "status"),
                        f"Name:\t{comm}\nState:\t{state}\nThreads:\t{status_threads}\n",
                    )
                if tasks is not None:
                    for tid, tstate in tasks:
                        _write(
                            os.path.join(fake_proc, str(pid), "task", str(tid), "stat"),
                            f"{tid} ({comm}) {tstate} 1 {pgid} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 {num_threads} 0 12345\n",
                        )

            _make_proc_entry(101, "my ) zomb", "Z", 500, 1, status_threads=1, tasks=[(101, "Z")])
            _make_proc_entry(102, "my ) mtz", "Z", 501, 2, status_threads=2, tasks=[(102, "Z"), (103, "S")])
            _make_proc_entry(104, "task_only", "Z", 502, 1, status_threads=1, tasks=[(104, "Z"), (105, "S")])
            _make_proc_entry(106, "status_only", "Z", 503, 1, status_threads=2, tasks=[(106, "Z")])
            _make_proc_entry(107, "alive_s", "S", 504, 1)
            _make_proc_entry(108, "dead_x", "X", 505, 1, status_threads=1)

            script = f"""
set -u
mkdir -p "{fake_proc}/$$"
echo "$$ (bash) S 1 $$ 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1" > "{fake_proc}/$$/stat"
kill() {{ return 0; }}
{patched_helpers}

declare -F _proc_tgid_alive >/dev/null || {{ echo "missing _proc_tgid_alive"; exit 11; }}
declare -F _cmd_running >/dev/null || {{ echo "missing _cmd_running"; exit 12; }}
declare -F _reap_cmd >/dev/null || {{ echo "missing _reap_cmd"; exit 13; }}
tmp="{tmp}"; reaped=0; cmd_pid=""; pgid_gone=0; _jn=0

! _proc_tgid_alive 101 || {{ echo "true zombie 101 reported alive"; exit 21; }}
! _pgid_has_live 500 || {{ echo "pgid 500 with only true zombie reported live"; exit 23; }}

_proc_tgid_alive 102 || {{ echo "leader-zombie 102 (num_threads=2) reported dead"; exit 24; }}
_pgid_has_live 501 || {{ echo "pgid 501 with leader-zombie 102 reported empty"; exit 26; }}

_proc_tgid_alive 104 || {{ echo "leader-zombie 104 (live task) reported dead"; exit 27; }}
_pgid_has_live 502 || {{ echo "pgid 502 with live task reported empty"; exit 28; }}

_proc_tgid_alive 106 || {{ echo "leader-zombie 106 (status Threads: 2) reported dead"; exit 29; }}
_pgid_has_live 503 || {{ echo "pgid 503 with status Threads: 2 reported empty"; exit 30; }}

_proc_tgid_alive 107 || {{ echo "sleeping 107 reported dead"; exit 31; }}
_pgid_has_live 504 || {{ echo "pgid 504 reported empty"; exit 32; }}

! _proc_tgid_alive 108 || {{ echo "dead X 108 reported alive"; exit 33; }}
! _pgid_has_live 505 || {{ echo "pgid 505 with dead X reported live"; exit 34; }}

! _proc_tgid_alive 999999 || {{ echo "missing pid 999999 reported alive"; exit 35; }}

# A PID that is not this shell's job (e.g. the "alive" 107 or a reused PID) is never running.
cmd_pid=107
! _cmd_running || {{ echo "_cmd_running claimed foreign pid 107"; exit 37; }}
command sleep 2 &
cmd_pid=$!
_cmd_running || {{ echo "_cmd_running missed an active job"; exit 38; }}
builtin kill -KILL "$cmd_pid" 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do _cmd_running || break; command sleep 0.05; done
! _cmd_running || {{ echo "_cmd_running still true after the job died"; exit 39; }}
_reap_cmd
[ "$reaped" -eq 1 ] && [ "$cmd_rc" -eq 137 ] || {{ echo "_reap_cmd rc=$cmd_rc reaped=$reaped"; exit 40; }}
! _cmd_running || {{ echo "_cmd_running true after reap"; exit 41; }}
echo ALL_FAKE_PROC_OK
"""
            res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("ALL_FAKE_PROC_OK", res.stdout)

    def test_missing_proc_pgid_ready_ps_fallbacks_and_interrupted_wait(self):
        with open(WORKER_SCRIPT, encoding="utf-8") as fh:
            worker_src = fh.read()

        # Subcase A: missing /proc + BSD-style ps (no nlwp) + SIG_IGN child + lease loss.
        with self.subTest("missing_proc_bsd_ps"):
            with tempfile.TemporaryDirectory(prefix="mh-pr47r3-noproc-bsd-") as tmp:
                env = self._setup_worker(tmp, hb_every="0", lose=False)
                _write(os.path.join(tmp, "hb-poll"), "")
                no_proc = os.path.join(tmp, "no-proc")
                patched_script = os.path.join(tmp, "worker_noproc.sh")
                _write(patched_script, worker_src.replace("/proc", no_proc), 0o755)

                bindir = os.path.join(tmp, "bin")
                fake_ps = os.path.join(bindir, "ps")
                _write(
                    fake_ps,
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if any('nlwp' in a for a in args):\n"
                    "    sys.exit(1)\n"
                    "def read_stat(pid):\n"
                    "    with open(f'/proc/{pid}/stat') as f:\n"
                    "        parts = f.read().rsplit(') ', 1)[1].split()\n"
                    "    return parts[0], parts[2]\n"
                    "if '-p' in args:\n"
                    "    pid = int(args[args.index('-p') + 1])\n"
                    "    try:\n"
                    "        st, _ = read_stat(pid)\n"
                    "        print(st)\n"
                    "        sys.exit(0)\n"
                    "    except OSError:\n"
                    "        sys.exit(1)\n"
                    "for ent in os.listdir('/proc'):\n"
                    "    if not ent.isdigit(): continue\n"
                    "    try:\n"
                    "        st, pg = read_stat(int(ent))\n"
                    "        print(f'{pg} {st}')\n"
                    "    except OSError:\n"
                    "        pass\n",
                    0o755,
                )
                env_bsd = dict(env, PATH=f"{bindir}:{env.get('PATH', '')}")

                lockf = os.path.join(tmp, "alive.lock")
                pidf = os.path.join(tmp, "child.pid")
                readyf = os.path.join(tmp, "child.ready")
                self.addCleanup(self._kill_pidfile, pidf)
                child_py = (
                    "import fcntl, os, signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
                    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                    f"with open({pidf!r}, 'w') as pf: pf.write(str(os.getpid()))\n"
                    f"with open({readyf!r}, 'w') as rf: rf.write('1')\n"
                    "time.sleep(20)\n"
                )
                proc = subprocess.Popen(
                    ["bash", patched_script, "--", sys.executable, "-c", child_py],
                    env=env_bsd,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and not os.path.exists(readyf):
                        time.sleep(0.02)
                    self.assertTrue(os.path.exists(readyf), "child never started")
                    _write(os.path.join(tmp, "lose"), "")
                    _, err = proc.communicate(timeout=5)
                except Exception:
                    _kill_proc_group(proc)
                    raise

                self.assertEqual(proc.returncode, 9, err)
                self.assertFalse(_is_flock_held(lockf), "child survived lease loss under no-/proc + BSD ps")

        # Subcase B: missing /proc + Linux ps (with nlwp) detecting Zl leader-zombie!
        if _supports_leader_zombie():
            with self.subTest("missing_proc_linux_ps_nlwp"):
                with tempfile.TemporaryDirectory(prefix="mh-pr47r3-noproc-nlwp-") as tmp:
                    env = self._setup_worker(tmp, hb_every="0", lose=False)
                    _write(os.path.join(tmp, "hb-poll"), "")
                    no_proc = os.path.join(tmp, "no-proc")
                    patched_script = os.path.join(tmp, "worker_noproc_ps.sh")
                    _write(patched_script, worker_src.replace("/proc", no_proc), 0o755)

                    lockf = os.path.join(tmp, "lz.lock")
                    pidf = os.path.join(tmp, "lz.pid")
                    readyf = os.path.join(tmp, "lz.ready")
                    self.addCleanup(self._kill_pidfile, pidf)
                    cmd_py = _leader_zombie_cmd(lockf, pidf, readyf, ignore_term=True)
                    proc = subprocess.Popen(
                        ["bash", patched_script, "--", sys.executable, "-c", cmd_py],
                        env=env,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    try:
                        pid = _wait_for_leader_zombie(pidf, timeout=6.0)
                        self.assertIsNotNone(pid, "leader-zombie never reached Z")
                        _write(os.path.join(tmp, "lose"), "")
                        _, err = proc.communicate(timeout=5)
                    except Exception:
                        _kill_proc_group(proc)
                        raise

                    self.assertEqual(proc.returncode, 9, err)
                    self.assertFalse(_is_flock_held(lockf), "leader-zombie survived under no-/proc + ps -o nlwp")

        # Subcase C: missing /proc + broken ps + delayed setpgid/pgid_ready + forced wait interruption.
        with self.subTest("missing_proc_no_ps_and_interrupted_wait"):
            with tempfile.TemporaryDirectory(prefix="mh-pr47r3-noproc-nops-") as tmp:
                env = self._setup_worker(tmp, hb_every="0", lose=False)
                _write(os.path.join(tmp, "hb-poll"), "")
                no_proc = os.path.join(tmp, "no-proc")
                patched_script = os.path.join(tmp, "worker_nops.sh")
                lockf = os.path.join(tmp, "nops.lock")
                pidf = os.path.join(tmp, "nops.pid")
                readyf = os.path.join(tmp, "nops.ready")
                early_break = os.path.join(tmp, "pgid_ready.early_break")
                wait_entered = os.path.join(tmp, "wait.entered")
                self.addCleanup(self._kill_pidfile, pidf)

                body = worker_src.replace("/proc", no_proc).replace(
                    "os.setpgid(0, 0)",
                    "import time; time.sleep(0.4); os.setpgid(0, 0)",
                )
                # Inject a one-shot _pid_is_dead override right after _signal_cmd() so after
                # readyf exists (and after pgid_ready loop finished), main loop enters blocking
                # `wait "$cmd_pid"`, then USR1 interrupts `wait` with rc=138 while child lives.
                hook_inject = (
                    f"\n_orig_pid_is_dead() {{ _pid_is_dead_impl \"$@\"; }}\n"
                    f"_pid_is_dead() {{\n"
                    f"  if [ -e {readyf!r} ] && [ ! -e {wait_entered!r} ]; then\n"
                    f"    : > {wait_entered!r}\n"
                    f"    return 0\n"
                    f"  fi\n"
                    f"  _orig_pid_is_dead \"$@\"\n"
                    f"}}\n"
                )
                if "_cmd_running()" in body:
                    # R4: no blocking `wait` on a running job any more; mark the first poll
                    # after the child is ready, then lose the lease while it still runs.
                    body = body.replace(
                        "      break\n    fi\n    sleep 0.1 2>/dev/null || sleep 1",
                        f"      break\n    fi\n    [ -e {readyf!r} ] && : > {wait_entered!r}\n    sleep 0.1 2>/dev/null || sleep 1",
                        1,
                    )
                elif "_pid_is_dead()" in body:
                    body = body.replace("_pid_is_dead()", "_pid_is_dead_impl()", 1)
                    body = body.replace("cleanup() {", hook_inject + "cleanup() {", 1)
                else:
                    body = body.replace('wait "$cmd_pid"', f': > {wait_entered!r}; wait "$cmd_pid"', 1)
                _write(patched_script, body, 0o755)

                bindir = os.path.join(tmp, "bin")
                _write(os.path.join(bindir, "ps"), "#!/bin/sh\nexit 1\n", 0o755)
                env_nops = dict(env, PATH=f"{bindir}:{env.get('PATH', '')}")

                child_py = (
                    "import fcntl, os, signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
                    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                    f"with open({pidf!r}, 'w') as pf: pf.write(str(os.getpid()))\n"
                    f"with open({readyf!r}, 'w') as rf: rf.write('1')\n"
                    "time.sleep(20)\n"
                )
                wrapper = f"""
sleep() {{
  if [ "${{1:-}}" = "0.1" ] && [ -n "${{pgid_ready:-}}" ] && [ ! -e "$pgid_ready" ]; then
    : > {early_break!r}
  fi
  command sleep "$@"
}}
export -f sleep
exec bash "$@"
"""
                proc = subprocess.Popen(
                    ["bash", "-c", wrapper, "_", patched_script, "--", sys.executable, "-c", child_py],
                    env=env_nops,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and not os.path.exists(wait_entered):
                        time.sleep(0.02)
                    self.assertTrue(os.path.exists(wait_entered), "never entered wait hook")
                    self.assertTrue(os.path.exists(readyf), "pgid_ready loop exited before child finished setpgid")
                    self.assertFalse(os.path.exists(early_break), "pgid_ready loop exited early before pgid.ready")
                    _write(os.path.join(tmp, "lose"), "")
                    _, err = proc.communicate(timeout=5)
                except Exception:
                    _kill_proc_group(proc)
                    raise

                self.assertEqual(proc.returncode, 9, err)
                self.assertFalse(_is_flock_held(lockf), "SIG_IGN child survived interrupted wait on lease loss")


class TestFinding2RunTierSymlinkAndIdentifierCheck(unittest.TestCase):
    """P2: tests/run_tier.py must use realpath consistently and reject invalid/keyword package names."""

    def _clean_env(self, tmp):
        env = dict(
            os.environ,
            MESH_TEST_DURATIONS=os.path.join(tmp, "dur.json"),
            MESH_TEST_PROC_LOCK=os.path.join(tmp, "p.lock"),
        )
        env.pop("PYTHONPATH", None)
        return env

    def test_symlinked_working_dir_and_repo_root_lift_top_level_dir(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r3-sym-") as tmp:
            env = self._clean_env(tmp)
            real_w = os.path.join(tmp, "real_work")
            sym_w = os.path.join(tmp, "sym_work")
            os.makedirs(os.path.join(real_w, "ext_pkg", "sub"))
            os.symlink(real_w, sym_w)
            _write(os.path.join(real_w, "ext_pkg", "__init__.py"), "")
            _write(os.path.join(real_w, "ext_pkg", "shared.py"), "X = 42\n")
            _write(os.path.join(real_w, "ext_pkg", "sub", "__init__.py"), "")
            _write(
                os.path.join(real_w, "ext_pkg", "sub", "test_a.py"),
                "import unittest\n"
                "from ext_pkg import shared\n"
                "from .. import shared as rel_shared\n"
                "class T(unittest.TestCase):\n"
                "    def test_1(self):\n"
                "        self.assertEqual(shared.X, 42)\n"
                "        self.assertEqual(rel_shared.X, 42)\n",
            )

            runner = os.path.realpath(os.path.join(ROOT, "tests", "run_tier.py"))
            r_sym = subprocess.run(
                ["bash", "-c", 'cd "$1" && "$2" "$3" "$PWD/ext_pkg/sub"', "_", sym_w, sys.executable, runner],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_sym.returncode, 0, r_sym.stdout + r_sym.stderr)
            self.assertIn("ok ext_pkg.sub.test_a.T.test_1", r_sym.stdout)
            self.assertIn("OK\n", r_sym.stdout)

            # Also verify symlinked REPO_ROOT vs physical target (and vice versa).
            real_repo = os.path.join(tmp, "real_repo")
            sym_repo = os.path.join(tmp, "sym_repo")
            os.makedirs(os.path.join(real_repo, "tests", "unit"))
            os.symlink(real_repo, sym_repo)
            shutil.copy2(runner, os.path.join(real_repo, "tests", "run_tier.py"))
            _write(os.path.join(real_repo, "tests", "__init__.py"), "")
            _write(os.path.join(real_repo, "tests", "unit", "__init__.py"), "")
            _write(os.path.join(real_repo, "fakemesh", "__init__.py"), "VAL = 'sym-ok'\n")
            _write(
                os.path.join(real_repo, "tests", "unit", "test_repo_sym.py"),
                "import unittest, fakemesh\n"
                "from .. import __init__ as tests_pkg\n"
                "class R(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertEqual(fakemesh.VAL, 'sym-ok')\n"
                "        self.assertIsNotNone(tests_pkg)\n",
            )
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            for run_path, target_path in (
                (os.path.join(sym_repo, "tests", "run_tier.py"), os.path.join(real_repo, "tests", "unit")),
                (os.path.join(real_repo, "tests", "run_tier.py"), os.path.join(sym_repo, "tests", "unit")),
            ):
                res_repo = subprocess.run(
                    [sys.executable, run_path, target_path],
                    cwd=outside,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(res_repo.returncode, 0, res_repo.stdout + res_repo.stderr)
                self.assertIn("ok tests.unit.test_repo_sym.R.test_ok", res_repo.stdout)

            self.assertTrue(run_tier._is_importable_under(os.path.join(sym_w, "ext_pkg", "sub"), real_w))
            self.assertTrue(run_tier._is_importable_under(os.path.join(real_w, "ext_pkg", "sub"), sym_w))

    def test_invalid_identifier_and_keyword_package_dirs_do_not_lift_top_level(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r3-ident-") as tmp:
            env = self._clean_env(tmp)
            work = os.path.join(tmp, "work")
            runner = os.path.realpath(os.path.join(ROOT, "tests", "run_tier.py"))

            bad_pkg_paths = [
                "rel.1",
                os.path.join("rel.1", "sub"),
                "my-pkg",
                "123pkg",
                "class",
                os.path.join("import", "nested"),
            ]
            for rel_dir in bad_pkg_paths:
                full_dir = os.path.join(work, rel_dir)
                cur = work
                for part in rel_dir.split(os.sep):
                    cur = os.path.join(cur, part)
                    _write(os.path.join(cur, "__init__.py"), "")
                _write(
                    os.path.join(full_dir, "test_r.py"),
                    "import unittest\n"
                    "class R(unittest.TestCase):\n"
                    "    def test_r(self): pass\n",
                )
                self.assertFalse(
                    run_tier._is_importable_under(full_dir, work),
                    f"{rel_dir} must not be treated as importable under {work}",
                )
                res = subprocess.run(
                    [sys.executable, runner, rel_dir],
                    cwd=work,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(res.returncode, 0, f"{rel_dir}: {res.stdout}\n{res.stderr}")
                self.assertIn("OK\n", res.stdout, rel_dir)

            # Soft keyword ('match') is still a valid Python package name and should remain importable.
            _write(os.path.join(work, "match", "__init__.py"), "")
            self.assertTrue(run_tier._is_importable_under(os.path.join(work, "match"), work))


if __name__ == "__main__":
    unittest.main()
