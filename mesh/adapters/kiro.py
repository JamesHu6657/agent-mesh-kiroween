"""Kiro CLI adapter per plan §14.5."""

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters.base import positional_prompt
from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    EXPECTED_HELP_HASHES,
    Probe,
    ProfileUnsupported,
    PromptTooLong,
    RunRequest,
    RunResult,
    find_missing_flags,
    parse_version_output,
    record_adapter_probe,
    run_readonly_cmd,
)


class KiroAdapter(Adapter):
    """Adapter for AWS Kiro CLI (kiro-cli)."""

    id = "kiro"
    kind = "vendor"
    binary_names = ["kiro-cli"]
    planned_version = "3.1.0"
    verified_version = "3.1.0"
    liveness = "stream"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = "M3"

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="help", note="--no-interactive"),
            "stream_output": Capability("stream_output", supported="yes", verification="help", note="--output-format stream-json"),
            "model_selection": Capability("model_selection", supported="yes", verification="help", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="help", note="effort-high-run-metadata.jsonl"),
            "resume": Capability("resume", supported="unknown", verification="help", note="--resume-id"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        if profile == "readonly":
            return ["--trust-tools="]
        elif profile == "edit":
            return ["--trust-tools=fs_read,fs_write"]
        elif profile == "dev":
            # Unsupported until the shell tool name is verified
            raise ProfileUnsupported(self.id, profile)
        elif profile == "elevated":
            # Unsupported (the only wider flag is trust-all)
            raise ProfileUnsupported(self.id, profile)
        elif profile == "unrestricted":
            return ["--trust-all-tools"]
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            # Linear full review C9: run the path validation resolved (~, relative), not the raw string.
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [
            binary,
            "chat",
            "--no-interactive",
            "--output-format",
            "stream-json",
            "--model",
            req.model or "claude-opus-5.5",
        ]
        if req.effort:
            argv.extend(["--effort", req.effort])

        argv.extend(self.profile_flags(req.profile))

        if req.resume_session_id:
            argv.extend(["--resume-id", req.resume_session_id])

        if req.options.get("agent"):
            argv.extend(["--agent", req.options["agent"]])

        # Prompt length check and fallback per §12.5, N16
        prompt_content = ""
        prompt_bytes = 0
        if os.path.exists(req.prompt_path):
            prompt_bytes = os.path.getsize(req.prompt_path)
            if prompt_bytes <= self.max_argv_prompt_bytes:
                with open(req.prompt_path, "r", encoding="utf-8") as f:
                    prompt_content = f.read()

        if prompt_bytes > self.max_argv_prompt_bytes:
            if req.profile == "readonly":
                raise PromptTooLong(
                    bytes_len=prompt_bytes,
                    limit=self.max_argv_prompt_bytes,
                    adapter=self.id,
                    profile=req.profile,
                )
            prompt_content = f"Read the file {req.prompt_path} and follow the instructions in it exactly. It is your complete task."

        # m4-p3-cleanup (Carbon r10 P3): the prompt is the last, positional argv item. Neither
        # CLI's real help fixture documents `--`, so a prompt that starts with "-" gets a
        # leading newline instead; the CLI then reads it as INPUT, never as a flag.
        prompt_content = positional_prompt(prompt_content)
        argv.append(prompt_content)

        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        # kiro-cli 3.1.0 `chat --no-interactive --output-format stream-json`, one JSON object per
        # line (tests/fixtures/real/kiro/3.1.0/):
        #   stream-effort-high-ok.jsonl:1  {"type":"runStarted","data":{"payloadSchema":"acp",...}}
        #   stream-effort-high-ok.jsonl:2  {"type":"metadata","data":{"sessionId":...}}       -> session_id
        #   stream-effort-high-ok.jsonl:4  {"type":"sessionUpdate","data":{"update":{"sessionUpdate":
        #                                   "agent_message_chunk","content":{"type":"text",...}}}}
        #                                                                                      -> fallback text
        #   stream-effort-high-ok.jsonl:5  {"type":"metadata","data":{"meteringUsage":[...],
        #                                   "turnDurationMs":...}}                             -> usage
        #   stream-effort-high-ok.jsonl:6  {"type":"runFinished","data":{"finalText":...,"status":...,
        #                                   "stopReason":...}}                                  -> final_text
        #   stream-shell-denied-refusal.jsonl  metadata stopReason "CONTENT_FILTERED" + refusal.category
        #                                                                     -> vendor_exit_meaning
        #   stream-run-error-auth.jsonl:last   {"type":"runError","data":{"stage":...,"message":...}}
        #                                      (no runFinished)                -> vendor_exit_meaning
        # Without runFinished.finalText, final_text is the agent_message_chunk texts (one "\n"
        # between messages split by tool calls, which is how 3.1.0 builds finalText); without
        # either it is "" and the supervisor falls back to the raw stdout.log.
        scan = _scan_stream(os.path.join(run_dir, "stdout.log"))
        if not scan["session_id"]:
            # 3.1.0 effort fixture name (help-tier capability evidence); session id only
            alt = _scan_stream(os.path.join(run_dir, "effort-high-run-metadata.jsonl"))
            scan["session_id"] = alt["session_id"]
        final_text = scan["final_text"] if scan["final_text"] else scan["message_text"]
        # Linear full review C6: a finalText that is only a truncated prefix of the streamed
        # message text loses the end of the answer; keep the complete stream instead.
        streamed = scan["message_text"] or ""
        if final_text and len(streamed) > len(final_text) and streamed.startswith(final_text.rstrip().rstrip(".…")):
            final_text = streamed
        return RunResult(
            final_text=final_text or "",
            session_id=scan["session_id"],
            usage=scan["usage"],
            vendor_exit_meaning=scan["vendor_exit_meaning"],
        )

    @staticmethod
    def stream_facts(stdout_text: str) -> Dict[str, Any]:
        """Smoke-facing facts from a kiro stream: session id, model, effort, run status.

        ``model`` is the metadata ``modelId`` when the stream carries one (3.1.0 rarely does);
        ``effort`` is the last metadata ``reasoning.effort``."""
        scan = _scan_lines(stdout_text.splitlines())
        return {
            "session_id": scan["session_id"],
            "model": scan["model"],
            "effort": scan["effort"],
            "run_started": scan["run_started"],
            "status": scan["status"],
            "stop_reason": scan["stop_reason"],
            "vendor_exit_meaning": scan["vendor_exit_meaning"],
            "stream_error": scan["stream_error"],
            "run_finished": scan["run_finished"],
            "error_message": scan["error_message"],
        }

    @staticmethod
    def parse_stderr_error(stderr_text: str) -> Optional[Dict[str, Any]]:
        """Maps known kiro stderr error signatures per §14.5 and N6."""
        if "failed to set effort" in stderr_text and "model does not support additional fields" in stderr_text:
            return {
                "reason": "model_or_effort_rejected",
                "details": {
                    "stderr": stderr_text.strip(),
                    "note": "README observation: this message was seen with an invalid model id, so check the model id first",
                },
            }
        if DENIED_MARKER in stderr_text:
            # stream-shell-denied-refusal-stderr.txt: an untrusted tool in --no-interactive is
            # denied (not prompted); the run itself goes on and can still end with status success.
            return {
                "reason": "tool_denied",
                "details": {"stderr": stderr_text.strip(), "count": stderr_text.count(DENIED_MARKER)},
            }
        return None

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        bin_path = self.resolve_binary(binary_path)
        if not bin_path:
            return None
        res = run_readonly_cmd([bin_path, "chat", "--list-models", "-f", "json"])
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
                return self.parse_models_from_json(data)
            except Exception:
                return None
        return None

    @staticmethod
    def parse_models_from_json(data: Dict[str, Any]) -> List[str]:
        models: List[str] = []
        for item in data.get("models", []):
            mid = item.get("model_id")
            if mid:
                models.append(mid)
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

        # 1. Version check: kiro-cli --version
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

        # 2. Help check: kiro-cli --help
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

        # 3. Chat help check: kiro-cli chat --help
        res_ch = run_readonly_cmd([bin_path, "chat", "--help"], timeout_s=timeout_s)

        # 4. Model list: kiro-cli chat --list-models -f json
        res_m = run_readonly_cmd([bin_path, "chat", "--list-models", "-f", "json"], timeout_s=timeout_s)
        models = None
        if res_m.returncode == 0:
            try:
                models = self.parse_models_from_json(json.loads(res_m.stdout))
            except Exception:
                models = None

        expected_help = EXPECTED_HELP_HASHES.get("kiro")
        expected_chat_help = EXPECTED_HELP_HASHES.get("kiro_chat")
        is_stale = (
            (self.verified_version and v != self.verified_version)
            or (expected_help and res_h.help_sha256 != expected_help)
            or (expected_chat_help and res_ch.help_sha256 != expected_chat_help)
        )

        caps = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if is_stale and cdict["verification"] != "none":
                cdict["verification"] = "stale"
            caps[name] = cdict

        flags = [
            "--no-interactive",
            "--output-format",
            "--model",
            "--effort",
            "--trust-tools=",
            "--trust-all-tools",
            "--resume-id",
            "--agent",
        ]

        flag_diff = None
        if expected_chat_help and res_ch.help_sha256 != expected_chat_help:
            missing = find_missing_flags(flags, res_ch.help_text)
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


DENIED_MARKER = "[denied] tool permission approval is not supported in non-interactive mode"
AUTH_MARKERS = ("bearer token", "not logged in", "unauthorized", "expired token", "token is expired")


def _scan_lines(lines) -> Dict[str, Any]:
    """One pass over kiro 3.1.0 stream-json lines. Malformed lines are skipped and counted."""
    out: Dict[str, Any] = {
        "session_id": None, "final_text": None, "message_text": "", "status": None, "stop_reason": None,
        "final_text_truncated": None, "model": None, "effort": None, "run_started": False,
        "error_stage": None, "error_message": None, "refusal": None, "content_filtered": False,
        "credits": None, "turn_duration_ms": None, "metadata_turns": 0, "context_pct": None,
        "malformed_lines": 0, "run_error_seen": False, "run_finished": False,
    }
    parts: List[str] = []
    last_was_chunk = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            out["malformed_lines"] += 1
            continue
        if not isinstance(event, dict):
            out["malformed_lines"] += 1
            continue
        etype = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        sid = data.get("sessionId")
        if isinstance(sid, str) and sid and not out["session_id"]:
            out["session_id"] = sid
        if etype == "runStarted":
            out["run_started"] = True
        elif etype == "metadata":
            if isinstance(data.get("modelId"), str) and data["modelId"]:
                out["model"] = data["modelId"]
            reasoning = data.get("reasoning")
            if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
                out["effort"] = reasoning["effort"]
            if isinstance(data.get("contextUsagePercentage"), (int, float)):
                out["context_pct"] = data["contextUsagePercentage"]
            if data.get("stopReason") == "CONTENT_FILTERED":
                out["content_filtered"] = True
                refusal = data.get("refusal")
                if isinstance(refusal, dict) and isinstance(refusal.get("category"), str):
                    out["refusal"] = refusal["category"]
            metering = data.get("meteringUsage")
            if isinstance(metering, list):
                total = 0.0
                for m in metering:
                    if isinstance(m, dict) and m.get("unit") == "credit" and isinstance(m.get("value"), (int, float)):
                        total += float(m["value"])
                out["credits"] = (out["credits"] or 0.0) + total
            if isinstance(data.get("turnDurationMs"), (int, float)):
                out["turn_duration_ms"] = (out["turn_duration_ms"] or 0) + int(data["turnDurationMs"])
                out["metadata_turns"] += 1
        elif etype == "sessionUpdate":
            update = data.get("update") if isinstance(data.get("update"), dict) else {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                    if parts and not last_was_chunk and not parts[-1].endswith("\n"):
                        parts.append("\n")
                    parts.append(content["text"])
                    last_was_chunk = True
                    continue
            elif update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                last_was_chunk = False
        elif etype == "runFinished":
            out["run_finished"] = True
            if isinstance(data.get("finalText"), str):
                out["final_text"] = data["finalText"]
            out["status"] = data.get("status") if isinstance(data.get("status"), str) else None
            out["stop_reason"] = data.get("stopReason") if isinstance(data.get("stopReason"), str) else None
            if isinstance(data.get("finalTextTruncated"), bool):
                out["final_text_truncated"] = data["finalTextTruncated"]
        elif etype == "runError":
            # any runError counts, whatever its data holds (null/missing stage, message, data)
            out["run_error_seen"] = True
            out["error_stage"] = data.get("stage") if isinstance(data.get("stage"), str) else None
            out["error_message"] = data.get("message") if isinstance(data.get("message"), str) else None
    out["message_text"] = "".join(parts)
    # PR #12 fix round 2 (docs/impl-notes-m3-kiro-real.md): success needs completion evidence.
    #   any runError event (even after runFinished success)  -> auth_failed if the message names
    #                                                            the bearer token/login, else run_error
    #   no runError, refusal without a successful finish      -> content_filtered
    #   no runError, no runFinished with status success       -> run_error
    #     (truncated/killed/empty stream, or runFinished with another or no status)
    #   otherwise                                             -> None
    # ``stream_error`` is the runError-only part (explicit error in the stream), used by S-X-6.
    if out["run_error_seen"]:
        msg = (out["error_message"] or "").lower()
        out["stream_error"] = "auth_failed" if any(k in msg for k in AUTH_MARKERS) else "run_error"
    else:
        out["stream_error"] = None
    if out["stream_error"]:
        out["vendor_exit_meaning"] = out["stream_error"]
    elif out["content_filtered"] and out["status"] != "success":
        out["vendor_exit_meaning"] = "content_filtered"
    elif out["status"] != "success":
        out["vendor_exit_meaning"] = "run_error"
    else:
        out["vendor_exit_meaning"] = None
    usage: Dict[str, Any] = {}
    if out["credits"] is not None:
        usage["kiro_credits"] = round(out["credits"], 6)
    if out["turn_duration_ms"] is not None:
        usage["turn_duration_ms"] = out["turn_duration_ms"]
        usage["turns"] = out["metadata_turns"]
    if out["context_pct"] is not None:
        usage["context_usage_pct"] = out["context_pct"]
    if out["model"]:
        usage["model"] = out["model"]
    if out["effort"]:
        usage["reasoning_effort"] = out["effort"]
    if out["status"]:
        usage["status"] = out["status"]
    if out["stop_reason"]:
        usage["stop_reason"] = out["stop_reason"]
    if out["content_filtered"]:
        usage["content_filtered"] = True
        if out["refusal"]:
            usage["refusal_category"] = out["refusal"]
    if out["final_text_truncated"]:
        usage["final_text_truncated"] = True
    out["usage"] = usage or None
    return out


def _scan_stream(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _scan_lines(f)
    except OSError:
        return _scan_lines([])
