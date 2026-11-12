"""Secret redaction per plan §15.2.

Two layers, applied in this order:

1. Exact values: every live token known to the process, plus env values whose
   names match ``(?i)(key|token|secret|passw|cookie|credential|auth)`` and are at
   least 8 characters. Longest value first, replaced with ``[REDACTED:<name>]``.
2. Patterns: each match is replaced with ``[REDACTED]``.

Callers can pass an explicit value list (runwrap uses the child env it builds)
or rely on the process defaults (env + secrets registered here).
"""

import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SECRET_ENV_NAME_RE = re.compile(r"(?i)(key|token|secret|passw|cookie|credential|auth)")
MIN_EXACT_VALUE_LEN = 8

PATTERN_SOURCES: List[str] = [
    r"mesh_(?:at|rt)_[A-Za-z0-9_-]{20,}",
    r"fk-[A-Za-z0-9_-]{16,}",
    r"sk-[A-Za-z0-9_-]{16,}",
    r"AIza[0-9A-Za-z_-]{35}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"xox[abpr]-[A-Za-z0-9-]{10,}",
    r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*\S+",
]
PATTERNS = [re.compile(source) for source in PATTERN_SOURCES]

_registered_secrets: Dict[str, str] = {}
_env_cache: Optional[Tuple[Tuple[str, str], ...]] = None


def register_secret(name: str, value: str) -> None:
    """Make an in-memory secret (e.g. a run token) known to default redaction."""
    if name and value:
        _registered_secrets[name] = value


def unregister_secret(name: str) -> None:
    _registered_secrets.pop(name, None)


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


def collect_exact_values(
    env: Optional[Mapping[str, str]] = None,
    extra: Optional[Iterable[Tuple[str, str]]] = None,
) -> List[Tuple[str, str]]:
    """Return (name, value) pairs that must be redacted exactly, longest first."""
    source = os.environ if env is None else env
    pairs: List[Tuple[str, str]] = []
    for name, value in source.items():
        if not value or len(value) < MIN_EXACT_VALUE_LEN:
            continue
        if SECRET_ENV_NAME_RE.search(name or ""):
            pairs.append((name, value))
    if extra:
        pairs.extend((name, value) for name, value in extra if name and value)
    pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
    return pairs


def default_exact_values() -> List[Tuple[str, str]]:
    """Env-derived exact values plus secrets registered in this process."""
    global _env_cache
    if _env_cache is None:
        _env_cache = tuple(collect_exact_values())
    extra = list(_registered_secrets.items())
    merged = list(_env_cache) + extra
    merged.sort(key=lambda pair: len(pair[1]), reverse=True)
    return merged


def redact(text: str, exact_values: Optional[Sequence[Tuple[str, str]]] = None) -> str:
    """Redact every exact secret value and pattern in ``text``."""
    if not text:
        return text
    out = text
    pairs = default_exact_values() if exact_values is None else list(exact_values)
    for name, value in sorted(pairs, key=lambda pair: len(pair[1]), reverse=True):
        if value and value in out:
            out = out.replace(value, f"[REDACTED:{name}]")
    for pattern in PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


def redact_value(value: Any, exact_values: Optional[Sequence[Tuple[str, str]]] = None) -> Any:
    """Recursively redact the string leaves of JSON-like data."""
    if isinstance(value, str):
        return redact(value, exact_values)
    if isinstance(value, dict):
        return {key: redact_value(val, exact_values) for key, val in value.items()}
    if isinstance(value, list):
        return [redact_value(item, exact_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, exact_values) for item in value)
    return value
