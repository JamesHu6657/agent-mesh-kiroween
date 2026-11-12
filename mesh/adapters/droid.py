"""Factory droid CLI adapter per plan §14.3."""

import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    EXPECTED_HELP_HASHES,
    Probe,
    ProfileUnsupported,
    RunRequest,
    RunResult,
    find_missing_flags,
    parse_version_output,
    record_adapter_probe,
    run_readonly_cmd,
)


class DroidAdapter(Adapter):
    """Adapter for Factory droid CLI."""

    id = "droid"
    kind = "vendor"
    binary_names = ["droid"]
    planned_version = "1.3.0"
    verified_version = "1.3.0"
    liveness = "stream"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = "M3"

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="help", note="droid exec"),
            "stream_output": Capability("stream_output", supported="yes", verification="help", note="--output-format stream-json"),
            "model_selection": Capability("model_selection", supported="yes", verification="help", note="-m/--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="help", note="exec-init-event-opus55-high.json"),
            "resume": Capability("resume", supported="unknown", verification="help", note="-s/--session-id"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        if profile == "readonly":
            return []
        elif profile == "edit":
            return ["--auto", "low"]
        elif profile == "dev":
            return ["--auto", "medium"]
        elif profile == "elevated":
            return ["--auto", "high"]
        elif profile == "unrestricted":
            return ["--skip-permissions-unsafe"]
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        binary = req.options.get("binary") or self.binary_names[0]
        argv = [
            binary,
            "exec",
            "--output-format",
            "stream-json",
            "--cwd",
            req.workspace,
            "--model",
            req.model or "claude-opus-5-5",
        ]
        if req.effort:
            argv.extend(["--reasoning-effort", req.effort])

        argv.extend(self.profile_flags(req.profile))

        if req.resume_session_id:
            argv.extend(["--session-id", req.resume_session_id])

        run_id = req.options.get("run_id") or "run_default"
        argv.extend(["--log-group-id", run_id, "--file", req.prompt_path])

        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        # droid 1.6.0 `exec --output-format stream-json`, one JSON object per line
        # (tests/fixtures/real/droid/1.6.0/):
        #   smoke-S-X-1-stdout.jsonl:1  {"type":"system","subtype":"init","session_id":...} -> session_id
        #   smoke-S-X-1-stdout.jsonl:3  {"type":"message","role":"assistant","text":...}    -> not used
        #   smoke-S-X-1-stdout.jsonl:4  {"type":"completion","finalText":...,"usage":{...}} -> final_text, usage
        #   smoke-S-X-7-stdout.jsonl:6  {"type":"error","source":"cli","message":...}       -> not used
        # Without a completion event final_text is "" and the supervisor falls back to the raw
        # stdout.log. C2 shows no model/auth exit code, so vendor_exit_meaning stays None.
        scan = _scan_stream(os.path.join(run_dir, "stdout.log"))
        session_id = scan["session_id"]
        if not session_id:
            # droid 1.3.0 init-event fixture name; session id only
            session_id = _scan_stream(os.path.join(run_dir, "exec-init-event-opus55-high.json"))["session_id"]
        return RunResult(
            final_text=scan["final_text"] or "",
            session_id=session_id,
            usage=scan["usage"],
            vendor_exit_meaning=None,
        )

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        bin_path = self.resolve_binary(binary_path)
        if not bin_path:
            return None
        res = run_readonly_cmd([bin_path, "exec", "--help"])
        if res.returncode == 0:
            return self.parse_models_from_help(res.help_text)
        return None

    @staticmethod
    def parse_models_from_help(help_text: str) -> List[str]:
        lines = help_text.splitlines()
        models: List[str] = []
        in_models = False
        for line in lines:
            if "Available Models:" in line:
                in_models = True
                continue
            if in_models:
                stripped = line.strip()
                if not stripped:
                    break
                # First token is model id
                parts = stripped.split()
                if parts:
                    models.append(parts[0])
        return models

    def probe(
        self,
        binary_path: Optional[str] = None,
        probed_version: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        timeout_s: float = 5.0,
    ) -> Probe:
        bin_path = self.resolve_binary(binary_path)
        if not bin_path:
            caps = {name: c.to_dict() for name, c in self.capabilities.items()}
            for cdict in caps.values():
                cdict["verification"] = "none"
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

        # 1. Version check: droid --version
        rc, v_out, v_err, _ = run_readonly_cmd([bin_path, "--version"], timeout_s=timeout_s)
        if rc != 0:
            caps = {name: c.to_dict() for name, c in self.capabilities.items()}
            for cdict in caps.values():
                cdict["verification"] = "none"
            err_kind = "timeout" if rc == -1 else "version_check_failed"
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
                details={"error": err_kind, "exit_code": rc, "stderr": v_err},
            )
            record_adapter_probe(conn, self.id, probe, bin_path)
            return probe

        v = probed_version or parse_version_output(v_out) or parse_version_output(v_err)
        if not v:
            caps = {name: c.to_dict() for name, c in self.capabilities.items()}
            for cdict in caps.values():
                cdict["verification"] = "unverified"
            probe = Probe(
                adapter=self.id,
                version=None,
                status="unverified",
                launch_enabled=self.launch_enabled,
                launch_milestone=self.launch_milestone,
                capabilities=caps,
                flags=[],
                models=None,
                binary_path=bin_path,
                details={"error": "unparseable_version", "output": v_out},
            )
            record_adapter_probe(conn, self.id, probe, bin_path)
            return probe

        # 2. Help check: droid --help
        res_h = run_readonly_cmd([bin_path, "--help"], timeout_s=timeout_s)
        if res_h.returncode != 0:
            caps = {name: c.to_dict() for name, c in self.capabilities.items()}
            probe = Probe(
                adapter=self.id,
                version=v,
                status="error",
                launch_enabled=self.launch_enabled,
                launch_milestone=self.launch_milestone,
                capabilities=caps,
                flags=[],
                models=None,
                binary_path=bin_path,
                details={"error": "help_check_failed", "exit_code": res_h.returncode, "stderr": res_h.stderr},
            )
            record_adapter_probe(conn, self.id, probe, bin_path)
            return probe

        # 3. Exec help check & models: droid exec --help
        res_eh = run_readonly_cmd([bin_path, "exec", "--help"], timeout_s=timeout_s)
        models = self.parse_models_from_help(res_eh.help_text) if res_eh.returncode == 0 else None

        # Compare help sha256 with plan §14.11
        expected_help = EXPECTED_HELP_HASHES.get("droid")
        expected_exec_help = EXPECTED_HELP_HASHES.get("droid_exec")
        is_stale = (
            (self.verified_version and v != self.verified_version)
            or (expected_help and res_h.help_sha256 != expected_help)
            or (expected_exec_help and res_eh.help_sha256 != expected_exec_help)
        )

        caps = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if is_stale and cdict["verification"] != "none":
                cdict["verification"] = "stale"
            caps[name] = cdict

        flags = [
            "--output-format",
            "--cwd",
            "--model",
            "--reasoning-effort",
            "--auto",
            "--skip-permissions-unsafe",
            "--session-id",
            "--log-group-id",
            "--file",
        ]

        flag_diff = None
        if expected_exec_help and res_eh.help_sha256 != expected_exec_help:
            missing = find_missing_flags(flags, res_eh.help_text)
            flag_diff = {"match": False, "missing_flags": missing}

        probe = Probe(
            adapter=self.id,
            version=v,
            status="partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=flags,
            models=models,
            binary_path=bin_path,
            help_sha256=res_h.help_sha256,
            flag_diff=flag_diff,
        )
        record_adapter_probe(conn, self.id, probe, bin_path)
        return probe


def _scan_stream(path: str) -> Dict[str, Any]:
    """One pass over a droid stream-json file; never raises (m3-r6 design §3).

    ``session_id``: the first ``system/init`` event's non-empty string ``session_id``.
    ``final_text``/``usage``: from the last ``completion`` event (``finalText`` if a string,
    a shallow copy of ``usage`` if a dict, else None each). ``malformed``: non-blank lines
    that are not valid JSON. Non-object JSON and other event types are ignored.
    """
    scan: Dict[str, Any] = {"session_id": None, "final_text": None, "usage": None, "malformed": 0}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    scan["malformed"] += 1
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if scan["session_id"] is None and isinstance(sid, str) and sid:
                        scan["session_id"] = sid
                elif etype == "completion":
                    text = event.get("finalText")
                    usage = event.get("usage")
                    scan["final_text"] = text if isinstance(text, str) else None
                    scan["usage"] = dict(usage) if isinstance(usage, dict) else None
    except OSError:
        return {"session_id": None, "final_text": None, "usage": None, "malformed": 0}
    return scan
