"""Linear full-codebase review of main 1f78a4f, part D (scripts/ and .kiro/hooks).

Each test runs the real script (in a throwaway git repo or with stubs); no network, no real
coder, reviewer or gitleaks is involved."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from tests.unit.test_inbox_worker_kit import STUB, SCRIPT as WORKER

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COORD = os.path.join(ROOT, "scripts", "coord")
HOOKS = os.path.join(ROOT, ".kiro", "hooks", "scripts")


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check, env=env)


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode:
        os.chmod(path, mode)


class RepoCase(unittest.TestCase):
    """A throwaway repo holding copies of the coordination scripts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        shutil.copytree(COORD, os.path.join(self.repo, "scripts", "coord"))
        shutil.copytree(HOOKS, os.path.join(self.repo, ".kiro", "hooks", "scripts"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")

    def run_sh(self, *args, cwd=None, env=None, timeout=60):
        return subprocess.run(["bash", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})), timeout=timeout)


class TestCoderGuardAtomic(RepoCase):
    def test_d1_two_dispatches_one_admitted(self):
        home = self.tmp
        wt = os.path.join(home, "workspace", "mesh-d1")
        os.makedirs(wt)
        disp = os.path.join(self.tmp, "disp.sh")
        write(disp, f'cd "{wt}"; source "{self.repo}/scripts/coord/coder-guard.sh"; coder_guard; sleep 1\n'
                    "bash -c 'exec -a \"droid exec -f p-linear-d1\" sleep 15' < /dev/null > /dev/null 2>&1 9>&- &\n"
                    "echo ADMITTED\n")
        env = dict(os.environ, HOME=home, CODER_GUARD_LOCK=os.path.join(self.tmp, "guard.lock"), MAX_CODERS="5")
        try:
            procs = [subprocess.Popen(["bash", disp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, env=env) for _ in range(2)]
            outs = [p.communicate(timeout=60)[0] for p in procs]
            self.assertEqual(sum("ADMITTED" in o for o in outs), 1, outs)
            self.assertTrue(any("REFUSED" in o for o in outs), outs)
        finally:
            subprocess.run(["pkill", "-f", "droid exec -f p-linear-d1"])


class TestPushHelpersScanWhatTheyPush(RepoCase):
    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.remote)
        git(self.repo, "remote", "add", "github", self.remote)
        git(self.repo, "push", "-q", "github", "main")

    def fake_gitleaks(self, ref):
        gl = os.path.join(self.tmp, "gl")
        write(gl, "#!/bin/sh\nset -e\n"
                  "export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x\n"
                  f'cd "{self.repo}"\n'
                  f"T=$(git rev-parse {ref}^{{tree}}); C=$(echo late | git commit-tree $T -p {ref}); "
                  f"git update-ref refs/heads/{ref} $C\nexit 0\n", 0o755)
        return gl

    def test_d2_push_review_pushes_scanned_sha(self):
        git(self.repo, "checkout", "-q", "-b", "wt/x")
        write(os.path.join(self.repo, "a.txt"), "a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "a")
        scanned = git(self.repo, "rev-parse", "wt/x").stdout.strip()
        git(self.repo, "checkout", "-q", "main")
        gl = self.fake_gitleaks("wt/x")
        r = self.run_sh("scripts/coord/push-review.sh", "x", env={"GITLEAKS": gl})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pushed = git(self.remote, "rev-parse", "review/x").stdout.strip()
        self.assertEqual(pushed, scanned)
        self.assertNotEqual(git(self.repo, "rev-parse", "wt/x").stdout.strip(), scanned)  # the race happened

    def test_d2_sync_github_pushes_scanned_sha(self):
        write(os.path.join(self.repo, "b.txt"), "b\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-qm", "b")
        scanned = git(self.repo, "rev-parse", "main").stdout.strip()
        gl = self.fake_gitleaks("main")
        r = self.run_sh("scripts/coord/sync-github.sh", env={"GITLEAKS": gl})
        pushed = git(self.remote, "rev-parse", "main").stdout.strip()
        self.assertEqual(pushed, scanned, r.stdout + r.stderr)
        self.assertNotEqual(git(self.repo, "rev-parse", "main").stdout.strip(), scanned)  # the race happened


class TestMergeScript(RepoCase):
    def setUp(self):
        super().setUp()
        self.review = os.path.join(self.repo, "docs", "coordination", "reviews", "r.md")
        git(self.repo, "checkout", "-q", "-b", "wt/x")
        write(os.path.join(self.repo, "f.txt"), "branch\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-qm", "x")
        git(self.repo, "checkout", "-q", "main")

    def commit_review(self, verdict):
        write(self.review, f"VERDICT: {verdict}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "review")

    def test_d3_uncommitted_accept_refused(self):
        self.commit_review("REVISE")
        write(self.review, "VERDICT: ACCEPT\n")
        r = self.run_sh("scripts/coord/merge.sh", "x", "r", "m")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn("wt/x", git(self.repo, "log", "--oneline", "-1").stdout)
        self.assertEqual(git(self.repo, "rev-list", "--count", "main..wt/x").stdout.strip(), "1")

    def test_d4_existing_merge_untouched(self):
        self.commit_review("ACCEPT")
        git(self.repo, "checkout", "-q", "-b", "other", "main~1")
        write(os.path.join(self.repo, "g.txt"), "other\n")
        git(self.repo, "add", "g.txt")
        git(self.repo, "commit", "-qm", "o")
        git(self.repo, "checkout", "-q", "main")
        write(os.path.join(self.repo, "g.txt"), "main\n")
        git(self.repo, "add", "g.txt")
        git(self.repo, "commit", "-qm", "m")
        self.assertNotEqual(git(self.repo, "merge", "other", check=False).returncode, 0)
        write(os.path.join(self.repo, "g.txt"), "resolved by hand\n")
        r = self.run_sh("scripts/coord/merge.sh", "x", "r", "m")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(git(self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode, 0)
        with open(os.path.join(self.repo, "g.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "resolved by hand\n")


class TestKiroRunner(RepoCase):
    def fake_kiro(self, rc):
        home = os.path.join(self.tmp, "home")
        write(os.path.join(home, ".local", "bin", "kiro-cli"),
              "#!/bin/sh\nmkdir -p \"$(dirname \"$OUTF\")\"\necho 'VERDICT: ACCEPT' > \"$OUTF\"\n"
              f"echo partial\nexit {rc}\n", 0o755)
        return home

    def test_d5_interrupted_reviewer_fails(self):
        home = self.fake_kiro(42)
        out = os.path.join(self.repo, "docs", "r.md")
        r = self.run_sh("-c", 'source ./kiro-run-common.sh; run_kiro docs/r.md prompt t',
                        cwd=os.path.join(self.repo, "scripts", "coord"), env={"HOME": home, "OUTF": out})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(out))  # the partial report is kept

    def test_d12_symlinked_entrypoint(self):
        home = self.fake_kiro(0)
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        os.symlink(os.path.join(self.repo, "scripts", "coord", "kiro-plan.sh"), os.path.join(bindir, "kiro-plan.sh"))
        out = os.path.join(self.repo, "docs", "coordination", "plans", "n.md")
        r = self.run_sh(os.path.join(bindir, "kiro-plan.sh"), "n", "goal", cwd=self.tmp,
                        env={"HOME": home, "OUTF": out})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(out))


class TestSecretScanQuotedNames(RepoCase):
    def test_d8_non_ascii_name_is_scanned(self):
        bindir = os.path.join(self.tmp, "bin")
        write(os.path.join(bindir, "gitleaks"), "#!/bin/sh\nexit 0\n", 0o755)
        if os.path.exists("/tmp/gitleaks"):
            self.skipTest("/tmp/gitleaks takes precedence over PATH")
        write(os.path.join(self.repo, ".kiro", "中文.md"), "see /home/alice/notes\n")
        git(self.repo, "add", "-A")
        r = self.run_sh(".kiro/hooks/scripts/secret-scan.sh",
                        env={"PATH": bindir + os.pathsep + os.environ["PATH"]})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("absolute home path", r.stdout)


class TestAcceptanceCheck(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return subprocess.run(["python3", os.path.join(HOOKS, "acceptance-check.py"), f.name],
                              capture_output=True, text=True, timeout=120)

    def test_d9_formatted_ids_are_checked(self):
        self.assertEqual(self.check("| **A99** | x |\n").returncode, 1)
        self.assertEqual(self.check("| `N99` | x |\n").returncode, 1)

    def test_d10_qualified_citation_must_match_module(self):
        bad = "tests/unit/test_inbox_worker_kit.py::test_d10_qualified_citation_must_match_module\n"
        self.assertEqual(self.check(bad).returncode, 1)
        good = ("tests/unit/test_linear_full_scripts.py::TestAcceptanceCheck::"
                "test_d10_qualified_citation_must_match_module\n")
        r = self.check(good)
        self.assertEqual(r.returncode, 0, r.stdout)


class TestWaitPid(unittest.TestCase):
    def test_d11_zero_is_refused(self):
        r = subprocess.run(["bash", os.path.join(COORD, "wait-pid.sh"), str(os.getpid()), "0"],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)


class TestViewKiroLive(RepoCase):
    def test_d13_partial_record_is_not_lost(self):
        log = os.path.join(self.repo, "logs", "raw", "kiro-coord-2026.jsonl")
        line = '{"data": {"update": {"sessionUpdate": "tool_call", "title": "LINEAR-D13-TOOL"}}}\n'
        write(log, line[:20])
        p = subprocess.Popen(["python3", os.path.join(self.repo, "scripts", "coord", "view-kiro-live.py")],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(2.5)
            with open(log, "a", encoding="utf-8") as f:
                f.write(line[20:])
            time.sleep(3)
        finally:
            p.send_signal(signal.SIGTERM)
            out = p.communicate(timeout=10)[0]
        self.assertIn("LINEAR-D13-TOOL", out)


STUB2 = STUB.replace("""  "inbox wait")
""", """  "inbox wait")
    if [ -f "$D/two-offers" ]; then
      echo '{"ok": true, "data": {"items": [{"id": "msg_1", "type": "task.offered", "payload": {"task_id": "tsk_1"}}, {"id": "msg_2", "type": "task.offered", "payload": {"task_id": "tsk_2"}}]}}'; exit 0
    fi
""", 1)


class TestInboxWorker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        os.makedirs(os.path.join(self.dir, "ws"))
        stub = os.path.join(self.dir, "mesh")
        write(stub, STUB2, 0o755)
        tok = os.path.join(self.dir, "worker.token")
        write(tok, "not-a-real-token\n", 0o600)
        self.env = dict(os.environ, MESH_BIN=stub, MESH_TOKEN_FILE=tok, STUB_DIR=self.dir,
                        WAIT_TIMEOUT="1s", WAIT_ROUNDS="1", KILL_GRACE_S="2")

    def calls(self):
        p = os.path.join(self.dir, "calls.log")
        return open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else []

    def test_d6_second_offer_is_not_acked(self):
        write(os.path.join(self.dir, "two-offers"), "")
        r = subprocess.run(["bash", WORKER, "--", "true"], capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("inbox ack msg_1", " | ".join(self.calls()))
        self.assertNotIn("inbox ack msg_2", " | ".join(self.calls()))

    def test_d7_sigterm_ignoring_command_is_killed(self):
        write(os.path.join(self.dir, "lose"), "")
        cmd = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        t0 = time.monotonic()
        r = subprocess.run(["bash", WORKER, "--", "python3", "-c", cmd], capture_output=True, text=True,
                           env=self.env, timeout=40)
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertLess(time.monotonic() - t0, 30)


if __name__ == "__main__":
    unittest.main()
