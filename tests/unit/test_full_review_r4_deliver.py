"""Full review r4 P1-1: an earlier delivery's intent is settled in its own transaction."""

import os
import unittest

from tests.unit.test_full_review_r1 import TestP1_5DeliverIntentRecovery


class TestR4P1_1SettleFirst(unittest.TestCase):
    setUp = TestP1_5DeliverIntentRecovery.setUp
    tearDown = TestP1_5DeliverIntentRecovery.tearDown
    _create_repo_project = TestP1_5DeliverIntentRecovery._create_repo_project
    _advance_integration = TestP1_5DeliverIntentRecovery._advance_integration
    _intent = TestP1_5DeliverIntentRecovery._intent
    delivered = TestP1_5DeliverIntentRecovery.delivered

    def test_settled_delivery_survives_a_later_rollback(self) -> None:
        from mesh.core.repos import deliver_to_base
        from mesh.daemon.recovery import settle_deliver_intent

        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        path = self._intent(home, old, new)
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))  # tx lost
        settle_deliver_intent(self.conn, home, "prj_x")
        self.assertFalse(os.path.exists(path))
        self.conn.execute("BEGIN;")  # the new request's transaction fails afterwards
        self.conn.execute("ROLLBACK;")
        self.assertEqual(len(self.delivered()), 1)
        settle_deliver_intent(self.conn, home, "../../etc/passwd")  # never a path outside


class TestR5P2_2PendingBeforeNoop(unittest.TestCase):
    setUp = TestP1_5DeliverIntentRecovery.setUp
    tearDown = TestP1_5DeliverIntentRecovery.tearDown
    _create_repo_project = TestP1_5DeliverIntentRecovery._create_repo_project
    _advance_integration = TestP1_5DeliverIntentRecovery._advance_integration
    _intent = TestP1_5DeliverIntentRecovery._intent

    def test_unsettled_intent_is_not_hidden_by_the_noop(self) -> None:
        """Full review r5 P2-2: base already at integration (the lost delivery moved it)
        and its intent unsettled: the handler refuses instead of answering noop."""
        from unittest import mock
        from mesh.core.repos import deliver_to_base
        from mesh.daemon.handlers import project as h
        from mesh.daemon.handlers.base import HandlerContext
        from mesh.errors import MeshError

        self._create_repo_project()
        old, new = self._advance_integration()
        home = self.fixture.path("home")
        os.makedirs(home)
        self._intent(home, old, new)
        deliver_to_base(self.repo, "main", self.integration, expected=(old, new))
        ctx = HandlerContext(conn=self.conn, principal={"id": "usr_1", "kind": "user"}, actor_kind="user",
                             via="direct", request_id="req_n", home=home, cmd="project.deliver",
                             args={"project_id": "prj_x"})
        with mock.patch.object(h, "check_authz"), \
                mock.patch("mesh.daemon.merge.merge_in_progress_for_repo", return_value=None), \
                mock.patch("mesh.core.invariants.check_i9_integrated_unmerged_tasks", return_value=[]):
            with self.assertRaises(MeshError) as cm:
                h.handle_project_deliver(ctx)
        self.assertEqual(cm.exception.details.get("reason"), "deliver_intent_pending")


if __name__ == "__main__":
    unittest.main()
