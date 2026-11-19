"""Full review r3 P1-4: deliver intents (recovery side)."""

import os
import unittest

from tests.unit.test_full_review_r1 import TestP1_5DeliverIntentRecovery


class TestR3P1_4UnreadableRepoKeepsIntent(unittest.TestCase):
    setUp = TestP1_5DeliverIntentRecovery.setUp
    tearDown = TestP1_5DeliverIntentRecovery.tearDown
    _create_repo_project = TestP1_5DeliverIntentRecovery._create_repo_project
    _advance_integration = TestP1_5DeliverIntentRecovery._advance_integration
    _intent = TestP1_5DeliverIntentRecovery._intent
    delivered = TestP1_5DeliverIntentRecovery.delivered

    def test_repo_unavailable_at_start_keeps_the_intent(self) -> None:
        from mesh.core.repos import deliver_to_base
        from mesh.daemon import recovery

        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        path = self._intent(home, old, new)
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))  # then SIGKILL
        moved = self.repo + ".offline"
        os.rename(self.repo, moved)  # e.g. its disk is not mounted yet
        try:
            counts = {}
            recovery._step_deliver_intents(self.conn, home, counts)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.delivered(), [])
            self.assertEqual(counts.get("deliver_intent_errors"), 1, counts)
        finally:
            os.rename(moved, self.repo)
        counts = {}
        recovery._step_deliver_intents(self.conn, home, counts)
        self.assertEqual(counts.get("deliveries_recovered"), 1, counts)
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
