"""ACC: N25 - the opt-in droid/kiro e2e (tests/smoke/test_e2e.py) skips, never ERRORs, when its gates are open but required config is missing; present-but-invalid config still raises. Offline: no process is started."""
import os
import sys
import unittest
from unittest import mock

from tests.smoke import harness
from tests.smoke import test_e2e

OPEN = {"MESH_SMOKE": "1", "MESH_SMOKE_ADAPTERS": "droid", "MESH_SMOKE_CONSENT": "droid", "MESH_SMOKE_E2E": "droid"}
FULL = dict(OPEN, MESH_SMOKE_MAX_RUNS="1", MESH_SMOKE_MODEL_DROID="claude-sonnet-5-5", MESH_SMOKE_OUT="/tmp/mh-e2e-gate-test")


def set_up(env):
    case = test_e2e.DroidE2E("test_n13_readonly")
    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(test_e2e.shutil, "which", return_value=sys.executable):
        case.setUp()
    return case


class TestE2EGate(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, test_e2e, "_RUNS_USED", test_e2e._RUNS_USED)

    def test_each_missing_required_var_skips(self):
        """ACC: N25 - gates open, one required variable missing -> SkipTest naming it."""
        for var in test_e2e.REQUIRED_VARS:
            with self.subTest(var=var):
                env = {k: v for k, v in FULL.items() if k != var}
                with self.assertRaises(unittest.SkipTest) as cm:
                    set_up(env)
                self.assertIn(var, str(cm.exception))

    def test_all_config_missing_skips(self):
        """ACC: N25 - gates open, no config at all -> SkipTest."""
        with self.assertRaises(unittest.SkipTest):
            set_up(OPEN)

    def test_present_but_invalid_value_still_raises(self):
        """ACC: N25 - a present but invalid value is a config error, not a skip."""
        for var, bad in (("MESH_SMOKE_MAX_RUNS", "abc"), ("MESH_SMOKE_MODEL_DROID", "auto"), ("MESH_SMOKE_OUT", "/var/tmp/x")):
            with self.subTest(var=var):
                with self.assertRaises(harness.SmokeConfigError):
                    set_up(dict(FULL, **{var: bad}))

    def test_closed_gate_still_skips_first(self):
        """ACC: N25 - with MESH_SMOKE absent the gate skip comes before any config check."""
        with self.assertRaises(unittest.SkipTest) as cm:
            set_up({})
        self.assertIn("e2e tier closed", str(cm.exception))


KOPEN = {"MESH_SMOKE": "1", "MESH_SMOKE_ADAPTERS": "kiro", "MESH_SMOKE_CONSENT": "kiro", "MESH_SMOKE_E2E": "kiro"}
KFULL = dict(KOPEN, MESH_SMOKE_MAX_RUNS="1", MESH_SMOKE_MODEL_KIRO="claude-haiku-5", MESH_SMOKE_OUT="/tmp/mh-e2e-gate-test")


def set_up_kiro(env, method="test_n13_readonly"):
    case = test_e2e.KiroE2E(method)
    with mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(test_e2e.shutil, "which", return_value=sys.executable):
        case.setUp()
    return case


class TestKiroE2EGate(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, test_e2e, "_RUNS_USED", test_e2e._RUNS_USED)

    def test_kiro_missing_required_var_skips(self):
        """ACC: N25 - kiro gates open, one of MESH_SMOKE_MAX_RUNS / MESH_SMOKE_MODEL_KIRO / MESH_SMOKE_OUT missing -> SkipTest naming it."""
        self.assertEqual(test_e2e.required_vars("kiro"),
                         ("MESH_SMOKE_MAX_RUNS", "MESH_SMOKE_MODEL_KIRO", "MESH_SMOKE_OUT"))
        for var in test_e2e.required_vars("kiro"):
            with self.subTest(var=var):
                env = {k: v for k, v in KFULL.items() if k != var}
                with self.assertRaises(unittest.SkipTest) as cm:
                    set_up_kiro(env)
                self.assertIn(var, str(cm.exception))

    def test_kiro_invalid_model_raises(self):
        """ACC: N25 - MESH_SMOKE_MODEL_KIRO=auto is a config error, not a skip."""
        with self.assertRaises(harness.SmokeConfigError):
            set_up_kiro(dict(KFULL, MESH_SMOKE_MODEL_KIRO="auto"))

    def test_kiro_code_demo_skips(self):
        """ACC: N25, N26 - kiro has no dev profile (plan §14.2): the code demo skips even when selected, before any config check or run reservation."""
        before = test_e2e._RUNS_USED
        with self.assertRaises(unittest.SkipTest) as cm:
            set_up_kiro(dict(KFULL, MESH_SMOKE_E2E_CODE="1"), "test_n26_code_task")
        self.assertIn("no dev profile", str(cm.exception))
        self.assertEqual(test_e2e._RUNS_USED, before)

    def test_e2e_selects_one_adapter(self):
        """ACC: N25 - MESH_SMOKE_E2E=kiro skips DroidE2E and vice versa; the base class always skips."""
        both = {"MESH_SMOKE": "1", "MESH_SMOKE_ADAPTERS": "droid,kiro", "MESH_SMOKE_CONSENT": "droid,kiro"}
        with self.assertRaises(unittest.SkipTest) as cm:
            set_up(dict(both, MESH_SMOKE_E2E="kiro"))
        self.assertIn("MESH_SMOKE_E2E!=droid", str(cm.exception))
        with self.assertRaises(unittest.SkipTest) as cm:
            set_up_kiro(dict(both, MESH_SMOKE_E2E="droid"))
        self.assertIn("MESH_SMOKE_E2E!=kiro", str(cm.exception))
        with self.assertRaises(unittest.SkipTest):
            test_e2e._VendorE2E("test_n13_readonly").setUp()

    def test_kiro_full_config_reserves_one_run(self):
        """ACC: N25 - kiro gates and config complete: setUp reserves the single run (MESH_SMOKE_MAX_RUNS=1); a second setUp skips run_cap. No process starts."""
        test_e2e._RUNS_USED = 0
        case = set_up_kiro(KFULL)
        self.assertEqual(case.config.model, "claude-haiku-5")
        self.assertEqual(test_e2e._RUNS_USED, 1)
        with self.assertRaises(unittest.SkipTest) as cm:
            set_up_kiro(KFULL)
        self.assertIn("run_cap", str(cm.exception))


class TestKiroSmokeEvidence(unittest.TestCase):
    def test_kiro_smoke_evidence_opens_launch(self):
        """ACC: N19, N12 - the committed kiro 3.1.0 smoke (James consent 2026-11-10 09:46/09:49, claude-haiku-5.5) has S-X-1 and S-X-6 pass, so the evidence gate opens kiro launch for 3.1.0 only."""
        from mesh.adapters import evidence
        ev = evidence.load_evidence("kiro", False, "3.1.0", {})
        self.assertTrue(ev.ok, ev.reason)
        self.assertTrue(ev.results_path.endswith("kiro-3.1.0-2026-11-10-2.json"))
        self.assertEqual(ev.cases["S-X-1"]["status"], "pass")
        self.assertEqual(ev.cases["S-X-6"]["status"], "pass")
        self.assertFalse(evidence.load_evidence("kiro", False, "3.2.0", {}).ok)

if __name__ == "__main__":
    unittest.main()
