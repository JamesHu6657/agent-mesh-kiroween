"""OpenClaw CLI adapter (not installed) per plan §14.8."""

import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters.base import (
    Adapter,
    AdapterNotInstalled,
    Capability,
    CommandSpec,
    EXPECTED_HELP_HASHES,
    Probe,
    RunRequest,
    RunResult,
    find_missing_flags,
    parse_version_output,
    record_adapter_probe,
    run_readonly_cmd,
)


class OpenclawAdapter(Adapter):
    """Adapter for OpenClaw (not installed)."""

    id = "openclaw"
    kind = "vendor"
    binary_names = ["openclaw"]
    planned_version = None
    liveness = "none"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = "M4"

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="unknown", verification="none", note="not installed"),
            "stream_output": Capability("stream_output", supported="unknown", verification="none", note="not installed"),
            "model_selection": Capability("model_selection", supported="unknown", verification="none", note="not installed"),
            "reasoning_effort": Capability("reasoning_effort", supported="unknown", verification="none", note="not installed"),
            "resume": Capability("resume", supported="unknown", verification="none", note="not installed"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        raise AdapterNotInstalled(self.id)

    def build(self, req: RunRequest) -> CommandSpec:
        raise AdapterNotInstalled(self.id)

    def parse(self, run_dir: str) -> RunResult:
        raise AdapterNotInstalled(self.id)

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        return None

    def probe(
        self,
        binary_path: Optional[str] = None,
        probed_version: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        timeout_s: float = 5.0,
    ) -> Probe:
        bin_path = self.resolve_binary(binary_path)
        caps: Dict[str, Dict[str, Any]] = {}
        for name, cap in self.capabilities.items():
            caps[name] = cap.to_dict()

        if not bin_path:
            probe = Probe(
                adapter=self.id,
                version=None,
                status="not_installed",
                launch_enabled=self.launch_enabled,
                launch_milestone=self.launch_milestone,
                capabilities=caps,
                flags=[],
                models=None,
                binary_path=binary_path,
            )
            record_adapter_probe(conn, self.id, probe, bin_path)
            return probe

        rc, v_out, v_err, _ = run_readonly_cmd([bin_path, "--version"], timeout_s=timeout_s)
        if rc != 0:
            probe = Probe(
                adapter=self.id,
                version=None,
                status="error",
                launch_enabled=self.launch_enabled,
                launch_milestone=self.launch_milestone,
                capabilities=caps,
                flags=[],
                models=None,
                binary_path=bin_path,
                details={"error": "version_check_failed", "exit_code": rc, "stderr": v_err},
            )
            record_adapter_probe(conn, self.id, probe, bin_path)
            return probe

        v = probed_version or parse_version_output(v_out) or parse_version_output(v_err)
        res_h = run_readonly_cmd([bin_path, "--help"], timeout_s=timeout_s)
        flags: List[str] = []
        flag_diff = None
        expected_help = EXPECTED_HELP_HASHES.get("openclaw")
        if expected_help and res_h.help_sha256 != expected_help:
            missing = find_missing_flags(flags, res_h.help_text)
            flag_diff = {"match": False, "missing_flags": missing}
        probe = Probe(
            adapter=self.id,
            version=v,
            status="unverified" if not v else "partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=flags,
            models=None,
            binary_path=bin_path,
            help_sha256=res_h.help_sha256 if res_h.returncode == 0 else None,
            flag_diff=flag_diff,
        )
        record_adapter_probe(conn, self.id, probe, bin_path)
        return probe
