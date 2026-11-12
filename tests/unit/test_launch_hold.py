"""L11 kiro launch hold (James sign-off 2026-11-12 19:32; ACC: N19, N20)."""

import unittest
from unittest import mock

from mesh.adapters import evidence
from mesh.adapters.registry import get_adapter


def _ok():
    return evidence.Evidence(ok=True, results_path=None, probed_version="3.1.0", results_version="3.1.0")


class TestLaunchHoldTable(unittest.TestCase):
    def test_table_empty_since_m4(self):
        """ACC: N19, N13 - M4: kiro N13 passed, so no adapter is held in production; launch_held stays a known reason."""
        self.assertEqual(evidence.LAUNCH_HOLDS, {})
        self.assertIn("launch_held", evidence.REASONS)
        self.assertEqual(evidence.effective_holds(False, {evidence.HOLD_ENV: "kiro:M4"}), {})

    def test_committed_kiro_evidence_launches_again(self):
        """ACC: N19, N13 - outside testing mode the committed kiro 3.1.0 evidence opens kiro launch again."""
        adapter = get_adapter("kiro", testing_mode=False)
        with mock.patch.object(evidence, "latest_probe", return_value=("3.1.0", None)):
            ev = evidence.effective_launch(None, adapter, False, "/x/kiro-cli", {})
        self.assertTrue(ev.ok, ev.reason)
        self.assertIsNone(ev.milestone)

    def test_hold_env_testing_only(self):
        """ACC: N19 - MESH_TEST_LAUNCH_HOLD adds holds in testing mode only; default milestone M4; unhold wins."""
        env = {evidence.HOLD_ENV: " kiro:M4 , agy , vibe:M5 "}
        self.assertEqual(evidence.effective_holds(True, env), {"kiro": "M4", "agy": "M4", "vibe": "M5"})
        self.assertEqual(evidence.effective_holds(False, env), {})
        self.assertIsNone(evidence.held_milestone("kiro", False, env))
        self.assertEqual(evidence.held_milestone("vibe", True, env), "M5")
        env[evidence.UNHOLD_ENV] = "kiro"
        self.assertIsNone(evidence.held_milestone("kiro", True, env))


class TestLaunchHold(unittest.TestCase):
    """The hold tool with kiro held (as during M3), via a patched table."""

    def setUp(self) -> None:
        holds = mock.patch.dict(evidence.LAUNCH_HOLDS, {"kiro": "M4"})
        holds.start()
        self.addCleanup(holds.stop)

    def _eff(self, adapter_id, testing, env):
        adapter = get_adapter(adapter_id, testing_mode=testing)
        with mock.patch.object(evidence, "latest_probe", return_value=("3.1.0", None)), \
                mock.patch.object(evidence, "load_evidence", return_value=_ok()):
            return evidence.effective_launch(None, adapter, testing, "/x/kiro-cli", env)

    def test_kiro_held_outside_testing_even_with_unhold_env(self):
        """ACC: N19 - passing kiro evidence -> launch off, launch_held, M4; the unhold env is ignored outside testing mode."""
        for env in ({}, {evidence.UNHOLD_ENV: "kiro"}):
            ev = self._eff("kiro", False, env)
            self.assertFalse(ev.ok)
            self.assertFalse(bool(ev))
            self.assertEqual(ev.reason, "launch_held")
            self.assertEqual(ev.milestone, "M4")

    def test_kiro_held_in_testing_without_unhold(self):
        """ACC: N19 - testing mode without the unhold env still holds kiro."""
        ev = self._eff("kiro", True, {evidence.UNHOLD_ENV: "droid, agy"})
        self.assertEqual(ev.reason, "launch_held")

    def test_testing_unhold_lifts(self):
        """ACC: N19 - testing mode with MESH_TEST_LAUNCH_UNHOLD=kiro lifts the hold."""
        ev = self._eff("kiro", True, {evidence.UNHOLD_ENV: " droid , kiro "})
        self.assertTrue(ev.ok)
        self.assertIsNone(ev.milestone)

    def test_droid_not_held(self):
        """ACC: N19 - droid with passing evidence stays launchable."""
        self.assertTrue(self._eff("droid", False, {}).ok)

    def test_failing_evidence_keeps_its_reason(self):
        """ACC: N19 - a hold never masks a failing evidence reason."""
        bad = evidence.Evidence(ok=False, reason="evidence_stale")
        ev = evidence.apply_hold(bad, "kiro", False, {})
        self.assertEqual(ev.reason, "evidence_stale")
        self.assertIsNone(ev.milestone)

    def test_probe_fields(self):
        """ACC: N20 - apply_to_probe reports launch_enabled false, milestone M4, reason launch_held for held kiro."""
        adapter = get_adapter("kiro", testing_mode=False)
        ev = evidence.apply_hold(_ok(), "kiro", False, {})
        data = evidence.apply_to_probe({"status": "partially_verified", "capabilities": {}}, adapter, ev)
        self.assertFalse(data["launch_enabled"])
        self.assertEqual(data["launch_milestone"], "M4")
        self.assertEqual(data["launch_reason"], "launch_held")

    def test_committed_kiro_evidence_is_held(self):
        """ACC: N19 - the real committed kiro 3.1.0 evidence passes, and the hold still turns launch off."""
        ev = evidence.load_evidence("kiro", False, "3.1.0", {})
        self.assertTrue(ev.ok, ev.reason)
        held = evidence.apply_hold(ev, "kiro", False, {})
        self.assertEqual((held.ok, held.reason, held.milestone), (False, "launch_held", "M4"))


if __name__ == "__main__":
    unittest.main()
