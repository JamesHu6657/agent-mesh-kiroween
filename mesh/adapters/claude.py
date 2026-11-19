"""Claude Code adapter (``claude -p --output-format stream-json --verbose``).

Written against Claude Code **2.1.325** (top entry of anthropics/claude-code CHANGELOG.md on
2026-11-16; npm ``@anthropic-ai/claude-code`` ``latest`` still pointed at 2.1.260). No real
``--help`` or stream fixture is committed yet, so flags and events come from the official
sources below (checked 2026-11-16), not from memory; ``probe()`` re-checks the emitted flags
against the installed binary's live help (``claude --help`` does not list every flag, so a
missing one is a warning, not a failure):

* https://code.claude.com/docs/en/cli-reference: ``-p/--print``, ``--output-format``,
  ``--verbose``, ``--model``, ``--effort low|medium|high|xhigh|max|ultracode``,
  ``--permission-mode``, ``--allowedTools``, ``--resume``; ``claude auth status`` prints JSON
  and exits 0 when logged in, 1 when not; its ``authMethod`` is one of ``none``,
  ``claude.ai``, ``oauth_token``, ``api_key``, ``api_key_helper``, ``third_party``.
* https://code.claude.com/docs/en/permission-modes: ``dontAsk`` allows file reads in the
  working directories and pre-approved tools and denies anything that would prompt;
  ``bypassPermissions`` skips every check (used for ``unrestricted`` only).
* https://code.claude.com/docs/en/headless: ``-p`` reads stdin; exit 0 on success, non-zero
  on failure; a failure inside the run (e.g. missing authentication) is printed as the
  result on stdout; SIGTERM -> exit 143 and the running Bash command tree is terminated;
  ``system/api_retry`` events (``error_status`` null = no HTTP response; ``error`` category).
* Agent SDK TypeScript reference (code.claude.com/docs/en/agent-sdk/typescript):
  ``SDKResultMessage`` (``subtype`` success | error_during_execution | error_max_turns |
  error_max_budget_usd, ``is_error``, ``result``, ``permission_denials``, ``errors``),
  ``SDKAssistantMessage.error`` (authentication_failed | billing_error | rate_limit |
  invalid_request | server_error | unknown | max_output_tokens).

Stream, one JSON object per line::

    {"type":"system","subtype":"init","session_id":..,"model":..,"permissionMode":..,
     "apiKeySource":..}                                                  -> session_id, usage
    {"type":"assistant","message":{"content":[{"type":"text","text":..}]},
     "parent_tool_use_id":null,"error":"authentication_failed"?}         -> fallback text
    {"type":"system","subtype":"api_retry","error_status":null,"error":"unknown",..}
    {"type":"result","subtype":"success","is_error":false,"result":..,"total_cost_usd":..,
     "num_turns":..,"usage":{..},"permission_denials":[..]}              -> final_text

Not verified on a real machine yet: the exact not-logged-in / usage-limit result wording, a
prompt read from stdin without a positional query, ``dontAsk`` denials in ``-p`` mode.
"""

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters import vendor_stream as vs
from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    EffortNotAllowed,
    Probe,
    ProfileUnsupported,
    RunRequest,
    RunResult,
    find_missing_flags,
    is_safe_cli_token,
    parse_version_output,
    record_adapter_probe,
    run_readonly_cmd,
    validate_adapter_options,
    validate_run_request_tokens,
)
from mesh.constants import build_external_env

CLAUDE_VERSION = "2.1.325"
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultracode")
#: Tool allowlists per profile, on top of ``--permission-mode dontAsk`` (reads only).
#: Path-scoped: an Edit rule covers all built-in file-writing tools (Edit, Write, NotebookEdit);
#: "./**" is relative to the session cwd (the task workspace). A bare tool name would approve
#: writes anywhere on the host.
EDIT_TOOLS = "Edit(./**)"
#: Explicit denies per profile. A deny wins over an allow from any settings layer, so saved
#: allow rules cannot re-enable these listed tools; --strict-mcp-config loads no MCP server.
#: Other tools a user pre-approves in ~/.claude (e.g. via skills) are not covered.
READONLY_DENY = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"
#: Full review r4 P2-3: the paths themselves too (a worktree's ./.git is a file).
EDIT_DENY = "Bash,WebFetch,WebSearch,Edit(./.git),Edit(./.git/**),Edit(./.claude),Edit(./.claude/**)"
#: Only the user's own settings are loaded: project/local settings (and their hooks) come from
#: the task workspace, which the task itself controls.
SETTING_SOURCES = "user"
FLAGS = ["--print", "--output-format", "--verbose", "--model", "--effort", "--permission-mode",
         "--allowedTools", "--disallowedTools", "--setting-sources", "--strict-mcp-config", "--resume"]
CAP_FLAGS = {"headless": "--print", "stream_output": "--output-format", "model_selection": "--model",
             "reasoning_effort": "--effort", "resume": "--resume"}
AUTH_METHODS = {"api_key": "api_key", "api_key_helper": "api_key", "claude.ai": "subscription",
                "oauth_token": "subscription", "none": "none", "third_party": "third_party"}
CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: SDKAssistantMessage.error categories -> failure meaning (rate_limit is refined by text).
ASSISTANT_ERRORS = {"authentication_failed": vs.AUTH_FAILED, "oauth_org_not_allowed": vs.AUTH_FAILED,
                    "billing_error": vs.QUOTA_EXHAUSTED, "rate_limit": vs.RATE_LIMITED}
RESULT_SUBTYPES = {"error_max_turns": "turn_limit", "error_max_budget_usd": "budget_exceeded"}
INSTALL_HINT = ("claude not found on PATH: install Claude Code (https://code.claude.com/docs), sign in with "
                "`claude auth login` (subscription) or set ANTHROPIC_API_KEY, or give the agent --binary <path>")


class ClaudeAdapter(Adapter):
    """Adapter for Anthropic Claude Code (``claude``)."""

    id = "claude"
    supported_efforts = CLAUDE_EFFORTS
    kind = "vendor"
    binary_names = ["claude"]
    planned_version = CLAUDE_VERSION
    verified_version = None  # no committed help fixture; probe() checks flags live
    liveness = "stream"
    max_argv_prompt_bytes = 100_000  # the prompt goes on stdin; kept for the shared limit
    launch_enabled = False
    launch_milestone = "M3"
    #: supervisor._vendor_nonzero_failure: Claude Code prints run failures as the stdout
    #: result and exits non-zero, so a non-zero exit takes the parsed meaning as its reason.
    nonzero_exit_uses_stream_meaning = True

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="none", note="-p/--print"),
            "stream_output": Capability("stream_output", supported="yes", verification="none",
                                        note="--output-format stream-json --verbose"),
            "model_selection": Capability("model_selection", supported="yes", verification="none", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="none", note="--effort"),
            "resume": Capability("resume", supported="unknown", verification="none", note="--resume <session id>"),
        }

    def resolve_binary(self, binary_path: Optional[str] = None) -> Optional[str]:
        if binary_path:
            return super().resolve_binary(binary_path)
        return vs.resolve_on_path(self.binary_names)

    def profile_flags(self, profile: str) -> List[str]:
        # Permission prompts are never skipped below `unrestricted`: dontAsk denies whatever
        # is not on the allowlist (recorded in the result's permission_denials), and explicit
        # denies keep settings-file allow rules from widening the profile.
        if profile == "readonly":
            return ["--setting-sources", SETTING_SOURCES, "--strict-mcp-config", "--permission-mode", "dontAsk",
                    "--disallowedTools", READONLY_DENY]
        if profile == "edit":
            return ["--setting-sources", SETTING_SOURCES, "--strict-mcp-config", "--permission-mode", "dontAsk",
                    "--disallowedTools", EDIT_DENY, "--allowedTools", EDIT_TOOLS]
        if profile == "unrestricted":
            return ["--permission-mode", "bypassPermissions"]
        # dev: a bare Bash allow pre-approves every shell command with no sandbox (git push,
        # sudo, ...), wider than droid's dev; refused like kiro until a scoped rule set and
        # smoke evidence exist. elevated: the only wider modes are auto and bypassPermissions.
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        validate_run_request_tokens(req)
        validate_adapter_options(self.id, req.options, strict_keys=False)
        if req.effort is not None and req.effort not in CLAUDE_EFFORTS:
            raise EffortNotAllowed(
                f"Claude Code {CLAUDE_VERSION} documents --effort {list(CLAUDE_EFFORTS)}, not '{req.effort}'",
                details={"reason": "effort_not_supported_by_cli", "effort": req.effort,
                         "allowed": list(CLAUDE_EFFORTS)},
            )
        profile_flags = self.profile_flags(req.profile)
        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if req.model:
            argv.extend(["--model", req.model])
        if req.effort:
            argv.extend(["--effort", req.effort])
        if req.resume_session_id:
            argv.extend(["--resume", req.resume_session_id])
        # Last: --allowedTools/--disallowedTools take variadic lists, so nothing positional may follow. The
        # prompt is never in argv: claude -p reads it from stdin (prompt.md).
        argv.extend(profile_flags)
        return CommandSpec(argv=argv, cwd=req.workspace, stdin=os.path.abspath(req.prompt_path))

    def parse(self, run_dir: str) -> RunResult:
        info = vs.new_stream_info()
        scan = _scan_events(vs.iter_jsonl_file(os.path.join(run_dir, "stdout.log"), info), info)
        return RunResult(
            final_text=scan["final_text"],
            session_id=scan["session_id"],
            usage=scan["usage"],
            vendor_exit_meaning=scan["vendor_exit_meaning"],
        )

    @staticmethod
    def stream_facts(stdout_text: str) -> Dict[str, Any]:
        """Smoke-facing facts of a Claude Code stream."""
        info = vs.new_stream_info()
        scan = _scan_events(vs.iter_jsonl_lines((stdout_text or "").split("\n"), info), info)
        return {k: scan[k] for k in ("session_id", "model", "init_seen", "result_seen", "result_subtype",
                                     "vendor_exit_meaning", "permission_denials")}

    @staticmethod
    def parse_stderr_error(stderr_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Known stderr signatures: a rejected flag (reported before the run starts) or an
        auth/quota/network message. None otherwise (run failures are on stdout)."""
        text = stderr_text or ""
        if not text.strip():
            return None
        lower = text.lower()
        if ("error: unknown option" in lower or "error: option '" in lower or "error: invalid option" in lower
                or "error: invalid value" in lower or "error: missing required argument" in lower):
            return {"reason": "cli_usage_error",
                    "details": {"stderr": vs.bounded(text.strip()),
                                "note": f"claude rejected its arguments; Mesh targets Claude Code {CLAUDE_VERSION}"}}
        meaning = vs.classify_error_text(text)
        if meaning:
            return {"reason": meaning, "details": {"stderr": vs.bounded(text.strip())}}
        return None

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        # Claude Code has no read-only model-list command: the allowlist comes from the user
        # (aliases such as opus/sonnet or full model ids).
        return None

    @staticmethod
    def parse_auth_status(rc: int, stdout: str) -> Dict[str, Any]:
        """``claude auth status`` (JSON) reduced to the login method; e-mail, organisation
        and any other field are never kept."""
        raw = None
        try:
            data = json.loads(stdout or "")
            if isinstance(data, dict) and isinstance(data.get("authMethod"), str):
                raw = data["authMethod"]
        except ValueError:
            data = None
        method = AUTH_METHODS.get(raw, "unknown") if raw is not None else "unknown"
        return {"method": method, "auth_method": raw if raw in AUTH_METHODS else None,
                "logged_in": rc == 0 and method not in ("none", "unknown"), "exit_code": rc}

    def auth_status(self, bin_path: str, timeout_s: float = 5.0) -> Dict[str, Any]:
        res = run_readonly_cmd([bin_path, "auth", "status"], timeout_s=timeout_s)
        auth = self.parse_auth_status(res.returncode, res.stdout)
        env = build_external_env()
        auth["credential_env"] = [name for name in CREDENTIAL_ENV if env.get(name)]
        return auth

    def probe(
        self,
        binary_path: Optional[str] = None,
        probed_version: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        timeout_s: float = 5.0,
    ) -> Probe:
        bin_path = self.resolve_binary(binary_path)
        if not bin_path:
            return vs.error_probe(self, conn, "not_installed", None, binary_path, {"hint": INSTALL_HINT}, "none")
        rc, v_out, v_err, _ = run_readonly_cmd([bin_path, "--version"], timeout_s=timeout_s)
        if rc != 0:
            return vs.error_probe(self, conn, "error", None, bin_path,
                                  {"error": "timeout" if rc == -1 else "version_check_failed",
                                   "exit_code": rc, "stderr": vs.bounded(v_err)}, "none")
        v = probed_version or parse_version_output(v_out) or parse_version_output(v_err)
        if not v:
            return vs.error_probe(self, conn, "unverified", None, bin_path,
                                  {"error": "unparseable_version", "output": vs.bounded(v_out)}, "unverified")
        res_h = run_readonly_cmd([bin_path, "--help"], timeout_s=timeout_s)
        if res_h.returncode != 0:
            return vs.error_probe(self, conn, "error", v, bin_path,
                                  {"error": "timeout" if res_h.returncode == -1 else "help_check_failed",
                                   "exit_code": res_h.returncode, "stderr": vs.bounded(res_h.stderr)}, "none")
        missing = find_missing_flags(FLAGS, res_h.help_text)
        caps: Dict[str, Dict[str, Any]] = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if cap.supported == "yes":
                cdict["verification"] = "unverified" if CAP_FLAGS.get(name) in missing else "help"
            caps[name] = cdict
        details: Dict[str, Any] = {"auth": self.auth_status(bin_path, timeout_s), "documented_version": CLAUDE_VERSION}
        if v != CLAUDE_VERSION:
            details["version_note"] = f"Mesh's claude adapter was written against {CLAUDE_VERSION}; installed {v}"
        probe = Probe(
            adapter=self.id,
            version=v,
            status="partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=list(FLAGS),
            models=None,
            binary_path=bin_path,
            help_sha256=res_h.help_sha256,
            flag_diff={"match": False, "missing_flags": missing} if missing else None,
            details=details,
        )
        record_adapter_probe(conn, self.id, probe, bin_path)
        return probe


def _text_blocks(message: Any) -> List[str]:
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    return [b["text"] for b in message["content"]
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _scan_events(events, info: Dict[str, Any]) -> Dict[str, Any]:
    """One pass over Claude Code stream-json events (see the module docstring)."""
    out: Dict[str, Any] = {
        "session_id": None, "model": None, "permission_mode": None, "api_key_source": None,
        "init_seen": False, "result_seen": False, "result_subtype": None, "is_error": False,
        "result_text": None, "errors": [], "assistant_errors": [], "retries": 0, "retry_no_response": False,
        "denied_tools": [], "permission_denials": 0, "denied_events": 0,
    }
    parts: List[str] = []
    result_usage: Dict[str, Any] = {}
    for event in events:
        sid = event.get("session_id")
        if out["session_id"] is None and is_safe_cli_token(sid):
            out["session_id"] = sid
        etype = event.get("type")
        if etype == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                out["init_seen"] = True
                for key, src in (("model", "model"), ("permission_mode", "permissionMode"),
                                 ("api_key_source", "apiKeySource")):
                    val = event.get(src)
                    if is_safe_cli_token(val) and len(val) <= 128:
                        out[key] = val
            elif subtype == "api_retry":
                out["retries"] += 1
                # Network only if the last retry got no HTTP response and nothing clean came after.
                out["retry_no_response"] = event.get("error_status") is None
            elif subtype == "permission_denied":
                out["denied_events"] += 1
        elif etype == "assistant":
            if event.get("parent_tool_use_id") is None:  # main conversation only, not subagents
                if isinstance(event.get("error"), str):
                    out["assistant_errors"].append(event["error"])
                else:
                    # Any clean main reply (text, thinking or tool_use): earlier retries and
                    # assistant errors recovered and must not name a later failure.
                    out["retry_no_response"] = False
                    out["assistant_errors"] = []
                parts.extend(_text_blocks(event.get("message")))
        elif etype == "result":
            out["result_seen"] = True
            out["result_subtype"] = event.get("subtype") if isinstance(event.get("subtype"), str) else None
            out["is_error"] = event.get("is_error") is True
            if isinstance(event.get("result"), str):
                out["result_text"] = event["result"]
            if isinstance(event.get("errors"), list):
                out["errors"] = [e for e in event["errors"] if isinstance(e, str)]
            denials = event.get("permission_denials")
            if isinstance(denials, list):
                out["permission_denials"] = len(denials)
                out["denied_tools"] = sorted({d["tool_name"] for d in denials
                                              if isinstance(d, dict) and isinstance(d.get("tool_name"), str)})
            for key in ("total_cost_usd", "duration_ms", "num_turns"):
                val = event.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    result_usage[key] = val
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                    val = usage.get(key)
                    if isinstance(val, int) and not isinstance(val, bool):
                        result_usage[key] = val
            if isinstance(event.get("stop_reason"), str):
                result_usage["stop_reason"] = event["stop_reason"]
    # Failure rule: a result with subtype success and is_error false is a normal finish; any
    # other result is turn_limit / budget_exceeded by subtype, else the assistant error
    # category (auth, billing, rate limit refined to quota by its text), else the error text,
    # else network_error when a retry got no HTTP response, else run_error. No result at all:
    # output_truncated (runwrap log cap only) or incomplete (cut line, stopped, killed, never started).
    if out["result_seen"]:
        failed = out["is_error"] or out["result_subtype"] != "success"
        if not failed:
            meaning = None
        elif out["result_subtype"] in RESULT_SUBTYPES:
            meaning = RESULT_SUBTYPES[out["result_subtype"]]
        else:
            # errors[] are tool/run errors (e.g. a tool's own HTTP 401): kept in usage, never
            # classified as the vendor's failure. Only the last main-conversation error counts.
            by_text = vs.classify_error_text(out["result_text"])
            meaning = None
            if out["assistant_errors"]:
                meaning = ASSISTANT_ERRORS.get(out["assistant_errors"][-1])
            if meaning == vs.RATE_LIMITED and by_text == vs.QUOTA_EXHAUSTED:
                meaning = vs.QUOTA_EXHAUSTED
            meaning = (meaning or by_text or (vs.NETWORK_ERROR if out["retry_no_response"] else None)
                       or vs.RUN_ERROR)
    else:
        meaning = vs.end_of_stream_meaning(info)
    out["vendor_exit_meaning"] = meaning
    if out["result_text"]:
        out["final_text"] = out["result_text"]
    else:
        out["final_text"] = "\n\n".join(parts)
    usage_out: Dict[str, Any] = dict(result_usage)
    usage_out["status"] = "completed" if (out["result_seen"] and meaning is None) else (meaning or "completed")
    for key in ("model", "permission_mode", "api_key_source", "result_subtype"):
        if out[key]:
            usage_out[key] = out[key]
    if out["permission_denials"]:
        # Like kiro's tool_denied: a denied tool is not a failure, the run went on.
        usage_out["tool_denied"] = out["permission_denials"]
        usage_out["denied_tools"] = out["denied_tools"]
    if out["retries"]:
        usage_out["api_retries"] = out["retries"]
    if out["is_error"] and out["result_text"]:
        usage_out["error_message"] = vs.bounded(out["result_text"])
    if out["errors"]:
        usage_out["errors"] = [vs.bounded(e) for e in out["errors"][:5]]
    if info.get("malformed"):
        usage_out["malformed_lines"] = info["malformed"]
    if info.get("log_capped"):
        usage_out["log_capped"] = True
    out["usage"] = usage_out
    return out
