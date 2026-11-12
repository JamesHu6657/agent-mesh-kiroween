"""State machines and transitions per plan §6.

Defines valid states, allowed transitions, and transition guards for:
- Tasks (§6.2, T1–T22)
- Projects (§6.1, P1–P5)
- Runs (§6.3)
- Leases (§6.4)
- Approvals (§6.5)
- Outbox messages (§6.6)
"""

from typing import Dict, List, NamedTuple, Optional, Set, Tuple
from mesh import constants as c
from mesh.errors import MeshError


# ---------------------------------------------------------------------------
# Project State Machine (§6.1)
# ---------------------------------------------------------------------------

PROJECT_STATE_ACTIVE = "active"
PROJECT_STATE_PAUSED = "paused"
PROJECT_STATE_COMPLETED = "completed"
PROJECT_STATE_CANCELLED = "cancelled"

PROJECT_STATES: List[str] = [
    PROJECT_STATE_ACTIVE,
    PROJECT_STATE_PAUSED,
    PROJECT_STATE_COMPLETED,
    PROJECT_STATE_CANCELLED,
]

PROJECT_TERMINAL_STATES: Set[str] = {
    PROJECT_STATE_COMPLETED,
    PROJECT_STATE_CANCELLED,
}

ALLOWED_PROJECT_INITIAL_STATES: Set[str] = {
    PROJECT_STATE_ACTIVE,  # P1
}

# (from_state, to_state)
ALLOWED_PROJECT_TRANSITIONS: Set[Tuple[str, str]] = {
    (PROJECT_STATE_ACTIVE, PROJECT_STATE_PAUSED),     # P2
    (PROJECT_STATE_PAUSED, PROJECT_STATE_ACTIVE),     # P3
    (PROJECT_STATE_ACTIVE, PROJECT_STATE_COMPLETED),  # P4
    (PROJECT_STATE_ACTIVE, PROJECT_STATE_CANCELLED),  # P5
    (PROJECT_STATE_PAUSED, PROJECT_STATE_CANCELLED),  # P5
}


def is_project_transition_allowed(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None:
        return to_state in ALLOWED_PROJECT_INITIAL_STATES
    return (from_state, to_state) in ALLOWED_PROJECT_TRANSITIONS


def check_project_transition(from_state: Optional[str], to_state: str) -> None:
    if not is_project_transition_allowed(from_state, to_state):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Invalid project transition from '{from_state}' to '{to_state}'",
            details={"entity": "project", "from_state": from_state, "to_state": to_state},
        )


# ---------------------------------------------------------------------------
# Task State Machine (§6.2)
# ---------------------------------------------------------------------------

TASK_STATE_CREATED = "created"
TASK_STATE_ASSIGNED = "assigned"
TASK_STATE_QUEUED = "queued"
TASK_STATE_RUNNING = "running"
TASK_STATE_CANCELLING = "cancelling"
TASK_STATE_SUBMITTED = "submitted"
TASK_STATE_REWORK = "rework"
TASK_STATE_ACCEPTED = "accepted"
TASK_STATE_MERGING = "merging"
TASK_STATE_MERGE_FAILED = "merge_failed"
TASK_STATE_MERGED = "merged"
TASK_STATE_FAILED = "failed"
TASK_STATE_ESCALATED = "escalated"
TASK_STATE_CANCELLED = "cancelled"

TASK_STATES: List[str] = [
    TASK_STATE_CREATED,
    TASK_STATE_ASSIGNED,
    TASK_STATE_QUEUED,
    TASK_STATE_RUNNING,
    TASK_STATE_CANCELLING,
    TASK_STATE_SUBMITTED,
    TASK_STATE_REWORK,
    TASK_STATE_ACCEPTED,
    TASK_STATE_MERGING,
    TASK_STATE_MERGE_FAILED,
    TASK_STATE_MERGED,
    TASK_STATE_FAILED,
    TASK_STATE_ESCALATED,
    TASK_STATE_CANCELLED,
]

TASK_TERMINAL_STATES: Set[str] = {
    TASK_STATE_MERGED,
    TASK_STATE_CANCELLED,
}


def is_task_terminal(status: str, kind: str = "code") -> bool:
    """Check if task status is terminal for the given kind per plan §6.2."""
    if status in (TASK_STATE_MERGED, TASK_STATE_CANCELLED):
        return True
    if kind == "general" and status == TASK_STATE_ACCEPTED:
        return True
    return False

ALLOWED_TASK_INITIAL_STATES: Set[str] = {
    TASK_STATE_CREATED,   # T1
    TASK_STATE_ASSIGNED,  # T2
}

class TaskTransitionDef(NamedTuple):
    id: str
    from_states: List[str]
    to_state: str
    trigger: str
    actor: str
    description: str


TASK_TRANSITIONS_TABLE: List[TaskTransitionDef] = [
    TaskTransitionDef("T3", [TASK_STATE_CREATED], TASK_STATE_ASSIGNED, "task assign", "lead", "Task assigned to agent"),
    TaskTransitionDef("T4", [TASK_STATE_ASSIGNED], TASK_STATE_QUEUED, "scheduler", "system", "Deps satisfied, queued"),
    TaskTransitionDef("T5", [TASK_STATE_QUEUED], TASK_STATE_RUNNING, "scheduler/task start", "system/assignee", "Slot free, started"),
    TaskTransitionDef("T6", [TASK_STATE_QUEUED], TASK_STATE_FAILED, "scheduler", "system", "Failed before launch (model/adapter/timeout)"),
    TaskTransitionDef("T7", [TASK_STATE_RUNNING], TASK_STATE_SUBMITTED, "task submit/auto-submit", "executor/system", "Deliverable submitted"),
    TaskTransitionDef("T8", [TASK_STATE_RUNNING], TASK_STATE_FAILED, "supervisor/task fail", "system/executor", "Run failed or timed out"),
    TaskTransitionDef("T9", [TASK_STATE_RUNNING, TASK_STATE_SUBMITTED], TASK_STATE_CANCELLING, "task cancel", "lead", "Cancel while holding lease"),
    TaskTransitionDef("T10", [TASK_STATE_CANCELLING], TASK_STATE_CANCELLED, "supervisor", "system", "Process group confirmed gone"),
    TaskTransitionDef("T11", [TASK_STATE_SUBMITTED], TASK_STATE_ACCEPTED, "task accept", "lead", "Deliverable accepted"),
    TaskTransitionDef("T12", [TASK_STATE_SUBMITTED], TASK_STATE_REWORK, "task reject", "lead", "Deliverable rejected with reason"),
    TaskTransitionDef("T13", [TASK_STATE_REWORK], TASK_STATE_QUEUED, "scheduler", "system", "Rework queued"),
    TaskTransitionDef("T14", [TASK_STATE_ACCEPTED], TASK_STATE_MERGING, "task merge", "lead", "Code task merge started"),
    TaskTransitionDef("T15", [TASK_STATE_MERGING], TASK_STATE_MERGED, "merge pipeline", "system", "Clean merge and tests passed"),
    TaskTransitionDef("T16", [TASK_STATE_MERGING], TASK_STATE_MERGE_FAILED, "merge pipeline", "system", "Conflict or tests failed"),
    TaskTransitionDef("T17", [TASK_STATE_MERGE_FAILED], TASK_STATE_MERGING, "task merge", "lead", "Retry merge"),
    TaskTransitionDef("T18", [TASK_STATE_MERGE_FAILED], TASK_STATE_REWORK, "task reject", "lead", "Reject failed merge to rework"),
    TaskTransitionDef("T19", [TASK_STATE_FAILED, TASK_STATE_ESCALATED], TASK_STATE_QUEUED, "task retry", "lead", "Retry failed/escalated task"),
    TaskTransitionDef(
        "T20",
        [
            TASK_STATE_ASSIGNED,
            TASK_STATE_QUEUED,
            TASK_STATE_REWORK,
            TASK_STATE_FAILED,
            TASK_STATE_ESCALATED,
            TASK_STATE_MERGE_FAILED,
        ],
        TASK_STATE_ASSIGNED,
        "task reassign",
        "lead",
        "Reassign to new agent",
    ),
    TaskTransitionDef("T21", [TASK_STATE_FAILED], TASK_STATE_ESCALATED, "task escalate", "lead", "Escalate to user/origin"),
    TaskTransitionDef(
        "T22",
        [
            TASK_STATE_CREATED,
            TASK_STATE_ASSIGNED,
            TASK_STATE_QUEUED,
            TASK_STATE_REWORK,
            TASK_STATE_SUBMITTED,
            TASK_STATE_ACCEPTED,
            TASK_STATE_FAILED,
            TASK_STATE_ESCALATED,
            TASK_STATE_MERGE_FAILED,
        ],
        TASK_STATE_CANCELLED,
        "task cancel",
        "lead",
        "Cancel task without lease",
    ),
]

ALLOWED_TASK_TRANSITIONS: Set[Tuple[str, str]] = set()
for _t in TASK_TRANSITIONS_TABLE:
    for _from in _t.from_states:
        ALLOWED_TASK_TRANSITIONS.add((_from, _t.to_state))


def is_task_transition_allowed(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None:
        return to_state in ALLOWED_TASK_INITIAL_STATES
    return (from_state, to_state) in ALLOWED_TASK_TRANSITIONS


def check_task_transition(from_state: Optional[str], to_state: str) -> None:
    if not is_task_transition_allowed(from_state, to_state):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Invalid task transition from '{from_state}' to '{to_state}'",
            details={"entity": "task", "from_state": from_state, "to_state": to_state},
        )


# ---------------------------------------------------------------------------
# Run State Machine (§6.3)
# ---------------------------------------------------------------------------

RUN_STATE_STARTING = "starting"
RUN_STATE_RUNNING = "running"
RUN_STATE_EXITED = "exited"
RUN_STATE_KILLED = "killed"
RUN_STATE_LOST = "lost"
RUN_STATE_SPAWN_FAILED = "spawn_failed"

RUN_STATES: List[str] = [
    RUN_STATE_STARTING,
    RUN_STATE_RUNNING,
    RUN_STATE_EXITED,
    RUN_STATE_KILLED,
    RUN_STATE_LOST,
    RUN_STATE_SPAWN_FAILED,
]

ALLOWED_RUN_INITIAL_STATES: Set[str] = {
    RUN_STATE_STARTING,
}

ALLOWED_RUN_TRANSITIONS: Set[Tuple[str, str]] = {
    (RUN_STATE_STARTING, RUN_STATE_RUNNING),
    (RUN_STATE_STARTING, RUN_STATE_SPAWN_FAILED),
    (RUN_STATE_RUNNING, RUN_STATE_EXITED),
    (RUN_STATE_RUNNING, RUN_STATE_KILLED),
    (RUN_STATE_RUNNING, RUN_STATE_LOST),
}


def is_run_transition_allowed(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None:
        return to_state in ALLOWED_RUN_INITIAL_STATES
    return (from_state, to_state) in ALLOWED_RUN_TRANSITIONS


def check_run_transition(from_state: Optional[str], to_state: str) -> None:
    if not is_run_transition_allowed(from_state, to_state):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Invalid run transition from '{from_state}' to '{to_state}'",
            details={"entity": "run", "from_state": from_state, "to_state": to_state},
        )


# ---------------------------------------------------------------------------
# Approval State Machine (§6.5)
# ---------------------------------------------------------------------------

APPROVAL_STATE_PENDING = "pending"
APPROVAL_STATE_APPROVED = "approved"
APPROVAL_STATE_REJECTED = "rejected"
APPROVAL_STATE_EXPIRED = "expired"
APPROVAL_STATE_CONSUMED = "consumed"
APPROVAL_STATE_CANCELLED = "cancelled"

APPROVAL_STATES: List[str] = [
    APPROVAL_STATE_PENDING,
    APPROVAL_STATE_APPROVED,
    APPROVAL_STATE_REJECTED,
    APPROVAL_STATE_EXPIRED,
    APPROVAL_STATE_CONSUMED,
    APPROVAL_STATE_CANCELLED,
]

ALLOWED_APPROVAL_INITIAL_STATES: Set[str] = {
    APPROVAL_STATE_PENDING,
}

ALLOWED_APPROVAL_TRANSITIONS: Set[Tuple[str, str]] = {
    (APPROVAL_STATE_PENDING, APPROVAL_STATE_APPROVED),
    (APPROVAL_STATE_PENDING, APPROVAL_STATE_REJECTED),
    (APPROVAL_STATE_PENDING, APPROVAL_STATE_EXPIRED),
    (APPROVAL_STATE_PENDING, APPROVAL_STATE_CANCELLED),
    (APPROVAL_STATE_APPROVED, APPROVAL_STATE_CONSUMED),
    (APPROVAL_STATE_APPROVED, APPROVAL_STATE_EXPIRED),
}


def is_approval_transition_allowed(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None:
        return to_state in ALLOWED_APPROVAL_INITIAL_STATES
    return (from_state, to_state) in ALLOWED_APPROVAL_TRANSITIONS


def check_approval_transition(from_state: Optional[str], to_state: str) -> None:
    if not is_approval_transition_allowed(from_state, to_state):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Invalid approval transition from '{from_state}' to '{to_state}'",
            details={"entity": "approval", "from_state": from_state, "to_state": to_state},
        )


# ---------------------------------------------------------------------------
# Outbox State Machine (§6.6)
# ---------------------------------------------------------------------------

OUTBOX_STATE_PENDING = "pending"
OUTBOX_STATE_DELIVERING = "delivering"
OUTBOX_STATE_DELIVERED = "delivered"
OUTBOX_STATE_ACKED = "acked"
OUTBOX_STATE_DEAD = "dead"
OUTBOX_STATE_SUPERSEDED = "superseded"

OUTBOX_STATES: List[str] = [
    OUTBOX_STATE_PENDING,
    OUTBOX_STATE_DELIVERING,
    OUTBOX_STATE_DELIVERED,
    OUTBOX_STATE_ACKED,
    OUTBOX_STATE_DEAD,
    OUTBOX_STATE_SUPERSEDED,
]

ALLOWED_OUTBOX_INITIAL_STATES: Set[str] = {
    OUTBOX_STATE_PENDING,
}

ALLOWED_OUTBOX_TRANSITIONS: Set[Tuple[str, str]] = {
    (OUTBOX_STATE_PENDING, OUTBOX_STATE_DELIVERING),
    (OUTBOX_STATE_DELIVERING, OUTBOX_STATE_PENDING),
    (OUTBOX_STATE_DELIVERING, OUTBOX_STATE_DELIVERED),
    (OUTBOX_STATE_DELIVERING, OUTBOX_STATE_DEAD),
    (OUTBOX_STATE_PENDING, OUTBOX_STATE_DEAD),
    (OUTBOX_STATE_DELIVERED, OUTBOX_STATE_ACKED),
    (OUTBOX_STATE_PENDING, OUTBOX_STATE_SUPERSEDED),
    (OUTBOX_STATE_DELIVERED, OUTBOX_STATE_SUPERSEDED),
}


def is_outbox_transition_allowed(from_state: Optional[str], to_state: str) -> bool:
    if from_state is None:
        return to_state in ALLOWED_OUTBOX_INITIAL_STATES
    return (from_state, to_state) in ALLOWED_OUTBOX_TRANSITIONS


def check_outbox_transition(from_state: Optional[str], to_state: str) -> None:
    if not is_outbox_transition_allowed(from_state, to_state):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Invalid outbox transition from '{from_state}' to '{to_state}'",
            details={"entity": "outbox", "from_state": from_state, "to_state": to_state},
        )
