"""Deterministic ID generation and validation per plan §5.4.

Format: <prefix>_<26 chars>
where 26 chars = 10 hex digits of Unix millisecond time + 16 lowercase base32 chars.
Prefixes: prj tsk run apr agt usr cred evt msg req boot lead mrg crit.
"""

import re
import secrets
import time
from typing import Optional, Set

PREFIXES: Set[str] = {
    "prj",
    "tsk",
    "run",
    "apr",
    "agt",
    "usr",
    "cred",
    "evt",
    "msg",
    "req",
    "boot",
    "lead",
    "mrg",
    "crit",
}

# RFC 4648 lowercase base32 alphabet
BASE32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
ID_PATTERN = re.compile(r"^([a-z]+)_([0-9a-f]{10})([a-z2-7]{16})$")


def generate_id(prefix: str, ts_ms: Optional[int] = None) -> str:
    """Generate an identifier for a given prefix.

    Args:
        prefix: One of the 14 valid prefixes in PREFIXES.
        ts_ms: Optional Unix timestamp in milliseconds. Defaults to current time.

    Returns:
        Formatted ID string: <prefix>_<10 hex digits><16 base32 chars>.
    """
    if prefix not in PREFIXES:
        raise ValueError(f"Invalid ID prefix '{prefix}'; must be one of {sorted(PREFIXES)}")

    if ts_ms is None:
        ts_ms = int(time.time() * 1000)

    # 10 hex digits of Unix millisecond time (lowest 40 bits)
    time_part = f"{(ts_ms & 0xFFFFFFFFFF):010x}"
    random_part = "".join(secrets.choice(BASE32_ALPHABET) for _ in range(16))
    return f"{prefix}_{time_part}{random_part}"


def is_valid_id(val: str, prefix: Optional[str] = None) -> bool:
    """Validate whether val conforms to the Mesh ID specification.

    Args:
        val: String to test.
        prefix: If supplied, requires val to have this specific prefix.

    Returns:
        True if valid, False otherwise.
    """
    if not isinstance(val, str):
        return False

    m = ID_PATTERN.match(val)
    if not m:
        return False

    pfx = m.group(1)
    if pfx not in PREFIXES:
        return False

    if prefix is not None and pfx != prefix:
        return False

    return True


def parse_id(val: str) -> Optional[dict]:
    """Parse an ID into its components (prefix, time_ms, random_suffix).

    Returns:
        Dict with components if valid, or None if invalid.
    """
    if not is_valid_id(val):
        return None

    m = ID_PATTERN.match(val)
    assert m is not None
    return {
        "prefix": m.group(1),
        "time_hex": m.group(2),
        "random": m.group(3),
    }


new_id = generate_id
