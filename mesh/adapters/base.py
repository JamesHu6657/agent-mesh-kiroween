"""Base adapter interface and data structures per plan §14.1."""

from dataclasses import dataclass, field
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from typing import Any, Dict, List, Optional, TextIO, Tuple

import mesh.constants as c
from mesh.constants import build_external_env
from mesh.errors import MeshError

# Expected help sha256 hashes per plan §14.11
# Committed fixtures evidence captured read-only by coordinator.
# Runtime code never imports test code. Accepted deviation (D5, 2026-11-09): for launch
# evidence (mesh/adapters/evidence.py) the runtime reads tests/smoke/results and hashes the
# files under tests/fixtures/real that those results cite.
EXPECTED_HELP_HASHES: Dict[str, str] = {
    "droid": "1df2e88e997f1488d22fa59340bc79c13198a143363981ee8825a641260143f3",  # droid --help
    "droid_exec": "61880b6d4c40e86e497653e95f01beb3a7cdad223a4fd98010d53d6dce30449a",  # droid exec --help
    "agy": "e92946f85cec63eca61f93014ca49f53b1e84277eac7954e7fac7d8d712940fd",  # agy --help
    "kiro": "353d58ab2ccd44f36372cf16a00a3d051519ccaf4778b5269d6dded4622d17d5",  # kiro-cli --help
    "kiro_chat": "56bf95560094d7bee17372d18b8fc40ff834ff6b72c338863f91428719b00eeb",  # kiro-cli chat --help
    "vibe": "15d858a3434d0664ee8af445933ef438bc7a6efac9961de31351013c0d2ee158",  # vibe --help
}


def parse_version_output(text: str) -> Optional[str]:
    """Parse version string from '<bin> --version' output.
    
    Extracts semver-like strings e.g. '1.5.2', '1.3.0', '3.1.0'.
    """
    if not text:
        return None
    m = re.search(
        r"(?<![a-zA-Z0-9._])(?:version\s+|v)?(\d+(?:\.\d+)+(?:-[a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)*)?)(?![a-zA-Z0-9_])(?!\.[a-zA-Z0-9])",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None


ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def normalize_help_text(stdout: str, stderr: str) -> str:
    """Normalize help text combining stdout and stderr per fixture capture rules:
    
    1. Merge stdout and stderr (as `cmd 2>&1`).
    2. Strip ANSI escape sequences.
    3. Drop lines containing 'warning: setlocale'.
    """
    combined = (stdout or "") + (stderr or "")
    if not combined:
        return ""
    stripped = ANSI_ESCAPE_RE.sub("", combined)
    lines = [
        line
        for line in stripped.splitlines(keepends=True)
        if "warning: setlocale" not in line
    ]
    return "".join(lines)


def is_flag_present(flag: str, help_text: str) -> bool:
    """Check if a CLI flag is present in help text.
    
    Performs boundary-aware check so e.g. '--print' does not match inside '--print-timeout',
    and handles flags with trailing '=' like '--trust-tools='.
    """
    if not help_text:
        return False
    base = flag.rstrip("=")
    pattern = r"(?:^|[\s,\[(])" + re.escape(base) + r"(?:[\s=,\]):]|$)"
    return bool(re.search(pattern, help_text))


def find_missing_flags(flags: List[str], help_text: str) -> List[str]:
    """Return list of flags genuinely absent from help text."""
    if not help_text:
        return list(flags)
    return [fl for fl in flags if not is_flag_present(fl, help_text)]


class ReadonlyCmdResult(tuple):
    """Result of run_readonly_cmd.
    
    Subclasses 4-tuple (returncode, stdout, stderr, help_sha256) so existing tuple
    unpackings (rc, out, err, sha) continue to work seamlessly, while providing
    .help_text, .help_sha256, .stdout, .stderr, and .returncode attributes.
    """
    def __new__(cls, returncode: int, stdout: str, stderr: str, help_sha256: str, help_text: str):
        return super().__new__(cls, (returncode, stdout, stderr, help_sha256))

    def __init__(self, returncode: int, stdout: str, stderr: str, help_sha256: str, help_text: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.help_sha256 = help_sha256
        self.help_text = help_text
        self.sha256 = help_sha256


#: After a probe timeout kill, how long its output pipes are still read.
PROBE_KILL_DRAIN_S = 1.0


def _run_probe_bounded(argv: List[str], timeout_s: float, max_output_bytes: int,
                       env: Dict[str, str]) -> Tuple[bytes, bytes, int]:
    """Blind review P2: run a probe in its own process group, keep at most
    ``max_output_bytes`` of each stream while draining the rest, and on timeout kill the
    whole group (descendants too). Raises ``subprocess.TimeoutExpired`` on timeout."""
    import signal
    import time

    from mesh.daemon import children

    try:
        proc = children.popen_registered(
            "probe", argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, shell=False, start_new_session=True,
        )
    except children.SpawnedThenFailed as exc:
        try:
            os.killpg(exc.proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            exc.proc.wait(timeout=PROBE_KILL_DRAIN_S)
        except Exception:
            pass
        children.unhold(exc.proc.pid)
        raise
    # PR #40 r1: no reader threads. A descendant that left the group (setsid) may keep
    # the pipes open forever; reads are non-blocking with an overall deadline, and the
    # pipe fds are closed directly (closing a file object another thread reads blocks).
    import selectors

    bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    sel = selectors.DefaultSelector()
    for fd in bufs:
        os.set_blocking(fd, False)
        sel.register(fd, selectors.EVENT_READ)
    timed_out = False
    deadline = time.monotonic() + timeout_s
    drain_until: Optional[float] = None
    try:
        while True:
            now = time.monotonic()
            # PR #40 r2: process exit is tracked apart from pipe EOF; a probe that closes
            # its output and keeps running is waited for, and killed only at the deadline.
            exited = proc.poll() is not None
            if exited and drain_until is None:
                drain_until = now + PROBE_KILL_DRAIN_S
            if exited and not sel.get_map():
                break
            if drain_until is not None and now >= drain_until:
                break
            if drain_until is None and now >= deadline:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                drain_until = now + PROBE_KILL_DRAIN_S
                continue
            limit = drain_until if drain_until is not None else deadline
            wait = max(0.0, min(0.05, limit - now))
            if not sel.get_map():
                time.sleep(wait)
                continue
            for key, _ in sel.select(timeout=wait):
                fd = key.fd
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(fd)
                    continue
                room = max_output_bytes - len(bufs[fd])
                if room > 0:
                    bufs[fd].extend(chunk[:room])
        # The group is killed either way: a probe never leaves descendants behind.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=PROBE_KILL_DRAIN_S)
        except subprocess.TimeoutExpired:
            pass
    finally:
        sel.close()
        out_b = bytes(bufs[proc.stdout.fileno()])
        err_b = bytes(bufs[proc.stderr.fileno()])
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass
        children.unregister(proc.pid)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout_s)
    return out_b, err_b, proc.returncode if proc.returncode is not None else -1


def run_readonly_cmd(
    argv: List[str],
    timeout_s: float = 5.0,
    max_output_bytes: int = 200_000,
) -> ReadonlyCmdResult:
    """Execute a vendor CLI binary with ONLY read-only invocations.
    
    Guarantees:
    - shell=False.
    - stdin is /dev/null (subprocess.DEVNULL).
    - Minimal environment built by build_external_env().
    - Hard timeout (default 5.0s).
    - Output size limited to max_output_bytes.
    - Computes normalized help text (combining stdout+stderr normalized like fixture capture)
      and its sha256.
    
    Returns:
        ReadonlyCmdResult (returncode, stdout_str, stderr_str, help_sha256)
    """
    env = build_external_env()
    try:
        raw_stdout, raw_stderr, returncode = _run_probe_bounded(argv, timeout_s, max_output_bytes, env)
        stdout_str = raw_stdout[:max_output_bytes].decode("utf-8", errors="replace")
        stderr_str = raw_stderr[:max_output_bytes].decode("utf-8", errors="replace")
        help_text = normalize_help_text(stdout_str, stderr_str)
        help_sha256 = hashlib.sha256(help_text.encode("utf-8")).hexdigest()
        return ReadonlyCmdResult(returncode, stdout_str, stderr_str, help_sha256, help_text)
    except subprocess.TimeoutExpired:
        return ReadonlyCmdResult(-1, "", f"Command timed out after {timeout_s}s", "", "")
    except FileNotFoundError:
        return ReadonlyCmdResult(127, "", "Binary not found", "", "")
    except PermissionError:
        return ReadonlyCmdResult(126, "", "Permission denied executing binary", "", "")
    except Exception as e:
        return ReadonlyCmdResult(1, "", f"Execution error: {e}", "", "")


def record_adapter_probe(
    conn: Optional[sqlite3.Connection],
    adapter: str,
    probe: "Probe",
    binary_path: Optional[str] = None,
) -> None:
    """Record probe result in adapter_probes table per plan §14.10 and §5.5."""
    if conn is None:
        return
    try:
        from mesh.core.clock import now_utc
        from mesh.core.redact import redact_value

        probed_at = probe.probed_at or now_utc()
        probe.probed_at = probed_at
        red_bin = redact_value(binary_path or probe.binary_path, jsonl=True)
        red_ver = redact_value(probe.version, jsonl=True)
        red_flags = redact_value(probe.flags, jsonl=True)
        red_models = redact_value(probe.models, jsonl=True) if probe.models is not None else None
        conn.execute(
            """
            INSERT OR REPLACE INTO adapter_probes (
                adapter, probed_at, binary_path, version, help_sha256, flags_json, models_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                adapter,
                probed_at,
                red_bin,
                red_ver,
                probe.help_sha256,
                json.dumps(red_flags),
                json.dumps(red_models) if red_models is not None else None,
                probe.status,
            ),
        )
    except Exception:
        pass


class ProfileUnsupported(MeshError):
    """Raised when an adapter cannot express a permission profile."""

    def __init__(self, adapter: str, profile: str):
        super().__init__(
            code=c.PROFILE_UNSUPPORTED,
            exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
            message=f"Adapter '{adapter}' does not support permission profile '{profile}'",
            details={"adapter": adapter, "profile": profile},
        )



def positional_prompt(text: str) -> str:
    """Make ``text`` safe as the last positional argv item of a vendor CLI: a prompt that
    starts with "-" (and so could be parsed as an option) gets a leading newline. Mesh's own
    prompts start with "# Agent Mesh task" or "Read the file", so this only matters for a
    caller-supplied prompt file (m4-p3-cleanup)."""
    return "\n" + text if text.startswith("-") else text


ADAPTER_ALLOWED_OPTIONS: Dict[str, frozenset] = {
    "droid": frozenset({"binary"}),
    "claude": frozenset({"binary"}),  # r3 P2-2: build() reads options.binary
    "codex": frozenset({"binary"}),
    "devin": frozenset({"binary"}),
    "agy": frozenset({"binary", "sandbox", "agent"}),
    "kiro": frozenset({"binary", "agent"}),
    "vibe": frozenset({"binary", "trust_workdir", "max_turns", "max_price_usd", "max_tokens"}),
    "fake": frozenset({"binary", "resume_verified"}),
    "hermes": frozenset({"binary"}),
    "openclaw": frozenset({"binary"}),
    "none": frozenset(),
}

_BOOL_OPTION_KEYS = frozenset({"sandbox", "trust_workdir", "resume_verified"})
_POS_INT_OPTION_KEYS = frozenset({"max_turns", "max_tokens"})


def is_safe_cli_token(val: Any) -> bool:
    """True for non-empty str without leading '-' or whitespace/control characters."""
    if not isinstance(val, str) or not val:
        return False
    if val.startswith("-"):
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in val)


def validate_cli_token(val: Any, param: str, *, max_len: Optional[int] = None) -> str:
    """Validate a CLI flag argument string (no leading '-', whitespace, or control chars)."""
    if not is_safe_cli_token(val) or (max_len is not None and len(val) > max_len):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid '{param}': must be a non-empty string without leading '-', whitespace, or control characters",
            details={"param": param},
        )
    return val


def validate_adapter_options(adapter_id: str, options: Any, *, strict_keys: bool = True) -> Dict[str, Any]:
    """Validate per-adapter option types/bounds and (when strict_keys=True) allowed keys."""
    if not isinstance(options, dict):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Adapter 'options' must be a JSON object",
            details={"param": "options"},
        )
    allowed = ADAPTER_ALLOWED_OPTIONS.get(adapter_id, frozenset())
    for k, v in options.items():
        if not isinstance(k, str) or not k:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Adapter option keys must be non-empty strings",
                details={"param": "options"},
            )
        if strict_keys and k not in allowed:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Unsupported option '{k}' for adapter '{adapter_id}'",
                details={"param": f"options.{k}", "adapter": adapter_id, "key": k},
            )
        if k == "binary":
            if (
                not isinstance(v, str)
                or not v.strip()
                or "\x00" in v
                or v.lstrip().startswith("-")
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in v)
            ):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Option 'binary' must be a non-empty path string not starting with '-'",
                    details={"param": "options.binary"},
                )
        elif k in ("run_id", "agent"):
            validate_cli_token(v, f"options.{k}")
        elif k in _BOOL_OPTION_KEYS:
            if not isinstance(v, bool):
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Option '{k}' must be a boolean",
                    details={"param": f"options.{k}"},
                )
        elif k in _POS_INT_OPTION_KEYS:
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Option '{k}' must be a positive integer",
                    details={"param": f"options.{k}"},
                )
        elif k == "max_price_usd":
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Option 'max_price_usd' must be a positive finite number",
                    details={"param": "options.max_price_usd"},
                )
    return dict(options)


def validate_run_request_tokens(req: "RunRequest") -> None:
    """Validate flag-argument tokens carried in RunRequest before building argv."""
    if req.model is not None:
        validate_cli_token(req.model, "model", max_len=256)
    if req.effort is not None:
        validate_cli_token(req.effort, "effort", max_len=64)
    if req.resume_session_id is not None:
        validate_cli_token(req.resume_session_id, "resume_session_id")

class AdapterUnsupported(MeshError):
    """Raised when an adapter cannot launch or is disabled."""

    def __init__(self, message: str, milestone: Optional[str] = None, reason: Optional[str] = None):
        details = {}
        if milestone:
            details["milestone"] = milestone
        if reason:
            details["reason"] = reason
        super().__init__(
            code=c.ADAPTER_UNSUPPORTED,
            exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
            message=message,
            details=details,
        )


class AdapterNotInstalled(MeshError):
    """Raised when an agent is configured with an uninstalled adapter."""

    def __init__(self, adapter: str):
        super().__init__(
            code=c.ADAPTER_NOT_INSTALLED,
            exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
            message=f"Adapter '{adapter}' is not installed on this system",
            details={"adapter": adapter},
        )


class TestDoubleDisabled(MeshError):
    """Raised when the fake adapter is used outside testing mode."""

    def __init__(self):
        super().__init__(
            code=c.TEST_DOUBLE_DISABLED,
            exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
            message="Test double adapter 'fake' is disabled outside testing mode (MESH_TESTING=1)",
            details={"adapter": "fake"},
        )


class PromptTooLong(MeshError):
    """Raised when prompt exceeds limits and no fallback is possible."""

    def __init__(self, bytes_len: int, limit: int, adapter: str, profile: str):
        super().__init__(
            code=c.PROMPT_TOO_LONG,
            exit_code=c.EXIT_USAGE,
            message=f"Prompt length {bytes_len} bytes exceeds limit of {limit} for adapter '{adapter}' under profile '{profile}'",
            details={"bytes": bytes_len, "limit": limit, "adapter": adapter, "profile": profile},
        )


class EffortUnsupported(MeshError):
    """Raised when effort is requested on an adapter with no effort support."""

    def __init__(self, adapter: str):
        super().__init__(
            code=c.EFFORT_UNSUPPORTED,
            exit_code=c.EXIT_USAGE,
            message=f"Adapter '{adapter}' does not support reasoning effort",
            details={"adapter": adapter},
        )


class EffortNotAllowed(MeshError):
    """Raised when effort is rejected due to policy or model conflicts."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            code=c.EFFORT_NOT_ALLOWED,
            exit_code=c.EXIT_USAGE,
            message=message,
            details=details or {},
        )


@dataclass
class Capability:
    name: str
    supported: str  # 'yes' | 'no' | 'unknown'
    verification: str  # 'help' | 'smoke' | 'none' | 'stale'
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "supported": self.supported,
            "verification": self.verification,
            "note": self.note,
        }


@dataclass
class RunRequest:
    prompt_path: str
    workspace: str
    model: Optional[str]
    effort: Optional[str]
    profile: str
    resume_session_id: Optional[str] = None
    timeout_s: int = 600
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandSpec:
    argv: List[str]
    cwd: str
    stdin: str = "devnull"  # 'devnull' | path


@dataclass
class RunResult:
    final_text: str = ""
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    vendor_exit_meaning: Optional[str] = None


@dataclass
class Probe:
    adapter: str
    version: Optional[str]
    status: str
    launch_enabled: bool
    launch_milestone: Optional[str]
    capabilities: Dict[str, Dict[str, Any]]
    flags: List[str]
    models: Optional[List[str]] = None
    binary_path: Optional[str] = None
    help_sha256: Optional[str] = None
    probed_at: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    flag_diff: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "adapter": self.adapter,
            "version": self.version,
            "status": self.status,
            "launch_enabled": self.launch_enabled,
            "launch_milestone": self.launch_milestone,
            "capabilities": self.capabilities,
            "flags": self.flags,
            "models": self.models,
        }
        if self.binary_path is not None:
            d["binary_path"] = self.binary_path
        if self.help_sha256 is not None:
            d["help_sha256"] = self.help_sha256
        if self.probed_at is not None:
            d["probed_at"] = self.probed_at
        if self.details is not None:
            d["details"] = self.details
        if self.flag_diff is not None:
            d["flag_diff"] = self.flag_diff
        return d


class Adapter:
    """Base class for model/agent execution adapters."""

    id: str = "none"
    kind: str = "vendor"  # 'vendor' | 'test_double' | 'none'
    binary_names: List[str] = []
    planned_version: Optional[str] = None
    verified_version: Optional[str] = None
    capabilities: Dict[str, Capability] = {}
    liveness: str = "none"  # 'stream' | 'none'
    max_argv_prompt_bytes: int = 100_000
    #: The exact effort values the vendor CLI accepts; None = no fixed list (checked elsewhere).
    supported_efforts: Optional[Tuple[str, ...]] = None
    launch_enabled: bool = False
    launch_milestone: Optional[str] = "M3"

    def resolve_binary(self, binary_path: Optional[str] = None) -> Optional[str]:
        """Resolve executable binary from explicit path or PATH."""
        if binary_path:
            p = os.path.expanduser(binary_path)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return os.path.abspath(p)
            return None
        env_path = build_external_env().get("PATH")
        for name in self.binary_names:
            resolved = shutil.which(name, path=env_path)
            if resolved:
                if self.id in ("hermes", "openclaw"):
                    # Hermes and OpenClaw are not installed in M1; shims in tests/shims are tripwires
                    real = os.path.realpath(resolved)
                    if "/tests/shims/" in real or real.endswith("/tests/shims/" + name):
                        continue
                return os.path.abspath(resolved)
        return None

    def is_installed(self, binary_path: Optional[str] = None) -> bool:
        """Check if adapter is installed without starting full probe."""
        if self.id in ("none", "fake"):
            return True
        return self.resolve_binary(binary_path) is not None

    def probe(
        self,
        binary_path: Optional[str] = None,
        probed_version: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        timeout_s: float = 5.0,
    ) -> Probe:
        """Inspect adapter availability, version, flags, and capabilities."""
        v = probed_version if probed_version is not None else self.planned_version
        caps: Dict[str, Dict[str, Any]] = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if self.verified_version and v != self.verified_version and cdict["verification"] != "none":
                cdict["verification"] = "stale"
            caps[name] = cdict
        probe = Probe(
            adapter=self.id,
            version=v,
            status="partially_verified",
            launch_enabled=self.launch_enabled,
            launch_milestone=self.launch_milestone,
            capabilities=caps,
            flags=[],
            models=self.list_models(binary_path),
        )
        record_adapter_probe(conn, self.id, probe, binary_path)
        return probe

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        """List models supported by this adapter, if available."""
        return None

    def build(self, req: RunRequest) -> CommandSpec:
        """Construct the execution argv and invocation spec."""
        raise NotImplementedError

    def parse(self, run_dir: str) -> RunResult:
        """Parse execution output and extract deliverable / session info."""
        raise NotImplementedError

    def profile_flags(self, profile: str) -> List[str]:
        """Map permission profile to adapter command line flags."""
        raise ProfileUnsupported(self.id, profile)


DEFAULT_MAX_RUN_FILE_BYTES = 50 * 1024 * 1024 + 65536


def open_run_file_nofollow(
    path: str,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: Optional[int] = DEFAULT_MAX_RUN_FILE_BYTES,
) -> TextIO:
    """Open a regular file inside a run dir safely without following symlinks or blocking on FIFOs."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path or path.endswith(os.sep):
        raise OSError(errno.EINVAL, "Invalid run file path", path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "Refusing non-regular run file", path)
        if max_bytes is not None and st.st_size > max_bytes:
            raise OSError(errno.EFBIG, f"Run file exceeds {max_bytes} bytes", path)
        nonblock = getattr(os, "O_NONBLOCK", 0)
        if nonblock:
            try:
                import fcntl

                fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                if fl & nonblock:
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~nonblock)
            except OSError:
                pass
        return os.fdopen(fd, "r", encoding=encoding, errors=errors)
    except BaseException:
        os.close(fd)
        raise


def read_run_file_nofollow(
    path: str,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: Optional[int] = DEFAULT_MAX_RUN_FILE_BYTES,
) -> str:
    """Read a regular run-dir file safely via ``open_run_file_nofollow`` with a bounded read."""
    with open_run_file_nofollow(path, encoding=encoding, errors=errors, max_bytes=max_bytes) as f:
        if max_bytes is None:
            return f.read()
        text = f.read(max_bytes + 1)
        if len(text) > max_bytes:
            raise OSError(errno.EFBIG, f"Run file exceeds {max_bytes} bytes", path)
        return text
