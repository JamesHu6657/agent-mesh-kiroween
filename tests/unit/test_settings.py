"""Unit tests for settings registry (§5.7) and concurrency policy (§9.2)."""

import os
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core.policy import parse_positive_int
from mesh.core.settings import get_default_settings, validate_setting
from mesh.errors import MeshError


class TestSettingsRegistry(unittest.TestCase):
    """Test plan §5.7 settings registry and §9.2 concurrency policy."""

    def test_settings_registry(self) -> None:
        """ACC: B13 - Every §5.7 key, default, type, and range; unknown key -> INVALID_SETTING."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            ws = os.path.join(home, "workspaces")
            os.makedirs(home, mode=0o700)
            os.makedirs(ws, mode=0o700)

            defaults = get_default_settings(home)
            # Verify known keys exist
            expected_keys = [
                "lead.default_agent",
                "lead.wake_debounce_s",
                "lead.profile",
                "lead.turn_timeout_s",
                "scheduler.max_total_runs",
                "workspaces.root",
                "task.default_timeout_s",
                "task.default_max_attempts",
                "task.max_attempts_cap",
                "inbox.ack_timeout_s",
                "lease.ttl_s",
                "kill.grace_s",
                "kill.post_submit_grace_s",
                "outbox.max_attempts",
                "approval.ttl_seconds",
                "notify.command",
                "approval.policy.config",
                "approval.policy.lead_change",
                "approval.policy.project_control",
                "approval.policy.scope_change",
                "approval.policy.permission_escalation",
                "approval.policy.retry_over_limit",
                "approval.policy.delete",
                "approval.policy.deliver_to_base",
                "approval.policy.spend_money",
                "approval.policy.deploy",
                "approval.policy.delete_external",
                "approval.policy.external_other",
            ]
            for ek in expected_keys:
                self.assertIn(ek, defaults, f"Missing setting key: {ek}")

            # Unknown key -> INVALID_SETTING with details.key
            with self.assertRaises(MeshError) as ctx:
                validate_setting("nosuch.key", 1, home)
            self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual(ctx.exception.details["key"], "nosuch.key")

            # Wrong type (string instead of int) -> INVALID_SETTING with details.key
            with self.assertRaises(MeshError) as ctx:
                validate_setting("task.default_timeout_s", "600", home)
            self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
            self.assertEqual(ctx.exception.details["key"], "task.default_timeout_s")

            # Wrong type (bool instead of int) -> INVALID_SETTING with details.key
            with self.assertRaises(MeshError) as ctx:
                validate_setting("task.default_timeout_s", True, home)
            self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
            self.assertEqual(ctx.exception.details["key"], "task.default_timeout_s")

            # Out of range (0 < 60) -> INVALID_SETTING with details.key
            with self.assertRaises(MeshError) as ctx:
                validate_setting("task.default_timeout_s", 0, home)
            self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
            self.assertEqual(ctx.exception.details["key"], "task.default_timeout_s")

            # Valid int setting -> returns int
            res = validate_setting("task.default_timeout_s", 600, home)
            self.assertEqual(res, 600)

            # approval.policy.config is fixed -> trying to set to direct_or_relayed fails
            with self.assertRaises(MeshError) as ctx:
                validate_setting("approval.policy.config", "direct_or_relayed", home)
            self.assertEqual(ctx.exception.code, c.INVALID_SETTING)

            # approval.policy.project_control valid values
            val_p = validate_setting("approval.policy.project_control", "direct_only", home)
            self.assertEqual(val_p, "direct_only")

    def test_policy_concurrency(self) -> None:
        """ACC: D8, D9, D10 - Concurrency policy parsing and validation per plan §9.2."""
        # D8: Invalid values must each raise INVALID_CONCURRENCY with the exact message
        invalid_values = [
            0,
            -1,
            1.5,
            "abc",
            "",
            "1e3",
            "0x10",
            "+3",
            " 2",
            9223372036854775808,
            True,
            "2.0",
            "1",
            "7",
            "9223372036854775807",
        ]

        for inv in invalid_values:
            with self.subTest(val=inv):
                with self.assertRaises(MeshError) as ctx:
                    parse_positive_int(inv, param_name="max_concurrency")
                self.assertEqual(ctx.exception.code, c.INVALID_CONCURRENCY)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
                expected_msg = f"max_concurrency must be a positive integer (1..9223372036854775807); got '{inv}'"
                self.assertEqual(ctx.exception.message, expected_msg)

        # D9: Valid values: 1, 7, 9223372036854775807 (strict JSON integers)
        valid_values = [1, 7, 9223372036854775807]
        for v in valid_values:
            with self.subTest(val=v):
                parsed = parse_positive_int(v)
                self.assertEqual(parsed, int(v))

        # D10: scheduler.max_total_runs uses INVALID_CONCURRENCY on invalid int
        with self.assertRaises(MeshError) as ctx:
            validate_setting("scheduler.max_total_runs", 0, "/tmp")
        self.assertEqual(ctx.exception.code, c.INVALID_CONCURRENCY)
        self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
        self.assertIn("scheduler.max_total_runs must be a positive integer", ctx.exception.message)

    def test_lockout_window_setting_is_testing_only(self) -> None:
        """ACC: G12 - auth.lockout_window_s is a testing-only knob; outside testing mode it is fixed at 300 s."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            os.makedirs(home, mode=0o700)

            self.assertEqual(get_default_settings(home)["auth.lockout_window_s"], 300)

            # Outside testing mode the key cannot be changed
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MESH_TESTING", None)
                with self.assertRaises(MeshError) as ctx:
                    validate_setting("auth.lockout_window_s", 10, home)
                self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
                self.assertEqual(ctx.exception.details["key"], "auth.lockout_window_s")

            # In testing mode it is settable within 1..3600
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                self.assertEqual(validate_setting("auth.lockout_window_s", 10, home), 10)
                self.assertEqual(validate_setting("auth.lockout_window_s", 1, home), 1)
                for bad in (0, 3601, True, "10"):
                    with self.subTest(val=bad):
                        with self.assertRaises(MeshError) as ctx:
                            validate_setting("auth.lockout_window_s", bad, home)
                        self.assertEqual(ctx.exception.code, c.INVALID_SETTING)


    def test_lead_wake_backoff_setting_is_testing_only(self) -> None:
        """ACC: L3 - lead.wake_backoff_s is a testing-only knob (Q9); outside testing
        mode the plan §12.4 backoff of 30/120/600 s is fixed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            home = os.path.join(tmpdir, "home")
            os.makedirs(home, mode=0o700)

            self.assertEqual(get_default_settings(home)["lead.wake_backoff_s"], [30, 120, 600])

            # Outside testing mode the key cannot be changed
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MESH_TESTING", None)
                with self.assertRaises(MeshError) as ctx:
                    validate_setting("lead.wake_backoff_s", [1, 1, 1], home)
                self.assertEqual(ctx.exception.code, c.INVALID_SETTING)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)
                self.assertEqual(ctx.exception.details["key"], "lead.wake_backoff_s")

            # In testing mode it is settable: exactly 3 integers, each 0..600
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                self.assertEqual(validate_setting("lead.wake_backoff_s", [1, 1, 1], home), [1, 1, 1])
                self.assertEqual(validate_setting("lead.wake_backoff_s", [0, 600, 30], home), [0, 600, 30])
                for bad in ([1, 1], [1, 1, 1, 1], [1, 1, 601], [1, 1, -1], [1, 1, True],
                            [1, 1, "1"], "1,1,1", 5, None):
                    with self.subTest(val=bad):
                        with self.assertRaises(MeshError) as ctx:
                            validate_setting("lead.wake_backoff_s", bad, home)
                        self.assertEqual(ctx.exception.code, c.INVALID_SETTING)

    def test_lead_wake_backoff_read_path_is_testing_only(self) -> None:
        """ACC: L3 - the read path returns the production 30/120/600 s outside testing
        mode even when the DB holds a testing value (review m2-r9 finding 6)."""
        import sqlite3

        from mesh.daemon.lead_turns import lead_wake_backoff_s

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT);")
            conn.execute("INSERT INTO settings (key, value_json) VALUES (?, ?);",
                         ("lead.wake_backoff_s", "[0, 0, 0]"))
            # Outside testing mode the stored value is never read.
            self.assertEqual(lead_wake_backoff_s(conn, testing_mode=False), [30, 120, 600])
            # In testing mode the stored value applies.
            self.assertEqual(lead_wake_backoff_s(conn, testing_mode=True), [0, 0, 0])
            # A malformed stored value falls back to the production constant.
            conn.execute("UPDATE settings SET value_json = ? WHERE key = ?;",
                         ("[1, 2]", "lead.wake_backoff_s"))
            self.assertEqual(lead_wake_backoff_s(conn, testing_mode=True), [30, 120, 600])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
