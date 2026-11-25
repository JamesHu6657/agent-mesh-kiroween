"""Unit tests for state machines and transitions per plan §6 and §17.2.

ACC: F7
"""

import unittest
from mesh import constants as c
from mesh.core import states
from mesh.errors import MeshError


INDEPENDENT_TASK_INITIAL_STATES = {"created", "assigned"}

INDEPENDENT_TASK_TRANSITIONS = {
    # T3: created -> assigned
    ("created", "assigned"),
    # T4: assigned -> queued
    ("assigned", "queued"),
    # T5: queued -> running
    ("queued", "running"),
    # T6: queued -> failed
    ("queued", "failed"),
    # T7: running -> submitted
    ("running", "submitted"),
    # T8: running -> failed
    ("running", "failed"),
    # T9: running/submitted -> cancelling
    ("running", "cancelling"),
    ("submitted", "cancelling"),
    # T10: cancelling -> cancelled
    ("cancelling", "cancelled"),
    # T11: submitted -> accepted
    ("submitted", "accepted"),
    # T12: submitted -> rework
    ("submitted", "rework"),
    # T13: rework -> queued
    ("rework", "queued"),
    # T14: accepted -> merging
    ("accepted", "merging"),
    # T15: merging -> merged
    ("merging", "merged"),
    # T16: merging -> merge_failed
    ("merging", "merge_failed"),
    # T17: merge_failed -> merging
    ("merge_failed", "merging"),
    # T18: merge_failed -> rework
    ("merge_failed", "rework"),
    # T19: failed/escalated -> queued
    ("failed", "queued"),
    ("escalated", "queued"),
    # T20: assigned, queued, rework, failed, escalated, merge_failed -> assigned
    ("assigned", "assigned"),
    ("queued", "assigned"),
    ("rework", "assigned"),
    ("failed", "assigned"),
    ("escalated", "assigned"),
    ("merge_failed", "assigned"),
    # T21: failed -> escalated
    ("failed", "escalated"),
    # T22: created, assigned, queued, rework, submitted, accepted, failed, escalated, merge_failed -> cancelled
    ("created", "cancelled"),
    ("assigned", "cancelled"),
    ("queued", "cancelled"),
    ("rework", "cancelled"),
    ("submitted", "cancelled"),
    ("accepted", "cancelled"),
    ("failed", "cancelled"),
    ("escalated", "cancelled"),
    ("merge_failed", "cancelled"),
}


class TestStates(unittest.TestCase):
    """Exhaustive state machine tests. ACC: F7"""

    def test_task_transitions_exhaustive(self) -> None:
        """Every allowed task transition passes; all other (from, to) pairs fail."""
        # Verify states module tables match independent plan §6.2 encoding
        self.assertEqual(states.ALLOWED_TASK_INITIAL_STATES, INDEPENDENT_TASK_INITIAL_STATES)
        self.assertEqual(states.ALLOWED_TASK_TRANSITIONS, INDEPENDENT_TASK_TRANSITIONS)

        # Initial transitions (from None)
        for s in states.TASK_STATES:
            if s in INDEPENDENT_TASK_INITIAL_STATES:
                self.assertTrue(states.is_task_transition_allowed(None, s))
                states.check_task_transition(None, s)
            else:
                self.assertFalse(states.is_task_transition_allowed(None, s))
                with self.assertRaises(MeshError) as ctx:
                    states.check_task_transition(None, s)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        # Pairwise transitions (from s1 to s2)
        total_pairs = 0
        allowed_count = 0
        for s1 in states.TASK_STATES:
            for s2 in states.TASK_STATES:
                total_pairs += 1
                pair = (s1, s2)
                expected_allowed = pair in INDEPENDENT_TASK_TRANSITIONS
                actual_allowed = states.is_task_transition_allowed(s1, s2)
                self.assertEqual(
                    actual_allowed,
                    expected_allowed,
                    f"Transition {s1} -> {s2} mismatch: expected {expected_allowed}, got {actual_allowed}",
                )
                if expected_allowed:
                    allowed_count += 1
                    states.check_task_transition(s1, s2)
                else:
                    with self.assertRaises(MeshError) as ctx:
                        states.check_task_transition(s1, s2)
                    self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                    self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        self.assertEqual(total_pairs, len(states.TASK_STATES) ** 2)
        self.assertEqual(allowed_count, len(INDEPENDENT_TASK_TRANSITIONS))

    def test_task_terminal_states(self) -> None:
        """ACC: F7 - Verify terminal state rules per plan §6.2.

        Terminal states: merged, cancelled, and accepted when kind=general.
        """
        # Merged and cancelled are always terminal
        for kind in ("code", "general"):
            self.assertTrue(states.is_task_terminal("merged", kind=kind))
            self.assertTrue(states.is_task_terminal("cancelled", kind=kind))

        # Accepted is terminal only for kind=general
        self.assertTrue(states.is_task_terminal("accepted", kind="general"))
        self.assertFalse(states.is_task_terminal("accepted", kind="code"))

        # Other states are never terminal
        non_terminals = [
            "created",
            "assigned",
            "queued",
            "running",
            "cancelling",
            "submitted",
            "rework",
            "merging",
            "merge_failed",
            "failed",
            "escalated",
        ]
        for st in non_terminals:
            for kind in ("code", "general"):
                self.assertFalse(states.is_task_terminal(st, kind=kind))

    def test_project_transitions_exhaustive(self) -> None:
        """Every allowed project transition passes; all other pairs fail."""
        for s in states.PROJECT_STATES:
            if s in states.ALLOWED_PROJECT_INITIAL_STATES:
                self.assertTrue(states.is_project_transition_allowed(None, s))
                states.check_project_transition(None, s)
            else:
                self.assertFalse(states.is_project_transition_allowed(None, s))
                with self.assertRaises(MeshError) as ctx:
                    states.check_project_transition(None, s)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        for s1 in states.PROJECT_STATES:
            for s2 in states.PROJECT_STATES:
                expected = (s1, s2) in states.ALLOWED_PROJECT_TRANSITIONS
                self.assertEqual(states.is_project_transition_allowed(s1, s2), expected)
                if expected:
                    states.check_project_transition(s1, s2)
                else:
                    with self.assertRaises(MeshError) as ctx:
                        states.check_project_transition(s1, s2)
                    self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                    self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

    def test_run_transitions_exhaustive(self) -> None:
        """Every allowed run transition passes; all other pairs fail."""
        for s in states.RUN_STATES:
            if s in states.ALLOWED_RUN_INITIAL_STATES:
                self.assertTrue(states.is_run_transition_allowed(None, s))
                states.check_run_transition(None, s)
            else:
                self.assertFalse(states.is_run_transition_allowed(None, s))
                with self.assertRaises(MeshError) as ctx:
                    states.check_run_transition(None, s)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        for s1 in states.RUN_STATES:
            for s2 in states.RUN_STATES:
                expected = (s1, s2) in states.ALLOWED_RUN_TRANSITIONS
                self.assertEqual(states.is_run_transition_allowed(s1, s2), expected)
                if expected:
                    states.check_run_transition(s1, s2)
                else:
                    with self.assertRaises(MeshError) as ctx:
                        states.check_run_transition(s1, s2)
                    self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                    self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

    def test_approval_transitions_exhaustive(self) -> None:
        """Every allowed approval transition passes; all other pairs fail."""
        for s in states.APPROVAL_STATES:
            if s in states.ALLOWED_APPROVAL_INITIAL_STATES:
                self.assertTrue(states.is_approval_transition_allowed(None, s))
                states.check_approval_transition(None, s)
            else:
                self.assertFalse(states.is_approval_transition_allowed(None, s))
                with self.assertRaises(MeshError) as ctx:
                    states.check_approval_transition(None, s)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        for s1 in states.APPROVAL_STATES:
            for s2 in states.APPROVAL_STATES:
                expected = (s1, s2) in states.ALLOWED_APPROVAL_TRANSITIONS
                self.assertEqual(states.is_approval_transition_allowed(s1, s2), expected)
                if expected:
                    states.check_approval_transition(s1, s2)
                else:
                    with self.assertRaises(MeshError) as ctx:
                        states.check_approval_transition(s1, s2)
                    self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                    self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

    def test_outbox_transitions_exhaustive(self) -> None:
        """Every allowed outbox transition passes; all other pairs fail."""
        for s in states.OUTBOX_STATES:
            if s in states.ALLOWED_OUTBOX_INITIAL_STATES:
                self.assertTrue(states.is_outbox_transition_allowed(None, s))
                states.check_outbox_transition(None, s)
            else:
                self.assertFalse(states.is_outbox_transition_allowed(None, s))
                with self.assertRaises(MeshError) as ctx:
                    states.check_outbox_transition(None, s)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)

        for s1 in states.OUTBOX_STATES:
            for s2 in states.OUTBOX_STATES:
                expected = (s1, s2) in states.ALLOWED_OUTBOX_TRANSITIONS
                self.assertEqual(states.is_outbox_transition_allowed(s1, s2), expected)
                if expected:
                    states.check_outbox_transition(s1, s2)
                else:
                    with self.assertRaises(MeshError) as ctx:
                        states.check_outbox_transition(s1, s2)
                    self.assertEqual(ctx.exception.exit_code, c.EXIT_CONFLICT)
                    self.assertEqual(ctx.exception.code, c.INVALID_TRANSITION)


if __name__ == "__main__":
    unittest.main()
