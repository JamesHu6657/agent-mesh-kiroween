"""Sakana review of public PR #4, core half, unit level.

P1-2: one shared secret pattern with a generic *_API_KEY rule. P2-3: the keyed `task start`
replay rule. P3-1: sshd bystander rule for socket activation and privsep/auth names.
Uncertain item: pre-upgrade runs with a plain stored session still match; redacted ones
get the pre_upgrade hint (against a real meta table, no digest row).
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import leases
from mesh.daemon import server, terminate
from mesh.errors import MeshError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PATTERN_FILE = os.path.join(ROOT, "scripts", "coord", "secret-pattern.sh")
SCANNERS = [os.path.join(ROOT, p) for p in (
    "scripts/coord/sync-github.sh", "scripts/coord/push-review.sh", ".kiro/hooks/scripts/secret-scan.sh")]


def _grep(lines):
    """Lines the shared pattern flags (grep -E, as the scanners use it)."""
    script = '. "$1"; grep -E "$P" || true'
    out = subprocess.run(["bash", "-c", script, "x", PATTERN_FILE], input="\n".join(lines) + "\n",
                         capture_output=True, text=True, check=True).stdout
    return [l for l in out.splitlines() if l]


class TestGenericApiKeyPattern(unittest.TestCase):
    # Built at runtime so this file itself never matches the pattern.
    EQ = "="

    def kv(self, name, value="v4lue-0123456789"):
        return name + self.EQ + value

    def test_any_api_key_name_is_flagged(self):
        hits = _grep([self.kv("AGENT37_API_KEY"), self.kv("HOSTA_API_KEY"), self.kv("FOO_API_KEY"),
                      self.kv("MWAPI_API_KEY"), self.kv("API_KEY"), "export " + self.kv("OPENAI_API_KEY")])
        self.assertEqual(len(hits), 6, hits)

    def test_non_secrets_not_flagged(self):
        self.assertEqual(_grep([self.kv("API_KEY_NAME"), self.kv("FOO_API_KEY", "$(cat"),
                                self.kv("FOO_API_KEY", '"$(cat ~/k)"'), "FOO_API_KEY is set",
                                self.kv("FOO_API_KEY", "abc")]), [])

    def test_other_rules_kept(self):
        self.assertEqual(len(_grep([self.kv("BRIGHTDATA_ZONE_PASSWORD"), self.kv("HERMES_TELEGRAM_BOT_TOKEN"),
                                    self.kv("WECHAT_ROUTINE_WEBHOOK_KEY"), "ghp_" + "a" * 36])), 4)

    def test_scanners_share_the_pattern_and_files_do_not_match_themselves(self):
        for path in SCANNERS:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("scripts/coord/secret-pattern.sh", text, path)
            self.assertNotRegex(text, r"(?m)^P='", f"{path} still has its own pattern copy")
        with open(PATTERN_FILE, encoding="utf-8") as f:
            self.assertEqual(_grep(f.read().splitlines()), [])
        for path in SCANNERS:
            with open(path, encoding="utf-8") as f:
                self.assertEqual(_grep(f.read().splitlines()), [], path)


class TestStartReplayRule(unittest.TestCase):
    KEY = "start-key-1"

    def _hash(self, args):
        return hashlib.sha256(server.canonical_json({"cmd": "task.start", "args": args})).hexdigest()

    def _ok(self, stored_args, new_args, stored_session=None, cmd="task.start"):
        stored = {"ok": True, "data": {"run_id": "run_1", "session": stored_session or stored_args.get("session")}}
        return server._start_replay_ok(cmd, new_args, None, self.KEY, self._hash(stored_args), json.dumps(stored))

    def test_derived_default_retry_matches_explicit_first_call(self):
        derived = leases.session_for_idempotency_key(self.KEY)
        self.assertTrue(self._ok({"task_id": "t", "session": "ses_x"}, {"task_id": "t", "session": derived}))

    def test_cli_and_core_derive_the_same_session(self):
        from mesh.cli.commands.task import session_for_key
        self.assertEqual(session_for_key(self.KEY), leases.session_for_idempotency_key(self.KEY))

    def test_explicit_other_session_does_not_match(self):
        self.assertFalse(self._ok({"task_id": "t", "session": "ses_x"}, {"task_id": "t", "session": "ses_y"}))

    def test_other_argument_change_does_not_match(self):
        derived = leases.session_for_idempotency_key(self.KEY)
        self.assertFalse(self._ok({"task_id": "t", "session": "ses_x"}, {"task_id": "u", "session": derived}))

    def test_other_commands_and_error_or_redacted_responses_do_not_match(self):
        derived = leases.session_for_idempotency_key(self.KEY)
        self.assertFalse(self._ok({"task_id": "t", "session": "ses_x"}, {"task_id": "t", "session": derived},
                                  cmd="task.heartbeat"))
        err = json.dumps({"ok": False, "error": {"code": "X"}})
        self.assertFalse(server._start_replay_ok("task.start", {"task_id": "t", "session": derived}, None, self.KEY,
                                                 self._hash({"task_id": "t", "session": "ses_x"}), err))
        self.assertFalse(self._ok({"task_id": "t", "session": "w-secret=alpha"}, {"task_id": "t", "session": derived},
                                  stored_session="w-[REDACTED]"))
        self.assertFalse(server._start_replay_ok("task.start", {"task_id": "t", "session": derived}, None, None,
                                                 self._hash({"task_id": "t", "session": "ses_x"}), "{}"))


class TestSshdVariants(unittest.TestCase):
    """candidate (us, young) -> parent (root) -> grandparent (listener, or PID 1)."""

    def _check(self, comms, gp=500, ruid=None, exe_bad=(), gp_start=10):
        me = os.geteuid()
        procs = {1: (0, 1), 500: (1, gp_start), 900: (gp, 2000), 901: (900, 2001)}
        ruid = {1: 0, 500: 0, 900: 0, 901: me} if ruid is None else ruid
        if gp == 1:
            procs[1] = (0, gp_start)

        def real_uid(p):
            if p not in ruid:
                raise FileNotFoundError(p)
            return ruid[p]

        def stat(p):
            v = procs.get(int(p))
            return None if v is None else {"ppid": v[0], "starttime": v[1], "state": "S"}

        real_stat = os.stat

        def ostat(path, *a, **k):
            if isinstance(path, str) and path.startswith("/proc/"):
                return mock.Mock(st_uid=0 if int(path.split("/")[2]) != 901 else me)
            return real_stat(path, *a, **k)

        with mock.patch.object(terminate, "get_proc_stat", stat), mock.patch("os.stat", ostat), \
                mock.patch.object(terminate, "_comm", lambda p: comms.get(p, "")), \
                mock.patch.object(terminate, "_real_uid", real_uid), \
                mock.patch.object(terminate, "_exe_contradicts_sshd", lambda p: p in exe_bad):
            return terminate._foreign_spawned_bystander(stat(901), 1000, 901)

    # (a) systemd socket activation: no listener, the root per-connection sshd's parent is PID 1.
    def test_socket_activated_chain_is_bystander(self):
        for parent in ("sshd", "sshd-session", "sshd: james [priv]"):
            self.assertTrue(self._check({1: "systemd", 900: parent, 901: "sshd-session"}, gp=1), parent)

    def test_socket_activation_needs_systemd_root_pid1(self):
        self.assertFalse(self._check({1: "init", 900: "sshd-session", 901: "sshd-session"}, gp=1))
        self.assertFalse(self._check({1: "systemd", 900: "sshd-session", 901: "sshd-session"}, gp=1,
                                     ruid={1: os.geteuid(), 900: 0, 901: os.geteuid()}))
        self.assertFalse(self._check({1: "systemd", 900: "sshd-session", 901: "sshd-session"}, gp=1,
                                     ruid={900: 0, 901: os.geteuid()}))  # PID 1 status unreadable

    def test_socket_activation_parent_still_must_be_root_sshd(self):
        me = os.geteuid()
        self.assertFalse(self._check({1: "systemd", 900: "python3", 901: "sshd-session"}, gp=1))
        self.assertFalse(self._check({1: "systemd", 900: "sshd-session", 901: "sshd-session"}, gp=1,
                                     ruid={1: 0, 900: me, 901: me}))
        self.assertFalse(self._check({1: "systemd", 900: "sshd-session", 901: "sshd-session"}, gp=1,
                                     exe_bad={900}))
        self.assertFalse(self._check({1: "systemd", 900: "sshd-session", 901: "python3"}, gp=1))

    # (b) privsep / pam / OpenSSH 10 names.
    def test_privsep_and_auth_names(self):
        for parent, cand in (("sshd: james [priv]", "sshd: james@notty"), ("sshd-session", "sshd-auth"),
                             ("sshd-auth", "sshd-session"), ("sshd", "sshd")):
            self.assertTrue(self._check({500: "sshd", 900: parent, 901: cand}), (parent, cand))

    def test_lookalike_names_are_not_sshd(self):
        for bad in ("sshdx", "sshd-evil", "sshd:", "xsshd"):
            self.assertFalse(self._check({500: "sshd", 900: bad, 901: "sshd-session"}), bad)
            self.assertFalse(self._check({500: "sshd", 900: "sshd-session", 901: bad}), bad)
            self.assertFalse(self._check({500: bad, 900: "sshd-session", 901: "sshd-session"}), bad)

    def test_young_listener_still_refused(self):
        self.assertFalse(self._check({500: "sshd", 900: "sshd-session", 901: "sshd-session"}, gp_start=5000))

    def test_exe_name_list_includes_sshd_auth(self):
        self.assertIn("sshd-auth", terminate._SSHD_NAMES)
        with mock.patch("os.readlink", return_value="/usr/lib/openssh/sshd-auth"):
            self.assertFalse(terminate._exe_contradicts_sshd(4242))
        with mock.patch("os.readlink", return_value="/usr/lib/openssh/sshd-auth (deleted)"):
            self.assertTrue(terminate._exe_contradicts_sshd(4242))


class TestPreUpgradeRunsRealMeta(unittest.TestCase):
    """check_run_session against a real meta table with no digest row (pre-upgrade run)."""

    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        return conn

    def test_plain_stored_session_matches_and_others_refused(self):
        conn = self._conn()
        started = {"ts": "t0", "actor": "w", "request_id": "r", "session": "ses_plain"}
        with mock.patch.object(leases, "_run_event", return_value=started):
            leases.check_run_session(conn, "tsk_a", "run_a", "ses_plain")
            with self.assertRaises(MeshError) as cm:
                leases.check_run_session(conn, "tsk_a", "run_a", "ses_other")
            self.assertEqual(cm.exception.details["reason"], "session_mismatch")
            self.assertNotIn("pre_upgrade", cm.exception.details)

    def test_redacted_stored_session_gives_pre_upgrade_hint(self):
        conn = self._conn()
        started = {"ts": "t0", "actor": "w", "request_id": "r", "session": "worker-[REDACTED]"}
        for presented in (None, "worker-secret=alpha", "worker-[REDACTED]"):
            with mock.patch.object(leases, "_run_event", return_value=started):
                with self.assertRaises(MeshError) as cm:
                    leases.check_run_session(conn, "tsk_a", "run_a", presented)
            self.assertTrue(cm.exception.details["pre_upgrade"], presented)
            self.assertEqual(cm.exception.code, c.FENCE_MISMATCH)

    def test_upgraded_run_uses_digest(self):
        conn = self._conn()
        leases.remember_run_session(conn, "run_a", "worker-secret=alpha")
        started = {"ts": "t0", "actor": "w", "request_id": "r", "session": "worker-[REDACTED]"}
        with mock.patch.object(leases, "_run_event", return_value=started):
            leases.check_run_session(conn, "tsk_a", "run_a", "worker-secret=alpha")


if __name__ == "__main__":
    unittest.main()
