"""Integration regression tests for batch7 PR44 round 1 findings."""

import concurrent.futures
import os
import shutil
import tempfile
import time
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness, serial
from tests.integration.test_inbox import create_agent
from tests.stubs import create_test_stubs


class TestPr44R1Integration(unittest.TestCase):
    """End-to-end daemon checks for wait capability revocation and set_lead redaction."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-pr44-int-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_relayer = create_agent(
            self.harness,
            "relayer",
            adapter="none",
            delivery="inbox",
            can_lead=False,
            capabilities=["intake", "relay"],
        )
        self.tok_lead1 = create_agent(
            self.harness,
            "lead1",
            adapter="fake",
            delivery="launch",
            can_lead=True,
            allowed_models=["fake-model"],
            default_model="fake-model",
        )
        self.tok_lead2 = create_agent(
            self.harness,
            "lead2",
            adapter="fake",
            delivery="launch",
            can_lead=True,
            allowed_models=["fake-model"],
            default_model="fake-model",
        )

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    @serial
    def test_live_inbox_wait_stops_promptly_when_relay_capability_revoked(self) -> None:
        """P2-2: agent wait with --relayed terminates immediately once relay capability is revoked."""
        start = time.monotonic()

        def run_wait():
            return self.harness.cli(
                "inbox", "wait", "--timeout", "8s", "--relayed", "--user-statement", "poll for user",
                token=self.tok_relayer, timeout=12.0,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(run_wait)
            time.sleep(0.35)
            rc_upd, resp_upd = self.harness.cli(
                "agent", "update", "relayer", "--capability", "intake", user=True,
            )
            self.assertEqual(rc_upd, 0, f"agent update failed: {resp_upd}")
            rc_wait, resp_wait = fut.result(timeout=10.0)

        elapsed = time.monotonic() - start
        self.assertEqual(rc_wait, c.EXIT_FORBIDDEN, f"expected EXIT_FORBIDDEN, got {rc_wait}: {resp_wait}")
        self.assertEqual(resp_wait.get("error", {}).get("code"), c.RELAY_NOT_ALLOWED)
        self.assertLess(elapsed, 5.0)

    @serial
    def test_live_project_set_lead_redacts_stored_and_listed_reasons(self) -> None:
        """P2-3: project set-lead redacts reason/ended_reason across lead_assignments & reads."""
        rc, p_resp = self.harness.cli(
            "project", "create", "--title", "P", "--goal", "G", "--criterion", "done", "--lead", "lead1", user=True,
        )
        self.assertEqual(rc, 0, p_resp)
        pid = p_resp["data"]["id"]

        rc2, sl_resp = self.harness.cli(
            "project", "set-lead", pid, "lead2",
            "--reason", "handoff Bearer pr44_live_secret_tok_9876 password=hunter_live",
            user=True,
        )
        self.assertEqual(rc2, 0, sl_resp)

        rc3, leads_resp = self.harness.cli("project", "leads", pid, user=True)
        self.assertEqual(rc3, 0, leads_resp)
        items = leads_resp["data"]["items"]
        self.assertEqual(len(items), 2)
        for item in items:
            for k in ("reason", "ended_reason"):
                val = item.get(k) or ""
                self.assertNotIn("pr44_live_secret_tok_9876", val)
                self.assertNotIn("hunter_live", val)
        self.assertIn("[REDACTED]", items[0]["ended_reason"])
        self.assertIn("[REDACTED]", items[1]["reason"])


if __name__ == "__main__":
    unittest.main()
