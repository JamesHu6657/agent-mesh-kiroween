"""Adapter 'none' for inbox-only agents per plan §14.9."""

import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters.base import (
    Adapter,
    AdapterUnsupported,
    Capability,
    CommandSpec,
    Probe,
    RunRequest,
    RunResult,
    record_adapter_probe,
)


class NoneAdapter(Adapter):
    """Adapter for agents with no CLI launcher (inbox-only participation)."""

    id = "none"
    kind = "none"
    binary_names = []
    planned_version = None
    liveness = "none"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = None

    def __init__(self):
        self.capabilities = {
            "inbox_participation": Capability(
                "inbox_participation",
                supported="yes",
                verification="help",
                note="inbox participation only; cannot launch",
            )
        }

    def profile_flags(self, profile: str) -> List[str]:
        return []

    def build(self, req: RunRequest) -> CommandSpec:
        raise AdapterUnsupported("Adapter 'none' is for inbox-only agents and cannot launch processes")

    def parse(self, run_dir: str) -> RunResult:
        raise AdapterUnsupported("Adapter 'none' does not execute launched runs")

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        return None

    def probe(
        self,
        binary_path: Optional[str] = None,
        probed_version: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        timeout_s: float = 5.0,
    ) -> Probe:
        caps: Dict[str, Dict[str, Any]] = {}
        for name, cap in self.capabilities.items():
            caps[name] = cap.to_dict()

        probe = Probe(
            adapter=self.id,
            version=None,
            status="partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=[],
            models=None,
        )
        record_adapter_probe(conn, self.id, probe)
        return probe
