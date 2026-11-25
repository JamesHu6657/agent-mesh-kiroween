"""Regression tests for PR #47 round-4 findings (inbox-worker-example.sh).

P2-1: after bash reaps $cmd_pid the PID may be reused; the worker must never probe or
      signal it again, and must not spin on a repeated `wait`.
P2-2: an unreadable /proc/<pid>/stat is "unknown", never "dead"; a lost lease must still
      escalate to SIGKILL; /proc read errors must not leak to stderr.
P3:   the no-/proc ps fallback must use POSIX `ps -A` (BSD `ps -e` lists environment).
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

if sys.platform != "win32":
    import fcntl
else:
    fcntl = None  # type: ignore[assignment]

from tests.unit.test_inbox_worker_kit import SCRIPT as WORKER_SCRIPT, STUB as WORKER_STUB

# The worker spawns many short-lived processes (sleep/ps/subshells) in its own session; a
# concurrent real-/proc scanner can catch one mid-exec (transient EACCES on environ) and
# report an uncertain leftover scan. Hold the host-wide /proc lock exclusively.
MESH_PROC_LOCK = "exclusive"


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


def _flock_held(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def _env(tmp, poll=True):
    os.makedirs(os.path.join(tmp, "ws"), mode=0o700)
    stub = os.path.join(tmp, "mesh")
    body = WORKER_STUB
    if poll:
        body = body.replace(
            'if [ -f "$D/lose" ]; then echo \'{"ok": false, "error": {"code": "LEASE_EXPIRED"}}\'; exit 9; fi',
            'if [ -f "$D/lose" ]; then echo \'{"ok": false, "error": {"code": "LEASE_EXPIRED"}}\'; exit 9; fi\n'
            '        sleep 0.05',
        )
    _write(stub, body, 0o755)
    tok = os.path.join(tmp, "worker.token")
    _write(tok, "tok\n", 0o600)
    _write(os.path.join(tmp, "hb-every"), "0")
    return dict(os.environ, MESH_BIN=stub, MESH_TOKEN_FILE=tok, STUB_DIR=tmp,
                WAIT_TIMEOUT="1s", WAIT_ROUNDS="1", KILL_GRACE_S="1")


def _wait_file(path, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


def _run_until(proc, timeout):
    try:
        _out, err = proc.communicate(timeout=timeout)
        return False, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        _out, err = proc.communicate()
        return True, err


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/* + os.fork/killpg test")
class TestPR47R4(unittest.TestCase):
    def test_reused_pid_after_reap_is_never_signalled_and_no_spin(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r4-reuse-") as tmp:
            env = _env(tmp)
            fake_proc = os.path.join(tmp, "proc")
            os.makedirs(fake_proc)
            kill_log = os.path.join(tmp, "kill.log")
            init = (
                f'mkdir -p "{fake_proc}/$$"\ncat "/proc/$$/stat" > "{fake_proc}/$$/stat"\n'
                f'kill() {{ printf "R=%s kill %s\\n" "${{reaped:-0}}" "$*" >> "{kill_log}"; builtin kill "$@"; }}\n'
            )
            with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                patched = fh.read().replace("/proc", fake_proc).replace("set -u\n", "set -u\n" + init, 1)
            w_sh = os.path.join(tmp, "worker.sh")
            _write(w_sh, patched, 0o755)
            pidf, readyf = os.path.join(tmp, "cmd.pid"), os.path.join(tmp, "cmd.ready")
            # On SIGTERM the command makes its (fake) /proc entry look like an unrelated
            # live daemon that reused the PID, then dies.
            cmd_py = f"""import os, signal, time
pid = os.getpid()
pdir = os.path.join({fake_proc!r}, str(pid))
os.makedirs(pdir, exist_ok=True)
open(os.path.join(pdir, "stat"), "w").write(f"{{pid}} (cmd) S {{os.getppid()}} {{pid}} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100\\n")
def on_term(sig, frame):
    open(os.path.join(pdir, "stat"), "w").write(f"{{pid}} (reused_daemon) S 1 99999 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 99999\\n")
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(pid, signal.SIGTERM)
signal.signal(signal.SIGTERM, on_term)
open({pidf!r}, "w").write(str(pid)); open({readyf!r}, "w").write("1")
time.sleep(20)
"""
            p = subprocess.Popen(["bash", w_sh, "--", sys.executable, "-c", cmd_py], env=env,
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.assertTrue(_wait_file(readyf), "command never started")
            _write(os.path.join(tmp, "lose"), "")
            hung, err = _run_until(p, 5)
            self.assertFalse(hung, f"worker hung after the command was reaped: {err}")
            self.assertEqual(p.returncode, 9, err)
            kills = open(kill_log).read().splitlines() if os.path.exists(kill_log) else []
            cmd_pid = open(pidf).read().strip()
            # The command died of the first SIGTERM: no escalation, and no signal to the bare
            # (possibly reused) PID once the worker had reaped it.
            self.assertFalse([k for k in kills if "-KILL" in k], kills)
            positive = [k for k in kills if f" {cmd_pid}" in " " + " ".join(k.split()[2:]).replace(f"-{cmd_pid}", "")]
            self.assertTrue(positive, kills)
            self.assertFalse([k for k in positive if k.startswith("R=1")], kills)

    def test_unreadable_proc_stat_still_escalates_to_sigkill(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r4-eacces-") as tmp:
            env = _env(tmp, poll=False)
            fake_proc = os.path.join(tmp, "proc")
            os.makedirs(fake_proc)
            init = f'mkdir -p "{fake_proc}/$$"\ncat "/proc/$$/stat" > "{fake_proc}/$$/stat"\n'
            with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                body = fh.read().replace("/proc", fake_proc).replace("set -u\n", "set -u\n" + init, 1)
            w_sh = os.path.join(tmp, "worker2.sh")
            _write(w_sh, body, 0o755)
            lockf, pidf, readyf = (os.path.join(tmp, n) for n in ("lck", "p", "r"))
            self.addCleanup(self._cleanup, fake_proc, pidf)
            cmd_py = f"""import fcntl, os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600); fcntl.flock(fd, fcntl.LOCK_EX)
pid = os.getpid(); pdir = os.path.join({fake_proc!r}, str(pid)); os.makedirs(pdir, exist_ok=True)
st = os.path.join(pdir, "stat"); open(st, "w").write(f"{{pid}} (c) S {{os.getppid()}} {{pid}} 0 0 -1 0 0 0 0 0 0 0 0 0 0 20 0 1 0 100\\n")
os.chmod(st, 0); open({pidf!r}, "w").write(str(pid)); open({readyf!r}, "w").write("1"); time.sleep(20)
"""
            p = subprocess.Popen(["bash", w_sh, "--", sys.executable, "-c", cmd_py], env=env,
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.assertTrue(_wait_file(readyf), "command never started")
            time.sleep(0.15)
            _write(os.path.join(tmp, "lose"), "")
            hung, err = _run_until(p, 6)
            self.assertFalse(hung, err)
            self.assertEqual(p.returncode, 9, err)
            self.assertFalse(_flock_held(lockf), "SIGTERM-ignoring command survived a lost lease")
            self.assertNotIn("Permission denied", err)
            self.assertNotIn("No such file", err)

    def _cleanup(self, fake_proc, pidf):
        for r, _d, fs in os.walk(fake_proc):
            for fn in fs:
                try:
                    os.chmod(os.path.join(r, fn), 0o644)
                except OSError:
                    pass
        try:
            os.kill(int(open(pidf).read()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def _posix_bsd_ps(self, always_fail=False):
        if always_fail:
            return f"#!{sys.executable}\nimport sys\nsys.exit(1)\n"
        return (
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "if not ('-A' in args or '-ax' in args): sys.exit(0)\n"
            "specs, i = [], 0\n"
            "while i < len(args):\n"
            "    a = args[i]\n"
            "    if a == '-o' and i + 1 < len(args):\n"
            "        specs.append(args[i + 1]); i += 2; continue\n"
            "    if a.startswith('-o') and len(a) > 2:\n"
            "        specs.append(a[2:]); i += 1; continue\n"
            "    i += 1\n"
            "cols, headers = [], []\n"
            "for spec in specs:\n"
            "    if '=' in spec:\n"
            "        kw, hdr = spec.split('=', 1)\n"
            "        cols.append(kw); headers.append(hdr)\n"
            "    else:\n"
            "        for kw in spec.replace(',', ' ').split():\n"
            "            cols.append(kw); headers.append(kw.upper())\n"
            "if not cols or any(c not in ('pgid', 'stat') for c in cols): sys.exit(1)\n"
            "if any(headers): print(' '.join(headers))\n"
            "for ent in os.listdir('/proc'):\n"
            "    if not ent.isdigit(): continue\n"
            "    try:\n"
            "        parts = open(f'/proc/{ent}/stat').read().rsplit(') ', 1)[1].split()\n"
            "    except OSError: continue\n"
            "    row = {'pgid': parts[2], 'stat': parts[0]}\n"
            "    print(' '.join(row[c] for c in cols))\n"
        )

    def _run_zombie_under_subreaper(self, w_sh, env, timeout=20.0):
        wrapper = (
            "import ctypes, json, os, signal, subprocess, sys, tempfile, time\n"
            "libc = ctypes.CDLL(None)\n"
            "if libc.prctl(36, 1, 0, 0, 0) != 0: sys.exit(98)\n"
            "tmo = float(sys.argv[1])\n"
            "with tempfile.TemporaryFile('w+', encoding='utf-8') as errf:\n"
            "    t0 = time.monotonic()\n"
            "    p = subprocess.Popen(sys.argv[2:], start_new_session=True,\n"
            "                         stdout=subprocess.DEVNULL, stderr=errf)\n"
            "    try:\n"
            "        p.wait(timeout=tmo)\n"
            "    except subprocess.TimeoutExpired:\n"
            "        try: os.killpg(p.pid, signal.SIGKILL)\n"
            "        except OSError: pass\n"
            "        p.wait(); sys.exit(99)\n"
            "    dt = time.monotonic() - t0\n"
            "    errf.seek(0); err = errf.read()\n"
            "sys.stdout.write(json.dumps({'rc': p.returncode, 'dt': dt, 'err': err}))\n"
        )
        cmd_py = (
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0: os._exit(0)\n"
            "dl = time.monotonic() + 3.0\n"
            "while time.monotonic() < dl:\n"
            "    try:\n"
            "        if open(f'/proc/{pid}/stat').read().rsplit(') ', 1)[1].split()[0] == 'Z': break\n"
            "    except OSError: pass\n"
            "    time.sleep(0.01)\n"
            "sys.exit(0)\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", wrapper, str(timeout), "bash", w_sh, "--", sys.executable, "-c", cmd_py],
            env=env, capture_output=True, text=True, timeout=timeout + 5.0,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_no_proc_ps_fallback_uses_posix_A(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r4-psA-") as tmp:
            env = _env(tmp)
            no_proc = os.path.join(tmp, "no-proc")
            with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                src = fh.read()
            self.assertFalse("ps -e " in src, "BSD ps -e (environment) still used")
            w_sh = os.path.join(tmp, "worker_bsd.sh")
            _write(w_sh, src.replace("/proc", no_proc), 0o755)
            # BSD-like POSIX ps: without -A/-ax it lists nothing, -o splits on first '=' only, no nlwp.
            bindir = os.path.join(tmp, "bin")
            ps_bin = os.path.join(bindir, "ps")
            _write(ps_bin, self._posix_bsd_ps(), 0o755)
            env = dict(env, PATH=f"{bindir}:{env.get('PATH', '')}")
            lockf, pidf, readyf = (os.path.join(tmp, n) for n in ("grp.lock", "grp.pid", "grp.ready"))
            self.addCleanup(self._cleanup, no_proc, pidf)
            # The leader exits at once; a SIGTERM-ignoring grandchild keeps the group alive.
            cmd_py = f"""import fcntl, os, signal, sys, time
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600); fcntl.flock(fd, fcntl.LOCK_EX)
    open({pidf!r}, "w").write(str(os.getpid())); open({readyf!r}, "w").write("1")
    time.sleep(20); os._exit(0)
while not os.path.exists({readyf!r}): time.sleep(0.02)
sys.exit(0)
"""
            p = subprocess.Popen(["bash", w_sh, "--", sys.executable, "-c", cmd_py], env=env,
                                 start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.assertTrue(_wait_file(readyf), "grandchild never started")
            hung, err = _run_until(p, 8)
            self.assertFalse(hung, err)
            self.assertFalse(_flock_held(lockf), "group member survived: ps fallback missed it")

        # Zombie-only PGID under KILL_GRACE_S=5 must be detected empty quickly (< 1.5s),
        # and an always-failing ps must fail that speed check (non-vacuous).
        if sys.platform.startswith("linux"):
            with tempfile.TemporaryDirectory(prefix="mh-pr47r4-zps-") as tmp:
                env = _env(tmp)
                env["KILL_GRACE_S"] = "5"
                no_proc = os.path.join(tmp, "no-proc")
                with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                    src = fh.read()
                w_sh = os.path.join(tmp, "worker_z.sh")
                _write(w_sh, src.replace("/proc", no_proc), 0o755)
                bindir = os.path.join(tmp, "bin")
                ps_bin = os.path.join(bindir, "ps")
                _write(ps_bin, self._posix_bsd_ps(), 0o755)
                env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
                res = self._run_zombie_under_subreaper(w_sh, env)
                self.assertEqual(res["rc"], 0, res["err"])
                self.assertLess(res["dt"], 1.5, f"zombie-only PGID took {res['dt']:.2f}s (expected < 1.5s)")

                _write(ps_bin, self._posix_bsd_ps(always_fail=True), 0o755)
                res_fail = self._run_zombie_under_subreaper(w_sh, env)
                self.assertEqual(res_fail["rc"], 0, res_fail["err"])
                self.assertGreaterEqual(
                    res_fail["dt"], 4.5,
                    f"non-vacuity failed: broken ps still exited in {res_fail['dt']:.2f}s",
                )


if __name__ == "__main__":
    unittest.main()
