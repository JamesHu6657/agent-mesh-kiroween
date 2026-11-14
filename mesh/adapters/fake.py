"""Test double adapter ('fake') for offline testing per plan §14.9."""

import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    Probe,
    ProfileUnsupported,
    RunRequest,
    RunResult,
    record_adapter_probe,
)


class FakeAdapter(Adapter):
    """Test double adapter for offline integration tests."""

    id = "fake"
    kind = "test_double"
    binary_names = ["python3"]
    planned_version = "1.0.0"
    liveness = "stream"
    max_argv_prompt_bytes = 100_000
    launch_enabled = True
    launch_milestone = None

    def __init__(self, resume_verified: bool = False):
        self.resume_verified = resume_verified
        resume_status = "yes" if resume_verified else "unknown"
        resume_verif = "smoke" if resume_verified else "none"

        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="smoke", note="fake test double"),
            "stream_output": Capability("stream_output", supported="yes", verification="smoke", note="fake stdout/stderr"),
            "model_selection": Capability("model_selection", supported="yes", verification="smoke", note="fake models"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="smoke", note="fake reasoning effort"),
            "resume": Capability("resume", supported=resume_status, verification=resume_verif, note="fake session resume"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        if profile in ("readonly", "edit", "dev", "elevated", "unrestricted"):
            return ["--fake-profile", profile]
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        argv = [
            req.options.get("binary") or sys.executable,
            "-P",
            "-s",
            "-m",
            "mesh.testing.fake_agent",
            *self.profile_flags(req.profile),
        ]
        if req.resume_session_id:
            argv.extend(["--fake-resume", req.resume_session_id])
        argv.append(req.prompt_path)
        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        session_id = None
        try:
            with open(os.path.join(run_dir, "stdout.log"), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return RunResult()
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "session_id" in value:
                session_id = value["session_id"]
        return RunResult(final_text=text, session_id=session_id)

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        return ["m-a", "m-b", "m-c"]

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
            version=self.planned_version,
            status="verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=["--fake-profile"],
            models=self.list_models(binary_path),
        )
        record_adapter_probe(conn, self.id, probe)
        return probe
