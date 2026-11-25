"""Full-codebase review r3 (fix/full-review-r1 @ 24340e2) regressions."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from mesh.core.redact import StreamRedactor, redact


def both(raw: str, exact=None) -> str:
    """Batch and stream output (they must agree)."""
    exact = list(exact or [])
    out = redact(raw, exact, jsonl=True)
    sr = StreamRedactor(exact, jsonl=True)
    streamed = "".join(sr.feed(ch) for ch in raw) + sr.flush()
    assert streamed == out, (streamed, out)
    return out


class TestR3P1_1DuplicateKeysAndKeyBlocks(unittest.TestCase):
    EXACT = [("tok", 'ab"cdefghij')]

    def test_duplicate_keys_never_release_an_earlier_secret(self) -> None:
        for raw, secret in (
            ('{"a": "sk-1234567890abcdef1234", "a": "ok"}', "sk-1234567890abcdef1234"),
            ('{"tok": "mesh_rt_0123456789abcdef0123456789abcdef", "tok": "ok"}', "0123456789abcdef0123"),
            ('{"api\\u005fkey": "secret123zz", "api_key": ""}', "secret123zz"),
            ('{"env": "ab\\"cdefghij", "env": "clean"}', 'cdefghij'),
        ):
            out = both(raw + "\n", self.EXACT)
            self.assertNotIn(secret, out, raw)
            json.loads(out)
            self.assertEqual(out.count('"'), out.count('"'))  # still one JSON line
        out = both('{"a": "x", "a": "y"}\n')
        self.assertEqual(out, '{"a": "x", "a": "y"}\n')  # nothing found: byte for byte

    def test_key_block_in_a_line_keeps_the_other_fields_redacted(self) -> None:
        raw = json.dumps({"api_key": ["SUPER_SECRET_1"], "auth": "Bearer\teyJhbGciOiJIUzI1NiJ9abcdef",
                          "pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN\n-----END PRIVATE KEY-----",
                          "env": 'ab"cdefghij'}) + "\n"
        out = both(raw, self.EXACT)
        for s in ("SUPER_SECRET_1", "eyJhbGciOiJIUzI1NiJ9abcdef", "MIIEvQIBADAN", "cdefghij"):
            self.assertNotIn(s, out)
        json.loads(out)

    def test_escaped_begin_carries_the_key_block_to_the_next_lines(self) -> None:
        raw = ('{"x": "-----BEGIN\\u0020PRIVATE KEY-----\\nMIIfirst"}\n'
               "MIIBODYLINE2\n-----END PRIVATE KEY-----\n" + '{"ok": 1}\n')
        out = both(raw)
        self.assertNotIn("MIIfirst", out)
        self.assertNotIn("MIIBODYLINE2", out)
        self.assertTrue(out.endswith('{"ok": 1}\n'))


class TestR3P1_2MarkersEmbeddedJsonArming(unittest.TestCase):
    def test_forged_marker_in_a_prefixed_line(self) -> None:
        out = both('INFO payload={"api_key": "[REDACTED:real_secret_val]"}\n')
        self.assertNotIn("real_secret_val", out)

    def test_prefixed_embedded_json_in_a_string(self) -> None:
        for raw, secret in (
            (json.dumps({"out": 'Result: {"api_key": ["SECRET_IN_ARRAY"]}'}), "SECRET_IN_ARRAY"),
            (json.dumps({"out": 'Result: {"api\\u005fkey": "secret123zz"}'}), "secret123zz"),
            ('log={\\"api\\u005fkey\\": [\\"SECRETX123\\"]}', "SECRETX123"),
        ):
            out = both(raw + "\n")
            self.assertNotIn(secret, out, raw)

    def test_arming_survives_blank_strings_keys_and_numbers(self) -> None:
        for raw in ('["password:", "", "hunter22xyz"]', '{"cmd": "password:", "n": 1, "val": "hunter22xyz"}'):
            out = both(raw + "\n")
            self.assertNotIn("hunter22xyz", out, raw)
            json.loads(out)

    def test_redaction_is_idempotent(self) -> None:
        for raw in ('{"type":"test","msg":"set password=[REDACTED]"}',
                    'INFO payload={"api_key": "[REDACTED]"}',
                    '{"a": "[REDACTED]", "a": "ok"}'):
            self.assertEqual(both(raw + "\n"), raw + "\n")


class TestR3P2_1MergeLogTail(unittest.TestCase):
    def test_tail_and_details_keep_json_lines(self) -> None:
        from mesh.daemon import merge

        tmp = tempfile.mkdtemp(prefix="mh-r3t-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "test.log")
        line = '{"type":"test","msg":"set password=[REDACTED]"}'
        with open(path, "w", encoding="utf-8") as f:
            f.write("-----BEGIN PRIVATE KEY-----\nMIIsecretbody\n" + line + "\n")
        tail, _cut = merge.log_tail_capped(path, lines=2)  # the BEGIN line is outside the tail
        self.assertEqual(tail[-1], line)
        details = merge.redact_merge_value({"log_tail": tail, "log_path": path})
        self.assertEqual(details["log_tail"][-1], line)
        json.loads(details["log_tail"][-1])


class TestR3P1_3WorkspaceDelete(unittest.TestCase):
    def test_live_delete_path_drops_the_cached_preparation(self) -> None:
        from mesh.daemon import supervisor
        from mesh.daemon.handlers import run as run_h

        with supervisor._WS_PREP_LOCK:
            supervisor._WS_PREP["tsk_r3"] = {"done": True, "result": {"workspace_path": "/x"}}
        with mock.patch.object(run_h, "_delete_workspace_dir_impl", return_value=True):
            self.assertTrue(run_h._delete_workspace_dir_sync("s", "r", "m", "prj", "tsk_r3", None))
        self.assertNotIn("tsk_r3", supervisor._WS_PREP)
        with supervisor._WS_PREP_LOCK:
            supervisor._WS_PREP["tsk_r3"] = {"done": True, "result": {}}
        with mock.patch.object(run_h, "_delete_workspace_dir_impl", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                run_h._delete_workspace_dir_sync("s", "r", "m", "prj", task_id="tsk_r3", repo_path=None)
        self.assertNotIn("tsk_r3", supervisor._WS_PREP)

    def test_start_of_a_deleted_worktree_is_refused(self) -> None:
        import mesh.constants as c
        from mesh.daemon.handlers.task import require_prepared_worktree
        from mesh.errors import MeshError

        tmp = tempfile.mkdtemp(prefix="mh-r3w-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(MeshError) as cm:
            require_prepared_worktree("tsk_1", os.path.join(tmp, "gone"))
        self.assertEqual(cm.exception.details["reason"], "workspace_missing")
        self.assertEqual(cm.exception.exit_code, c.EXIT_CONFLICT)
        with open(os.path.join(tmp, ".git"), "w", encoding="utf-8") as f:
            f.write("gitdir: x\n")
        require_prepared_worktree("tsk_1", tmp)


class TestR3P1_4LeadDeliverIntent(unittest.TestCase):
    def test_lead_turn_drops_intents_only_after_commit(self) -> None:
        from mesh.daemon import lead_turns

        tmp = tempfile.mkdtemp(prefix="mh-r3l-")
        self.addCleanup(shutil.rmtree, tmp, True)
        conn = sqlite3.connect(os.path.join(tmp, "x.db"), isolation_level=None)
        self.addCleanup(conn.close)
        intent = os.path.join(tmp, "prj.json")

        def finish(conn_, home, run, exit_info, testing_mode=False):
            open(intent, "w").close()
            lead_turns._PENDING_INTENT_UNLINKS.append(intent)
            return "exited"

        with mock.patch.object(lead_turns, "finish_lead_turn", side_effect=finish):
            self.assertEqual(lead_turns.finish_lead_turn_committed(conn, tmp, {}, None), "exited")
        self.assertFalse(os.path.exists(intent))

        def failing(conn_, home, run, exit_info, testing_mode=False):
            finish(conn_, home, run, exit_info)
            raise RuntimeError("tx failed")

        with mock.patch.object(lead_turns, "finish_lead_turn", side_effect=failing):
            with self.assertRaises(RuntimeError):
                lead_turns.finish_lead_turn_committed(conn, tmp, {}, None)
        self.assertTrue(os.path.exists(intent))
        self.assertEqual(lead_turns._PENDING_INTENT_UNLINKS, [])

    def test_internal_deliver_request_carries_its_idempotency_key(self) -> None:
        from mesh.daemon import server
        from mesh.daemon.handlers.base import HandlerContext

        seen = {}

        def handler(ctx):
            seen["idem"] = getattr(ctx, "idempotency", None)
            raise RuntimeError("stop")

        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("BEGIN;")
        ctx = HandlerContext(
            conn=conn,
            principal={"id": "agt_l", "kind": "agent", "scope": {"role": "lead", "project_id": "prj"}},
            actor_kind="agent",
            via="run_token",
            request_id="req_1",
            home=tempfile.gettempdir(),
            cmd="project.deliver",
            args={"project_id": "prj"},
            dry_run=False,
        )
        with mock.patch.dict(server.INTERNAL_REQUEST_HANDLERS, {"project.deliver": handler}), \
                mock.patch.object(server, "check_authz"), \
                mock.patch.object(server, "_lookup_live_idempotency", return_value=None):
            server.dispatch_internal_request(ctx, "act:run_1:0")
        self.assertEqual(seen["idem"]["key"], "act:run_1:0")
        self.assertTrue(seen["idem"]["request_hash"])


class TestR3P1_5WrapperPidFromDisk(unittest.TestCase):
    def test_running_row_without_wrapper_pid_uses_wrapper_json(self) -> None:
        from mesh.daemon.server import DaemonServer

        tmp = tempfile.mkdtemp(prefix="mh-r3p-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "wrapper.json"), "w", encoding="utf-8") as f:
            json.dump({"pid": 424242, "starttime": 77}, f)
        fake = mock.Mock()
        fake.db_conn.execute.return_value.fetchall.return_value = [
            {"id": "run_m", "wrapper_pid": None, "proc_starttime": None, "status": "running", "run_dir": tmp},
            {"id": "run_e", "wrapper_pid": None, "proc_starttime": None, "status": "exited", "run_dir": tmp},
        ]
        fake.known_run_ids, fake.active_runs = set(), {}
        DaemonServer.refresh_run_caches(fake)
        self.assertEqual(fake.active_runs.get("run_m"), 424242)
        self.assertNotIn("run_e", fake.active_runs)


class TestR3P1_6DaemonEnv(unittest.TestCase):
    SECRETS = {"MESH_PASSPHRASE": "pw-r3-secret", "MESH_PASSPHRASE_FILE": "/x/pw", "MESH_TOKEN": "mesh_at_x"}

    def test_daemon_start_spawns_without_credentials(self) -> None:
        from mesh.cli.commands import daemon as d

        seen = {}

        def popen(argv, **kw):
            seen["env"] = kw["env"]
            raise RuntimeError("stop")

        args = mock.Mock(home=tempfile.gettempdir(), dry_run=False)
        with mock.patch.dict(os.environ, self.SECRETS), mock.patch.object(d.subprocess, "Popen", popen), \
                mock.patch.object(d, "MeshClient") as mc:
            mc.return_value.execute_cmd.side_effect = Exception("down")
            try:
                d.handle_daemon_start(args)
            except Exception:
                pass
        self.assertIn("env", seen, "Popen not reached")
        for k in self.SECRETS:
            self.assertNotIn(k, seen["env"])

    def test_daemon_run_reexecs_without_credentials(self) -> None:
        from mesh.cli.commands import daemon as d

        if sys.platform == "win32":
            from mesh.platform import proc as plat_proc

            with mock.patch.dict(os.environ, self.SECRETS), mock.patch.object(plat_proc, "reexec_clean_env") as ex:
                ex.side_effect = SystemExit(0)
                with self.assertRaises(SystemExit):
                    d._exec_without_secret_env()
            env = ex.call_args[0][1]
            for k in self.SECRETS:
                self.assertNotIn(k, env)
            with mock.patch.object(plat_proc, "reexec_clean_env") as ex2:
                env2 = {k: v for k, v in os.environ.items() if k not in self.SECRETS}
                with mock.patch.dict(os.environ, env2, clear=True):
                    d._exec_without_secret_env()
                ex2.assert_not_called()
            return

        with mock.patch.dict(os.environ, self.SECRETS), mock.patch.object(d.os, "execve") as ex:
            ex.side_effect = SystemExit(0)
            with self.assertRaises(SystemExit):
                d._exec_without_secret_env()
        env = ex.call_args[0][2]
        for k in self.SECRETS:
            self.assertNotIn(k, env)
        with mock.patch.object(d.os, "execve") as ex2:
            env2 = {k: v for k, v in os.environ.items() if k not in self.SECRETS}
            with mock.patch.dict(os.environ, env2, clear=True):
                d._exec_without_secret_env()
            ex2.assert_not_called()


class TestR3P2_2StdinAdapters(unittest.TestCase):
    def test_claude_and_codex(self) -> None:
        from mesh.adapters.base import validate_adapter_options
        from mesh.core.prompts import prompt_strategy

        for a in ("claude", "codex"):
            self.assertEqual(prompt_strategy(a, "edit", 200_000), "file")
            self.assertEqual(validate_adapter_options(a, {"binary": "/opt/bin/x"})["binary"], "/opt/bin/x")


if __name__ == "__main__":
    unittest.main()
