"""Devin CLI adapter (``devin -p --prompt-file <prompt.md> ...``, local agent only).

Written against Devin CLI **3000.14.30821** (``devin --help``, ``devin auth status``,
``devin sandbox setup`` and real error output captured 2026-11-19; see
docs/adapters-devin.md). ``probe()`` re-checks the emitted flags against the installed
binary's live help.

* Never ``--cloud``: the run is the local agent with the task worktree as cwd.
* Prompt: ``-p`` does not read stdin ("Print mode runs a single turn non-interactively, so it
  needs a prompt") and the prompt never goes in argv, so it is passed as
  ``--prompt-file <run_dir>/prompt.md`` (written 0600 by the supervisor in the private run dir).
* ``--respect-workspace-trust false``: print mode cannot show the trust prompt and refuses an
  untrusted directory ("Refusing to run in an untrusted workspace").
* No machine-readable output: stdout is the plain-text reply (terminal escapes stripped),
  errors are ``Error: ...`` lines on stderr with exit 1.
* Print mode rejects every tool call its permission mode does not auto-approve, one stderr
  line each: "warning: rejected a tool call that requires confirmation. Running in
  non-interactive mode. Use --permission-mode dangerous to auto-approve all tools." (real
  output, 2026-11-19); those lines are counted as ``usage.tool_denied``, not a failure.
* No effort flag: effort is part of the model id (e.g. ``swe-2-flash-medium``).

Project config layer (share/devin/docs reference/configuration): Devin loads
``.devin/config.json`` / ``.devin/config.local.json`` (permissions, hooks, read_config_from),
``.devin/mcp_config.json`` / ``.devin/mcp_config.local.json``, ``.devin/hooks.v1.json`` /
``.devin/hooks.json``, ``.devin/agents`` (subagent profiles), and imports MCP servers from
other tools (``.mcp.json``, ``.claude/settings*.json``, ``.cursor/mcp.json``,
``opencode.json``, ``.zed/settings.json``), walking up to the ``.git`` / ``.jj`` root. The
legacy project directory ``.cognition/`` is still read the same way as ``.devin/``
("Warning: Found legacy project directory .cognition/"), so any ``.cognition`` entry is
refused. A ``skills/**/SKILL.md`` (under ``.devin``, ``.cognition``, ``.agents``,
``.claude``, ``.cursor``, ``.codex``, ``.kiro``, ``.github``, ``.windsurf``, ``.codeium``)
whose frontmatter has ``allowed-tools`` or ``permissions`` auto-approves tools while it
runs, so it is refused too (bounded scan; over the bound or unreadable = refused). Devin
reads that frontmatter with a real YAML parser and follows symlinks, so the check does not
parse YAML: a skill passes only when its frontmatter is plain ``key: value`` lines with
keys from ``SKILL_PLAIN_KEYS`` (see ``_frontmatter_grants``), and any symlink in a skills
tree is refused. ``--config`` only replaces the user config, so no switch disables these:
below ``unrestricted`` such a layer is refused (``devin_project_layer_present``).
Instruction-only files (AGENTS.md, CLAUDE.md, rules, skills with plain frontmatter) are not
refused.

Profiles, measured 2026-11-19 (docs/adapters-devin.md): only ``unrestricted`` is supported.
``edit`` is unsupported because an accept-edits run can write itself a skill with grants and
use it in the same run. ``readonly`` is unsupported because an auto run loads skills lazily
from directories it reads (inside or outside the workspace) and their grants apply, which no
check before the launch can cover. So the project layer check above and the environment
check (``ENV_WIDENERS``) run for no supported profile today; build() keeps them for a profile
that comes back with per-run deny rules (docs, "Future work").
"""

import errno
import os
import re
import sqlite3
import stat
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
    parse_version_output,
    read_run_file_nofollow,
    record_adapter_probe,
    run_readonly_cmd,
    validate_adapter_options,
    validate_run_request_tokens,
)
from mesh.adapters.project_layer import _root_chain, project_layer_paths

DEVIN_VERSION = "3000.14.30821"
FLAGS = ["--print", "--prompt-file", "--model", "--permission-mode", "--respect-workspace-trust"]
CAP_FLAGS = {"headless": "--print", "model_selection": "--model"}
#: Mesh profile -> ``--permission-mode``. readonly: unsupported. Measured 2026-11-19 (review
#: r2): an auto run read a file in a directory that holds a skill with ``allowed-tools: [exec]``
#: (a workspace subdirectory, a repository outside the workspace), the session loaded the
#: skill and its shell command ran; any readable directory counts, so a check before the
#: launch cannot hold the readonly boundary (docs/adapters-devin.md).
#: edit: unsupported. Measured 2026-11-19 (review r2
#: P2-2): an accept-edits run wrote ``.devin/skills/p/SKILL.md`` with ``allowed-tools: [exec]``,
#: the session loaded the new skill at once and its shell command ran, so a check before the
#: launch cannot hold the edit boundary (docs/adapters-devin.md). dev: accept-edits
#: auto-approves no shell command (print mode rejects them), smart lets a model judge shell
#: safety and --sandbox is a Research Preview, so no mode gives a dependable "workspace
#: writes + shell" boundary.
#: elevated: the only wider mode is dangerous.
PERMISSION_MODES = {"unrestricted": "dangerous"}
#: Project config Devin loads or imports from the workspace (see the module docstring):
#: (config dir, names, user dir). "" = the file sits in the directory itself.
#: "agents": project subagent profiles (``agents/<name>.md``, ``agents/<name>/AGENT.md``) pick
#: their own ``model:``, outside Mesh's model allowlist (vendor docs, subagents.mdx).
DEVIN_DIR_NAMES = ("config.json", "config.local.json", "hooks.v1.json", "hooks.json", "mcp_config.json",
                   "mcp_config.local.json", "plugins", "agents")
#: The project directory before the rename to ``.devin``; Devin still reads it (MCP servers,
#: config, hooks, skills). Refused whole: nothing but Devin project config lives there.
LEGACY_DIR = ".cognition"
PROJECT_LAYERS = (
    (".devin", DEVIN_DIR_NAMES, "~/.devin"),
    (LEGACY_DIR, DEVIN_DIR_NAMES, "~/" + LEGACY_DIR),
    ("", (".mcp.json", "opencode.json"), "~/.devin-no-user-dir"),
    (".claude", ("settings.json", "settings.local.json"), "~/.claude"),
    (".cursor", ("mcp.json",), "~/.cursor"),
    (".codex", ("config.toml",), "~/.codex"),
    (".gemini", ("settings.json",), "~/.gemini"),
    (".windsurf", ("mcp_config.json", "hooks.json"), "~/.windsurf"),
    (".agents", ("plugins", "hooks.json", "agents"), "~/.agents"),
    (".zed", ("settings.json",), "~/.zed"),
)
#: Devin finds the project root by a ``.git`` or ``.jj`` entry walking up from cwd.
ROOT_MARKERS = (".git", ".jj")
#: Skill folders Devin reads (its own and imported); a SKILL.md whose frontmatter has
#: ``allowed-tools`` or ``permissions`` auto-approves tools while the skill runs.
SKILL_DIRS = (".devin", LEGACY_DIR, ".agents", ".claude", ".cursor", ".codex", ".kiro", ".github", ".windsurf",
              ".codeium")
#: Frontmatter keys that neither approve tools nor change the model or the agent a skill runs
#: as. Every other key (allowed-tools, permissions, model, agent, subagent, unknown ones)
#: counts as a grant.
SKILL_PLAIN_KEYS = frozenset({"name", "description", "argument-hint", "triggers", "license", "compatibility"})
_BREAKS = r"\x00-\x1f\x7f\x85\u2028\u2029"
#: The only frontmatter line that passes: a bare key at column 0, then nothing or one space-
#: separated value that cannot continue on another line (plain text starting with a letter or
#: digit, a quoted string without escapes, or a flow list of words).
SKILL_PLAIN_LINE_RE = re.compile(
    r"([a-z][a-z-]*):(?: +(?:[A-Za-z0-9][^%(b)s]*|\"[^\"\\%(b)s]*\"|'[^'%(b)s]*'|\[[A-Za-z0-9_, -]*\]))? *"
    % {"b": _BREAKS})
SKILL_FILE_NAME = "skill.md"
#: Scan bounds: past either one the skills tree counts as granting (fail closed).
SKILL_SCAN_MAX_ENTRIES = 2000
SKILL_HEAD_BYTES = 65536
#: The refusal message names at most this many paths, each cut to this many characters: the
#: names are task-controlled and the message is stored with every failed attempt.
SHOWN_PATHS = 10
SHOWN_PATH_CHARS = 200
#: meshd environment variables that change what a non-unrestricted run may do: DEVIN_SANDBOX
#: selects the sandbox (Autonomous) mode, which auto-approves shell; the other turns off the
#: symlink guard of the edit/write tools.
ENV_WIDENERS = ("DEVIN_SANDBOX", "DEVIN_CLI_DANGEROUSLY_FOLLOW_SYMLINKS")
INSTALL_HINT = ("devin not found on PATH: install the Devin CLI with "
                "`curl -fsSL https://cli.devin.ai/install.sh | bash`, sign in with `devin auth login`, "
                "or give the agent --binary <path>")
#: Devin-specific error wording on top of vendor_stream's shared markers.
AUTH_MARKERS = ("login canceled", "devin auth login", "not logged in")
QUOTA_MARKERS = ("usage limit", "quota exceeded", "exceeded your quota", "out of credits")
UNKNOWN_MODEL_RE = re.compile(r"^error: unknown model\b", re.IGNORECASE)
UNTRUSTED = "refusing to run in an untrusted workspace"
#: The rejected model name devin echoes ("Unknown model: 'x'") must not be classified.
UNKNOWN_MODEL_ECHO_RE = re.compile(r"(unknown model:\s*)(['\"]).*?\2", re.IGNORECASE)
#: A context window overflow is a run error, not an exhausted quota.
CONTEXT_RE = re.compile(r"context (window|length)|maximum context|prompt is too long", re.IGNORECASE)
USAGE_MARKERS = ("needs a prompt", "error: unexpected argument", "error: invalid value",
                 "error: a value is required", "error: unrecognized subcommand")
TOOL_REJECTED = "rejected a tool call"
#: CSI / OSC terminal sequences devin writes even to a pipe (e.g. "\x1b[?2004l").
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _frontmatter_grants(text: str, truncated: bool = False) -> bool:
    """True unless ``text`` (the head of a SKILL.md) is plainly instruction-only.

    Fail closed, no YAML parsing: Devin's parser accepts quoted, tagged, anchored, complex,
    flow and indented keys, CR / NEL / LS line breaks, and frontmatter markers with leading
    blank lines, indentation, trailing text or CRLF. A file passes when it has no frontmatter
    marker at its start, or when it starts with exactly ``---`` + LF and every line up to the
    closing ``---`` line is empty or matches SKILL_PLAIN_LINE_RE with a SKILL_PLAIN_KEYS key."""
    text = text.lstrip("\ufeff")
    if not text.startswith("---\n"):
        return text.lstrip().startswith("---")
    lines = text.split("\n")[1:]
    if truncated:
        lines.pop()  # cut mid-line by the read bound
    for line in lines:
        if line == "---":
            return False
        if line:
            m = SKILL_PLAIN_LINE_RE.fullmatch(line)
            if not m or m.group(1) not in SKILL_PLAIN_KEYS:
                return True
    return True  # unterminated frontmatter (or longer than the read): treat as granting


def _read_head(path: str, limit: int) -> bytes:
    """Up to ``limit`` bytes of a regular file. build() runs on the scheduler's event loop
    and the file is task-controlled, so (like ``base.open_run_file_nofollow``) the open
    neither follows a symlink nor blocks on a FIFO, and anything but a regular file raises."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOCTTY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Refusing non-regular skill file", path)
        chunks: List[bytes] = []
        while limit > 0:
            chunk = os.read(fd, limit)
            if not chunk:
                break
            chunks.append(chunk)
            limit -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _skill_grants(path: str) -> bool:
    """True when ``path``'s frontmatter may grant tools, or it is a symlink, not a regular
    file, or cannot be read."""
    try:
        head = _read_head(path, SKILL_HEAD_BYTES + 1)
    except OSError:
        return True
    return _frontmatter_grants(head[:SKILL_HEAD_BYTES].decode("utf-8", "replace"), len(head) > SKILL_HEAD_BYTES)


def _skill_grants_found(workspace: str) -> List[str]:
    found: List[str] = []
    budget = SKILL_SCAN_MAX_ENTRIES
    for directory in _root_chain(workspace, ROOT_MARKERS):
        for sd in SKILL_DIRS:
            top = os.path.join(directory, sd, "skills")
            if not os.path.lexists(top):
                continue
            if os.path.realpath(os.path.join(directory, sd)) == os.path.realpath(os.path.expanduser("~/" + sd)):
                continue  # the user's own folder is user config
            if os.path.islink(top) or not os.path.isdir(top):
                found.append(top)
                continue
            for root, dirs, files in os.walk(top, onerror=lambda _e, t=top: found.append(t)):
                budget -= len(dirs) + len(files)
                if budget < 0:
                    found.append(top)
                    return found
                for name in dirs + files:
                    path = os.path.join(root, name)
                    # Devin follows symlinks here; the walk does not, so a link is refused.
                    if os.path.islink(path) or (name.lower() == SKILL_FILE_NAME and _skill_grants(path)):
                        found.append(path)
    return found


def _legacy_dirs_found(workspace: str) -> List[str]:
    user_real = os.path.realpath(os.path.expanduser("~/" + LEGACY_DIR))
    found = [os.path.join(d, LEGACY_DIR) for d in _root_chain(workspace, ROOT_MARKERS)]
    return [p for p in found if os.path.lexists(p) and os.path.realpath(p) != user_real]


def project_layer_found(workspace: str) -> List[str]:
    found: List[str] = _legacy_dirs_found(workspace)
    for config_dir, names, user_dir in PROJECT_LAYERS:
        found.extend(project_layer_paths(workspace, config_dir, names, user_dir, root_markers=ROOT_MARKERS))
    found.extend(_skill_grants_found(workspace))
    return found


def _refuse_env_wideners(req: RunRequest) -> None:
    if req.profile == "unrestricted":
        return
    from mesh.constants import build_external_env

    env = build_external_env()
    hit = [n for n in ENV_WIDENERS if env.get(n)]
    if hit:
        raise AdapterUnsupported(
            f"devin_env_widens_profile: devin refuses profile '{req.profile}': {', '.join(hit)} is set in "
            "meshd's environment and would widen it",
            reason="devin_env_widens_profile",
        )


def _refuse_project_layer(req: RunRequest) -> None:
    if req.profile == "unrestricted":
        return
    found = project_layer_found(req.workspace)
    if found:
        ws = os.path.realpath(req.workspace)
        rels = sorted({os.path.relpath(p, ws) for p in found})
        shown = [r if len(r) <= SHOWN_PATH_CHARS else r[:SHOWN_PATH_CHARS] + "..." for r in rels[:SHOWN_PATHS]]
        more = f" (+{len(rels) - len(shown)} more)" if len(rels) > len(shown) else ""
        raise AdapterUnsupported(
            f"devin_project_layer_present: devin refuses profile '{req.profile}': the workspace has a project "
            f"config layer ({', '.join(shown)}{more}) whose permission rules/MCP servers/hooks would widen the profile",
            reason="devin_project_layer_present",
        )


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def classify_devin_error(text: Optional[str]) -> Optional[str]:
    """Failure meaning of a devin error message: shared markers first, then devin's own."""
    if not text:
        return None
    meaning = vs.classify_error_text(text)
    if meaning:
        return meaning
    lower = text.lower()
    if any(m in lower for m in AUTH_MARKERS):
        return vs.AUTH_FAILED
    if any(m in lower for m in QUOTA_MARKERS):
        return vs.QUOTA_EXHAUSTED
    return None


def _tool_rejections(stderr_text: str) -> int:
    """``warning: rejected a tool call ...`` lines on stderr (never the model's reply)."""
    return sum(1 for ln in strip_ansi(stderr_text).splitlines()
               if ln.strip().lower().startswith("warning:") and TOOL_REJECTED in ln.lower())


def _error_lines(stderr_text: str) -> List[str]:
    """``Error:`` lines of stderr, the echoed model name of an unknown-model error masked."""
    return [UNKNOWN_MODEL_ECHO_RE.sub(r"\1<model>", ln.strip()) for ln in strip_ansi(stderr_text).splitlines()
            if ln.strip().lower().startswith("error:")]


def _run_error_kind(errors: List[str]) -> Optional[str]:
    for e in errors:
        lower = e.lower()
        if UNTRUSTED in lower:
            return "workspace_untrusted"
        if UNKNOWN_MODEL_RE.match(e):
            return "unknown_model"
        if CONTEXT_RE.search(e):
            return "context_window"
    return None


class DevinAdapter(Adapter):
    """Adapter for the Devin CLI (``devin``), local agent."""

    id = "devin"
    #: Empty (treated like None by check_adapter_effort_values): effort is part of the model id,
    #: refused upstream as EFFORT_UNSUPPORTED via the reasoning_effort capability; build() is a last guard.
    supported_efforts = ()
    kind = "vendor"
    binary_names = ["devin"]
    planned_version = DEVIN_VERSION
    verified_version = None  # no committed help fixture; probe() checks flags live
    liveness = "none"
    max_argv_prompt_bytes = 100_000  # the prompt goes in --prompt-file; kept for the shared limit
    launch_enabled = False
    launch_milestone = "M3"
    #: devin reports auth/quota/model failures as "Error: ..." on stderr and exits 1, so a
    #: non-zero exit takes the parsed meaning as its reason (same switch as codex/claude).
    nonzero_exit_uses_stream_meaning = True

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="none",
                                   note="-p/--print --prompt-file"),
            "stream_output": Capability("stream_output", supported="no", verification="none",
                                        note="plain-text stdout only (no JSON output format)"),
            "model_selection": Capability("model_selection", supported="yes", verification="none", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="no", verification="none",
                                           note="effort is part of the model id"),
            "resume": Capability("resume", supported="no", verification="none",
                                 note="no session id is printed in print mode"),
        }

    def resolve_binary(self, binary_path: Optional[str] = None) -> Optional[str]:
        if binary_path:
            return super().resolve_binary(binary_path)
        return vs.resolve_on_path(self.binary_names)

    def profile_flags(self, profile: str) -> List[str]:
        mode = PERMISSION_MODES.get(profile)
        if mode is None:
            raise ProfileUnsupported(self.id, profile)
        return ["--permission-mode", mode]

    def build(self, req: RunRequest) -> CommandSpec:
        validate_run_request_tokens(req)
        validate_adapter_options(self.id, req.options, strict_keys=False)
        if req.effort is not None:
            raise EffortNotAllowed(
                f"Devin CLI {DEVIN_VERSION} has no effort flag; pick the effort with the model id "
                f"(e.g. swe-2-flash-medium), not '{req.effort}'",
                details={"reason": "effort_not_supported_by_cli", "effort": req.effort, "allowed": []},
            )
        profile_flags = self.profile_flags(req.profile)
        _refuse_env_wideners(req)
        _refuse_project_layer(req)
        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [binary, "-p", "--prompt-file", os.path.abspath(req.prompt_path)]
        if req.model:
            argv.extend(["--model", req.model])
        argv.extend(profile_flags)
        argv.extend(["--respect-workspace-trust", "false"])
        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        stdout = _read(run_dir, "stdout.log")
        stderr = _read(run_dir, "stderr.log")
        facts = self.output_facts(stdout, stderr)
        return RunResult(final_text=facts["final_text"], session_id=None, usage=facts["usage"],
                         vendor_exit_meaning=facts["vendor_exit_meaning"])

    @staticmethod
    def output_facts(stdout_text: str, stderr_text: str) -> Dict[str, Any]:
        """Final text, failure meaning and usage of one print-mode run.

        Failure rule: an ``Error:`` line on stderr is a failed run. Only those lines are
        classified (warnings never are): unknown model, untrusted workspace and context window
        overflow are run_error; else the shared auth/quota/rate/network classification (plus
        devin's wording), else run_error, except rejected arguments, which have no meaning
        (reason cli_usage_error from parse_stderr_error). No error line: a normal finish."""
        final_text = strip_ansi(stdout_text).strip()
        errors = _error_lines(stderr_text)
        meaning = None
        usage_error = False
        if errors:
            joined = "\n".join(errors)
            meaning = vs.RUN_ERROR if _run_error_kind(errors) else classify_devin_error(joined)
            # Rejected arguments: no run meaning, so the supervisor keeps exit_nonzero and
            # parse_stderr_error names the reason cli_usage_error.
            usage_error = meaning is None and any(m in joined.lower() for m in USAGE_MARKERS)
            if meaning is None and not usage_error:
                meaning = vs.RUN_ERROR
        usage: Dict[str, Any] = {"status": "cli_usage_error" if usage_error else (meaning or "completed")}
        denied = _tool_rejections(stderr_text)
        if denied:
            usage["tool_denied"] = denied
        if errors:
            usage["error_message"] = vs.bounded(errors[-1])
        return {"final_text": final_text, "vendor_exit_meaning": meaning, "usage": usage}

    @staticmethod
    def parse_stderr_error(stderr_text: Optional[str]) -> Optional[Dict[str, Any]]:
        """Known stderr signatures: rejected arguments, unknown model, untrusted workspace, or
        an auth/quota/rate/network message. None otherwise."""
        text = strip_ansi(stderr_text or "")
        if not text.strip():
            return None
        bounded = vs.bounded(text.strip())
        errors = _error_lines(text)
        joined = "\n".join(errors)
        kind = _run_error_kind(errors)
        if kind:
            return {"reason": "run_error", "details": {"stderr": bounded, "error": kind}}
        if any(m in joined.lower() for m in USAGE_MARKERS):
            return {"reason": "cli_usage_error",
                    "details": {"stderr": bounded,
                                "note": f"devin rejected its arguments; Mesh targets Devin CLI {DEVIN_VERSION}"}}
        meaning = classify_devin_error(joined)
        if meaning:
            return {"reason": meaning, "details": {"stderr": bounded}}
        if _tool_rejections(text):
            return {"reason": "tool_denied", "details": {"stderr": bounded}}
        return None

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        # `devin models` needs a login and lists the account's models; the allowlist comes
        # from the user (--allowed-model / --default-model).
        return None

    @staticmethod
    def parse_auth_status(rc: int, stdout: str) -> Dict[str, Any]:
        """``devin auth status`` reduced to the login method. It exits 0 logged in or not, so
        the text decides; name, e-mail, ids and team fields are never kept."""
        lines = [ln.strip() for ln in strip_ansi(stdout).splitlines() if ln.strip()]
        first = lines[0].lower() if lines else ""
        if first.startswith("not logged in"):
            method = "none"
        elif first.startswith("logged in (via devin)"):
            method = "subscription"
        elif first.startswith("logged in") and "api key" in first:
            method = "api_key"
        else:
            method = "unknown"
        return {"method": method, "logged_in": rc == 0 and method in ("subscription", "api_key"),
                "exit_code": rc}

    def auth_status(self, bin_path: str, timeout_s: float = 5.0) -> Dict[str, Any]:
        res = run_readonly_cmd([bin_path, "auth", "status"], timeout_s=timeout_s)
        return self.parse_auth_status(res.returncode, res.stdout)

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
        details: Dict[str, Any] = {"auth": self.auth_status(bin_path, timeout_s), "documented_version": DEVIN_VERSION}
        if v != DEVIN_VERSION:
            details["version_note"] = f"Mesh's devin adapter was written against {DEVIN_VERSION}; installed {v}"
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


def _read(run_dir: str, name: str) -> str:
    try:
        return read_run_file_nofollow(os.path.join(run_dir, name)) or ""
    except OSError:
        return ""
