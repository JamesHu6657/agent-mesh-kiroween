"""Unit tests for the Agent Mesh desktop bridge (whitelist, auth, reconnect, event pump)."""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import mesh.constants as c
from mesh.cli.main import main as cli_main
from mesh.desktop.bridge import ALLOWED_RPC_METHODS, CAPABILITIES, DesktopBridge, DesktopJsApi
from mesh.errors import MeshError


def _has_webview_and_display() -> bool:
    if importlib.util.find_spec("webview") is None:
        return False
    if sys.platform != "win32" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


class _FakeStreamSocket:
    """Fake socket supporting chunked NDJSON responses and timeout ticks."""

    def __init__(self, script: List[Any]) -> None:
        self._script = list(script)
        self.sent_payloads: List[Dict[str, Any]] = []
        self.timeout_value: Optional[float] = None
        self.closed = False
        self.close_thread_ident: Optional[int] = None
        self._cond = threading.Condition()

    def settimeout(self, timeout: Optional[float]) -> None:
        self.timeout_value = timeout

    def sendall(self, data: bytes) -> None:
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                self.sent_payloads.append(json.loads(line))

    def push_item(self, item: Any) -> None:
        with self._cond:
            self._script.append(item)
            self._cond.notify_all()

    def recv(self, _bufsize: int) -> bytes:
        with self._cond:
            timeout = self.timeout_value if self.timeout_value is not None else 0.05
            deadline = time.monotonic() + max(0.005, float(timeout))
            while not self._script and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("poll timeout")
                self._cond.wait(timeout=min(remaining, 0.05))
            if self.closed:
                return b""
            nxt = self._script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if isinstance(nxt, bytes):
            return nxt
        return json.dumps(nxt, separators=(",", ":")).encode("utf-8") + b"\n"

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self.close_thread_ident = threading.get_ident()
            self._cond.notify_all()


class _FakeDaemonController:
    """Thread-safe fake daemon state backing fake MeshClient instances."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.online: bool = True
        self.doctor_rpc_supported: bool = False
        self.evidence_mode: str = "warn"
        self.passphrase: str = "correct-horse-battery-staple"
        self.requests: List[Dict[str, Any]] = []
        self.events_to_catch_up: List[Dict[str, Any]] = []
        self.stream_sockets: List[_FakeStreamSocket] = []
        self.next_stream_script: List[Any] = []
        self.stream_opened = threading.Event()
        self.connect_timeouts: List[Optional[float]] = []

    def make_client(self, home: str, timeout: float) -> Any:
        controller = self

        class _Client:
            def __init__(self) -> None:
                self.home = home
                self.timeout = timeout
                self.socket_path = os.path.join(home, "run", "mesh.sock")

            def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                with controller.lock:
                    controller.requests.append(payload)
                    online = controller.online
                    expected_pw = controller.passphrase
                    catch_up = list(controller.events_to_catch_up)

                if not online:
                    raise MeshError(
                        code=c.DAEMON_UNAVAILABLE,
                        exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                        message="Fake daemon offline",
                    )

                cmd = payload.get("cmd")
                auth = payload.get("auth") or {}
                req_id = payload.get("request_id")

                if cmd == "daemon.status":
                    return {
                        "ok": True,
                        "data": {"status": "running", "pid": 4242, "uptime_s": 12.3, "active_runs": 0},
                        "request_id": req_id,
                    }
                if cmd == "version":
                    return {"ok": True, "data": {"version": "1.0.0.dev1"}, "request_id": req_id}

                if auth.get("type") != "user":
                    return {
                        "ok": False,
                        "exit_code": c.EXIT_UNAUTHENTICATED,
                        "error": {"code": c.TOKEN_MISSING, "message": "Token or passphrase required", "details": {}},
                        "request_id": req_id,
                    }
                if auth.get("passphrase") != expected_pw:
                    return {
                        "ok": False,
                        "exit_code": c.EXIT_UNAUTHENTICATED,
                        "error": {"code": c.PASSPHRASE_INVALID, "message": "Invalid passphrase", "details": {}},
                        "request_id": req_id,
                    }

                if cmd == "whoami":
                    return {
                        "ok": True,
                        "data": {"principal_id": "usr_1", "kind": "user", "name": "user", "via": "direct"},
                        "request_id": req_id,
                    }
                if cmd == "project.list":
                    return {"ok": True, "data": {"items": [{"id": "prj_1", "name": "Demo"}]}, "request_id": req_id}
                if cmd == "event.list":
                    req_args = payload.get("args") or {}
                    since = int(req_args.get("since") or 0)
                    limit = int(req_args.get("limit") or 1000)
                    items = [ev for ev in catch_up if int(ev.get("seq", 0)) > since][:limit]
                    return {"ok": True, "data": {"items": items}, "request_id": req_id}
                if cmd == "config.get":
                    key = (payload.get("args") or {}).get("key")
                    if key == "adapter.evidence_mode":
                        with controller.lock:
                            mode = controller.evidence_mode
                        return {"ok": True, "data": {"key": key, "value": mode}, "request_id": req_id}
                    return {"ok": True, "data": {"key": key, "value": None}, "request_id": req_id}
                if cmd == "doctor":
                    with controller.lock:
                        supported = controller.doctor_rpc_supported
                    if not supported:
                        return {
                            "ok": False,
                            "exit_code": c.EXIT_USAGE,
                            "error": {"code": c.INVALID_ARGUMENT, "message": "Unknown command: 'doctor'", "details": {}},
                            "request_id": req_id,
                        }
                    return {
                        "ok": True,
                        "data": {
                            "healthy": True,
                            "checks": [{"name": "daemon_rpc_doctor", "status": "ok", "message": "rpc doctor ok"}],
                        },
                        "request_id": req_id,
                    }
                if cmd == "daemon.stop":
                    with controller.lock:
                        controller.online = False
                    return {"ok": True, "data": {"status": "stopping"}, "request_id": req_id}
                return {"ok": True, "data": {"echo_cmd": cmd, "args": payload.get("args")}, "request_id": req_id}

            def _connect_socket(self, timeout: Optional[float] = None) -> _FakeStreamSocket:
                with controller.lock:
                    controller.connect_timeouts.append(timeout)
                    if not controller.online:
                        raise MeshError(
                            code=c.DAEMON_UNAVAILABLE,
                            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                            message="Fake daemon offline for stream",
                        )
                    sock = _FakeStreamSocket(controller.next_stream_script)
                    controller.stream_sockets.append(sock)
                    controller.stream_opened.set()
                    return sock

        return _Client()


class TestDesktopBridgeWhitelistAndAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-desk-unit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.ctrl = _FakeDaemonController()
        self.bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            push_enabled=False,
        )
        self.addCleanup(self.bridge.close)

    def test_whitelist_rejects_disallowed_and_unimplemented_rpcs(self) -> None:
        blocked = [
            "project.plan.confirm",
            "project.conversation",
            "task.log.tail",
            "chat.reply",
            "plan.propose",
            "plan.dispatch",
            "daemon.start",
            "event.tail",
            "agent.token.issue",
            "nonexistent.rpc",
            "",
            None,
        ]
        for method in blocked:
            res = self.bridge.call(method)  # type: ignore[arg-type]
            self.assertFalse(res["ok"], f"Expected rejection for {method!r}")
            self.assertEqual(res["error"]["code"], "RPC_NOT_ALLOWED")
        self.assertEqual(self.ctrl.requests, [], "Disallowed RPCs must never hit MeshClient")

    def test_non_dict_params_rejected(self) -> None:
        res = self.bridge.call("project.list", ["bad"])  # type: ignore[arg-type]
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], c.INVALID_ARGUMENT)

    def test_unauth_rpcs_do_not_require_passphrase(self) -> None:
        res_st = self.bridge.call("daemon.status")
        res_ver = self.bridge.call("version")
        self.assertTrue(res_st["ok"])
        self.assertTrue(res_ver["ok"])
        self.assertEqual(self.ctrl.requests[0]["auth"], {"type": "none"})
        self.assertEqual(self.ctrl.requests[1]["auth"], {"type": "none"})

    def test_auth_envelope_and_secret_non_leakage(self) -> None:
        locked = self.bridge.call("project.list", {})
        self.assertFalse(locked["ok"])
        self.assertEqual(locked["error"]["code"], c.TOKEN_MISSING)
        self.assertEqual(locked["error"]["exit_code"], c.EXIT_UNAUTHENTICATED)

        bad = self.bridge.set_passphrase("wrong-secret-value")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], c.PASSPHRASE_INVALID)
        self.assertFalse(self.bridge.auth_status()["data"]["auth_configured"])

        good = self.bridge.set_passphrase(self.ctrl.passphrase)
        self.assertTrue(good["ok"])
        self.assertTrue(good["data"]["auth_verified"])

        listed = self.bridge.call("project.list", {"status": "active"})
        self.assertTrue(listed["ok"])
        last_req = self.ctrl.requests[-1]
        self.assertEqual(last_req["v"], 1)
        self.assertEqual(last_req["cmd"], "project.list")
        self.assertEqual(last_req["auth"], {"type": "user", "passphrase": self.ctrl.passphrase})
        self.assertEqual(last_req["args"], {"status": "active"})

        # Verify passphrase never appears in repr or status/pipe metadata
        secret = self.ctrl.passphrase
        for inspect_obj in (
            repr(self.bridge),
            json.dumps(self.bridge.auth_status()),
            json.dumps(self.bridge.pipe_info()),
            json.dumps(self.bridge.connection_state()),
        ):
            self.assertNotIn(secret, inspect_obj)

        self.bridge.clear_passphrase()
        after = self.bridge.call("project.list", {})
        self.assertFalse(after["ok"])
        self.assertEqual(after["error"]["code"], c.TOKEN_MISSING)


class TestDesktopBridgeReconnectAndPump(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-desk-reconn-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.ctrl = _FakeDaemonController()

    def test_disconnect_backoff_and_reconnect_with_catchup(self) -> None:
        self.ctrl.online = False
        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03, 0.04),
            probe_interval_s=0.05,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        # Store passphrase while daemon is down
        res = bridge.set_passphrase(self.ctrl.passphrase, verify=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["data"]["auth_verified"])

        bridge.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if bridge.connection_state()["data"]["reconnect_attempt"] >= 2:
                break
            time.sleep(0.01)
        self.assertGreaterEqual(bridge.connection_state()["data"]["reconnect_attempt"], 2)
        self.assertFalse(bridge.connection_state()["data"]["connected"])

        # Seed an event and bring daemon back online
        self.ctrl.events_to_catch_up = [
            {"seq": 15, "type": "task.submitted", "project_id": "prj_1", "task_id": "tsk_1"},
        ]
        # Force bridge's last_seq > 0 so catch-up runs across reconnects
        bridge._enqueue_and_push({"seq": 10, "type": "task.created", "project_id": "prj_1", "task_id": "tsk_1"})
        bridge.poll_events()

        with self.ctrl.lock:
            self.ctrl.online = True

        deadline = time.monotonic() + 3.0
        saw_reconnect = False
        collected: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            polled = bridge.poll_events()["data"]["items"]
            collected.extend(polled)
            if any(ev.get("type") == "_desktop.connection" and ev.get("connected") is True for ev in collected):
                if any(ev.get("seq") == 15 for ev in collected):
                    saw_reconnect = True
                    break
            time.sleep(0.02)

        self.assertTrue(saw_reconnect, f"Expected reconnect + seq=15 catchup, got {collected}")
        state = bridge.connection_state()["data"]
        self.assertTrue(state["connected"])
        self.assertTrue(state["auth_verified"])
        self.assertEqual(state["reconnect_attempt"], 0)

        # Disconnect triggered by RPC returning DAEMON_UNAVAILABLE
        with self.ctrl.lock:
            self.ctrl.online = False
        fail_res = bridge.call("project.list", {})
        self.assertFalse(fail_res["ok"])
        self.assertEqual(fail_res["error"]["code"], c.DAEMON_UNAVAILABLE)
        self.assertFalse(bridge.connection_state()["data"]["connected"])

    def test_event_tail_stream_push_and_queue_fallback(self) -> None:
        self.ctrl.next_stream_script = [
            TimeoutError("idle tick"),
            {"seq": 21, "type": "task.started", "project_id": "prj_1", "task_id": "tsk_1"},
            b'{"seq":22,"type":"task.',
            b'submitted","project_id":"prj_1"}\n',
        ]
        pushed_js: List[str] = []

        class _FakeWindow:
            def __init__(self, accept: bool = True) -> None:
                self.accept = accept

            def evaluate_js(self, script: str) -> bool:
                pushed_js.append(script)
                return self.accept

        win = _FakeWindow(accept=False)
        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            stream_poll_timeout_s=0.05,
            push_enabled=True,
        )
        self.addCleanup(bridge.close)
        bridge.attach_window(win)
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])

        self.assertTrue(self.ctrl.stream_opened.wait(timeout=2.0))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if bridge.connection_state()["data"]["last_seq"] >= 22:
                break
            time.sleep(0.01)
        self.assertEqual(bridge.connection_state()["data"]["last_seq"], 22)

        # Since fake window returned False (JS not ready), events stayed safely queued
        polled = bridge.poll_events()["data"]["items"]
        self.assertEqual([e.get("seq") for e in polled], [21, 22])

        # Verify request sent to event.tail socket included exact auth and omitted since on fresh unlock
        sock = self.ctrl.stream_sockets[0]
        self.assertEqual(sock.sent_payloads[0]["cmd"], "event.tail")
        self.assertEqual(sock.sent_payloads[0]["auth"], {"type": "user", "passphrase": self.ctrl.passphrase})
        self.assertNotIn("since", sock.sent_payloads[0].get("args") or {})

        # Now test successful JS push
        win.accept = True
        sock.push_item({"seq": 23, "type": "task.accepted", "project_id": "prj_1"})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any("23" in call for call in pushed_js):
                break
            time.sleep(0.01)
        self.assertTrue(any("window.__meshOnEvents" in call and '"seq":23' in call for call in pushed_js))
        # Pushed batch consumed from pending queue so poll_events doesn't double-deliver
        self.assertEqual(bridge.poll_events()["data"]["items"], [])

        caller_ident = threading.get_ident()
        bridge.stop_event_stream()
        self.assertTrue(sock.closed)
        self.assertIsNotNone(sock.close_thread_ident)
        self.assertNotEqual(sock.close_thread_ident, caller_ident)


class TestDesktopBridgeDaemonAndCli(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-desk-cli-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))

    def test_start_daemon_strips_secrets_and_uses_no_window_on_win32(self) -> None:
        recorded: List[Tuple[List[str], Dict[str, Any]]] = []

        def fake_run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded.append((list(argv), dict(kwargs)))
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "running", "pid": 999}}),
                stderr="",
            )

        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            subprocess_runner=fake_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        with mock.patch.dict(
            os.environ,
            {
                "MESH_PASSPHRASE": "leak-me-not",
                "MESH_PASSPHRASE_FILE": "/tmp/pw",
                "MESH_TOKEN": "tok-leak",
                "KEEP_ME": "yes",
            },
            clear=False,
        ):
            with mock.patch("mesh.desktop.bridge.sys.platform", "win32"):
                res = bridge.start_daemon()

        self.assertTrue(res["ok"], res)
        self.assertEqual(len(recorded), 1)
        argv, kwargs = recorded[0]
        self.assertEqual(argv[-4:], ["--home", bridge.home, "daemon", "start"])
        self.assertEqual(os.path.realpath(bridge.home), os.path.realpath(self.home))
        self.assertIn("daemon", argv)
        self.assertIn("start", argv)
        child_env = kwargs["env"]
        self.assertNotIn("MESH_PASSPHRASE", child_env)
        self.assertNotIn("MESH_PASSPHRASE_FILE", child_env)
        self.assertNotIn("MESH_TOKEN", child_env)
        self.assertEqual(child_env.get("KEEP_ME"), "yes")
        self.assertEqual(child_env.get("PYTHONUTF8"), "1")
        self.assertEqual(child_env.get("_MESH_DAEMON_NO_WINDOW"), "1")
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertEqual(kwargs.get("errors"), "replace")
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("creationflags"), getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_desktop_daemon_spawn_uses_create_no_window_and_process_group(self) -> None:
        import argparse
        from mesh.cli.commands import daemon as daemon_cmd

        with (
            mock.patch.object(daemon_cmd.sys, "platform", "win32"),
            mock.patch.object(daemon_cmd, "_meshd_lock_held", return_value=False),
            mock.patch.object(daemon_cmd, "MeshClient") as m_mc,
            mock.patch.object(daemon_cmd.subprocess, "Popen", side_effect=RuntimeError("stop")) as m_popen,
            mock.patch.dict(os.environ, {"_MESH_DAEMON_NO_WINDOW": "1"}, clear=False),
        ):
            m_mc.return_value.home = self.home
            m_mc.return_value.execute_cmd.side_effect = RuntimeError("down")
            args = argparse.Namespace(home=self.home, dry_run=False, user=False)
            with self.assertRaises(RuntimeError):
                daemon_cmd.handle_daemon_start(args)

        _, popen_kw = m_popen.call_args
        expected_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        self.assertEqual(popen_kw["creationflags"], expected_flags)
        self.assertNotIn("_MESH_DAEMON_NO_WINDOW", popen_kw["env"])

    def test_start_daemon_strips_foreign_pythonpath_for_checkout_and_site_packages(self) -> None:
        recorded: List[Dict[str, Any]] = []

        def fake_run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded.append(dict(kwargs))
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "running", "pid": 1001}}),
                stderr="",
            )

        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            subprocess_runner=fake_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        with mock.patch.dict(os.environ, {"PYTHONPATH": "/foreign/shadow/pkg"}, clear=False):
            with mock.patch("mesh.desktop.bridge.is_site_packages_dir", return_value=False):
                res_checkout = bridge.start_daemon()
            with mock.patch("mesh.desktop.bridge.is_site_packages_dir", return_value=True):
                res_site = bridge.start_daemon()

        self.assertTrue(res_checkout["ok"], res_checkout)
        self.assertTrue(res_site["ok"], res_site)
        self.assertEqual(len(recorded), 2)

        env_checkout = recorded[0]["env"]
        expected_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # Bridge's install_root is mesh-fresh-v1 repo root
        self.assertEqual(os.path.realpath(env_checkout["PYTHONPATH"]), os.path.realpath(expected_root))
        self.assertEqual(env_checkout.get("PYTHONUTF8"), "1")

        env_site = recorded[1]["env"]
        self.assertFalse(any(k.upper() == "PYTHONPATH" for k in env_site), env_site)
        self.assertEqual(env_site.get("PYTHONUTF8"), "1")

    def test_stop_daemon_and_capabilities(self) -> None:
        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.capabilities()["confirm_plan"])
        self.assertTrue(bridge.capabilities()["task_diff"])
        self.assertTrue(bridge.capabilities()["project_conversation"])
        self.assertTrue(bridge.capabilities()["approval_policy_inline"])
        self.assertTrue(bridge.set_passphrase(ctrl.passphrase, verify=True)["ok"])
        stop_res = bridge.stop_daemon()
        self.assertTrue(stop_res["ok"])
        self.assertFalse(bridge.connection_state()["data"]["connected"])

    def test_cli_desktop_check_runs_without_pywebview(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--home", self.home, "desktop", "--check"])
        self.assertEqual(rc, 0, buf.getvalue())
        env = json.loads(buf.getvalue())
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["home"], os.path.realpath(self.home))
        self.assertIn("index.html", env["data"]["files"])

    def test_allowed_rpc_set_is_complete_and_clean(self) -> None:
        expected_allowed = frozenset({
            "version",
            "whoami",
            "daemon.status",
            "daemon.stop",
            "config.list",
            "config.get",
            "config.set",
            "agent.list",
            "agent.show",
            "agent.create",
            "agent.update",
            "agent.disable",
            "agent.enable",
            "adapter.list",
            "adapter.show",
            "adapter.probe",
            "adapter.models",
            "project.list",
            "project.show",
            "project.leads",
            "project.set_lead",
            "project.pause",
            "project.resume",
            "project.cancel",
            "project.deliver",
            "project.complete",
            "project.update",
            "project.set_permission_mode",
            "project.diff",
            "chat.send",
            "chat.list",
            "chat.queue_cancel",
            "chat.queue_edit",
            "lead.interrupt",
            "plan.list",
            "plan.show",
            "plan.cancel",
            "grant.list",
            "grant.revoke",
            "intake.submit",
            "task.list",
            "task.show",
            "task.create",
            "task.assign",
            "task.reassign",
            "task.accept",
            "task.reject",
            "task.merge",
            "task.retry",
            "task.escalate",
            "task.cancel",
            "task.close",
            "task.log",
            "task.diff",
            "dep.list",
            "run.list",
            "run.show",
            "run.log",
            "run.activity",
            "run.release",
            "workspace.list",
            "workspace.show",
            "workspace.prune",
            "approval.list",
            "approval.show",
            "approval.decide",
            "event.list",
            "audit.verify",
        })
        self.assertEqual(ALLOWED_RPC_METHODS, expected_allowed)
        for removed_rpc in ("approval.approve", "approval.reject", "approval.cancel", "project.note"):
            self.assertNotIn(removed_rpc, ALLOWED_RPC_METHODS)
        for lead_only_rpc in ("chat.reply", "plan.propose", "plan.dispatch", "plan.note_interpretation"):
            self.assertNotIn(lead_only_rpc, ALLOWED_RPC_METHODS)
        self.assertNotIn("event.tail", ALLOWED_RPC_METHODS)
        for batch3_rpc in ("task.diff", "project.diff", "run.activity", "lead.interrupt", "chat.queue_cancel", "chat.queue_edit"):
            self.assertIn(batch3_rpc, ALLOWED_RPC_METHODS)

        # Calling removed legacy approval/project.note methods through DesktopJsApi is blocked before MeshClient
        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        self.assertTrue(api.set_passphrase(ctrl.passphrase, verify=True)["ok"])
        ctrl.requests.clear()
        for blocked in ("approval.approve", "approval.reject", "approval.cancel", "project.note"):
            res = api.call(blocked, {})
            self.assertFalse(res["ok"], f"{blocked} must be rejected by DesktopJsApi")
            self.assertEqual(res["error"]["code"], "RPC_NOT_ALLOWED")
        self.assertEqual(ctrl.requests, [])

    def test_js_api_facade_hides_lifecycle_methods_and_blocks_dangerous_args(self) -> None:
        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        self.assertFalse(hasattr(api, "attach_window"))
        self.assertFalse(hasattr(api, "start"))
        self.assertFalse(hasattr(api, "request_stop"))
        self.assertFalse(hasattr(api, "close"))
        self.assertFalse(hasattr(api, "start_event_stream"))
        self.assertFalse(hasattr(api, "stop_event_stream"))
        self.assertTrue(api.set_passphrase(ctrl.passphrase, verify=True)["ok"])

        bad_calls = [
            ("adapter.probe", {"adapter_id": "codex", "binary": "/bin/sh"}, "ARGUMENT_NOT_ALLOWED"),
            ("agent.create", {"name": "a1", "adapter": "codex", "binary_path": "/bin/sh"}, "ARGUMENT_NOT_ALLOWED"),
            ("agent.create", {"name": "a1", "adapter": "codex", "options": {"binary": "/bin/sh"}}, "ARGUMENT_NOT_ALLOWED"),
            ("agent.update", {"name": "a1", "options": {"BINARY_PATH": "/bin/sh"}}, "ARGUMENT_NOT_ALLOWED"),
            ("agent.create", {"name": "a1", "adapter": "codex", "option": ["binary=/bin/sh"]}, "ARGUMENT_NOT_ALLOWED"),
            ("config.set", {"key": "notify.command", "value": "echo pwned"}, "SETTING_NOT_ALLOWED"),
        ]
        ctrl.requests.clear()
        for method, args, expected_code in bad_calls:
            res = api.call(method, args)
            self.assertFalse(res["ok"], f"Expected validation rejection for {method} {args}")
            self.assertEqual(res["error"]["code"], expected_code)
        self.assertEqual(ctrl.requests, [], "Dangerous options must be blocked before MeshClient")

    def test_js_api_facade_blocks_pywebview_dotted_path_traversal(self) -> None:
        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        self.assertIsInstance(api, DesktopJsApi)
        self.assertTrue(api.set_passphrase(ctrl.passphrase, verify=True)["ok"])

        # Simulate pywebview 6.x get_functions + get_nested_attribute (splits on '.' via getattr)
        def _sim_get_functions(obj: Any) -> Dict[str, List[str]]:
            out: Dict[str, List[str]] = {}
            for name in dir(obj):
                if name.startswith("_"):
                    continue
                attr = getattr(obj, name)
                if inspect.ismethod(attr):
                    out[name] = list(inspect.getfullargspec(attr).args)[1:]
                elif inspect.isfunction(attr):
                    out[name] = list(inspect.getfullargspec(attr).args)
            return out

        def _sim_get_nested_attribute(obj: Any, attr_str: str) -> Any:
            for seg in attr_str.split("."):
                obj = getattr(obj, seg, None)
                if obj is None:
                    return None
            return obj

        funcs = _sim_get_functions(api)
        expected_methods = {
            "capabilities",
            "auth_status",
            "set_passphrase",
            "clear_passphrase",
            "pipe_info",
            "connection_state",
            "home_status",
            "initialize_home",
            "detect_local_clis",
            "pick_folder",
            "inspect_repo_folder",
            "desktop_prefs",
            "set_desktop_prefs",
            "quit_app",
            "call",
            "daemon_status",
            "start_daemon",
            "stop_daemon",
            "doctor_report",
            "poll_events",
            "reconnect_now",
            "window_minimize",
            "window_toggle_maximize",
            "window_restore_for_drag",
            "window_drag_move",
            "window_drag_end",
            "window_resize_start",
            "window_resize",
            "window_resize_end",
            "window_close",
        }
        self.assertEqual(set(funcs), expected_methods)
        # All exposed facade functions use (*args, **kwargs) with no default params
        for pub_name in expected_methods:
            self.assertEqual(funcs[pub_name], [], f"Expected default-free *args signature for {pub_name}")

        blocked_paths = [
            "_bridge",
            "_bridge.__dict__.get",
            "_bridge._passphrase",
            "_bridge.close",
            "_bridge.request_stop",
            "__class__",
            "__class__.__mro__",
            "__dict__",
            "__getattribute__",
            "__setattr__",
            "__delattr__",
            "__dir__",
            "__init__",
            "__new__",
            "_passphrase",
            "home",
            "close",
            "start",
            "request_stop",
            "attach_window",
            "start_event_stream",
            "stop_event_stream",
            "call.__self__",
            "call.__func__",
            "call.__self__._bridge",
            "call.__self__.__class__",
            "call.__self__.__dict__",
            "call.__func__.__closure__.0",
            "call.__closure__.0",
            "call.__builtins__.__setitem__",
            "call.__builtins__.__delitem__",
            "call.__builtins__.clear",
            "call.__builtins__.len",
            "call.__globals__.__builtins__.__setitem__",
            "call.__globals__.DesktopBridge",
        ]
        for path in blocked_paths:
            self.assertIsNone(_sim_get_nested_attribute(api, path), f"Path leaked: {path}")

        # Facade itself rejects attribute mutation
        with self.assertRaises(AttributeError):
            setattr(api, "call", lambda *a, **k: {})
        with self.assertRaises(AttributeError):
            delattr(api, "call")

        # Sealed functions have no defaults, empty read-only __builtins__, and fresh objects per lookup
        import builtins
        import types

        for pub_name in funcs:
            fn1 = _sim_get_nested_attribute(api, pub_name)
            fn2 = _sim_get_nested_attribute(api, pub_name)
            self.assertIsNotNone(fn1)
            self.assertIsNot(fn1, fn2, f"{pub_name} must mint a fresh function per lookup")
            self.assertIsNone(getattr(fn1, "__defaults__"))
            self.assertIsNone(getattr(fn1, "__kwdefaults__"))
            fn_globals = getattr(fn1, "__globals__")
            fn_builtins = getattr(fn1, "__builtins__")
            self.assertEqual(fn_globals, {"__builtins__": {}})
            self.assertEqual(fn_builtins, {})
            self.assertIsInstance(fn_builtins, types.MappingProxyType)
            self.assertIsNot(fn_builtins, builtins.__dict__)
            self.assertIsNot(fn_builtins, builtins)
            with self.assertRaises(TypeError):
                fn_builtins["len"] = 0  # type: ignore[index]

        # P2-1 mutation attempts: __defaults__, __kwdefaults__, __code__, __globals__, __setattr__
        # 1. Simulate JS dotted-path calls via pywebview (e.g. call.__setattr__('__defaults__', None))
        setattr_fn = _sim_get_nested_attribute(api, "call.__setattr__")
        self.assertIsNotNone(setattr_fn)
        setattr_fn("__defaults__", None)
        setattr_fn("__defaults__", ("poison",))
        setattr_fn("__kwdefaults__", {"params": "poison"})
        # 2. Direct in-Python mutation of a fetched function reference
        fetched_call = _sim_get_nested_attribute(api, "call")
        fetched_call.__defaults__ = None
        self.assertTrue(fetched_call("project.list", {})["ok"])
        fetched_call.__defaults__ = ("poison1", "poison2")
        self.assertTrue(fetched_call("project.list", {})["ok"])
        fetched_call.__kwdefaults__ = {"idempotency_key": "poison"}
        self.assertTrue(fetched_call("project.list", {})["ok"])
        fetched_call.__globals__["injected"] = "evil"
        fetched_call.__globals__["__builtins__"] = {"len": 0}
        self.assertTrue(fetched_call("project.list", {})["ok"])

        # Non-code __code__ mutation (what JS JSON can send) raises TypeError
        with self.assertRaises(TypeError):
            _sim_get_nested_attribute(api, "call.__setattr__")("__code__", None)

        # Even a matching-free-vars __code__ replacement in Python only affects the ephemeral copy
        dummy_cell = "poison"

        def _poison_code(*_a: Any, **_k: Any) -> Dict[str, Any]:
            return {"ok": False, "error": {"code": dummy_cell}}

        fetched_call.__code__ = _poison_code.__code__
        # Next lookup through DesktopJsApi is completely pristine and unaffected!
        fresh_call = _sim_get_nested_attribute(api, "call")
        self.assertIsNot(fresh_call.__code__, _poison_code.__code__)
        self.assertIsNone(fresh_call.__defaults__)
        self.assertIsNone(fresh_call.__kwdefaults__)
        self.assertEqual(fresh_call.__globals__, {"__builtins__": {}})
        self.assertIsInstance(fresh_call.__builtins__, types.MappingProxyType)
        self.assertTrue(fresh_call("project.list", {})["ok"])
        self.assertEqual(len([1, 2, 3]), 3)
        self.assertNotIn(ctrl.passphrase, repr(api))
        self.assertFalse(hasattr(DesktopBridge, "_record_max_seq"))

    def test_filtered_event_list_does_not_advance_last_seq(self) -> None:
        ctrl = _FakeDaemonController()
        ctrl.events_to_catch_up = [
            {"seq": 10, "type": "task.created", "project_id": "prj_1"},
            {"seq": 25, "type": "task.created", "project_id": "prj_2"},
            {"seq": 50, "type": "task.submitted", "project_id": "prj_1"},
        ]
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        with bridge._lock:
            bridge._passphrase = ctrl.passphrase
            bridge._auth_verified = True

        # Filtered and direct event.list calls must NOT advance global _last_seq
        for params in (
            {"project": "prj_1", "limit": 1000},
            {"task": "tsk_1"},
            {"type": "task.created"},
            {},
        ):
            res = bridge.call("event.list", params)
            self.assertTrue(res["ok"])
            self.assertIsNone(bridge._last_seq)
            self.assertEqual(bridge.connection_state()["data"]["last_seq"], 0)

        # Catch-up and event.tail DO advance _last_seq
        bridge._catch_up_events(ctrl.passphrase, 0)
        self.assertEqual(bridge.connection_state()["data"]["last_seq"], 50)

    def test_catch_up_events_paginates_above_1000_and_resumes_stream_since(self) -> None:
        ctrl = _FakeDaemonController()
        ctrl.events_to_catch_up = [
            {"seq": i, "type": "task.created", "project_id": "prj_1"}
            for i in range(1, 2051)
        ]
        ctrl.next_stream_script = [TimeoutError("idle tick")]
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            stream_poll_timeout_s=0.05,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(ctrl.passphrase, verify=True)["ok"])
        bridge.stop_event_stream()
        ctrl.stream_opened.clear()

        ctrl.requests.clear()
        bridge._catch_up_events(ctrl.passphrase, 0)
        event_list_calls = [r for r in ctrl.requests if r.get("cmd") == "event.list"]
        self.assertEqual([r["args"]["since"] for r in event_list_calls], [0, 1000, 2000])
        self.assertEqual(bridge.connection_state()["data"]["last_seq"], 2050)

        # Reconnect stream resumes from last_seq=2050
        bridge.start_event_stream()
        self.assertTrue(ctrl.stream_opened.wait(timeout=2.0))
        sock = ctrl.stream_sockets[-1]
        deadline = time.monotonic() + 2.0
        while not sock.sent_payloads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(sock.sent_payloads, "Expected event.tail request on reconnect stream")
        self.assertEqual(sock.sent_payloads[0]["args"].get("since"), 2050)
        bridge.stop_event_stream()

    def test_doctor_report_never_opens_db_when_daemon_online_or_locked(self) -> None:
        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(ctrl.passphrase, verify=True)["ok"])

        # 1. Online daemon without doctor RPC handler -> IPC fallback, never handle_doctor
        with mock.patch("mesh.cli.commands.doctor.handle_doctor", side_effect=AssertionError("must not call handle_doctor")):
            rep_ipc = bridge.doctor_report()
        self.assertTrue(rep_ipc["ok"])
        self.assertEqual(rep_ipc["data"]["source"], "ipc")
        self.assertEqual(rep_ipc["data"]["status"], "healthy")
        ev_check = next(ch for ch in rep_ipc["data"]["checks"] if ch["name"] == "evidence_mode")
        self.assertTrue(ev_check["ok"])
        self.assertIn("warn:", ev_check["detail"])

        ctrl.evidence_mode = "strict"
        rep_strict = bridge.doctor_report()
        self.assertTrue(rep_strict["ok"])
        ev_strict = next(ch for ch in rep_strict["data"]["checks"] if ch["name"] == "evidence_mode")
        self.assertIn("strict:", ev_strict["detail"])

        # Scheduler sweep error dict formatted cleanly (step, item, error, at)
        with bridge._lock:
            bridge._daemon_status = {
                "pid": 4242,
                "testing_mode": False,
                "scheduler": {
                    "sweep_errors": 2,
                    "last_sweep_error": {
                        "step": "promote_tasks",
                        "item": "tsk_99",
                        "error": "RuntimeError: boom",
                        "at": "2026-11-22T08:00:00Z",
                    },
                },
            }
        sched_rep = bridge._ipc_doctor_report()
        sched_chk = next(ch for ch in sched_rep["checks"] if ch["name"] == "scheduler_errors")
        self.assertFalse(sched_chk["ok"])
        self.assertEqual(
            sched_chk["detail"],
            "2 since meshd started; last: promote_tasks on tsk_99: RuntimeError: boom at 2026-11-22T08:00:00Z",
        )

        # 2. Online daemon supporting doctor RPC -> uses RPC doctor, never handle_doctor
        ctrl.doctor_rpc_supported = True
        with mock.patch("mesh.cli.commands.doctor.handle_doctor", side_effect=AssertionError("must not call handle_doctor")):
            rep_rpc = bridge.doctor_report()
        self.assertTrue(rep_rpc["ok"])
        self.assertEqual(rep_rpc["data"]["checks"][0]["name"], "daemon_rpc_doctor")

        # 3. Offline daemon with exclusive lock held -> offline_lock_safe, never handle_doctor
        with ctrl.lock:
            ctrl.online = False
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        with (
            mock.patch("mesh.platform.locks.is_exclusive_lock_held", return_value=True),
            mock.patch("mesh.cli.commands.doctor.handle_doctor", side_effect=AssertionError("must not call handle_doctor")),
        ):
            rep_locked = bridge.doctor_report()
        self.assertTrue(rep_locked["ok"])
        self.assertEqual(rep_locked["data"]["source"], "offline_lock_safe")
        self.assertEqual(rep_locked["data"]["status"], "daemon_lock_active")

        # 4. Offline + unlocked -> delegates safely to local handle_doctor
        with (
            mock.patch("mesh.platform.locks.is_exclusive_lock_held", return_value=False),
            mock.patch(
                "mesh.cli.commands.doctor.handle_doctor",
                return_value={"status": "healthy", "home": self.home, "checks": [{"name": "db_file", "ok": True, "detail": "ok"}]},
            ) as mock_doc,
        ):
            rep_off = bridge.doctor_report()
        self.assertTrue(rep_off["ok"])
        self.assertEqual(rep_off["data"]["status"], "healthy")
        mock_doc.assert_called_once()

        # 5. Offline OSError from handle_doctor returns sanitized error envelope
        with (
            mock.patch("mesh.platform.locks.is_exclusive_lock_held", return_value=False),
            mock.patch("mesh.cli.commands.doctor.handle_doctor", side_effect=PermissionError("locked")),
        ):
            rep_err = bridge.doctor_report()
        self.assertFalse(rep_err["ok"])
        self.assertEqual(rep_err["error"]["code"], c.INTERNAL_ERROR)

        # 6. _safe_env_checks passes CREATE_NO_WINDOW on win32
        git_run_kwargs: List[Dict[str, Any]] = []

        def fake_git_run(*_args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            git_run_kwargs.append(dict(kwargs))
            return subprocess.CompletedProcess(["git", "--version"], 0, stdout="git version 2.45.0\n", stderr="")

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch("mesh.desktop.bridge.shutil.which", return_value="git"),
            mock.patch("mesh.desktop.bridge.subprocess.run", side_effect=fake_git_run),
        ):
            bridge._safe_env_checks(testing_mode=True)
        self.assertEqual(len(git_run_kwargs), 1)
        self.assertEqual(
            git_run_kwargs[0].get("creationflags"),
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )

    def test_stream_exit_code_push_retention_and_non_lost_wakeup(self) -> None:
        ctrl = _FakeDaemonController()
        pushed: List[str] = []

        class _RejectingWindow:
            def evaluate_js(self, script: str) -> bool:
                pushed.append(script)
                return False

        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=True)
        self.addCleanup(bridge.close)
        bridge.attach_window(_RejectingWindow())
        self.assertTrue(bridge.set_passphrase(ctrl.passphrase, verify=True)["ok"])

        # Top-level exit_code on stream line triggers auth invalidation
        stop_ev = threading.Event()
        bridge._handle_stream_line(
            json.dumps({"ok": False, "exit_code": c.EXIT_UNAUTHENTICATED, "error": {"code": c.TOKEN_INVALID, "message": "expired"}}),
            stop_ev,
        )
        self.assertFalse(bridge.auth_status()["data"]["auth_verified"])

        # Queue overflow (deque maxlen=2000) when evaluate_js returns False preserves newest 2000 events
        for seq in range(1, 2006):
            bridge._enqueue_and_push({"seq": seq, "type": "task.updated"})
        first_half = bridge.poll_events(limit=1000)["data"]
        second_half = bridge.poll_events(limit=1000)["data"]
        self.assertTrue(first_half["has_more"])
        self.assertFalse(second_half["has_more"])
        drained = first_half["items"] + second_half["items"]
        self.assertEqual(len(drained), 2000)
        self.assertEqual(drained[0]["seq"], 6)
        self.assertEqual(drained[-1]["seq"], 2005)

        # Wakeup signaled during _probe_once() is not cleared afterward
        probes: List[int] = []
        second_probe = threading.Event()

        def fake_probe() -> bool:
            n = len(probes) + 1
            probes.append(n)
            if n == 1:
                bridge._wake_event.set()
            elif n >= 2:
                second_probe.set()
            return False

        bridge._stop_event.clear()
        with mock.patch.object(bridge, "_probe_once", side_effect=fake_probe):
            t = threading.Thread(target=bridge._supervisor_loop, daemon=True)
            t.start()
            self.assertTrue(second_probe.wait(timeout=1.5), f"Expected immediate re-probe, got {probes}")
            bridge._stop_event.set()
            bridge._wake_event.set()
            t.join(timeout=1.5)

    def test_mesh_socket_env_and_direct_handle_desktop_explicit_home(self) -> None:
        import mesh.client as _client_mod
        from argparse import Namespace
        from mesh.cli.commands.desktop import handle_desktop

        custom_sock = os.path.join(self.home, "custom.sock")
        default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
        prev_explicit = _client_mod.EXPLICIT_HOME
        try:
            # 1. Default home + unset MESH_HOME + EXPLICIT_HOME=False -> MESH_SOCKET applies identically to bridge & MeshClient
            _client_mod.EXPLICIT_HOME = False
            env_patch = {"MESH_SOCKET": custom_sock}
            with mock.patch.dict(os.environ, env_patch, clear=False):
                os.environ.pop("MESH_HOME", None)
                bridge_def = DesktopBridge(home=default_home, push_enabled=False)
                self.addCleanup(bridge_def.close)
                self.assertEqual(bridge_def._resolve_endpoint(), bridge_def._build_client(0.5).socket_path)
                self.assertEqual(bridge_def._resolve_endpoint(), custom_sock)

            # 2. Explicit --home -> ignores MESH_SOCKET and uses <home>/run/mesh.sock
            with mock.patch.dict(os.environ, env_patch, clear=False):
                info = handle_desktop(Namespace(home=self.home, check=True, dry_run=False))
                self.assertTrue(_client_mod.EXPLICIT_HOME)
                self.assertEqual(info["home"], self.home)
                bridge_exp = DesktopBridge(home=self.home, push_enabled=False)
                self.addCleanup(bridge_exp.close)
                self.assertEqual(bridge_exp._resolve_endpoint(), bridge_exp._build_client(0.5).socket_path)
                self.assertNotEqual(bridge_exp._resolve_endpoint(), custom_sock)
        finally:
            _client_mod.EXPLICIT_HOME = prev_explicit

    @unittest.skipUnless(_has_webview_and_display(), "requires pywebview and an active graphical display")
    def test_optional_pywebview_module_import(self) -> None:
        import importlib

        webview = importlib.import_module("webview")
        self.assertTrue(hasattr(webview, "create_window"))


class TestDesktopBridgeSecurityAndLifecycleRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-desk-reg-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.ctrl = _FakeDaemonController()

    def test_clear_passphrase_resets_last_seq_and_pending_events(self) -> None:
        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            stream_poll_timeout_s=0.05,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        self.ctrl.next_stream_script = [
            {"seq": 42, "type": "task.created", "project_id": "prj_1"},
            TimeoutError("idle"),
        ]
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        self.assertTrue(self.ctrl.stream_opened.wait(timeout=2.0))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if bridge.connection_state()["data"]["last_seq"] >= 42:
                break
            time.sleep(0.01)
        self.assertEqual(bridge.connection_state()["data"]["last_seq"], 42)
        self.assertTrue(len(bridge._pending_events) >= 1)

        bridge.clear_passphrase()
        self.assertEqual(bridge.connection_state()["data"]["last_seq"], 0)
        self.assertIsNone(bridge._last_seq)
        # Private daemon events (seq=42) are cleared; only the synthetic _desktop.connection lock signal remains
        polled_after_lock = bridge.poll_events()["data"]["items"]
        self.assertEqual(len(polled_after_lock), 1, polled_after_lock)
        lock_ev = polled_after_lock[0]
        self.assertEqual(lock_ev.get("type"), "_desktop.connection")
        self.assertTrue(lock_ev.get("auth_locked"))
        self.assertFalse((lock_ev.get("connection") or {}).get("auth_verified"))
        self.assertNotIn("seq", lock_ev)
        self.assertEqual(bridge.poll_events()["data"]["items"], [])

        # Re-unlock must NOT send stale since=42
        self.ctrl.stream_opened.clear()
        self.ctrl.next_stream_script = [TimeoutError("idle")]
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        self.assertTrue(self.ctrl.stream_opened.wait(timeout=2.0))
        sock2 = self.ctrl.stream_sockets[-1]
        deadline = time.monotonic() + 2.0
        while not sock2.sent_payloads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(sock2.sent_payloads)
        self.assertNotIn("since", sock2.sent_payloads[0].get("args") or {})

    def test_tray_clear_passphrase_pushes_connection_lock_event_to_webview(self) -> None:
        """Round 3 P2-3: tray-initiated clear_passphrase pushes _desktop.connection(auth_locked=True) to WebView."""
        pushed_scripts: List[str] = []

        class _FakeWindow:
            def evaluate_js(self, script: str) -> bool:
                pushed_scripts.append(script)
                return True

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            stream_poll_timeout_s=0.05,
            push_enabled=True,
        )
        self.addCleanup(bridge.close)
        bridge.attach_window(_FakeWindow())

        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        pushed_scripts.clear()

        # Simulate Windows tray menu calling clear_passphrase(forget_stored=False) from a background thread
        done = threading.Event()
        res_box: List[Dict[str, Any]] = []

        def _tray_lock() -> None:
            res_box.append(bridge.clear_passphrase(forget_stored=False))
            done.set()

        t = threading.Thread(target=_tray_lock, daemon=True)
        t.start()
        self.assertTrue(done.wait(timeout=3.0))
        self.assertTrue(res_box and res_box[0].get("ok"))

        self.assertTrue(pushed_scripts, "Expected clear_passphrase to push _desktop.connection to WebView")
        joined = "\n".join(pushed_scripts)
        self.assertIn("window.__meshOnEvents", joined)
        self.assertIn('"_desktop.connection"', joined)
        self.assertIn('"auth_locked":true', joined)
        self.assertIn('"auth_verified":false', joined)
        # Already pushed cleanly, so poll queue is drained
        self.assertEqual(bridge.poll_events()["data"]["items"], [])

    def test_bounded_stream_connect_timeout_and_no_send_after_stop(self) -> None:
        connect_entered = threading.Event()
        allow_connect_return = threading.Event()
        sock_holder: List[_FakeStreamSocket] = []

        def slow_connect_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)
            orig_connect = base._connect_socket

            def _wrapped_connect(connect_timeout: Optional[float] = None) -> _FakeStreamSocket:
                with self.ctrl.lock:
                    self.ctrl.connect_timeouts.append(connect_timeout)
                sock = _FakeStreamSocket([TimeoutError("idle")])
                sock_holder.append(sock)
                connect_entered.set()
                allow_connect_return.wait(timeout=3.0)
                return sock

            base._connect_socket = _wrapped_connect  # type: ignore[method-assign]
            return base

        bridge = DesktopBridge(
            home=self.home,
            client_factory=slow_connect_client,
            probe_timeout_s=1.25,
            stream_poll_timeout_s=0.05,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        with bridge._lock:
            bridge._passphrase = self.ctrl.passphrase
            bridge._auth_verified = True

        res = bridge.start_event_stream()
        self.assertTrue(res["ok"])
        self.assertTrue(connect_entered.wait(timeout=2.0))
        self.assertEqual(self.ctrl.connect_timeouts[0], 1.25)

        # Clear passphrase in another thread while _connect_socket is still blocked
        cleared = threading.Event()

        def _do_clear() -> None:
            bridge.clear_passphrase()
            cleared.set()

        t_clear = threading.Thread(target=_do_clear, daemon=True)
        t_clear.start()
        time.sleep(0.05)
        allow_connect_return.set()
        t_clear.join(timeout=3.0)
        self.assertTrue(cleared.is_set())

        sock = sock_holder[0]
        self.assertEqual(sock.sent_payloads, [], "Cleared passphrase must never be sent after stop")
        self.assertTrue(sock.closed, "Socket opened before stop must be closed cleanly")

        # Verify bounded probe timeout is active for sendall and poll timeout applied before recv
        timeouts_at_send: List[Optional[float]] = []

        class _OrderCheckSocket(_FakeStreamSocket):
            def sendall(self, data: bytes) -> None:
                timeouts_at_send.append(self.timeout_value)
                super().sendall(data)

        def order_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            def _connect(t: Optional[float] = None) -> _FakeStreamSocket:
                s = _OrderCheckSocket([TimeoutError("idle")])
                s.settimeout(t)
                sock_holder.append(s)
                self.ctrl.stream_opened.set()
                return s

            base._connect_socket = _connect  # type: ignore[method-assign]
            return base

        bridge2 = DesktopBridge(
            home=self.home,
            client_factory=order_client,
            probe_timeout_s=1.75,
            stream_poll_timeout_s=0.12,
            push_enabled=False,
        )
        self.addCleanup(bridge2.close)
        self.ctrl.stream_opened.clear()
        self.assertTrue(bridge2.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        self.assertTrue(self.ctrl.stream_opened.wait(timeout=2.0))
        s2 = sock_holder[-1]
        deadline = time.monotonic() + 2.0
        while not s2.sent_payloads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(timeouts_at_send, [1.75])
        deadline = time.monotonic() + 2.0
        while s2.timeout_value != 0.12 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(s2.timeout_value, 0.12)

    def test_request_stop_is_non_deadlocking_while_evaluate_js_blocks(self) -> None:
        eval_entered = threading.Event()
        allow_eval_return = threading.Event()
        eval_calls: List[str] = []

        class _GuiDeadlockWindow:
            def evaluate_js(self, script: str) -> bool:
                eval_calls.append(script)
                eval_entered.set()
                allow_eval_return.wait(timeout=3.0)
                return True

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            push_enabled=True,
        )
        win = _GuiDeadlockWindow()
        bridge.attach_window(win)
        with bridge._lock:
            bridge._passphrase = self.ctrl.passphrase
            bridge._auth_verified = True

        # Start push on a worker thread that blocks inside evaluate_js (simulating GUI thread stall)
        push_thread = threading.Thread(
            target=lambda: bridge._enqueue_and_push({"seq": 100, "type": "task.created"}),
            daemon=True,
        )
        push_thread.start()
        self.assertTrue(eval_entered.wait(timeout=2.0))

        # request_stop() from the "GUI thread" must return immediately without waiting for push_thread
        t0 = time.monotonic()
        bridge.request_stop()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, f"request_stop blocked for {elapsed:.3f}s")
        self.assertIsNone(bridge._window)
        self.assertIsNone(bridge._passphrase)

        # Queue another event while _window is cleared -> must NOT call evaluate_js
        bridge._enqueue_and_push({"seq": 101, "type": "task.updated"})
        allow_eval_return.set()
        push_thread.join(timeout=2.0)
        bridge.close()
        self.assertEqual(len(eval_calls), 1, "No further evaluate_js calls allowed after request_stop")

    def test_stop_event_stream_retains_thread_ref_and_closes_pipe_on_timeout(self) -> None:
        from mesh.platform import ipc as plat_ipc

        class _FakePipeClientStream(plat_ipc.PipeClientStream):
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        fake_pipe = _FakePipeClientStream()
        unblock_thread = threading.Event()

        def stuck_loop() -> None:
            unblock_thread.wait(timeout=3.0)

        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        with bridge._lock:
            bridge._passphrase = self.ctrl.passphrase
            bridge._auth_verified = True
        t = threading.Thread(target=stuck_loop, daemon=True)
        t.start()
        stop_ev = threading.Event()
        with bridge._lock:
            bridge._stream_thread = t
            bridge._stream_stop_event = stop_ev
            bridge._stream_sock = fake_pipe

        with mock.patch.object(t, "join", return_value=None):
            bridge.stop_event_stream()

        self.assertTrue(stop_ev.is_set())
        self.assertEqual(fake_pipe.close_calls, 1, "Windows PipeClientStream must be closed after join timeout")
        # Thread reference must be retained while the old thread is still alive
        self.assertIs(bridge._stream_thread, t)
        self.assertIs(bridge._stream_stop_event, stop_ev)
        # Starting stream while old thread is still winding down must NOT spawn a duplicate thread
        res = bridge.start_event_stream()
        self.assertTrue(res["ok"])
        self.assertFalse(res["data"]["streaming"])
        self.assertTrue(res["data"]["stopping"])
        self.assertIs(bridge._stream_thread, t)

        unblock_thread.set()
        t.join(timeout=2.0)
        bridge.stop_event_stream()
        self.assertIsNone(bridge._stream_thread)
        self.assertIsNone(bridge._stream_stop_event)
        self.assertIsNone(bridge._stream_sock)

    def test_detect_and_ensure_webview2_runtime_with_fake_winreg(self) -> None:
        from mesh.desktop import app as desktop_app

        class _FakeKey:
            def __init__(self, pv: Any, *, use_ctx: bool = True) -> None:
                self.pv = pv
                self.use_ctx = use_ctx
                self.closed = False

            def __enter__(self) -> "_FakeKey":
                if not self.use_ctx:
                    raise AttributeError("no ctx")
                return self

            def __exit__(self, *_args: Any) -> None:
                self.closed = True

        class _FakeWinreg:
            HKEY_LOCAL_MACHINE = 0x80000002
            HKEY_CURRENT_USER = 0x80000001

            def __init__(self, mapping: Dict[Tuple[int, str], Any]) -> None:
                self.mapping = mapping
                self.opened: List[Tuple[int, str]] = []
                self.closed_keys: List[_FakeKey] = []

            def OpenKey(self, hive: int, subkey: str) -> _FakeKey:
                self.opened.append((hive, subkey))
                if (hive, subkey) not in self.mapping:
                    raise FileNotFoundError(subkey)
                return self.mapping[(hive, subkey)]

            def QueryValueEx(self, key: _FakeKey, name: str) -> Tuple[Any, int]:
                if name != "pv" or key.pv is None:
                    raise FileNotFoundError(name)
                return key.pv, 1

            def CloseKey(self, key: _FakeKey) -> None:
                key.closed = True
                self.closed_keys.append(key)

        guid = desktop_app.WEBVIEW2_CLIENT_GUID
        paths = [
            (_FakeWinreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (_FakeWinreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
            (_FakeWinreg.HKEY_CURRENT_USER, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
            (_FakeWinreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        ]

        # 1. Missing or invalid pv (empty / 0.0.0.0) -> None and MeshError with zh-CN + link
        for bad_map in (
            {},
            {paths[0]: _FakeKey("")},
            {paths[1]: _FakeKey("   ")},
            {paths[2]: _FakeKey("0.0.0.0")},
            {paths[3]: _FakeKey(None)},
        ):
            reg = _FakeWinreg(bad_map)
            self.assertIsNone(desktop_app.detect_webview2_runtime(winreg_mod=reg))
            with self.assertRaises(MeshError) as ctx:
                desktop_app.ensure_webview2_runtime(winreg_mod=reg)
            self.assertIn("WebView2", ctx.exception.message)
            self.assertIn(desktop_app.WEBVIEW2_DOWNLOAD_URL, ctx.exception.message)

        # 2. Valid pv in each of the 4 hives/subkeys succeeds (including fallback after 0.0.0.0)
        for hive, subkey in paths:
            reg = _FakeWinreg({paths[0]: _FakeKey("0.0.0.0"), (hive, subkey): _FakeKey("130.0.2849.68")})
            if (hive, subkey) == paths[0]:
                reg = _FakeWinreg({(hive, subkey): _FakeKey("130.0.2849.68")})
            self.assertEqual(desktop_app.ensure_webview2_runtime(winreg_mod=reg), "130.0.2849.68")

        # 3. launch_desktop on win32 checks WebView2 BEFORE _import_webview() so pywebview never alters stderr
        fake_webview = mock.MagicMock()
        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app, "_import_webview", return_value=fake_webview) as mock_imp,
            mock.patch.dict(sys.modules, {"winreg": _FakeWinreg({})}),
        ):
            with self.assertRaises(MeshError) as ctx_launch:
                desktop_app.launch_desktop(home=self.home)
        self.assertIn(desktop_app.WEBVIEW2_DOWNLOAD_URL, ctx_launch.exception.message)
        self.assertIn(desktop_app.WEBVIEW2_DOWNLOAD_URL, ctx_launch.exception.message.splitlines())
        mock_imp.assert_not_called()
        fake_webview.create_window.assert_not_called()
        fake_webview.start.assert_not_called()

    def test_launch_desktop_binds_request_stop_and_inspects_webview_start_signature(self) -> None:
        from mesh.desktop import app as desktop_app

        events_bound: List[Any] = []

        class _FakeEventSlot:
            def __iadd__(self, fn: Any) -> "_FakeEventSlot":
                events_bound.append(fn)
                return self

        class _FakeEvents:
            closed = _FakeEventSlot()

        class _FakeWindow:
            events = _FakeEvents()

            def evaluate_js(self, _s: str) -> bool:
                return True

        start_calls: List[Dict[str, Any]] = []

        class _FakeWebviewNoHttpServer:
            @staticmethod
            def create_window(**_kw: Any) -> _FakeWindow:
                return _FakeWindow()

            @staticmethod
            def start(*, debug: bool = False, gui: Optional[str] = None) -> None:
                start_calls.append({"debug": debug, "gui": gui})
                # Simulate unrelated TypeError inside start -> must NOT retry!
                raise TypeError("internal webview start failure")

        with (
            mock.patch.object(desktop_app, "_import_webview", return_value=_FakeWebviewNoHttpServer),
            mock.patch.object(desktop_app, "ensure_webview2_runtime", return_value="130.0.0.0"),
            mock.patch("mesh.desktop.app.DesktopBridge.start"),
            mock.patch("mesh.desktop.app.DesktopBridge.close") as mock_close,
        ):
            with self.assertRaisesRegex(TypeError, "internal webview start failure"):
                desktop_app.launch_desktop(home=self.home)

        self.assertEqual(len(start_calls), 1, "Signature check must avoid retrying webview.start on internal TypeError")
        self.assertEqual(len(events_bound), 1)
        mock_close.assert_called_once()

        # Verify closed event callback delegates to bridge.request_stop (not bridge.close)
        start_calls.clear()
        events_bound.clear()

        class _FakeWebviewWithHttpServer:
            @staticmethod
            def create_window(**_kw: Any) -> _FakeWindow:
                return _FakeWindow()

            @staticmethod
            def start(*, debug: bool = False, http_server: bool = True, gui: Optional[str] = None) -> None:
                start_calls.append({"debug": debug, "http_server": http_server, "gui": gui})
                events_bound[0]()

        with (
            mock.patch.object(desktop_app, "_import_webview", return_value=_FakeWebviewWithHttpServer),
            mock.patch.object(desktop_app, "ensure_webview2_runtime", return_value="130.0.0.0"),
            mock.patch("mesh.desktop.app.DesktopBridge.start"),
            mock.patch("mesh.desktop.app.DesktopBridge.request_stop") as mock_req_stop,
            mock.patch("mesh.desktop.app.DesktopBridge.close") as mock_close2,
        ):
            res = desktop_app.launch_desktop(home=self.home)

        self.assertEqual(res["status"], "closed")
        self.assertEqual(start_calls[0]["http_server"], False)
        mock_req_stop.assert_called_once()
        mock_close2.assert_called_once()

    def test_frameless_window_creation_and_sealed_window_controls(self) -> None:
        import types
        from mesh.desktop import app as desktop_app

        created_kw: List[Dict[str, Any]] = []

        class _FakeControlWindow:
            def __init__(self) -> None:
                self.actions: List[str] = []
                self.events = types.SimpleNamespace(
                    closed=self._slot("closed"),
                    maximized=self._slot("maximized"),
                    restored=self._slot("restored"),
                )
                self._callbacks: Dict[str, List[Any]] = {"closed": [], "maximized": [], "restored": []}

            def _slot(self, name: str) -> Any:
                owner = self

                class _Slot:
                    def __iadd__(self, fn: Any) -> "_Slot":
                        owner._callbacks[name].append(fn)
                        return self

                return _Slot()

            def minimize(self) -> None:
                self.actions.append("minimize")

            def maximize(self) -> None:
                self.actions.append("maximize")
                for cb in self._callbacks["maximized"]:
                    cb()

            def restore(self) -> None:
                self.actions.append("restore")
                for cb in self._callbacks["restored"]:
                    cb()

            def destroy(self) -> None:
                self.actions.append("destroy")

            def evaluate_js(self, _s: str) -> bool:
                return True

        # 1. Sealed DesktopJsApi window control methods act only on attached window
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        # Detached window returns clean error envelopes (never raises)
        for method_name in (
            "window_minimize",
            "window_toggle_maximize",
            "window_restore_for_drag",
            "window_drag_move",
            "window_drag_end",
            "window_resize_end",
            "window_close",
        ):
            fn = getattr(api, method_name)
            self.assertEqual(getattr(fn, "__globals__"), {"__builtins__": {}})
            res = fn()
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], c.INTERNAL_ERROR)
        resize_start_fn = getattr(api, "window_resize_start")
        self.assertEqual(getattr(resize_start_fn, "__globals__"), {"__builtins__": {}})
        resize_start_detached = api.window_resize_start("se")
        self.assertFalse(resize_start_detached["ok"])
        self.assertEqual(resize_start_detached["error"]["code"], c.INTERNAL_ERROR)
        resize_fn = getattr(api, "window_resize")
        self.assertEqual(getattr(resize_fn, "__globals__"), {"__builtins__": {}})
        resize_detached = api.window_resize(1200, 800, "se")
        self.assertFalse(resize_detached["ok"])
        self.assertEqual(resize_detached["error"]["code"], c.INTERNAL_ERROR)
        self.assertFalse(hasattr(api, "_window"))
        self.assertFalse(hasattr(api, "_set_window_maximized"))
        self.assertFalse(hasattr(api, "_on_window_shown"))
        self.assertFalse(hasattr(api, "_on_window_resized"))
        self.assertFalse(hasattr(api, "_on_window_maximized"))

        # Attached window executes minimize / maximize / restore / close
        win = _FakeControlWindow()
        bridge.attach_window(win)

        min_res = api.window_minimize()
        self.assertTrue(min_res["ok"])
        self.assertTrue(min_res["data"]["minimized"])

        max_res = api.window_toggle_maximize()
        self.assertTrue(max_res["ok"])
        self.assertTrue(max_res["data"]["maximized"])

        rest_res = api.window_toggle_maximize()
        self.assertTrue(rest_res["ok"])
        self.assertFalse(rest_res["data"]["maximized"])

        close_res = api.window_close()
        self.assertTrue(close_res["ok"])
        self.assertTrue(close_res["data"]["closing"])
        self.assertEqual(win.actions, ["minimize", "maximize", "restore", "destroy"])

        # After request_stop detaches the window, controls safely refuse
        bridge.request_stop()
        self.assertFalse(api.window_minimize()["ok"])

        # 2. launch_desktop creates a frameless window with easy_drag disabled and wires maximize/restore hooks
        win2 = _FakeControlWindow()

        class _FakeWebviewFrameless:
            @staticmethod
            def create_window(**kw: Any) -> _FakeControlWindow:
                created_kw.append(dict(kw))
                return win2

            @staticmethod
            def start(*, debug: bool = False, http_server: bool = True, gui: Optional[str] = None) -> None:
                # Exercise js_api window controls while window is attached
                js_api = created_kw[0]["js_api"]
                self.assertTrue(js_api.window_toggle_maximize()["data"]["maximized"])
                self.assertFalse(js_api.window_toggle_maximize()["data"]["maximized"])
                for cb in win2._callbacks["closed"]:
                    cb()

        with (
            mock.patch.object(desktop_app, "_import_webview", return_value=_FakeWebviewFrameless),
            mock.patch.object(desktop_app, "ensure_webview2_runtime", return_value="130.0.0.0"),
            mock.patch("mesh.desktop.app.DesktopBridge.start"),
        ):
            desktop_app.launch_desktop(home=self.home)

        self.assertEqual(len(created_kw), 1)
        self.assertIs(created_kw[0]["frameless"], True)
        self.assertIs(created_kw[0]["easy_drag"], False)
        self.assertEqual(created_kw[0]["background_color"], "#0b0b0c")
        self.assertEqual(win2.actions, ["maximize", "restore"])

    def test_window_toggle_maximize_ignores_static_maximized_false_attribute(self) -> None:
        class _StaticMaximizedWindow:
            # Mirrors pywebview 6.2.1 Window.maximized (static creation-time flag, stays False)
            maximized = False

            def __init__(self) -> None:
                self.actions: List[str] = []

            def maximize(self) -> None:
                self.actions.append("maximize")

            def restore(self) -> None:
                self.actions.append("restore")

        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        win = _StaticMaximizedWindow()
        bridge.attach_window(win)

        results = [api.window_toggle_maximize() for _ in range(4)]
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual([r["data"]["maximized"] for r in results], [True, False, True, False])
        self.assertEqual(win.actions, ["maximize", "restore", "maximize", "restore"])

        # Native maximized/restored events keep internal state synchronized
        bridge._set_window_maximized(True)
        res_after_event = api.window_toggle_maximize()
        self.assertTrue(res_after_event["ok"])
        self.assertFalse(res_after_event["data"]["maximized"])
        self.assertEqual(win.actions[-1], "restore")

    def test_win32_maximize_uses_monitor_work_area_and_restores_bounds(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        class _Win32BoundsWindow:
            maximized = False

            def __init__(self, native: Any = None, *, x: int = 120, y: int = 90, w: int = 1280, h: int = 760) -> None:
                self.x = x
                self.y = y
                self.width = w
                self.height = h
                self.min_size = (1040, 680)
                self.native = native
                self.calls: List[Tuple[str, Tuple[int, ...]]] = []

            def move(self, x: int, y: int) -> None:
                self.x = x
                self.y = y
                self.calls.append(("move", (x, y)))

            def resize(self, width: int, height: int) -> None:
                self.width = width
                self.height = height
                self.calls.append(("resize", (width, height)))

            def minimize(self) -> None:
                self.calls.append(("minimize", ()))

            def maximize(self) -> None:
                self.calls.append(("maximize", ()))

            def restore(self) -> None:
                self.calls.append(("restore", ()))

        class _FakeWin32User32:
            def __init__(
                self,
                *,
                dpi: int = 96,
                monitor_rect: Tuple[int, int, int, int] = (0, 0, 1600, 1200),
                work_rect: Tuple[int, int, int, int] = (0, 0, 1600, 1160),
                window_rect: Tuple[int, int, int, int] = (120, 90, 1400, 850),
                cursor_pos: Tuple[int, int] = (800, 18),
            ) -> None:
                self.dpi = dpi
                self.monitor_rect = monitor_rect
                self.work_rect = work_rect
                self.window_rect = window_rect  # (left, top, right, bottom)
                self.cursor_pos = cursor_pos
                self.iconic = False
                self.zoomed = False
                self.mfw_calls: List[Tuple[Any, int]] = []
                self.spi_calls: int = 0
                self.swp_calls: List[Tuple[int, int, int, int, int, int]] = []
                self.show_calls: List[Tuple[int, int]] = []

            def MonitorFromWindow(self, hwnd: Any, flags: int) -> int:
                self.mfw_calls.append((hwnd, flags))
                return 0x999

            def GetMonitorInfoW(self, hmon: Any, mi_ptr: Any) -> int:
                self.assert_hmon = hmon
                mi = mi_ptr._obj
                mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom = self.monitor_rect
                mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom = self.work_rect
                return 1

            def GetWindowRect(self, _hwnd: Any, rect_ptr: Any) -> int:
                r = rect_ptr._obj
                r.left, r.top, r.right, r.bottom = self.window_rect
                return 1

            def SetWindowPos(
                self,
                hwnd: int,
                _insert_after: int,
                x: int,
                y: int,
                w: int,
                h: int,
                flags: int,
            ) -> int:
                self.swp_calls.append((hwnd, x, y, w, h, flags))
                cur_l, cur_t, cur_r, cur_b = self.window_rect
                cur_w = cur_r - cur_l
                cur_h = cur_b - cur_t
                new_l = cur_l if (flags & 0x0002) else x
                new_t = cur_t if (flags & 0x0002) else y
                new_w = cur_w if (flags & 0x0001) else w
                new_h = cur_h if (flags & 0x0001) else h
                self.window_rect = (new_l, new_t, new_l + new_w, new_t + new_h)
                return 1

            def GetDpiForWindow(self, _hwnd: Any) -> int:
                return self.dpi

            def IsIconic(self, _hwnd: Any) -> int:
                return 1 if self.iconic else 0

            def IsZoomed(self, _hwnd: Any) -> int:
                return 1 if self.zoomed else 0

            def ShowWindow(self, hwnd: int, cmd: int) -> int:
                self.show_calls.append((hwnd, cmd))
                if cmd == 9:
                    self.iconic = False
                    self.zoomed = False
                return 1

            def GetCursorPos(self, pt_ptr: Any) -> int:
                pt = pt_ptr._obj
                pt.x, pt.y = self.cursor_pos
                return 1

            def SystemParametersInfoW(self, *_args: Any) -> int:
                self.spi_calls += 1
                return 0

        windll_calls: List[Tuple[str, bool]] = []

        def _make_windll_factory(dll_obj: Any) -> Any:
            def _factory(name: str, use_last_error: bool = False) -> Any:
                windll_calls.append((name, use_last_error))
                return dll_obj

            return _factory

        # 1. 100% DPI (96), 150% DPI (144, runtime DPI change), and secondary monitor offset at 150% DPI:
        #    SetWindowPos uses physical rcWork directly and max -> minimize -> taskbar restore -> toggle preserves bounds.
        for dpi, work_rect, init_rect in (
            (96, (0, 0, 1600, 1160), (120, 90, 1400, 850)),
            (144, (0, 0, 1600, 1140), (100, 80, 1460, 940)),
            (144, (1920, 0, 4480, 1380), (2100, 120, 3540, 1020)),
        ):
            windll_calls.clear()
            fake_handle = types.SimpleNamespace(ToInt64=lambda: 0x12345)
            win_mon = _Win32BoundsWindow(native=types.SimpleNamespace(Handle=fake_handle))
            user32 = _FakeWin32User32(
                dpi=dpi,
                monitor_rect=(0, 0, 1600, 1200),
                work_rect=work_rect,
                window_rect=init_rect,
            )
            bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
            self.addCleanup(bridge.close)
            api = DesktopJsApi(bridge)
            bridge.attach_window(win_mon)

            expected_wa = (
                work_rect[0],
                work_rect[1],
                work_rect[2] - work_rect[0],
                work_rect[3] - work_rect[1],
            )
            expected_init = (
                init_rect[0],
                init_rect[1],
                init_rect[2] - init_rect[0],
                init_rect[3] - init_rect[1],
            )

            sentinel_shared_windll = mock.MagicMock(side_effect=AssertionError("shared windll must not be used"))
            with (
                mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
                mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
                mock.patch.object(ctypes, "WinDLL", _make_windll_factory(user32), create=True),
                mock.patch.object(ctypes, "windll", sentinel_shared_windll, create=True),
            ):
                # Step A: Maximize uses SetWindowPos in physical pixels (never pywebview move/resize!)
                r_max = api.window_toggle_maximize()
                self.assertTrue(r_max["ok"], r_max)
                self.assertTrue(r_max["data"]["maximized"])
                self.assertEqual(
                    r_max["data"]["work_area"],
                    {"x": expected_wa[0], "y": expected_wa[1], "width": expected_wa[2], "height": expected_wa[3]},
                )
                self.assertEqual(user32.window_rect, work_rect)
                self.assertEqual(win_mon.calls, [], "Direct Win32 SetWindowPos path must not mix pywebview move/resize")
                self.assertEqual(bridge._window_restore_bounds, expected_init)

                # Native maximized event while bridge-maximized must NOT overwrite restore bounds
                bridge._set_window_maximized(True)
                self.assertEqual(bridge._window_restore_bounds, expected_init)

                # Step B: Minimize -> simulate taskbar restore firing window.events.restored
                r_min = api.window_minimize()
                self.assertTrue(r_min["ok"])
                user32.iconic = True
                # Simulate taskbar restore unminimizing the window with slightly drifted bounds
                user32.iconic = False
                user32.window_rect = (10, 10, 1500, 1100)
                bridge._on_window_restored()

                # Restored event MUST preserve maximized state & restore bounds and re-snap to rcWork
                self.assertTrue(bridge._window_maximized)
                self.assertTrue(bridge._window_bridge_maximized)
                self.assertEqual(bridge._window_restore_bounds, expected_init)
                self.assertEqual(user32.window_rect, work_rect)

                # Step C: Next toggle restores to the exact saved physical bounds
                r_rest = api.window_toggle_maximize()
                self.assertTrue(r_rest["ok"], r_rest)
                self.assertFalse(r_rest["data"]["maximized"])
                self.assertEqual(user32.window_rect, init_rect)
                self.assertFalse(bridge._window_maximized)
                self.assertIsNone(bridge._window_restore_bounds)

                # Step D: Subsequent maximize -> titlebar drag restore under cursor -> drag move
                r_max_again = api.window_toggle_maximize()
                self.assertTrue(r_max_again["ok"])
                self.assertEqual(user32.window_rect, work_rect)

                cx0 = expected_wa[0] + 900
                cy0 = expected_wa[1] + 24
                user32.cursor_pos = (cx0, cy0)
                r_drag_rest = api.window_restore_for_drag(0.5, 18)
                self.assertTrue(r_drag_rest["ok"], r_drag_rest)
                self.assertFalse(r_drag_rest["data"]["maximized"])
                self.assertTrue(r_drag_rest["data"]["restored"])
                b = r_drag_rest["data"]["bounds"]
                self.assertEqual((b["width"], b["height"]), (expected_init[2], expected_init[3]))
                # Window positioned under cursor (cx0 at 50% width)
                self.assertEqual(b["x"], cx0 - expected_init[2] // 2)
                self.assertGreaterEqual(b["y"], expected_wa[1])
                self.assertLessEqual(b["y"], cy0)

                # Continue dragging: cursor moves (+100, +50) -> window moves (+100, +50)
                user32.cursor_pos = (cx0 + 100, cy0 + 50)
                r_drag_mv = api.window_drag_move(100, 50)
                self.assertTrue(r_drag_mv["ok"])
                self.assertTrue(r_drag_mv["data"]["moved"])
                self.assertEqual(r_drag_mv["data"]["x"], b["x"] + 100)
                self.assertEqual(r_drag_mv["data"]["y"], b["y"] + 50)

                # Calling window_restore_for_drag while NOT maximized is a safe no-op
                r_noop = api.window_restore_for_drag(0.5, 18)
                self.assertTrue(r_noop["ok"])
                self.assertFalse(r_noop["data"]["restored"])

                # Step E: DPI-aware physical resize on Win32 (150% DPI scales logical -> physical)
                user32.window_rect = (200, 150, 200 + int(round(1200 * dpi / 96.0)), 150 + int(round(800 * dpi / 96.0)))
                prev_right = user32.window_rect[2]
                r_res = api.window_resize(1100, 750, "w")
                self.assertTrue(r_res["ok"], r_res)
                scale = dpi / 96.0
                expected_phys_w = int(round(1100 * scale))
                expected_phys_h = int(round(750 * scale))
                self.assertEqual(user32.window_rect[2] - user32.window_rect[0], expected_phys_w)
                self.assertEqual(user32.window_rect[3] - user32.window_rect[1], expected_phys_h)
                self.assertEqual(user32.window_rect[2], prev_right, "West edge resize must anchor right edge in physical px")

            self.assertTrue(all(call == ("user32", True) for call in windll_calls))
            self.assertEqual(user32.mfw_calls[0], (0x12345, 2))
            self.assertEqual(user32.spi_calls, 0)
            sentinel_shared_windll.user32.assert_not_called()

        # 2. SPI_GETWORKAREA fallback when window.native HWND is unavailable
        win_spi = _Win32BoundsWindow(native=None)

        class _FakeUser32Spi:
            def __init__(self) -> None:
                self.spi_actions: List[int] = []

            def SystemParametersInfoW(self, action: int, _param: int, rect_ptr: Any, _winini: int) -> int:
                self.spi_actions.append(action)
                r = rect_ptr._obj
                r.left, r.top, r.right, r.bottom = 0, 0, 1920, 1040
                return 1

        user32_spi = _FakeUser32Spi()
        bridge_spi = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge_spi.close)
        api_spi = DesktopJsApi(bridge_spi)
        bridge_spi.attach_window(win_spi)
        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", _make_windll_factory(user32_spi), create=True),
        ):
            r_max2 = api_spi.window_toggle_maximize()
            self.assertTrue(r_max2["ok"])
            self.assertTrue(r_max2["data"]["maximized"])
            self.assertEqual((win_spi.x, win_spi.y, win_spi.width, win_spi.height), (0, 0, 1920, 1040))
            r_rest2 = api_spi.window_toggle_maximize()
            self.assertTrue(r_rest2["ok"])
            self.assertFalse(r_rest2["data"]["maximized"])
            self.assertEqual((win_spi.x, win_spi.y, win_spi.width, win_spi.height), (120, 90, 1280, 760))
        self.assertEqual(user32_spi.spi_actions, [0x0030])

    def test_window_resize_clamps_to_min_size_and_anchors_edges(self) -> None:
        class _ResizableWindow:
            def __init__(self) -> None:
                self.x = 200
                self.y = 150
                self.width = 1360
                self.height = 860
                self.min_size = (1040, 680)
                self.ops: List[Tuple[str, Tuple[int, int]]] = []

            def move(self, x: int, y: int) -> None:
                self.x = x
                self.y = y
                self.ops.append(("move", (x, y)))

            def resize(self, width: int, height: int) -> None:
                self.width = width
                self.height = height
                self.ops.append(("resize", (width, height)))

        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        win = _ResizableWindow()
        bridge.attach_window(win)
        bridge._set_window_maximized(True)

        # 1. SE drag below min_size clamps to (1040, 680) without moving origin and clears maximized state
        res_se = api.window_resize(800, 500, "se")
        self.assertTrue(res_se["ok"], res_se)
        self.assertEqual(res_se["data"], {"width": 1040, "height": 680, "edge": "se", "maximized": False})
        self.assertEqual((win.x, win.y, win.width, win.height), (200, 150, 1040, 680))
        self.assertEqual(win.ops, [("resize", (1040, 680))])
        self.assertFalse(bridge._window_maximized)

        # 2. West edge drag keeps right edge (200 + 1040 = 1240) anchored
        win.ops.clear()
        res_w = api.window_resize(1240, 680, "w")
        self.assertTrue(res_w["ok"], res_w)
        self.assertEqual((win.x, win.y, win.width, win.height), (0, 150, 1240, 680))
        self.assertEqual(win.x + win.width, 1240)
        self.assertEqual(win.ops, [("move", (0, 150)), ("resize", (1240, 680))])

        # 3. North-west drag keeps bottom-right corner (1240, 830) anchored
        win.ops.clear()
        res_nw = api.window_resize(1140, 780, "nw")
        self.assertTrue(res_nw["ok"], res_nw)
        self.assertEqual((win.x, win.y, win.width, win.height), (100, 50, 1140, 780))
        self.assertEqual((win.x + win.width, win.y + win.height), (1240, 830))
        self.assertEqual(win.ops, [("move", (100, 50)), ("resize", (1140, 780))])

        # 4. Invalid dimensions or edge rejected cleanly
        for bad_args in (
            (True, 700, None),
            (float("nan"), 700, None),
            ("1200", 700, None),
            (1200, 700, "diagonal"),
            (1200, 700, 123),
        ):
            bad_res = api.window_resize(*bad_args)  # type: ignore[arg-type]
            self.assertFalse(bad_res["ok"], f"Expected rejection for {bad_args!r}")
            self.assertEqual(bad_res["error"]["code"], c.INVALID_ARGUMENT)

    def test_webview2_missing_shows_win32_messagebox_when_stderr_is_none(self) -> None:
        import ctypes
        import types
        from mesh.desktop import app as desktop_app

        msgbox_calls: List[Tuple[Any, str, str, int]] = []
        windll_calls: List[Tuple[str, bool]] = []

        def fake_msgbox(hwnd: Any, text: str, caption: str, flags: int) -> int:
            msgbox_calls.append((hwnd, text, caption, flags))
            return 1

        def fake_windll_factory(name: str, use_last_error: bool = False) -> Any:
            windll_calls.append((name, use_last_error))
            return types.SimpleNamespace(MessageBoxW=fake_msgbox)

        class _StrictCp1252Stream:
            encoding = "cp1252"

            def __init__(self) -> None:
                self.chunks: List[str] = []

            def write(self, s: str) -> int:
                # Raises UnicodeEncodeError if any zh-CN / non-cp1252 char is written!
                s.encode("cp1252")
                self.chunks.append(s)
                return len(s)

            def flush(self) -> None:
                pass

            def getvalue(self) -> str:
                return "".join(self.chunks)

        missing_err = MeshError(
            code=c.INTERNAL_ERROR,
            exit_code=c.EXIT_INTERNAL,
            message=(
                "未检测到 Microsoft Edge WebView2 Runtime（桌面端禁止静默回退到 MSHTML）。\n\n"
                "请先下载并安装 WebView2 Runtime（可按 Ctrl+C 复制以下链接）：\n"
                f"{desktop_app.WEBVIEW2_DOWNLOAD_URL}"
            ),
            details={"missing_runtime": "webview2", "install_url": desktop_app.WEBVIEW2_DOWNLOAD_URL},
        )

        # 1. Headless pythonw/gui-script (sys.stderr is None) on win32 -> native MessageBoxW popup
        #    Even if pywebview replaces sys.stderr with a cp1252 NUL stream during launch_desktop!
        cp1252_nul = _StrictCp1252Stream()

        def _launch_that_clobbers_stderr(**_kw: Any) -> Any:
            desktop_app.sys.stderr = cp1252_nul  # simulate pywebview import replacing sys.stderr=None
            raise missing_err

        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stderr", None),
            mock.patch.object(desktop_app, "launch_desktop", side_effect=_launch_that_clobbers_stderr),
            mock.patch.object(desktop_app, "append_desktop_log"),
            mock.patch.object(ctypes, "WinDLL", fake_windll_factory, create=True),
        ):
            rc = desktop_app.main(["--home", self.home])

        self.assertEqual(rc, c.EXIT_INTERNAL)
        self.assertEqual(cp1252_nul.getvalue(), "", "No-console mode must never write to pywebview's fake cp1252 stderr")
        self.assertEqual(windll_calls, [("user32", True)])
        self.assertEqual(len(msgbox_calls), 1)
        hwnd, text, caption, flags = msgbox_calls[0]
        self.assertEqual(hwnd, 0)
        self.assertIn("WebView2", text)
        self.assertIn(desktop_app.WEBVIEW2_DOWNLOAD_URL, text.splitlines())
        self.assertEqual(caption, "Agent Mesh")
        self.assertEqual(flags, 0x10)

        # 2. Console launch on win32 with a strict cp1252 stderr -> ASCII-safe JSON (no UnicodeEncodeError), no MessageBoxW
        msgbox_calls.clear()
        cp1252_console = _StrictCp1252Stream()
        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stderr", cp1252_console),
            mock.patch.object(desktop_app, "launch_desktop", side_effect=missing_err),
            mock.patch.object(ctypes, "WinDLL", fake_windll_factory, create=True),
        ):
            rc_console = desktop_app.main(["--home", self.home])

        self.assertEqual(rc_console, c.EXIT_INTERNAL)
        self.assertEqual(msgbox_calls, [])
        payload = json.loads(cp1252_console.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["details"]["missing_runtime"], "webview2")
        self.assertIn("未检测到 Microsoft Edge WebView2 Runtime", payload["error"]["message"])

    def test_start_daemon_uninitialized_home_returns_specific_code_and_zh_hint(self) -> None:
        def fake_fail_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "exit_code": c.EXIT_INTERNAL,
                        "error": {
                            "code": c.INTERNAL_ERROR,
                            "message": "Daemon process exited immediately with returncode 5",
                            "details": {},
                        },
                    }
                ),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            subprocess_runner=fake_fail_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        # 1. Missing mesh.db -> specific HOME_NOT_INITIALIZED code + zh-CN setup hint (N5 includes --home for non-default)
        res_uninit = bridge.start_daemon()
        self.assertFalse(res_uninit["ok"])
        self.assertEqual(res_uninit["error"]["code"], "HOME_NOT_INITIALIZED")
        self.assertEqual(res_uninit["error"]["exit_code"], c.EXIT_NOT_FOUND)
        expected_hint = f'mesh --home "{bridge.home}" setup'
        self.assertIn(expected_hint, res_uninit["error"]["message"])
        self.assertNotIn("mesh init", res_uninit["error"]["message"])
        self.assertIn("尚未初始化", res_uninit["error"]["message"])
        self.assertEqual(res_uninit["error"]["details"]["hint"], expected_hint)

        # 2. When mesh.db exists, preserve the underlying CLI error unchanged
        with open(os.path.join(self.home, "mesh.db"), "wb") as fp:
            fp.write(b"")
        res_init = bridge.start_daemon()
        self.assertFalse(res_init["ok"])
        self.assertEqual(res_init["error"]["code"], c.INTERNAL_ERROR)

    def _make_mock_win32_user32(
        self,
        *,
        dpi: int = 96,
        system_dpi: Optional[int] = None,
        monitor_rect: Tuple[int, int, int, int] = (0, 0, 1600, 1200),
        work_rect: Tuple[int, int, int, int] = (0, 0, 1600, 1160),
        window_rect: Tuple[int, int, int, int] = (120, 90, 1400, 850),
        normal_rect: Optional[Tuple[int, int, int, int]] = None,
        cursor_pos: Tuple[int, int] = (800, 18),
        style: int = 0x16010000,
        on_show_restore: Optional[Any] = None,
        on_swp: Optional[Any] = None,
    ) -> Any:
        class _MockUser32:
            def __init__(self) -> None:
                self.dpi = dpi
                self.system_dpi = dpi if system_dpi is None else system_dpi
                self.monitor_rect = monitor_rect
                self.work_rect = work_rect
                self.window_rect = window_rect
                self.normal_rect = normal_rect
                self.cursor_pos = cursor_pos
                self.style = style
                self.iconic = False
                self.zoomed = False
                self.swp_calls: List[Tuple[int, int, int, int, int, int]] = []
                self.show_calls: List[Tuple[int, int]] = []
                self.get_wl_calls: List[Tuple[int, int]] = []
                self.set_wl_calls: List[Tuple[int, int, int]] = []

            def MonitorFromWindow(self, _hwnd: Any, _flags: int) -> int:
                return 0x777

            def GetMonitorInfoW(self, _hmon: Any, mi_ptr: Any) -> int:
                mi = mi_ptr._obj
                mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom = self.monitor_rect
                mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom = self.work_rect
                return 1

            def SystemParametersInfoW(self, action: int, _param: int, rect_ptr: Any, _winini: int) -> int:
                if action != 0x0030:
                    return 0
                r = rect_ptr._obj
                r.left, r.top, r.right, r.bottom = self.work_rect
                return 1

            def GetWindowRect(self, _hwnd: Any, rect_ptr: Any) -> int:
                r = rect_ptr._obj
                r.left, r.top, r.right, r.bottom = self.window_rect
                return 1

            def SetWindowPos(
                self,
                hwnd: int,
                _insert_after: int,
                x: int,
                y: int,
                w: int,
                h: int,
                flags: int,
            ) -> int:
                self.swp_calls.append((hwnd, x, y, w, h, flags))
                cur_l, cur_t, cur_r, cur_b = self.window_rect
                cur_w = cur_r - cur_l
                cur_h = cur_b - cur_t
                new_l = cur_l if (flags & 0x0002) else x
                new_t = cur_t if (flags & 0x0002) else y
                new_w = cur_w if (flags & 0x0001) else w
                new_h = cur_h if (flags & 0x0001) else h
                self.window_rect = (new_l, new_t, new_l + new_w, new_t + new_h)
                if callable(on_swp):
                    on_swp(hwnd, new_l, new_t, new_w, new_h, flags)
                return 1

            def GetDpiForWindow(self, _hwnd: Any) -> int:
                return self.dpi

            def GetDpiForSystem(self) -> int:
                return self.system_dpi

            def GetWindowLongW(self, hwnd: int, index: int) -> int:
                self.get_wl_calls.append((hwnd, index))
                return self.style

            def SetWindowLongW(self, hwnd: int, index: int, new_style: int) -> int:
                prev = self.style
                self.set_wl_calls.append((hwnd, index, new_style))
                self.style = new_style
                return prev

            def IsIconic(self, _hwnd: Any) -> int:
                return 1 if self.iconic else 0

            def IsZoomed(self, _hwnd: Any) -> int:
                return 1 if self.zoomed else 0

            def ShowWindow(self, hwnd: int, cmd: int) -> int:
                self.show_calls.append((hwnd, cmd))
                if cmd == 9:
                    self.iconic = False
                    self.zoomed = False
                    if self.normal_rect is not None:
                        self.window_rect = self.normal_rect
                    if callable(on_show_restore):
                        on_show_restore()
                return 1

            def GetCursorPos(self, pt_ptr: Any) -> int:
                pt = pt_ptr._obj
                pt.x, pt.y = self.cursor_pos
                return 1

        return _MockUser32()

    def test_win32_native_maximize_converts_to_work_area_and_pushes_state(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        pushed_scripts: List[str] = []
        pushed_ev = threading.Event()

        class _NativeMaxWindow:
            min_size = (1040, 680)

            def __init__(self, hwnd_val: int) -> None:
                self.native = types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: hwnd_val))

            def evaluate_js(self, script: str) -> bool:
                pushed_scripts.append(script)
                pushed_ev.set()
                return True

        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=True)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        win = _NativeMaxWindow(0x22222)
        bridge.attach_window(win)

        # Simulate Win+Up: Windows zooms frameless window to full monitor (0, 0, 1600, 1200) covering taskbar.
        # ShowWindow(SW_RESTORE) restores normal_rect and synchronously triggers restored event.
        user32 = self._make_mock_win32_user32(
            dpi=96,
            monitor_rect=(0, 0, 1600, 1200),
            work_rect=(0, 0, 1600, 1160),
            window_rect=(0, 0, 1600, 1200),
            normal_rect=(120, 90, 1400, 850),
            on_show_restore=lambda: bridge._on_window_restored(),
        )
        user32.zoomed = True

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            bridge._on_window_maximized()
            self.assertTrue(pushed_ev.wait(timeout=2.0), "Expected _desktop.window_state push to evaluate_js")

            self.assertFalse(user32.zoomed)
            self.assertEqual(user32.show_calls, [(0x22222, 9)])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            self.assertTrue(bridge._window_maximized)
            self.assertTrue(bridge._window_bridge_maximized)
            self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))
            self.assertTrue(
                any('"_desktop.window_state"' in s and '"maximized":true' in s for s in pushed_scripts),
                f"Expected maximized=true push in {pushed_scripts!r}",
            )

            # Pressing Win+Up AGAIN while already bridge-maximized must re-snap to work area
            # without clobbering the saved normal restore bounds.
            user32.zoomed = True
            user32.window_rect = (0, 0, 1600, 1200)
            user32.normal_rect = (0, 0, 1600, 1160)
            bridge._on_window_maximized()
            self.assertFalse(user32.zoomed)
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))

            # Max button toggle restores to the exact pre-Win+Up bounds
            r_rest = api.window_toggle_maximize()
            self.assertTrue(r_rest["ok"], r_rest)
            self.assertFalse(r_rest["data"]["maximized"])
            self.assertEqual(user32.window_rect, (120, 90, 1400, 850))

            # Fallback when ShowWindow does not restore window_rect: uses last_normal_bounds, never full-screen
            user32.normal_rect = None
            user32.zoomed = True
            user32.window_rect = (0, 0, 1600, 1200)
            bridge._on_window_maximized()
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))

    def test_win32_initial_geometry_clamped_to_work_area_dpi_aware(self) -> None:
        import ctypes
        import types
        import mesh.desktop.app as desktop_app
        import mesh.desktop.bridge as bridge_mod

        # 1. Pre-creation DPI-aware geometry calculation
        for dpi, work_rect, expected in (
            (96, (0, 0, 1600, 1160), (1360, 860, (1040, 680))),
            (144, (0, 0, 1600, 1140), (1066, 760, (1040, 680))),
            (144, (0, 0, 1280, 660), (853, 440, (853, 440))),
        ):
            user32 = self._make_mock_win32_user32(dpi=dpi, system_dpi=dpi, work_rect=work_rect)
            with (
                mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
                mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
                mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
            ):
                self.assertEqual(bridge_mod.compute_initial_window_geometry(), expected)

        # 2. launch_desktop passes clamped initial size/min_size to webview.create_window and clamps on shown
        created_kw: List[Dict[str, Any]] = []
        shown_cbs: List[Any] = []

        class _ShownSlot:
            def __iadd__(self, fn: Any) -> "_ShownSlot":
                shown_cbs.append(fn)
                return self

        class _FakeShownWindow:
            def __init__(self) -> None:
                self.native = types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x33333))
                self.events = types.SimpleNamespace(
                    closed=_ShownSlot(),
                    shown=_ShownSlot(),
                    maximized=_ShownSlot(),
                    restored=_ShownSlot(),
                    resized=_ShownSlot(),
                )

        # Simulate Devin's exact 150% DPI initial bug: 1604x1170 at (76, 76) on 1600x1140 work area
        user32_150 = self._make_mock_win32_user32(
            dpi=144,
            system_dpi=144,
            monitor_rect=(0, 0, 1600, 1200),
            work_rect=(0, 0, 1600, 1140),
            window_rect=(76, 76, 76 + 1604, 76 + 1170),
            style=0x16030000,
        )

        class _FakeWebviewClamp:
            @staticmethod
            def create_window(**kw: Any) -> _FakeShownWindow:
                created_kw.append(dict(kw))
                return _FakeShownWindow()

            @staticmethod
            def start(**_kw: Any) -> None:
                # Fire shown event (index 1 in registration order: closed, shown, maximized, restored, resized)
                shown_cbs[1]()

        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32_150, create=True),
            mock.patch.object(desktop_app, "ensure_webview2_runtime", return_value="130.0.0.0"),
            mock.patch.object(desktop_app, "_import_webview", return_value=_FakeWebviewClamp),
            mock.patch("mesh.desktop.app.DesktopBridge.load_saved_window_bounds", return_value=None),
            mock.patch("mesh.desktop.app.DesktopBridge.start"),
        ):
            desktop_app.launch_desktop(home=self.home)

        self.assertEqual(created_kw[0]["width"], 1066)
        self.assertEqual(created_kw[0]["height"], 760)
        self.assertEqual(created_kw[0]["min_size"], (1040, 680))
        # Physical window clamped strictly inside rcWork (0, 0, 1600, 1140)
        self.assertEqual(user32_150.window_rect, (0, 0, 1600, 1140))

        # 3. Window already inside work area needs no redundant SetWindowPos on shown
        user32_ok = self._make_mock_win32_user32(
            dpi=96,
            work_rect=(0, 0, 1600, 1160),
            window_rect=(120, 90, 1400, 850),
            style=0x16030000,
        )
        bridge_ok = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge_ok.close)
        bridge_ok.attach_window(_FakeShownWindow())
        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32_ok, create=True),
        ):
            bridge_ok._on_window_shown()
        self.assertEqual(user32_ok.swp_calls, [])
        self.assertEqual(bridge_ok._window_last_normal_bounds, (120, 90, 1280, 760))

    def test_win32_scale_change_while_maximized_reapplies_work_area(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        win = types.SimpleNamespace(
            native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x44444)),
            min_size=(1040, 680),
        )
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        bridge.attach_window(win)

        # Simulate synchronous resized re-entry inside SetWindowPos to verify loop guard
        user32 = self._make_mock_win32_user32(
            dpi=96,
            monitor_rect=(0, 0, 1600, 1200),
            work_rect=(0, 0, 1600, 1160),
            window_rect=(120, 90, 1400, 850),
            on_swp=lambda *_a: bridge._on_window_resized(1600, 1140),
        )

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            self.assertTrue(api.window_toggle_maximize()["ok"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            swp_before = len(user32.swp_calls)

            # Switch scale 100% -> 150%: taskbar grows, rcWork shrinks to (0, 0, 1600, 1140)
            user32.dpi = 144
            user32.work_rect = (0, 0, 1600, 1140)
            bridge._on_window_resized(1600, 1160)

            self.assertEqual(user32.window_rect, (0, 0, 1600, 1140))
            self.assertEqual(bridge._window_maximized_bounds, (0, 0, 1600, 1140))
            self.assertEqual(len(user32.swp_calls), swp_before + 1, "Re-entry must not cause resize loop")

            # Subsequent resized event when already matching rcWork is a no-op
            bridge._on_window_resized(1600, 1140)
            self.assertEqual(len(user32.swp_calls), swp_before + 1)

            # Minimized (iconic) window must NOT be touched on resized
            user32.iconic = True
            user32.window_rect = (-32000, -32000, -31840, -31972)
            bridge._on_window_resized(160, 28)
            self.assertEqual(len(user32.swp_calls), swp_before + 1)

    def test_window_drag_anchor_cleared_at_drag_end_and_rejects_inactive_move(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        win = types.SimpleNamespace(
            native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x55555)),
            min_size=(1040, 680),
        )
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        bridge.attach_window(win)

        user32 = self._make_mock_win32_user32(
            dpi=96,
            work_rect=(0, 0, 1600, 1160),
            window_rect=(120, 90, 1400, 850),
            cursor_pos=(900, 24),
        )

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            # 1. Reject window_drag_move when no drag is active
            res_idle = api.window_drag_move(10, 20)
            self.assertFalse(res_idle["ok"])
            self.assertEqual(res_idle["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(user32.swp_calls, [])

            # 2. Maximize -> restore_for_drag -> drag_move works
            self.assertTrue(api.window_toggle_maximize()["ok"])
            r_rest = api.window_restore_for_drag(0.5, 18)
            self.assertTrue(r_rest["ok"] and r_rest["data"]["restored"])
            b = r_rest["data"]["bounds"]

            user32.cursor_pos = (1000, 74)
            r_mv = api.window_drag_move(100, 50)
            self.assertTrue(r_mv["ok"] and r_mv["data"]["moved"])
            self.assertEqual((r_mv["data"]["x"], r_mv["data"]["y"]), (b["x"] + 100, b["y"] + 50))
            rect_after_drag = user32.window_rect
            swp_count = len(user32.swp_calls)

            # 3. End drag clears anchor and active flag
            r_end = api.window_drag_end()
            self.assertTrue(r_end["ok"])
            self.assertEqual(r_end["data"], {"ended": True, "was_active": True})
            self.assertFalse(bridge._window_drag_active)
            self.assertIsNone(bridge._window_drag_anchor)

            # 4. Subsequent window_drag_move with cursor far away must be rejected and NEVER teleport
            user32.cursor_pos = (10, 10)
            r_stale = api.window_drag_move(0, 0)
            self.assertFalse(r_stale["ok"])
            self.assertEqual(r_stale["error"]["code"], c.INVALID_TRANSITION)
            self.assertEqual(user32.window_rect, rect_after_drag)
            self.assertEqual(len(user32.swp_calls), swp_count)

            # 5. Idempotent second window_drag_end reports was_active=False
            r_end2 = api.window_drag_end()
            self.assertTrue(r_end2["ok"])
            self.assertEqual(r_end2["data"], {"ended": True, "was_active": False})

    def test_win32_shown_adds_ws_minimizebox_style_for_taskbar_minimize(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        win = types.SimpleNamespace(
            native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x66666)),
            min_size=(1040, 680),
        )
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        bridge.attach_window(win)

        # Frameless style with WS_MAXIMIZEBOX (0x00010000) but missing WS_MINIMIZEBOX (0x00020000)
        user32 = self._make_mock_win32_user32(
            dpi=96,
            work_rect=(0, 0, 1600, 1160),
            window_rect=(120, 90, 1400, 850),
            style=0x16010000,
        )

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            bridge._on_window_shown()

            self.assertEqual(user32.get_wl_calls, [(0x66666, -16)])
            self.assertEqual(user32.set_wl_calls, [(0x66666, -16, 0x16030000)])
            self.assertTrue(user32.style & 0x00020000, "WS_MINIMIZEBOX must be set")
            self.assertTrue(user32.style & 0x00010000, "WS_MAXIMIZEBOX must be preserved")
            self.assertEqual(len(user32.swp_calls), 1)
            self.assertEqual(user32.swp_calls[0][5], 0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020)

            # Second shown call is a no-op once WS_MINIMIZEBOX is already set
            bridge._on_window_shown()
            self.assertEqual(len(user32.set_wl_calls), 1)
            self.assertEqual(len(user32.swp_calls), 1)

    def test_startup_generic_exception_shows_messagebox_when_no_console(self) -> None:
        import ctypes
        import types
        from mesh.desktop import app as desktop_app

        msgbox_calls: List[Tuple[Any, str, str, int]] = []

        def fake_msgbox(hwnd: Any, text: str, caption: str, flags: int) -> int:
            msgbox_calls.append((hwnd, text, caption, flags))
            return 1

        class _StrictCp1252Stream:
            def __init__(self) -> None:
                self.chunks: List[str] = []

            def write(self, s: str) -> int:
                s.encode("cp1252")
                self.chunks.append(s)
                return len(s)

            def flush(self) -> None:
                pass

            def getvalue(self) -> str:
                return "".join(self.chunks)

        # 1. No-console (sys.stderr is None) generic RuntimeError -> MessageBoxW dialog, exit code 1
        cp1252_nul = _StrictCp1252Stream()

        def _boom_no_console(**_kw: Any) -> Any:
            desktop_app.sys.stderr = cp1252_nul
            raise RuntimeError("unexpected WebView2 COM failure")

        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stderr", None),
            mock.patch.object(desktop_app, "launch_desktop", side_effect=_boom_no_console),
            mock.patch.object(
                ctypes,
                "WinDLL",
                lambda *_a, **_kw: types.SimpleNamespace(MessageBoxW=fake_msgbox),
                create=True,
            ),
        ):
            rc = desktop_app.main(["--home", self.home])

        self.assertEqual(rc, c.EXIT_INTERNAL)
        self.assertEqual(cp1252_nul.getvalue(), "")
        self.assertEqual(len(msgbox_calls), 1)
        hwnd, text, caption, flags = msgbox_calls[0]
        self.assertEqual(hwnd, 0)
        self.assertEqual(caption, "Agent Mesh")
        self.assertEqual(flags, 0x10)
        self.assertIn("RuntimeError", text)
        self.assertIn("unexpected WebView2 COM failure", text)

        # 2. Console launch generic exception -> ASCII-safe JSON to stderr, no MessageBoxW
        msgbox_calls.clear()
        cp1252_console = _StrictCp1252Stream()
        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stderr", cp1252_console),
            mock.patch.object(desktop_app, "launch_desktop", side_effect=OSError("初始化失败")),
            mock.patch.object(
                ctypes,
                "WinDLL",
                lambda *_a, **_kw: types.SimpleNamespace(MessageBoxW=fake_msgbox),
                create=True,
            ),
        ):
            rc2 = desktop_app.main(["--home", self.home])

        self.assertEqual(rc2, c.EXIT_INTERNAL)
        self.assertEqual(msgbox_calls, [])
        payload = json.loads(cp1252_console.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], c.INTERNAL_ERROR)
        self.assertEqual(payload["error"]["details"]["exception_type"], "OSError")
        self.assertIn("初始化失败", payload["error"]["message"])

    def test_win32_physical_cursor_resize_eliminates_150pct_rounding_drift(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        win = types.SimpleNamespace(
            native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x77777)),
            min_size=(1040, 680),
        )
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        bridge.attach_window(win)

        # 150% DPI: physical window (200, 150, 2000, 1350) -> 1800x1200 physical (1200x800 logical)
        # min_size (1040, 680) -> physical min (1560, 1020)
        user32 = self._make_mock_win32_user32(
            dpi=144,
            work_rect=(0, 0, 2560, 1380),
            window_rect=(200, 150, 2000, 1350),
            cursor_pos=(201, 600),
        )

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            # Invalid edge rejected cleanly
            bad_start = api.window_resize_start("diagonal")
            self.assertFalse(bad_start["ok"])
            self.assertEqual(bad_start["error"]["code"], c.INVALID_ARGUMENT)

            # 1. West edge drag at 150% DPI: exact physical delta (-137px), right edge anchored at 2000,
            #    orthogonal height remains EXACTLY 1200 even when CSS height input is rounded/shifted (801).
            r_start = api.window_resize_start("w")
            self.assertTrue(r_start["ok"] and r_start["data"]["physical"])
            user32.cursor_pos = (64, 623)  # dx_phys = -137, dy_phys = +23
            r_w = api.window_resize(1290, 801, "w")
            self.assertTrue(r_w["ok"], r_w)
            self.assertEqual(user32.window_rect, (63, 150, 2000, 1350))
            self.assertEqual(
                r_w["data"]["physical_bounds"],
                {"x": 63, "y": 150, "width": 1937, "height": 1200},
            )
            self.assertEqual(api.window_resize_end()["data"], {"ended": True, "was_active": True})
            self.assertIsNone(bridge._window_resize_anchor)

            # 2. East edge drag: exact +73px width, height unchanged (1200), x unchanged (200)
            user32.window_rect = (200, 150, 2000, 1350)
            user32.cursor_pos = (1999, 500)
            self.assertTrue(api.window_resize_start("e")["ok"])
            user32.cursor_pos = (2072, 480)
            self.assertTrue(api.window_resize(1250, 799, "e")["ok"])
            self.assertEqual(user32.window_rect, (200, 150, 2073, 1350))
            self.assertTrue(api.window_resize_end()["ok"])

            # 3. South edge drag: exact -45px height, width unchanged (1800)
            user32.window_rect = (200, 150, 2000, 1350)
            user32.cursor_pos = (800, 1349)
            self.assertTrue(api.window_resize_start("s")["ok"])
            user32.cursor_pos = (819, 1304)
            self.assertTrue(api.window_resize(1201, 770, "s")["ok"])
            self.assertEqual(user32.window_rect, (200, 150, 2000, 1305))
            self.assertTrue(api.window_resize_end()["ok"])

            # 4. North edge drag: exact -50px top, bottom anchored at 1350, width unchanged (1800)
            user32.window_rect = (200, 150, 2000, 1350)
            user32.cursor_pos = (800, 151)
            self.assertTrue(api.window_resize_start("n")["ok"])
            user32.cursor_pos = (790, 101)
            self.assertTrue(api.window_resize(1199, 833, "n")["ok"])
            self.assertEqual(user32.window_rect, (200, 100, 2000, 1350))
            self.assertTrue(api.window_resize_end()["ok"])

            # 5. North-west corner drag below physical min_size (1560x1020) anchors bottom-right (2000, 1350)
            user32.window_rect = (200, 150, 2000, 1350)
            user32.cursor_pos = (200, 150)
            self.assertTrue(api.window_resize_start("nw")["ok"])
            user32.cursor_pos = (700, 550)
            self.assertTrue(api.window_resize(700, 400, "nw")["ok"])
            self.assertEqual(user32.window_rect, (440, 330, 2000, 1350))
            self.assertEqual(user32.window_rect[2] - user32.window_rect[0], 1560)
            self.assertEqual(user32.window_rect[3] - user32.window_rect[1], 1020)
            self.assertTrue(api.window_resize_end()["ok"])

    def test_win32_sync_resized_during_restore_and_drag_restore_preserves_final_bounds(self) -> None:
        import ctypes
        import types
        import mesh.desktop.bridge as bridge_mod

        win = types.SimpleNamespace(
            native=types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 0x88888)),
            min_size=(1040, 680),
        )
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)
        bridge.attach_window(win)

        # Every SetWindowPos synchronously fires _on_window_resized with the new (w, h),
        # and ShowWindow(SW_RESTORE) synchronously fires _on_window_restored.
        user32 = self._make_mock_win32_user32(
            dpi=96,
            monitor_rect=(0, 0, 1600, 1200),
            work_rect=(0, 0, 1600, 1160),
            window_rect=(120, 90, 1400, 850),
            cursor_pos=(800, 18),
            on_show_restore=lambda: bridge._on_window_restored(),
            on_swp=lambda _hwnd, _x, _y, w, h, _flags: bridge._on_window_resized(w, h),
        )

        with (
            mock.patch("mesh.desktop.bridge.sys.platform", "win32"),
            mock.patch.object(bridge_mod, "_WIN32_USER32_CACHE", None),
            mock.patch.object(ctypes, "WinDLL", lambda *_a, **_kw: user32, create=True),
        ):
            # 1. 100% DPI: Maximize -> Toggle Restore -> Re-Maximize -> Toggle Restore
            r_max1 = api.window_toggle_maximize()
            self.assertTrue(r_max1["ok"] and r_max1["data"]["maximized"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            self.assertEqual(len(user32.swp_calls), 1)
            self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))
            self.assertFalse(bridge._window_reapplying_max)

            r_rest1 = api.window_toggle_maximize()
            self.assertTrue(r_rest1["ok"] and not r_rest1["data"]["maximized"])
            self.assertEqual(user32.window_rect, (120, 90, 1400, 850))
            self.assertEqual(len(user32.swp_calls), 2, "Synchronous resized must not snap restored window back")
            self.assertFalse(bridge._window_maximized)
            self.assertFalse(bridge._window_bridge_maximized)
            self.assertFalse(bridge._window_restoring)

            # Second maximize must still record the normal bounds, not the work area
            r_max2 = api.window_toggle_maximize()
            self.assertTrue(r_max2["ok"] and r_max2["data"]["maximized"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))

            r_rest2 = api.window_toggle_maximize()
            self.assertTrue(r_rest2["ok"] and not r_rest2["data"]["maximized"])
            self.assertEqual(user32.window_rect, (120, 90, 1400, 850))

            # 2. 150% DPI: Maximize -> Toggle Restore
            user32.dpi = 144
            user32.work_rect = (0, 0, 1600, 1140)
            user32.window_rect = (80, 60, 1520, 1020)
            self.assertTrue(api.window_toggle_maximize()["ok"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1140))
            swp_before = len(user32.swp_calls)
            r_rest_150 = api.window_toggle_maximize()
            self.assertTrue(r_rest_150["ok"] and not r_rest_150["data"]["maximized"])
            self.assertEqual(user32.window_rect, (80, 60, 1520, 1020))
            self.assertEqual(len(user32.swp_calls), swp_before + 1)

            # 3. Win+Up native maximize -> Toggle Restore with synchronous resized firing
            user32.dpi = 96
            user32.work_rect = (0, 0, 1600, 1160)
            user32.normal_rect = (120, 90, 1400, 850)
            user32.window_rect = (0, 0, 1600, 1200)
            user32.zoomed = True
            bridge._on_window_maximized()
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            r_rest_winup = api.window_toggle_maximize()
            self.assertTrue(r_rest_winup["ok"] and not r_rest_winup["data"]["maximized"])
            self.assertEqual(user32.window_rect, (120, 90, 1400, 850))

            # 4. Maximize -> window_restore_for_drag -> window_drag_move with synchronous resized firing
            self.assertTrue(api.window_toggle_maximize()["ok"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            user32.cursor_pos = (900, 24)
            swp_before = len(user32.swp_calls)
            r_drag = api.window_restore_for_drag(0.5, 18)
            self.assertTrue(r_drag["ok"] and r_drag["data"]["restored"])
            b = r_drag["data"]["bounds"]
            self.assertEqual((b["width"], b["height"]), (1280, 760))
            self.assertEqual(
                user32.window_rect,
                (b["x"], b["y"], b["x"] + 1280, b["y"] + 760),
                "Drag-restore must keep restored size instead of snapping back to work area",
            )
            self.assertEqual(len(user32.swp_calls), swp_before + 1)
            self.assertFalse(bridge._window_maximized)
            self.assertFalse(bridge._window_restoring)
            self.assertTrue(bridge._window_drag_active)

            user32.cursor_pos = (1000, 74)
            r_mv = api.window_drag_move(100, 50)
            self.assertTrue(r_mv["ok"] and r_mv["data"]["moved"])
            self.assertEqual(
                user32.window_rect,
                (b["x"] + 100, b["y"] + 50, b["x"] + 100 + 1280, b["y"] + 50 + 760),
            )
            self.assertTrue(api.window_drag_end()["ok"])

            # 5. window_resize while bridge-maximized must not snap back to work area
            user32.window_rect = (120, 90, 1400, 850)
            self.assertTrue(api.window_toggle_maximize()["ok"])
            self.assertEqual(user32.window_rect, (0, 0, 1600, 1160))
            swp_before = len(user32.swp_calls)
            r_rz = api.window_resize(1100, 720, "se")
            self.assertTrue(r_rz["ok"] and not r_rz["data"]["maximized"])
            self.assertEqual(user32.window_rect, (0, 0, 1100, 720))
            self.assertEqual(len(user32.swp_calls), swp_before + 1)
            self.assertFalse(bridge._window_maximized)
            self.assertFalse(bridge._window_restoring)

            # 6. Failure rollback: SetWindowPos failure preserves maximized state and clears _window_restoring
            user32.window_rect = (120, 90, 1400, 850)
            self.assertTrue(api.window_toggle_maximize()["ok"])
            orig_swp = user32.SetWindowPos
            user32.SetWindowPos = lambda *_a: 0  # type: ignore[method-assign]
            try:
                r_fail_toggle = api.window_toggle_maximize()
                self.assertFalse(r_fail_toggle["ok"])
                self.assertTrue(bridge._window_maximized)
                self.assertTrue(bridge._window_bridge_maximized)
                self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))
                self.assertFalse(bridge._window_restoring)

                r_fail_drag = api.window_restore_for_drag(0.5, 18)
                self.assertFalse(r_fail_drag["ok"])
                self.assertTrue(bridge._window_maximized)
                self.assertTrue(bridge._window_bridge_maximized)
                self.assertEqual(bridge._window_restore_bounds, (120, 90, 1280, 760))
                self.assertFalse(bridge._window_drag_active)
                self.assertFalse(bridge._window_restoring)
            finally:
                user32.SetWindowPos = orig_swp  # type: ignore[method-assign]

    def test_poll_events_and_start_event_stream_validate_int_arguments(self) -> None:
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        for bad_limit in ("a", "", None, True, False, 1.5, float("nan"), float("inf"), {}, []):
            res = api.poll_events(bad_limit)  # type: ignore[arg-type]
            self.assertFalse(res["ok"], f"Expected error for poll_events({bad_limit!r})")
            self.assertEqual(res["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(res["error"]["exit_code"], c.EXIT_USAGE)
            self.assertEqual(res["error"]["details"]["param"], "limit")

        # Valid int, numeric string, and integer-valued float succeed
        self.assertTrue(api.poll_events(5)["ok"])
        self.assertTrue(api.poll_events("5")["ok"])
        self.assertTrue(api.poll_events(10.0)["ok"])

        for bad_since in ("a", "", True, False, 2.5, float("nan"), float("inf"), {}, []):
            res = bridge.start_event_stream(bad_since)  # type: ignore[arg-type]
            self.assertFalse(res["ok"], f"Expected error for start_event_stream({bad_since!r})")
            self.assertEqual(res["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(res["error"]["exit_code"], c.EXIT_USAGE)
            self.assertEqual(res["error"]["details"]["param"], "since")

        # Valid numeric string reaches auth check cleanly without ValueError
        res_unauth = bridge.start_event_stream("12")  # type: ignore[arg-type]
        self.assertFalse(res_unauth["ok"])
        self.assertEqual(res_unauth["error"]["code"], c.TOKEN_MISSING)
        self.assertEqual(bridge._last_seq, 12)

    def test_gui_script_help_and_arg_error_show_messagebox_when_no_console(self) -> None:
        import ctypes
        import types
        from mesh.desktop import app as desktop_app

        msgbox_calls: List[Tuple[Any, str, str, int]] = []

        def fake_msgbox(hwnd: Any, text: str, caption: str, flags: int) -> int:
            msgbox_calls.append((hwnd, text, caption, flags))
            return 1

        # 1. --help without console -> MessageBoxW (MB_ICONINFORMATION) and exit 0
        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stdout", None),
            mock.patch.object(desktop_app.sys, "stderr", None),
            mock.patch.object(
                ctypes,
                "WinDLL",
                lambda *_a, **_kw: types.SimpleNamespace(MessageBoxW=fake_msgbox),
                create=True,
            ),
        ):
            rc_help = desktop_app.main(["--help"])

        self.assertEqual(rc_help, c.EXIT_OK)
        self.assertEqual(len(msgbox_calls), 1)
        hwnd, text, caption, flags = msgbox_calls[0]
        self.assertEqual(hwnd, 0)
        self.assertEqual(caption, "Agent Mesh")
        self.assertEqual(flags, 0x40)
        self.assertIn("mesh-desktop", text)
        self.assertIn("--check", text)

        # 2. Invalid CLI flag without console -> MessageBoxW (MB_ICONERROR) and exit 2
        msgbox_calls.clear()
        with (
            mock.patch.object(desktop_app.sys, "platform", "win32"),
            mock.patch.object(desktop_app.sys, "stdout", None),
            mock.patch.object(desktop_app.sys, "stderr", None),
            mock.patch.object(
                ctypes,
                "WinDLL",
                lambda *_a, **_kw: types.SimpleNamespace(MessageBoxW=fake_msgbox),
                create=True,
            ),
        ):
            rc_err = desktop_app.main(["--definitely-unknown-flag"])

        self.assertEqual(rc_err, c.EXIT_USAGE)
        self.assertEqual(len(msgbox_calls), 1)
        hwnd, text, caption, flags = msgbox_calls[0]
        self.assertEqual(hwnd, 0)
        self.assertEqual(caption, "Agent Mesh")
        self.assertEqual(flags, 0x10)
        self.assertIn("--definitely-unknown-flag", text)


class TestDesktopBridgeAutoStartS1(unittest.TestCase):
    """Unit tests for S1: auto-start meshd, single-flight lock, stopping/backoff, and onboarding state."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="mh-desk-s1-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.ctrl = _FakeDaemonController()

    def _touch_mesh_db(self) -> None:
        with open(os.path.join(self.home, "mesh.db"), "wb") as fp:
            fp.write(b"")

    def test_connection_state_exposes_home_initialized_flag(self) -> None:
        self.ctrl.online = False
        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        st0 = bridge.connection_state()["data"]
        self.assertIs(st0["home_initialized"], False)
        self.assertEqual(st0["daemon_log_path"], os.path.join(bridge.home, "logs", "meshd.log"))

        self._touch_mesh_db()
        st1 = bridge.connection_state()["data"]
        self.assertIs(st1["home_initialized"], True)

    def test_supervisor_pushes_onboarding_required_when_home_uninitialized_without_spawning(self) -> None:
        self.ctrl.online = False
        spawn_calls: List[List[str]] = []

        def fake_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            spawn_calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "running"}}', stderr="")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.05,
            subprocess_runner=fake_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        bridge.start()
        deadline = time.monotonic() + 2.0
        collected: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            collected.extend(bridge.poll_events()["data"]["items"])
            if bridge.connection_state()["data"]["reconnect_attempt"] >= 2 and any(
                ev.get("type") == "_desktop.onboarding_required" for ev in collected
            ):
                break
            time.sleep(0.01)

        onboarding_evs = [ev for ev in collected if ev.get("type") == "_desktop.onboarding_required"]
        self.assertEqual(len(onboarding_evs), 1, f"Expected exactly one onboarding event, got {collected}")
        self.assertIs(onboarding_evs[0]["home_initialized"], False)
        self.assertEqual(onboarding_evs[0]["hint"], f'mesh --home "{bridge.home}" setup')
        self.assertEqual(spawn_calls, [], "Must not spawn mesh daemon start when home is uninitialized")

    def test_supervisor_auto_starts_meshd_when_home_initialized_and_reconnects(self) -> None:
        self._touch_mesh_db()
        self.ctrl.online = False
        recorded: List[Tuple[List[str], Dict[str, Any]]] = []

        def fake_run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded.append((list(argv), dict(kwargs)))
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 4321}}),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.05,
            subprocess_runner=fake_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        with mock.patch.dict(
            os.environ,
            {"MESH_PASSPHRASE": "do-not-leak", "MESH_TOKEN": "tok-no"},
            clear=False,
        ):
            bridge.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if bridge.connection_state()["data"]["connected"]:
                    break
                time.sleep(0.01)

        state = bridge.connection_state()["data"]
        self.assertTrue(state["connected"])
        self.assertIs(state["home_initialized"], True)
        self.assertFalse(state["daemon_starting"])
        self.assertIsNone(state["last_start_error"])
        self.assertEqual(len(recorded), 1)
        self.assertNotIn("MESH_PASSPHRASE", recorded[0][1]["env"])
        self.assertNotIn("MESH_TOKEN", recorded[0][1]["env"])

    def test_start_daemon_single_flight_lock_prevents_duplicate_spawns(self) -> None:
        self._touch_mesh_db()
        self.ctrl.online = False
        started_ev = threading.Event()
        release_ev = threading.Event()
        spawn_count = 0
        count_lock = threading.Lock()

        def blocking_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            with count_lock:
                spawn_count += 1
            started_ev.set()
            release_ev.wait(timeout=3.0)
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 5555}}),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            subprocess_runner=blocking_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        results: List[Dict[str, Any]] = []
        t1 = threading.Thread(target=lambda: results.append(bridge.start_daemon()))
        t1.start()
        self.assertTrue(started_ev.wait(timeout=2.0))
        self.assertTrue(bridge.connection_state()["data"]["daemon_starting"])

        # Concurrent callers must NOT spawn a second subprocess
        res2 = bridge.start_daemon()
        res3 = bridge.start_daemon()
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["data"].get("in_progress"))
        self.assertTrue(res3["ok"])
        self.assertTrue(res3["data"].get("in_progress"))

        release_ev.set()
        t1.join(timeout=3.0)
        self.assertEqual(spawn_count, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertTrue(bridge.connection_state()["data"]["connected"])

    def test_supervisor_handles_daemon_already_running_stopping_then_retries_with_backoff(self) -> None:
        self._touch_mesh_db()
        self.ctrl.online = False
        attempts = 0
        saw_stopping_state = False

        def flaky_stopping_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps(
                        {
                            "ok": False,
                            "exit_code": c.EXIT_INTERNAL,
                            "error": {
                                "code": c.DAEMON_ALREADY_RUNNING,
                                "message": "A previous meshd is still stopping and holds meshd.lock",
                                "details": {"status": "stopping"},
                            },
                        }
                    ),
                    stderr="",
                )
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 7777}}),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03, 0.04),
            probe_interval_s=0.05,
            subprocess_runner=flaky_stopping_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        bridge.start()
        deadline = time.monotonic() + 3.0
        collected: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            collected.extend(bridge.poll_events()["data"]["items"])
            st = bridge.connection_state()["data"]
            if st["daemon_stopping"]:
                saw_stopping_state = True
                self.assertIsNone(st["last_start_error"])
            if st["connected"]:
                break
            time.sleep(0.01)

        final_st = bridge.connection_state()["data"]
        self.assertTrue(final_st["connected"], f"Expected auto-start retry after stopping to succeed; events={collected}")
        self.assertEqual(attempts, 3)
        self.assertFalse(final_st["daemon_stopping"])
        self.assertIsNone(final_st["last_start_error"])
        self.assertTrue(
            saw_stopping_state or any(ev.get("stopping") is True for ev in collected),
            f"Expected stopping status during draining retries, got {collected}",
        )

    def test_supervisor_records_persistent_start_error_and_auto_reconnects_later(self) -> None:
        self._touch_mesh_db()
        self.ctrl.online = False
        spawn_count = 0

        def fail_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "exit_code": c.EXIT_INTERNAL,
                        "error": {
                            "code": c.INTERNAL_ERROR,
                            "message": "Daemon process exited immediately with returncode 1",
                            "details": {"rc": 1},
                        },
                    }
                ),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.05,
            subprocess_runner=fail_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        bridge.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            st = bridge.connection_state()["data"]
            if st["last_start_error"] is not None and st["reconnect_attempt"] >= 2:
                break
            time.sleep(0.01)

        st_err = bridge.connection_state()["data"]
        self.assertFalse(st_err["connected"])
        self.assertIsNotNone(st_err["last_start_error"])
        self.assertEqual(st_err["last_start_error"]["code"], c.INTERNAL_ERROR)
        self.assertEqual(spawn_count, 1, "Failed auto-start must fall back to probe backoff, not spawn-loop")
        self.assertTrue(len(st_err["daemon_diag_log"]) >= 2)

        # Later, daemon becomes reachable externally -> supervisor auto-reconnects and clears error
        with self.ctrl.lock:
            self.ctrl.online = True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if bridge.connection_state()["data"]["connected"]:
                break
            time.sleep(0.01)
        st_ok = bridge.connection_state()["data"]
        self.assertTrue(st_ok["connected"])
        self.assertIsNone(st_ok["last_start_error"])

    def _wait_until(self, predicate: Callable[[], bool], *, timeout: float = 3.0, msg: str = "condition timed out") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.assertTrue(predicate(), msg)

    def test_stop_daemon_prevents_auto_restart_until_manual_start(self) -> None:
        self._touch_mesh_db()
        spawn_count = 0

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 8888}}),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.04,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        bridge.start()
        self._wait_until(
            lambda: bridge.connection_state()["data"]["connected"]
            and bridge.connection_state()["data"]["daemon_status"] is not None,
            msg="Bridge did not connect and populate daemon_status",
        )

        # User explicitly stops daemon
        stop_res = bridge.stop_daemon()
        self.assertTrue(stop_res["ok"])

        # F3: Wait until supervisor clears daemon_stopping and observes offline backoff cycles
        self._wait_until(
            lambda: (
                not bridge.connection_state()["data"]["connected"]
                and not bridge.connection_state()["data"]["daemon_stopping"]
                and bridge.connection_state()["data"]["user_stopped"] is True
                and bridge.connection_state()["data"]["reconnect_attempt"] >= 3
            ),
            msg="Expected user_stopped=True, daemon_stopping=False, and backoff probes without respawn",
        )
        self.assertEqual(spawn_count, 0)

        # Explicit manual start_daemon still works
        start_res = bridge.start_daemon()
        self.assertTrue(start_res["ok"])
        self.assertEqual(spawn_count, 1)
        st = bridge.connection_state()["data"]
        self.assertTrue(st["connected"])
        self.assertFalse(st["user_stopped"])

    def test_f1_same_pid_answering_after_stop_never_auto_restarts_and_clears_stopping(self) -> None:
        """F1 + F3: fake daemon keeps answering daemon.status with the same PID for several probes after stop."""
        self._touch_mesh_db()
        spawn_count = 0
        state_lock = threading.Lock()
        daemon_state: Dict[str, Any] = {"online": True, "pid": 51001, "probes_after_stop": 0, "stop_called": False}

        def make_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            class _LingerClient:
                socket_path = base.socket_path

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    cmd = payload.get("cmd")
                    req_id = payload.get("request_id", "req_f1")
                    if cmd == "daemon.status":
                        with state_lock:
                            if not daemon_state["online"]:
                                raise MeshError(
                                    code=c.DAEMON_UNAVAILABLE,
                                    exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                                    message="Daemon offline",
                                )
                            if daemon_state["stop_called"]:
                                daemon_state["probes_after_stop"] += 1
                                if daemon_state["probes_after_stop"] >= 4:
                                    daemon_state["online"] = False
                                    raise MeshError(
                                        code=c.DAEMON_UNAVAILABLE,
                                        exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                                        message="Daemon finished shutdown",
                                    )
                            pid = daemon_state["pid"]
                        return {
                            "ok": True,
                            "data": {"status": "running", "pid": pid, "uptime_s": 5, "active_runs": 0},
                            "request_id": req_id,
                        }
                    if cmd == "daemon.stop":
                        with state_lock:
                            daemon_state["stop_called"] = True
                            # Intentionally keep online=True for 3 more probes like real meshd!
                        return {"ok": True, "data": {"status": "stopping"}, "request_id": req_id}
                    return base.send_request(payload)

                def _connect_socket(self, timeout: Optional[float] = None) -> Any:
                    return base._connect_socket(timeout)

            return _LingerClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 99999}}), stderr=""
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.025,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        bridge.start()
        self._wait_until(
            lambda: bridge.connection_state()["data"]["connected"]
            and (bridge.connection_state()["data"]["daemon_status"] or {}).get("pid") == 51001,
            msg="Bridge failed to connect to initial daemon",
        )

        # Stop via direct call("daemon.stop") as well as stop_daemon() (F1 item 3)
        stop_res = bridge.call("daemon.stop", {})
        self.assertTrue(stop_res["ok"])

        # Wait until lingering probes finish, daemon goes offline, daemon_stopping clears, and backoff runs
        collected: List[Dict[str, Any]] = []

        def _settled() -> bool:
            collected.extend(bridge.poll_events()["data"]["items"])
            st = bridge.connection_state()["data"]
            return (
                not st["connected"]
                and st["user_stopped"] is True
                and st["daemon_stopping"] is False
                and st["reconnect_attempt"] >= 3
            )

        self._wait_until(_settled, timeout=4.0, msg="Stopping state did not clear cleanly after lingering probes")
        self.assertEqual(spawn_count, 0, "Supervisor must NEVER auto-restart a user-stopped daemon")
        self.assertTrue(
            any(
                ev.get("type") == "_desktop.connection"
                and ev.get("stopping") is False
                and ev.get("user_stopped") is True
                for ev in collected
            ),
            f"Expected _desktop.connection clearing daemon_stopping, got {collected}",
        )

        # An externally started daemon with a DIFFERENT pid must reconnect and clear user_stopped
        with state_lock:
            daemon_state["stop_called"] = False
            daemon_state["pid"] = 52002
            daemon_state["online"] = True
        self._wait_until(
            lambda: bridge.connection_state()["data"]["connected"]
            and (bridge.connection_state()["data"]["daemon_status"] or {}).get("pid") == 52002,
            msg="Expected bridge to reconnect when a new external daemon pid starts",
        )
        self.assertFalse(bridge.connection_state()["data"]["user_stopped"])
        self.assertEqual(spawn_count, 0)

    def test_f1_probe_in_flight_during_stop_cannot_undo_user_stop(self) -> None:
        """F1 + F9: a probe already in flight when stop_daemon() runs must not clear user_stopped."""
        self._touch_mesh_db()
        spawn_count = 0
        probe_entered = threading.Event()
        probe_release = threading.Event()
        gate_lock = threading.Lock()
        gate = {"block_next_probe": False, "online": True}

        def make_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            class _RaceClient:
                socket_path = base.socket_path

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    cmd = payload.get("cmd")
                    req_id = payload.get("request_id", "req_race")
                    if cmd == "daemon.status":
                        with gate_lock:
                            should_block = gate["block_next_probe"]
                            if should_block:
                                gate["block_next_probe"] = False
                            online = gate["online"]
                        if should_block:
                            probe_entered.set()
                            probe_release.wait(timeout=3.0)
                            # Return the old pid as if the response was already on the wire before stop
                            return {
                                "ok": True,
                                "data": {"status": "running", "pid": 61001, "uptime_s": 10, "active_runs": 0},
                                "request_id": req_id,
                            }
                        if not online:
                            raise MeshError(
                                code=c.DAEMON_UNAVAILABLE,
                                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                                message="Daemon offline",
                            )
                        return {
                            "ok": True,
                            "data": {"status": "running", "pid": 61001, "uptime_s": 10, "active_runs": 0},
                            "request_id": req_id,
                        }
                    if cmd == "daemon.stop":
                        with gate_lock:
                            gate["online"] = False
                        return {"ok": True, "data": {"status": "stopping"}, "request_id": req_id}
                    return base.send_request(payload)

                def _connect_socket(self, timeout: Optional[float] = None) -> Any:
                    return base._connect_socket(timeout)

            return _RaceClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "data": {"status": "started"}}), stderr=""
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.03,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        bridge.start()
        self._wait_until(
            lambda: bridge.connection_state()["data"]["connected"]
            and (bridge.connection_state()["data"]["daemon_status"] or {}).get("pid") == 61001,
        )

        # Arm the next supervisor probe to stall in flight
        with gate_lock:
            gate["block_next_probe"] = True
        self.assertTrue(probe_entered.wait(timeout=2.0), "Supervisor probe did not enter flight")

        # While that probe is in flight, user calls stop_daemon()
        stop_res = bridge.stop_daemon()
        self.assertTrue(stop_res["ok"])

        # Now release the stale in-flight probe returning old pid 61001
        probe_release.set()

        self._wait_until(
            lambda: (
                not bridge.connection_state()["data"]["connected"]
                and bridge.connection_state()["data"]["user_stopped"] is True
                and not bridge.connection_state()["data"]["daemon_stopping"]
                and bridge.connection_state()["data"]["reconnect_attempt"] >= 3
            ),
            msg="In-flight probe must not undo user_stopped or trigger auto-start",
        )
        self.assertEqual(spawn_count, 0)

    def test_f9_manual_start_races_auto_start_single_flight(self) -> None:
        """F9: manual start_daemon() racing an in-flight supervisor auto-start spawns only once."""
        self._touch_mesh_db()
        self.ctrl.online = False
        auto_entered = threading.Event()
        auto_release = threading.Event()
        spawn_count = 0
        count_lock = threading.Lock()

        def blocking_auto_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            with count_lock:
                spawn_count += 1
            auto_entered.set()
            auto_release.wait(timeout=3.0)
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 71001}}), stderr=""
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.03),
            probe_interval_s=0.05,
            subprocess_runner=blocking_auto_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        bridge.start()
        self.assertTrue(auto_entered.wait(timeout=2.0), "Supervisor did not begin auto-start")

        # Manual start arrives while auto-start holds single-flight lock
        manual_res = bridge.start_daemon()
        self.assertTrue(manual_res["ok"])
        self.assertTrue(manual_res["data"].get("in_progress"))

        auto_release.set()
        self._wait_until(lambda: bridge.connection_state()["data"]["connected"])
        self.assertEqual(spawn_count, 1)

    def test_f6_auto_start_gated_on_daemon_unavailable_not_probe_timeout(self) -> None:
        """F6: probe timeout or non-DAEMON_UNAVAILABLE error must NOT trigger auto-start."""
        self._touch_mesh_db()
        spawn_count = 0

        def timeout_client_factory(home: str, timeout: float) -> Any:
            class _TimeoutClient:
                socket_path = os.path.join(home, "run", "mesh.sock")

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    raise MeshError(
                        code=c.WAIT_TIMEOUT,
                        exit_code=c.EXIT_WAIT_TIMEOUT,
                        message="Probe timed out against busy daemon",
                    )

            return _TimeoutClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {}}', stderr="")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=timeout_client_factory,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        bridge.start()
        self._wait_until(lambda: bridge.connection_state()["data"]["reconnect_attempt"] >= 3)
        self.assertEqual(spawn_count, 0, "Non-DAEMON_UNAVAILABLE probe failure must not spawn mesh daemon start")

    def test_f7_crash_loop_caps_auto_starts_and_surfaces_persistent_error(self) -> None:
        """F7: daemon that connects then crashes repeatedly is capped at auto_start_max_attempts."""
        self._touch_mesh_db()
        self.ctrl.online = False
        spawn_count = 0
        connected_events = 0

        def crash_loop_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 80000 + spawn_count}}), stderr=""
            )

        def on_ev(ev: Dict[str, Any]) -> None:
            nonlocal connected_events
            if ev.get("type") == "_desktop.connection" and ev.get("connected") is True:
                connected_events += 1
                # Crash immediately after connecting
                with self.ctrl.lock:
                    self.ctrl.online = False

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.02,
            subprocess_runner=crash_loop_run,
            on_event=on_ev,
            push_enabled=False,
            auto_start_max_attempts=3,
            auto_start_window_s=300.0,
        )
        self.addCleanup(bridge.close)
        bridge.start()

        self._wait_until(
            lambda: (bridge.connection_state()["data"].get("last_start_error") or {}).get("code")
            == "AUTO_START_RATE_LIMITED",
            timeout=4.0,
            msg="Expected crash loop to hit AUTO_START_RATE_LIMITED",
        )
        st = bridge.connection_state()["data"]
        self.assertEqual(spawn_count, 3)
        self.assertIs(st["last_start_error"]["auto"], True)

        # Manual start resets the rate-limit window
        manual_res = bridge.start_daemon()
        self.assertTrue(manual_res["ok"])
        self.assertEqual(spawn_count, 4)

    def test_f10_supervisor_survives_unexpected_start_exception(self) -> None:
        """F10: unexpected exception from subprocess_runner must not kill supervisor thread."""
        self._touch_mesh_db()
        self.ctrl.online = False

        def exploding_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise RuntimeError("injected unexpected failure")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.02, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=exploding_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        bridge.start()

        self._wait_until(
            lambda: bridge.connection_state()["data"]["last_start_error"] is not None
            and bridge.connection_state()["data"]["reconnect_attempt"] >= 2,
            msg="Supervisor did not record error and continue backoff after unexpected exception",
        )
        self.assertTrue(bridge._supervisor_thread is not None and bridge._supervisor_thread.is_alive())
        self.assertIs(bridge.connection_state()["data"]["last_start_error"]["auto"], True)

        # Supervisor is still alive and reconnects when daemon comes online
        with self.ctrl.lock:
            self.ctrl.online = True
        self._wait_until(lambda: bridge.connection_state()["data"]["connected"])

    def test_f12_and_f13_snapshot_event_omits_diag_log_and_manual_error_sets_auto_false(self) -> None:
        """F12 + F13: events omit daemon_diag_log (only connection_state returns it); manual error has auto=False."""
        self._touch_mesh_db()
        self.ctrl.online = False

        def fail_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="manual boom")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            subprocess_runner=fail_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        res = bridge.start_daemon()
        self.assertFalse(res["ok"])
        st = bridge.connection_state()["data"]
        self.assertIsNotNone(st["last_start_error"])
        self.assertIs(st["last_start_error"]["auto"], False)
        self.assertIn("daemon_diag_log", st)
        self.assertGreaterEqual(len(st["daemon_diag_log"]), 2)

        events = bridge.poll_events()["data"]["items"]
        conn_events = [ev for ev in events if ev.get("type") == "_desktop.connection" and isinstance(ev.get("connection"), dict)]
        self.assertTrue(conn_events, f"Expected _desktop.connection events, got {events}")
        for ev in conn_events:
            self.assertNotIn("daemon_diag_log", ev["connection"])

    def test_start_daemon_already_running_without_stopping_probes_and_connects(self) -> None:
        self._touch_mesh_db()
        self.ctrl.online = True

        def already_running_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "exit_code": c.EXIT_INTERNAL,
                        "error": {
                            "code": c.DAEMON_ALREADY_RUNNING,
                            "message": "Daemon is already running",
                            "details": {},
                        },
                    }
                ),
                stderr="",
            )

        bridge = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            subprocess_runner=already_running_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        res = bridge.start_daemon()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["started"]["status"], "already_running")
        self.assertTrue(bridge.connection_state()["data"]["connected"])

    def test_n1_two_bridges_stop_in_a_suppresses_b_and_fresh_launch_still_starts(self) -> None:
        """N1: stopping in bridge A writes run/autostart.hold so open bridge B does not restart meshd;
        opening a fresh bridge C later still auto-starts on its first probe round."""
        from mesh.core.paths import autostart_hold_path, read_autostart_hold

        self._touch_mesh_db()
        spawn_a = 0
        spawn_b = 0
        spawn_c = 0

        def run_a(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_a
            spawn_a += 1
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        def run_b(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_b
            spawn_b += 1
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        def run_c(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_c
            spawn_c += 1
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        bridge_a = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=run_a,
            push_enabled=False,
        )
        bridge_b = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=run_b,
            push_enabled=False,
        )
        self.addCleanup(bridge_a.close)
        self.addCleanup(bridge_b.close)

        self.assertTrue(bridge_a.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        bridge_a.start()
        bridge_b.start()
        self._wait_until(
            lambda: bridge_a.connection_state()["data"]["connected"]
            and bridge_b.connection_state()["data"]["connected"]
        )

        # Window A stops daemon -> writes run/autostart.hold
        stop_res = bridge_a.stop_daemon()
        self.assertTrue(stop_res["ok"], stop_res)
        self.assertIsNotNone(read_autostart_hold(self.home))

        # Both A and B settle offline with user_stopped=True and 0 spawns
        self._wait_until(
            lambda: (
                not bridge_a.connection_state()["data"]["connected"]
                and bridge_a.connection_state()["data"]["user_stopped"] is True
                and not bridge_a.connection_state()["data"]["daemon_stopping"]
                and bridge_a.connection_state()["data"]["reconnect_attempt"] >= 3
                and not bridge_b.connection_state()["data"]["connected"]
                and bridge_b.connection_state()["data"]["user_stopped"] is True
                and not bridge_b.connection_state()["data"]["daemon_stopping"]
                and bridge_b.connection_state()["data"]["reconnect_attempt"] >= 3
            ),
            timeout=4.0,
            msg="Bridge B must honor autostart.hold from Bridge A and not restart meshd",
        )
        self.assertEqual(spawn_a, 0)
        self.assertEqual(spawn_b, 0)
        # F3 + N1: Window A must still push the closing (stopping=False, user_stopped=True) event
        evs_a = bridge_a.poll_events(limit=200)["data"]["items"]
        self.assertTrue(
            any(
                ev.get("type") == "_desktop.connection"
                and ev.get("connected") is False
                and ev.get("stopping") is False
                and ev.get("user_stopped") is True
                for ev in evs_a
            ),
            f"Expected Window A to push stopping=False, user_stopped=True event, got {evs_a}",
        )

        # Opening a fresh window C ("next day") while autostart.hold exists DOES auto-start on first round
        bridge_a.close()
        bridge_b.close()
        self.assertTrue(os.path.isfile(autostart_hold_path(self.home)))
        bridge_c = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=run_c,
            push_enabled=False,
        )
        self.addCleanup(bridge_c.close)
        bridge_c.start()
        self._wait_until(lambda: bridge_c.connection_state()["data"]["connected"], timeout=3.0)
        self.assertEqual(spawn_c, 1)
        self.assertIsNone(read_autostart_hold(self.home))

    def test_n2_cli_daemon_start_recovers_from_concurrent_lock_race(self) -> None:
        """N2: if spawned meshd exits early because another caller won meshd.lock, handle_daemon_start
        probes daemon.status (and waits briefly while lock is held) and returns already_running."""
        import argparse
        from mesh.cli.commands import daemon as daemon_cli

        self._touch_mesh_db()
        ns = argparse.Namespace(home=self.home, user=False, dry_run=False)

        # Case 1: child exits rc=1 while winner holds lock for 2 ticks then answers daemon.status
        class _DeadProc:
            returncode = 1
            pid = 12345

            def poll(self) -> int:
                return 1

        status_calls = 0

        class _RaceClient:
            def __init__(self, home: str, timeout: float = 1.0) -> None:
                self.home = home

            def execute_cmd(self, _args: Any, cmd: str, **_kw: Any) -> Dict[str, Any]:
                nonlocal status_calls
                self.last_cmd = cmd
                status_calls += 1
                # Call 1 is pre-spawn check (offline); call 2 is immediate post-exit (still starting);
                # call 3 is after 1 lock-held sleep tick -> winner is now answering!
                if status_calls < 3:
                    raise MeshError(c.DAEMON_UNAVAILABLE, exit_code=c.EXIT_DAEMON_UNAVAILABLE, message="not up yet")
                return {"ok": True, "data": {"status": "running", "pid": 98765}}

        lock_checks = iter([False, True, False])

        with (
            mock.patch.object(daemon_cli, "MeshClient", _RaceClient),
            mock.patch.object(daemon_cli, "_meshd_lock_held", side_effect=lambda _h: next(lock_checks, False)),
            mock.patch.object(daemon_cli.subprocess, "Popen", return_value=_DeadProc()),
            mock.patch.object(daemon_cli.time, "sleep", lambda _s: None),
        ):
            res = daemon_cli.handle_daemon_start(ns)
        self.assertEqual(res["status"], "already_running")
        self.assertEqual(res["pid"], 98765)

        # Case 2: genuine startup failure (lock NOT held, daemon.status fails) still raises INTERNAL_ERROR
        class _OfflineClient:
            def __init__(self, home: str, timeout: float = 1.0) -> None:
                self.home = home

            def execute_cmd(self, _args: Any, _cmd: str, **_kw: Any) -> Dict[str, Any]:
                raise MeshError(c.DAEMON_UNAVAILABLE, exit_code=c.EXIT_DAEMON_UNAVAILABLE, message="offline")

        with (
            mock.patch.object(daemon_cli, "MeshClient", _OfflineClient),
            mock.patch.object(daemon_cli, "_meshd_lock_held", return_value=False),
            mock.patch.object(daemon_cli.subprocess, "Popen", return_value=_DeadProc()),
        ):
            with self.assertRaises(MeshError) as cm:
                daemon_cli.handle_daemon_start(ns)
        self.assertEqual(cm.exception.code, c.INTERNAL_ERROR)
        self.assertIn("exited immediately with returncode 1", cm.exception.message)

    def test_n3_lock_held_skips_auto_start_on_connect_phase_error_and_recovers(self) -> None:
        """N3: connect-phase DAEMON_UNAVAILABLE ('Resource temporarily unavailable') must NOT spawn
        mesh daemon start while run/meshd.lock is exclusively held."""
        from mesh.platform import locks as plat_locks

        self._touch_mesh_db()
        spawn_count = 0
        lock_state = {"held": True, "online": False}

        def busy_client_factory(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            class _BusyClient:
                socket_path = base.socket_path

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    if not lock_state["online"]:
                        raise MeshError(
                            code=c.DAEMON_UNAVAILABLE,
                            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                            message=f"Daemon unavailable at {self.socket_path}: [Errno 11] Resource temporarily unavailable",
                        )
                    return base.send_request(payload)

            return _BusyClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=busy_client_factory,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)

        with mock.patch.object(plat_locks, "is_exclusive_lock_held", side_effect=lambda _p: lock_state["held"]):
            bridge.start()
            self._wait_until(
                lambda: bridge.connection_state()["data"]["reconnect_attempt"] >= 4
                and bridge.connection_state()["data"]["daemon_stopping"] is True,
                timeout=3.0,
            )
            st_busy = bridge.connection_state()["data"]
            self.assertEqual(spawn_count, 0, "Must never spawn while meshd.lock is held")
            self.assertTrue(st_busy["daemon_stopping"])
            self.assertFalse(st_busy["user_stopped"])
            self.assertIsNone(st_busy["last_start_error"])
            busy_diag = [e for e in st_busy["daemon_diag_log"] if e["code"] == "DAEMON_BUSY_OR_STOPPING"]
            self.assertEqual(len(busy_diag), 1, "Busy diag entry should not spam every probe")

            # Once daemon recovers (e.g. SIGCONT), bridge reconnects with 0 spawns
            lock_state["online"] = True
            self._wait_until(lambda: bridge.connection_state()["data"]["connected"], timeout=3.0)
            self.assertEqual(spawn_count, 0)
            self.assertFalse(bridge.connection_state()["data"]["daemon_stopping"])

    def test_n4_connected_event_never_carries_daemon_starting_true(self) -> None:
        """N4: connected _desktop.connection events and start_daemon return snapshot have daemon_starting=False."""
        self._touch_mesh_db()
        self.ctrl.online = False

        def ok_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            with self.ctrl.lock:
                self.ctrl.online = True
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"ok": True, "data": {"status": "started", "pid": 4242}}), stderr=""
            )

        # 1. Manual start_daemon()
        bridge_manual = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            subprocess_runner=ok_run,
            push_enabled=False,
        )
        self.addCleanup(bridge_manual.close)
        res_m = bridge_manual.start_daemon()
        self.assertTrue(res_m["ok"], res_m)
        self.assertIs(res_m["data"]["connection"]["connected"], True)
        self.assertIs(res_m["data"]["connection"]["daemon_starting"], False)
        evs_m = bridge_manual.poll_events()["data"]["items"]
        conn_true_m = [e for e in evs_m if e.get("type") == "_desktop.connection" and e.get("connected") is True]
        self.assertTrue(conn_true_m, f"Expected connected=True event, got {evs_m}")
        for ev in conn_true_m:
            self.assertIs(ev["connection"]["daemon_starting"], False, f"Stale daemon_starting=True in {ev}")

        # 2. Supervisor auto-start
        self.ctrl.online = False
        bridge_auto = DesktopBridge(
            home=self.home,
            client_factory=self.ctrl.make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=ok_run,
            push_enabled=False,
        )
        self.addCleanup(bridge_auto.close)
        bridge_auto.start()
        evs_a: List[Dict[str, Any]] = []
        def _has_connected_event() -> bool:
            evs_a.extend(bridge_auto.poll_events()["data"]["items"])
            return any(e.get("type") == "_desktop.connection" and e.get("connected") is True for e in evs_a)
        self._wait_until(_has_connected_event, timeout=3.0)
        conn_true_a = [e for e in evs_a if e.get("type") == "_desktop.connection" and e.get("connected") is True]
        self.assertTrue(conn_true_a, f"Expected connected=True event, got {evs_a}")
        for ev in conn_true_a:
            self.assertIs(ev["connection"]["daemon_starting"], False, f"Stale daemon_starting=True in {ev}")

    def test_n5_compute_setup_hint_default_custom_home_and_portable_windows(self) -> None:
        """N5: setup_hint adapts to default home, custom --home, and portable Windows mesh.exe/mesh.cmd."""
        from mesh.core.paths import default_home_path
        from mesh.desktop.bridge import compute_setup_hint

        def_home = default_home_path()
        self.assertEqual(
            compute_setup_hint(def_home, executable="/usr/bin/python3", platform_name="linux"),
            "mesh setup",
        )
        self.assertEqual(
            compute_setup_hint("/tmp/custom mesh", executable="/usr/bin/python3", platform_name="linux"),
            'mesh --home "/tmp/custom mesh" setup',
        )

        # Portable Windows with mesh.exe
        portable_exe = r"C:\Apps\AgentMesh\python\pythonw.exe"
        self.assertEqual(
            compute_setup_hint(
                def_home,
                executable=portable_exe,
                platform_name="win32",
                isfile_fn=lambda p: p == r"C:\Apps\AgentMesh\mesh.exe",
            ),
            r'"C:\Apps\AgentMesh\mesh.exe" setup',
        )
        self.assertEqual(
            compute_setup_hint(
                r"D:\data\my mesh",
                executable=portable_exe,
                platform_name="win32",
                isfile_fn=lambda p: p == r"C:\Apps\AgentMesh\mesh.exe",
            ),
            r'"C:\Apps\AgentMesh\mesh.exe" --home "D:\data\my mesh" setup',
        )
        # Portable Windows fallback to mesh.cmd when mesh.exe absent
        self.assertEqual(
            compute_setup_hint(
                def_home,
                executable=portable_exe,
                platform_name="win32",
                isfile_fn=lambda p: p == r"C:\Apps\AgentMesh\mesh.cmd",
            ),
            r'"C:\Apps\AgentMesh\mesh.cmd" setup',
        )

        # Bridge snapshot exposes setup_hint
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        self.assertEqual(
            bridge.connection_state()["data"]["setup_hint"],
            f'mesh --home "{bridge.home}" setup',
        )

    def test_n6_daemon_stop_transport_error_keeps_suppression_auth_error_rolls_back(self) -> None:
        """N6: DAEMON_UNAVAILABLE on daemon.stop keeps user_stopped=True and writes autostart.hold;
        auth errors roll back user_stopped so auto-start is unaffected."""
        from mesh.core.paths import clear_autostart_hold, read_autostart_hold

        self._touch_mesh_db()
        spawn_count = 0
        mode_lock = threading.Lock()
        mode = {"stop_err": "transport", "online": True}

        def make_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            class _StopErrClient:
                socket_path = base.socket_path

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    cmd = payload.get("cmd")
                    req_id = payload.get("request_id", "req_n6")
                    if cmd == "daemon.stop":
                        with mode_lock:
                            stop_err = mode["stop_err"]
                            if stop_err == "transport":
                                mode["online"] = False
                        if stop_err == "transport":
                            raise MeshError(
                                code=c.DAEMON_UNAVAILABLE,
                                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                                message="Connection closed by daemon without response",
                            )
                        return {
                            "ok": False,
                            "exit_code": c.EXIT_UNAUTHENTICATED,
                            "error": {"code": c.PASSPHRASE_INVALID, "message": "Wrong passphrase"},
                            "request_id": req_id,
                        }
                    with mode_lock:
                        online = mode["online"]
                    if not online:
                        raise MeshError(
                            code=c.DAEMON_UNAVAILABLE,
                            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                            message="Daemon offline",
                        )
                    return base.send_request(payload)

                def _connect_socket(self, timeout: Optional[float] = None) -> Any:
                    return base._connect_socket(timeout)

            return _StopErrClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])

        # 1. Auth error rolls back user_stopped and does NOT write autostart.hold
        with mode_lock:
            mode["stop_err"] = "auth"
        auth_res = bridge.stop_daemon()
        self.assertFalse(auth_res["ok"])
        self.assertEqual(auth_res["error"]["code"], c.PASSPHRASE_INVALID)
        self.assertFalse(bridge.connection_state()["data"]["user_stopped"])
        self.assertIsNone(read_autostart_hold(self.home))

        # 2. Transport error (lost reply) keeps user_stopped=True, writes hold, and suppresses restart
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        bridge.start()
        self._wait_until(lambda: bridge.connection_state()["data"]["connected"], timeout=3.0)
        with mode_lock:
            mode["stop_err"] = "transport"
        trans_res = bridge.stop_daemon()
        self.assertFalse(trans_res["ok"])
        self.assertEqual(trans_res["error"]["code"], c.DAEMON_UNAVAILABLE)
        self.assertIsNotNone(read_autostart_hold(self.home))

        self._wait_until(
            lambda: not bridge.connection_state()["data"]["connected"]
            and bridge.connection_state()["data"]["user_stopped"] is True
            and bridge.connection_state()["data"]["reconnect_attempt"] >= 3,
            timeout=3.0,
        )
        self.assertEqual(spawn_count, 0)
        clear_autostart_hold(self.home)

    def test_n6_in_flight_first_probe_during_stop_transport_error_cannot_clear_hold(self) -> None:
        """Deterministic regression for N6 race: an in-flight first supervisor probe that fetched
        daemon.status before daemon.stop (when _daemon_status was still None) must not clear
        user_stopped or delete autostart.hold when daemon.stop hits a transport error."""
        from mesh.core.paths import clear_autostart_hold, read_autostart_hold

        self._touch_mesh_db()
        spawn_count = 0
        state_lock = threading.Lock()
        state = {"online": True, "pid": 4242, "block_first_version": True}
        version_entered = threading.Event()
        version_release = threading.Event()
        probe_finished_after_stop = threading.Event()

        def make_client(home: str, timeout: float) -> Any:
            base = self.ctrl.make_client(home, timeout)

            class _StallFirstProbeClient:
                socket_path = base.socket_path

                def send_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                    cmd = payload.get("cmd")
                    req_id = payload.get("request_id", "req_n6_race")
                    if cmd == "version":
                        should_block = False
                        with state_lock:
                            if state["block_first_version"]:
                                state["block_first_version"] = False
                                should_block = True
                        if should_block:
                            version_entered.set()
                            version_release.wait(timeout=3.0)
                            # Simulate version finishing (either ok or error) after stop_daemon ran
                            return {"ok": True, "data": {"version": "1.0.0.dev1"}, "request_id": req_id}
                    if cmd == "daemon.stop":
                        with state_lock:
                            state["online"] = False
                        raise MeshError(
                            code=c.DAEMON_UNAVAILABLE,
                            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                            message="Connection closed by daemon without response",
                        )
                    with state_lock:
                        online = state["online"]
                        pid = state["pid"]
                    if not online:
                        if version_release.is_set():
                            probe_finished_after_stop.set()
                        raise MeshError(
                            code=c.DAEMON_UNAVAILABLE,
                            exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                            message="Daemon offline",
                        )
                    if cmd == "daemon.status":
                        return {
                            "ok": True,
                            "data": {"status": "running", "pid": pid, "uptime_s": 5, "active_runs": 0},
                            "request_id": req_id,
                        }
                    return base.send_request(payload)

                def _connect_socket(self, timeout: Optional[float] = None) -> Any:
                    return base._connect_socket(timeout)

            return _StallFirstProbeClient()

        def count_run(argv: List[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal spawn_count
            spawn_count += 1
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {"status": "started"}}', stderr="")

        bridge = DesktopBridge(
            home=self.home,
            client_factory=make_client,
            backoff_schedule=(0.015, 0.02),
            probe_interval_s=0.03,
            subprocess_runner=count_run,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        # set_passphrase sets _connected=True while _daemon_status is still None
        self.assertTrue(bridge.set_passphrase(self.ctrl.passphrase, verify=True)["ok"])
        self.assertIsNone(bridge.connection_state()["data"]["daemon_status"])

        bridge.start()
        self.assertTrue(version_entered.wait(timeout=2.0), "First probe did not stall in version RPC")

        # stop_daemon hits transport error while first probe is still stalled mid-flight
        trans_res = bridge.stop_daemon()
        self.assertFalse(trans_res["ok"])
        self.assertEqual(trans_res["error"]["code"], c.DAEMON_UNAVAILABLE)
        self.assertIsNotNone(read_autostart_hold(self.home))

        # Release the stale pre-stop probe and confirm it CANNOT clear hold or user_stopped
        version_release.set()
        self.assertTrue(probe_finished_after_stop.wait(timeout=2.0))
        self._wait_until(
            lambda: not bridge.connection_state()["data"]["connected"]
            and bridge.connection_state()["data"]["user_stopped"] is True
            and bridge.connection_state()["data"]["reconnect_attempt"] >= 3,
            timeout=3.0,
        )
        self.assertIsNotNone(read_autostart_hold(self.home))
        self.assertEqual(spawn_count, 0)

        # A genuinely new daemon started AFTER the stop still reconnects and clears the hold
        with state_lock:
            state["pid"] = 52099
            state["online"] = True
        self._wait_until(
            lambda: bridge.connection_state()["data"]["connected"]
            and (bridge.connection_state()["data"]["daemon_status"] or {}).get("pid") == 52099,
            timeout=3.0,
        )
        self.assertFalse(bridge.connection_state()["data"]["user_stopped"])
        self.assertIsNone(read_autostart_hold(self.home))
        self.assertEqual(spawn_count, 0)
        clear_autostart_hold(self.home)

    def test_home_status_and_initialize_home_guards(self) -> None:
        ctrl = _FakeDaemonController()
        ctrl.online = False
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        # 1. Uninitialized home_status
        hs0 = api.home_status()
        self.assertTrue(hs0["ok"], hs0)
        self.assertFalse(hs0["data"]["home_initialized"])
        self.assertFalse(hs0["data"]["daemon_running"])

        # 2. Short passphrase rejected and never echoed
        short_pw = "too-short"
        bad = api.initialize_home(short_pw)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], c.INVALID_ARGUMENT)
        self.assertNotIn(short_pw, json.dumps(bad))

        # 3. Existing mesh.db strictly refused (no reset/overwrite)
        self._touch_mesh_db()
        hs1 = api.home_status()
        self.assertTrue(hs1["ok"], hs1)
        self.assertTrue(hs1["data"]["home_initialized"])

        secret_pw = "already-init-secret-passphrase-123"
        with mock.patch("mesh.core.init.init_mesh_home") as mock_init:
            dup = api.initialize_home(secret_pw)
        self.assertFalse(dup["ok"])
        self.assertEqual(dup["error"]["code"], c.ALREADY_EXISTS)
        mock_init.assert_not_called()
        self.assertNotIn(secret_pw, json.dumps(dup))

    def test_initialize_home_inits_starts_daemon_and_unlocks_without_leaking_passphrase(self) -> None:
        secret_pw = "super-secret-wizard-passphrase-99"
        ctrl = _FakeDaemonController()
        ctrl.passphrase = secret_pw
        ctrl.online = False
        spawn_calls: List[Dict[str, Any]] = []

        def fake_spawn(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            spawn_calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
            ctrl.online = True
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"ok": True, "data": {"status": "running", "pid": 4242}}),
                stderr="",
            )

        def fake_init_mesh_home(home_path: str, pw: str) -> Dict[str, Any]:
            self.assertEqual(pw, secret_pw)
            os.makedirs(home_path, exist_ok=True)
            with open(os.path.join(home_path, "mesh.db"), "wb") as f:
                f.write(b"SQLite format 3\x00")
            return {"home": home_path, "user_id": "usr_test", "initialized": True}

        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            subprocess_runner=fake_spawn,
            push_enabled=False,
        )
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        with mock.patch.dict(os.environ, {"MESH_PASSPHRASE": "env-secret-should-strip"}, clear=False), \
                mock.patch("mesh.core.init.init_mesh_home", side_effect=fake_init_mesh_home):
            res = api.initialize_home(secret_pw)

        self.assertTrue(res["ok"], res)
        self.assertTrue(res["data"]["initialized"])
        self.assertTrue(res["data"]["daemon_started"])
        self.assertTrue(res["data"]["auth"]["auth_verified"])
        self.assertEqual(len(spawn_calls), 1)
        # Passphrase never in argv, env, response, or diag logs
        self.assertNotIn(secret_pw, json.dumps(spawn_calls[0]["argv"]))
        self.assertNotIn("MESH_PASSPHRASE", spawn_calls[0]["env"])
        self.assertNotIn(secret_pw, json.dumps(spawn_calls[0]["env"]))
        self.assertNotIn(secret_pw, json.dumps(res))
        self.assertNotIn(secret_pw, json.dumps(bridge.connection_state()))

    def test_detect_local_clis_reports_profiles_presets_and_excludes_held_or_stub(self) -> None:
        from mesh.adapters.base import ReadonlyCmdResult

        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        # Cannot pass caller binary arguments
        with self.assertRaises(TypeError):
            api.detect_local_clis("/bin/sh")  # type: ignore[call-arg]

        fake_bins = {
            "claude": "/usr/local/bin/claude",
            "codex": "/usr/local/bin/codex",
            "devin": "/usr/local/bin/devin",
        }

        def fake_resolve(adapter_self: Any, binary_path: Optional[str] = None) -> Optional[str]:
            self.assertIsNone(binary_path)
            return fake_bins.get(adapter_self.id)

        def fake_cmd(argv: List[str], timeout_s: float = 5.0, **_kw: Any) -> ReadonlyCmdResult:
            self.assertLessEqual(timeout_s, 3.0)
            if "claude" in argv[0]:
                return ReadonlyCmdResult(0, "claude 2.1.5", "", "h", "claude 2.1.5")
            if "codex" in argv[0]:
                return ReadonlyCmdResult(0, "codex 0.166.0", "", "h", "codex 0.166.0")
            if "devin" in argv[0]:
                return ReadonlyCmdResult(0, "devin 3000.1.0", "", "h", "devin 3000.1.0")
            return ReadonlyCmdResult(127, "", "not found", "", "")

        with mock.patch("mesh.adapters.base.Adapter.resolve_binary", new=fake_resolve), \
                mock.patch("mesh.adapters.claude.ClaudeAdapter.resolve_binary", new=fake_resolve), \
                mock.patch("mesh.adapters.codex.CodexAdapter.resolve_binary", new=fake_resolve), \
                mock.patch("mesh.adapters.devin.DevinAdapter.resolve_binary", new=fake_resolve), \
                mock.patch("mesh.adapters.base.run_readonly_cmd", side_effect=fake_cmd):
            res = api.detect_local_clis()

        self.assertTrue(res["ok"], res)
        by_id = {c_item["id"]: c_item for c_item in res["data"]["clis"]}
        self.assertIn("claude", by_id)
        self.assertIn("codex", by_id)
        self.assertIn("devin", by_id)
        self.assertIn("hermes", by_id)
        self.assertIn("openclaw", by_id)

        self.assertTrue(by_id["claude"]["can_lead_eligible"])
        self.assertEqual(by_id["claude"]["default_safe_profile"], "edit")
        self.assertEqual(by_id["claude"]["version"], "2.1.5")
        self.assertIsInstance(by_id["claude"]["model_presets"], dict)

        self.assertTrue(by_id["codex"]["can_lead_eligible"])
        self.assertEqual(by_id["codex"]["default_safe_profile"], "dev")
        self.assertEqual(by_id["codex"]["version"], "0.166.0")

        # Devin is launch_held (M5) and unrestricted-only -> NEVER eligible as lead
        self.assertFalse(by_id["devin"]["can_lead_eligible"])
        self.assertTrue(by_id["devin"]["launch_held"])
        self.assertIsNone(by_id["devin"]["default_safe_profile"])

        # Stubs never eligible
        self.assertFalse(by_id["hermes"]["can_lead_eligible"])
        self.assertFalse(by_id["openclaw"]["can_lead_eligible"])
        self.assertEqual(res["data"]["eligible_leads"], ["claude", "codex"])

    def test_detect_local_clis_never_executes_cwd_binary_on_win32_or_relative_path(self) -> None:
        """P2-3 regression: detect_local_clis never resolves/executes cwd or relative PATH entries."""
        bridge = DesktopBridge(home=self.home, client_factory=self.ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        with tempfile.TemporaryDirectory(prefix="mh-p2-3-cwd-") as td:
            cwd_dir = os.path.join(td, "repo_cwd")
            empty_bin = os.path.join(td, "empty_bin")
            os.makedirs(cwd_dir)
            os.makedirs(empty_bin)
            for fname in ("droid.CMD", "droid.cmd", "droid"):
                p = os.path.join(cwd_dir, fname)
                with open(p, "w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\necho pwned\n")
                os.chmod(p, 0o755)

            called_cmds: List[List[str]] = []

            def spy_cmd(argv: List[str], **_kw: Any) -> Any:
                called_cmds.append(list(argv))
                raise AssertionError(f"Should not execute untrusted cwd binary: {argv}")

            old_cwd = os.getcwd()
            try:
                os.chdir(cwd_dir)
                # Mocked win32 semantics
                with mock.patch.object(sys, "platform", "win32"), \
                        mock.patch.dict(os.environ, {"PATH": f".{os.pathsep}{empty_bin}", "PATHEXT": ".EXE;.CMD"}), \
                        mock.patch("mesh.adapters.base.run_readonly_cmd", side_effect=spy_cmd):
                    res_win = api.detect_local_clis()
                self.assertTrue(res_win["ok"], res_win)
                by_id = {x["id"]: x for x in res_win["data"]["clis"]}
                self.assertFalse(by_id["droid"]["installed"])
                self.assertIsNone(by_id["droid"]["binary_path"])
                self.assertEqual(called_cmds, [])

                # POSIX with empty / relative PATH entries
                with mock.patch.dict(os.environ, {"PATH": f":{os.curdir}:{empty_bin}"}), \
                        mock.patch("mesh.adapters.base.run_readonly_cmd", side_effect=spy_cmd):
                    res_posix = api.detect_local_clis()
                self.assertTrue(res_posix["ok"], res_posix)
                by_id_p = {x["id"]: x for x in res_posix["data"]["clis"]}
                self.assertFalse(by_id_p["droid"]["installed"])
                self.assertEqual(called_cmds, [])
            finally:
                os.chdir(old_cwd)

    def test_initialize_home_reports_unverified_and_recovers_partial_mid_init_db(self) -> None:
        """P3-7 regression: accurate daemon_started/auth_verified + recovery from empty-user mesh.db."""
        from mesh.core.db import init_db as init_db_file

        # 1. Partial mid-init mesh.db (valid schema, 0 user principals) is treated as uninitialized
        db_path = os.path.join(self.home, "mesh.db")
        conn = init_db_file(db_path)
        conn.close()

        ctrl = _FakeDaemonController()
        ctrl.online = False
        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        hs = api.home_status()
        self.assertTrue(hs["ok"], hs)
        self.assertFalse(hs["data"]["home_initialized"], "Userless schema-only mesh.db must allow init recovery")

        # 2. If start_daemon stays offline after init, initialize_home must return error with daemon_started=False
        def fake_init_ok(home_path: str, _pw: str) -> Dict[str, Any]:
            with open(os.path.join(home_path, "mesh.db"), "wb") as f:
                f.write(b"x")
            return {"home": home_path, "user_id": "usr_1", "initialized": True}

        with mock.patch("mesh.core.init.init_mesh_home", side_effect=fake_init_ok), \
                mock.patch.object(bridge, "_start_daemon_impl", return_value={"ok": True, "data": {"in_progress": False}}):
            res_offline = api.initialize_home("long-valid-passphrase-123")
        self.assertFalse(res_offline["ok"], res_offline)
        self.assertTrue(res_offline["error"]["details"]["initialized"])
        self.assertFalse(res_offline["error"]["details"]["daemon_started"])

        # 3. If set_passphrase returns ok=True with auth_verified=False, initialize_home must NOT report success
        os.unlink(db_path)
        ctrl.online = True
        with mock.patch("mesh.core.init.init_mesh_home", side_effect=fake_init_ok), \
                mock.patch.object(bridge, "_start_daemon_impl", return_value={"ok": True, "data": {"started": True}}), \
                mock.patch.object(bridge, "_probe_once", return_value=True), \
                mock.patch.object(
                    bridge,
                    "set_passphrase",
                    return_value={"ok": True, "data": {"auth_configured": True, "auth_verified": False}},
                ):
            bridge._connected = True
            res_unver = api.initialize_home("long-valid-passphrase-123")
        self.assertFalse(res_unver["ok"], res_unver)
        self.assertTrue(res_unver["error"]["details"]["initialized"])
        self.assertFalse(res_unver["error"]["details"]["auth_verified"])

    def test_desktop_log_rotates_and_never_logs_passphrase_or_secrets(self) -> None:
        """Issue 3: desktop.log is exposed, written on bridge/app lifecycle, rotates, and scrubs secrets."""
        from mesh.core import logfile
        from mesh.desktop import app as desktop_app
        from mesh.desktop.bridge import append_desktop_log

        secret_pw = "MyUltraSecretDesktopPassphrase!987"
        bearer_tok = "Bearer sk-test-secret-token-1234567890"
        ctrl = _FakeDaemonController()
        ctrl.passphrase = secret_pw
        ctrl.online = True

        bridge = DesktopBridge(home=self.home, client_factory=ctrl.make_client, push_enabled=False)
        self.addCleanup(bridge.close)

        st = bridge.connection_state()["data"]
        expected_log = os.path.realpath(os.path.join(self.home, "logs", "desktop.log"))
        self.assertEqual(os.path.realpath(st["desktop_log_path"]), expected_log)
        self.assertEqual(os.path.realpath(bridge.pipe_info()["data"]["desktop_log_path"]), expected_log)

        # Lifecycle + failed unlock + successful unlock + chat.send RPC + clear_passphrase
        bad_pw = "WrongSecretPassphraseShouldNotAppear!111"
        bad_auth = bridge.set_passphrase(bad_pw, verify=True)
        self.assertFalse(bad_auth["ok"])
        ok_auth = bridge.set_passphrase(secret_pw, verify=True)
        self.assertTrue(ok_auth["ok"], ok_auth)
        bridge._record_diag(
            "warn",
            "TEST_DIAG",
            f"Diagnostic containing {secret_pw} and {bearer_tok}",
            {"passphrase": secret_pw, "note": f"echo {secret_pw} {bearer_tok}"},
        )
        bridge.call("chat.send", {"text": f"hello with {secret_pw}"})
        bridge.clear_passphrase()

        self.assertTrue(os.path.isfile(expected_log), "desktop.log was not created")
        with open(expected_log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("AUTH_UNLOCK_FAILED", content)
        self.assertIn("AUTH_UNLOCK_OK", content)
        self.assertIn("TEST_DIAG", content)
        self.assertIn("AUTH_LOCKED", content)
        self.assertNotIn(secret_pw, content)
        self.assertNotIn(bad_pw, content)
        self.assertNotIn("sk-test-secret-token-1234567890", content)

        # Rotation check using logfile.ROTATE_BYTES / KEEP
        with tempfile.TemporaryDirectory(prefix="mh-desk-rot-") as rot_home:
            with mock.patch.object(logfile, "ROTATE_BYTES", 200):
                for idx in range(25):
                    append_desktop_log(rot_home, "INFO", "ROTATE_TEST", f"entry-{idx}-" + ("x" * 60))
            log_dir = os.path.join(rot_home, "logs")
            names = set(os.listdir(log_dir))
            self.assertIn("desktop.log", names)
            self.assertIn("desktop.log.1", names)
            self.assertIn(f"desktop.log.{logfile.KEEP}", names)
            self.assertNotIn(f"desktop.log.{logfile.KEEP + 1}", names)

        # app.main --check does NOT create desktop.log; launch errors DO log to desktop.log
        with tempfile.TemporaryDirectory(prefix="mh-desk-app-") as app_home:
            with mock.patch("sys.stdout", new=io.StringIO()):
                rc_chk = desktop_app.main(["--home", app_home, "--check"])
            self.assertEqual(rc_chk, c.EXIT_OK)
            self.assertFalse(os.path.exists(os.path.join(app_home, "logs", "desktop.log")))

            with mock.patch.object(
                desktop_app,
                "launch_desktop",
                side_effect=MeshError(code=c.INTERNAL_ERROR, exit_code=c.EXIT_INTERNAL, message=f"boom {bearer_tok}"),
            ), mock.patch("sys.stderr", new=io.StringIO()):
                rc_err = desktop_app.main(["--home", app_home])
            self.assertEqual(rc_err, c.EXIT_INTERNAL)
            with open(os.path.join(app_home, "logs", "desktop.log"), encoding="utf-8") as f:
                app_log = f.read()
            self.assertIn("DESKTOP_ERROR", app_log)
            self.assertNotIn("sk-test-secret-token-1234567890", app_log)


class _MemCredStore:
    """In-memory mock of _win32_cred for testing opt-in credential vault behavior."""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.entries: Dict[str, str] = {}

    def is_supported(self) -> bool:
        return self.supported

    def has_passphrase(self, home: str) -> bool:
        return home in self.entries

    def read_passphrase(self, home: str) -> Optional[str]:
        return self.entries.get(home)

    def write_passphrase(self, home: str, passphrase: str) -> bool:
        if not self.supported:
            return False
        self.entries[home] = passphrase
        return True

    def delete_passphrase(self, home: str) -> bool:
        self.entries.pop(home, None)
        return True


class TestDesktopBridgeBatch3S4S6Tray(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-desk-b3-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.state_path = os.path.join(self.tmp, "desktop_state.json")

    def test_s4_pick_folder_and_inspect_repo_folder_detects_branch_subdir_and_tests(self) -> None:
        from mesh.desktop.state import DesktopStateStore
        from tests.gitfixture import GitFixture

        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("demo-app")
        # Create a second branch and switch to master/custom branch to prove we never hardcode main
        fixture.git(repo, "branch", "release")
        subdir = os.path.join(repo, "src", "pkg")
        os.makedirs(subdir)
        with open(os.path.join(repo, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write("[project]\nname = 'demo'\n")
        with open(os.path.join(repo, "package.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"scripts": {"test": "vitest run"}}))

        ctrl = _FakeDaemonController()
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=DesktopStateStore(self.home, path=self.state_path),
        )
        self.addCleanup(bridge.close)

        class _FakeDialogWindow:
            def __init__(self, ret: Any) -> None:
                self.ret = ret
                self.calls: List[Any] = []

            def create_file_dialog(self, dialog_type: Any, directory: str = "") -> Any:
                self.calls.append((dialog_type, directory))
                return self.ret

        win = _FakeDialogWindow((subdir,))
        bridge.attach_window(win)
        api = DesktopJsApi(bridge)

        # 1. Pick subdirectory -> detects toplevel, branches, test candidates, default_test_command="none"
        picked = api.pick_folder()
        self.assertTrue(picked["ok"], picked)
        self.assertFalse(picked["data"]["cancelled"])
        self.assertEqual(os.path.realpath(picked["data"]["path"]), os.path.realpath(subdir))
        insp = picked["data"]["inspection"]
        self.assertTrue(insp["is_git_repo"])
        self.assertFalse(insp["is_toplevel"])
        self.assertEqual(os.path.realpath(insp["toplevel"]), os.path.realpath(repo))
        self.assertIn(insp["recommended_base_branch"], insp["branches"])
        self.assertIn("release", insp["branches"])
        self.assertEqual(insp["default_test_command"], "none")
        cand_argvs = [item["argv"] for item in insp["test_candidates"]]
        self.assertIn(["pytest"], cand_argvs)
        self.assertIn(["npm", "test"], cand_argvs)
        self.assertIsNone(insp["path_conflict"])

        # 2. Cancelled picker
        win.ret = None
        canc = api.pick_folder()
        self.assertTrue(canc["ok"])
        self.assertTrue(canc["data"]["cancelled"])
        self.assertIsNone(canc["data"]["path"])

        # 3. Path overlapping MESH_HOME reports a clear path_conflict
        overlap_dir = os.path.join(self.home, "subrepo")
        os.makedirs(overlap_dir)
        ov = api.inspect_repo_folder(overlap_dir)
        self.assertTrue(ov["ok"])
        self.assertTrue(ov["data"]["path_conflict"])

        # 4. Relative path is rejected
        rel = api.inspect_repo_folder("relative/path")
        self.assertFalse(rel["ok"])
        self.assertEqual(rel["error"]["code"], c.INVALID_ARGUMENT)

    def test_s6_passphrase_memory_only_by_default_and_opt_in_cred_vault(self) -> None:
        from mesh.desktop.state import DesktopStateStore

        ctrl = _FakeDaemonController()
        cred = _MemCredStore(supported=True)
        store = DesktopStateStore(self.home, path=self.state_path)
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
            cred_store=cred,
        )
        self.addCleanup(bridge.close)
        api = DesktopJsApi(bridge)

        # Default unlock (remember=False) is strictly memory-only
        res = api.set_passphrase(ctrl.passphrase, True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["data"]["stored_credential_supported"])
        self.assertFalse(res["data"]["stored_credential_present"])
        self.assertEqual(cred.entries, {})

        # Explicit opt-in remember=True stores into OS vault only (never in desktop_state.json or auth_status)
        res2 = api.set_passphrase(ctrl.passphrase, True, True)
        self.assertTrue(res2["ok"], res2)
        self.assertTrue(res2["data"]["stored_credential_present"])
        self.assertNotIn(ctrl.passphrase, json.dumps(res2))
        self.assertEqual(cred.entries.get(bridge.home), ctrl.passphrase)

        # New bridge session auto-unlocks from opt-in vault into memory on first probe
        bridge2 = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
            cred_store=cred,
        )
        self.addCleanup(bridge2.close)
        self.assertTrue(bridge2._probe_once())
        st2 = bridge2.auth_status()["data"]
        self.assertTrue(st2["auth_verified"])
        self.assertNotIn(ctrl.passphrase, json.dumps(st2))

        # Stale vault credential is automatically wiped on PASSPHRASE_INVALID
        cred.entries[bridge2.home] = "stale-wrong-passphrase"
        bridge3 = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
            cred_store=cred,
        )
        self.addCleanup(bridge3.close)
        self.assertTrue(bridge3._probe_once())
        self.assertFalse(bridge3.auth_status()["data"]["auth_verified"])
        self.assertNotIn(bridge3.home, cred.entries)

        # clear_passphrase(forget_stored=True) wipes both memory and vault
        cred.entries[bridge2.home] = ctrl.passphrase
        bridge2.clear_passphrase(forget_stored=True)
        self.assertFalse(bridge2.auth_status()["data"]["auth_configured"])
        self.assertNotIn(bridge2.home, cred.entries)

    def test_s6_cred_vault_off_by_default_and_never_leaks_to_logs_env_or_state(self) -> None:
        import inspect
        from mesh.desktop.state import DesktopStateStore

        sig = inspect.signature(DesktopBridge.set_passphrase)
        self.assertIs(sig.parameters["remember"].default, False)

        ctrl = _FakeDaemonController()
        cred = _MemCredStore(supported=True)
        store = DesktopStateStore(self.home, path=self.state_path)
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
            cred_store=cred,
        )
        self.addCleanup(bridge.close)

        # Opt-in vault is strictly off by default when remember is omitted
        res_default = bridge.set_passphrase(ctrl.passphrase, verify=True)
        self.assertTrue(res_default["ok"], res_default)
        self.assertTrue(res_default["data"]["stored_credential_supported"])
        self.assertFalse(res_default["data"]["stored_credential_present"])
        self.assertEqual(cred.entries, {})

        # Opt-in write + auto-unlock must never leak passphrase into desktop.log, state JSON, status, or env
        res_optin = bridge.set_passphrase(ctrl.passphrase, verify=True, remember=True)
        self.assertTrue(res_optin["ok"], res_optin)
        self.assertTrue(res_optin["data"]["stored_credential_present"])

        bridge2 = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
            cred_store=cred,
        )
        self.addCleanup(bridge2.close)
        self.assertTrue(bridge2._probe_once())
        self.assertTrue(bridge2.auth_status()["data"]["auth_verified"])

        desktop_log = os.path.join(self.home, "logs", "desktop.log")
        self.assertTrue(os.path.isfile(desktop_log))
        with open(desktop_log, encoding="utf-8") as f:
            log_text = f.read()
        self.assertIn("AUTH_UNLOCK_OK", log_text)
        self.assertNotIn(ctrl.passphrase, log_text)
        if os.path.isfile(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                self.assertNotIn(ctrl.passphrase, f.read())
        self.assertNotIn(ctrl.passphrase, json.dumps(bridge2.connection_state()))
        self.assertNotIn(ctrl.passphrase, json.dumps(bridge2.pipe_info()))
        self.assertNotIn(ctrl.passphrase, json.dumps(dict(os.environ)))

    def test_tray_close_hides_and_quit_stops_nothing_unless_chosen(self) -> None:
        from mesh.desktop.state import DesktopStateStore

        ctrl = _FakeDaemonController()
        store = DesktopStateStore(self.home, path=self.state_path)
        bridge = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
        )
        self.addCleanup(bridge.close)

        class _FakeWin:
            def __init__(self) -> None:
                self.x = 120
                self.y = 90
                self.width = 1320
                self.height = 840
                self.hidden = 0
                self.shown = 0
                self.destroyed = 0

            def hide(self) -> None:
                self.hidden += 1

            def show(self) -> None:
                self.shown += 1

            def destroy(self) -> None:
                self.destroyed += 1

        class _FakeTray:
            def __init__(self) -> None:
                self.active = True
                self.statuses: List[Dict[str, Any]] = []
                self.balloons: List[Tuple[str, str]] = []
                self.stopped = 0

            def update_status(self, st: Dict[str, Any]) -> None:
                self.statuses.append(st)

            def show_balloon(self, title: str, text: str) -> None:
                self.balloons.append((title, text))

            def stop(self) -> None:
                self.stopped += 1
                self.active = False

        win = _FakeWin()
        tray = _FakeTray()
        bridge.attach_window(win)
        bridge.attach_tray(tray)
        self.assertTrue(bridge.set_passphrase(ctrl.passphrase, verify=True)["ok"])

        api = DesktopJsApi(bridge)

        # 1. Closing window hides to tray, saves bounds, keeps passphrase & meshd alive
        close_res = api.window_close()
        self.assertTrue(close_res["ok"], close_res)
        self.assertTrue(close_res["data"]["hidden_to_tray"])
        self.assertEqual(win.hidden, 1)
        self.assertEqual(win.destroyed, 0)
        self.assertEqual(len(tray.balloons), 1)
        self.assertTrue(bridge.auth_status()["data"]["auth_verified"])
        self.assertTrue(ctrl.online)
        self.assertEqual(store.load_window_bounds()["width"], 1320)

        # Show brings window back
        self.assertTrue(bridge.window_show()["ok"])
        self.assertEqual(win.shown, 1)

        # 2. Default quit_app(False) destroys GUI and stops NOTHING in meshd
        quit_res = api.quit_app(False)
        self.assertTrue(quit_res["ok"], quit_res)
        self.assertFalse(quit_res["data"]["daemon_stopped"])
        self.assertTrue(ctrl.online, "Default quit must NEVER stop meshd")
        self.assertEqual(win.destroyed, 1)
        self.assertEqual(tray.stopped, 1)
        self.assertFalse(bridge.auth_status()["data"]["auth_configured"])

        # 3. Explicit quit_app(True) stops meshd via authenticated daemon.stop RPC
        bridge2 = DesktopBridge(
            home=self.home,
            client_factory=ctrl.make_client,
            push_enabled=False,
            state_store=store,
        )
        self.addCleanup(bridge2.close)
        win2 = _FakeWin()
        tray2 = _FakeTray()
        bridge2.attach_window(win2)
        bridge2.attach_tray(tray2)
        self.assertTrue(bridge2.set_passphrase(ctrl.passphrase, verify=True)["ok"])
        quit_stop_res = DesktopJsApi(bridge2).quit_app(True)
        self.assertTrue(quit_stop_res["ok"], quit_stop_res)
        self.assertTrue(quit_stop_res["data"]["daemon_stopped"])
        self.assertFalse(ctrl.online, "Explicit quit_app(True) should stop meshd via RPC")


if __name__ == "__main__":
    unittest.main()
