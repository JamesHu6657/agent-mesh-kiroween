"""Regression tests for batch7 PR #47 round-2 review findings (3 findings)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.unit.test_inbox_worker_kit import SCRIPT as WORKER_SCRIPT, STUB as WORKER_STUB

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t.com",
}


def _git(cwd, *args, check=True, env_extra=None):
    env = dict(os.environ, **GIT_ENV)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _write(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mode is not None:
        os.chmod(path, mode)


def _make_coord_repo(tmp):
    repo = os.path.join(tmp, "repo")
    shutil.copytree(
        os.path.join(ROOT, "scripts", "coord"),
        os.path.join(repo, "scripts", "coord"),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        os.path.join(ROOT, ".kiro", "hooks", "scripts"),
        os.path.join(repo, ".kiro", "hooks", "scripts"),
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    fake_gl = os.path.join(tmp, "fake-gitleaks")
    _write(fake_gl, "#!/bin/sh\nexit 0\n", 0o755)
    _git(repo, "init", "-q", "-b", "main")
    _write(os.path.join(repo, "README.md"), "init\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo, fake_gl


def _grep_secret_pattern(lines):
    pat_file = os.path.join(ROOT, "scripts", "coord", "secret-pattern.sh")
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; grep -E "$P" || true', "_", pat_file],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


class TestFinding1RunTierTopLevelAndImportError(unittest.TestCase):
    """P2: run_tier.py must check __init__.py chain before lifting top_level and report discovery ImportError."""

    def _clean_env(self, tmp):
        env = dict(
            os.environ,
            MESH_TEST_DURATIONS=os.path.join(tmp, "dur.json"),
            MESH_TEST_PROC_LOCK=os.path.join(tmp, "p.lock"),
        )
        env.pop("PYTHONPATH", None)
        return env

    def test_non_package_dir_under_cwd_or_repo_root_runs_without_import_crash(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-rt-") as tmp:
            env = self._clean_env(tmp)
            # Review Repro: external non-package dir directly under cwd.
            work = os.path.join(tmp, "work")
            ext_tier = os.path.join(work, "ext_tier")
            _write(os.path.join(ext_tier, "helper_local.py"), "VAL = 99\n")
            _write(
                os.path.join(ext_tier, "test_ok.py"),
                "import unittest, helper_local, mesh.constants\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self): self.assertEqual(helper_local.VAL, 99)\n",
            )
            res = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), "ext_tier"],
                cwd=work, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("OK\n", res.stdout)

            # Non-package directory and partial package chain under REPO_ROOT.
            fake_repo = os.path.join(tmp, "fakerepo")
            os.makedirs(os.path.join(fake_repo, "tests"))
            shutil.copy2(os.path.join(ROOT, "tests", "run_tier.py"), os.path.join(fake_repo, "tests", "run_tier.py"))
            _write(os.path.join(fake_repo, "scratch_tier", "helper_s.py"), "V = 7\n")
            _write(
                os.path.join(fake_repo, "scratch_tier", "test_scratch.py"),
                "import unittest, helper_s\n"
                "class S(unittest.TestCase):\n"
                "    def test_s(self): self.assertEqual(helper_s.V, 7)\n",
            )
            # Intermediate parent dir lacks __init__.py and is not a valid package name.
            _write(os.path.join(fake_repo, "partial-dir", "sub_tier", "__init__.py"), "")
            _write(os.path.join(fake_repo, "partial-dir", "sub_tier", "helper_sub.py"), "W = 11\n")
            _write(
                os.path.join(fake_repo, "partial-dir", "sub_tier", "test_sub.py"),
                "import unittest, helper_sub\n"
                "class P(unittest.TestCase):\n"
                "    def test_p(self): self.assertEqual(helper_sub.W, 11)\n",
            )

            for caller_cwd in (fake_repo, tmp):
                for target in (
                    os.path.join(fake_repo, "scratch_tier"),
                    os.path.join(fake_repo, "partial-dir", "sub_tier"),
                ):
                    res_repo = subprocess.run(
                        [sys.executable, os.path.join(fake_repo, "tests", "run_tier.py"), target],
                        cwd=caller_cwd, env=env, capture_output=True, text=True, timeout=20,
                    )
                    self.assertEqual(res_repo.returncode, 0, f"cwd={caller_cwd} target={target}: {res_repo.stdout}\n{res_repo.stderr}")
                    self.assertIn("OK\n", res_repo.stdout)

    def test_valid_package_chain_under_cwd_lifts_top_level(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-pkg-") as tmp:
            env = self._clean_env(tmp)
            work = os.path.join(tmp, "work")
            _write(os.path.join(work, "ext_pkg", "__init__.py"), "")
            _write(os.path.join(work, "ext_pkg", "shared_mod.py"), "X = 'lifted'\n")
            _write(os.path.join(work, "ext_pkg", "sub", "__init__.py"), "")
            _write(
                os.path.join(work, "ext_pkg", "sub", "test_pkg.py"),
                "import unittest\n"
                "from ext_pkg import shared_mod\n"
                "class PkgTest(unittest.TestCase):\n"
                "    def test_pkg(self): self.assertEqual(shared_mod.X, 'lifted')\n",
            )
            res = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), os.path.join("ext_pkg", "sub")],
                cwd=work, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("ok ext_pkg.sub.test_pkg.PkgTest.test_pkg", res.stdout)
            self.assertIn("OK\n", res.stdout)

    def test_discovery_importerror_is_reported_not_uncaught_crash(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-derr-") as tmp:
            env = self._clean_env(tmp)
            missing = os.path.join(tmp, "does_not_exist")
            res_missing = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), missing],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res_missing.returncode, 1, res_missing.stdout + res_missing.stderr)
            self.assertIn("ERROR discovery_error", res_missing.stdout)
            self.assertIn("FAILED (errors=1)", res_missing.stdout)
            self.assertIn("FAILURES AND ERRORS:", res_missing.stderr)
            self.assertIn("Start directory is not importable", res_missing.stderr)

            # Even if TestLoader.discover populates loader.errors and raises ImportError mid-scan,
            # run_tier reports both errors cleanly instead of crashing.
            tdir = os.path.join(tmp, "t")
            os.makedirs(tdir)
            code = (
                "import sys, unittest\n"
                "from tests import run_tier\n"
                "def broken_discover(self, s, pattern='test*.py', top_level_dir=None):\n"
                "    self.errors.append('prior_loader_error_pr47r2')\n"
                "    raise ImportError('shadowed module boom')\n"
                "unittest.TestLoader.discover = broken_discover\n"
                "sys.argv = ['run_tier.py', sys.argv[1]]\n"
                "raise SystemExit(run_tier.main())\n"
            )
            res_mid = subprocess.run(
                [sys.executable, "-c", code, tdir],
                cwd=ROOT,
                env=dict(env, PYTHONPATH=ROOT),
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res_mid.returncode, 1, res_mid.stdout + res_mid.stderr)
            self.assertIn("FAILED (errors=2)", res_mid.stdout)
            self.assertIn("shadowed module boom", res_mid.stderr)
            self.assertIn("prior_loader_error_pr47r2", res_mid.stderr)


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/secret-pattern.sh + grep -E test")
class TestFinding2SecretPatternFalsePositivesAndGaps(unittest.TestCase):
    """P2: secret-pattern.sh must avoid empty-literal/Python false positives and catch f-strings + double-escaped JSON."""

    def test_excludes_empty_literals_and_ordinary_python_statements(self):
        ak = "OPENAI_" + "API_KEY"
        negatives = [
            f'client = connect({ak}="",timeout=30)',
            'env = {"' + ak + '":"",**extra}',
            f'{ak}="";export PATH=/usr/bin:$PATH',
            f'{ak}=""#unset_in_test',
            f"{ak}='',timeout=30",
            f'{ak}="""""",timeout=30',
            f'{ak}=f"",timeout=30',
            f'{ak}=fr"",timeout=30',
            '"{\\"' + ak + '\\":\\"\\",timeout:30}"',
            '"{\\\\\\"' + ak + '\\\\\\":\\\\\\"\\\\\\",timeout:30}"',
            f'{ak} = os.environ.get("{ak}")',
            f'{ak}: Optional[str] = None',
            f'{ak}: str = ""',
            'daemon_extra_env = {"FAKE_SECRET_' + 'API_KEY": FAKE_KEY}',
            '"FACTORY_' + 'API_KEY": SECRET',
            f'{ak}=get(x)',
            f'{ak} = f"$MY_KEY_VAR"',
            f'{ak} = f"$(cat ~/k)"',
            f'{ak} = f"`cat ~/k`"',
            f'{ak} = f"short"',
            f'{ak} = rf"short"',
            f'{ak}=$MY_KEY_VAR',
            f'API_KEY_NAME="my-custom-secret-value-123"',
        ]
        self.assertEqual(_grep_secret_pattern(negatives), [])

    def test_catches_python3_fstrings_and_double_escaped_json(self):
        ak = "OPENAI_" + "API_KEY"
        val = "my-custom-secret-value-123"
        positives = [
            f'{ak} = f"{val}"',
            f'{ak} = F"{val}"',
            f'{ak} = fr"{val}"',
            f'{ak} = rf"{val}"',
            f'{ak} = Fr"{val}"',
            f'{ak} = fR"{val}"',
            f'{ak} = Rf"{val}"',
            f'{ak} = rF"{val}"',
            f'{ak} = FR"{val}"',
            f'{ak} = RF"{val}"',
            f"{ak} = f'{val}'",
            f'{ak} = f"""{val}"""',
            f"{ak} = rf'''{val}'''",
            f'{ak} = f\\"{val}\\"',
            '"{\\\\\\"' + ak + '\\\\\\": \\\\\\"' + val + '\\\\\\"}"',
            '"{\\\\"' + ak + '\\\\": \\\\"' + val + '\\\\"}"',
        ]
        for sample in positives:
            self.assertEqual(_grep_secret_pattern([sample]), [sample], f"missed: {sample!r}")

    def test_scanners_pass_empty_and_python_code_and_block_fstring_and_double_escaped_json(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-sec-") as tmp:
            repo, fake_gl = _make_coord_repo(tmp)
            bare = os.path.join(tmp, "github.git")
            _git(tmp, "init", "--bare", bare)
            _git(repo, "remote", "add", "github", bare)
            _git(repo, "push", "github", "main:refs/heads/main")
            env = dict(os.environ, GITLEAKS=fake_gl)

            # Clean Python file with empty-string kwargs and type annotations must pass all 3 scanners.
            clean_py = os.path.join(repo, "clean_mod.py")
            ak = "OPENAI_" + "API_KEY"
            _write(
                clean_py,
                "import os\nfrom typing import Optional\n"
                f"{ak}: Optional[str] = None\n"
                f'val = os.environ.get("{ak}")\n'
                f'client = dict({ak}="",timeout=30)\n',
            )
            _git(repo, "add", clean_py)
            res_clean = subprocess.run(
                ["bash", ".kiro/hooks/scripts/secret-scan.sh"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_clean.returncode, 0, res_clean.stdout + res_clean.stderr)
            _git(repo, "commit", "-qm", "clean python", env_extra=GIT_ENV)

            # f-string and double-escaped JSON secrets must be blocked by all 3 scanners.
            val = "sk_" + "custom_" + "secret_12345"
            leak_py = os.path.join(repo, "leak_fstr.py")
            _write(leak_py, f'{ak} = fr"{val}"\n')
            leak_json = os.path.join(repo, "leak_nested.json")
            _write(leak_json, '{"payload": "{\\\\\\"' + ak + '\\\\\\": \\\\\\"' + val + '\\\\\\"}"}\n')
            _git(repo, "add", leak_py, leak_json)

            res_scan = subprocess.run(
                ["bash", ".kiro/hooks/scripts/secret-scan.sh"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_scan.returncode, 1, res_scan.stdout + res_scan.stderr)
            self.assertIn("pattern hit in staged leak_fstr.py", res_scan.stdout)
            self.assertIn("pattern hit in staged leak_nested.json", res_scan.stdout)

            _git(repo, "commit", "-qm", "leaks", env_extra=GIT_ENV)
            _git(repo, "branch", "wt/r2")
            res_pr = subprocess.run(
                ["bash", "scripts/coord/push-review.sh", "r2"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_pr.returncode, 3, res_pr.stdout + res_pr.stderr)
            self.assertIn("pattern hits", res_pr.stdout)

            res_sync = subprocess.run(
                ["bash", "scripts/coord/sync-github.sh"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_sync.returncode, 3, res_sync.stdout + res_sync.stderr)
            self.assertIn("pattern hits", res_sync.stdout)

        for rel in (
            "scripts/coord/secret-pattern.sh",
            ".kiro/hooks/scripts/secret-scan.sh",
            "scripts/coord/push-review.sh",
            "scripts/coord/sync-github.sh",
            "tests/unit/test_batch7_pr47_r2.py",
        ):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                self.assertEqual(_grep_secret_pattern(fh.read().splitlines()), [], rel)


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/inbox-worker.sh + /proc PID-reuse test")
class TestFinding3InboxWorkerNoPostReapSignaling(unittest.TestCase):
    """P3: inbox-worker-example.sh must not signal reaped PID or empty/gone PGID."""

    def _setup_worker(self, tmp, hb_every="40", lose=False):
        os.makedirs(os.path.join(tmp, "ws"), mode=0o700)
        stub = os.path.join(tmp, "mesh")
        _write(stub, WORKER_STUB, 0o755)
        tok = os.path.join(tmp, "worker.token")
        _write(tok, "tok\n", 0o600)
        _write(os.path.join(tmp, "hb-every"), hb_every)
        if lose:
            _write(os.path.join(tmp, "lose"), "")
        env = dict(
            os.environ,
            MESH_BIN=stub,
            MESH_TOKEN_FILE=tok,
            STUB_DIR=tmp,
            WAIT_TIMEOUT="1s",
            WAIT_ROUNDS="1",
            KILL_GRACE_S="2",
        )
        return env

    def _run_traced_worker(self, env, klog, cmd_args, extra_wrapper="", timeout=15):
        wrapper = f"""
kill() {{
  local rc
  builtin kill "$@"
  rc=$?
  echo "PID=$$ REAPED=${{reaped:-0}} GONE=${{pgid_gone:-0}} CMD=${{cmd_pid:-}} RC=$rc ARGS:$*" >> {klog!r}
{extra_wrapper}
  return $rc
}}
export -f kill
exec bash {WORKER_SCRIPT!r} -- "$@"
"""
        return subprocess.run(
            ["bash", "-c", wrapper, "_", *cmd_args],
            env=env, capture_output=True, text=True, timeout=timeout,
        )

    def _parse_cmd_kills(self, klog):
        """Return list of (sig, targets, rc, reaped, gone) for kill calls targeting cmd or its PGID."""
        with open(klog, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        events = []
        for line in lines:
            parts = line.split(" ARGS:", 1)
            if len(parts) != 2:
                continue
            meta = parts[0]
            rc = int(meta.split("RC=")[1])
            reaped = int(meta.split("REAPED=")[1].split()[0])
            gone = int(meta.split("GONE=")[1].split()[0])
            args = parts[1].split()
            if "--" not in args:
                continue
            idx = args.index("--")
            sig = args[0].lstrip("-") if args[0].startswith("-") else "TERM"
            targets = args[idx + 1 :]
            events.append((sig, targets, rc, reaped, gone))
        return events

    def test_lost_lease_when_cmd_exits_on_sigterm_never_sends_sigkill_after_pgid_gone(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-wk1-") as tmp:
            env = self._setup_worker(tmp, hb_every="0", lose=True)
            klog = os.path.join(tmp, "kill.log")
            # Fire a late USR1 the moment kill -0 -- -pgid observes the PGID is empty,
            # exercising the post-reap USR1 trap window before pgid_gone/cmd_pid are cleared.
            late_usr1_flag = os.path.join(tmp, "late-usr1.sent")
            hook = f"""
  if [ "${{1:-}}" = "-0" ] && [ "$rc" -ne 0 ] && [ ! -e {late_usr1_flag!r} ]; then
    : > {late_usr1_flag!r}
    builtin kill -USR1 "$$" 2>/dev/null || true
  fi
"""
            res = self._run_traced_worker(env, klog, ["sleep", "10"], extra_wrapper=hook)
            self.assertEqual(res.returncode, 9, res.stderr)
            self.assertTrue(os.path.exists(late_usr1_flag), "kill -0 probe on empty PGID did not occur")

            events = self._parse_cmd_kills(klog)
            # Find the first probe where kill -0 -- -pgid returned non-zero (PGID gone).
            gone_idx = next(
                (i for i, (sig, _, rc, _, _) in enumerate(events) if sig == "0" and rc != 0),
                None,
            )
            self.assertIsNotNone(gone_idx, events)
            # After PGID was observed empty, no signal (TERM/KILL/...) may ever be sent.
            signals_after_gone = [e for e in events[gone_idx + 1 :] if e[0] != "0"]
            self.assertEqual(
                signals_after_gone,
                [],
                f"signaled after PGID was already confirmed gone: {events[gone_idx + 1 :]}",
            )
            self.assertFalse(
                any(sig == "KILL" for sig, _, _, _, _ in events),
                f"unexpected SIGKILL when command exited on SIGTERM: {events}",
            )
            self.assertFalse(
                any(gone == 1 for _, _, _, _, gone in events),
                f"kill invoked when pgid_gone==1: {events}",
            )

    def test_reaped_parent_with_leftover_child_never_signals_positive_pid_or_dead_pgid(self):
        import time
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-wk2-") as tmp:
            env = self._setup_worker(tmp, hb_every="40", lose=False)
            env["KILL_GRACE_S"] = "5"
            klog = os.path.join(tmp, "kill.log")
            usr1_sent = os.path.join(tmp, "usr1.sent")
            ready = os.path.join(tmp, "child.ready")
            child_py = (
                "import signal, sys, time; "
                "seen = False\n"
                "def on_term(*_):\n"
                "    global seen\n"
                "    if not seen:\n"
                "        seen = True\n"
                "        time.sleep(0.35)\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, on_term); "
                "open(sys.argv[1], 'w').write('1'); "
                "time.sleep(10)"
            )
            launcher_py = (
                "import os, subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_py!r}, {ready!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"deadline = time.monotonic() + 5\n"
                f"while time.monotonic() < deadline and not os.path.exists({ready!r}): time.sleep(0.02)\n"
            )
            # While reaped=1 and child is still draining, inject USR1 once to verify USR1 trap
            # does not signal positive cmd_pid after reap.
            hook = f"""
  if [ "${{1:-}}" = "-TERM" ] && [ ! -e {usr1_sent!r} ]; then
    : > {usr1_sent!r}
    builtin kill -USR1 "$$" 2>/dev/null || true
  fi
"""
            t0 = time.monotonic()
            res = self._run_traced_worker(env, klog, [sys.executable, "-c", launcher_py], extra_wrapper=hook)
            elapsed = time.monotonic() - t0
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertLess(elapsed, 2.5, f"grace loop stalled instead of detecting zombie/empty PGID: {elapsed:.2f}s")
            self.assertTrue(os.path.exists(usr1_sent))

            events = self._parse_cmd_kills(klog)
            # Parent exited before cleanup, so positive cmd_pid must NEVER be signaled at all.
            pos_targets = [
                (sig, targets)
                for sig, targets, _, _, _ in events
                if sig != "0" and any(not t.startswith("-") for t in targets)
            ]
            self.assertEqual(pos_targets, [], f"signaled already-reaped positive cmd_pid: {pos_targets}")

            # PGID leftover exited during grace (under container PID 1 it may briefly remain a
            # Z zombie so kill -0 returns 0 while _pgid_has_live detects Z via /proc); either way
            # grace loop must break cleanly with zero SIGKILL and only PGID-targeted SIGTERM.
            self.assertFalse(
                any(sig == "KILL" for sig, _, _, _, _ in events),
                f"unexpected SIGKILL when leftover exited during grace: {events}",
            )
            self.assertTrue(
                any(sig == "TERM" for sig, _, _, _, _ in events),
                f"expected SIGTERM on live leftover PGID: {events}",
            )
            self.assertFalse(
                any(sig != "0" and reaped == 0 for sig, _, _, reaped, _ in events),
                events,
            )

    def test_trap_reinstall_after_reap_has_empty_cmd_pid_and_cleanup_signals_nothing(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-wk3-") as tmp:
            env = self._setup_worker(tmp, hb_every="40", lose=False)
            klog = os.path.join(tmp, "kill.log")
            trap_saw_cmd_pid = os.path.join(tmp, "trap-saw-cmd-pid")
            trap_reinstalled = os.path.join(tmp, "trap-reinstalled")
            wrapper = f"""
kill() {{
  local rc
  builtin kill "$@"
  rc=$?
  echo "PID=$$ REAPED=${{reaped:-0}} GONE=${{pgid_gone:-0}} CMD=${{cmd_pid:-}} RC=$rc ARGS:$*" >> {klog!r}
  return $rc
}}
trap() {{
  local rc
  builtin trap "$@"
  rc=$?
  if [ "${{1:-}}" = "exit 130" ] && [ "${{2:-}}" = "INT" ] && [ -e {os.path.join(tmp, 'calls.log')!r} ]; then
    : > {trap_reinstalled!r}
    if [ -n "${{cmd_pid:-}}" ]; then
      echo "$cmd_pid" > {trap_saw_cmd_pid!r}
    fi
    if [ "${{INJECT_TERM_AT_REINSTALL:-0}}" = "1" ]; then
      builtin kill -TERM "$$" 2>/dev/null || true
    fi
  fi
  return $rc
}}
export -f kill trap
exec bash {WORKER_SCRIPT!r} -- true
"""
            res = subprocess.run(
                ["bash", "-c", wrapper],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(os.path.exists(trap_reinstalled))
            self.assertFalse(
                os.path.exists(trap_saw_cmd_pid),
                "trap 'exit 130' INT TERM was restored before clearing cmd_pid!",
            )
            events = self._parse_cmd_kills(klog) if os.path.exists(klog) else []
            sig_events = [(sig, targets) for sig, targets, _, _, _ in events if sig != "0"]
            self.assertEqual(sig_events, [], f"unexpected signals sent for simple `true`: {sig_events}")

            # Even if SIGTERM fires right when trap 'exit 130' INT TERM is reinstalled (triggering
            # EXIT cleanup immediately), cleanup must see empty cmd_pid/pgid_gone=1 and signal nothing.
            os.remove(trap_reinstalled)
            if os.path.exists(klog):
                os.remove(klog)
            env_term = dict(env, INJECT_TERM_AT_REINSTALL="1")
            res_term = subprocess.run(
                ["bash", "-c", wrapper],
                env=env_term, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res_term.returncode, 130, res_term.stderr)
            self.assertTrue(os.path.exists(trap_reinstalled))
            self.assertFalse(
                os.path.exists(trap_saw_cmd_pid),
                "cmd_pid was still set when trap 'exit 130' INT TERM was restored!",
            )
            events_term = self._parse_cmd_kills(klog) if os.path.exists(klog) else []
            sig_events_term = [(sig, targets) for sig, targets, _, _, _ in events_term if sig != "0"]
            self.assertEqual(
                sig_events_term,
                [],
                f"cleanup signaled stale cmd_pid/pgid when SIGTERM hit at trap reinstall: {sig_events_term}",
            )

    def test_normal_exit_with_simultaneous_lease_loss_signals_nothing(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47r2-wk4-") as tmp:
            env = self._setup_worker(tmp, hb_every="40", lose=False)
            klog = os.path.join(tmp, "kill.log")
            hook_ran = os.path.join(tmp, "wait-lost.ran")
            # Simulate lease loss + USR1 arriving the instant wait "$cmd_pid" reaps a
            # fast-exiting command whose PGID is already empty.
            wrapper = f"""
kill() {{
  local rc
  builtin kill "$@"
  rc=$?
  echo "PID=$$ REAPED=${{reaped:-0}} GONE=${{pgid_gone:-0}} CMD=${{cmd_pid:-}} RC=$rc ARGS:$*" >> {klog!r}
  return $rc
}}
wait() {{
  local rc
  builtin wait "$@"
  rc=$?
  if [ -n "${{cmd_pid:-}}" ] && [ "${{1:-}}" = "${{cmd_pid:-}}" ] && [ ! -e {hook_ran!r} ]; then
    : > {hook_ran!r}
    : > "$lost"
    builtin kill -USR1 "$worker_pid" 2>/dev/null || true
  fi
  return $rc
}}
export -f kill wait
exec bash {WORKER_SCRIPT!r} -- true
"""
            res = subprocess.run(
                ["bash", "-c", wrapper],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res.returncode, 9, res.stderr)
            self.assertTrue(os.path.exists(hook_ran), "wait hook did not trigger")
            events = self._parse_cmd_kills(klog) if os.path.exists(klog) else []
            sig_events = [(sig, targets) for sig, targets, _, _, _ in events if sig != "0"]
            self.assertEqual(
                sig_events,
                [],
                f"signaled released cmd_pid/PGID after normal reap + simultaneous lease loss: {sig_events}",
            )


if __name__ == "__main__":
    unittest.main()
