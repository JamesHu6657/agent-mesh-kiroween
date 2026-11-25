"""Checks for the inbox-worker onboarding kit: docs/inbox-workers.md and
scripts/inbox-worker-example.sh.

Offline only. The example script is syntax-checked with `bash -n`, every `mesh <resource>
<verb>` it (and the guide) uses is checked against the real CLI parser, and its control flow
(submit on success, fail on a non-zero exit, no submit when the lease is lost) is run against a
stub `mesh` executable that returns canned JSON. No meshd, network or real token is involved.
"""

import sys
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest

from mesh.cli.main import build_parser

# The worker spawns many short-lived processes (sleep/ps/subshells) in its own session; a
# concurrent real-/proc scanner can catch one mid-exec (transient EACCES on environ) and
# report an uncertain leftover scan. Hold the host-wide /proc lock exclusively.
MESH_PROC_LOCK = "exclusive"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "inbox-worker-example.sh")
GUIDE = os.path.join(ROOT, "docs", "inbox-workers.md")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def real_commands():
    """Return {resource: set(verbs)} from the real argparse tree (no daemon needed)."""
    parser = build_parser()
    out = {}
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict) or not choices:
            continue
        for resource, sub in choices.items():
            verbs = set()
            for sub_action in sub._actions:
                sub_choices = getattr(sub_action, "choices", None)
                if isinstance(sub_choices, dict) and sub_choices:
                    verbs.update(sub_choices)
            out[resource] = verbs
        break
    return out


STUB = r"""#!/bin/sh
# Stub mesh: canned JSON, records every call (never sees a real token).
D="$STUB_DIR"
echo "$*" >> "$D/calls.log"
case "$1 $2" in
  "inbox wait")
    if [ -f "$D/wait-rc" ]; then
      echo '{"ok": false, "error": {"code": "TOKEN_REVOKED"}}'; exit "$(cat "$D/wait-rc")"
    fi
    if [ -f "$D/notices-first" ] && ! grep -q "inbox ack msg_r9" "$D/calls.log"; then
      i=0; items=""
      while [ $i -lt 10 ]; do
        items="$items{\"id\": \"msg_r$i\", \"type\": \"task.rejected\", \"payload\": {}},"; i=$((i+1))
      done
      echo "{\"ok\": true, \"data\": {\"items\": [${items%,}]}}"; exit 0
    fi
    if [ -f "$D/no-offer" ]; then
      echo '{"ok": true, "data": {"items": []}}'; exit 8
    fi
    echo '{"ok": true, "data": {"items": [{"id": "msg_1", "type": "task.offered", "payload": {"task_id": "tsk_1"}}]}}' ;;
  "inbox ack") echo '{"ok": true, "data": {"acked": 1}}' ;;
  "task start")
    hb_every=1
    [ -f "$D/hb-every" ] && hb_every="$(cat "$D/hb-every")"
    printf '{"ok": true, "data": {"run_id": "run_1", "fence": 3, "workspace": "%s", "branch": "mesh/p/t", "session": "ses_x", "heartbeat_every_s": %s}}\n' "$D/ws" "$hb_every" ;;
  "task show") echo '{"ok": true, "data": {"spec": "do the thing"}}' ;;
  "task heartbeat"|"task submit"|"task fail")
    if [ "${MESH_SESSION:-}" != "ses_x" ]; then
      echo '{"ok": false, "error": {"code": "FENCE_MISMATCH", "details": {"reason": "session_mismatch"}}}'
      exit 9
    fi
    case "$1 $2" in
      "task heartbeat")
        if [ -f "$D/hb-fail-once" ] && [ ! -f "$D/hb-failed" ]; then
          : > "$D/hb-failed"; echo '{"ok": false, "error": {"code": "DAEMON_UNAVAILABLE"}}'; exit 10
        fi
        if [ -f "$D/lose" ]; then echo '{"ok": false, "error": {"code": "LEASE_EXPIRED"}}'; exit 9; fi
        echo '{"ok": true, "data": {}}' ;;
      *) echo '{"ok": true, "data": {}}' ;;
    esac ;;
  *) echo '{"ok": false, "error": {"code": "INVALID_ARGUMENT"}}'; exit 2 ;;
esac
"""


@unittest.skipUnless(sys.platform != "win32", "POSIX bash scripts/coord/inbox-worker.sh static analysis and UID test")
class InboxWorkerKitStaticTest(unittest.TestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(SCRIPT))
        self.assertTrue(os.stat(SCRIPT).st_mode & stat.S_IXUSR, "script must be executable")

    def test_bash_syntax(self):
        result = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_uses_only_real_mesh_subcommands(self):
        commands = real_commands()
        self.assertIn("task", commands)
        used = set(re.findall(r"\bmesh_cmd\s+([a-z]+)\s+([a-z]+)", read("scripts/inbox-worker-example.sh")))
        self.assertTrue(used, "script should call mesh through mesh_cmd")
        for resource, verb in sorted(used):
            self.assertIn(resource, commands, f"unknown mesh resource {resource!r}")
            self.assertIn(verb, commands[resource], f"unknown mesh command: mesh {resource} {verb}")
        # The protocol steps the task asks for.
        for step in [("inbox", "wait"), ("inbox", "ack"), ("task", "start"), ("task", "heartbeat"),
                     ("task", "submit"), ("task", "fail")]:
            self.assertIn(step, used)

    def test_guide_uses_only_real_mesh_subcommands(self):
        commands = real_commands()
        text = read("docs/inbox-workers.md")
        found = set(re.findall(r"`?\bmesh ([a-z]+) ([a-z][a-z-]*)", text))
        self.assertTrue(found)
        for resource, verb in sorted(found):
            if resource not in commands:
                continue  # prose such as "mesh runs"
            if not commands[resource]:
                continue
            self.assertIn(verb, commands[resource], f"guide mentions unknown command: mesh {resource} {verb}")

    def test_token_is_never_read_or_printed(self):
        text = read("scripts/inbox-worker-example.sh")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn('--token-file "$MESH_TOKEN_FILE"', code)
        self.assertNotRegex(code, r"(cat|head|tail|less|echo|printf|source|\.)\s+[\"']?\$\{?MESH_TOKEN_FILE")
        self.assertNotRegex(code, r"<\s*[\"']?\$\{?MESH_TOKEN_FILE")
        self.assertNotIn("set -x", code)
        self.assertNotRegex(code, r"MESH_TOKEN\b")

    def test_script_and_guide_are_linked(self):
        for rel, needles in [
            ("README.md", ["docs/inbox-workers.md", "scripts/inbox-worker-example.sh"]),
            ("docs/examples/README.md", ["../inbox-workers.md", "../../scripts/inbox-worker-example.sh"]),
        ]:
            text = read(rel)
            for needle in needles:
                self.assertIn(needle, text, f"{rel} must link {needle}")
                target = os.path.normpath(os.path.join(os.path.dirname(os.path.join(ROOT, rel)), needle))
                self.assertTrue(os.path.exists(target), f"{rel}: broken link {needle}")

    def test_guide_covers_required_topics(self):
        text = read("docs/inbox-workers.md").lower()
        for topic in ["0600", "inbox wait", "inbox list", "task show", "task start", "run_id", "fence",
                      "workspace", "heartbeat", "keepalive", "progress", "commit", "task submit",
                      "deliverable", "task fail", "rework", "lease_expired", "mesh_session"]:
            self.assertIn(topic, text, f"guide should mention {topic!r}")


@unittest.skipUnless(sys.platform != "win32", "POSIX bash scripts/coord/inbox-worker.sh runtime test")
class InboxWorkerScriptRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        os.makedirs(os.path.join(self.dir, "ws"))
        self.stub = os.path.join(self.dir, "mesh")
        with open(self.stub, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        os.chmod(self.stub, 0o755)
        self.token = os.path.join(self.dir, "worker.token")
        with open(self.token, "w", encoding="utf-8") as handle:
            handle.write("not-a-real-token-xyzzy\n")
        os.chmod(self.token, 0o600)
        self.env = dict(os.environ, MESH_BIN=self.stub, MESH_TOKEN_FILE=self.token, STUB_DIR=self.dir,
                        WAIT_TIMEOUT="1s", WAIT_ROUNDS="1")

    def run_script(self, *command, timeout=30, env=None):
        return subprocess.run(["bash", SCRIPT, "--", *command], capture_output=True, text=True,
                              env=env or self.env, timeout=timeout)

    def calls(self):
        path = os.path.join(self.dir, "calls.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()

    def test_success_submits(self):
        result = self.run_script("sh", "-c", 'echo "$MESH_TASK_ID $MESH_RUN_ID $MESH_FENCE" > out.txt')
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.dir, "ws", "out.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "tsk_1 run_1 3")  # ran in the workspace
        calls = self.calls()
        self.assertTrue(any(c.startswith("task submit tsk_1 --run run_1 --fence 3") for c in calls), calls)
        self.assertTrue(any(c.startswith("inbox ack msg_1") for c in calls), calls)
        self.assertFalse(any(c.startswith("task fail") for c in calls), calls)
        self.assertNotIn("not-a-real-token-xyzzy", result.stdout + result.stderr)

    def test_nonzero_exit_fails_the_task(self):
        result = self.run_script("sh", "-c", "exit 7")
        self.assertEqual(result.returncode, 1, result.stderr)
        calls = self.calls()
        self.assertTrue(any(c.startswith("task fail tsk_1 --run run_1 --fence 3 --reason") for c in calls), calls)
        self.assertFalse(any(c.startswith("task submit") for c in calls), calls)

    def test_heartbeats_are_sent_while_the_command_runs(self):
        result = self.run_script("sleep", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        beats = [c for c in self.calls() if c.startswith("task heartbeat tsk_1 --run run_1 --fence 3")]
        self.assertGreaterEqual(len(beats), 1, self.calls())

    def test_lost_lease_stops_the_command_and_skips_submit(self):
        open(os.path.join(self.dir, "lose"), "w").close()
        result = self.run_script("sleep", "20", timeout=15)
        self.assertEqual(result.returncode, 9, result.stderr)
        calls = self.calls()
        self.assertFalse(any(c.startswith(("task submit", "task fail")) for c in calls), calls)

    def test_no_offer_exits_3(self):
        open(os.path.join(self.dir, "no-offer"), "w").close()
        result = self.run_script("true")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse(any(c.startswith("task start") for c in self.calls()))

    def test_insecure_token_file_is_refused(self):
        os.chmod(self.token, 0o644)
        result = self.run_script("true")
        self.assertEqual(result.returncode, 2)
        self.assertIn("0600", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_missing_command_or_token_is_a_usage_error(self):
        result = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 2)
        env = dict(self.env)
        env.pop("MESH_TOKEN_FILE")
        result = self.run_script("true", env=env)
        self.assertEqual(result.returncode, 2)


    # PR #37 review N3-N6

    def test_lost_lease_stops_the_whole_command_tree(self):
        # N3: a descendant of the command must not keep writing after the lease is lost.
        open(os.path.join(self.dir, "lose"), "w").close()
        marker = os.path.join(self.dir, "ws", "late-marker")
        child = f"import time; time.sleep(3); open({marker!r}, 'w').write('x')"
        result = self.run_script("python3", "-c",
                                 f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(20)",
                                 timeout=20)
        self.assertEqual(result.returncode, 9, result.stderr)
        import time
        time.sleep(4)
        self.assertFalse(os.path.exists(marker), "descendant survived the lost lease")

    def test_transient_heartbeat_error_is_retried(self):
        # N4: one exit-10 heartbeat must not end the renewals.
        open(os.path.join(self.dir, "hb-fail-once"), "w").close()
        result = self.run_script("sleep", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        beats = [c for c in self.calls() if c.startswith("task heartbeat")]
        self.assertGreaterEqual(len(beats), 2, self.calls())
        self.assertIn("heartbeat failed (exit 10)", result.stderr)

    def test_non_offer_messages_are_acked_and_do_not_starve(self):
        # N5: ten older notices before the offer.
        open(os.path.join(self.dir, "notices-first"), "w").close()
        env = dict(self.env, WAIT_ROUNDS="3")
        result = self.run_script("true", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        for i in range(10):
            self.assertIn(f"inbox ack msg_r{i}", " ".join(calls))
        self.assertTrue(any(c.startswith("task submit tsk_1") for c in calls), calls)

    def test_inbox_errors_are_not_an_empty_inbox(self):
        # N6: a revoked token surfaces its exit code instead of "no task offered".
        with open(os.path.join(self.dir, "wait-rc"), "w") as handle:
            handle.write("3")
        env = dict(self.env, WAIT_ROUNDS="0")
        result = self.run_script("true", env=env, timeout=15)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("inbox wait failed (exit 3)", result.stderr)
        self.assertIn("TOKEN_REVOKED", result.stderr)
        self.assertEqual(len([c for c in self.calls() if c.startswith("inbox wait")]), 1)

    def test_stub_enforces_mesh_session_and_worker_sets_it(self):
        # Batch 7 G6 (Finding 4): stub must enforce MESH_SESSION on heartbeat/submit/fail.
        for verb in ("heartbeat", "submit", "fail"):
            bad = subprocess.run(
                [self.stub, "task", verb, "tsk_1"],
                capture_output=True, text=True,
                env=dict(self.env, MESH_SESSION="wrong_ses"),
            )
            self.assertEqual(bad.returncode, 9, f"{verb}: {bad.stdout}")
            self.assertIn("FENCE_MISMATCH", bad.stdout)
        res_fail = self.run_script("sh", "-c", "exit 7")
        self.assertEqual(res_fail.returncode, 1, res_fail.stderr)
        self.assertNotIn("FENCE_MISMATCH", res_fail.stderr)

    def test_daemon_unavailable_preserves_exit_10_and_11(self):
        import time
        for rc in ("10", "11"):
            with open(os.path.join(self.dir, "wait-rc"), "w", encoding="utf-8") as handle:
                handle.write(rc)
            t0 = time.monotonic()
            result = self.run_script("true", timeout=10)
            elapsed = time.monotonic() - t0
            self.assertEqual(result.returncode, int(rc), result.stderr)
            self.assertNotIn("no task offered", result.stderr)
            self.assertLess(elapsed, 4.0, result.stderr)

    def test_child_env_strips_token_and_session(self):
        env = dict(self.env, MESH_TOKEN="tok_dummy_env_value")
        env["OUT_JSON"] = os.path.join(self.dir, "child-env.json")
        py = (
            "import json, os; "
            "open(os.environ['OUT_JSON'], 'w').write(json.dumps("
            "{k: os.environ.get(k) for k in ('MESH_TOKEN_FILE', 'MESH_SESSION', 'MESH_TOKEN', "
            "'MESH_TASK_ID', 'MESH_RUN_ID', 'MESH_FENCE')}))"
        )
        result = self.run_script("python3", "-c", py, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(env["OUT_JSON"], encoding="utf-8") as handle:
            got = json.load(handle)
        self.assertIsNone(got["MESH_TOKEN_FILE"])
        self.assertIsNone(got["MESH_SESSION"])
        self.assertIsNone(got["MESH_TOKEN"])
        self.assertEqual(got["MESH_TASK_ID"], "tsk_1")
        self.assertEqual(got["MESH_RUN_ID"], "run_1")
        self.assertEqual(got["MESH_FENCE"], "3")

    def test_heartbeat_sleep_is_reaped_and_pipes_close_promptly(self):
        with open(os.path.join(self.dir, "hb-every"), "w", encoding="utf-8") as handle:
            handle.write("40")
        result = self.run_script("true", timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_early_usr1_and_sigterm_ignoring_child_cleanup(self):
        isolated_tmp = os.path.join(self.dir, "worker-tmp")
        os.makedirs(isolated_tmp)
        open(os.path.join(self.dir, "lose"), "w").close()
        with open(os.path.join(self.dir, "hb-every"), "w", encoding="utf-8") as handle:
            handle.write("0")
        env = dict(self.env, TMPDIR=isolated_tmp, KILL_GRACE_S="1")
        py = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"
        result = self.run_script("python3", "-c", py, env=env, timeout=10)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(os.listdir(isolated_tmp), [], "temp dir leaked after USR1/cleanup")

    def test_sigterm_kills_process_group_and_cleans_up(self):
        import signal, time
        isolated_tmp = os.path.join(self.dir, "worker-tmp-term")
        os.makedirs(isolated_tmp)
        ready = os.path.join(self.dir, "ws", "ready")
        late = os.path.join(self.dir, "ws", "late")
        child = f"import time; time.sleep(1.5); open({late!r}, 'w').write('x')"
        parent = (
            f"import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            f"open({ready!r}, 'w').write('ok'); "
            f"time.sleep(20)"
        )
        env = dict(self.env, TMPDIR=isolated_tmp, KILL_GRACE_S="1")
        proc = subprocess.Popen(
            ["bash", SCRIPT, "--", "python3", "-c", parent],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not os.path.exists(ready):
                time.sleep(0.05)
            self.assertTrue(os.path.exists(ready), "child never started")
            time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=10)
        except Exception:
            proc.kill()
            proc.communicate()
            raise
        self.assertEqual(proc.returncode, 143, err)
        self.assertEqual(os.listdir(isolated_tmp), [], "temp dir leaked after SIGTERM")
        time.sleep(1.8)
        self.assertFalse(os.path.exists(late), "grandchild survived SIGTERM on PGID")


if __name__ == "__main__":
    unittest.main()
