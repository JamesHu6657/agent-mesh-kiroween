"""Capture writer and promotion for real fixtures and smoke results (m3-r1; ACC: N21).

Probe and smoke tiers write into a *staging* directory (``/tmp/mh-*``) with
:func:`write_capture`. Only the coordinator promotes a reviewed staging tree into the
repository with :func:`promote` (or ``python3 -m tests.tierlib promote --from S --to R``).

Staging layout -> destination:

* ``<staging>/<adapter>/<version>/<file>``  -> ``<repo>/tests/fixtures/real/<adapter>/<version>/<file>``
* ``<staging>/results/<file>``               -> ``<repo>/tests/smoke/results/<file>``

Every data file has a sidecar ``<file>.meta.json`` holding exactly
``{"command", "captured_at_utc", "sha256"}``. Nothing is ever overwritten (``O_EXCL``),
``synthetic_*`` names are refused, and no other destination is reachable.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Dict, List, Sequence, Tuple

SIDECAR_SUFFIX = ".meta.json"
SIDECAR_KEYS = frozenset({"command", "captured_at_utc", "sha256"})
RESULTS_DIR = "results"

_ADAPTER_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?\Z")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class TierError(Exception):
    """A refused write or promotion; nothing was written for the refused path."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _check_rel(rel_path: str) -> List[str]:
    """Validate a staging-relative path and return its parts.

    Accepted: ``<adapter>/<version>/<name>`` or ``results/<name>``; every part a plain
    name; no ``synthetic_*`` part; no sidecar name as data."""
    if not isinstance(rel_path, str) or not rel_path or os.path.isabs(rel_path) or "\\" in rel_path:
        raise TierError(f"refused path {rel_path!r}: must be relative")
    parts = rel_path.split("/")
    for p in parts:
        if p in ("", ".", "..") or not _NAME_RE.match(p):
            raise TierError(f"refused path {rel_path!r}: bad component {p!r}")
        if p.startswith("synthetic_"):
            raise TierError(f"refused path {rel_path!r}: synthetic_* names never enter real trees")
    if parts[-1].endswith(SIDECAR_SUFFIX):
        raise TierError(f"refused path {rel_path!r}: sidecar names are reserved")
    if parts[0] == RESULTS_DIR:
        if len(parts) != 2:
            raise TierError(f"refused path {rel_path!r}: results files sit directly under results/")
    elif len(parts) != 3 or not _ADAPTER_RE.match(parts[0]) or not _VERSION_RE.match(parts[1]):
        raise TierError(f"refused path {rel_path!r}: expected <adapter>/<version>/<file> or results/<file>")
    return parts


def _excl_write(path: str, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        raise TierError(f"refused: {path} already exists")
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def sidecar_bytes(command: Sequence[str], data: bytes, captured_at_utc: str = "") -> bytes:
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(c, str) for c in command):
        raise TierError("command must be a non-empty list of strings")
    meta = {"command": list(command), "captured_at_utc": captured_at_utc or _now_utc(),
            "sha256": sha256_bytes(data)}
    return (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_capture(staging: str, rel_path: str, data: bytes, command: Sequence[str]) -> str:
    """Write ``data`` at ``<staging>/<rel_path>`` plus its sidecar, both ``O_EXCL``.

    ``command`` is the argv that produced the data (binary *name*, not its path).
    Returns the data file path."""
    if not isinstance(data, (bytes, bytearray)):
        raise TierError("data must be bytes")
    if not os.path.isabs(staging):
        raise TierError(f"refused staging {staging!r}: must be absolute")
    _check_rel(rel_path)
    path = os.path.join(staging, *rel_path.split("/"))
    side = path + SIDECAR_SUFFIX
    if os.path.lexists(path) or os.path.lexists(side):
        raise TierError(f"refused: {path} (or its sidecar) already exists")
    meta = sidecar_bytes(command, bytes(data))
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    _excl_write(path, bytes(data))
    _excl_write(side, meta)
    return path


def read_sidecar(data_path: str) -> Dict[str, object]:
    """Load and validate ``<data_path>.meta.json`` against the data file."""
    side = data_path + SIDECAR_SUFFIX
    if not os.path.isfile(side) or os.path.islink(side):
        raise TierError(f"missing sidecar for {data_path}")
    with open(side, "rb") as f:
        try:
            meta = json.loads(f.read().decode("utf-8"))
        except ValueError as exc:
            raise TierError(f"bad sidecar {side}: {exc}")
    if not isinstance(meta, dict) or set(meta) != SIDECAR_KEYS:
        raise TierError(f"bad sidecar {side}: keys must be exactly {sorted(SIDECAR_KEYS)}")
    cmd = meta["command"]
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        raise TierError(f"bad sidecar {side}: command must be a non-empty list of strings")
    if not isinstance(meta["captured_at_utc"], str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["captured_at_utc"]):
        raise TierError(f"bad sidecar {side}: captured_at_utc must be YYYY-MM-DDTHH:MM:SSZ")
    with open(data_path, "rb") as f:
        actual = sha256_bytes(f.read())
    if meta["sha256"] != actual:
        raise TierError(f"sha256 mismatch for {data_path}: sidecar {meta['sha256']}, file {actual}")
    return meta


def _destination(repo_root: str, parts: List[str]) -> str:
    if parts[0] == RESULTS_DIR:
        return os.path.join(repo_root, "tests", "smoke", "results", parts[1])
    return os.path.join(repo_root, "tests", "fixtures", "real", *parts)


def plan_promotion(staging: str, repo_root: str) -> List[Tuple[str, str]]:
    """Validate a whole staging tree; return ``[(src, dst)]`` for data files. Raises on any
    problem, before anything is written."""
    if os.path.islink(staging.rstrip(os.sep) or staging):
        raise TierError(f"refused symlinked staging dir {staging}")
    staging = os.path.realpath(staging)
    repo_root = os.path.realpath(repo_root)
    if not os.path.isdir(staging):
        raise TierError(f"no staging dir {staging}")
    if not os.path.isdir(os.path.join(repo_root, "tests", "fixtures", "real")):
        raise TierError(f"{repo_root} is not a Mesh checkout (no tests/fixtures/real)")
    if repo_root == staging or staging.startswith(repo_root + os.sep):
        raise TierError("staging must be outside the repository")
    pairs: List[Tuple[str, str]] = []
    sidecars = set()
    for dirpath, dirnames, filenames in os.walk(staging):
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                raise TierError(f"refused symlink {os.path.join(dirpath, d)}")
        for name in filenames:
            src = os.path.join(dirpath, name)
            if os.path.islink(src):
                raise TierError(f"refused symlink {src}")
            rel = os.path.relpath(src, staging).replace(os.sep, "/")
            if name.endswith(SIDECAR_SUFFIX):
                sidecars.add(rel)
                continue
            parts = _check_rel(rel)
            read_sidecar(src)
            dst = _destination(repo_root, parts)
            if os.path.lexists(dst) or os.path.lexists(dst + SIDECAR_SUFFIX):
                raise TierError(f"refused: {dst} already exists")
            _check_dest_parent(repo_root, dst)
            pairs.append((src, dst))
    orphans = sidecars - {os.path.relpath(s, staging).replace(os.sep, "/") + SIDECAR_SUFFIX for s, _ in pairs}
    if orphans:
        raise TierError(f"sidecar without data file: {sorted(orphans)[0]}")
    if not pairs:
        raise TierError(f"nothing to promote in {staging}")
    return sorted(pairs)


def _check_dest_parent(repo_root: str, dst: str) -> None:
    """The destination's real parent must lie under the real fixtures/real or smoke/results."""
    parent = os.path.realpath(os.path.dirname(dst))
    allowed = [os.path.realpath(os.path.join(repo_root, "tests", "fixtures", "real")),
               os.path.realpath(os.path.join(repo_root, "tests", "smoke", "results"))]
    if not any(parent == a or parent.startswith(a + os.sep) for a in allowed):
        raise TierError(f"refused: {dst} resolves outside the allowed trees ({parent})")


def promote(staging: str, repo_root: str) -> List[str]:
    """Copy every validated staging file and its sidecar into the repository (``O_EXCL``),
    then re-verify each sha256 at the destination. Returns the destination data paths."""
    pairs = plan_promotion(staging, repo_root)
    repo_root = os.path.realpath(repo_root)
    out: List[str] = []
    for src, dst in pairs:
        with open(src, "rb") as f:
            data = f.read()
        with open(src + SIDECAR_SUFFIX, "rb") as f:
            meta = f.read()
        _check_dest_parent(repo_root, dst)  # before makedirs: no dir is created outside
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _check_dest_parent(repo_root, dst)
        _excl_write(dst, data)
        _excl_write(dst + SIDECAR_SUFFIX, meta)
        read_sidecar(dst)
        out.append(dst)
    return out


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tests.tierlib")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote", help="promote a reviewed staging tree (coordinator only)")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    args = parser.parse_args(list(argv))
    try:
        written = promote(args.src, args.dst)
    except TierError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "written": written}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
