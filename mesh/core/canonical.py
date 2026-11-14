"""Canonical JSON and operation hashing per plan §7.4 and acceptance item G22.

Rules:
1. Canonical JSON uses sort_keys=True, separators=(",",":"), ensure_ascii=False.
2. Floats are strictly forbidden in canonical JSON and op params.
3. Excluded keys in params: idempotency_key, dry_run, approval, format, if_version.
4. op_hash = "sha256:" + sha256(canonical).hexdigest()
"""

import hashlib
import json
from typing import Any, Dict, Optional, Set, Tuple

EXCLUDED_OP_KEYS: Set[str] = {
    "idempotency_key",
    "dry_run",
    "approval",
    "format",
    "if_version",
}


def check_no_floats(obj: Any) -> None:
    """Recursively verify that obj contains no float values.

    Raises:
        ValueError: If any float is encountered.
    """
    if isinstance(obj, float):
        raise ValueError(f"Floats are forbidden in canonical JSON; found float value {obj!r}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, float):
                raise ValueError(f"Floats are forbidden in canonical JSON; found float key {k!r}")
            check_no_floats(k)
            check_no_floats(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            check_no_floats(item)


def canonical_json(obj: Any) -> bytes:
    """Serialize obj to deterministic canonical UTF-8 JSON bytes.

    Features:
        - Keys sorted alphabetically.
        - Compact separators without whitespace (',', ':').
        - UTF-8 characters preserved without unicode escapes (ensure_ascii=False).
        - Floats rejected.

    Raises:
        ValueError if obj or any nested element is a float.
    """
    check_no_floats(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def hash_canonical(obj: Any) -> str:
    """Return the SHA-256 hexadecimal digest of canonical_json(obj)."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


class OpHashResult(tuple):
    """Result of compute_op_hash supporting both (op_doc, op_hash) and (op_hash, canonical_bytes)."""
    def __new__(cls, op_doc: Dict[str, Any], op_hash: str, canonical_bytes: bytes, as_canonical_pair: bool = False):
        if as_canonical_pair:
            return super().__new__(cls, (op_hash, canonical_bytes))
        else:
            return super().__new__(cls, (op_doc, op_hash))


def compute_op_hash(
    op: Optional[str] = None,
    requested_by: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    project_id: Optional[str] = None,
    target_id: Optional[str] = None,
    category: Optional[str] = None,
    *,
    cmd: Optional[str] = None,
    project: Optional[str] = None,
    target: Optional[str] = None,
) -> Any:
    """Compute canonical op dictionary and op_hash per plan §7.4.

    Single source of truth for operation hashing. Binds the gate category
    unconditionally under a reserved top-level key in the op document.
    """
    as_canonical_pair = False
    if cmd is not None:
        op = cmd
        as_canonical_pair = True
    if project is not None:
        project_id = project
    if target is not None:
        target_id = target
    if params is None:
        params = {}

    cleaned_params = {
        k: v for k, v in params.items()
        if k not in EXCLUDED_OP_KEYS and k != "category"
    }
    check_no_floats(cleaned_params)

    op_doc: Dict[str, Any] = {
        "v": 1,
        "op": op,
        "requested_by": requested_by,
        "project": project_id,
        "target": target_id,
        "params": cleaned_params,
    }
    if category is not None:
        op_doc["category"] = category

    canonical = canonical_json(op_doc)
    op_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return OpHashResult(op_doc, op_hash, canonical, as_canonical_pair=as_canonical_pair)
