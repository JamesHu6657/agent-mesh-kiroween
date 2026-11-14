"""Smoke results schema and case-to-capability map (plan §14.10; m3-r2; ACC: N12, N20).

Pure: no subprocess, no network. ``validate`` and ``load_latest`` read files only.

Decisions: Q2 (launch cases S-X-1, S-X-6), Q4 (file names), Q13 (smoke model).
"""

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = "mesh.smoke.v1"
STATUSES = ("pass", "fail", "observed", "not_applicable", "not_run", "blocked")

# Existing capability keys only (droid.py / kiro.py capabilities); no new keys.
CASE_CAPS: Dict[str, Tuple[str, ...]] = {
    "S-X-1": ("headless", "stream_output"),
    "S-X-2": ("resume",),
    "S-X-3": ("resume",),
    "S-X-4": ("model_selection",),
    "S-X-5": ("reasoning_effort",),
    "S-X-6": (),  # launch gate only
    "S-X-7": (),
    "S-DROID-8": (),
    "S-KIRO-8": (),
    "S-VIBE-8": (),  # m3-smoke-agy-vibe: vibe shell under readonly (observation)
    "S-VIBE-9": (),
    "S-AGY-1": (),
    "S-AGY-8": (),
}
LAUNCH_CASES = ("S-X-1", "S-X-6")
CASE_IDS = tuple(CASE_CAPS)

DOC_KEYS = ("schema", "adapter", "binary", "version", "captured_at_utc", "mesh_commit", "model", "effort",
            "profile", "timeout_s", "consent_ref", "cases")
CASE_KEYS = ("id", "status", "reason", "rc", "duration_s", "fixture", "fixture_sha256", "observation",
             "evaluated_from", "carried_from")
SIDECAR_SUFFIX = ".meta.json"
SIDECAR_KEYS = frozenset({"command", "captured_at_utc", "sha256"})
FIXTURE_PREFIX = "tests/fixtures/real/"

_TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_DATE_RE = re.compile(r"\d{4}-\d\d-\d\d\Z")
_VERSION_RE = re.compile(r"\d+(?:\.\d+)*\Z")
_ADAPTER_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


_FIXTURE_NAME_RE = re.compile(r"smoke-S-[A-Z0-9-]+-stdout\.jsonl\Z")


def version_ok(version: str) -> bool:
    """The results schema's version rule (numeric dotted), checked before any spawn."""
    return isinstance(version, str) and bool(_VERSION_RE.match(version))


def capabilities_used(adapter) -> List[str]:
    """"Every capability Mesh uses" = every key with ``supported == "yes"``, sorted."""
    return sorted(k for k, c in adapter.capabilities.items() if getattr(c, "supported", None) == "yes")


def cases_for_capability(cap: str) -> List[str]:
    return [cid for cid, caps in CASE_CAPS.items() if cap in caps]


def fixture_rel(adapter: str, version: str, name: str) -> str:
    """The repo-relative fixture path a results doc records."""
    return f"{FIXTURE_PREFIX}{adapter}/{version}/{name}"


def _sha_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _check_sidecar(path: str) -> Optional[str]:
    side = path + SIDECAR_SUFFIX
    if not os.path.isfile(side) or os.path.islink(side):
        return "missing sidecar"
    try:
        with open(side, "rb") as f:
            meta = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return f"unreadable sidecar ({exc})"
    if not isinstance(meta, dict) or set(meta) != SIDECAR_KEYS:
        return "sidecar keys must be exactly command, captured_at_utc, sha256"
    cmd = meta.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
        return "sidecar command must be a non-empty list of strings"
    if not isinstance(meta.get("captured_at_utc"), str) or not _TS_RE.match(meta["captured_at_utc"]):
        return "sidecar captured_at_utc must be YYYY-MM-DDTHH:MM:SSZ"
    actual = _sha_file(path)
    if meta.get("sha256") != actual:
        return f"sidecar sha256 {meta.get('sha256')} != file {actual}"
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(doc: Any, repo_root: str, fixture_root: Optional[str] = None) -> List[str]:
    """Every problem with a results document (empty list = valid).

    ``fixture`` values must be ``tests/fixtures/real/<adapter>/<version>/<name>``; the file
    is looked up under ``fixture_root`` (a staging dir) or ``<repo_root>/tests/fixtures/real``
    and must carry a matching sidecar. ``synthetic_*`` basenames are refused everywhere."""
    errs: List[str] = []
    if not isinstance(doc, dict):
        return ["document is not an object"]
    missing = [k for k in DOC_KEYS if k not in doc]
    extra = sorted(set(doc) - set(DOC_KEYS))
    if missing:
        errs.append(f"missing fields: {missing}")
    if extra:
        errs.append(f"unknown fields: {extra}")
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}")
    adapter, version = doc.get("adapter"), doc.get("version")
    if not isinstance(adapter, str) or not _ADAPTER_RE.match(adapter):
        errs.append("adapter must be a lowercase id")
        adapter = None
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        errs.append("version must be numeric dotted")
        version = None
    for key in ("binary", "mesh_commit", "model", "consent_ref"):
        if not isinstance(doc.get(key), str) or not doc.get(key):
            errs.append(f"{key} must be a non-empty string")
    if isinstance(doc.get("binary"), str) and not os.path.isabs(doc["binary"]):
        errs.append("binary must be absolute")
    if doc.get("model") == "auto":
        errs.append("model 'auto' is refused")
    if not isinstance(doc.get("captured_at_utc"), str) or not _TS_RE.match(doc.get("captured_at_utc", "")):
        errs.append("captured_at_utc must be YYYY-MM-DDTHH:MM:SSZ")
    if doc.get("effort") is not None and not (isinstance(doc["effort"], str) and doc["effort"]):
        errs.append("effort must be a string or null")
    if doc.get("profile") != "readonly":
        errs.append("profile must be 'readonly'")
    if not _is_num(doc.get("timeout_s")) or doc.get("timeout_s", 0) <= 0:
        errs.append("timeout_s must be a positive number")
    cases = doc.get("cases")
    if not isinstance(cases, list):
        errs.append("cases must be a list")
        return errs
    root = os.path.abspath(fixture_root) if fixture_root else os.path.join(os.path.abspath(repo_root), "tests", "fixtures", "real")
    seen = set()
    for i, case in enumerate(cases):
        where = f"cases[{i}]"
        if not isinstance(case, dict):
            errs.append(f"{where} is not an object")
            continue
        cid = case.get("id")
        where = f"cases[{i}] {cid!r}"
        if not isinstance(cid, str):
            errs.append(f"{where}: case id must be a string")
            continue
        cmissing = [k for k in CASE_KEYS if k not in case]
        cextra = sorted(set(case) - set(CASE_KEYS))
        if cmissing:
            errs.append(f"{where}: missing fields {cmissing}")
        if cextra:
            errs.append(f"{where}: unknown fields {cextra}")
        if cid not in CASE_CAPS:
            errs.append(f"{where}: unknown case id")
        elif cid in seen:
            errs.append(f"{where}: duplicate case id")
        seen.add(cid)
        status = case.get("status")
        if status not in STATUSES:
            errs.append(f"{where}: status must be one of {list(STATUSES)}")
        if status != "pass" and status is not None and status in STATUSES and status != "observed" and not case.get("reason"):
            errs.append(f"{where}: status {status} needs a reason")
        for key in ("reason", "observation"):
            if case.get(key) is not None and not isinstance(case.get(key), (str, dict)):
                errs.append(f"{where}: {key} must be a string, object or null")
        for key in ("evaluated_from", "carried_from"):
            if case.get(key) is not None and not isinstance(case.get(key), str):
                errs.append(f"{where}: {key} must be a string or null")
        if case.get("rc") is not None and (not isinstance(case["rc"], int) or isinstance(case["rc"], bool)):
            errs.append(f"{where}: rc must be an int or null")
        if not _is_num(case.get("duration_s")) or case.get("duration_s", -1) < 0:
            errs.append(f"{where}: duration_s must be a number >= 0")
        for key in ("evaluated_from", "carried_from"):
            v = case.get(key)
            if isinstance(v, str) and os.path.basename(v).startswith("synthetic_"):
                errs.append(f"{where}: {key} names a synthetic_* file")
        fx, fsha = case.get("fixture"), case.get("fixture_sha256")
        if fx is None:
            if fsha is not None:
                errs.append(f"{where}: fixture_sha256 without fixture")
            if status == "pass" and cid in ("S-X-1",):
                errs.append(f"{where}: a passing S-X-1 needs a fixture")
            continue
        if not isinstance(fx, str):
            errs.append(f"{where}: fixture must be a string or null")
            continue
        if os.path.basename(fx).startswith("synthetic_") or "/synthetic/" in "/" + fx:
            errs.append(f"{where}: fixture is synthetic: {fx}")
            continue
        parts = fx.split("/")
        if (not fx.startswith(FIXTURE_PREFIX) or len(parts) != 6 or any(p in ("", ".", "..") for p in parts)
                or adapter is None or version is None or parts[3] != adapter or parts[4] != version):
            errs.append(f"{where}: fixture outside {FIXTURE_PREFIX}{adapter}/{version}/: {fx}")
            continue
        if not _FIXTURE_NAME_RE.match(parts[5]):
            errs.append(f"{where}: fixture must be a smoke-S-*-stdout.jsonl capture: {fx}")
            continue
        path = os.path.join(root, adapter, version, parts[5])
        if not os.path.isfile(path) or os.path.islink(path):
            errs.append(f"{where}: fixture file missing: {fx}")
            continue
        problem = _check_sidecar(path)
        if problem:
            errs.append(f"{where}: {fx}: {problem}")
        actual = _sha_file(path)
        if fsha != actual:
            errs.append(f"{where}: fixture_sha256 {fsha} != file {actual}")
    return errs


def _name_re(adapter: str) -> "re.Pattern[str]":
    return re.compile(re.escape(adapter) + r"-(\d+(?:\.\d+)*)-(\d{4}-\d\d-\d\d)(?:-(\d+))?\.json\Z")


def parse_results_name(adapter: str, name: str) -> Optional[Tuple[str, str, int]]:
    """``(version, date, suffix)`` for ``<adapter>-<version>-<date>[-N].json`` (suffix 1 if none)."""
    m = _name_re(adapter).match(name)
    if not m:
        return None
    suffix = int(m.group(3)) if m.group(3) else 1
    if m.group(3) is not None and suffix < 2:
        return None
    return m.group(1), m.group(2), suffix


def results_name(adapter: str, version: str, date: str, existing: Iterable[str]) -> str:
    """Q4: ``<adapter>-<version>-<UTC date>.json``; collisions ``-2``, ``-3``, ..."""
    if not _ADAPTER_RE.match(adapter or "") or not _VERSION_RE.match(version or "") or not _DATE_RE.match(date or ""):
        raise ValueError(f"bad results name parts: {adapter!r} {version!r} {date!r}")
    taken = set(existing)
    base = f"{adapter}-{version}-{date}"
    if f"{base}.json" not in taken:
        return f"{base}.json"
    n = 2
    while f"{base}-{n}.json" in taken:
        n += 1
    return f"{base}-{n}.json"


def load_latest(results_dir: str, adapter: str, version: Optional[str] = None) -> Optional[str]:
    """Path of the latest results file for ``adapter``: highest date, then suffix (Q4).
    Sidecars and other names are ignored; None if there is none. With ``version`` (the
    probed CLI version), files captured for that version win over any other version's
    (Linear full review C7: a later capture of an older CLI must not shadow it)."""
    if version:
        same = _load_latest(results_dir, adapter, version)
        if same:
            return same
    return _load_latest(results_dir, adapter, None)


def _load_latest(results_dir: str, adapter: str, version: Optional[str]) -> Optional[str]:
    if not results_dir or not os.path.isdir(results_dir):
        return None
    best: Optional[Tuple[Tuple[str, int], str]] = None
    for name in os.listdir(results_dir):
        parsed = parse_results_name(adapter, name)
        if parsed is None or (version is not None and parsed[0] != version):
            continue
        path = os.path.join(results_dir, name)
        if not os.path.isfile(path):
            continue
        key = (parsed[1], parsed[2])
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None
