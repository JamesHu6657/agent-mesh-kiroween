"""Regression tests for batch7 g6 script and test-suite fixes."""

import ast
import contextlib
import importlib.util
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t.com",
}


def run_git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=dict(os.environ, **GIT_ENV),
        capture_output=True,
        text=True,
        check=check,
    )


def write_text(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def load_module(name, rel_path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def grep_secret_pattern(lines):
    pat_file = os.path.join(ROOT, "scripts", "coord", "secret-pattern.sh")
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; grep -E "$P" || true', "_", pat_file],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


def make_script_repo(base_dir):
    repo = os.path.join(base_dir, "repo")
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
    gitleaks = os.path.join(base_dir, "fake-gitleaks")
    write_text(gitleaks, "#!/bin/sh\nexit 0\n", 0o755)
    run_git(repo, "init", "-b", "main")
    write_text(os.path.join(repo, "README.md"), "init\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "init")
    return repo, gitleaks


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/secret-pattern.sh and chmod 0o600 test")
class TestSecretScannerBatch7(unittest.TestCase):
    def test_secret_pattern_matches_quoted_keys_mesh_tokens_and_private_keys(self):
        # Construct test patterns piecewise so this test file never triggers secret scanners.
        api_key = "OPENAI_" + "API_KEY"
        mesh_tok = lambda kind: "mesh" + f"_{kind}_" + "0" * 24
        gh_tok = lambda pfx: pfx + "_" + "0123456789abcdefghijklmnopqrstuvwxyz"
        pem = lambda kind: "-----BEGIN " + kind + "PRIVATE KEY-----"
        secrets = [
            f'{api_key}="my-secret-value"',
            f'{api_key} = "my-secret-value"',
            f'"{api_key}": "my-secret-value"',
            f"{api_key}='my-secret-value'",
            mesh_tok("at"),
            mesh_tok("rt"),
            "fk" + "-" + "0123456789abcdef",
            gh_tok("gho"),
            gh_tok("ghu"),
            gh_tok("ghs"),
            gh_tok("ghr"),
            pem("RSA "),
            pem(""),
            pem("EC "),
        ]
        for sample in secrets:
            self.assertEqual(grep_secret_pattern([sample]), [sample], f"missed secret: {sample}")

        non_secrets = [
            'API_KEY_NAME="OPENAI"',
            f'FOO_{"API_KEY"}="$(cat ~/k)"',
            f'FOO_{"API_KEY"}=abc',
            f'{api_key}=""',
        ]
        self.assertEqual(grep_secret_pattern(non_secrets), [])

    def test_secret_scan_catches_nul_bytes_unreadable_files_and_scopes_agents_excl(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, gitleaks = make_script_repo(tmp)
            env = dict(os.environ, GITLEAKS=gitleaks)
            scan = os.path.join(repo, ".kiro", "hooks", "scripts", "secret-scan.sh")
            nul_secret = b"\x00bin\nOPENAI_" + b"API_KEY" + b'="my-secret-value"\n'

            # Staged binary with NUL byte + double-quoted secret.
            write_bytes(os.path.join(repo, "blob.bin"), nul_secret)
            run_git(repo, "add", "blob.bin")
            res = subprocess.run(["bash", scan], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("pattern hit in staged blob.bin", res.stdout)

            # Staged binary under .kiro/ with NUL byte + home path.
            write_bytes(os.path.join(repo, ".kiro", "note.bin"), b"\x00\n/home/alice/secret\n")
            run_git(repo, "reset")
            run_git(repo, "add", ".kiro/note.bin")
            res = subprocess.run(["bash", scan], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("absolute home path in staged .kiro/note.bin", res.stdout)

            # --dir catches NUL byte secret and scopes agents exclusion strictly to .kiro/agents/*.
            run_git(repo, "reset")
            os.remove(os.path.join(repo, "blob.bin"))
            os.remove(os.path.join(repo, ".kiro", "note.bin"))
            scan_dir = os.path.join(repo, "scan_dir")
            write_bytes(os.path.join(scan_dir, "blob.bin"), nul_secret)
            write_text(os.path.join(scan_dir, "agents", "leak.txt"), "/home/alice/leak-top\n")
            res_dir = subprocess.run(
                ["bash", scan, "--dir", "scan_dir"], cwd=repo, env=env, capture_output=True, text=True
            )
            self.assertEqual(res_dir.returncode, 1, res_dir.stdout + res_dir.stderr)
            self.assertIn("blob.bin", res_dir.stdout)
            self.assertIn("agents/leak.txt", res_dir.stdout)

            write_text(os.path.join(repo, ".kiro", "agents", "perm.txt"), "/home/alice/allowed\n")
            write_text(os.path.join(repo, ".kiro", "sub", "agents", "leak2.txt"), "/home/alice/leak-sub\n")
            res_kiro = subprocess.run(
                ["bash", scan, "--dir", ".kiro"], cwd=repo, env=env, capture_output=True, text=True
            )
            self.assertEqual(res_kiro.returncode, 1, res_kiro.stdout + res_kiro.stderr)
            self.assertIn(".kiro/sub/agents/leak2.txt", res_kiro.stdout)
            self.assertNotIn("perm.txt", res_kiro.stdout)

            # Unreadable file in --dir must fail closed with exit 2 and never print clean.
            if os.geteuid() != 0:
                err_dir = os.path.join(repo, "err_dir")
                bad = os.path.join(err_dir, "unreadable.txt")
                write_text(bad, "x\n", 0o000)
                try:
                    res_err = subprocess.run(
                        ["bash", scan, "--dir", "err_dir"], cwd=repo, env=env, capture_output=True, text=True
                    )
                finally:
                    os.chmod(bad, 0o600)
                self.assertEqual(res_err.returncode, 2, res_err.stdout + res_err.stderr)
                self.assertNotIn("pattern scan clean", res_err.stdout)
                self.assertNotIn("no absolute home paths", res_err.stdout)

    def test_sync_github_and_push_review_reject_quoted_and_nul_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, gitleaks = make_script_repo(tmp)
            bare = os.path.join(tmp, "github.git")
            run_git(tmp, "init", "--bare", bare)
            run_git(repo, "remote", "add", "github", bare)
            run_git(repo, "push", "github", "main:refs/heads/main")

            payload = b"\x00data\nOPENAI_" + b"API_KEY" + b'="my-secret-value"\nmesh' + b"_at_" + b"0" * 24 + b"\n"
            write_bytes(os.path.join(repo, "leak.bin"), payload)
            run_git(repo, "add", "leak.bin")
            run_git(repo, "commit", "-m", "leak commit")
            run_git(repo, "branch", "wt/r1")

            env = dict(os.environ, GITLEAKS=gitleaks)
            res_sync = subprocess.run(
                ["bash", "scripts/coord/sync-github.sh"], cwd=repo, env=env, capture_output=True, text=True
            )
            self.assertEqual(res_sync.returncode, 3, res_sync.stdout + res_sync.stderr)
            self.assertIn("pattern hits", res_sync.stdout)

            res_pr = subprocess.run(
                ["bash", "scripts/coord/push-review.sh", "r1"], cwd=repo, env=env, capture_output=True, text=True
            )
            self.assertEqual(res_pr.returncode, 3, res_pr.stdout + res_pr.stderr)
            self.assertIn("pattern hits", res_pr.stdout)


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/coder-guard.sh and os.killpg test")
class TestCoderGuardAndDispatchBatch7(unittest.TestCase):
    def test_coder_guard_detects_vibe_and_non_home_git_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = make_script_repo(tmp)
            wt1 = os.path.join(tmp, "custom-wt-one")
            wt2 = os.path.join(tmp, "custom-wt-two")
            run_git(repo, "worktree", "add", "-b", "wt/one", wt1)
            run_git(repo, "worktree", "add", "-b", "wt/two", wt2)

            vibe_bin = os.path.join(tmp, "bin", "vibe")
            write_text(vibe_bin, "#!/bin/sh\nsleep 30\n", 0o755)
            guard = os.path.join(repo, "scripts", "coord", "coder-guard.sh")
            env = dict(
                os.environ,
                PATH=f"{os.path.dirname(vibe_bin)}:{os.environ.get('PATH', '')}",
                CODER_GUARD_LOCK=os.path.join(tmp, "guard.lock"),
                MAX_CODERS="2",
            )

            def _kill_group(proc: subprocess.Popen) -> None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

            def _spawn_vibe(argv: list[str], cwd: str) -> subprocess.Popen:
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.addCleanup(_kill_group, proc)
                return proc

            _spawn_vibe(["vibe", "--prompt-file", "p1.md"], wt1)
            res_same = subprocess.run(
                ["bash", "-c", f'source "{guard}"; coder_guard'],
                cwd=wt1, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_same.returncode, 3, res_same.stdout + res_same.stderr)
            self.assertIn("already active in this worktree", res_same.stdout)

            _spawn_vibe([vibe_bin, "--workdir", wt2, "--prompt-file", "p2.md"], wt2)
            res_cap = subprocess.run(
                ["bash", "-c", f'source "{guard}"; coder_guard'],
                cwd=repo, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_cap.returncode, 3, res_cap.stdout + res_cap.stderr)
            self.assertIn("2 coder worktrees active", res_cap.stdout)

    def test_dispatch_scripts_resolve_coder_guard_from_outside_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = make_script_repo(tmp)
            write_text(os.path.join(repo, "prompts", "t.md"), "task prompt\n")
            for prefix in ("agy", "vibe", "cc"):
                write_text(os.path.join(repo, "logs", "raw", f"{prefix}-r1.jsonl"), "existing\n")
            env = dict(os.environ, CODER_GUARD_LOCK=os.path.join(tmp, "guard.lock"))
            coord_dir = os.path.join(repo, "scripts", "coord")
            for script in ("dispatch-agy.sh", "dispatch-vibe.sh", "dispatch-commandcode.sh"):
                res = subprocess.run(
                    ["bash", f"./{script}", "prompts/t.md", "r1"],
                    cwd=coord_dir, env=env, capture_output=True, text=True,
                )
                self.assertEqual(res.returncode, 2, f"{script}: {res.stdout}\n{res.stderr}")
                self.assertIn("REFUSED: run name exists", res.stdout, script)
                self.assertNotIn("No such file or directory", res.stderr, script)


class TestTestSuiteFixesBatch7(unittest.TestCase):
    FIVE_FILES = [
        "tests/integration/test_blind_review.py",
        "tests/unit/test_blind_review_b34.py",
        "tests/unit/test_blind_review_unit.py",
        "tests/unit/test_m3_gatefix.py",
        "tests/unit/test_merge.py",
    ]

    def test_five_files_place_unittest_main_after_all_test_classes(self):
        for rel in self.FIVE_FILES:
            path = os.path.join(ROOT, rel)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=rel)
            main_line = None
            last_class_line = 0
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    last_class_line = max(last_class_line, node.lineno)
                elif isinstance(node, ast.If):
                    src = ast.unparse(node.test)
                    if "__main__" in src:
                        main_line = node.lineno
            self.assertIsNotNone(main_line, f"{rel}: missing __main__ block")
            self.assertGreater(main_line, last_class_line, f"{rel}: __main__ before class at line {last_class_line}")

        # Direct execution of the last TestCase in test_merge.py works only if __main__ is at EOF.
        merge_path = os.path.join(ROOT, "tests", "unit", "test_merge.py")
        with open(merge_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        last_cls = [n for n in tree.body if isinstance(n, ast.ClassDef)][-1]
        first_test = next(
            n.name for n in last_cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        )
        res = subprocess.run(
            [sys.executable, merge_path, f"{last_cls.name}.{first_test}"],
            cwd=ROOT, env=dict(os.environ, PYTHONPATH=ROOT), capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_run_tier_reports_skips_and_surfaces_import_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            dur = os.path.join(tmp, "durations.json")
            skip_dir = os.path.join(tmp, "skip_tier")
            write_text(
                os.path.join(skip_dir, "test_skip_mod.py"),
                "import unittest\nclass T(unittest.TestCase):\n"
                "    @unittest.skip('demo')\n    def test_s(self): pass\n",
            )
            env = dict(os.environ, MESH_TEST_DURATIONS=dur)
            env.pop("PYTHONPATH", None)
            res_skip = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), skip_dir],
                cwd=ROOT, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_skip.returncode, 0, res_skip.stdout + res_skip.stderr)
            self.assertIn("OK (skipped=1)", res_skip.stdout)

            err_dir = os.path.join(tmp, "err_tier")
            write_text(os.path.join(err_dir, "test_broken_mod.py"), "import nonexistent_mod_g6\n")
            res_err = subprocess.run(
                [sys.executable, os.path.join(ROOT, "tests", "run_tier.py"), err_dir],
                cwd=ROOT, env=env, capture_output=True, text=True,
            )
            self.assertEqual(res_err.returncode, 1, res_err.stdout + res_err.stderr)
            self.assertIn("errors=1", res_err.stdout)
            self.assertIn("nonexistent_mod_g6", res_err.stderr)
            self.assertNotIn("type object '_FailedTest'", res_err.stderr)


@unittest.skipUnless(sys.platform != "win32", "POSIX bash + scripts/coord/* shell-script and install test")
class TestCoordAndInstallBatch7(unittest.TestCase):
    def test_migrate_to_paseo_fail_closed_and_no_code_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = make_script_repo(tmp)
            home = os.path.join(tmp, "home")
            paseo_agent = os.path.join(home, ".paseo", "agents", "kiro", "ag_1.json")
            write_text(
                paseo_agent,
                '{"features": [{"id": "auto_accept", "value": false}]}',
            )
            write_text(os.path.join(repo, "docs", "coordination", ".session"), "ses_1\n")
            write_text(os.path.join(repo, "prompts", "kiro-coordinator-continue.md"), "go\n")
            os.makedirs(os.path.join(repo, "logs", "raw"), exist_ok=True)

            bindir = os.path.join(tmp, "bin")
            send_log = os.path.join(tmp, "send.log")
            write_text(os.path.join(bindir, "tmux"), "#!/bin/sh\nexit 1\n", 0o755)
            write_text(os.path.join(bindir, "pgrep"), "#!/bin/sh\nexit 1\n", 0o755)
            write_text(os.path.join(bindir, "sleep"), "#!/bin/sh\nexit 0\n", 0o755)
            paseo_stub = (
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  import) echo \'{"agentId": "ag_1"}\' ;;\n'
                f'  send) echo "$*" >> "{send_log}"; printf \'{{"features": null}}\' > "{paseo_agent}" ;;\n'
                '  wait|agent|archive) exit 0 ;;\n'
                "esac\n"
            )
            write_text(os.path.join(bindir, "paseo"), paseo_stub, 0o755)
            env = dict(os.environ, HOME=home, PATH=f"{bindir}:{os.environ.get('PATH', '')}")
            res = subprocess.run(
                ["bash", "scripts/coord/migrate-to-paseo.sh"],
                cwd=repo, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
            self.assertIn("STOP: auto-accept is missing", res.stdout)
            with open(send_log, encoding="utf-8") as fh:
                self.assertEqual(len(fh.read().splitlines()), 1)

            # Quotes/syntax in AID must never execute Python code.
            marker = os.path.join(tmp, "pwned")
            bad_aid = f"x'); open({marker!r}, 'w').write('x'); ('"
            paseo_inj = os.path.join(bindir, "paseo")
            write_text(
                paseo_inj,
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"if len(sys.argv) > 1 and sys.argv[1] == 'import': print(json.dumps({{'agentId': {bad_aid!r}}}))\n",
                0o755,
            )
            res2 = subprocess.run(
                ["bash", "scripts/coord/migrate-to-paseo.sh"],
                cwd=repo, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(res2.returncode, 1, res2.stdout + res2.stderr)
            self.assertFalse(os.path.exists(marker))
            self.assertIn("auto_accept check", res2.stdout)

    def test_view_kiro_live_null_fields_and_closes_handle_on_rotation(self):
        vkl = load_module("view_kiro_live", "scripts/coord/view-kiro-live.py")
        records = [
            None,
            {"data": None},
            {"data": {"update": None}},
            {"data": {"update": {"sessionUpdate": "agent_message_chunk", "content": None}}},
            {"data": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": None}}}},
            {"data": {"update": {"sessionUpdate": "tool_call", "title": None}}},
            {"data": {"update": {"sessionUpdate": "tool_call_update", "status": "completed"}}},
            {"type": "metadata", "data": None},
            {"type": "metadata", "data": {"stopReason": "end_turn"}},
        ]
        buf = "partial "
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for rec in records:
                buf = vkl.process_record(rec, buf)
        self.assertIn("TOOL ", out.getvalue())
        self.assertIn("[turn end: end_turn]", out.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "logs", "raw"))
            os.makedirs(os.path.join(tmp, "scripts", "coord"))
            f1 = os.path.join(tmp, "logs", "raw", "kiro-coord-2a.jsonl")
            f2 = os.path.join(tmp, "logs", "raw", "kiro-coord-2b.jsonl")
            write_text(f1, "")
            write_text(f2, "")
            opened = []
            real_open = open

            def track_open(path, *args, **kwargs):
                fh = real_open(path, *args, **kwargs)
                opened.append((path, fh))
                return fh

            class _StopLoop(Exception):
                pass

            seq = iter(["logs/raw/kiro-coord-2a.jsonl", "logs/raw/kiro-coord-2a.jsonl", "logs/raw/kiro-coord-2b.jsonl"])

            def fake_newest():
                try:
                    return next(seq)
                except StopIteration:
                    raise _StopLoop()

            orig_file = vkl.__file__
            try:
                vkl.__file__ = os.path.join(tmp, "scripts", "coord", "view-kiro-live.py")
                cwd = os.getcwd()
                with mock.patch.object(vkl, "newest", side_effect=fake_newest), \
                     mock.patch.object(vkl.time, "sleep"), \
                     mock.patch("builtins.open", side_effect=track_open), \
                     contextlib.redirect_stdout(io.StringIO()):
                    try:
                        with self.assertRaises(_StopLoop):
                            vkl.main()
                    finally:
                        os.chdir(cwd)
            finally:
                vkl.__file__ = orig_file
            self.assertEqual(len(opened), 2)
            self.assertTrue(opened[0][1].closed, "first log file handle was leaked on rotation")
            self.assertTrue(opened[1][1].closed, "second log file handle was leaked on exit")

    def test_agy_summary_handles_null_fields(self):
        agy = load_module("agy_summary", "scripts/coord/agy-summary.py")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "agy.jsonl")
            write_text(
                p,
                "\n".join([
                    "null",
                    '{"event": "init", "init": null}',
                    '{"event": "step_update", "step_update": null}',
                    '{"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_info": null}}',
                    '{"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE", "tool_info": {"parameters": null}}}',
                    '{"event": "result", "result": null}',
                    '{"event": "result", "result": {"response": null, "status": "ok"}}',
                ]) + "\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                agy.summarize(p)
            self.assertIn("RESULT ok", out.getvalue())

    def test_merge_sh_validates_identifiers_and_pre_merge_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = make_script_repo(tmp)
            write_text(os.path.join(repo, "shared.txt"), "base\n")
            run_git(repo, "add", "shared.txt")
            run_git(repo, "commit", "-m", "base shared")
            run_git(repo, "checkout", "-b", "wt/good")
            write_text(os.path.join(repo, "shared.txt"), "from-branch\n")
            run_git(repo, "commit", "-am", "branch change")
            sha = run_git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
            run_git(repo, "checkout", "main")
            write_text(os.path.join(repo, "docs", "coordination", "reviews", "rev1.md"), f"VERDICT: ACCEPT\n{sha}\n")
            run_git(repo, "add", "docs/coordination/reviews/rev1.md")
            run_git(repo, "commit", "-m", "review accept")

            bad_run = subprocess.run(
                ["bash", "scripts/coord/merge.sh", "../bad", "rev1", "m"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(bad_run.returncode, 2)
            self.assertIn("REFUSED: bad run name", bad_run.stdout)

            bad_rev = subprocess.run(
                ["bash", "scripts/coord/merge.sh", "good", "bad..rev", "m"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(bad_rev.returncode, 2)
            self.assertIn("REFUSED: bad review name", bad_rev.stdout)

            # Uncommitted local edit on shared.txt triggers git pre-merge refusal without MERGE_HEAD.
            write_text(os.path.join(repo, "shared.txt"), "dirty-on-main\n")
            res = subprocess.run(
                ["bash", "scripts/coord/merge.sh", "good", "rev1", "merge"],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("MERGE FAILED (pre-merge)", res.stdout)
            self.assertNotIn("no merge to abort", res.stderr)

    def test_install_sh_flags_and_foreign_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "bin")
            env = dict(os.environ, MESH_BIN_DIR=dest)
            install = os.path.join(ROOT, "scripts", "install.sh")
            link = os.path.join(dest, "mesh")

            r_help = subprocess.run(["sh", install, "--help"], env=env, capture_output=True, text=True)
            self.assertEqual(r_help.returncode, 0, r_help.stderr)
            self.assertIn("usage:", r_help.stdout)
            self.assertFalse(os.path.lexists(link))

            for bad_args in (["--bogus"], ["extra", "args"]):
                r_bad = subprocess.run(["sh", install, *bad_args], env=env, capture_output=True, text=True)
                self.assertEqual(r_bad.returncode, 2, r_bad.stderr)
                self.assertFalse(os.path.lexists(link))

            os.makedirs(dest, exist_ok=True)
            os.symlink("/tmp/foreign-mesh-target", link)
            r_foreign = subprocess.run(["sh", install], env=env, capture_output=True, text=True)
            self.assertEqual(r_foreign.returncode, 1)
            self.assertEqual(os.readlink(link), "/tmp/foreign-mesh-target")

            os.unlink(link)
            r_ok = subprocess.run(["sh", install], env=env, capture_output=True, text=True)
            self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
            self.assertEqual(os.readlink(link), os.path.join(ROOT, "bin", "mesh"))

            r_un = subprocess.run(["sh", install, "--uninstall"], env=env, capture_output=True, text=True)
            self.assertEqual(r_un.returncode, 0, r_un.stderr)
            self.assertFalse(os.path.lexists(link))

    def test_affected_tests_subdir_aliases_and_integration_hint(self):
        script = os.path.join(ROOT, ".kiro", "hooks", "scripts", "affected-tests.sh")

        r_sub = subprocess.run(
            ["bash", script, "auth.py"],
            cwd=os.path.join(ROOT, "mesh", "core"),
            capture_output=True, text=True,
        )
        self.assertEqual(r_sub.returncode, 0, r_sub.stdout + r_sub.stderr)
        self.assertIn("tests.unit.test_auth", r_sub.stdout)

        r_hook = subprocess.run(
            ["bash", script],
            cwd=os.path.join(ROOT, "mesh", "daemon"),
            input='{"tool_input": {"path": "terminate.py"}}',
            capture_output=True, text=True,
        )
        self.assertEqual(r_hook.returncode, 0, r_hook.stdout + r_hook.stderr)
        self.assertIn("tests.unit.test_termination_units", r_hook.stdout)

        r_alias = subprocess.run(
            ["bash", script, "mesh/core/authz.py", "mesh/daemon/handlers/task.py"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(r_alias.returncode, 0, r_alias.stdout + r_alias.stderr)
        self.assertIn("tests.unit.test_leases", r_alias.stdout)
        self.assertIn("tests.unit.test_projects", r_alias.stdout)

        r_int = subprocess.run(
            ["bash", script, "tests/integration/test_pr39_r1.py"],
            cwd=ROOT, input="", capture_output=True, text=True,
        )
        self.assertEqual(r_int.returncode, 0, r_int.stdout + r_int.stderr)
        self.assertIn("python3 -m unittest tests/integration/test_pr39_r1.py", r_int.stdout)


if __name__ == "__main__":
    unittest.main()
