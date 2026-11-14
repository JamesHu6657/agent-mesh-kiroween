"""Antigravity (agy) CLI adapter per plan §14.4."""

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters.base import positional_prompt
from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    EffortNotAllowed,
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


EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max")
AGY_ERROR_MARKER = "AGY_ERROR:"
# Token counters of agy 1.5.3 result.usage and step_update.usage
_USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
# Text-bearing keys tried on agent_response steps (none present in 1.5.3; forward-compatible)
_STEP_TEXT_KEYS = ("text", "response", "content", "message")


class AgyAdapter(Adapter):
    """Adapter for Google Antigravity (agy) CLI."""

    id = "agy"
    kind = "vendor"
    binary_names = ["agy"]
    planned_version = "1.5.2"
    verified_version = "1.5.2"
    liveness = "stream"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = "M3"

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="help", note="agy --print"),
            "stream_output": Capability("stream_output", supported="yes", verification="help", note="--output-format stream-json"),
            "model_selection": Capability("model_selection", supported="yes", verification="help", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="help", note="--effort"),
            "resume": Capability("resume", supported="unknown", verification="help", note="--conversation"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        if profile == "readonly":
            return ["--mode", "plan"]
        elif profile == "edit":
            return ["--mode", "accept-edits"]
        elif profile == "dev":
            # Unsupported until smoke S-AGY-8
            raise ProfileUnsupported(self.id, profile)
        elif profile == "elevated":
            raise ProfileUnsupported(self.id, profile)
        elif profile == "unrestricted":
            return ["--dangerously-skip-permissions"]
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        # Check model effort suffix conflict per §14.4, §9.1
        if req.model and req.effort:
            for suffix in EFFORT_SUFFIXES:
                if req.model.endswith(suffix):
                    raise EffortNotAllowed(
                        f"Model '{req.model}' already encodes reasoning effort; cannot specify effort '{req.effort}'",
                        details={"reason": "model_encodes_effort", "model": req.model, "effort": req.effort},
                    )

        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            # Linear full review C9: run the path validation resolved (~, relative), not the raw string.
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [
            binary,
            "--print",
            "--output-format",
            "stream-json",
            "--model",
            req.model or "gemini-3.8-flash-high",
        ]
        if req.effort:
            argv.extend(["--effort", req.effort])

        argv.extend(self.profile_flags(req.profile))

        if req.options.get("sandbox"):
            argv.append("--sandbox")
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
            run_dir = os.path.dirname(os.path.abspath(req.prompt_path))
            argv.extend(["--add-dir", run_dir])
            prompt_content = f"Read the file {req.prompt_path} and follow the instructions in it exactly. It is your complete task."

        if req.resume_session_id:
            argv.extend(["--conversation", req.resume_session_id])

        # m4-p3-cleanup (Carbon r10 P3): the prompt is the last, positional argv item. Neither
        # CLI's real help fixture documents `--`, so a prompt that starts with "-" gets a
        # leading newline instead; the CLI then reads it as INPUT, never as a flag.
        prompt_content = positional_prompt(prompt_content)
        argv.extend(["--print-timeout", f"{req.timeout_s}s", prompt_content])

        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        # agy 1.5.3 `--print --output-format stream-json`, one JSON object per line
        # (tests/fixtures/real/agy/1.5.3/):
        #   stream-step1-denied.jsonl:1  {"event":"init","conversation_id":...,"init":{"model":...}}
        #                                                               -> session_id, usage.model
        #   stream-step1-denied.jsonl:3  {"event":"step_update","step_update":{"step_type":
        #                                 "agent_response","usage":{...}}}  -> fallback usage only;
        #                                 1.5.3 puts no model text on agent_response steps
        #   stream-fixC-denied.jsonl:14  step_type "tool", state "ERROR", tool_info.error -> not used
        #   stream-step1-denied.jsonl:last {"event":"result","result":{"status":"SUCCESS",
        #                                 "response":...,"usage":{...},"duration_seconds":...,
        #                                 "num_turns":...,"denied_actions":[...]}}
        #                                                               -> final_text, usage
        #   stream-step6-network-error.jsonl:last  result status "ERROR", "error":...
        #                                 "finished_reason":"error"    -> vendor_exit_meaning
        # final_text is result.response when that string is non-empty, else the text of the
        # last agent_response step. If that last step's text is empty (or it has no string
        # under _STEP_TEXT_KEYS), final_text is "" — no fallback to an older non-empty step
        # (same rule as vibe). 1.5.3 agent_response steps carry no text, so this is "". The
        # supervisor then falls back to the raw stdout.log. Tool output is
        # never used. Any run without a result event (missing/empty/garbage-only stdout, or a
        # stream cut before the result) takes the same no-result path: run_error, the reason in
        # usage["error"], and stderr.log is still parsed (usage["stderr_error"]). Never raises.
        scan = _scan_stream(os.path.join(run_dir, "stdout.log"))
        result = scan["result"]
        usage: Dict[str, Any] = {}
        if result is not None and isinstance(result.get("usage"), dict):
            usage["source"] = "result"
            res_usage = result["usage"]
            for key in _USAGE_KEYS:
                # A key missing (or not an int) in result.usage takes the step sum, 0 without steps
                val = res_usage.get(key)
                usage[key] = val if _is_count(val) else scan["step_usage"][key]
        elif scan["seen"]:
            usage["source"] = "steps"
            usage.update(scan["step_usage"])
        else:
            # No event read at all: token counters are 0, never missing or None
            usage["source"] = "none"
            usage.update(scan["step_usage"])
        usage["model"] = scan["model"]
        usage["duration_seconds"] = result.get("duration_seconds") if result is not None else None
        usage["num_turns"] = result.get("num_turns") if result is not None else None
        status = result.get("status") if result is not None else None
        usage["status"] = status if isinstance(status, str) else None
        if result is not None and isinstance(result.get("denied_actions"), list):
            usage["denied_actions"] = len(result["denied_actions"])
        if scan["malformed"]:
            usage["malformed_lines"] = scan["malformed"]

        vendor_exit_meaning = None
        if result is None:
            vendor_exit_meaning = "run_error"
            if scan["missing"]:
                usage["error"] = "missing stdout"
            elif scan["empty"]:
                usage["error"] = "empty stdout"
            elif not scan["seen"]:
                usage["error"] = "no result event (no parseable events)"
            else:
                usage["error"] = "no result event"
        elif usage["status"] != "SUCCESS" or result.get("error"):
            vendor_exit_meaning = "run_error"
            err = result.get("error")
            usage["error"] = err if isinstance(err, str) and err else (
                f"status {usage['status']}" if usage["status"] else "result without status")
        if vendor_exit_meaning:
            stderr_err = self.parse_stderr_error(_read_text(os.path.join(run_dir, "stderr.log")))
            if stderr_err is not None:
                usage["stderr_error"] = stderr_err

        session_id = scan["session_id"]
        if not session_id and result is not None:
            sid = result.get("conversation_id")
            session_id = sid if isinstance(sid, str) and sid else None

        final_text = ""
        if result is not None and isinstance(result.get("response"), str) and result["response"]:
            final_text = result["response"]
        elif scan["agent_text"]:
            final_text = scan["agent_text"]
        return RunResult(
            final_text=final_text,
            session_id=session_id,
            usage=usage,
            vendor_exit_meaning=vendor_exit_meaning,
        )

    @staticmethod
    def stream_facts(stdout_text: str) -> Dict[str, Any]:
        """Smoke-facing facts from an agy 1.5.3 stream-json text; never raises.

        ``session_id``: the first ``init`` event's ``conversation_id`` (falling back to the first
        ``conversation_id`` on a step_update or result event); ``model`` / ``cwd``: the first
        ``init.model`` / ``init.cwd``. ``effort``: an ``init.effort`` / ``init.reasoning_effort``
        string when present; agy 1.5.3 prints none (the effort is part of the model id, e.g.
        ``gemini-3.8-flash-high``), so it is None for every real capture.

        ``stream_error`` (the agy counterpart of kiro's runError-only ``stream_error``, used by
        S-X-6): ``"run_error"`` when the LAST ``result`` event reports an explicit error, i.e.
        ``status`` is not ``"SUCCESS"`` or ``error`` is non-empty (stream-step6-network-error.jsonl:
        status ERROR, finished_reason error); else None. A missing result event is NOT a
        stream error (expected after a SIGTERM). ``vendor_exit_meaning`` (used by S-X-2/4/5 at
        rc 0): the same rule as ``parse()`` on the stream alone: ``stream_error``, or
        ``"run_error"`` when the stream has no result event at all."""
        facts: Dict[str, Any] = {"session_id": None, "model": None, "effort": None, "cwd": None,
                                 "stream_error": None, "vendor_exit_meaning": None}
        fallback_sid: Optional[str] = None
        last_result: Optional[Dict[str, Any]] = None
        saw_result = False
        for line in (stdout_text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("event")
            if etype == "init":
                sid = event.get("conversation_id")
                if facts["session_id"] is None and isinstance(sid, str) and sid:
                    facts["session_id"] = sid
                init = event.get("init")
                if not isinstance(init, dict):
                    continue
                for key, names in (("model", ("model",)), ("cwd", ("cwd",)),
                                   ("effort", ("effort", "reasoning_effort"))):
                    if facts[key] is not None:
                        continue
                    for name in names:
                        val = init.get(name)
                        if isinstance(val, str) and val:
                            facts[key] = val
                            break
            elif etype in ("step_update", "result"):
                body = event.get(etype)
                if etype == "result":
                    saw_result = True
                    last_result = body if isinstance(body, dict) else {}
                sid = body.get("conversation_id") if isinstance(body, dict) else None
                if fallback_sid is None and isinstance(sid, str) and sid:
                    fallback_sid = sid
        if facts["session_id"] is None:
            facts["session_id"] = fallback_sid
        if last_result is not None and (last_result.get("status") != "SUCCESS" or last_result.get("error")):
            facts["stream_error"] = "run_error"
        facts["vendor_exit_meaning"] = facts["stream_error"] or (None if saw_result else "run_error")
        return facts

    @staticmethod
    def parse_stderr_error(stderr_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """The last ``AGY_ERROR: {json}`` line in agy stderr, or None; never raises.

        stream-step6-network-error-stderr.txt: a plain ``error: ...`` line, then
        ``AGY_ERROR: {"short_error":...,"status":...,"error_code":...,"code_kind":...,
        "retryable":...,"error_id":...}``. Returns ``short_error`` (str or None) and
        ``retryable`` (bool or None), plus ``status``/``error_code``/``code_kind`` when present.
        """
        if not isinstance(stderr_text, str) or AGY_ERROR_MARKER not in stderr_text:
            return None
        found: Optional[Dict[str, Any]] = None
        for line in stderr_text.splitlines():
            line = line.strip()
            if not line.startswith(AGY_ERROR_MARKER):
                continue
            try:
                data = json.loads(line[len(AGY_ERROR_MARKER):].strip())
            except (ValueError, RecursionError):
                continue
            if not isinstance(data, dict):
                continue
            short = data.get("short_error")
            retryable = data.get("retryable")
            out: Dict[str, Any] = {
                "short_error": short if isinstance(short, str) else None,
                "retryable": retryable if isinstance(retryable, bool) else None,
            }
            for key in ("status", "error_code", "code_kind"):
                if key in data:
                    out[key] = data[key]
            found = out
        return found

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        bin_path = self.resolve_binary(binary_path)
        if not bin_path:
            return None
        rc, out, _, _ = run_readonly_cmd([bin_path, "models"])
        if rc == 0:
            return self.parse_models_from_output(out)
        return None

    @staticmethod
    def parse_models_from_output(output_text: str) -> List[str]:
        models: List[str] = []
        for line in output_text.splitlines():
            line = line.strip()
            if not line or line.startswith("Fetching available models"):
                continue
            parts = line.split("\t")
            if parts and parts[0]:
                models.append(parts[0].strip())
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

        # 1. Version check: agy --version
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

        # 2. Help check: agy --help
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

        # 3. Model list: agy models
        res_m = run_readonly_cmd([bin_path, "models"], timeout_s=timeout_s)
        models = self.parse_models_from_output(res_m.stdout) if res_m.returncode == 0 else None

        expected_help = EXPECTED_HELP_HASHES.get("agy")
        is_stale = (
            (self.verified_version and v != self.verified_version)
            or (expected_help and res_h.help_sha256 != expected_help)
        )

        caps = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if is_stale and cdict["verification"] != "none":
                cdict["verification"] = "stale"
            caps[name] = cdict

        flags = [
            "--print",
            "--output-format",
            "--model",
            "--effort",
            "--mode",
            "--dangerously-skip-permissions",
            "--sandbox",
            "--agent",
            "--add-dir",
            "--conversation",
            "--print-timeout",
        ]

        flag_diff = None
        if expected_help and res_h.help_sha256 != expected_help:
            missing = find_missing_flags(flags, res_h.help_text)
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


def _is_count(value: Any) -> bool:
    """True for an int token counter (bool excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _scan_stream(path: str) -> Dict[str, Any]:
    """One pass over an agy stream-json file; never raises.

    ``seen``: at least one JSON object event was read. ``session_id``/``model``/``cwd``: the
    FIRST ``init`` event's ``conversation_id`` / ``init.model`` / ``init.cwd``, all from that
    one event; later init events are ignored even when the first lacks a field (None then;
    parse() falls back to result.conversation_id for the session id only). ``saw_init``: an
    init event was read. ``cwd`` is scan-only (not put in usage). ``result``: the last ``result``
    event's dict. ``step_usage``: token counters summed over ``agent_response`` steps (used
    without a result usage, and per key when result.usage lacks that key; equal to
    result.usage in all three 1.5.3 fixtures). Lines without an ``event`` key are counted
    as seen but otherwise ignored.
    ``agent_text``: the text of the last ``agent_response`` step, taking a string under
    one of ``_STEP_TEXT_KEYS`` (including an empty string). If that last step has no such
    string or the string is empty, ``agent_text`` is ``""`` and an older non-empty step is
    not kept (same rule as vibe). ``malformed``: non-blank lines that are not valid JSON
    (for example a truncated last line). ``missing``: the file could not be opened/read.
    ``empty``: no non-blank line was read.
    """
    scan: Dict[str, Any] = {
        "seen": False, "saw_init": False, "session_id": None, "model": None, "cwd": None,
        "result": None,
        "step_usage": {k: 0 for k in _USAGE_KEYS}, "agent_text": "", "malformed": 0,
        "missing": False, "empty": True,
    }
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                scan["empty"] = False
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    scan["malformed"] += 1
                    continue
                if not isinstance(event, dict):
                    continue
                scan["seen"] = True
                etype = event.get("event")
                if etype == "init":
                    # First init EVENT wins as a whole: later init events are ignored even
                    # when the first one lacks a field (no mixing fields across inits).
                    if scan["saw_init"]:
                        continue
                    scan["saw_init"] = True
                    sid = event.get("conversation_id")
                    if isinstance(sid, str) and sid:
                        scan["session_id"] = sid
                    init = event.get("init")
                    if isinstance(init, dict):
                        if isinstance(init.get("model"), str):
                            scan["model"] = init["model"]
                        if isinstance(init.get("cwd"), str):
                            scan["cwd"] = init["cwd"]
                elif etype == "step_update":
                    step = event.get("step_update")
                    if not isinstance(step, dict) or step.get("step_type") != "agent_response":
                        continue
                    step_usage = step.get("usage")
                    if isinstance(step_usage, dict):
                        for key in _USAGE_KEYS:
                            val = step_usage.get(key)
                            if _is_count(val):
                                scan["step_usage"][key] += val
                    text_found = ""
                    for key in _STEP_TEXT_KEYS:
                        text = step.get(key)
                        if isinstance(text, str):
                            text_found = text
                            break
                    scan["agent_text"] = text_found
                elif etype == "result":
                    res = event.get("result")
                    if isinstance(res, dict):
                        scan["result"] = res
    except OSError:
        scan["seen"] = False
        scan["missing"] = True
    return scan
