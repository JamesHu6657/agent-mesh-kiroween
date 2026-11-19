"""Regression tests for batch7 PR #47 round-5 review findings (P2 + P3).

P2: `_pgid_has_live` must never defer to `ps` when `/proc` is readable and the scan
    hit an unreadable entry (EACCES/EIO); `ps` reads the same `/proc` and silently skips
    unreadable entries. Treat as live (fail closed), bounded by KILL_GRACE_S -> SIGKILL.
P3: `_pgid_has_live_ps` must pass separate `-o` column arguments per POSIX/BSD `ps`
    (splitting on first `=` treats `-o pgid=,nlwp=,stat=` as one column) and treat an
    empty `stat` column as unknown (return 2). Tests must be non-vacuous.
"""

import ctypes
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.unit.test_inbox_worker_kit import SCRIPT as WORKER_SCRIPT, STUB as WORKER_STUB

# Worker spawns short-lived processes in its own session; hold /proc lock exclusively.
MESH_PROC_LOCK = "exclusive"


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
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


def _env(tmp, poll=False, grace_s="1"):
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
    return dict(
        os.environ,
        MESH_BIN=stub,
        MESH_TOKEN_FILE=tok,
        STUB_DIR=tmp,
        WAIT_TIMEOUT="1s",
        WAIT_ROUNDS="1",
        KILL_GRACE_S=str(grace_s),
    )


def _posix_fake_ps(
    supported_cols=("pgid", "nlwp", "stat"),
    require_any=("-A", "-ax"),
    reject_flags=(),
    proc_root="/proc",
    always_fail=False,
):
    """Build a fake `ps` implementing POSIX `-o field=header` (split on first '=' only)."""
    if always_fail:
        return f"#!{sys.executable}\nimport sys\nsys.exit(1)\n"
    return (
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"SUPPORTED = set({sorted(supported_cols)!r})\n"
        f"REQUIRE_ANY = tuple({list(require_any)!r})\n"
        f"REJECT = set({sorted(reject_flags)!r})\n"
        f"PROC_ROOT = {proc_root!r}\n"
        "args = sys.argv[1:]\n"
        "if any(a in REJECT for a in args):\n"
        "    sys.exit(1)\n"
        "if REQUIRE_ANY and not any(a in REQUIRE_ANY for a in args):\n"
        "    sys.exit(0)\n"
        "specs = []\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    a = args[i]\n"
        "    if a == '-o' and i + 1 < len(args):\n"
        "        specs.append(args[i + 1])\n"
        "        i += 2\n"
        "        continue\n"
        "    if a.startswith('-o') and len(a) > 2:\n"
        "        specs.append(a[2:])\n"
        "        i += 1\n"
        "        continue\n"
        "    i += 1\n"
        "if not specs:\n"
        "    sys.exit(1)\n"
        "cols, headers = [], []\n"
        "for spec in specs:\n"
        "    if '=' in spec:\n"
        "        kw, hdr = spec.split('=', 1)\n"
        "        cols.append(kw)\n"
        "        headers.append(hdr)\n"
        "    else:\n"
        "        for kw in spec.replace(',', ' ').split():\n"
        "            cols.append(kw)\n"
        "            headers.append(kw.upper())\n"
        "if any(c not in SUPPORTED for c in cols):\n"
        "    sys.exit(1)\n"
        "if any(headers):\n"
        "    print(' '.join(headers))\n"
        "for ent in os.listdir(PROC_ROOT):\n"
        "    if not ent.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        with open(os.path.join(PROC_ROOT, ent, 'stat'), encoding='utf-8') as f:\n"
        "            parts = f.read().rsplit(') ', 1)[1].split()\n"
        "    except OSError:\n"
        "        continue\n"
        "    row_map = {'pgid': parts[2], 'nlwp': parts[17], 'stat': parts[0]}\n"
        "    print(' '.join(row_map[c] for c in cols))\n"
    )


def _supports_subreaper():
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None)
        return hasattr(libc, "prctl")
    except OSError:
        return False


def _run_in_subreaper(cmd, env, timeout=15.0):
    """Run `cmd` under an isolated PR_SET_CHILD_SUBREAPER wrapper.

    Uses `p.wait()` with an error log file (not PIPE) so a surviving grandchild that
    inherits stderr never stalls EOF detection; the wrapper only waits on `p.pid`
    (never -1), leaving any exited grandchild as a real Z zombie in `-$cmd_pid`.
    """
    wrapper = (
        "import ctypes, json, os, signal, subprocess, sys, tempfile, time\n"
        "libc = ctypes.CDLL(None)\n"
        "if libc.prctl(36, 1, 0, 0, 0) != 0:\n"
        "    sys.exit(98)\n"
        "tmo = float(sys.argv[1])\n"
        "with tempfile.TemporaryFile('w+', encoding='utf-8') as errf:\n"
        "    t0 = time.monotonic()\n"
        "    p = subprocess.Popen(sys.argv[2:], start_new_session=True,\n"
        "                         stdout=subprocess.DEVNULL, stderr=errf)\n"
        "    try:\n"
        "        p.wait(timeout=tmo)\n"
        "        hung = False\n"
        "    except subprocess.TimeoutExpired:\n"
        "        hung = True\n"
        "        try:\n"
        "            os.killpg(p.pid, signal.SIGKILL)\n"
        "        except OSError:\n"
        "            pass\n"
        "        p.wait()\n"
        "    dt = time.monotonic() - t0\n"
        "    errf.seek(0)\n"
        "    err = errf.read()\n"
        "sys.stdout.write(json.dumps({'rc': p.returncode, 'dt': dt, 'hung': hung, 'err': err}))\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", wrapper, str(timeout), *cmd],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout + 5.0,
    )
    if res.returncode != 0:
        raise RuntimeError(f"subreaper wrapper failed (rc={res.returncode}): {res.stderr}")
    return json.loads(res.stdout)


class TestPR47R5(unittest.TestCase):
    def _cleanup_fake_proc(self, fake_proc, pidf):
        for root, dirs, files in os.walk(fake_proc):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o755)
                except OSError:
                    pass
        if os.path.exists(pidf):
            try:
                with open(pidf, encoding="utf-8") as fh:
                    os.kill(int(fh.read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def test_unreadable_proc_entry_after_leader_reaped_never_defers_to_ps(self):
        """P2: leader exited/reaped, SIG_IGN grandchild has 0o000 fake_proc stat, ps reads same fake_proc."""
        for lease_lost in (False, True):
            with self.subTest(lease_lost=lease_lost):
                with tempfile.TemporaryDirectory(prefix="mh-pr47r5-p2-") as tmp:
                    env = _env(tmp, poll=lease_lost, grace_s="1")
                    fake_proc = os.path.join(tmp, "proc")
                    bindir = os.path.join(tmp, "bin")
                    os.makedirs(fake_proc)
                    os.makedirs(bindir)
                    # Fake ps reads the EXACT same fake_proc and silently skips EACCES entries,
                    # mirroring Linux procps-ng / BusyBox ps. Also log if ps is ever called.
                    ps_log = os.path.join(tmp, "ps.calls")
                    ps_script = _posix_fake_ps(proc_root=fake_proc).replace(
                        "args = sys.argv[1:]\n",
                        f"open({ps_log!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nargs = sys.argv[1:]\n",
                    )
                    _write(os.path.join(bindir, "ps"), ps_script, 0o755)
                    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"

                    init = f'mkdir -p "{fake_proc}/$$"\ncat "/proc/$$/stat" > "{fake_proc}/$$/stat"\n'
                    with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                        patched = fh.read().replace("/proc", fake_proc).replace("set -u\n", "set -u\n" + init, 1)
                    w_sh = os.path.join(tmp, "worker.sh")
                    _write(w_sh, patched, 0o755)

                    lockf = os.path.join(tmp, "g.lock")
                    pidf = os.path.join(tmp, "g.pid")
                    readyf = os.path.join(tmp, "g.ready")
                    self.addCleanup(self._cleanup_fake_proc, fake_proc, pidf)

                    cmd_py = f"""import fcntl, os, signal, sys, time
pgid = os.getpid()
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    gp = os.getpid()
    gdir = os.path.join({fake_proc!r}, str(gp))
    os.makedirs(gdir, exist_ok=True)
    st = os.path.join(gdir, "stat")
    open(st, "w").write(f"{{gp}} (grand) S 1 {{pgid}} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100\\n")
    os.chmod(st, 0)
    open({pidf!r}, "w").write(str(gp))
    open({readyf!r}, "w").write(str(gp))
    time.sleep(20)
    os._exit(0)
while not os.path.exists({readyf!r}):
    time.sleep(0.02)
if {lease_lost!r}:
    open({os.path.join(tmp, 'lose')!r}, "w").close()
    time.sleep(0.25)
sys.exit(0)
"""
                    err_path = os.path.join(tmp, "worker.err")
                    with open(err_path, "w+", encoding="utf-8") as errf:
                        proc = subprocess.Popen(
                            ["bash", w_sh, "--", sys.executable, "-c", cmd_py],
                            env=env,
                            start_new_session=True,
                            stdout=subprocess.DEVNULL,
                            stderr=errf,
                        )
                        try:
                            proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except OSError:
                                pass
                            proc.wait()
                            errf.seek(0)
                            self.fail(f"worker hung with unreadable /proc entry: {errf.read()}")
                        errf.seek(0)
                        err = errf.read()

                    expected_rc = 9 if lease_lost else 0
                    self.assertEqual(proc.returncode, expected_rc, err)
                    self.assertFalse(
                        _flock_held(lockf),
                        "SIGTERM-ignoring grandchild survived after leader was reaped with 0o000 /proc stat",
                    )
                    self.assertFalse(
                        os.path.exists(ps_log),
                        "readable /proc scan with unreadable entry must never invoke ps",
                    )
                    self.assertNotIn("Permission denied", err)

    def test_unreadable_proc_pid_dir_and_persistent_unreadable_termination(self):
        """P2: 0o000 /proc/<pid> dir is caught AND persistent zombie + unreadable /proc terminates boundedly."""
        if not _supports_subreaper():
            self.skipTest("PR_SET_CHILD_SUBREAPER required")

        with tempfile.TemporaryDirectory(prefix="mh-pr47r5-persist-") as tmp:
            env = _env(tmp, grace_s="1")
            fake_proc = os.path.join(tmp, "proc")
            bindir = os.path.join(tmp, "bin")
            os.makedirs(fake_proc)
            os.makedirs(bindir)
            _write(os.path.join(bindir, "ps"), _posix_fake_ps(proc_root=fake_proc), 0o755)
            env["PATH"] = f"{bindir}:{env.get('PATH', '')}"

            init = f'mkdir -p "{fake_proc}/$$"\ncat "/proc/$$/stat" > "{fake_proc}/$$/stat"\n'
            with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                patched = fh.read().replace("/proc", fake_proc).replace("set -u\n", "set -u\n" + init, 1)
            w_sh = os.path.join(tmp, "worker.sh")
            _write(w_sh, patched, 0o755)

            lockf = os.path.join(tmp, "g.lock")
            pidf = os.path.join(tmp, "g.pid")
            readyf = os.path.join(tmp, "g.ready")
            self.addCleanup(self._cleanup_fake_proc, fake_proc, pidf)

            # Grandchild's fake_proc/<gp> directory itself is chmod 0o000 (so /proc/[0-9]*/stat glob
            # would skip it, and both the zombie and the unreadable fake_proc dir persist even after SIGKILL!).
            cmd_py = f"""import fcntl, os, signal, sys, time
pgid = os.getpid()
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    fd = os.open({lockf!r}, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    gp = os.getpid()
    gdir = os.path.join({fake_proc!r}, str(gp))
    os.makedirs(gdir, exist_ok=True)
    open(os.path.join(gdir, "stat"), "w").write(f"{{gp}} (grand) S 1 {{pgid}} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100\\n")
    os.chmod(gdir, 0)
    open({pidf!r}, "w").write(str(gp))
    open({readyf!r}, "w").write(str(gp))
    time.sleep(20)
    os._exit(0)
while not os.path.exists({readyf!r}):
    time.sleep(0.02)
sys.exit(0)
"""
            res = _run_in_subreaper(["bash", w_sh, "--", sys.executable, "-c", cmd_py], env=env, timeout=8.0)
            self.assertFalse(res["hung"], f"worker looped forever on persistent zombie + unreadable /proc: {res['err']}")
            self.assertEqual(res["rc"], 0, res["err"])
            self.assertFalse(_flock_held(lockf), "grandchild survived with 0o000 /proc/<pid> directory")
            self.assertLess(res["dt"], 4.0, f"bounded escalation took too long: {res['dt']:.2f}s")

    def test_pgid_has_live_ps_separate_o_columns_and_empty_stat_unknown(self):
        """P3: verify separate -o arguments in script and direct `_pgid_has_live_ps` semantics."""
        with open(WORKER_SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"-o\s+\S+=,\S+=", "combined multi-column -o a=,b= still present")
        self.assertIn("ps -A -o pgid= -o nlwp= -o stat=", code)
        self.assertIn("ps -A -o pgid= -o stat=", code)
        self.assertIn("ps -ax -o pgid= -o stat=", code)

        m = re.search(r"(?ms)^_pgid_has_live_ps\(\).*?(?=^_pgid_has_live\(\))", src)
        self.assertIsNotNone(m, "could not locate _pgid_has_live_ps")
        fn_src = m.group(0)

        with tempfile.TemporaryDirectory(prefix="mh-pr47r5-psfn-") as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            fake_proc = os.path.join(tmp, "proc")
            os.makedirs(fake_proc)

            def _mk_stat(pid, pgid, state, nlwp=1):
                _write(
                    os.path.join(fake_proc, str(pid), "stat"),
                    f"{pid} (p) {state} 1 {pgid} 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 {nlwp} 0 100\n",
                )

            _mk_stat(101, 500, "Z", nlwp=1)
            _mk_stat(102, 501, "Z", nlwp=2)
            _mk_stat(103, 502, "S", nlwp=1)

            ps_path = os.path.join(bindir, "ps")

            def _run_ps_fn(ps_body):
                _write(ps_path, ps_body, 0o755)
                script = f"""
set -u
{fn_src}
_pgid_has_live_ps 500; rc_z=$?
_pgid_has_live_ps 501; rc_mtz=$?
_pgid_has_live_ps 502; rc_live=$?
echo "z=$rc_z mtz=$rc_mtz live=$rc_live"
"""
                env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
                return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=5)

            # 1. Linux-style POSIX ps (supports pgid, nlwp, stat): Z(nlwp=1)->1, Z(nlwp=2)->0, S->0
            r_linux = _run_ps_fn(_posix_fake_ps(("pgid", "nlwp", "stat"), proc_root=fake_proc))
            self.assertEqual(r_linux.returncode, 0, r_linux.stderr)
            self.assertEqual(r_linux.stdout.strip(), "z=1 mtz=0 live=0")

            # 2. BSD-style POSIX ps (rejects nlwp, supports -A -o pgid= -o stat=): Z->1, S->0
            r_bsd = _run_ps_fn(_posix_fake_ps(("pgid", "stat"), proc_root=fake_proc))
            self.assertEqual(r_bsd.returncode, 0, r_bsd.stderr)
            self.assertEqual(r_bsd.stdout.strip(), "z=1 mtz=1 live=0")

            # 3. OpenBSD-style POSIX ps (rejects nlwp and -A, requires -ax): Z->1, S->0
            r_obsd = _run_ps_fn(_posix_fake_ps(("pgid", "stat"), require_any=("-ax",), reject_flags=("-A",), proc_root=fake_proc))
            self.assertEqual(r_obsd.returncode, 0, r_obsd.stderr)
            self.assertEqual(r_obsd.stdout.strip(), "z=1 mtz=1 live=0")

            # 4. Truncated ps output (empty st column for matching pgid) must return 2 (unknown) in both branches
            trunc_3 = f"#!{sys.executable}\nimport sys\nprint('500 1\\n501 2\\n502 1')\n"
            r_t3 = _run_ps_fn(trunc_3)
            self.assertEqual(r_t3.stdout.strip(), "z=2 mtz=2 live=2")

            trunc_2 = f"#!{sys.executable}\nimport sys\nif any('nlwp' in a for a in sys.argv[1:]): sys.exit(1)\nprint('500\\n501\\n502')\n"
            r_t2 = _run_ps_fn(trunc_2)
            self.assertEqual(r_t2.stdout.strip(), "z=2 mtz=2 live=2")

            # 5. Header-only (zero numeric pgid rows) or always-failing ps must return 2
            r_hdr = _run_ps_fn(f"#!{sys.executable}\nprint('PGID NLWP STAT')\n")
            self.assertEqual(r_hdr.stdout.strip(), "z=2 mtz=2 live=2")

            r_fail = _run_ps_fn(_posix_fake_ps(always_fail=True))
            self.assertEqual(r_fail.stdout.strip(), "z=2 mtz=2 live=2")

    def _run_zombie_only_no_proc_worker(self, ps_body, grace_s=5):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r5-zps-") as tmp:
            env = _env(tmp, grace_s=str(grace_s))
            no_proc = os.path.join(tmp, "no-proc")
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            _write(os.path.join(bindir, "ps"), ps_body, 0o755)
            env["PATH"] = f"{bindir}:{env.get('PATH', '')}"

            with open(WORKER_SCRIPT, encoding="utf-8") as fh:
                src = fh.read()
            w_sh = os.path.join(tmp, "worker.sh")
            _write(w_sh, src.replace("/proc", no_proc), 0o755)

            # Leader forks a child that immediately exits into Z, waits until kernel reports
            # state Z in /proc, then leader exits 0. Subreaper retains the Z grandchild so
            # `kill -0 -- -$cmd_pid` stays 0 while _pgid_has_live_ps must inspect state Z.
            cmd_py = """import os, sys, time
pid = os.fork()
if pid == 0:
    os._exit(0)
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    try:
        st = open(f"/proc/{pid}/stat", encoding="utf-8").read().rsplit(") ", 1)[1].split()[0]
        if st == "Z":
            break
    except OSError:
        pass
    time.sleep(0.01)
sys.exit(0)
"""
            return _run_in_subreaper(["bash", w_sh, "--", sys.executable, "-c", cmd_py], env=env, timeout=grace_s + 15.0)

    def test_no_proc_zombie_only_pgid_detected_fast_and_not_vacuous(self):
        """P3: with KILL_GRACE_S=5, zombie-only PGID finishes in < 1.5s; always-failing ps takes >= 4.5s."""
        if not _supports_subreaper():
            self.skipTest("PR_SET_CHILD_SUBREAPER required")

        cases = [
            ("linux_nlwp", _posix_fake_ps(("pgid", "nlwp", "stat"), require_any=("-A",), proc_root="/proc")),
            ("bsd_A", _posix_fake_ps(("pgid", "stat"), require_any=("-A",), proc_root="/proc")),
            ("openbsd_ax", _posix_fake_ps(("pgid", "stat"), require_any=("-ax",), reject_flags=("-A",), proc_root="/proc")),
        ]
        for label, ps_body in cases:
            with self.subTest(mode=label):
                res = self._run_zombie_only_no_proc_worker(ps_body, grace_s=5)
                self.assertFalse(res["hung"], res["err"])
                self.assertEqual(res["rc"], 0, res["err"])
                self.assertLess(
                    res["dt"],
                    1.5,
                    f"{label}: zombie-only PGID was not detected empty quickly (dt={res['dt']:.2f}s): {res['err']}",
                )

        # Non-vacuity check: if ps always fails, _pgid_has_live must assume live and wait the full KILL_GRACE_S=5s.
        res_fail = self._run_zombie_only_no_proc_worker(_posix_fake_ps(always_fail=True), grace_s=5)
        self.assertFalse(res_fail["hung"], res_fail["err"])
        self.assertEqual(res_fail["rc"], 0, res_fail["err"])
        self.assertGreaterEqual(
            res_fail["dt"],
            4.5,
            f"test would be vacuous: always-failing ps finished in {res_fail['dt']:.2f}s instead of >= 4.5s",
        )


if __name__ == "__main__":
    unittest.main()
