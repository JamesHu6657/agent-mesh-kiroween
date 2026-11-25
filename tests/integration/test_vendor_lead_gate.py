"""N19 (lead clause): no lead turns for vendor adapters in M3-lite.

The rule sits first in `lead_turns.lead_launch_check`, before m2-r10's launch gate: a vendor
lead adapter never starts a turn, whatever the launch evidence says. The messages stay
`pending` and one `lead.turn_refused {reason:"vendor_lead_m3_lite"}` is recorded per
lead assignment. Launch evidence is simulated with a launch-enabled droid adapter
patched into `lead_turns.get_adapter` (the evidence module of m3-r5 is not on this
branch); no vendor binary is ever started.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from mesh.adapters import evidence, smoke_schema
from mesh.adapters.droid import DroidAdapter
from mesh.adapters.evidence import Evidence
from mesh.adapters.fake import FakeAdapter
from mesh.adapters.registry import get_adapter
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.daemon import lead_turns, supervisor
from mesh.errors import MeshError
from tests import tierlib
from tests.helpers import MeshHarness
from tests.integration.test_lead_actions import LeadActionCase
from tests.integration.test_lead_turns import create_agent
from tests.stubs import _write_python_stub, create_test_stubs


class _EvidencedDroid(DroidAdapter):
    """droid as if its launch evidence were complete (launch_enabled true)."""

    launch_enabled = True


class _DisabledDouble(FakeAdapter):
    """A non-vendor adapter (the test double) that is not launch-enabled."""

    launch_enabled = False


class TestVendorLeadGate(LeadActionCase):
    def _inbox_vendor_project(self, name: str = "vlead") -> str:
        # Inbox delivery: meshd's own tick never plans a turn for this lead, so the
        # only planner is the direct call below.
        create_agent(self.harness, name, adapter="droid", delivery="inbox", can_lead=True)
        rc, resp = self.harness.cli("intake", "submit", "--title", "Vendor gate", "--goal", "g",
                                    "--criterion", "C1", "--lead", name, token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _lead(self, conn: Any, project_id: str) -> dict:
        row = conn.execute(
            "SELECT id AS assignment_id, agent_id, project_id FROM lead_assignments "
            "WHERE project_id = ? AND ended_at IS NULL;", (project_id,)).fetchone()
        return dict(row)

    def test_vendor_lead_with_evidence_never_starts_a_turn(self) -> None:
        """ACC: N19 - a launch-enabled (evidenced) vendor lead gets no turn: plan is None,
        no lead_turn run, messages stay pending, exactly one lead.turn_refused
        {reason: vendor_lead_m3_lite} per assignment, and no vendor binary ran."""
        self.assertTrue(lead_turns.lead_launch_allowed(None, FakeAdapter(), testing_mode=True))
        self.assertFalse(lead_turns.lead_launch_allowed(None, _EvidencedDroid(), testing_mode=True))
        self.assertFalse(lead_turns.lead_launch_allowed(None, DroidAdapter(), testing_mode=True))

        project_id = self._inbox_vendor_project()
        self._follow_up(project_id, "wake the vendor lead")
        wake_before = [(r["id"], r["status"]) for r in self._lead_wake_rows(project_id)]
        # A writable connection (harness.db() is read-only): the refusal records an event.
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project_id)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project_id,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: _EvidencedDroid()):
                for _ in range(3):
                    plan = lead_turns.plan_lead_turn(conn, self.harness.home, project_id, lead,
                                                     list(messages), testing_mode=True)
                    self.assertIsNone(plan)
        finally:
            conn.close()

        refused = self._events(project_id, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        payload = json.loads(refused[0]["payload_json"])
        self.assertEqual(payload["reason"], "vendor_lead_m3_lite")
        self.assertEqual(payload["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(payload["milestone"], "M3")
        self.assertEqual(payload["lead_assignment_id"], lead["assignment_id"])
        self.assertEqual(self._lead_turn_runs(project_id), [])
        self.assertEqual([(r["id"], r["status"]) for r in self._lead_wake_rows(project_id)], wake_before)
        self.assertEqual({s for _, s in wake_before} - {"pending"}, set())

    def test_vendor_rule_runs_first(self) -> None:
        """ACC: N19 - the vendor rule runs first in `lead_launch_check` (coordinator ruling
        r4b): a vendor lead is refused with `vendor_lead_m3_lite` whether or not it is
        launch-enabled, and a non-vendor adapter that is not launch-enabled still gets
        the m2-r10 refusal (rc 13, no `details.reason`, reason
        `adapter_not_launch_enabled` in `lead.turn_refused`)."""
        for adapter in (DroidAdapter(), _EvidencedDroid()):
            for testing_mode in (True, False):
                with self.assertRaises(lead_turns.MeshError) as cm:
                    lead_turns.lead_launch_check(None, adapter, testing_mode=testing_mode)
                self.assertEqual(cm.exception.code, "ADAPTER_UNSUPPORTED")
                self.assertEqual(cm.exception.details, {"milestone": "M3", "reason": "vendor_lead_m3_lite"})
        with self.assertRaises(lead_turns.MeshError) as cm:
            lead_turns.lead_launch_check(None, _DisabledDouble(), testing_mode=True)
        self.assertEqual(cm.exception.code, "ADAPTER_UNSUPPORTED")
        self.assertNotIn("reason", cm.exception.details)

    def test_non_vendor_not_launch_enabled_refused_as_m2(self) -> None:
        """ACC: N19 - a launch lead on a non-vendor adapter that is not launch-enabled
        gets no turn and one `lead.turn_refused {reason: adapter_not_launch_enabled}`;
        a vendor lead that is not launch-enabled gets `vendor_lead_m3_lite`."""
        project_id = self._inbox_vendor_project()
        self._follow_up(project_id, "wake the lead")
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project_id)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project_id,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: _DisabledDouble()):
                self.assertIsNone(lead_turns.plan_lead_turn(conn, self.harness.home, project_id, lead,
                                                            list(messages), testing_mode=True))
        finally:
            conn.close()
        refused = self._events(project_id, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(json.loads(refused[0]["payload_json"])["reason"], "adapter_not_launch_enabled")
        self.assertEqual(self._lead_turn_runs(project_id), [])

        project2 = self._inbox_vendor_project("vlead2")
        self._follow_up(project2, "wake the vendor lead")
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            lead = self._lead(conn, project2)
            messages = conn.execute(
                "SELECT * FROM outbox WHERE project_id = ? AND status = 'pending' "
                "ORDER BY event_seq ASC, id ASC;", (project2,)).fetchall()
            with mock.patch.object(lead_turns, "get_adapter", lambda *a, **k: DroidAdapter()):
                self.assertIsNone(lead_turns.plan_lead_turn(conn, self.harness.home, project2, lead,
                                                            list(messages), testing_mode=True))
        finally:
            conn.close()
        refused = self._events(project2, "lead.turn_refused")
        self.assertEqual(len(refused), 1)
        self.assertEqual(json.loads(refused[0]["payload_json"])["reason"], "vendor_lead_m3_lite")
        self.assertEqual(self._lead_turn_runs(project2), [])

    def test_launch_enabled_vendor_lead_cli_refused_by_default(self) -> None:
        """ACC: N19 (L4 guard) - even with a launch-capable vendor agent (`droid`),
        CLI intake/project lead selection refuses by default with rc 13 and
        `details.reason == 'vendor_lead_m3_lite'`."""
        create_agent(self.harness, "vlaunch", adapter="droid", delivery="launch", can_lead=True)
        for cmd in (
            ("intake", "submit", "--title", "Guard", "--goal", "g", "--criterion", "C1", "--lead", "vlaunch"),
            ("project", "create", "--title", "Guard2", "--goal", "g", "--criterion", "C1", "--lead", "vlaunch"),
        ):
            rc, resp = self.harness.cli(*cmd, token=self.tok_grok)
            self.assertEqual(rc, 13, resp)
            self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
            self.assertEqual(resp["error"]["details"].get("reason"), "vendor_lead_m3_lite")


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SYNTH = os.path.join(_REPO_ROOT, "tests", "fixtures", "synthetic")
_CODEX_VER = "0.166.0"
_CODEX_MODEL = "gpt-6.1-sol"

_CODEX_LEAD_STUB = """#!{python}
import json, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.166.0"); sys.exit(0)
if args == ["--help"]:
    print("Usage: codex [OPTIONS] [PROMPT]\\n  -a, --ask-for-approval <POLICY>\\n  -C, --cd <DIR>\\n  -m, --model <MODEL>")
    sys.exit(0)
if args == ["exec", "--help"]:
    print("Usage: codex exec [OPTIONS] [PROMPT]\\n      --json\\n      --skip-git-repo-check\\n  -C, --cd <DIR>\\n"
          "  -m, --model <MODEL>\\n  -c, --config <key=value>\\n  -s, --sandbox <SANDBOX_MODE>\\n"
          "      --dangerously-bypass-approvals-and-sandbox")
    sys.exit(0)
if args == ["login", "status"]:
    print("Logged in using an API key"); sys.exit(0)
if "exec" in args and "--json" in args and args[-1] == "-":
    prompt = sys.stdin.read()
    with open({argv_log!r}, "w", encoding="utf-8") as af:
        json.dump(args, af)
    with open({stream!r}, "r", encoding="utf-8") as fp:
        sys.stdout.write(fp.read())
    sys.stdout.flush()
    sys.exit(0)
sys.exit(99)
"""


class TestVendorLeadOptIn(unittest.TestCase):
    """L4 + C-5: explicit ``lead.vendor_leads`` opt-in gated by evidence.effective_launch."""

    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-vlead-optin-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stage = os.path.join(self.tmp, "S")
        self.stubs_dir = os.path.join(self.tmp, "stubs")
        os.makedirs(self.stubs_dir, mode=0o700)
        create_test_stubs(self.stubs_dir)
        self._stage_codex_evidence()

        self.harness = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.harness.start_daemon(extra_env={evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")})

        self.tok_intake = create_agent(self.harness, "intake1", adapter="none", delivery="inbox",
                                       capabilities=["intake"])
        self.tok_relayer = create_agent(self.harness, "relayer", adapter="none", delivery="inbox",
                                        capabilities=["intake", "relay"])
        self.tok_plain = create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "1", user=True)
        self.assertEqual(rc, 0, resp)

    def _stage_codex_evidence(self) -> None:
        with open(os.path.join(_SYNTH, "codex", "synthetic_ok.jsonl"), "rb") as f:
            data = f.read()
        cases = []
        for cid in ("S-X-1", "S-X-6"):
            name = f"smoke-{cid}-stdout.jsonl"
            tierlib.write_capture(self.stage, f"codex/{_CODEX_VER}/{name}", data, ["codex"])
            cases.append({
                "id": cid, "status": "pass", "reason": None, "rc": 0, "duration_s": 1.0,
                "fixture": smoke_schema.fixture_rel("codex", _CODEX_VER, name),
                "fixture_sha256": tierlib.sha256_bytes(data), "observation": None,
                "evaluated_from": None, "carried_from": None,
            })
        doc = {
            "schema": smoke_schema.SCHEMA, "adapter": "codex", "binary": "/usr/local/bin/codex",
            "version": _CODEX_VER, "captured_at_utc": "2026-11-16T01:02:03Z", "mesh_commit": "abc1234",
            "model": _CODEX_MODEL, "effort": None, "profile": "readonly", "timeout_s": 180,
            "consent_ref": "test", "cases": cases,
        }
        rname = smoke_schema.results_name("codex", _CODEX_VER, "2026-11-16", [])
        tierlib.write_capture(self.stage, f"results/{rname}", json.dumps(doc).encode(), ["test"])

    def _make_codex_stub(self, tag: str, stream_path: Optional[str] = None,
                         argv_log: Optional[str] = None, probe: bool = True) -> str:
        bdir = os.path.join(self.tmp, f"bin-{tag}")
        sp = stream_path or os.path.join(_SYNTH, "codex", "synthetic_ok.jsonl")
        al = argv_log or os.path.join(self.tmp, f"argv-{tag}.json")
        path = _write_python_stub(bdir, "codex", _CODEX_LEAD_STUB.format(python=sys.executable, stream=sp, argv_log=al))
        if probe:
            time.sleep(0.05)
            rc, resp = self.harness.cli("adapter", "probe", "codex", "--binary", path, user=True)
            self.assertEqual(rc, 0, resp)
            self.assertEqual((resp["data"]["status"], resp["data"]["version"]), ("partially_verified", _CODEX_VER))
        return path

    def test_lead_vendor_leads_setting_requires_direct_user_and_boolean(self) -> None:
        rc, resp = self.harness.cli("config", "get", "lead.vendor_leads", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["value"], False)

        # Agent callers (with or without relay) are refused rc 4 CAPABILITY_MISSING.
        for tok in (self.tok_plain, self.tok_relayer):
            rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "true", token=tok)
            self.assertEqual(rc, 4, resp)
            self.assertEqual(resp["error"]["code"], "CAPABILITY_MISSING")
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "true",
                                    "--relayed", "--user-statement", "please", token=self.tok_relayer)
        self.assertEqual(rc, 4, resp)
        self.assertEqual(resp["error"]["code"], "CAPABILITY_MISSING")

        # Non-boolean values rejected rc 2 INVALID_SETTING.
        for bad in ('"true"', "1", "null"):
            rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", bad, user=True)
            self.assertEqual(rc, 2, resp)
            self.assertEqual(resp["error"]["code"], "INVALID_SETTING")

        # Direct user succeeds and toggles cleanly.
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "true", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "get", "lead.vendor_leads", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIs(resp["data"]["value"], True)
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "false", user=True)
        self.assertEqual(rc, 0, resp)

    def test_lead_launch_check_unit_c5_opt_in_and_toggle(self) -> None:
        conn = init_db(os.path.join(self.tmp, "c5.db"))
        self.addCleanup(conn.close)
        now = now_iso_utc()
        adapter = get_adapter("codex", testing_mode=True)

        # Default (off): even passing evidence is refused with vendor_lead_m3_lite without calling effective_launch.
        with mock.patch.object(evidence, "effective_launch", return_value=Evidence(ok=True)) as ev_mock:
            with self.assertRaises(MeshError) as cm:
                lead_turns.lead_launch_check(conn, adapter, testing_mode=True, binary_path="/x/codex")
        self.assertEqual(cm.exception.details.get("reason"), "vendor_lead_m3_lite")
        ev_mock.assert_not_called()

        # Opt-in ON + no/failing evidence -> refused with evidence reason.
        with transaction(conn):
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('lead.vendor_leads', 'true', ?, 'u');",
                (now,),
            )
        with self.assertRaises(MeshError) as cm:
            lead_turns.lead_launch_check(conn, adapter, testing_mode=True, binary_path="/x/codex")
        self.assertEqual(cm.exception.code, "ADAPTER_UNSUPPORTED")
        self.assertIn(cm.exception.details.get("reason"), ("no_smoke_evidence", "no_probe_version"))

        # Opt-in ON + C-5 effective_launch invoked with binary_path and allowed when passing.
        with mock.patch.object(evidence, "effective_launch", return_value=Evidence(ok=True)) as ev_mock:
            self.assertIsNone(
                lead_turns.lead_launch_check(conn, adapter, testing_mode=True, binary_path="/x/codex")
            )
            self.assertTrue(lead_turns.lead_launch_allowed(conn, adapter, testing_mode=True, binary_path="/x/codex"))
        self.assertEqual(ev_mock.call_args_list, [
            mock.call(conn, adapter, True, "/x/codex"),
            mock.call(conn, adapter, True, "/x/codex"),
        ])

        # Toggling off again refuses with vendor_lead_m3_lite even with passing evidence mock.
        with transaction(conn):
            conn.execute("UPDATE settings SET value_json = 'false' WHERE key = 'lead.vendor_leads';")
        with mock.patch.object(evidence, "effective_launch", return_value=Evidence(ok=True)) as ev_mock:
            with self.assertRaises(MeshError) as cm:
                lead_turns.lead_launch_check(conn, adapter, testing_mode=True, binary_path="/x/codex")
        self.assertEqual(cm.exception.details.get("reason"), "vendor_lead_m3_lite")
        ev_mock.assert_not_called()

    def test_vendor_lead_follows_evidence_mode_warn_and_holds(self) -> None:
        """Vendor leads follow the same adapter.evidence_mode policy as workers: warn launches
        with an evidence warning, strict refuses without evidence, and holds/stubs still refuse."""
        conn = init_db(os.path.join(self.tmp, "warnmode.db"))
        self.addCleanup(conn.close)
        now = now_iso_utc()
        with transaction(conn):
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('lead.vendor_leads', 'true', ?, 'u');",
                (now,),
            )
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at, updated_by) VALUES ('adapter.evidence_mode', '\"warn\"', ?, 'u');",
                (now,),
            )

        # (a) Warn mode + missing/stale evidence -> allowed with matching warning (same as workers).
        adapter_prod = get_adapter("codex", testing_mode=False)
        missing_bin = os.path.join(self.tmp, "nonexistent-codex")
        ev_exec = evidence.effective_launch(conn, adapter_prod, testing_mode=False, binary_path=missing_bin)
        self.assertTrue(ev_exec.ok, ev_exec)
        self.assertTrue(ev_exec.warning, ev_exec)
        warn_ret = lead_turns.lead_launch_check(conn, adapter_prod, testing_mode=False, binary_path=missing_bin)
        self.assertEqual(warn_ret, ev_exec.warning)
        self.assertTrue(lead_turns.lead_launch_allowed(conn, adapter_prod, testing_mode=False, binary_path=missing_bin))

        adapter_test = get_adapter("codex", testing_mode=True)
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")}):
            # no_probe_version in warn mode -> launches with warning
            self.assertEqual(
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=missing_bin),
                "no_probe_version",
            )
            # no_smoke_evidence in warn mode (droid has no results in staged dir) -> launches with warning
            droid_test = get_adapter("droid", testing_mode=True)
            self.assertEqual(
                lead_turns.lead_launch_check(conn, droid_test, testing_mode=True, binary_path=missing_bin),
                "no_smoke_evidence",
            )
            # evidence_stale in warn mode -> launches with warning
            stale_bin = self._make_codex_stub("warn_stale", probe=False)
            with transaction(conn):
                conn.execute(
                    "INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, status, flags_json) "
                    "VALUES ('codex', ?, ?, ?, 'verified', '{}');",
                    (now_iso_utc(time.time() - 120), stale_bin, _CODEX_VER),
                )
            self.assertEqual(
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=stale_bin),
                "evidence_stale",
            )
            # Valid matching evidence -> allowed with warning=None
            with transaction(conn):
                conn.execute(
                    "INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, status, flags_json) "
                    "VALUES ('codex', ?, ?, ?, 'verified', '{}');",
                    (now_iso_utc(time.time() + 10), stale_bin, _CODEX_VER),
                )
            self.assertIsNone(
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=stale_bin)
            )

        # (d) Launch holds and unimplemented stubs still refuse in warn mode.
        devin = get_adapter("devin", testing_mode=False)
        with self.assertRaises(MeshError) as cm_hold:
            lead_turns.lead_launch_check(conn, devin, testing_mode=False, binary_path="/x/devin")
        self.assertEqual(cm_hold.exception.code, "ADAPTER_UNSUPPORTED")
        self.assertEqual(cm_hold.exception.details.get("reason"), "launch_held")
        self.assertEqual(cm_hold.exception.details.get("milestone"), "M5")

        with mock.patch.dict(os.environ, {evidence.HOLD_ENV: "codex:M9"}):
            with self.assertRaises(MeshError) as cm_testhold:
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=stale_bin)
            self.assertEqual(cm_testhold.exception.details.get("reason"), "launch_held")
            self.assertEqual(cm_testhold.exception.details.get("milestone"), "M9")

        for stub_id in ("hermes", "openclaw"):
            stub = get_adapter(stub_id, testing_mode=False)
            with self.assertRaises(MeshError) as cm_stub:
                lead_turns.lead_launch_check(conn, stub, testing_mode=False, binary_path=f"/x/{stub_id}")
            self.assertEqual(cm_stub.exception.code, "ADAPTER_UNSUPPORTED")
            self.assertEqual(cm_stub.exception.details.get("reason"), "no_smoke_evidence")

        # (b) Switching to strict mode refuses missing/stale evidence (same as workers).
        with transaction(conn):
            conn.execute("UPDATE settings SET value_json = '\"strict\"' WHERE key = 'adapter.evidence_mode';")
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")}):
            with self.assertRaises(MeshError) as cm_strict:
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=missing_bin)
            self.assertEqual(cm_strict.exception.details.get("reason"), "no_probe_version")

        # (c) Switching lead.vendor_leads off refuses even in warn mode with valid evidence.
        with transaction(conn):
            conn.execute("UPDATE settings SET value_json = '\"warn\"' WHERE key = 'adapter.evidence_mode';")
            conn.execute("UPDATE settings SET value_json = 'false' WHERE key = 'lead.vendor_leads';")
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: os.path.join(self.stage, "results")}):
            with self.assertRaises(MeshError) as cm_off:
                lead_turns.lead_launch_check(conn, adapter_test, testing_mode=True, binary_path=stale_bin)
            self.assertEqual(cm_off.exception.details.get("reason"), "vendor_lead_m3_lite")

    def test_warn_mode_launches_vendor_lead_with_warning_and_holds_refuse_e2e(self) -> None:
        """E2E: (a) vendor_leads on + evidence_mode=warn + no/stale evidence launches lead turn
        with CLI warning and lead.turn_started evidence_warning; (b) strict refuses; (c) vendor_leads
        off refuses in warn mode; (d) launch hold still refuses CLI and live turn planning."""
        stream_path = os.path.join(self.tmp, "codex_warn_actions.jsonl")
        with open(stream_path, "w", encoding="utf-8") as f:
            for item in (
                {"type": "thread.started", "thread_id": "th_vwarn_1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_w1",
                        "type": "agent_message",
                        "text": (
                            'Warn mode note.\n```mesh-actions\n'
                            '[{"cmd":"project.note","args":{"note":"warn mode lead ok"}}]\n```'
                        ),
                    },
                },
                {"type": "turn.completed"},
            ):
                f.write(json.dumps(item) + "\n")

        unprobed_bin = self._make_codex_stub("e2e_unprobed", stream_path=stream_path, probe=False)
        stale_bin = self._make_codex_stub("e2e_stale", stream_path=stream_path, probe=True)
        os.utime(stale_bin, (time.time() + 120, time.time() + 120))
        ev_bin = self._make_codex_stub("e2e_ev", stream_path=stream_path, probe=True)
        devin_bin = os.path.join(self.tmp, "dummy-devin.cmd" if sys.platform == "win32" else "dummy-devin")
        with open(devin_bin, "w", encoding="utf-8") as df:
            df.write("@echo off\r\nexit /b 0\r\n" if sys.platform == "win32" else "#!/bin/sh\nexit 0\n")
        os.chmod(devin_bin, 0o755)

        for name, ad, bpath, prof in (
            ("cx_w_unprobed", "codex", unprobed_bin, "readonly"),
            ("cx_w_stale", "codex", stale_bin, "readonly"),
            ("cx_w_ev", "codex", ev_bin, "readonly"),
            ("dv_held", "devin", devin_bin, "unrestricted"),
        ):
            rc, resp = self.harness.setup_cli(
                "agent", "create", "--name", name, "--adapter", ad, "--delivery", "launch",
                "--can-lead", "--max-concurrency", "1", "--binary", bpath,
                "--default-profile", prof, "--allowed-model", _CODEX_MODEL,
                "--default-model", _CODEX_MODEL, user=True,
            )
            self.assertEqual(rc, 0, resp)

        # Configure warn mode first, with lead.vendor_leads still OFF -> (c) still refused!
        rc, resp = self.harness.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)
        self.assertEqual(rc, 0, resp)
        for lead_name in ("cx_w_ev", "cx_w_unprobed"):
            rc, resp = self.harness.cli(
                "intake", "submit", "--title", f"Off-{lead_name}", "--goal", "g",
                "--criterion", "C1", "--lead", lead_name, token=self.tok_intake,
            )
            self.assertEqual(rc, 13, resp)
            self.assertEqual(resp["error"]["details"].get("reason"), "vendor_lead_m3_lite")

        # Enable lead.vendor_leads.
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "true", user=True)
        self.assertEqual(rc, 0, resp)

        # (d) Launch hold (devin) still refuses in warn mode even with lead.vendor_leads=true!
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", "DevinHeld", "--goal", "g",
            "--criterion", "C1", "--lead", "dv_held", token=self.tok_intake,
        )
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(resp["error"]["details"].get("reason"), "launch_held")
        self.assertEqual(resp["error"]["details"].get("milestone"), "M5")

        # (a) Warn mode + no_probe_version and evidence_stale -> CLI warns, lead turn executes,
        # and lead.turn_started records evidence_warning!
        def _wait_turn_done(prj_id: str) -> None:
            def _done() -> bool:
                c = self.harness.db()
                try:
                    r = c.execute("SELECT status FROM runs WHERE kind = 'lead_turn' AND project_id = ?;",
                                  (prj_id,)).fetchone()
                    return r is not None and r["status"] == "exited"
                finally:
                    c.close()

            self.assertTrue(self.harness.wait_for(_done, timeout=30.0, step=0.1),
                            f"warn-mode vendor lead turn for {prj_id} did not exit")

        for tag, lead_name, expected_warn in (
            ("unprobed", "cx_w_unprobed", "no_probe_version"),
            ("stale", "cx_w_stale", "evidence_stale"),
        ):
            rc, resp = self.harness.cli(
                "intake", "submit", "--title", f"Warn-{tag}", "--goal", "Run warn lead",
                "--criterion", "C1", "--lead", lead_name, token=self.tok_intake,
            )
            self.assertEqual(rc, 0, resp)
            self.assertTrue(resp.get("warnings"), resp)
            self.assertTrue(any(expected_warn in w and "codex" in w for w in resp["warnings"]), resp)
            prj_id = resp["data"]["id"]
            _wait_turn_done(prj_id)

            conn = self.harness.db()
            try:
                run = conn.execute("SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ?;",
                                   (prj_id,)).fetchone()
                self.assertEqual((run["status"], run["exit_code"], run["adapter"], run["model"]),
                                 ("exited", 0, "codex", _CODEX_MODEL))
                started_ev = conn.execute(
                    "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_started';",
                    (prj_id,),
                ).fetchone()
                self.assertIsNotNone(started_ev)
                self.assertEqual(json.loads(started_ev["payload_json"]).get("evidence_warning"), expected_warn)
                notes = conn.execute(
                    "SELECT payload_json FROM events WHERE project_id = ? AND type = 'project.note';",
                    (prj_id,),
                ).fetchall()
                self.assertEqual(len(notes), 1)
                self.assertEqual(json.loads(notes[0]["payload_json"]).get("text"), "warn mode lead ok")
                out_statuses = [
                    r["status"] for r in conn.execute(
                        "SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (prj_id,)
                    ).fetchall()
                ]
                self.assertEqual(out_statuses, ["acked"])
            finally:
                conn.close()

        # project set-lead in warn mode also surfaces the CLI warning.
        rc, resp = self.harness.cli(
            "project", "create", "--title", "SwapWarn", "--goal", "g",
            "--criterion", "C1", "--lead", "lead1", token=self.tok_intake,
        )
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp.get("warnings"), [])
        prj_swap = resp["data"]["id"]
        rc, resp = self.harness.cli("project", "set-lead", prj_swap, "cx_w_unprobed", "--reason", "warn swap", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(any("no_probe_version" in w and "codex" in w for w in resp.get("warnings", [])), resp)
        _wait_turn_done(prj_swap)

        # P3-1: idempotent re-set of the same active warn-mode vendor lead keeps unchanged=True and warnings.
        rc, resp_same = self.harness.cli(
            "project", "set-lead", prj_swap, "cx_w_unprobed", "--reason", "idempotent warn re-set", user=True
        )
        self.assertEqual(rc, 0, resp_same)
        self.assertTrue(resp_same["data"].get("unchanged"), resp_same)
        self.assertEqual(resp_same["data"].get("assignment_id"), resp["data"].get("assignment_id"))
        self.assertTrue(any("no_probe_version" in w and "codex" in w for w in resp_same.get("warnings", [])), resp_same)

        # P3-3(2): project create --lead <warn-mode unprobed vendor lead> returns resp["warnings"].
        rc, resp_create = self.harness.cli(
            "project", "create", "--title", "CreateWarn", "--goal", "g",
            "--criterion", "C1", "--lead", "cx_w_unprobed", token=self.tok_intake,
        )
        self.assertEqual(rc, 0, resp_create)
        self.assertTrue(any("no_probe_version" in w and "codex" in w for w in resp_create.get("warnings", [])), resp_create)
        _wait_turn_done(resp_create["data"]["id"])

        # (a2) Also verify no_smoke_evidence in warn mode launches with warning:
        # restart daemon without staged codex results and without MESH_TEST_LAUNCH_HOLD.
        self.harness.stop_daemon()
        empty_res = os.path.join(self.tmp, "empty-results")
        os.makedirs(empty_res, exist_ok=True)
        self.harness.start_daemon(extra_env={evidence.OVERRIDE_ENV: empty_res})
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", "NoSmokeWarn", "--goal", "g",
            "--criterion", "C1", "--lead", "cx_w_ev", token=self.tok_intake,
        )
        self.assertEqual(rc, 0, resp)
        self.assertTrue(any("no_smoke_evidence" in w and "codex" in w for w in resp.get("warnings", [])), resp)
        prj_nosmoke = resp["data"]["id"]
        _wait_turn_done(prj_nosmoke)
        conn = self.harness.db()
        try:
            started_ev = conn.execute(
                "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_started';",
                (prj_nosmoke,),
            ).fetchone()
            self.assertEqual(json.loads(started_ev["payload_json"]).get("evidence_warning"), "no_smoke_evidence")
        finally:
            conn.close()

        # (b) Switching adapter.evidence_mode back to strict refuses no_smoke_evidence!
        rc, resp = self.harness.cli("config", "set", "adapter.evidence_mode", '"strict"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", "NoSmokeStrict", "--goal", "g",
            "--criterion", "C1", "--lead", "cx_w_ev", token=self.tok_intake,
        )
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["details"].get("reason"), "no_smoke_evidence")

        # (d2) Launch hold refuses live turn planning in warn mode too (pre-assigned lead + MESH_TEST_LAUNCH_HOLD).
        rc, resp = self.harness.cli("config", "set", "adapter.evidence_mode", '"warn"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "30", user=True)
        self.assertEqual(rc, 0, resp)
        self.harness.hold_launch_starts()
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", "HoldLivePlan", "--goal", "g",
            "--criterion", "C1", "--lead", "cx_w_ev", token=self.tok_intake,
        )
        self.assertEqual(rc, 0, resp)
        prj_held = resp["data"]["id"]
        self.harness.stop_daemon()
        self.harness.release_launch_starts()
        self.harness.start_daemon(extra_env={
            evidence.OVERRIDE_ENV: os.path.join(self.stage, "results"),
            evidence.HOLD_ENV: "codex:M9",
        })
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "1", user=True)
        self.assertEqual(rc, 0, resp)

        def _held_refused():
            c = self.harness.db()
            try:
                return c.execute(
                    "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
                    (prj_held,),
                ).fetchall()
            finally:
                c.close()

        self.assertTrue(self.harness.wait_for(lambda: len(_held_refused()) >= 1, timeout=15.0, step=0.1))
        held_payload = json.loads(_held_refused()[0]["payload_json"])
        self.assertEqual(held_payload["reason"], "launch_held")
        self.assertEqual(held_payload["milestone"], "M9")
        conn = self.harness.db()
        try:
            self.assertEqual(
                conn.execute("SELECT 1 FROM runs WHERE project_id = ? AND kind = 'lead_turn';",
                             (prj_held,)).fetchall(),
                [],
            )
            self.assertEqual(
                {r["status"] for r in conn.execute(
                    "SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (prj_held,)
                ).fetchall()},
                {"pending"},
            )
        finally:
            conn.close()

    def test_opt_in_evidence_gate_e2e_and_toggle_off(self) -> None:
        stream_path = os.path.join(self.tmp, "codex_lead_actions.jsonl")
        with open(stream_path, "w", encoding="utf-8") as f:
            for item in (
                {"type": "thread.started", "thread_id": "th_vlead_1"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": (
                            'Recording a note.\n```mesh-actions\n'
                            '[{"cmd":"project.note","args":{"note":"vendor lead note ok"}}]\n```'
                        ),
                    },
                },
                {"type": "turn.completed"},
            ):
                f.write(json.dumps(item) + "\n")
        argv_log = os.path.join(self.tmp, "codex_argv.json")
        probed_bin = self._make_codex_stub("probed", stream_path=stream_path, argv_log=argv_log, probe=True)
        unprobed_bin = self._make_codex_stub("unprobed", probe=False)

        # Create evidenced launch codex agent, unprobed launch codex agent, and un-evidenced droid agent.
        for name, ad, bpath, model in (
            ("cx_ev", "codex", probed_bin, _CODEX_MODEL),
            ("cx_unprobed", "codex", unprobed_bin, _CODEX_MODEL),
            ("dr_noev", "droid", None, "fake-model"),
        ):
            args = [
                "agent", "create", "--name", name, "--adapter", ad, "--delivery", "launch",
                "--can-lead", "--max-concurrency", "1",
                "--allowed-model", model, "--default-model", model,
            ]
            if bpath is not None:
                args.extend(["--binary", bpath])
            rc, resp = self.harness.setup_cli(*args, user=True)
            self.assertEqual(rc, 0, resp)

        # 1. Default (lead.vendor_leads false): even evidenced vendor lead is refused with vendor_lead_m3_lite.
        for cmd in (
            ("intake", "submit", "--title", "V0", "--goal", "g", "--criterion", "C1", "--lead", "cx_ev"),
            ("project", "create", "--title", "V0b", "--goal", "g", "--criterion", "C1", "--lead", "cx_ev"),
        ):
            rc, resp = self.harness.cli(*cmd, token=self.tok_intake)
            self.assertEqual(rc, 13, resp)
            self.assertEqual(resp["error"]["details"].get("reason"), "vendor_lead_m3_lite")

        # 2. Turn on lead.vendor_leads via direct user passphrase auth.
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "true", user=True)
        self.assertEqual(rc, 0, resp)

        # 2a. Opt-in ON + no probe for agent's binary -> refused with no_probe_version (binary_path wired).
        for name, expected_reason in (("cx_unprobed", "no_probe_version"), ("dr_noev", "no_smoke_evidence")):
            rc, resp = self.harness.cli(
                "intake", "submit", "--title", f"Bad-{name}", "--goal", "g", "--criterion", "C1", "--lead", name,
                token=self.tok_intake,
            )
            self.assertEqual(rc, 13, resp)
            self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
            self.assertEqual(resp["error"]["details"].get("reason"), expected_reason)

        # Turn planning in the live daemon refuses a launch vendor agent once its evidence goes
        # stale (`evidence_stale`), recording one `lead.turn_refused` and keeping wakes pending.
        stale_bin = self._make_codex_stub("stale_plan", probe=True)
        rc, resp = self.harness.setup_cli(
            "agent", "create", "--name", "cx_stale", "--adapter", "codex", "--delivery", "launch",
            "--can-lead", "--max-concurrency", "1", "--binary", stale_bin,
            "--allowed-model", _CODEX_MODEL, "--default-model", _CODEX_MODEL, user=True,
        )
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "30", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("intake", "submit", "--title", "TurnPlanRefusal", "--goal", "g",
                                    "--criterion", "C1", "--lead", "cx_stale", token=self.tok_intake)
        self.assertEqual(rc, 0, resp)
        p_refuse = resp["data"]["id"]
        future_ts = time.time() + 120
        os.utime(stale_bin, (future_ts, future_ts))
        rc, resp = self.harness.cli("config", "set", "lead.wake_debounce_s", "1", user=True)
        self.assertEqual(rc, 0, resp)

        def _refused_rows():
            c = self.harness.db()
            try:
                return c.execute(
                    "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
                    (p_refuse,),
                ).fetchall()
            finally:
                c.close()

        self.assertTrue(self.harness.wait_for(lambda: len(_refused_rows()) >= 1, timeout=15.0, step=0.1))
        ref = _refused_rows()
        self.assertEqual(len(ref), 1)
        self.assertEqual(json.loads(ref[0]["payload_json"])["reason"], "evidence_stale")
        c_chk = self.harness.db()
        try:
            self.assertEqual(
                c_chk.execute("SELECT 1 FROM runs WHERE project_id = ? AND kind = 'lead_turn';", (p_refuse,)).fetchall(),
                [],
            )
            w_stats = [
                r["status"] for r in c_chk.execute(
                    "SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (p_refuse,)
                ).fetchall()
            ]
            self.assertTrue(w_stats)
            self.assertEqual(set(w_stats), {"pending"})
        finally:
            c_chk.close()

        # 3. Opt-in ON + valid C-5 evidence: project create, set-lead, and full lead turn execution succeed!
        rc, resp = self.harness.cli("project", "create", "--title", "PlainPrj", "--goal", "g",
                                    "--criterion", "C1", "--lead", "lead1", token=self.tok_intake)
        self.assertEqual(rc, 0, resp)
        prj_swap = resp["data"]["id"]
        rc, resp = self.harness.cli("project", "set-lead", prj_swap, "cx_unprobed", "--reason", "test", user=True)
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["details"].get("reason"), "no_probe_version")
        rc, resp = self.harness.cli("project", "set-lead", prj_swap, "cx_ev", "--reason", "test", user=True)
        self.assertEqual(rc, 0, resp)

        rc, resp = self.harness.cli("intake", "submit", "--title", "VendorLeadGo", "--goal", "Run vendor turn",
                                    "--criterion", "C1", "--lead", "cx_ev", token=self.tok_intake)
        self.assertEqual(rc, 0, resp)
        prj_go = resp["data"]["id"]

        def _turn_done() -> bool:
            c = self.harness.db()
            try:
                r = c.execute("SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ?;", (prj_go,)).fetchone()
                return r is not None and r["status"] == "exited"
            finally:
                c.close()

        self.assertTrue(self.harness.wait_for(_turn_done, timeout=30.0, step=0.1), "vendor lead turn did not exit")
        conn = self.harness.db()
        try:
            run = conn.execute("SELECT * FROM runs WHERE kind = 'lead_turn' AND project_id = ?;", (prj_go,)).fetchone()
            self.assertEqual((run["status"], run["exit_code"], run["adapter"], run["model"]),
                             ("exited", 0, "codex", _CODEX_MODEL))
            lease = conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"leadturn:{prj_go}",)).fetchone()
            self.assertIsNone(lease)
            notes = conn.execute(
                "SELECT payload_json FROM events WHERE project_id = ? AND type = 'project.note';", (prj_go,)
            ).fetchall()
            self.assertEqual(len(notes), 1)
            self.assertEqual(json.loads(notes[0]["payload_json"]).get("text"), "vendor lead note ok")
            out_statuses = [
                r["status"] for r in conn.execute(
                    "SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (prj_go,)
                ).fetchall()
            ]
            self.assertEqual(out_statuses, ["acked"])
        finally:
            conn.close()
        with open(argv_log, "r", encoding="utf-8") as af:
            called_argv = json.load(af)
        self.assertIn("--model", called_argv)
        self.assertEqual(called_argv[called_argv.index("--model") + 1], _CODEX_MODEL)

        # 4. Toggle OFF again -> refuses vendor leads again with vendor_lead_m3_lite!
        rc, resp = self.harness.cli("config", "set", "lead.vendor_leads", "false", user=True)
        self.assertEqual(rc, 0, resp)
        for cmd in (
            ("intake", "submit", "--title", "Voff", "--goal", "g", "--criterion", "C1", "--lead", "cx_ev"),
            ("project", "set-lead", prj_go, "cx_ev", "--reason", "toggle off"),
        ):
            use_kw = {"user": True} if cmd[0] == "project" else {"token": self.tok_intake}
            rc, resp = self.harness.cli(*cmd, **use_kw)
            self.assertEqual(rc, 13, resp)
            self.assertEqual(resp["error"]["details"].get("reason"), "vendor_lead_m3_lite")


class TestVendorLeadRefusalCacheLaunchPath(unittest.TestCase):
    """r3 P1-1, P3-1, P3-2, P3-6: production `start_lead_turns` path on `delivery='launch'`
    vendor agents (no inbox shortcut / no live-daemon second-connection race)."""

    def setUp(self) -> None:
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-vlead-cache-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, mode=0o700)
        self.conn = init_db(os.path.join(self.home, "mesh.db"))
        self.addCleanup(self.conn.close)
        lead_turns._VENDOR_REFUSAL_CACHE.clear()
        self.addCleanup(lead_turns._VENDOR_REFUSAL_CACHE.clear)
        self.addCleanup(self._cleanup_wrappers)
        self.seq = 0
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO principals (id, kind, name, created_at) VALUES ('usr_c','user','u',?);", (now,))
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('boot_id', 'boot_cache_test');")
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('lead.vendor_leads', 'true', ?, 'usr_c');", (now,),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('lead.wake_debounce_s', '0', ?, 'usr_c');", (now,),
            )

    def _cleanup_wrappers(self) -> None:
        for rid, proc in list(supervisor._WRAPPERS.items()):
            supervisor._WRAPPERS.pop(rid, None)
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass

    def _make_codex_bin(self, tag: str) -> str:
        bdir = os.path.join(self.tmp, f"bin-{tag}")
        stream = os.path.join(_SYNTH, "codex", "synthetic_ok.jsonl")
        argv_log = os.path.join(self.tmp, f"argv-{tag}.json")
        return _write_python_stub(bdir, "codex", _CODEX_LEAD_STUB.format(python=sys.executable, stream=stream, argv_log=argv_log))

    def _seed_launch_lead(self, tag: str, binary_path: str, probe_at: Optional[str] = None) -> Dict[str, str]:
        self.seq += 1
        pid = f"prj_{tag}_{self.seq}"
        aid = f"agt_{tag}_{self.seq}"
        las_id = f"las_{tag}_{self.seq}"
        out_id = f"out_{tag}_{self.seq}"
        old_ts = now_iso_utc(time.time() - 60)
        now = now_iso_utc()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, ?);",
                (aid, f"lead_{tag}_{self.seq}", now),
            )
            self.conn.execute(
                "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, binary_path, "
                "default_model, created_at, updated_at) VALUES (?, 'codex', 'launch', 1, 1, ?, ?, ?, ?);",
                (aid, binary_path, _CODEX_MODEL, now, now),
            )
            self.conn.execute(
                "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                "VALUES (?, ?, 'Goal', 'active', 'usr_c', 'direct', ?, ?);",
                (pid, f"P_{tag}_{self.seq}", now, now),
            )
            self.conn.execute(
                "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
                "VALUES (?, ?, ?, 'usr_c', 'direct', ?);",
                (las_id, pid, aid, now),
            )
            ev = record_event(
                self.conn, type_="project.note", actor_id="usr_c", actor_kind="user",
                via="direct", project_id=pid, payload={"text": f"wake {tag}"},
            )
            self.conn.execute(
                "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                "type, payload_json, status, attempts, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead.wake', '{}', 'pending', 0, ?, ?);",
                (out_id, ev["seq"], aid, f"dk_{out_id}", pid, old_ts, old_ts),
            )
            if probe_at is not None:
                self.conn.execute(
                    "INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, status, flags_json) "
                    "VALUES ('codex', ?, ?, ?, 'verified', '{}');",
                    (probe_at, binary_path, _CODEX_VER),
                )
        return {"project_id": pid, "agent_id": aid, "assignment_id": las_id, "outbox_id": out_id}

    def _stage_fx(self, tag: str, *, bad_fixture: bool = False, fail_case: bool = False) -> Dict[str, Any]:
        stage = os.path.join(self.tmp, f"stage_{tag}")
        rdir = os.path.join(stage, "results")
        with open(os.path.join(_SYNTH, "codex", "synthetic_ok.jsonl"), "rb") as f:
            fx_data = f.read()
        fx_paths: Dict[str, str] = {}
        cases = []
        for idx, cid in enumerate(("S-X-1", "S-X-6")):
            fname = f"smoke-{cid}-stdout.jsonl"
            rel = f"codex/{_CODEX_VER}/{fname}"
            tierlib.write_capture(stage, rel, fx_data, ["codex"])
            fx_paths[cid] = os.path.join(stage, *rel.split("/"))
            cases.append({
                "id": cid,
                "status": "fail" if (fail_case and idx == 0) else "pass",
                "reason": None,
                "rc": 0,
                "duration_s": 1.0,
                "fixture": smoke_schema.fixture_rel("codex", _CODEX_VER, fname),
                "fixture_sha256": ("0" * 64) if (bad_fixture and idx == 0) else tierlib.sha256_bytes(fx_data),
                "observation": None,
                "evaluated_from": None,
                "carried_from": None,
            })
        if bad_fixture:
            # Make fixture bytes match the recorded sha256 only after fixture repair? Or corrupt bytes:
            # write bad bytes on disk while results file expects tierlib.sha256_bytes(fx_data)!
            bad_bytes = b'{"tampered": true}\n'
            with open(fx_paths["S-X-1"], "wb") as f:
                f.write(bad_bytes)
            with open(fx_paths["S-X-1"] + tierlib.SIDECAR_SUFFIX, "wb") as sf:
                sf.write(tierlib.sidecar_bytes(["codex"], bad_bytes))
            cases[0]["fixture_sha256"] = tierlib.sha256_bytes(fx_data)
        doc = {
            "schema": smoke_schema.SCHEMA, "adapter": "codex", "binary": "/usr/local/bin/codex",
            "version": _CODEX_VER, "captured_at_utc": "2026-11-16T01:02:03Z", "mesh_commit": "abc1234",
            "model": _CODEX_MODEL, "effort": None, "profile": "readonly", "timeout_s": 180,
            "consent_ref": "test", "cases": cases,
        }
        rname = smoke_schema.results_name("codex", _CODEX_VER, "2026-11-16", [])
        rfile = tierlib.write_capture(stage, f"results/{rname}", json.dumps(doc).encode("utf-8"), ["test"])
        return {"stage": stage, "rdir": rdir, "rfile": rfile, "doc": doc, "cases": cases,
                "fx_paths": fx_paths, "fx_data": fx_data}

    def _drain_turn(self, run_id: str) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            supervisor.watch_launch_runs(self.conn, self.home, testing_mode=True)
            supervisor.confirm_pending_exits(self.conn, self.home, testing_mode=True)
            row = self.conn.execute("SELECT status FROM runs WHERE id = ?;", (run_id,)).fetchone()
            if row and row["status"] not in ("starting", "running"):
                return
            time.sleep(0.02)
        self.fail(f"launch vendor stub turn {run_id} did not close")

    def test_p1_1_refusal_cache_no_traceback_growth_on_launch_lead(self) -> None:
        """r3 P1-1: 200 ticks of `start_lead_turns` on a refused vendor launch lead cache only
        error metadata, build fresh `MeshError` instances, keep traceback depth constant, and
        retain bounded memory."""
        st = self._stage_fx("leak")
        codex_bin = self._make_codex_bin("leak")
        # Probe timestamp BEFORE binary mtime -> evidence_stale at start_lead_turns.
        meta = self._seed_launch_lead("leak", codex_bin, probe_at=now_iso_utc(time.time() - 120))
        las_id = meta["assignment_id"]

        import gc
        import weakref

        seen_errors: List[tuple] = []
        real_check = lead_turns._plan_lead_launch_check

        def capture_check(*args, **kwargs):
            try:
                return real_check(*args, **kwargs)
            except lead_turns.MeshError as exc:
                seen_errors.append((weakref.ref(exc), len(traceback.extract_tb(exc.__traceback__))))
                raise

        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st["rdir"]}), \
                mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as ev_spy:
            # Verify with plain wrapper (`new=capture_check`, no MagicMock call history retention)
            # that every hit builds an unretained fresh MeshError whose traceback depth never grows.
            with mock.patch.object(lead_turns, "_plan_lead_launch_check", new=capture_check):
                for _ in range(200):
                    self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])

        gc.collect()
        self.assertEqual(ev_spy.call_count, 1)
        self.assertEqual(len(seen_errors), 200)
        self.assertTrue(all(ref() is None for ref, _ in seen_errors), "MeshError retained alive after tick")
        self.assertLessEqual(max(depth for _, depth in seen_errors), 4, f"traceback grew: {[d for _, d in seen_errors[:5]]}")
        entry = lead_turns._VENDOR_REFUSAL_CACHE[(self.home, las_id)]
        self.assertIsInstance(entry, lead_turns._VendorRefusalEntry)
        self.assertNotIsInstance(entry, BaseException)
        self.assertEqual(entry.code, "ADAPTER_UNSUPPORTED")
        self.assertEqual(entry.details.get("reason"), "evidence_stale")
        evs = self.conn.execute(
            "SELECT payload_json FROM events WHERE type = 'lead.turn_refused' AND project_id = ?;",
            (meta["project_id"],),
        ).fetchall()
        self.assertEqual(len(evs), 1)
        self.assertEqual(json.loads(evs[0]["payload_json"])["reason"], "evidence_stale")

    def test_launch_cache_invalidates_on_fixture_results_ttl_probe_and_dedupes_reason(self) -> None:
        """r3 P3-1, P3-2, P3-6, P3-7: launch-path refusal cache hits across ticks, invalidates on
        (1) referenced fixture repair only, (2) results file rewrite, (3) 30s TTL expiry, and
        (4) probe update, while deduping `lead.turn_refused` by `(assignment, reason)`."""
        # 1. Fixture-only repair invalidation (`evidence_invalid` -> pass without touching results file).
        st1 = self._stage_fx("fx1", bad_fixture=True)
        bin1 = self._make_codex_bin("fx1")
        meta1 = self._seed_launch_lead("fx1", bin1, probe_at=now_iso_utc(time.time() + 10))
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st1["rdir"]}), \
                mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy1:
            for _ in range(4):
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
            self.assertEqual(spy1.call_count, 1)
            entry1 = lead_turns._VENDOR_REFUSAL_CACHE[(self.home, meta1["assignment_id"])]
            self.assertEqual(entry1.results_path, st1["rfile"])
            self.assertIn(st1["fx_paths"]["S-X-1"], entry1.fixture_paths)
            self.assertIn(st1["fx_paths"]["S-X-1"] + tierlib.SIDECAR_SUFFIX, entry1.fixture_paths)

            # Repair ONLY the referenced fixture file + sidecar (results file & probe untouched).
            time.sleep(0.02)
            with open(st1["fx_paths"]["S-X-1"], "wb") as f:
                f.write(st1["fx_data"])
            with open(st1["fx_paths"]["S-X-1"] + tierlib.SIDECAR_SUFFIX, "wb") as sf:
                sf.write(tierlib.sidecar_bytes(["codex"], st1["fx_data"]))
            spy1.reset_mock()
            started = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
            self.assertEqual(len(started), 1)
            self.assertEqual(spy1.call_count, 1)
        self._drain_turn(started[0])

        # Close project 1 by marking inactive before testing next independent cases on same DB.
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?;", (meta1["project_id"],))

        # 2. Results file in-place rewrite + 30s TTL expiry.
        st2 = self._stage_fx("res2", fail_case=True)
        bin2 = self._make_codex_bin("res2")
        meta2 = self._seed_launch_lead("res2", bin2, probe_at=now_iso_utc(time.time() + 10))
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st2["rdir"]}), \
                mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy2:
            # Verify TTL expiry triggers re-evaluation even when no files changed.
            now_box = {"t": 1000.0}
            with mock.patch.object(lead_turns, "_monotonic", side_effect=lambda: now_box["t"]):
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                now_box["t"] = 1010.0  # < 30s TTL: cache hit
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(spy2.call_count, 1)
                now_box["t"] = 1031.0  # >= 30s TTL: re-runs effective_launch
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(spy2.call_count, 2)

            # In-place results file rewrite immediately invalidates within TTL.
            time.sleep(0.02)
            pass_cases = [dict(st2["cases"][0], status="pass"), st2["cases"][1]]
            pass_doc = dict(st2["doc"], cases=pass_cases, consent_ref="test-updated-evidence")
            pass_bytes = json.dumps(pass_doc).encode("utf-8")
            with open(st2["rfile"], "wb") as f:
                f.write(pass_bytes)
            with open(st2["rfile"] + tierlib.SIDECAR_SUFFIX, "wb") as sf:
                sf.write(tierlib.sidecar_bytes(["test"], pass_bytes))
            spy2.reset_mock()
            started2 = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
            self.assertEqual(len(started2), 1)
            self.assertEqual(spy2.call_count, 1)
        self._drain_turn(started2[0])
        with transaction(self.conn):
            self.conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?;", (meta2["project_id"],))

        # 3. Probe row change + (assignment, reason) dedupe on same assignment.
        st3 = self._stage_fx("prb3")
        bin3 = self._make_codex_bin("prb3")
        meta3 = self._seed_launch_lead("prb3", bin3, probe_at=None)  # no_probe_version
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st3["rdir"]}), \
                mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy3:
            for _ in range(3):
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
            self.assertEqual(spy3.call_count, 1)

            # Toggle opt-in off -> SAME assignment hits new reason `vendor_lead_m3_lite`: emits 1 more event!
            with transaction(self.conn):
                self.conn.execute("UPDATE settings SET value_json = 'false' WHERE key = 'lead.vendor_leads';")
            for _ in range(3):
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
            reasons = [
                json.loads(r["payload_json"])["reason"]
                for r in self.conn.execute(
                    "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused' ORDER BY seq ASC;",
                    (meta3["project_id"],),
                ).fetchall()
            ]
            self.assertEqual(reasons, ["no_probe_version", "vendor_lead_m3_lite"])

            # Toggle opt-in back on + insert probe row -> cache invalidates immediately and starts turn.
            with transaction(self.conn):
                self.conn.execute("UPDATE settings SET value_json = 'true' WHERE key = 'lead.vendor_leads';")
                self.conn.execute(
                    "INSERT INTO adapter_probes (adapter, probed_at, binary_path, version, status, flags_json) "
                    "VALUES ('codex', ?, ?, ?, 'verified', '{}');",
                    (now_iso_utc(time.time() + 10), bin3, _CODEX_VER),
                )
            spy3.reset_mock()
            started3 = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
            self.assertEqual(len(started3), 1)
            self.assertEqual(spy3.call_count, 1)
        self._drain_turn(started3[0])

    def test_p3_3_refusal_cache_key_error_preserves_refusal_event(self) -> None:
        """r6 P3-3: an error while building the refusal-cache key must skip caching and
        still re-raise the original MeshError so `lead.turn_refused` is recorded."""
        st_e = self._stage_fx("keyerr")
        bin_e = self._make_codex_bin("keyerr")
        meta_e = self._seed_launch_lead("keyerr", bin_e, probe_at=None)  # no_probe_version
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st_e["rdir"]}), mock.patch.object(
            evidence, "launch_inputs_signature", side_effect=sqlite3.OperationalError("simulated key error")
        ), mock.patch.object(lead_turns, "_log") as log_spy:
            started = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
        self.assertEqual(started, [])
        self.assertNotIn((self.home, meta_e["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)
        evs = self.conn.execute(
            "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
            (meta_e["project_id"],),
        ).fetchall()
        self.assertEqual(len(evs), 1)
        self.assertEqual(json.loads(evs[0]["payload_json"])["reason"], "no_probe_version")
        self.assertFalse(any("simulated key error" in str(c) for c in log_spy.call_args_list))

    def test_refusal_cache_invalidates_when_evidence_mode_switches_to_warn(self) -> None:
        """Switching adapter.evidence_mode from strict to warn invalidates a cached refusal,
        launches the lead turn, and records evidence_warning on lead.turn_started; switching back
        to strict within the 30s TTL refuses a second wake on the same assignment (allow is never cached)."""
        st_w = self._stage_fx("evmode_inv")
        bin_w = self._make_codex_bin("evmode_inv")
        meta_w = self._seed_launch_lead("evmode_inv", bin_w, probe_at=None)  # no_probe_version
        now_box = {"t": 1000.0}
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st_w["rdir"]}), \
                mock.patch.object(lead_turns, "_monotonic", side_effect=lambda: now_box["t"]):
            with mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy:
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(spy.call_count, 1)
            self.assertIn((self.home, meta_w["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)

            now_box["t"] = 1002.0
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('adapter.evidence_mode', '\"warn\"', ?, 'usr_c');",
                    (now_iso_utc(),),
                )
            with mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy2:
                started = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
                self.assertEqual(len(started), 1)
                self.assertEqual(spy2.call_count, 1)
            self.assertNotIn((self.home, meta_w["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)
            started_ev = self.conn.execute(
                "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_started';",
                (meta_w["project_id"],),
            ).fetchone()
            self.assertIsNotNone(started_ev)
            self.assertEqual(json.loads(started_ev["payload_json"]).get("evidence_warning"), "no_probe_version")
            self._drain_turn(started[0])

            # Switch back to strict within the 30s TTL and deliver a second wake on the same assignment.
            now_box["t"] = 1005.0
            old_ts = now_iso_utc(time.time() - 60)
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('adapter.evidence_mode', '\"strict\"', ?, 'usr_c');",
                    (now_iso_utc(),),
                )
                ev2 = record_event(
                    self.conn, type_="project.note", actor_id="usr_c", actor_kind="user",
                    via="direct", project_id=meta_w["project_id"], payload={"text": "wake strict back"},
                )
                self.conn.execute(
                    "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                    "type, payload_json, status, attempts, next_attempt_at, created_at) "
                    "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead.wake', '{}', 'pending', 0, ?, ?);",
                    ("out_wake2_inv", ev2["seq"], meta_w["agent_id"], "dk_wake2_inv",
                     meta_w["project_id"], old_ts, old_ts),
                )
            with mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy3:
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(spy3.call_count, 1)
            self.assertIn((self.home, meta_w["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)
            wake2 = self.conn.execute("SELECT status FROM outbox WHERE id = 'out_wake2_inv';").fetchone()
            self.assertEqual(wake2["status"], "pending")
            refused = self.conn.execute(
                "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
                (meta_w["project_id"],),
            ).fetchall()
            self.assertEqual(len(refused), 1)
            self.assertEqual(json.loads(refused[0]["payload_json"]).get("reason"), "no_probe_version")

    def test_warn_mode_allow_never_cached_when_switching_back_to_strict(self) -> None:
        """P3-3(1): same assignment_id runs a warn-mode turn (0 refusals), switches adapter.evidence_mode
        back to strict within the 30s TTL, and receives a second wake -> planning refuses and records
        lead.turn_refused, proving 'allow' is never cached."""
        st = self._stage_fx("warn_to_strict")
        bin_w = self._make_codex_bin("warn_to_strict")
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                "VALUES ('adapter.evidence_mode', '\"warn\"', ?, 'usr_c');",
                (now_iso_utc(),),
            )
        meta = self._seed_launch_lead("warn_to_strict", bin_w, probe_at=None)  # no_probe_version
        now_box = {"t": 2000.0}
        with mock.patch.dict(os.environ, {evidence.OVERRIDE_ENV: st["rdir"]}), \
                mock.patch.object(lead_turns, "_monotonic", side_effect=lambda: now_box["t"]):
            with mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy_warn:
                started = lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True)
                self.assertEqual(len(started), 1)
                self.assertEqual(spy_warn.call_count, 1)
            self.assertNotIn((self.home, meta["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)
            self._drain_turn(started[0])
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
                    (meta["project_id"],),
                ).fetchone()[0],
                0,
            )

            now_box["t"] = 2010.0  # well within the 30s refusal-cache TTL
            old_ts = now_iso_utc(time.time() - 60)
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value_json, updated_at, updated_by) "
                    "VALUES ('adapter.evidence_mode', '\"strict\"', ?, 'usr_c');",
                    (now_iso_utc(),),
                )
                ev2 = record_event(
                    self.conn, type_="project.note", actor_id="usr_c", actor_kind="user",
                    via="direct", project_id=meta["project_id"], payload={"text": "second wake"},
                )
                self.conn.execute(
                    "INSERT INTO outbox (id, event_seq, recipient_id, channel, dedup_key, project_id, "
                    "type, payload_json, status, attempts, next_attempt_at, created_at) "
                    "VALUES (?, ?, ?, 'lead_wake', ?, ?, 'lead.wake', '{}', 'pending', 0, ?, ?);",
                    ("out_wake2_ws", ev2["seq"], meta["agent_id"], "dk_wake2_ws",
                     meta["project_id"], old_ts, old_ts),
                )

            with mock.patch.object(evidence, "effective_launch", wraps=evidence.effective_launch) as spy_strict:
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(lead_turns.start_lead_turns(self.conn, self.home, testing_mode=True), [])
                self.assertEqual(spy_strict.call_count, 1)

            self.assertIn((self.home, meta["assignment_id"]), lead_turns._VENDOR_REFUSAL_CACHE)
            refused = self.conn.execute(
                "SELECT payload_json FROM events WHERE project_id = ? AND type = 'lead.turn_refused';",
                (meta["project_id"],),
            ).fetchall()
            self.assertEqual(len(refused), 1)
            payload = json.loads(refused[0]["payload_json"])
            self.assertEqual(payload.get("reason"), "no_probe_version")
            self.assertEqual(payload.get("lead_assignment_id"), meta["assignment_id"])
            wake2 = self.conn.execute("SELECT status FROM outbox WHERE id = 'out_wake2_ws';").fetchone()
            self.assertEqual(wake2["status"], "pending")


if __name__ == "__main__":
    unittest.main()
