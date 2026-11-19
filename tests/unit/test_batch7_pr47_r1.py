"""Regression tests for batch7 PR #47 round-1 review findings (1-8)."""

import ast
import fcntl
import glob
import importlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import run_tier
from tests.unit import test_merge as tm
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


class TestFinding1MergeRefPinning(unittest.TestCase):
    """P1: scripts/coord/merge.sh must merge verified refs/heads/wt/<N> SHA and refuse moves."""

    def test_tag_shadow_merges_reviewed_branch_not_tag(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-mtag-") as tmp:
            repo, _ = _make_coord_repo(tmp)
            # Create an unreviewed side commit and tag it wt/r1.
            _git(repo, "checkout", "-q", "-b", "side")
            _write(os.path.join(repo, "secret.txt"), "UNREVIEWED TAG CONTENT\n")
            _git(repo, "add", "secret.txt")
            _git(repo, "commit", "-qm", "unreviewed side commit")
            _git(repo, "tag", "wt/r1")
            _git(repo, "checkout", "-q", "main")
            _git(repo, "branch", "-D", "side")

            # Create the real reviewed branch wt/r1 from main.
            _git(repo, "checkout", "-q", "-b", "wt/r1")
            _write(os.path.join(repo, "feature.txt"), "REVIEWED BRANCH CONTENT\n")
            _git(repo, "add", "feature.txt")
            _git(repo, "commit", "-qm", "reviewed branch commit")
            reviewed_sha = _git(repo, "rev-parse", "refs/heads/wt/r1^{commit}").stdout.strip()
            _git(repo, "checkout", "-q", "main")

            _write(
                os.path.join(repo, "docs", "coordination", "reviews", "rev1.md"),
                f"VERDICT: ACCEPT\nCovers {reviewed_sha[:7]}\n",
            )
            _git(repo, "add", "docs/coordination/reviews/rev1.md")
            _git(repo, "commit", "-qm", "record accept review")

            res = subprocess.run(
                ["bash", "scripts/coord/merge.sh", "r1", "rev1", "merge r1"],
                cwd=repo,
                env=dict(os.environ, **GIT_ENV),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn(f"merged wt/r1 ({reviewed_sha})", res.stdout)
            self.assertNotIn("WARN:", res.stdout)
            self.assertTrue(os.path.exists(os.path.join(repo, "feature.txt")))
            self.assertFalse(os.path.exists(os.path.join(repo, "secret.txt")))
            parents = _git(repo, "log", "-1", "--format=%P", "HEAD").stdout.split()
            self.assertIn(reviewed_sha, parents)

    def test_branch_move_between_check_and_merge_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-mrace-") as tmp:
            repo, _ = _make_coord_repo(tmp)
            _git(repo, "checkout", "-q", "-b", "wt/r1")
            _write(os.path.join(repo, "feature.txt"), "reviewed\n")
            _git(repo, "add", "feature.txt")
            _git(repo, "commit", "-qm", "reviewed commit")
            reviewed_sha = _git(repo, "rev-parse", "refs/heads/wt/r1^{commit}").stdout.strip()

            # Prepare an unreviewed second commit for wt/r1 and rewind branch back to reviewed_sha.
            _write(os.path.join(repo, "late.txt"), "UNREVIEWED LATE COMMIT\n")
            _git(repo, "add", "late.txt")
            _git(repo, "commit", "-qm", "unreviewed late commit")
            late_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
            _git(repo, "checkout", "-q", "main")
            _git(repo, "branch", "-f", "wt/r1", reviewed_sha)

            _write(
                os.path.join(repo, "docs", "coordination", "reviews", "rev1.md"),
                f"VERDICT: ACCEPT\n{reviewed_sha[:7]}\n",
            )
            _git(repo, "add", "docs/coordination/reviews/rev1.md")
            _git(repo, "commit", "-qm", "record accept review")
            main_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

            # Deterministic race hook: when merge.sh runs `grep` to check ${H:0:7} in rev1.md,
            # advance refs/heads/wt/r1 to late_sha before git merge executes.
            real_grep = shutil.which("grep")
            real_git = shutil.which("git")
            race_flag = os.path.join(tmp, "raced")
            fake_bin = os.path.join(tmp, "bin")
            _write(
                os.path.join(fake_bin, "grep"),
                "#!/bin/sh\n"
                'hit=0\n'
                'for a in "$@"; do case "$a" in docs/coordination/reviews/*.md) hit=1 ;; esac; done\n'
                f'if [ "$hit" = 1 ] && [ ! -e "{race_flag}" ]; then\n'
                f'  : > "{race_flag}"\n'
                f'  "{real_git}" update-ref refs/heads/wt/r1 "{late_sha}"\n'
                "fi\n"
                f'exec "{real_grep}" "$@"\n',
                0o755,
            )

            env = dict(os.environ, **GIT_ENV, PATH=f"{fake_bin}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                ["bash", "scripts/coord/merge.sh", "r1", "rev1", "merge r1"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertTrue(os.path.exists(race_flag), "race hook did not trigger")
            self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
            self.assertIn("REFUSED:", res.stdout)
            self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), main_before)
            self.assertFalse(os.path.exists(os.path.join(repo, "late.txt")))


class TestFinding2SecretPatternQuotesAndPrefixes(unittest.TestCase):
    """P1: secret-pattern.sh must catch escaped quotes, triple quotes, and Python prefixes."""

    def test_matches_json_escaped_triple_quoted_and_python_prefixed_secrets(self):
        # Construct test patterns piecewise so this test file never triggers secret scanners.
        ak = "OPENAI_" + "API_KEY"
        bd = "BRIGHTDATA_" + "PASSWORD"
        tg = "HERMES_TELEGRAM_" + "BOT_TOKEN"
        wc = "WECHAT_ROUTINE_" + "WEBHOOK_KEY"
        val = "sk_" + "custom_" + "secret_12345"

        positives = [
            '{"command": "' + ak + '=\\"' + val + '\\" run"}',
            '"{\\"' + ak + '\\": \\"' + val + '\\"}"',
            f'{ak} = """{val}"""',
            f"{ak} = '''{val}'''",
            f'{ak} = r"{val}"',
            f'{ak} = b"{val}"',
            f'{ak} = u"{val}"',
            f'{ak} = R"{val}"',
            f'{ak} = B"{val}"',
            f'{ak} = U"{val}"',
            f'{ak} = rb"{val}"',
            f'{ak} = br"{val}"',
            f"{ak} = r'{val}'",
            f'{ak} = r"""{val}"""',
            f"{ak}=b'''{val}'''",
            f'{ak} = r\\"{val}\\"',
            f'{bd}=\\"{val}\\"',
            f'{tg}=r"{val}"',
            f'{wc}=b"{val}"',
            f"{ak}=real_val_123",
            f"{ak}=r_real_123",
        ]
        for sample in positives:
            self.assertEqual(_grep_secret_pattern([sample]), [sample], f"missed: {sample!r}")

        negatives = [
            'API_KEY_NAME=\\"OPENAI\\"',
            f'FOO_{"API_KEY"}=\\"$(cat ~/k)\\"',
            f'FOO_{"API_KEY"}=\\"`cat ~/k`\\"',
            f'FOO_{"API_KEY"}=$MY_KEY_VAR',
            f'FOO_{"API_KEY"}=r"$MY_KEY_VAR"',
            f'FOO_{"API_KEY"}=\\"short\\"',
            f'FOO_{"API_KEY"}=r"short"',
            f'FOO_{"API_KEY"}="""short"""',
            f'{ak}=\\"\\"',
            f'{ak}=""""""',
            f"{ak}=''''''",
            f'{ak}=r""',
            f'{ak}=b""',
            f'{ak}=rb""',
            f"{ak}=\\nmy_secret_val\\n",
            f'{ak}="" foo=123456',
            f'{ak}=\\"\\" foo=123456',
        ]
        self.assertEqual(_grep_secret_pattern(negatives), [])

    def test_scanners_block_escaped_json_and_triple_quotes_and_no_self_match(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-sec-") as tmp:
            repo, fake_gl = _make_coord_repo(tmp)
            bare = os.path.join(tmp, "github.git")
            _git(tmp, "init", "--bare", bare)
            _git(repo, "remote", "add", "github", bare)
            _git(repo, "push", "github", "main:refs/heads/main")

            hook_json = os.path.join(repo, ".kiro", "agents", "custom.json")
            _write(
                hook_json,
                '{"command": "OPENAI_' + 'API_KEY=\\"sk_' + 'custom_secret_12345\\" run"}\n',
            )
            mod_py = os.path.join(repo, "mod.py")
            _write(
                mod_py,
                'KEY_' + 'ALT_API_KEY = r"""sk_' + 'custom_secret_67890"""\n',
            )
            _git(repo, "add", hook_json, mod_py)
            env = dict(os.environ, GITLEAKS=fake_gl)

            res_scan = subprocess.run(
                ["bash", ".kiro/hooks/scripts/secret-scan.sh"],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_scan.returncode, 1, res_scan.stdout + res_scan.stderr)
            self.assertIn("pattern hit in staged .kiro/agents/custom.json", res_scan.stdout)
            self.assertIn("pattern hit in staged mod.py", res_scan.stdout)

            _git(repo, "commit", "-qm", "escaped secret", env_extra=GIT_ENV)
            _git(repo, "branch", "wt/r1")

            res_pr = subprocess.run(
                ["bash", "scripts/coord/push-review.sh", "r1"],
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
            "tests/unit/test_batch7_pr47_r1.py",
        ):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                self.assertEqual(_grep_secret_pattern(fh.read().splitlines()), [], rel)


class TestFinding3MigrateToPaseoFailClosed(unittest.TestCase):
    """P2: migrate-to-paseo.sh paseo_auto_accept must fail closed on non-bools, wildcards, duplicates."""

    def _run_paseo_auto_accept(self, home, aid):
        script_path = os.path.join(ROOT, "scripts", "coord", "migrate-to-paseo.sh")
        with open(script_path, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"(?ms)^paseo_auto_accept\(\)\s*\{.*?^\}", text)
        self.assertIsNotNone(m, "paseo_auto_accept function not found")
        fn_src = m.group(0)
        res = subprocess.run(
            ["bash", "-c", fn_src + '\npaseo_auto_accept "$1"', "_", aid],
            env=dict(os.environ, HOME=home),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()

    def test_auto_accept_rejects_null_nonbool_wildcards_duplicates_and_malformed_json(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-paseo-") as tmp:
            home = os.path.join(tmp, "home")
            kiro_dir = os.path.join(home, ".paseo", "agents", "kiro")
            other_dir = os.path.join(home, ".paseo", "agents", "other")
            os.makedirs(kiro_dir)
            os.makedirs(other_dir)

            # Valid baseline agent in kiro/
            _write(
                os.path.join(kiro_dir, "ag_ok.json"),
                json.dumps({"features": [{"id": "auto_accept", "value": False}]}),
            )
            self.assertEqual(self._run_paseo_auto_accept(home, "ag_ok"), "off")

            _write(
                os.path.join(kiro_dir, "ag_ok_fv.json"),
                json.dumps({
                    "features": [{"id": "auto_accept", "value": False}],
                    "config": {"featureValues": {"auto_accept": False}},
                }),
            )
            self.assertEqual(self._run_paseo_auto_accept(home, "ag_ok_fv"), "off")

            # Fail-closed non-boolean / null / implicit defaultValue / conflicting featureValues
            non_off_payloads = {
                "ag_null": {"features": [{"id": "auto_accept", "value": None, "defaultValue": True}]},
                "ag_default_only": {"features": [{"id": "auto_accept", "defaultValue": True}]},
                "ag_str_true": {"features": [{"id": "auto_accept", "value": "true"}]},
                "ag_int_one": {"features": [{"id": "auto_accept", "value": 1}]},
                "ag_str_false": {"features": [{"id": "auto_accept", "value": "false"}]},
                "ag_int_zero": {"features": [{"id": "auto_accept", "value": 0}]},
                "ag_fv_str_true": {
                    "features": [{"id": "auto_accept", "value": False}],
                    "config": {"featureValues": {"auto_accept": "true"}},
                },
                "ag_fv_int_one": {
                    "features": [{"id": "auto_accept", "value": False}],
                    "config": {"featureValues": {"auto_accept": 1}},
                },
                "ag_dup_entry": {
                    "features": [
                        {"id": "auto_accept", "value": False},
                        {"id": "auto_accept", "value": None},
                    ]
                },
                "ag_bad_list": [],
                "ag_bad_cfg": {"features": [{"id": "auto_accept", "value": False}], "config": "bad"},
                "ag_bad_fv": {
                    "features": [{"id": "auto_accept", "value": False}],
                    "config": {"featureValues": ["bad"]},
                },
                "ag_bad_feats": {"features": {"id": "auto_accept", "value": False}},
            }
            for name, payload in non_off_payloads.items():
                _write(os.path.join(kiro_dir, f"{name}.json"), json.dumps(payload))
                got = self._run_paseo_auto_accept(home, name)
                self.assertNotEqual(got, "off", f"{name} returned off for {payload!r}")

            _write(os.path.join(kiro_dir, "ag_malformed.json"), "{not-json")
            self.assertNotEqual(self._run_paseo_auto_accept(home, "ag_malformed"), "off")

            # Wildcards / leading dash / traversal must not match ag_ok.json!
            for bad_aid in ("*", "ag_*", "ag_o?", "ag_[ox]k", "-ag_ok", "../kiro/ag_ok", ""):
                got = self._run_paseo_auto_accept(home, bad_aid)
                self.assertEqual(got, "invalid", f"bad_aid={bad_aid!r} got {got!r}")

            # Duplicate ID across two provider directories must never read just fs[0] and return off.
            _write(
                os.path.join(kiro_dir, "ag_dup.json"),
                json.dumps({"features": [{"id": "auto_accept", "value": False}]}),
            )
            _write(
                os.path.join(other_dir, "ag_dup.json"),
                json.dumps({"features": [{"id": "auto_accept", "value": True}]}),
            )
            self.assertNotEqual(self._run_paseo_auto_accept(home, "ag_dup"), "off")

    def test_script_aborts_on_wildcard_agent_id_and_null_value(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-mig-") as tmp:
            repo, _ = _make_coord_repo(tmp)
            home = os.path.join(tmp, "home")
            _write(
                os.path.join(home, ".paseo", "agents", "kiro", "ag_1.json"),
                json.dumps({"features": [{"id": "auto_accept", "value": None, "defaultValue": True}]}),
            )
            _write(
                os.path.join(home, ".paseo", "agents", "kiro", "ag_other.json"),
                json.dumps({"features": [{"id": "auto_accept", "value": False}]}),
            )
            _write(os.path.join(repo, "docs", "coordination", ".session"), "ses_1\n")
            _write(os.path.join(repo, "prompts", "kiro-coordinator-continue.md"), "go\n")
            os.makedirs(os.path.join(repo, "logs", "raw"), exist_ok=True)

            bindir = os.path.join(tmp, "bin")
            send_log = os.path.join(tmp, "send.log")
            _write(os.path.join(bindir, "tmux"), "#!/bin/sh\nexit 1\n", 0o755)
            _write(os.path.join(bindir, "pgrep"), "#!/bin/sh\nexit 1\n", 0o755)
            _write(os.path.join(bindir, "sleep"), "#!/bin/sh\nexit 0\n", 0o755)

            for aid_out in ("*", "ag_1"):
                if os.path.exists(send_log):
                    os.remove(send_log)
                _write(
                    os.path.join(bindir, "paseo"),
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    f'  import) printf \'{{"agentId": "%s"}}\\n\' "{aid_out}" ;;\n'
                    f'  send) echo "$*" >> "{send_log}" ;;\n'
                    '  wait|agent|archive) exit 0 ;;\n'
                    "esac\n",
                    0o755,
                )
                env = dict(os.environ, HOME=home, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
                res = subprocess.run(
                    ["bash", "scripts/coord/migrate-to-paseo.sh"],
                    cwd=repo, env=env, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn("ABORT: auto-accept not verifiably off", res.stdout)
                self.assertFalse(os.path.exists(send_log), f"paseo send called for {aid_out!r}")


class TestFinding4UnitProcLockAudit(unittest.TestCase):
    """P2: all /proc-scanning unit test modules must resolve MESH_PROC_LOCK='shared' (or exclusive)."""

    def test_proc_scanning_modules_resolve_shared_lock_in_run_tier(self):
        required_shared_modules = [
            "tests.unit.test_batch7_pr43_r1",
            "tests.unit.test_batch7_pr45_r1",
            "tests.unit.test_batch7_pr45_r2",
            "tests.unit.test_merge_recovery_minors",
            "tests.unit.test_batch7_g2",
            "tests.unit.test_blind_review_b34",
            "tests.unit.test_merge",
            "tests.unit.test_proc_probe",
            "tests.unit.test_runtargets_race",
            "tests.unit.test_seen_hold_r4d",
            "tests.unit.test_seen_hold_r4e",
            "tests.unit.test_termination_units",
        ]
        loader = unittest.TestLoader()
        old_default = run_tier.DEFAULT_PROC_LOCK
        run_tier.DEFAULT_PROC_LOCK = None
        try:
            for mod_name in required_shared_modules:
                mod = importlib.import_module(mod_name)
                suite = loader.loadTestsFromModule(mod)
                cases = run_tier.extract_tests(suite)
                self.assertTrue(cases, f"no cases in {mod_name}")
                for case in cases:
                    self.assertEqual(
                        run_tier.proc_lock_mode(case),
                        "shared",
                        f"{mod_name} {case.id()} lacks shared proc lock",
                    )

            # Base classes in test_merge also carry mesh_proc_lock='shared' for cross-module subclasses.
            self.assertEqual(getattr(tm.TaskBranchCase, "mesh_proc_lock", None), "shared")
            self.assertEqual(getattr(tm.MergeDbCase, "mesh_proc_lock", None), "shared")

            # Comprehensive audit across ALL tests/unit/test_*.py for unmocked real /proc scans.
            target_attrs = {
                ("terminate", "run_targets"),
                ("terminate", "begin"),
                ("recovery", "recover"),
                ("recovery", "_step_merge_runs"),
                ("supervisor", "watch_launch_runs"),
                ("supervisor", "advance_terminations"),
                ("supervisor", "_watch_lead_turn"),
                ("merge", "run_merge_pipeline"),
                ("merge", "run_test_step"),
            }
            for path in sorted(glob.glob(os.path.join(ROOT, "tests", "unit", "test_*.py"))):
                rel = os.path.relpath(path, ROOT)
                mod_name = rel[:-3].replace(os.sep, ".")
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=rel)
                uses_real_scan = any(
                    (
                        isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name)
                        and (n.value.id, n.attr) in target_attrs
                    )
                    or (isinstance(n, ast.Name) and n.id == "find_run_processes")
                    for n in ast.walk(tree)
                )
                if not uses_real_scan or mod_name == "tests.unit.test_linear_full_daemon":
                    continue
                has_module_lock = any(
                    isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "MESH_PROC_LOCK" for t in n.targets)
                    for n in tree.body
                )
                self.assertTrue(has_module_lock, f"{rel} scans real /proc without MESH_PROC_LOCK")
        finally:
            run_tier.DEFAULT_PROC_LOCK = old_default

    def test_run_tier_serializes_shared_behind_exclusive_lock(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-lock-") as tmp:
            lock_path = os.path.join(tmp, "proc.lock")
            tdir = os.path.join(tmp, "t")
            dur = os.path.join(tmp, "dur.json")
            done_flag = os.path.join(tmp, "shared.ran")
            _write(
                os.path.join(tdir, "test_sh.py"),
                "import os, unittest\n"
                "MESH_PROC_LOCK = 'shared'\n"
                "class S(unittest.TestCase):\n"
                f"    def test_s(self): open({done_flag!r}, 'w').write('ok')\n",
            )
            # Hold exclusive lock for 0.6s in background.
            holder = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import fcntl, os, sys, time; "
                    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
                    "fcntl.flock(fd, fcntl.LOCK_EX); "
                    "open(sys.argv[2], 'w').write('held'); "
                    "time.sleep(0.6); "
                    "fcntl.flock(fd, fcntl.LOCK_UN)",
                    lock_path, os.path.join(tmp, "held"),
                ]
            )
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(os.path.join(tmp, "held")) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(os.path.join(tmp, "held")))
                env = dict(os.environ, MESH_TEST_PROC_LOCK=lock_path, MESH_TEST_DURATIONS=dur)
                t0 = time.monotonic()
                res = subprocess.run(
                    [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), tdir],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
                )
                elapsed = time.monotonic() - t0
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertTrue(os.path.exists(done_flag))
                self.assertGreaterEqual(elapsed, 0.45)
            finally:
                holder.wait(timeout=5)


class TestFinding5And8InboxWorkerCleanup(unittest.TestCase):
    """P2/P3: heartbeat child is exec'd (no orphan $MESH_BIN) and leftovers get SIGTERM promptly."""

    def _setup_worker_dir(self, tmp):
        d = os.path.join(tmp, "w")
        os.makedirs(os.path.join(d, "ws"), mode=0o700)
        tok = os.path.join(d, "worker.token")
        _write(tok, "tok\n", 0o600)
        return d, tok

    def test_heartbeat_child_not_orphaned_on_normal_exit_or_sigterm(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-hb-") as tmp:
            d, tok = self._setup_worker_dir(tmp)
            stub = os.path.join(d, "mesh")
            hb_lock = os.path.join(d, "hb.lock")
            hb_ready = os.path.join(d, "hb.ready")
            hb_pid_file = os.path.join(d, "hb.pid")

            # Custom stub holding an exclusive flock while task heartbeat blocks for 30s.
            custom_stub = WORKER_STUB.replace(
                "        echo '{\"ok\": true, \"data\": {}}' ;;",
                "        exec python3 -c '\n"
                "import fcntl, os, sys, time\n"
                "pidf, readyf, lockf = sys.argv[1:4]\n"
                "fd = os.open(lockf, os.O_CREAT | os.O_RDWR, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "open(pidf, \"w\").write(str(os.getpid()))\n"
                "open(readyf, \"w\").write(\"1\")\n"
                "time.sleep(30)\n"
                f"' '{hb_pid_file}' '{hb_ready}' '{hb_lock}' ;;\n",
            )
            _write(stub, custom_stub, 0o755)
            _write(os.path.join(d, "hb-every"), "0")
            env = dict(
                os.environ,
                MESH_BIN=stub,
                MESH_TOKEN_FILE=tok,
                STUB_DIR=d,
                WAIT_TIMEOUT="1s",
                WAIT_ROUNDS="1",
            )

            def cleanup_hb():
                if os.path.exists(hb_pid_file):
                    try:
                        os.kill(int(open(hb_pid_file).read().strip()), signal.SIGKILL)
                    except (OSError, ValueError):
                        pass

            self.addCleanup(cleanup_hb)

            # Command waits until heartbeat process has started and holds hb.lock, then exits 0.
            cmd = (
                f"import os, time; "
                f"deadline = time.monotonic() + 10\n"
                f"while time.monotonic() < deadline and not os.path.exists({hb_ready!r}): time.sleep(0.02)\n"
                f"assert os.path.exists({hb_ready!r}), 'heartbeat never started'\n"
            )
            res = subprocess.run(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", cmd],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            # Kernel releases hb.lock immediately when the heartbeat process is dead.
            fd = os.open(hb_lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail("heartbeat $MESH_BIN subprocess was left running as an orphan!")
            finally:
                os.close(fd)

            # Also verify no orphan when worker receives SIGTERM during heartbeat execution.
            for p in (hb_ready, hb_pid_file):
                if os.path.exists(p):
                    os.remove(p)
            cmd_wait = f"import time; time.sleep(30)"
            proc = subprocess.Popen(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", cmd_wait],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not os.path.exists(hb_ready):
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(hb_ready), "heartbeat never started on SIGTERM case")
                proc.send_signal(signal.SIGTERM)
                _, err = proc.communicate(timeout=10)
            except Exception:
                proc.kill()
                proc.communicate()
                raise
            self.assertEqual(proc.returncode, 143, err)
            fd2 = os.open(hb_lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail("heartbeat $MESH_BIN subprocess orphaned after worker SIGTERM!")
            finally:
                os.close(fd2)

    def test_leftover_pgid_processes_receive_sigterm_immediately_on_exit(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-term-") as tmp:
            d, tok = self._setup_worker_dir(tmp)
            stub = os.path.join(d, "mesh")
            _write(stub, WORKER_STUB, 0o755)
            _write(os.path.join(d, "hb-every"), "40")
            sig_marker = os.path.join(d, "got-sigterm")
            ready = os.path.join(d, "bg-ready")
            bg_lock = os.path.join(d, "bg.lock")
            child_py = (
                "import fcntl, os, signal, sys, time; "
                "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "signal.signal(signal.SIGTERM, lambda *_: (open(sys.argv[2], 'w').write('term'), sys.exit(0))); "
                "open(sys.argv[3], 'w').write('1'); "
                "time.sleep(30)"
            )
            launcher_py = (
                "import os, subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_py!r}, {bg_lock!r}, {sig_marker!r}, {ready!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"deadline = time.monotonic() + 5\n"
                f"while time.monotonic() < deadline and not os.path.exists({ready!r}): time.sleep(0.02)\n"
            )
            env = dict(
                os.environ,
                MESH_BIN=stub,
                MESH_TOKEN_FILE=tok,
                STUB_DIR=d,
                WAIT_TIMEOUT="1s",
                WAIT_ROUNDS="1",
                KILL_GRACE_S="5",
            )
            t0 = time.monotonic()
            res = subprocess.run(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", launcher_py],
                env=env, capture_output=True, text=True, timeout=15,
            )
            elapsed = time.monotonic() - t0
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(os.path.exists(sig_marker), "leftover child did not receive SIGTERM")
            self.assertLess(elapsed, 2.0, f"worker waited full KILL_GRACE_S instead of SIGTERM: {elapsed:.2f}s")
            fd = os.open(bg_lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.fail("leftover process still alive")
            finally:
                os.close(fd)

            # A leftover child that ignores SIGTERM still gets bounded KILL_GRACE_S and then SIGKILL.
            ign_lock = os.path.join(d, "ign.lock")
            ign_ready = os.path.join(d, "ign-ready")
            ign_child = (
                "import fcntl, os, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "open(sys.argv[2], 'w').write('1'); "
                "time.sleep(30)"
            )
            ign_launcher = (
                "import os, subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {ign_child!r}, {ign_lock!r}, {ign_ready!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"deadline = time.monotonic() + 5\n"
                f"while time.monotonic() < deadline and not os.path.exists({ign_ready!r}): time.sleep(0.02)\n"
            )
            t1 = time.monotonic()
            res_ign = subprocess.run(
                ["bash", WORKER_SCRIPT, "--", sys.executable, "-c", ign_launcher],
                env=dict(env, KILL_GRACE_S="1"), capture_output=True, text=True, timeout=15,
            )
            dt_ign = time.monotonic() - t1
            self.assertEqual(res_ign.returncode, 0, res_ign.stderr)
            self.assertGreaterEqual(dt_ign, 0.9, f"SIG_IGN leftover was killed before grace expired: {dt_ign:.2f}s")
            fd_ign = os.open(ign_lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd_ign, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.fail("SIG_IGN leftover survived SIGKILL")
            finally:
                os.close(fd_ign)

            # Exact review Repro (`sh -c 'sleep 30 ... & exit 0'`): must return well before KILL_GRACE_S=5.
            t2 = time.monotonic()
            res_sh = subprocess.run(
                ["bash", WORKER_SCRIPT, "--", "sh", "-c", "sleep 30 </dev/null >/dev/null 2>&1 & exit 0"],
                env=env, capture_output=True, text=True, timeout=15,
            )
            dt_sh = time.monotonic() - t2
            self.assertEqual(res_sh.returncode, 0, res_sh.stderr)
            self.assertLess(dt_sh, 2.0, f"sh background sleep stalled grace loop: {dt_sh:.2f}s")


class TestFinding6RunTierDiscoveryAndSkip(unittest.TestCase):
    """P2: run_tier.py must propagate top_level in PYTHONPATH, handle module SkipTest, and keep loader.errors."""

    def _clean_env(self, tmp):
        env = dict(os.environ, MESH_TEST_DURATIONS=os.path.join(tmp, "dur.json"), MESH_TEST_PROC_LOCK=os.path.join(tmp, "p.lock"))
        env.pop("PYTHONPATH", None)
        return env

    def test_external_dir_and_outside_cwd_work_without_pythonpath(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-rt-") as tmp:
            env = self._clean_env(tmp)
            ext_dir = os.path.join(tmp, "ext_tests")
            _write(os.path.join(ext_dir, "helper_ext.py"), "VALUE = 42\n")
            _write(
                os.path.join(ext_dir, "test_ext.py"),
                "import unittest, helper_ext, mesh.constants\n"
                "class E(unittest.TestCase):\n"
                "    def test_ok(self): self.assertEqual(helper_ext.VALUE, 42)\n",
            )
            res = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), ext_dir],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("OK\n", res.stdout)

            # Invoking tests/run_tier.py on a fake repo's tests/unit from outside or parent cwd without PYTHONPATH.
            fake_repo = os.path.join(tmp, "fakerepo")
            os.makedirs(os.path.join(fake_repo, "tests", "unit"))
            _write(os.path.join(fake_repo, "tests", "__init__.py"), "")
            _write(os.path.join(fake_repo, "tests", "unit", "__init__.py"), "")
            shutil.copy2(os.path.join(ROOT, "tests", "run_tier.py"), os.path.join(fake_repo, "tests", "run_tier.py"))
            _write(os.path.join(fake_repo, "fakemesh", "__init__.py"), "TOKEN = 'ok'\n")
            _write(
                os.path.join(fake_repo, "tests", "unit", "test_in_repo.py"),
                "import unittest, fakemesh\n"
                "class R(unittest.TestCase):\n"
                "    def test_imp(self): self.assertEqual(fakemesh.TOKEN, 'ok')\n",
            )
            outside_cwd = os.path.join(tmp, "outside_cwd")
            os.makedirs(outside_cwd)
            for caller_cwd in (outside_cwd, tmp):
                res_out = subprocess.run(
                    [sys.executable, os.path.join(fake_repo, "tests", "run_tier.py"), os.path.join(fake_repo, "tests", "unit")],
                    cwd=caller_cwd, env=env, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(res_out.returncode, 0, f"cwd={caller_cwd}: {res_out.stdout}\n{res_out.stderr}")
                self.assertIn("OK\n", res_out.stdout)

    def test_module_level_skiptest_reported_as_skipped_with_rc_zero(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-mskip-") as tmp:
            env = self._clean_env(tmp)
            tdir = os.path.join(tmp, "mskip")
            _write(
                os.path.join(tdir, "test_mod_skip.py"),
                "import unittest\nraise unittest.SkipTest('demo module skip')\n",
            )
            res = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), tdir],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("OK (skipped=1)", res.stdout)
            self.assertNotIn("ERROR", res.stdout)

    def test_loader_errors_not_discarded_when_failed_test_also_present(self):
        with tempfile.TemporaryDirectory(prefix="mh-pr47-lerr-") as tmp:
            tdir = os.path.join(tmp, "lerr")
            _write(os.path.join(tdir, "test_bad_imp.py"), "import nonexistent_mod_pr47\n")
            _write(os.path.join(tdir, "test_ok.py"), "import unittest\nclass K(unittest.TestCase):\n    def test_x(self): pass\n")
            # Run run_tier.main() in-process with a synthetic extra loader.errors entry alongside _FailedTest.
            code = (
                "import sys, unittest\n"
                "from tests import run_tier\n"
                "orig = unittest.TestLoader.discover\n"
                "def wrapped(self, s, pattern='test*.py', top_level_dir=None):\n"
                "    st = orig(self, s, pattern=pattern, top_level_dir=top_level_dir)\n"
                "    self.errors.append('extra_dir_discovery_error_pr47')\n"
                "    return st\n"
                "unittest.TestLoader.discover = wrapped\n"
                "sys.argv = ['run_tier.py', sys.argv[1]]\n"
                "raise SystemExit(run_tier.main())\n"
            )
            res = subprocess.run(
                [sys.executable, "-c", code, tdir],
                cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=ROOT, MESH_TEST_DURATIONS=os.path.join(tmp, "dur.json"),
                         MESH_TEST_PROC_LOCK=os.path.join(tmp, "p.lock")),
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("nonexistent_mod_pr47", res.stderr)
            self.assertIn("extra_dir_discovery_error_pr47", res.stderr)
            self.assertIn("errors=2", res.stdout)


class TestFinding7CoderGuardErrorHandlingAndSubdirs(unittest.TestCase):
    """P3: coder-guard.sh degrades on non-git dirs under pipefail, refuses loudly on git failure, handles subdirs."""

    def test_non_git_dir_admits_and_git_error_refuses_loudly_under_pipefail(self):
        guard = os.path.join(ROOT, "scripts", "coord", "coder-guard.sh")
        with tempfile.TemporaryDirectory(prefix="mh-pr47-cg-") as tmp:
            env = dict(os.environ, HOME=tmp, CODER_GUARD_LOCK=os.path.join(tmp, "cg.lock"))

            # Non-git directory with set -euo pipefail must degrade cleanly, never silent exit 128.
            nongit = os.path.join(tmp, "plain-dir")
            os.makedirs(nongit)
            res_ok = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; source "{guard}"; coder_guard; echo ADMITTED'],
                cwd=nongit, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_ok.returncode, 0, res_ok.stdout + res_ok.stderr)
            self.assertIn("ADMITTED", res_ok.stdout)

            # Corrupt .git dir under set -euo pipefail must fail with clear REFUSED message, never silent exit 128 or fail-open.
            corrupt = os.path.join(tmp, "corrupt-repo")
            os.makedirs(os.path.join(corrupt, ".git"))
            res_corrupt = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; source "{guard}"; coder_guard; echo ADMITTED'],
                cwd=corrupt, env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(res_corrupt.returncode, 0, res_corrupt.stdout + res_corrupt.stderr)
            self.assertNotIn("ADMITTED", res_corrupt.stdout)
            self.assertIn("REFUSED:", res_corrupt.stdout + res_corrupt.stderr)

            # Explicit git failure (e.g. dubious ownership / broken GIT_DIR) must fail loudly, not degrade open.
            real_repo, _ = _make_coord_repo(os.path.join(tmp, "r"))
            env_bad_git = dict(env, GIT_DIR=os.path.join(tmp, "missing-git-dir"))
            res_bad = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; source "{guard}"; coder_guard; echo ADMITTED'],
                cwd=real_repo, env=env_bad_git, capture_output=True, text=True,
            )
            self.assertNotEqual(res_bad.returncode, 0, res_bad.stdout + res_bad.stderr)
            self.assertNotIn("ADMITTED", res_bad.stdout)
            self.assertIn("REFUSED:", res_bad.stdout + res_bad.stderr)

    def test_custom_worktree_subdirs_enforce_same_worktree_and_count_distinct_roots(self):
        guard = os.path.join(ROOT, "scripts", "coord", "coder-guard.sh")
        with tempfile.TemporaryDirectory(prefix="mh-pr47-cgwt-") as tmp:
            repo, _ = _make_coord_repo(tmp)
            wt1 = os.path.join(tmp, "wt-alpha")
            wt2 = os.path.join(tmp, "wt-beta")
            _git(repo, "worktree", "add", "-q", "-b", "wt/a", wt1)
            _git(repo, "worktree", "add", "-q", "-b", "wt/b", wt2)
            wt1_sub_a = os.path.join(wt1, "sub", "a")
            wt1_sub_b = os.path.join(wt1, "sub", "b")
            wt2_sub = os.path.join(wt2, "sub")
            repo_sub = os.path.join(repo, "scripts")
            os.makedirs(wt1_sub_a)
            os.makedirs(wt1_sub_b)
            os.makedirs(wt2_sub)

            vibe_bin = os.path.join(tmp, "bin", "vibe")
            _write(vibe_bin, "#!/bin/sh\nsleep 30\n", 0o755)
            env = dict(
                os.environ,
                HOME=tmp,
                PATH=f"{os.path.dirname(vibe_bin)}:{os.environ.get('PATH', '')}",
                CODER_GUARD_LOCK=os.path.join(tmp, "cg.lock"),
                MAX_CODERS="2",
            )

            def _kill_pg(proc):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()

            def _spawn(cwd, tag):
                p = subprocess.Popen(
                    ["vibe", "--prompt-file", f"{tag}.md"],
                    cwd=cwd, env=env, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.addCleanup(_kill_pg, p)
                return p

            _spawn(wt1_sub_a, "p1")
            res_same = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; source "{guard}"; coder_guard; echo ADMITTED'],
                cwd=wt1_sub_b, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_same.returncode, 3, res_same.stdout + res_same.stderr)
            self.assertIn("already active in this worktree", res_same.stdout)

            _spawn(wt2_sub, "p2")
            res_cap = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; source "{guard}"; coder_guard; echo ADMITTED'],
                cwd=repo_sub, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_cap.returncode, 3, res_cap.stdout + res_cap.stderr)
            self.assertIn("2 coder worktrees active", res_cap.stdout)


if __name__ == "__main__":
    unittest.main()
