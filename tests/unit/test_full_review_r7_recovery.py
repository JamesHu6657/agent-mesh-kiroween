"""Full review r7 0005 (R3-P2-1): a deliver intent is settled when the base moved on
past the delivered commit, and is not when the base never reached it."""

import os
import unittest

from tests.unit.test_full_review_r1 import TestP1_5DeliverIntentRecovery


class TestR7DeliverIntentAncestry(unittest.TestCase):
    setUp = TestP1_5DeliverIntentRecovery.setUp
    tearDown = TestP1_5DeliverIntentRecovery.tearDown
    _create_repo_project = TestP1_5DeliverIntentRecovery._create_repo_project
    _advance_integration = TestP1_5DeliverIntentRecovery._advance_integration
    _intent = TestP1_5DeliverIntentRecovery._intent
    delivered = TestP1_5DeliverIntentRecovery.delivered

    def _commit_on_main(self, name: str) -> str:
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(name + "\n")
        self.fixture.git(self.repo, "add", "-A")
        self.fixture.git(self.repo, "commit", "-m", name, identity=True)
        return self.fixture.head(self.repo, "main")

    def _setup(self):
        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        return home, old, new

    def test_base_moved_past_delivered_commit_is_recorded(self) -> None:
        from mesh.core.repos import deliver_to_base
        from mesh.daemon.recovery import settle_deliver_intent

        home, old, new = self._setup()
        path = self._intent(home, old, new)
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))  # tx lost
        cur = self._commit_on_main("user-after.txt")  # user commits on top while meshd is down
        self.assertNotEqual(cur, new)
        settle_deliver_intent(self.conn, home, "prj_x")
        self.assertFalse(os.path.exists(path))
        rows = self.delivered()
        self.assertEqual(len(rows), 1)
        self.assertIn(new, rows[0][0])
        settle_deliver_intent(self.conn, home, "prj_x")  # idempotent
        self.assertEqual(len(self.delivered()), 1)

    def test_base_moved_elsewhere_without_delivery_is_not_recorded(self) -> None:
        from mesh.daemon.recovery import settle_deliver_intent

        home, old, new = self._setup()
        path = self._intent(home, old, new)
        cur = self._commit_on_main("unrelated.txt")  # base moved, but `new` never landed
        self.assertNotIn(cur, (old, new))
        settle_deliver_intent(self.conn, home, "prj_x")
        self.assertEqual(len(self.delivered()), 0)
        self.assertFalse(os.path.exists(path))

    def test_base_still_at_old_is_not_recorded(self) -> None:
        from mesh.daemon.recovery import settle_deliver_intent

        home, old, new = self._setup()
        self._intent(home, old, new)
        settle_deliver_intent(self.conn, home, "prj_x")
        self.assertEqual(len(self.delivered()), 0)


if __name__ == "__main__":
    unittest.main()
