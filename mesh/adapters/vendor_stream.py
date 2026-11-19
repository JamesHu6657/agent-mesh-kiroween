"""Shared helpers for the JSONL-stream adapters ``codex`` and ``claude``.

Pure: no subprocess, no network. Each adapter module documents the vendor CLI version and
the official sources its event names come from; this module only holds what both share:

* reading ``stdout.log`` line by line (malformed lines are counted, never raised);
* runwrap's log-cap marker (``mesh/runwrap.py``: ``[runwrap] log cap reached; output
  truncated``), the only case reported as ``output_truncated``; a cut last line alone is
  ``incomplete`` (``last_line_malformed`` is kept for diagnostics only);
* mapping vendor error text to a stable failure meaning (``auth_failed``,
  ``quota_exhausted``, ``rate_limited``, ``network_error``);
* treating a ``tests/shims/`` tripwire on PATH as "not installed" (as base.py does for
  hermes/openclaw), so the offline suite never executes a real codex/claude.
"""

import json
import os
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional

from mesh.adapters.base import Probe, open_run_file_nofollow, record_adapter_probe
from mesh.constants import build_external_env
from mesh.core.redact import redact

#: mesh/runwrap.py writes this line once stdout.log reached its cap; the rest is dropped.
LOG_CAP_MARKER = "[runwrap] log cap reached; output truncated"

AUTH_FAILED = "auth_failed"
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
NETWORK_ERROR = "network_error"
RUN_ERROR = "run_error"
OUTPUT_TRUNCATED = "output_truncated"
INCOMPLETE = "incomplete"

#: Lower-case substrings, checked in this order (auth, quota, rate limit, network).
AUTH_MARKERS = (
    "401 unauthorized", "403 forbidden", "missing bearer", "invalid api key", "incorrect api key",
    "invalid_api_key", "invalid x-api-key", "not logged in", "please run /login", "codex login",
    "please log in", "log in again", "refresh token", "token expired", "token has expired",
    "authentication_error", "authentication failed",
)
QUOTA_MARKERS = (
    "usage limit", "hit your limit", "insufficient_quota", "exceeded your current quota",
    "quota exceeded", "credit balance is too low", "out of credits", "spend limit",
    # Prepaid gateways behind ANTHROPIC_BASE_URL / OpenAI-compatible routes (seen on InstaBox,
    # 2026-11-17: "API Error: 400 You have insufficient credits..."; DeepSeek-style "Insufficient Balance").
    "insufficient credit", "insufficient balance",
)
RATE_MARKERS = ("too many requests", "rate limit", "rate_limit", "rate-limit")
#: A bare "429" is only a status when it stands alone next to status words.
RATE_STATUS_RE = re.compile(r"(?:status|http|code)[^0-9a-z]{0,3}429\b|\b429 too many")
NETWORK_MARKERS = (
    "stream disconnected", "stream ended unexpectedly", "error sending request", "connection refused",
    "connection reset", "connection error", "connection closed", "econnrefused", "econnreset",
    "enotfound", "etimedout", "failed to lookup address", "dns error", "network is unreachable",
    "network error", "fetch failed", "request timed out", "connection timed out", "operation timed out",
    "tls handshake",
)

ERROR_TEXT_MAX = 500


def classify_error_text(text: Optional[str]) -> Optional[str]:
    """The failure meaning a vendor error message names, or None when it names none."""
    if not text:
        return None
    lower = text.lower()
    for markers, meaning in ((AUTH_MARKERS, AUTH_FAILED), (QUOTA_MARKERS, QUOTA_EXHAUSTED),
                             (RATE_MARKERS, RATE_LIMITED), (NETWORK_MARKERS, NETWORK_ERROR)):
        if any(m in lower for m in markers):
            return meaning
        if meaning == RATE_LIMITED and RATE_STATUS_RE.search(lower):
            return meaning
    return None


def bounded(text: Optional[str], limit: int = ERROR_TEXT_MAX) -> Optional[str]:
    """Redact first, then cut, so a slice never leaves an unmatchable key prefix."""
    if text is None:
        return None
    text = redact(text)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def new_stream_info() -> Dict[str, Any]:
    return {"exists": False, "lines": 0, "malformed": 0, "last_line_malformed": False, "log_capped": False}


def iter_jsonl_lines(lines: Iterable[str], info: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """JSON objects of a JSONL stream; ``info`` counts lines, malformed lines, a malformed
    last line and runwrap's log-cap marker. Never raises on bad input."""
    last_ok = True
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        info["lines"] += 1
        if line == LOG_CAP_MARKER:
            info["log_capped"] = True
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            info["malformed"] += 1
            last_ok = False
            continue
        if not isinstance(event, dict):
            info["malformed"] += 1
            last_ok = False
            continue
        last_ok = True
        yield event
    info["last_line_malformed"] = not last_ok


def iter_jsonl_file(path: str, info: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    try:
        f = open_run_file_nofollow(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    info["exists"] = True
    with f:
        yield from iter_jsonl_lines(f, info)


def end_of_stream_meaning(info: Dict[str, Any]) -> str:
    """Meaning of a stream that ended without the vendor's completion event: cut by the log
    cap -> ``output_truncated``; otherwise (stopped, killed, cut mid-line, never started)
    -> ``incomplete``."""
    # Only runwrap's log cap means truncated output; a half-written last line alone usually
    # means the process was killed mid-write (OOM, SIGKILL), which stays incomplete.
    if info.get("log_capped"):
        return OUTPUT_TRUNCATED
    return INCOMPLETE


def resolve_on_path(binary_names: List[str]) -> Optional[str]:
    """First ``binary_names`` hit on the external PATH, or None.

    A ``tests/shims/`` tripwire counts as not installed and stops the search (the suite puts
    tests/shims first on PATH, so a real codex/claude later on PATH is never run offline). The
    shim only has to exist: a checkout without its exec bit still hides the real binary."""
    env_path = build_external_env().get("PATH") or ""
    for name in binary_names:
        for entry in env_path.split(os.pathsep):
            if not entry:
                continue
            candidate = os.path.join(entry, name)
            if not os.path.isfile(candidate):
                continue
            real = os.path.realpath(candidate)
            if "/tests/shims/" in real or "/tests/shims/" in os.path.abspath(candidate):
                return None
            if os.access(candidate, os.X_OK):
                return os.path.abspath(candidate)
    return None


def error_probe(adapter, conn, status: str, version: Optional[str], binary_path: Optional[str],
                details: Optional[Dict[str, Any]], verification: str) -> Probe:
    """A probe that stopped early (not installed, failed or unparseable version, failed help)."""
    caps = {name: cap.to_dict() for name, cap in adapter.capabilities.items()}
    for cdict in caps.values():
        cdict["verification"] = verification
    probe = Probe(
        adapter=adapter.id,
        version=version,
        status=status,
        launch_enabled=adapter.launch_enabled,
        launch_milestone=adapter.launch_milestone,
        capabilities=caps,
        flags=[],
        models=None,
        binary_path=binary_path,
        details=details,
    )
    record_adapter_probe(conn, adapter.id, probe, binary_path if status != "not_installed" else None)
    return probe
