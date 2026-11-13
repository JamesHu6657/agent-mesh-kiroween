"""Remote-worker and onboarding friction (no daemon needed): keepalive/onboard parsing,
deliverable and config-set messages, ack-window helpers, roster fields, cheat-sheet."""

import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import task as task_cmd
from mesh.cli.commands.agent import worker_cheat_sheet
from mesh.cli.main import build_parser, main
from mesh.core import settings as settings_mod
from mesh.daemon.handlers.task import _lease_timing, heartbeat_every_s
from mesh.daemon.scheduler import no_response_details
from mesh.errors import MeshError


def _run_main(*argv: str):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(list(argv))
    return rc, out.getvalue()


class TestLeaseTiming(unittest.TestCase):
    def test_heartbeat_every_is_a_third_of_ttl(self) -> None:
        self.assertEqual(heartbeat_every_s(120), 40)
        self.assertEqual(heartbeat_every_s(10), 3)
        self.assertEqual(heartbeat_every_s(2), 1)  # never 0

    def test_inbox_runs_have_no_enforced_deadline(self) -> None:
        info = _lease_timing(120, "2026-01-01T00:02:00.000Z", 3600)
        self.assertEqual(info["lease_ttl_s"], 120)
        self.assertEqual(info["heartbeat_every_s"], 40)
        self.assertEqual(info["timeout_s"], 3600)
        self.assertIsNone(info["deadline_at"])
        self.assertFalse(info["deadline_enforced"])


class TestKeepaliveCli(unittest.TestCase):
    def test_parser_and_help(self) -> None:
        args = build_parser().parse_args(["task", "keepalive", "tsk_1", "--run", "run_1", "--fence", "2", "--every", "5"])
        self.assertIs(args.func, task_cmd.handle_task_keepalive)
        self.assertEqual((args.run, args.fence, args.every), ("run_1", 2, 5))
        rc, out = _run_main("help", "quickstart")
        self.assertIn("mesh task keepalive", json.loads(out)["data"]["text"])

    def test_requires_run_fence_and_positive_every(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESH_RUN_ID", None)
            os.environ.pop("MESH_FENCE", None)
            rc, out = _run_main("task", "keepalive", "tsk_1", "--fence", "1")
            self.assertEqual(rc, c.EXIT_USAGE)
            self.assertEqual(json.loads(out)["error"]["details"]["param"], "run")
            rc, out = _run_main("task", "keepalive", "tsk_1", "--run", "r", "--fence", "1", "--every", "0")
            self.assertEqual(rc, c.EXIT_USAGE)
            self.assertEqual(json.loads(out)["error"]["details"]["param"], "every")

    def _fake_client(self, beats_before_error, error, run, task):
        calls = {"heartbeat": 0}

        def execute_cmd(args, cmd, payload=None, **_):
            if args.idempotency_key is not None:
                raise AssertionError("keepalive must not replay with an idempotency key")
            if cmd == "task.heartbeat":
                calls["heartbeat"] += 1
                if calls["heartbeat"] > beats_before_error:
                    raise error
                return {"data": {"expires_at": "soon"}}
            if cmd == "task.show":
                return {"data": dict(task, live_run={"lease_ttl_s": 3, "heartbeat_every_s": 1})}
            if cmd == "run.show":
                return {"data": run}
            raise AssertionError(cmd)

        client = mock.Mock()
        client.execute_cmd.side_effect = execute_cmd
        return client, calls

    def _keepalive(self, client):
        args = build_parser().parse_args(["task", "keepalive", "tsk_1", "--run", "run_1", "--fence", "1", "--every", "1"])
        args.idempotency_key = "k1"  # a user-given key is never reused per beat
        args.home = None
        with mock.patch.object(task_cmd, "MeshClient", return_value=client), \
                mock.patch("time.sleep"), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            try:
                return task_cmd.handle_task_keepalive(args), None, err.getvalue()
            except MeshError as exc:
                return None, exc, err.getvalue()

    def test_ends_with_exit_0_after_submit(self) -> None:
        client, calls = self._fake_client(
            3, MeshError(c.LEASE_EXPIRED, exit_code=c.EXIT_STALE, message="No active lease"),
            run={"status": "exited", "end_reason": "submitted", "ended_at": "t"},
            task={"status": "submitted", "fence_seq": 1})
        res, exc, err = self._keepalive(client)
        self.assertIsNone(exc)
        self.assertEqual(res["ended"], "run_ended")
        self.assertEqual(res["task_status"], "submitted")
        self.assertEqual(res["beats"], 3)
        self.assertIn("heartbeat 1 ok", err)

    def test_lost_lease_is_exit_9_with_clear_message(self) -> None:
        client, _ = self._fake_client(
            1, MeshError(c.LEASE_EXPIRED, exit_code=c.EXIT_STALE, message="No active lease"),
            run={"status": "lost", "end_reason": "lease_expired", "ended_at": "t"},
            task={"status": "failed", "fence_seq": 1})
        res, exc, _ = self._keepalive(client)
        self.assertEqual(exc.code, c.LEASE_EXPIRED)
        self.assertEqual(exc.exit_code, c.EXIT_STALE)
        self.assertIn("expired", exc.message)
        self.assertIn("retry", exc.message)

    def test_newer_fence_is_exit_9_stale_fence(self) -> None:
        client, _ = self._fake_client(
            0, MeshError(c.FENCE_MISMATCH, exit_code=c.EXIT_STALE, message="Fence mismatch"),
            run={"status": "running", "end_reason": None, "ended_at": None},
            task={"status": "running", "fence_seq": 2})
        res, exc, _ = self._keepalive(client)
        self.assertEqual(exc.code, c.FENCE_MISMATCH)
        self.assertIn("current fence 2", exc.message)

    def test_transient_errors_are_retried(self) -> None:
        errors = [MeshError(c.DAEMON_UNAVAILABLE, exit_code=c.EXIT_DAEMON_UNAVAILABLE, message="down")]
        state = {"n": 0}

        def execute_cmd(args, cmd, payload=None, **_):
            if cmd == "task.heartbeat":
                state["n"] += 1
                if state["n"] == 2 and errors:
                    raise errors.pop()
                if state["n"] >= 4:
                    raise MeshError(c.LEASE_EXPIRED, exit_code=c.EXIT_STALE, message="gone")
                return {"data": {"expires_at": "x"}}
            if cmd == "run.show":
                return {"data": {"status": "exited", "end_reason": "failed", "ended_at": "t"}}
            return {"data": {"status": "failed", "fence_seq": 1}}

        client = mock.Mock()
        client.execute_cmd.side_effect = execute_cmd
        res, exc, err = self._keepalive(client)
        self.assertIsNone(exc)
        self.assertIn("retrying", err)
        self.assertEqual(res["end_reason"], "failed")


class TestDeliverableMessages(unittest.TestCase):
    def _submit(self, value):
        return _run_main("task", "submit", "tsk_1", "--run", "run_1", "--fence", "1", "--deliverable", value)

    def test_missing_file_is_named(self) -> None:
        rc, out = self._submit("/tmp/definitely-missing-deliverable.json")
        self.assertEqual(rc, c.EXIT_USAGE)
        err = json.loads(out)["error"]
        self.assertIn("file not found: /tmp/definitely-missing-deliverable.json (and not valid inline JSON)", err["message"])
        self.assertEqual(err["details"]["reason"], "file_not_found")

    def test_bad_inline_json_says_inline(self) -> None:
        rc, out = self._submit('{"a": ')
        self.assertEqual(rc, c.EXIT_USAGE)
        self.assertIn("inline value", json.loads(out)["error"]["message"])

    def test_bad_json_file_says_file(self) -> None:
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "d.json")
            with open(p, "w") as f:
                f.write("not json")
            rc, out = self._submit(p)
            self.assertIn(f"file {p}", json.loads(out)["error"]["message"])
        finally:
            shutil.rmtree(d)


class TestConfigSetValue(unittest.TestCase):
    def test_non_json_for_int_setting_shows_quoted_form(self) -> None:
        rc, out = _run_main("config", "set", "task.default_timeout_s", "ten")
        self.assertEqual(rc, c.EXIT_USAGE)
        err = json.loads(out)["error"]
        self.assertEqual(err["code"], c.INVALID_SETTING)
        self.assertIn("not valid JSON", err["message"])
        self.assertIn("'\"ten\"'", err["message"])
        self.assertEqual(err["details"]["quoted_form"], '"ten"')

    def test_string_settings_take_bare_words(self) -> None:
        sent = {}

        def execute_cmd(args, cmd, payload=None, **_):
            sent.update(payload)
            return {"data": payload}

        from mesh.cli.commands import config as config_cmd
        client = mock.Mock()
        client.execute_cmd.side_effect = execute_cmd
        with mock.patch.object(config_cmd, "MeshClient", return_value=client):
            for key, raw, expect in (("lead.default_agent", "lead1", "lead1"),
                                     ("workspaces.root", "/srv/ws", "/srv/ws"),
                                     ("approval.policy.deploy", "direct_only", "direct_only"),
                                     ("lead.default_agent", "null", None),          # JSON keeps its meaning
                                     ("lead.default_agent", '"lead1"', "lead1")):
                with self.subTest(key=key, raw=raw):
                    rc, out = _run_main("config", "set", key, raw)
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(sent["value"], expect)
        self.assertTrue(settings_mod.string_setting("approval.policy.spend_money"))
        self.assertFalse(settings_mod.string_setting("notify.command"))


class TestAckTimeoutHelpers(unittest.TestCase):
    def test_validate_ack_timeout(self) -> None:
        self.assertEqual(settings_mod.validate_ack_timeout("2h"), 7200)
        self.assertEqual(settings_mod.validate_ack_timeout(900), 900)
        for bad in ("soon", "0", "-5", "30d", True):
            with self.subTest(bad=bad), self.assertRaises(MeshError) as cm:
                settings_mod.validate_ack_timeout(bad)
            self.assertEqual(cm.exception.details["param"], "ack_timeout")

    def test_store_and_clear(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
        self.assertIsNone(settings_mod.get_agent_ack_timeout_s(conn, "agt_1"))
        settings_mod.set_agent_ack_timeout_s(conn, "agt_1", 7200, "t", "usr")
        self.assertEqual(settings_mod.get_agent_ack_timeout_s(conn, "agt_1"), 7200)
        settings_mod.set_agent_ack_timeout_s(conn, "agt_1", 60, "t", "usr")
        self.assertEqual(settings_mod.get_agent_ack_timeout_s(conn, "agt_1"), 60)
        settings_mod.set_agent_ack_timeout_s(conn, "agt_1", None, "t", "usr")
        self.assertIsNone(settings_mod.get_agent_ack_timeout_s(conn, "agt_1"))
        # Not a config key: config set refuses it.
        with self.assertRaises(MeshError):
            settings_mod.validate_setting(settings_mod.agent_ack_timeout_key("agt_1"), 60, home="/tmp")

    def test_no_response_details_name_setting_and_retry(self) -> None:
        d = no_response_details("tsk_1", "mailbot", "t0", 600, per_agent=False)
        self.assertEqual(d["setting"], "inbox.ack_timeout_s")
        self.assertEqual(d["timeout_s"], 600)
        self.assertIn("mesh task retry tsk_1", d["hint"])
        self.assertIn("mesh agent update mailbot --ack-timeout", d["hint"])
        self.assertIn("mesh config set inbox.ack_timeout_s", d["hint"])
        d2 = no_response_details("tsk_1", "mailbot", "t0", 7200, per_agent=True)
        self.assertEqual(d2["setting"], "agent mailbot --ack-timeout")
        self.assertNotIn("config set", d2["hint"])


class TestAgentCliFlags(unittest.TestCase):
    def test_create_update_onboard_flags(self) -> None:
        p = build_parser()
        a = p.parse_args(["agent", "create", "--name", "x", "--adapter", "none", "--no-intake", "--ack-timeout", "2h"])
        self.assertTrue(a.no_intake)
        self.assertEqual(a.ack_timeout, "2h")
        a = p.parse_args(["agent", "update", "x", "--ack-timeout", "default", "--no-intake"])
        self.assertEqual(a.ack_timeout, "default")
        a = p.parse_args(["agent", "onboard", "w1", "--out", "/tmp/w1.token"])
        self.assertEqual((a.adapter, a.max_concurrency, a.out), ("none", "1", "/tmp/w1.token"))

    def test_create_help_explains_capability_default(self) -> None:
        p = build_parser()
        sub = [a for a in p._actions if a.__class__.__name__ == "_SubParsersAction"][0]
        agent = sub.choices["agent"]
        create = [a for a in agent._actions if a.__class__.__name__ == "_SubParsersAction"][0].choices["create"]
        text = create.format_help()
        flat = " ".join(text.split())
        self.assertIn("Default: intake", flat)
        self.assertIn("--no-intake", flat)
        self.assertIn("replaces that default", flat)

    def test_onboard_refuses_bad_out_before_contacting_daemon(self) -> None:
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "w.token")
            with open(p, "w") as f:
                f.write("x")
            with mock.patch("mesh.cli.commands.agent.MeshClient") as client:
                rc, out = _run_main("--home", d, "agent", "onboard", "w1", "--out", p)
                client.assert_not_called()
            self.assertEqual(rc, c.EXIT_USAGE)
            self.assertIn("mesh agent onboard --out", json.loads(out)["error"]["message"])
        finally:
            shutil.rmtree(d)

    def test_cheat_sheet(self) -> None:
        text = worker_cheat_sheet("w1", "/home/u/.config/agent-mesh/w1.token", 7200)
        for needle in ("--token-file /home/u/.config/agent-mesh/w1.token", "mesh task start <tsk>",
                       "mesh task keepalive <tsk> --run <run_id> --fence <fence>", "mesh task submit",
                       "mesh task fail", "mesh inbox list", "within 7200s"):
            self.assertIn(needle, text)
        self.assertIn("--home '/a b'", worker_cheat_sheet("w1", "/t", None, home="/a b"))
        # every command line in it parses
        import shlex
        parser = build_parser()
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("mesh "):
                import re
                argv = re.sub(r"<[a-z_]+>", "X", ln.split("#")[0].replace("<fence>", "1")).split(" > ")[0]
                argv = shlex.split(argv)[1:]
                with self.subTest(line=ln):
                    parser.parse_args(argv)


if __name__ == "__main__":
    unittest.main()
