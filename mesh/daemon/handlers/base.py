"""Base handler context, result structures, and effects queue for daemon requests."""

from dataclasses import dataclass, field
import sqlite3
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Effect:
    """An external side-effect to execute only after transaction commit."""
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HandlerContext:
    """Context passed to resource command handlers inside meshd."""
    conn: sqlite3.Connection
    principal: Dict[str, Any]
    actor_kind: str
    via: str
    request_id: str
    home: str
    cmd: str
    args: Dict[str, Any]
    dry_run: bool = False
    k_user: Optional[bytes] = None
    effects: List[Effect] = field(default_factory=list)
    testing_mode: bool = False
    approval: Optional[str] = None
    relayed_by: Optional[str] = None
    user_statement: Optional[str] = None
    evidence_ref: Optional[str] = None
    if_version: Optional[int] = None
    credential_id: Optional[str] = None


@dataclass
class HandlerResult:
    """Result returned by a handler."""
    data: Any = None
    exit_code: int = 0
    warnings: List[str] = field(default_factory=list)
    transitions: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
