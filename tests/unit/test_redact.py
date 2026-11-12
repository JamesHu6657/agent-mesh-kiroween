"""Unit tests for mesh/core/redact.py per plan §15.2.

ACC: C7 (partial: redaction unit layer; the end-to-end log/grep test lands in the
C7 integration test)
"""
import json

import os
import unittest

from mesh.core import redact as redact_mod
from mesh.core.redact import (
    collect_exact_values,
    redact,
    redact_value,
    register_secret,
    unregister_secret,
)

PATTERN_SAMPLES = {
    "mesh_token": "mesh_at_" + "A" * 43,
    "fk": "fk-" + "B" * 20,
    "sk": "sk-" + "c" * 20,
    "google": "AIza" + "d" * 35,
    "github": "ghp_" + "e" * 30,
    "slack": "xoxb-" + "f" * 12,
    "bearer": "Bearer " + "g" * 20,
    "private_key": (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA\n"
        "-----END RSA PRIVATE KEY-----"
    ),
    "assignment": "password: hunter2",
}


class TestRedactPatterns(unittest.TestCase):
    def test_each_pattern_is_redacted(self) -> None:
        for label, sample in PATTERN_SAMPLES.items():
            with self.subTest(pattern=label):
                out = redact(f"before {sample} after", exact_values=[])
                self.assertIn("[REDACTED]", out)
                self.assertNotIn(sample, out)

    def test_plain_text_unchanged(self) -> None:
        text = "Task completed successfully. attempt 2/3."
        self.assertEqual(redact(text, exact_values=[]), text)


class TestRedactExactValues(unittest.TestCase):
    def test_exact_value_replaced_with_name(self) -> None:
        pairs = [("MESH_TOKEN", "super-secret-value-123")]
        out = redact("token=super-secret-value-123 done", exact_values=pairs)
        self.assertIn("[REDACTED:MESH_TOKEN]", out)
        self.assertNotIn("super-secret-value-123", out)

    def test_longest_value_first_no_partial_leak(self) -> None:
        long_secret = "abcdefghijklmnop"
        short_secret = "abcdefghij"
        pairs = [("short", short_secret), ("long", long_secret)]
        out = redact(f"x{long_secret}y", exact_values=pairs)
        self.assertIn("[REDACTED:long]", out)
        self.assertNotIn(short_secret, out)
        self.assertNotIn(long_secret, out)

    def test_collect_exact_values_env_rules(self) -> None:
        env = {
            "FAKE_SECRET_API_KEY": "sk-acc0123456789abcdef",
            "MESH_HOME": "/tmp/mh-abc",  # name does not match the secret regex
            "SOME_TOKEN": "short",  # shorter than 8 chars
            "GITHUB_TOKEN": "gh-token-value-1234",
        }
        pairs = collect_exact_values(env=env)
        names = [name for name, _ in pairs]
        self.assertIn("FAKE_SECRET_API_KEY", names)
        self.assertIn("GITHUB_TOKEN", names)
        self.assertNotIn("MESH_HOME", names)
        self.assertNotIn("SOME_TOKEN", names)
        # Longest value first
        lengths = [len(value) for _, value in pairs]
        self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_registered_secret_used_by_default(self) -> None:
        register_secret("RUN_TOKEN", "mesh_rt_" + "z" * 30)
        try:
            out = redact("value mesh_rt_" + "z" * 30)
        finally:
            unregister_secret("RUN_TOKEN")
        self.assertIn("[REDACTED:RUN_TOKEN]", out)
        self.assertNotIn("mesh_rt_" + "z" * 30, out)

    def test_redact_value_recurses(self) -> None:
        secret = "sk-acc0123456789abcdef"
        pairs = [("FAKE_SECRET_API_KEY", secret)]
        value = {"summary": f"key {secret}", "items": [secret, 3], "nested": {"s": secret}}
        out = redact_value(value, exact_values=pairs)
        self.assertEqual(out["summary"], "key [REDACTED:FAKE_SECRET_API_KEY]")
        self.assertEqual(out["items"][0], "[REDACTED:FAKE_SECRET_API_KEY]")
        self.assertEqual(out["items"][1], 3)
        self.assertEqual(out["nested"]["s"], "[REDACTED:FAKE_SECRET_API_KEY]")

    def test_env_default_excludes_short_values(self) -> None:
        os.environ["MESH_TEST_SHORT_TOKEN"] = "tiny"
        try:
            redact_mod._env_cache = None  # recompute from the current env
            out = redact("tiny")
        finally:
            del os.environ["MESH_TEST_SHORT_TOKEN"]
            redact_mod._env_cache = None
        self.assertEqual(out, "tiny")


    def test_error_envelope_and_event_reason_redacted(self) -> None:
        """§15.2: error messages and event reason/payload are redacted where they are produced."""
        import tempfile

        from mesh.core.db import init_db
        from mesh.core.events import record_event
        from mesh.errors import MeshError

        secret = "sk-" + "A" * 24
        env = MeshError("INVALID_ARGUMENT", message=f"bad key {secret}", details={"k": secret}).to_envelope("r")
        self.assertNotIn(secret, json.dumps(env))
        self.assertIn("[REDACTED]", env["error"]["message"])

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = init_db(os.path.join(tmp.name, "mesh.db"))
        row = record_event(conn, type_="test.event", actor_id="system", actor_kind="system", via="system",
                           reason=f"reason {secret}", payload={"nested": [secret]})
        conn.close()
        self.assertNotIn(secret, row["reason"])
        self.assertNotIn(secret, row["payload_json"])


if __name__ == "__main__":
    unittest.main()
