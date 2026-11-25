"""Unit tests for the lead-turn helpers (plan §12.4, §12.6; checklist L1, L3).

Review m2-r9 findings 6 and 7: the backoff read path is testing-only, and the prompt's
authority section lists only the categories that need approval plus every enabled
launch agent except the project's current lead.
"""

import os
import tempfile
import unittest

from mesh.core.db import init_db
from mesh.daemon.lead_turns import _authority, build_prompt_input


class TestLeadTurnAuthority(unittest.TestCase):
    """ACC: L1 (prompt authority clause) - the §12.6 authority section's data."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="mh-leadturns-unit-")
        self.conn = init_db(os.path.join(self.temp_dir.name, "mesh.db"))
        self.conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', 'now');"
        )
        self.conn.execute(
            """INSERT INTO projects (id, name, goal, status, created_by, created_via, version, created_at, updated_at)
               VALUES ('prj_1', 'P', 'Goal', 'active', 'usr_1', 'direct', 1, 'now', 'now');"""
        )
        for name, delivery, models in (
            ("flead", "launch", '["fake-model"]'),
            ("fother", "launch", '["other-model"]'),
            ("ibx", "inbox", '["inbox-model"]'),
        ):
            self.conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES (?, 'agent', ?, 'now');",
                (name, name),
            )
            self.conn.execute(
                """INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json,
                                       allowed_models_json, max_concurrency, created_at, updated_at)
                   VALUES (?, 'fake', ?, 1, '[]', ?, 1, 'now', 'now');""",
                (name, delivery, models),
            )
        self.conn.execute(
            """INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at)
               VALUES ('asg_1', 'prj_1', 'flead', 'usr_1', 'direct', 'now');"""
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_authority_lists_only_categories_needing_approval(self) -> None:
        """ACC: L1 - `deliver_to_base` (policy `not_required`) is not listed as needing
        approval, and the model list covers every enabled launch agent except the
        project's current lead (review m2-r9 finding 7)."""
        self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at, updated_by) "
            "VALUES ('approval.policy.deliver_to_base', '\"not_required\"', 'now', 'usr_1');"
        )
        self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at, updated_by) "
            "VALUES ('approval.policy.deploy', '\"direct_or_relayed\"', 'now', 'usr_1');"
        )
        authority = _authority(self.conn, "prj_1")
        self.assertNotIn("deliver_to_base", authority["approval_categories"])
        self.assertIn("deploy", authority["approval_categories"])
        # Every enabled launch agent except the current lead; inbox agents are not
        # assignable launch targets and are left out.
        self.assertEqual(sorted(authority["allowed_models"]), ["fother"])
        self.assertEqual(authority["allowed_models"]["fother"], ["other-model"])

    def test_build_prompt_input_uses_the_authority_data(self) -> None:
        """ACC: L1 - `build_prompt_input` carries the filtered authority section into
        the rendered prompt (review m2-r9 finding 7)."""
        self.conn.execute(
            "INSERT INTO settings (key, value_json, updated_at, updated_by) "
            "VALUES ('approval.policy.deliver_to_base', '\"not_required\"', 'now', 'usr_1');"
        )
        inp = build_prompt_input(self.conn, "prj_1", [], 100_000)
        self.assertNotIn("deliver_to_base", inp.authority["approval_categories"])
        self.assertEqual(sorted(inp.authority["allowed_models"]), ["fother"])


if __name__ == "__main__":
    unittest.main()
