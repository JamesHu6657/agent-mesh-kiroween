"""Integration tests for agents, models, concurrency, and tokens.

ACC: D1, D2, D5, D8, D9, D10, D17, B2, B9, B10, C9, N2, N3
"""

import json
import os
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness


class TestAgentsIntegration(unittest.TestCase):
    """Integration test suite for agent and token lifecycle."""

    def setUp(self) -> None:
        self.harness = MeshHarness(init_db=True, start_daemon=True)

    def tearDown(self) -> None:
        self.harness.teardown()

    def test_agent_create_and_show(self) -> None:
        """ACC: D1 - Agent create with allowed-models and default-model, stored as given."""
        rc, resp = self.harness.cli(
            "agent", "create",
            "--name", "a1",
            "--adapter", "fake",
            "--allowed-model", "m-a",
            "--allowed-model", "m-b",
            "--default-model", "m-a",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["name"], "a1")
        self.assertEqual(resp["data"]["default_model"], "m-a")
        self.assertEqual(resp["data"]["allowed_models"], ["m-a", "m-b"])
        self.assertEqual(resp["data"]["max_concurrency"], 1)

        # Inspect agent show a1
        rc_show, show_resp = self.harness.cli("agent", "show", "a1", user=True)
        self.assertEqual(rc_show, 0)
        self.assertTrue(show_resp["ok"])
        self.assertEqual(show_resp["data"]["name"], "a1")
        self.assertEqual(show_resp["data"]["default_model"], "m-a")
        self.assertEqual(show_resp["data"]["allowed_models"], ["m-a", "m-b"])

    def test_agent_create_invalid_models(self) -> None:
        """ACC: D2 - Default model not in allowed list, and launch agent with no allowed models."""
        # 1. Default model m-z not in allowed list [m-a, m-b]
        rc_bad_def, resp_bad_def = self.harness.cli(
            "agent", "create",
            "--name", "a_bad_def",
            "--adapter", "fake",
            "--allowed-model", "m-a",
            "--allowed-model", "m-b",
            "--default-model", "m-z",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_bad_def, c.EXIT_USAGE)
        self.assertFalse(resp_bad_def["ok"])
        self.assertEqual(resp_bad_def["error"]["code"], c.DEFAULT_MODEL_NOT_ALLOWED)

        # 2. Launch agent with no allowed models
        rc_no_models, resp_no_models = self.harness.cli(
            "agent", "create",
            "--name", "a_no_models",
            "--adapter", "fake",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_no_models, c.EXIT_USAGE)
        self.assertFalse(resp_no_models["ok"])
        self.assertEqual(resp_no_models["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_no_models["error"]["details"]["param"], "allowed_models")

        # Verify no agent rows were created
        conn = self.harness.db()
        cur = conn.execute("SELECT name FROM principals WHERE name IN ('a_bad_def', 'a_no_models');")
        self.assertEqual(cur.fetchall(), [])
        conn.close()

    def test_agent_update_permissions(self) -> None:
        """ACC: D5 - Only user may update models; agent tokens receive CAPABILITY_MISSING."""
        # Create agent fx
        rc_fx, _ = self.harness.cli(
            "agent", "create",
            "--name", "fx",
            "--adapter", "fake",
            "--allowed-model", "m-a",
            "--allowed-model", "m-b",
            "--default-model", "m-a",
            "--max-concurrency", "2",
            user=True,
        )
        self.assertEqual(rc_fx, 0)

        # Create lead1 agent and grok agent
        self.harness.cli(
            "agent", "create",
            "--name", "lead1",
            "--adapter", "none",
            "--delivery", "inbox",
            "--can-lead",
            "--max-concurrency", "1",
            user=True,
        )
        self.harness.cli(
            "agent", "create",
            "--name", "grok",
            "--adapter", "none",
            "--delivery", "inbox",
            "--capability", "intake",
            "--capability", "relay",
            "--max-concurrency", "1",
            user=True,
        )

        # Issue tokens for lead1 and grok
        _, tok_lead1 = self.harness.cli("agent", "token", "issue", "lead1", user=True)
        token_lead1 = tok_lead1["data"]["token"]
        _, tok_grok = self.harness.cli("agent", "token", "issue", "grok", user=True)
        token_grok = tok_grok["data"]["token"]

        # Attempt agent update fx with lead1 token
        rc_lead1, resp_lead1 = self.harness.cli(
            "agent", "update", "fx",
            "--allowed-model", "m-z",
            token=token_lead1,
        )
        self.assertEqual(rc_lead1, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_lead1["error"]["code"], c.CAPABILITY_MISSING)

        # Attempt agent update fx with grok token
        rc_grok, resp_grok = self.harness.cli(
            "agent", "update", "fx",
            "--allowed-model", "m-z",
            token=token_grok,
        )
        self.assertEqual(rc_grok, c.EXIT_FORBIDDEN)
        self.assertEqual(resp_grok["error"]["code"], c.CAPABILITY_MISSING)

    def test_agent_concurrency_validation(self) -> None:
        """ACC: D8, D9, D10 - Concurrency validation: rejected values, valid values, and update with 0."""
        # ACC: D8 - Rejected values: 0, -1, 1.5, abc, '', 1e3, 0x10, +3, ' 2', 9223372036854775808
        bad_values = ["0", "-1", "1.5", "abc", "", "1e3", "0x10", "+3", " 2", "9223372036854775808"]
        for v in bad_values:
            rc, resp = self.harness.cli(
                "agent", "create",
                "--name", f"c_{v}",
                "--adapter", "fake",
                "--allowed-model", "m-a",
                "--default-model", "m-a",
                "--max-concurrency", v,
                user=True,
            )
            self.assertEqual(rc, c.EXIT_USAGE, f"Value '{v}' should be rejected with EXIT_USAGE")
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], c.INVALID_CONCURRENCY)
            self.assertEqual(
                resp["error"]["message"],
                f"max_concurrency must be a positive integer (1..9223372036854775807); got '{v}'",
            )
            # No agent row (and no principal) was created for the rejected value.
            db = self.harness.db()
            try:
                n = db.execute("SELECT COUNT(*) FROM principals WHERE name = ?;", (f"c_{v}",)).fetchone()[0]
                n_agents = db.execute(
                    "SELECT COUNT(*) FROM agents a JOIN principals p ON p.id = a.id WHERE p.name = ?;", (f"c_{v}",)
                ).fetchone()[0]
            finally:
                db.close()
            self.assertEqual(n, 0, f"principal row created for rejected value {v!r}")
            self.assertEqual(n_agents, 0, f"agent row created for rejected value {v!r}")

        # ACC: D9 - Valid values: 1, 7, 9223372036854775807
        good_values = [1, 7, 9223372036854775807]
        for v in good_values:
            name = f"c_ok_{v}"
            rc, resp = self.harness.cli(
                "agent", "create",
                "--name", name,
                "--adapter", "fake",
                "--allowed-model", "m-a",
                "--default-model", "m-a",
                "--max-concurrency", str(v),
                user=True,
            )
            self.assertEqual(rc, 0)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["data"]["max_concurrency"], v)

        # ACC: D10 - agent update fx --max-concurrency 0 [U] -> rc 2 INVALID_CONCURRENCY; previous unchanged
        rc_up0, resp_up0 = self.harness.cli("agent", "update", "c_ok_7", "--max-concurrency", "0", user=True)
        self.assertEqual(rc_up0, c.EXIT_USAGE)
        self.assertEqual(resp_up0["error"]["code"], c.INVALID_CONCURRENCY)

        # Verify value unchanged
        _, show = self.harness.cli("agent", "show", "c_ok_7", user=True)
        self.assertEqual(show["data"]["max_concurrency"], 7)
        self.assertEqual(
            resp_up0["error"]["message"],
            "max_concurrency must be a positive integer (1..9223372036854775807); got '0'",
        )

        # ACC: D10 - exactly as the checklist: agent fx, then scheduler.max_total_runs 0 [U].
        rc_fx, resp_fx = self.harness.cli(
            "agent", "create", "--name", "fx", "--adapter", "fake", "--allowed-model", "m-a",
            "--default-model", "m-a", "--max-concurrency", "2", user=True,
        )
        self.assertEqual(rc_fx, 0, resp_fx)
        rc_fx0, resp_fx0 = self.harness.cli("agent", "update", "fx", "--max-concurrency", "0", user=True)
        self.assertEqual(rc_fx0, c.EXIT_USAGE)
        self.assertEqual(resp_fx0["error"]["code"], c.INVALID_CONCURRENCY)
        _, show_fx = self.harness.cli("agent", "show", "fx", user=True)
        self.assertEqual(show_fx["data"]["max_concurrency"], 2)

        for prev in (None, 4):
            if prev is not None:
                rc_set, resp_set = self.harness.cli("config", "set", "scheduler.max_total_runs", str(prev), user=True)
                self.assertEqual(rc_set, 0, resp_set)
            rc_g0, resp_g0 = self.harness.cli("config", "set", "scheduler.max_total_runs", "0", user=True)
            self.assertEqual(rc_g0, c.EXIT_USAGE, resp_g0)
            self.assertEqual(resp_g0["error"]["code"], c.INVALID_CONCURRENCY)
            rc_get, resp_get = self.harness.cli("config", "get", "scheduler.max_total_runs", user=True)
            self.assertEqual(rc_get, 0, resp_get)
            self.assertEqual(resp_get["data"]["value"], prev, "scheduler.max_total_runs must be unchanged")

    def test_agent_concurrency_json_validation(self) -> None:
        """F2 regression: strict JSON integer concurrency validation on --input path, pure --input, and no rows created on failure."""
        bad_json_vals = [True, "2", 2.0, 0, None, [1], -5]
        for i, v in enumerate(bad_json_vals):
            # Pure --input -
            body = json.dumps({
                "name": f"j_bad_{i}",
                "adapter": "fake",
                "allowed_models": ["m-a"],
                "default_model": "m-a",
                "max_concurrency": v,
            })
            rc, resp = self.harness.cli("agent", "create", "--input", "-", stdin=body, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, f"Pure --input with concurrency {v!r} should be rc 2")
            self.assertEqual(resp["error"]["code"], c.INVALID_CONCURRENCY)

            # Flags + --input -
            body_flag = json.dumps({"max_concurrency": v})
            rc2, resp2 = self.harness.cli(
                "agent", "create",
                "--name", f"k_bad_{i}",
                "--adapter", "fake",
                "--allowed-model", "m-a",
                "--default-model", "m-a",
                "--input", "-",
                stdin=body_flag,
                user=True,
            )
            self.assertEqual(rc2, c.EXIT_USAGE, f"Flags + --input with concurrency {v!r} should be rc 2")
            self.assertEqual(resp2["error"]["code"], c.INVALID_CONCURRENCY)
            for r in (resp, resp2):
                self.assertTrue(
                    r["error"]["message"].startswith(
                        "max_concurrency must be a positive integer (1..9223372036854775807); got '"
                    ),
                    r["error"]["message"],
                )

        # ACC: D8 - the checklist's JSON cases: message ends with "; got '<v>'". <v> is rendered the way
        # policy.parse_positive_int renders the decoded value (pinned by tests/unit/test_settings D8:
        # JSON true -> 'True', JSON "2" -> '2'); see the report for the true/True reading of the row.
        for i, (v, shown) in enumerate([(True, "True"), ("2", "2"), (2.0, "2.0"), (0, "0")]):
            body = json.dumps({
                "name": f"jd8_{i}",
                "adapter": "fake",
                "allowed_models": ["m-a"],
                "default_model": "m-a",
                "max_concurrency": v,
            })
            rc, resp = self.harness.cli("agent", "create", "--input", "-", stdin=body, user=True)
            self.assertEqual(rc, c.EXIT_USAGE, resp)
            self.assertEqual(resp["error"]["code"], c.INVALID_CONCURRENCY)
            self.assertEqual(
                resp["error"]["message"],
                f"max_concurrency must be a positive integer (1..9223372036854775807); got '{shown}'",
            )

        # Verify no rows created in DB
        db = self.harness.db()
        names = [r[0] for r in db.execute("SELECT p.name FROM agents a JOIN principals p ON p.id=a.id").fetchall()]
        db.close()
        self.assertFalse(any(n.startswith(("j_bad_", "k_bad_", "jd8_")) for n in names))

        # Valid JSON integers via pure --input -
        good_json_vals = [1, 7, 9223372036854775807]
        for v in good_json_vals:
            name = f"j_ok_{v}"
            body = json.dumps({
                "name": name,
                "adapter": "fake",
                "allowed_models": ["m-a"],
                "default_model": "m-a",
                "max_concurrency": v,
            })
            rc, resp = self.harness.cli("agent", "create", "--input", "-", stdin=body, user=True)
            self.assertEqual(rc, 0, f"Pure --input with valid concurrency {v} failed: {resp}")
            self.assertEqual(resp["data"]["max_concurrency"], v)
            rc_show, show = self.harness.cli("agent", "show", name, user=True)
            self.assertEqual(rc_show, 0)
            self.assertEqual(show["data"]["max_concurrency"], v)

        # Restrict --input keys to documented params (unknown keys like token_file rejected)
        rc_inj, resp_inj = self.harness.cli(
            "whoami",
            "--input", "-",
            stdin=json.dumps({"token_file": "/tmp/dummy"}),
        )
        self.assertEqual(rc_inj, c.EXIT_USAGE)
        self.assertEqual(resp_inj["error"]["code"], c.UNKNOWN_PARAM)

    def test_agent_create_agy_model_effort_conflict(self) -> None:
        """ACC: D17 - agy agent create with model encoding effort + effort flag rejected; succeeds without effort."""
        # 1. With effort flag: rejected with EFFORT_NOT_ALLOWED, details.reason: "model_encodes_effort"
        rc_bad, resp_bad = self.harness.cli(
            "agent", "create",
            "--name", "ag1",
            "--adapter", "agy",
            "--allowed-model", "gemini-3.8-flash-high",
            "--default-model", "gemini-3.8-flash-high",
            "--allowed-effort", "high",
            "--default-effort", "high",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_bad, c.EXIT_USAGE)
        self.assertFalse(resp_bad["ok"])
        self.assertEqual(resp_bad["error"]["code"], c.EFFORT_NOT_ALLOWED)
        self.assertEqual(resp_bad["error"]["details"]["reason"], "model_encodes_effort")

        # Verify no agent row
        conn = self.harness.db()
        cur = conn.execute("SELECT id FROM principals WHERE name = 'ag1';")
        self.assertIsNone(cur.fetchone())
        conn.close()

        # 2. Without effort flag: succeeds (rc 0)
        rc_ok, resp_ok = self.harness.cli(
            "agent", "create",
            "--name", "ag1",
            "--adapter", "agy",
            "--allowed-model", "gemini-3.8-flash-high",
            "--default-model", "gemini-3.8-flash-high",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc_ok, 0)
        self.assertTrue(resp_ok["ok"])
        self.assertEqual(resp_ok["data"]["name"], "ag1")

    def test_agent_show_not_found(self) -> None:
        """ACC: B2 - agent show no-such-agent returns rc 5 NOT_FOUND in standard JSON envelope."""
        rc, resp = self.harness.cli("agent", "show", "no-such-agent", user=True)
        self.assertEqual(rc, c.EXIT_NOT_FOUND)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.NOT_FOUND)
        self.assertEqual(resp["exit_code"], c.EXIT_NOT_FOUND)

    def test_agent_update_version_mismatch(self) -> None:
        """ACC: B9 - agent update fx --if-version 999 -> rc 6 VERSION_MISMATCH; agent unchanged."""
        # Create fx
        self.harness.cli(
            "agent", "create",
            "--name", "fx",
            "--adapter", "fake",
            "--allowed-model", "m-a",
            "--default-model", "m-a",
            "--max-concurrency", "2",
            user=True,
        )

        # Update with wrong version
        rc, resp = self.harness.cli(
            "agent", "update", "fx",
            "--if-version", "999",
            "--max-concurrency", "3",
            user=True,
        )
        self.assertEqual(rc, c.EXIT_CONFLICT)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], c.VERSION_MISMATCH)

        # Verify unchanged
        _, show = self.harness.cli("agent", "show", "fx", user=True)
        self.assertEqual(show["data"]["max_concurrency"], 2)
        self.assertEqual(show["data"]["version"], 1)

    def test_agent_default_profiles(self) -> None:
        """ACC: B10 - Agent show created without profile flags shows default edit and max dev."""
        rc, resp = self.harness.cli(
            "agent", "create",
            "--name", "fx_default_prof",
            "--adapter", "fake",
            "--allowed-model", "m-a",
            "--default-model", "m-a",
            "--max-concurrency", "1",
            user=True,
        )
        self.assertEqual(rc, 0)

        rc_show, show_resp = self.harness.cli("agent", "show", "fx_default_prof", user=True)
        self.assertEqual(rc_show, 0)
        self.assertEqual(show_resp["data"]["default_profile"], "edit")
        self.assertEqual(show_resp["data"]["max_unapproved_profile"], "dev")

    def test_agent_tokens(self) -> None:
        """ACC: C9 - Token printed once, list shows no secret, db holds only lookup and hash."""
        # Create plain agent
        self.harness.cli(
            "agent", "create",
            "--name", "plain",
            "--adapter", "none",
            "--delivery", "inbox",
            "--capability", "intake",
            "--max-concurrency", "1",
            user=True,
        )

        # Issue token
        rc_issue, issue_resp = self.harness.cli("agent", "token", "issue", "plain", user=True)
        self.assertEqual(rc_issue, 0)
        self.assertTrue(issue_resp["ok"])
        token = issue_resp["data"]["token"]
        lookup = issue_resp["data"]["lookup"]
        self.assertTrue(token.startswith("mesh_at_"))
        self.assertIn(lookup, token)

        # List tokens: list shows no secret
        rc_list, list_resp = self.harness.cli("agent", "token", "list", "plain", user=True)
        self.assertEqual(rc_list, 0)
        creds = list_resp["data"]["credentials"]
        self.assertEqual(len(creds), 1)
        self.assertEqual(creds[0]["lookup"], lookup)
        self.assertNotIn("secret", creds[0])
        self.assertNotIn(token, json.dumps(creds))

        # Check database: db holds only lookup and 32-byte hash
        conn = self.harness.db()
        cur = conn.execute("SELECT lookup, secret_hash FROM credentials WHERE lookup = ?;", (lookup,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["lookup"], lookup)
        # Hash is 32 bytes (sha256 digest)
        self.assertEqual(len(row["secret_hash"]), 32)
        # Raw secret not in hash
        self.assertNotIn(token.encode("utf-8"), row["secret_hash"])
        conn.close()

    def test_agent_create_not_installed_adapter(self) -> None:
        """ACC: N2 - Agent create with hermes or openclaw returns rc 13 ADAPTER_NOT_INSTALLED."""
        for adp in ["hermes", "openclaw"]:
            rc, resp = self.harness.cli(
                "agent", "create",
                "--name", f"ag_{adp}",
                "--adapter", adp,
                "--max-concurrency", "1",
                "--allowed-model", "m-a",
                user=True,
            )
            self.assertEqual(rc, c.EXIT_ADAPTER_UNAVAILABLE)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["code"], c.ADAPTER_NOT_INSTALLED)


if __name__ == "__main__":
    unittest.main()
