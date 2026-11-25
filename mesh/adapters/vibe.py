"""Mistral Vibe CLI adapter per plan §14.6."""

import json
import math
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

from mesh.adapters.base import (
    Adapter,
    Capability,
    CommandSpec,
    EffortUnsupported,
    EXPECTED_HELP_HASHES,
    Probe,
    ProfileUnsupported,
    RunRequest,
    RunResult,
    find_missing_flags,
    is_safe_cli_token,
    open_run_file_nofollow,
    parse_version_output,
    read_run_file_nofollow,
    record_adapter_probe,
    run_readonly_cmd,
    validate_adapter_options,
    validate_run_request_tokens,
)
from mesh.platform import fs as _plat_fs
from mesh.platform import paths as _plat_paths
from mesh.platform import perms as _plat_perms


class VibeAdapter(Adapter):
    """Adapter for Mistral Vibe CLI."""

    id = "vibe"
    kind = "vendor"
    supported_profiles = ("readonly", "edit", "dev", "unrestricted")
    binary_names = ["vibe"]
    planned_version = "3.1.0"
    verified_version = "3.1.0"
    model_presets = {
        "chips": ["mistral-medium-3.5"],
        "default_allowed": ["mistral-medium-3.5"],
        "note": "Vibe 3.1.0 内置别名为 mistral-medium-3.5；其他模型名必须是 ~/.vibe/config.toml 中 [[models]] 定义的别名（可在 vibe 中用 /model 查看）",
    }
    liveness = "stream"
    max_argv_prompt_bytes = 100_000
    launch_enabled = False
    launch_milestone = "M3"

    def __init__(self):
        self.capabilities = {
            "headless": Capability("headless", supported="yes", verification="help", note="vibe --prompt-file"),
            "stream_output": Capability("stream_output", supported="yes", verification="help", note="--output streaming"),
            "model_selection": Capability("model_selection", supported="yes", verification="help", note="--model"),
            "reasoning_effort": Capability("reasoning_effort", supported="no", verification="help", note="no effort flag"),
            "resume": Capability("resume", supported="unknown", verification="help", note="--resume"),
        }

    def profile_flags(self, profile: str) -> List[str]:
        if profile == "readonly":
            return ["--agent", "ask"]
        elif profile == "edit":
            return ["--agent", "accept-edits"]
        elif profile == "dev":
            return ["--agent", "smart-approve"]
        elif profile == "elevated":
            # Unsupported (no evidence that --agent auto-approve differs from --auto-approve)
            raise ProfileUnsupported(self.id, profile)
        elif profile == "unrestricted":
            return ["--agent", "auto-approve", "--auto-approve"]
        raise ProfileUnsupported(self.id, profile)

    def build(self, req: RunRequest) -> CommandSpec:
        validate_run_request_tokens(req)
        validate_adapter_options(self.id, req.options, strict_keys=False)
        if req.effort:
            raise EffortUnsupported(self.id)

        binary = req.options.get("binary") or self.binary_names[0]
        if req.options.get("binary"):
            # Linear full review C9: run the path validation resolved (~, relative), not the raw string.
            binary = self.resolve_binary(binary) or os.path.abspath(os.path.expanduser(binary))
        argv = [
            binary,
            "--prompt-file",
            req.prompt_path,
            "--output",
            "streaming",
            "--workdir",
            req.workspace,
        ]

        # With --resume, omit --model per §14.6
        if req.model and not req.resume_session_id:
            argv.extend(["--model", req.model])

        argv.extend(self.profile_flags(req.profile))

        # Plan §14.6 "argv built": [--model] <profile flags> [--resume <sid>] [--trust]
        if req.resume_session_id:
            argv.extend(["--resume", req.resume_session_id])

        if req.options.get("trust_workdir"):
            argv.append("--trust")

        argv.extend(["--time-limit", str(req.timeout_s)])

        run_dir = os.path.dirname(os.path.abspath(req.prompt_path))
        output_dir = os.path.join(run_dir, "vibe-out")
        argv.extend(["--output-dir", output_dir])

        if "max_turns" in req.options:
            argv.extend(["--max-turns", str(req.options["max_turns"])])
        if "max_price_usd" in req.options:
            argv.extend(["--max-price", str(req.options["max_price_usd"])])
        if "max_tokens" in req.options:
            argv.extend(["--max-tokens", str(req.options["max_tokens"])])

        return CommandSpec(argv=argv, cwd=req.workspace, stdin="devnull")

    def parse(self, run_dir: str) -> RunResult:
        # vibe 3.1.0 `--output streaming --output-dir <run_dir>/vibe-out`
        # (tests/fixtures/real/vibe/3.1.0/):
        #   streaming-terminated.jsonl  one JSON object per line, each with "sessionId":
        #     {"type":"message","role":"user","content":[{"type":"text",...}]}       -> never used
        #     {"type":"message","role":"assistant","content":[{"type":"text",...}]}  -> final_text
        #     {"type":"effect","title":"file_system.bash","detail":{...}}           -> never used
        #   export-terminated.json  (vibe-out/export.json, schema_version 1)
        #     session_id, outcome, stop_reason, error, usage{...}, cost_usd, steps,
        #     exit_code, config.active_model                       -> session_id, usage, meaning
        # final_text is the text parts of the last assistant message, "" when that message has
        # no text (no fallback to an older one); without any assistant message it is "" and the
        # supervisor falls back to the raw stdout.log. Without an export and without any
        # assistant message, vendor_exit_meaning is run_error and usage is
        # {"error": "no export"} ({"error": "export not an object"} when export.json is valid
        # JSON but not an object) plus usage["stderr_tail"] (last non-empty stderr.log line,
        # capped to its last 500 chars) when present. With an assistant message but no export, usage and meaning stay None
        # (the stream already has model text; a missing export is not classified as a failed
        # run) and session_id comes from the stream.
        scan = _scan_stream(os.path.join(run_dir, "stdout.log"))
        vibe_out_dir = os.path.join(run_dir, "vibe-out")
        _redact_output_dir(vibe_out_dir)
        export = _read_export(os.path.join(vibe_out_dir, "export.json"))
        session_id = None
        usage = None
        meaning = None
        not_object = export is _EXPORT_NOT_OBJECT
        if not_object:
            export = None
        if export is not None:
            sid = export.get("session_id")
            if is_safe_cli_token(sid):
                session_id = sid
            usage = _export_usage(export)
            meaning = _exit_meaning(export)
        elif not scan["saw_assistant"]:
            meaning = "run_error"
            usage = {"error": "export not an object" if not_object else "no export"}
            tail = _stderr_tail(os.path.join(run_dir, "stderr.log"))
            if tail is not None:
                usage["stderr_tail"] = tail
        return RunResult(
            final_text=scan["final_text"] or "",
            session_id=session_id or scan["session_id"],
            usage=usage,
            vendor_exit_meaning=meaning,
        )

    @staticmethod
    def stream_facts(stdout_text: str) -> Dict[str, Any]:
        """Smoke-facing facts from a vibe 3.1.0 ``--output streaming`` text; never raises.

        ``session_id``: the first non-empty ``sessionId`` on any line (every line of
        streaming-terminated.jsonl carries it). The stream shows no model, effort or cwd (the
        model alias is only in ``vibe-out/export.json`` ``config.active_model``; vibe has no
        effort flag), so ``model``, ``effort`` and ``cwd`` are None.

        ``stream_error`` / ``vendor_exit_meaning`` are always None: the vibe 3.1.0 stream has
        no run-level error event (the outcome / error live only in ``vibe-out/export.json``,
        which ``parse()`` reads; a ``failed`` effect is a tool step, e.g. a denial, not a run
        error). The keys exist so S-X-6 and the S-X-2/4/5 vendor-error checks treat every
        adapter alike."""
        facts: Dict[str, Any] = {"session_id": None, "model": None, "effort": None, "cwd": None,
                                 "stream_error": None, "vendor_exit_meaning": None}
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
            sid = event.get("sessionId")
            if is_safe_cli_token(sid):
                facts["session_id"] = sid
                break
        return facts

    def list_models(self, binary_path: Optional[str] = None) -> Optional[List[str]]:
        # Vibe has no model-list command per §14.6 and N7
        return None

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

        # 1. Version check: vibe --version
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

        # 2. Help check: vibe --help
        res_h = run_readonly_cmd([bin_path, "--help"], timeout_s=timeout_s)
        if res_h.returncode != 0:
            caps = {name: c.to_dict() for name, c in self.capabilities.items()}
            for cdict in caps.values():
                cdict["verification"] = "none"
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

        expected_help = EXPECTED_HELP_HASHES.get("vibe")
        is_stale = (
            (self.verified_version and v != self.verified_version)
            or (expected_help and res_h.help_sha256 != expected_help)
        )

        caps = {}
        for name, cap in self.capabilities.items():
            cdict = cap.to_dict()
            if name == "model_selection":
                # Prompt: "vibe: only if its help documents a read-only model list, else models = null with status unverified"
                cdict["verification"] = "unverified"
                cdict["note"] = "no read-only model list command; user must supply allowed models"
            elif is_stale and cdict["verification"] != "none":
                cdict["verification"] = "stale"
            caps[name] = cdict

        flags = [
            "--prompt-file",
            "--output",
            "--workdir",
            "--model",
            "--resume",
            "--agent",
            "--auto-approve",
            "--trust",
            "--time-limit",
            "--output-dir",
            "--max-turns",
            "--max-price",
            "--max-tokens",
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
            models=None,
            binary_path=bin_path,
            help_sha256=res_h.help_sha256,
            flag_diff=flag_diff,
        )
        record_adapter_probe(conn, self.id, probe, bin_path)
        return probe


# export.json "outcome" values handled (schema_version 1). Only "terminated" is in a real
# capture (export-terminated.json, stop_reason "interrupted", exit_code 3); the others follow
# the m3-vibeparse task spec. outcome "error" is run_error even when export.error is null or
# missing. An outcome not in this table (or missing / not a string) falls back on stop_reason
# via _STOP_REASON_MEANING; the raw outcome stays in usage["outcome"].
_OUTCOME_MEANING = {
    "completed": None,
    "error": "run_error",
    "turn_limit": "turn_limit",
    "terminated": "terminated",
    "interrupted": "terminated",
}
# stop_reason fallback for unknown outcomes. "limit" maps to the generic "limit", not
# "turn_limit": --max-price / --max-tokens stops report stop_reason "limit" as well.
# Any other stop_reason -> None.
_STOP_REASON_MEANING = {
    "interrupted": "terminated",
    "limit": "limit",
}
_EXPORT_SCALARS = ("cost_usd", "steps", "outcome", "stop_reason", "exit_code")


_EXPORT_NOT_OBJECT = object()
_MAX_VIBE_FILE_BYTES = 50 * 1024 * 1024


def _unlink_nonregular(path: str) -> None:
    """Remove an unexpected file, symlink, or NTFS directory junction."""
    _plat_fs.unlink_nonregular(path)


def _redact_output_dir(out_dir: str) -> None:
    """Full review r5 P1-4 / r6 P1-5: vibe writes ``--output-dir`` itself (not through
    runwrap), so all files under it are redacted recursively in place with full JSONL
    rules; symlinks/junctions (including ``vibe-out`` itself) and unrewritable files are unlinked."""
    import stat
    import tempfile
    from mesh.core.paths import resolve_trusted_path
    from mesh.core.redact import redact_json_text

    if not isinstance(out_dir, str) or not out_dir.strip():
        return
    out_clean = _plat_paths.strip_trailing_seps(out_dir)
    if not out_clean or _plat_paths.is_fs_root(out_clean):
        return
    try:
        st_top = os.lstat(out_clean)
    except FileNotFoundError:
        return
    except OSError:
        return
    if _plat_paths.is_reparse_stat(st_top) or not stat.S_ISDIR(st_top.st_mode):
        try:
            _unlink_nonregular(out_clean)
        except OSError:
            pass
        return
    parent_real = os.path.realpath(os.path.dirname(out_clean) or ".")
    trusted_out = resolve_trusted_path(
        os.path.join(parent_real, os.path.basename(out_clean)),
        root=parent_real,
    )
    if trusted_out is None:
        return
    try:
        _plat_perms.set_private_dir_acl(trusted_out, 0o700)
    except OSError:
        pass
    for dirpath, dirnames, _ in os.walk(trusted_out, topdown=True, followlinks=False):
        if not _plat_paths.same_path(dirpath, trusted_out) and resolve_trusted_path(dirpath, root=trusted_out) is None:
            dirnames[:] = []
            continue
        for dname in list(dirnames):
            dpath = os.path.join(dirpath, dname)
            if _plat_paths.is_reparse_link(dpath):
                try:
                    _unlink_nonregular(dpath)
                except OSError:
                    pass
                dirnames.remove(dname)
                continue
            try:
                _plat_perms.set_private_dir_acl(dpath, 0o700)
            except OSError:
                pass
    for dirpath, dirnames, filenames in os.walk(trusted_out, topdown=False, followlinks=False):
        if not _plat_paths.same_path(dirpath, trusted_out) and resolve_trusted_path(dirpath, root=trusted_out) is None:
            continue
        for dname in dirnames:
            dpath = os.path.join(dirpath, dname)
            try:
                if _plat_paths.is_reparse_link(dpath):
                    _unlink_nonregular(dpath)
            except OSError:
                pass
        for name in filenames:
            path = os.path.join(dirpath, name)
            tmp = None
            try:
                lst = os.lstat(path)
                if _plat_paths.is_reparse_stat(lst) or not stat.S_ISREG(lst.st_mode):
                    _unlink_nonregular(path)
                    continue
                if lst.st_size > _MAX_VIBE_FILE_BYTES:
                    os.unlink(path)
                    continue
                with open_run_file_nofollow(
                    path, encoding="utf-8-sig", errors="replace", max_bytes=_MAX_VIBE_FILE_BYTES
                ) as f:
                    raw = f.read(_MAX_VIBE_FILE_BYTES + 1)
                if len(raw) > _MAX_VIBE_FILE_BYTES:
                    os.unlink(path)
                    continue
                red = redact_json_text(raw)
                if red != raw:
                    tfd, tmp = tempfile.mkstemp(dir=dirpath, prefix=f".{name}.redact.")
                    with os.fdopen(tfd, "w", encoding="utf-8") as wf:
                        wf.write(red)
                    _plat_perms.chmod_private(tmp, 0o600)
                    if _plat_paths.is_reparse_link(path):
                        os.unlink(tmp)
                        tmp = None
                        _unlink_nonregular(path)
                        continue
                    _plat_fs.atomic_replace(tmp, path)
                    tmp = None
            except Exception:  # noqa: BLE001 - fail closed
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                try:
                    _plat_perms.set_private_dir_acl(dirpath, 0o700)
                    _unlink_nonregular(path)
                except OSError:
                    try:
                        _plat_perms.chmod_private(path, 0o600)
                        if sys.platform == "win32":
                            try:
                                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                            except OSError:
                                pass
                        tfd = _plat_fs.open_regular_nofollow(
                            path, os.O_WRONLY | os.O_TRUNC, label="vibe output file"
                        )
                        os.close(tfd)
                    except OSError:
                        pass


def _read_export(path: str) -> Any:
    """vibe-out/export.json as a dict; None if missing, unreadable or not valid JSON;
    _EXPORT_NOT_OBJECT if it is valid JSON but not an object.

    Opened as utf-8-sig so a leading BOM is stripped. Symlinks/junctions and non-regular files are rejected.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    clean = _plat_paths.strip_trailing_seps(path)
    if not clean or _plat_paths.is_fs_root(clean):
        return None
    parent = os.path.dirname(clean)
    if (parent and _plat_paths.is_reparse_link(parent)) or _plat_paths.is_reparse_link(clean):
        return None
    try:
        with open_run_file_nofollow(
            clean, encoding="utf-8-sig", errors="replace", max_bytes=_MAX_VIBE_FILE_BYTES
        ) as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else _EXPORT_NOT_OBJECT


def _export_usage(export: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy of export ``usage`` plus top-level cost_usd, steps, outcome, stop_reason,
    exit_code and ``model`` (= ``config.active_model``, the alias vibe ran with)."""
    usage: Dict[str, Any] = {}
    raw = export.get("usage")
    if isinstance(raw, dict):
        usage.update(raw)
    for key in _EXPORT_SCALARS:
        if key in export:
            usage[key] = export[key]
    config = export.get("config")
    model = config.get("active_model") if isinstance(config, dict) else None
    if isinstance(model, str) and model:
        usage["model"] = model
    return _none_nonfinite_numbers(usage)


def _exit_meaning(export: Dict[str, Any]) -> Optional[str]:
    """``run_error`` if export.error is set (anything except None / "" / false / 0 / {} / [],
    see _export_error_set; so true and 1 are errors), or outcome is ``error`` (error
    null/missing/false/0 too); else the outcome mapping in
    _OUTCOME_MEANING; for an unknown outcome, the stop_reason mapping in
    _STOP_REASON_MEANING (else None)."""
    if _export_error_set(export.get("error")):
        return "run_error"
    outcome = export.get("outcome")
    if isinstance(outcome, str) and outcome in _OUTCOME_MEANING:
        return _OUTCOME_MEANING[outcome]
    stop_reason = export.get("stop_reason")
    if isinstance(stop_reason, str):
        return _STOP_REASON_MEANING.get(stop_reason)
    return None


def _message_text(event: Dict[str, Any]) -> str:
    content = event.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


_NONFINITE_MAX_DEPTH = 32


def _none_nonfinite(value: Any, depth: int = 0) -> Any:
    """``value`` with NaN / Infinity floats replaced by None, recursing into dicts and lists.

    Recursion stops at _NONFINITE_MAX_DEPTH nested containers: a container below that depth is
    returned unchanged (non-finite numbers in it are left as-is), so deep nesting never raises.
    Containers are copied, never mutated.
    """
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if depth >= _NONFINITE_MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _none_nonfinite(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_none_nonfinite(v, depth + 1) for v in value]
    return value


def _none_nonfinite_numbers(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Replace NaN / Infinity floats in usage with None (json.load accepts them), recursively
    in nested dicts / lists up to _NONFINITE_MAX_DEPTH levels (see _none_nonfinite)."""
    return _none_nonfinite(usage)


def _export_error_set(value: Any) -> bool:
    """False only for None, "", false, 0, {} and [] (no error); anything else is an error:
    true, 1, any other number, a non-empty str / dict / list, or any other type."""
    if value is None or value is False:
        return False
    if isinstance(value, (str, dict, list)):
        return len(value) > 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return True


def _stderr_tail(path: str) -> Optional[str]:
    """Last non-empty line of stderr.log (its last 500 chars), or None if missing/empty/unreadable."""
    try:
        raw = read_run_file_nofollow(path, encoding="utf-8-sig", errors="replace")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    tail = lines[-1]
    # Cap at _STDERR_TAIL_MAX chars, keeping the end (the error is usually last)
    return tail[-_STDERR_TAIL_MAX:]


_STDERR_TAIL_MAX = 500


def _scan_stream(path: str) -> Dict[str, Any]:
    """One pass over a vibe ``--output streaming`` file; never raises.

    Opened as utf-8-sig so a leading BOM is stripped.
    ``session_id``: the first non-empty string ``sessionId`` on any line. ``final_text``: the
    joined, stripped text parts of the LAST ``message`` line with role ``assistant``, even when
    that text is empty or the message has no text parts (then ""); an older non-empty assistant
    message is never used instead. A truncated (unparseable) last assistant line is skipped,
    so ``final_text`` stays that of the previous complete assistant message. User messages and
    ``effect`` tool lines are ignored; a ``thinking`` content part is not text.
    ``saw_assistant``: at least one complete assistant message was read.
    ``malformed``: non-blank lines that are not valid JSON (e.g. a truncated last line).
    """
    empty = {"session_id": None, "final_text": None, "malformed": 0, "saw_assistant": False}
    scan: Dict[str, Any] = dict(empty)
    try:
        with open_run_file_nofollow(path, encoding="utf-8-sig", errors="replace") as f:
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
                sid = event.get("sessionId")
                if scan["session_id"] is None and is_safe_cli_token(sid):
                    scan["session_id"] = sid
                if event.get("type") == "message" and event.get("role") == "assistant":
                    scan["saw_assistant"] = True
                    scan["final_text"] = _message_text(event).strip()
    except OSError:
        return dict(empty)
    return scan
