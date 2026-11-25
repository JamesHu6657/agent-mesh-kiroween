"""OpenAI Codex CLI adapter (``codex exec --json``).

Written against Codex CLI **0.166.0** (GitHub release ``rust-v0.166.0``, 2026-10-29). No real
``--help`` or stream fixture is committed yet, so flags and events come from the official
sources below (checked 2026-11-16), not from memory; ``probe()`` re-checks every emitted flag
against the installed binary's live help:

* https://developers.openai.com/codex/cli/reference: global flags ``--ask-for-approval``
  (``on-request|never``), ``--cd``, ``--config key=value``, ``--model``, ``--sandbox``
  (``read-only|workspace-write|danger-full-access``) and
  ``--dangerously-bypass-approvals-and-sandbox``.
* https://learn.chatgpt.com/docs/non-interactive-mode: ``codex exec --json`` turns stdout into
  JSONL; ``codex exec -`` reads the whole prompt from stdin; exec defaults to a read-only
  sandbox; ``--full-auto`` is no longer supported; ``--skip-git-repo-check``; API key via
  ``CODEX_API_KEY`` or saved ``codex login`` auth (ChatGPT login in ``$CODEX_HOME/auth.json``);
  ``codex exec resume <SESSION_ID>``.
* codex-rs/exec/src/cli.rs (main): ``--json``, ``--skip-git-repo-check``, ``resume``.
* config reference: ``model_reasoning_effort`` = ``low|medium|high|xhigh|max|ultra``
  (model dependent; ``minimal`` is no longer listed).
* openai/codex#22570: a real no-credential ``exec --json`` run (0.159.3/0.162.1): retry notices
  ``{"type":"error","message":"Reconnecting... n/5 (...401 Unauthorized...)"}``, a fatal
  ``error`` event, ``turn.failed``, exit code 1.

Event stream (``codex exec --json``), one JSON object per line::

    {"type":"thread.started","thread_id":"0199a213-..."}               -> session_id
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"agent_message","text":...}} -> final_text
    {"type":"item.completed","item":{"type":"command_execution",...}}  (counted)
    {"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,
                                      "output_tokens":..,"reasoning_output_tokens":..}}
    {"type":"turn.failed","error":{"message":...}}                       -> failure
    {"type":"error","message":"Reconnecting... 1/5 (...)"}              (retry, non-fatal)
    {"type":"error","message":...}                                       -> failure

Not verified on a real machine yet (see docs/impl-notes-codex-claude-adapters.md): a run with
ChatGPT login, the exact usage-limit wording, ``codex login status`` output wording.
"""

import os
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.adapters import vendor_stream as vs
from mesh.adapters.base import (
    Adapter,
    AdapterUnsupported,
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

CODEX_VERSION = "0.166.0"
CODEX_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
RECONNECT_PREFIX = "Reconnecting..."
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
#: Flags Mesh emits, by the help text that must list them.
TOP_FLAGS = ["--ask-for-approval"]
EXEC_FLAGS = ["--json", "--skip-git-repo-check", "--cd", "--model", "--config", "--sandbox",
              "--dangerously-bypass-approvals-and-sandbox"]
#: Capability -> the flag its live help check needs (None: exec help itself).
CAP_FLAGS = {"headless": None, "stream_output": "--json", "model_selection": "--model",
             "reasoning_effort": "--config", "resume": None}
API_KEY_ENV = ("CODEX_API_KEY", "OPENAI_API_KEY")
INSTALL_HINT = ("codex not found on PATH: install the Codex CLI (npm install -g @openai/codex), run "
                "`codex login` (ChatGPT) or set CODEX_API_KEY, or give the agent --binary <path>")


#: Project-level config Codex loads from the workspace when the repo is trusted in the user's
#: ~/.codex/config.toml (trust follows the git root, so task worktrees inherit it). MCP servers
#: and hooks declared there run as their own processes outside --sandbox.
PROJECT_LAYER_PATHS = (".codex", "mcp.json")


def project_layer_paths(workspace: str) -> List[str]:
    """Project-layer files Codex would load for ``workspace``: Codex finds the project root by
    walking up to the nearest ``.git`` and loads every ``.codex/`` from that root down to the
    cwd. Without a ``.git`` above, the cwd itself is the root. The user's own CODEX_HOME
    (``~/.codex``) is user config, not a project layer, and is skipped."""
    ws = os.path.realpath(workspace)
    chain = [ws]
    cur = ws
    root_found = os.path.lexists(os.path.join(ws, ".git"))
    while not root_found:
        parent = os.path.dirname(cur)
        if parent == cur:
            chain = [ws]  # no git root above: only the workspace itself
            break
        cur = parent
        chain.append(cur)
        root_found = os.path.lexists(os.path.join(cur, ".git"))
    codex_home = os.path.realpath(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    found = []
    for d in chain:
        for name in PROJECT_LAYER_PATHS:
            p = os.path.join(d, name)
            if os.path.lexists(p) and os.path.realpath(p) != codex_home:
                found.append(p)
    return found


def _refuse_project_layer(req: RunRequest) -> None:
    """Fail closed below unrestricted: a workspace that carries its own Codex config layer
    could start unsandboxed MCP servers/hooks from task content. Checked on every build, so a
    .codex/ written by an earlier dev run is caught on the next one."""
    if req.profile == "unrestricted":
        return
    found = [os.path.relpath(p, req.workspace) for p in project_layer_paths(req.workspace)]
    if found:
        raise AdapterUnsupported(
            f"codex_project_layer_present: codex refuses profile '{req.profile}': the workspace has a project config layer "
            f"({', '.join(found)}) whose MCP servers/hooks would run outside the sandbox",
            reason="codex_project_layer_present",
        )


class CodexAdapter(Adapter):
    """Adapter for the OpenAI Codex CLI (``codex``)."""

    id = "codex"
    supported_profiles = ("readonly", "dev", "unrestricted")
    supported_efforts = CODEX_EFFORTS
    kind = "vendor"
    binary_names = ["codex"]
    planned_version = CODEX_VERSION
    verified_version = None  # no committed help fixture; probe() checks flags live
    model_presets = {
        "chips": ["gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-luna"],
        "default_allowed": [],
        "note": "Mesh 不自动拉取 Codex 模型列表，可用模型取决于账号套餐（其中 gpt-5.6-luna 为已做冒烟测试的 ID，未必对所有套餐开放）；请点击预设芯片选择或自由输入模型 ID",
    }
    liveness = "stream"
    max_argv_prompt_bytes = 100_000  # the prompt goes on stdin; kept for the shared limit
    launch_enabled = False
    launch_milestone = "M3"
    #: supervisor._vendor_nonzero_failure: codex reports auth/quota/network failures in the
    #: stdout stream and exits 1, so a non-zero exit takes the parsed meaning as its reason.
    nonzero_exit_uses_stream_meaning = True

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="none", note="codex exec"),
            "stream_output": Capability("stream_output", supported="yes", verification="none", note="--json (JSONL)"),
            "model_selection": Capability("model_selection", supported="yes", verification="none", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="yes", verification="none",
                                           note="--config model_reasoning_effort=<effort>"),
            "resume": Capability("resume", supported="unknown", verification="none",
                                 note="codex exec resume <SESSION_ID>"),
        }

    def resolve_binary(self, binary_path: Optional[str] = None) -> Optional[str]:
        if binary_path:
            return super().resolve_binary(binary_path)
        return vs.resolve_on_path(self.binary_names)

    def profile_flags(self, profile: str) -> List[str]:
        if profile == "readonly":
            return ["--sandbox", "read-only"]
        if profile == "dev":
            # workspace-write: edits and sandboxed shell commands inside the workspace.
            return ["--sandbox", "workspace-write"]
        if profile == "unrestricted":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        # edit: codex has no edits-without-shell mode (workspace-write also runs commands);
        # elevated: no documented sandbox between workspace-write and danger-full-access.
        # Mesh never broadens a profile to make it work.
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        validate_run_request_tokens(req)
        _refuse_project_layer(req)
        validate_adapter_options(self.id, req.options, strict_keys=False)
        if req.effort is not None and req.effort not in CODEX_EFFORTS:
            raise EffortNotAllowed(
                f"Codex CLI {CODEX_VERSION} documents reasoning effort {list(CODEX_EFFORTS)}, not '{req.effort}'",
                details={"reason": "effort_not_supported_by_cli", "effort": req.effort,
                         "allowed": list(CODEX_EFFORTS)},
            )
        profile_flags = self.profile_flags(req.profile)
        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [binary]
        if req.profile != "unrestricted":
            # exec never prompts; "never" makes a command that needs approval fail instead.
            argv.extend(["--ask-for-approval", "never"])
        # --skip-git-repo-check: non-code task workspaces are plain directories; the cwd is a
        # Mesh workspace (a task worktree for code tasks), never the user's checkout.
        argv.extend(["exec", "--json", "--skip-git-repo-check", "--cd", req.workspace])
        if req.model:
            argv.extend(["--model", req.model])
        if req.effort:
            argv.extend(["--config", f"model_reasoning_effort={req.effort}"])
        argv.extend(profile_flags)
        if req.resume_session_id:
            argv.extend(["resume", req.resume_session_id])
        # "-": the whole prompt is read from stdin (prompt.md), never from argv.
        argv.append("-")
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
        """Smoke-facing facts of a codex stream."""
        info = vs.new_stream_info()
        scan = _scan_events(vs.iter_jsonl_lines((stdout_text or "").split("\n"), info), info)
        return {k: scan[k] for k in ("session_id", "turn_started", "turn_completed", "vendor_exit_meaning",
                                     "error_message", "reconnects")}

    @staticmethod
    def parse_stderr_error(stderr_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Known codex stderr signatures: an argv the CLI rejects, or an auth/quota/network
        message. None otherwise (in ``--json`` mode run errors are on stdout)."""
        text = stderr_text or ""
        if not text.strip():
            return None
        lower = text.lower()
        if "error: unexpected argument" in lower or "error: invalid value" in lower or "usage: codex" in lower:
            return {"reason": "cli_usage_error",
                    "details": {"stderr": vs.bounded(text.strip()),
                                "note": f"codex rejected its arguments; Mesh targets Codex CLI {CODEX_VERSION}"}}
        meaning = vs.classify_error_text(text)
        if meaning:
            return {"reason": meaning, "details": {"stderr": vs.bounded(text.strip())}}
        return None

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        # `codex debug models` is experimental and may contact the server: the model
        # allowlist is supplied by the user (agent create --allowed-model), as for vibe.
        return None

    @staticmethod
    def parse_login_status(rc: int, stdout: str, stderr: str) -> Dict[str, Any]:
        """``codex login status`` reduced to a method name; the raw text (which can show a
        masked key) is never kept."""
        text = ((stdout or "") + "\n" + (stderr or "")).lower()
        if "not logged in" in text:
            method = "none"
        elif "chatgpt" in text:
            method = "chatgpt"
        elif "api key" in text:
            method = "api_key"
        else:
            method = "unknown"
        return {"method": method, "logged_in": rc == 0 and method not in ("none", "unknown"), "exit_code": rc}

    def auth_status(self, bin_path: str, timeout_s: float = 5.0) -> Dict[str, Any]:
        res = run_readonly_cmd([bin_path, "login", "status"], timeout_s=timeout_s)
        auth = self.parse_login_status(res.returncode, res.stdout, res.stderr)
        env = build_external_env()
        auth["api_key_env"] = [name for name in API_KEY_ENV if env.get(name)]
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
                                   "exit_code": res_h.returncode,
                                   "stderr": vs.bounded(res_h.stderr)}, "none")
        res_eh = run_readonly_cmd([bin_path, "exec", "--help"], timeout_s=timeout_s)
        if res_eh.returncode != 0:
            return vs.error_probe(self, conn, "error", v, bin_path,
                                  {"error": "timeout" if res_eh.returncode == -1 else "exec_help_check_failed",
                                   "exit_code": res_eh.returncode, "stderr": vs.bounded(res_eh.stderr)}, "none")
        missing = find_missing_flags(TOP_FLAGS, res_h.help_text) + find_missing_flags(EXEC_FLAGS, res_eh.help_text)
        caps: Dict[str, Dict[str, Any]] = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            flag = CAP_FLAGS.get(name)
            if cap.supported == "yes":
                cdict["verification"] = "unverified" if flag in missing else "help"
            caps[name] = cdict
        details: Dict[str, Any] = {"auth": self.auth_status(bin_path, timeout_s), "documented_version": CODEX_VERSION}
        if v != CODEX_VERSION:
            details["version_note"] = f"Mesh's codex adapter was written against {CODEX_VERSION}; installed {v}"
        probe = Probe(
            adapter=self.id,
            version=v,
            status="partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=TOP_FLAGS + EXEC_FLAGS,
            models=None,
            binary_path=bin_path,
            help_sha256=res_eh.help_sha256,
            flag_diff={"match": False, "missing_flags": missing} if missing else None,
            details=details,
        )
        record_adapter_probe(conn, self.id, probe, bin_path)
        return probe


def _scan_events(events, info: Dict[str, Any]) -> Dict[str, Any]:
    """One pass over codex ``exec --json`` events (see the module docstring)."""
    out: Dict[str, Any] = {
        "session_id": None, "turn_started": False, "turn_completed": False, "turn_failed": False,
        "fatal_seen": False, "error_message": None, "reconnects": 0, "last_retry": None,
    }
    messages: List[str] = []
    tokens: Dict[str, int] = {}
    counts = {"commands": 0, "failed_commands": 0, "file_changes": 0, "notices": 0, "turns": 0}
    for event in events:
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if out["session_id"] is None and is_safe_cli_token(tid):
                out["session_id"] = tid
        elif etype == "turn.started":
            out["turn_started"] = True
        elif etype == "turn.completed":
            out["turn_completed"] = True
            counts["turns"] += 1
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in TOKEN_KEYS:
                    val = usage.get(key)
                    if isinstance(val, int) and not isinstance(val, bool):
                        tokens[key] = tokens.get(key, 0) + val
        elif etype == "turn.failed":
            out["turn_failed"] = True
            err = event.get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            if isinstance(msg, str) and msg:
                out["error_message"] = msg
        elif etype == "error":
            msg = event.get("message") if isinstance(event.get("message"), str) else ""
            if msg.startswith(RECONNECT_PREFIX):
                out["reconnects"] += 1
                out["last_retry"] = msg
            else:
                out["fatal_seen"] = True
                if msg:
                    out["error_message"] = msg
        elif etype == "item.completed":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            itype = item.get("type")
            if itype == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
            elif itype == "command_execution":
                counts["commands"] += 1
                if item.get("status") == "failed":
                    counts["failed_commands"] += 1
            elif itype == "file_change":
                counts["file_changes"] += 1
            elif itype == "error":
                counts["notices"] += 1
    # Failure rule: an explicit failure (turn.failed or a non-retry error event) is classified
    # from its message (auth/quota/rate/network, else run_error); otherwise a stream without
    # turn.completed is output_truncated (runwrap log cap only) or incomplete (cut line, stopped, killed,
    # empty); a completed turn is a normal finish (None).
    if out["turn_failed"] or out["fatal_seen"]:
        # A recovered "Reconnecting..." notice never names the failure: only the final error.
        meaning = vs.classify_error_text(out["error_message"]) or vs.RUN_ERROR
        status = "failed"
    elif not out["turn_completed"]:
        meaning = vs.end_of_stream_meaning(info)
        status = meaning
    else:
        meaning = None
        status = "completed"
    out["vendor_exit_meaning"] = meaning
    out["final_text"] = "\n\n".join(messages)
    usage_out: Dict[str, Any] = dict(tokens)
    usage_out["status"] = status
    usage_out.update({k: v for k, v in counts.items() if v})
    if out["reconnects"]:
        usage_out["reconnects"] = out["reconnects"]
    if out["error_message"]:
        usage_out["error_message"] = vs.bounded(out["error_message"])
    if info.get("malformed"):
        usage_out["malformed_lines"] = info["malformed"]
    if info.get("log_capped"):
        usage_out["log_capped"] = True
    out["usage"] = usage_out
    return out
