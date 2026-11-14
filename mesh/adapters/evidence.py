"""Evidence-driven launch (M3, plan m3-r5; ACC: N19, N20, N23).

A vendor adapter may launch only when committed smoke evidence matches what is installed:

* a results file exists for the adapter (else ``no_smoke_evidence``, checked first so an
  adapter with no evidence at all reports that, probed or not);
* the latest ``adapter_probes`` row (``ORDER BY probed_at DESC``) whose ``binary_path`` is
  the agent's binary (or the adapter's resolved default when the agent sets none) has a
  version V;
* the latest results file for the adapter (Q4 ordering) is at version V;
* ``smoke_schema.validate`` is clean against the matching fixture root and the results file
  has a valid sidecar;
* every ``smoke_schema.LAUNCH_CASES`` case (S-X-1, S-X-6) is ``pass``.

Reasons when not: ``no_probe_version``, ``no_smoke_evidence``, ``evidence_stale``,
``evidence_invalid``, ``launch_cases_failed``, ``launch_held``. Non-vendor kinds keep their
class attribute.

Launch hold (L11): an adapter in :data:`LAUNCH_HOLDS` stays off even when its evidence passes,
with reason ``launch_held`` and the milestone from the table. kiro was held from M3 (James
sign-off 2026-11-12 19:32) until its real end-to-end run N13 passed in M4 (m4-kiro-n13); the
table is empty again and the mechanism stays for future holds. Testing mode only (read from
the same env as the results override, ignored outside testing mode):
``MESH_TEST_LAUNCH_HOLD`` adds holds (``"kiro:M4,agy:M5"``; no milestone means ``M4``) and
``MESH_TEST_LAUNCH_UNHOLD`` lifts holds (comma-separated adapter ids).

Results location (Q3, D5): outside testing mode ``<repo>/tests/smoke/results`` with fixtures
under ``<repo>/tests/fixtures/real``; ``MESH_TEST_SMOKE_RESULTS_DIR`` is ignored there. In
testing mode only ``MESH_TEST_SMOKE_RESULTS_DIR`` counts (fixtures in its parent directory,
the smoke staging layout ``<S>/results`` + ``<S>/<adapter>/<version>/``); without it there is
no evidence. Nothing here executes a binary: only files and the DB are read.

A task already queued when its evidence later goes stale can still start; evidence is
checked at AG step 4 (create/assign/reassign/retry), not again when the run is spawned (T5,
L3). A launch hold is the exception (M3 gate re-review P2): the scheduler re-checks
:func:`held_milestone` at T6 and right before T5, so a task assigned or queued before the
hold fails with ``launch_held`` and never starts.
"""

import datetime
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import mesh
from mesh.adapters import smoke_schema

OVERRIDE_ENV = "MESH_TEST_SMOKE_RESULTS_DIR"
UNHOLD_ENV = "MESH_TEST_LAUNCH_UNHOLD"
HOLD_ENV = "MESH_TEST_LAUNCH_HOLD"
REASONS = ("no_probe_version", "no_smoke_evidence", "evidence_stale", "evidence_invalid", "launch_cases_failed",
           "launch_held")
#: L11: vendor adapters whose launch stays off whatever the evidence says -> milestone that lifts it.
#: Empty since M4 (kiro N13 passed, m4-kiro-n13); kept as a tool for future holds.
LAUNCH_HOLDS: Dict[str, str] = {}
REPO_ROOT = Path(mesh.__file__).resolve().parents[1]
#: Productize: a checkout holding tests/smoke/results and tests/fixtures/real, for installs
#: where the package is not run from a clone (pipx/uv tool install). Default: REPO_ROOT.
EVIDENCE_ROOT_ENV = "MESH_EVIDENCE_ROOT"


def evidence_root(env: Optional[Dict[str, str]] = None) -> Path:
    value = (env if env is not None else os.environ).get(EVIDENCE_ROOT_ENV)
    return Path(os.path.abspath(os.path.expanduser(value))) if value else REPO_ROOT


@dataclass
class Evidence:
    ok: bool
    reason: Optional[str] = None
    results_path: Optional[str] = None
    cases: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    probed_version: Optional[str] = None
    results_version: Optional[str] = None
    captured_at_utc: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    milestone: Optional[str] = None  # set only by a launch hold (L11)
    #: warn mode: the evidence reason that was waived (launch allowed with a warning).
    warning: Optional[str] = None

    def __bool__(self) -> bool:
        """Truthiness is the verdict, so ``if not launch_allowed(...)`` refuses (Kiro r5 finding 2)."""
        return self.ok

    def results_file(self) -> Optional[str]:
        """Repo-relative name for committed results, else the path as read."""
        if not self.results_path:
            return None
        try:
            return str(Path(self.results_path).resolve().relative_to(REPO_ROOT))
        except ValueError:
            return self.results_path


def _override(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    value = (env if env is not None else os.environ).get(OVERRIDE_ENV)
    return value or None


def results_dir(testing_mode: bool, env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """Q3: committed ``tests/smoke/results`` outside testing mode; in testing mode only the
    override (None without it)."""
    if testing_mode:
        o = _override(env)
        return Path(os.path.abspath(o)) if o else None
    return evidence_root(env) / "tests" / "smoke" / "results"


def fixture_root(testing_mode: bool, env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """Fixture root ``validate()`` uses with :func:`results_dir`."""
    if testing_mode:
        o = _override(env)
        return Path(os.path.abspath(o)).parent if o else None
    return evidence_root(env) / "tests" / "fixtures" / "real"


PROBE_OK_STATUSES = ("partially_verified", "verified")


def latest_probe(conn: Optional[sqlite3.Connection], adapter_id: str,
                 binary_path: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(version, probed_at)`` of the newest successful ``adapter_probes`` row for exactly this
    binary, or ``(None, None)``. Error/unverified rows never count (Kiro r5 finding 3): a failed
    probe may still have recorded a version."""
    if conn is None or not binary_path:
        return None, None
    row = conn.execute(
        "SELECT version, probed_at FROM adapter_probes WHERE adapter = ? AND binary_path = ? "
        "AND status IN (?, ?) ORDER BY probed_at DESC LIMIT 1;",
        (adapter_id, binary_path) + PROBE_OK_STATUSES).fetchone()
    if row is None:
        return None, None
    version, probed_at = tuple(row)
    return (version or None), probed_at


def latest_probe_version(conn: Optional[sqlite3.Connection], adapter_id: str,
                         binary_path: Optional[str]) -> Optional[str]:
    """Version of the newest successful ``adapter_probes`` row for exactly this binary, or None."""
    return latest_probe(conn, adapter_id, binary_path)[0]


def binary_changed_since(binary_path: Optional[str], probed_at: Optional[str]) -> bool:
    """Stat only (no process): True when the binary's mtime is later than the probe row, i.e.
    it was replaced/updated after the version was recorded (Kiro r5 finding 3). A binary that
    cannot be stat'ed or a row with an unparseable time is not judged changed here."""
    if not binary_path or not probed_at:
        return False
    try:
        mtime = os.stat(binary_path).st_mtime
        probed = datetime.datetime.fromisoformat(probed_at.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError):
        return False
    return mtime > probed


def _binary_for(adapter, binary_path: Optional[str]) -> Optional[str]:
    if binary_path:
        return os.path.abspath(os.path.expanduser(binary_path))
    try:
        return adapter.resolve_binary(None)  # PATH lookup only, never executes
    except Exception:
        return None


def load_evidence(adapter_id: str, testing_mode: bool, probed_version: Optional[str],
                  env: Optional[Dict[str, str]] = None, binary_changed: bool = False) -> Evidence:
    """Judge the latest results file for ``adapter_id`` against ``probed_version``."""
    ev = Evidence(ok=False, probed_version=probed_version)
    rdir = results_dir(testing_mode, env)
    try:
        path = smoke_schema.load_latest(str(rdir), adapter_id, probed_version) if rdir else None
    except OSError as exc:
        # Linear round 5 P3: unreadable evidence is an evidence failure, not an internal error.
        ev.reason, ev.errors = "evidence_invalid", [f"cannot list smoke results: {exc}"]
        return ev
    if not path:
        ev.reason = "no_smoke_evidence"
        return ev
    ev.results_path = path
    parsed = smoke_schema.parse_results_name(adapter_id, os.path.basename(path))
    ev.results_version = parsed[0] if parsed else None
    try:
        with open(path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        ev.reason, ev.errors = "evidence_invalid", [f"unreadable results file: {exc}"]
        return ev
    froot = fixture_root(testing_mode, env)
    root = REPO_ROOT if testing_mode else evidence_root(env)
    try:
        errors = smoke_schema.validate(doc, str(root), fixture_root=str(froot) if froot else None)
        side = smoke_schema._check_sidecar(path)
    except OSError as exc:
        ev.reason, ev.errors = "evidence_invalid", [f"cannot read smoke evidence: {exc}"]
        return ev
    if side:
        errors.append(f"results file: {side}")
    if isinstance(doc, dict):
        if doc.get("adapter") != adapter_id:
            errors.append(f"adapter {doc.get('adapter')!r} != {adapter_id!r}")
        if doc.get("version") != ev.results_version:
            errors.append(f"doc version {doc.get('version')!r} != file name version {ev.results_version!r}")
        ev.captured_at_utc = doc.get("captured_at_utc")
        cases = doc.get("cases") or []
        if not isinstance(cases, list):
            errors.append(f"cases must be a list, got {type(cases).__name__}")
            cases = []
        for case in cases:
            if isinstance(case, dict) and isinstance(case.get("id"), str):
                ev.cases[case["id"]] = case
    if not probed_version:
        ev.reason, ev.errors = "no_probe_version", errors
        return ev
    if ev.results_version != probed_version:
        ev.reason, ev.errors = "evidence_stale", errors
        return ev
    if binary_changed:
        ev.reason, ev.errors = "evidence_stale", errors + ["binary modified after its last probe; re-run adapter probe"]
        return ev
    if errors:
        ev.reason, ev.errors = "evidence_invalid", errors
        return ev
    if not all((ev.cases.get(cid) or {}).get("status") == "pass" for cid in smoke_schema.LAUNCH_CASES):
        ev.reason = "launch_cases_failed"
        return ev
    ev.ok = True
    return ev


def effective_launch(conn: Optional[sqlite3.Connection], adapter, testing_mode: bool,
                     binary_path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Evidence:
    """Effective ``launch_enabled`` for ``adapter`` (N19). Non-vendor kinds return the class
    attribute unchanged; vendors need matching probe and smoke evidence."""
    if getattr(adapter, "kind", "vendor") != "vendor":
        return Evidence(ok=bool(adapter.launch_enabled))
    binary = _binary_for(adapter, binary_path)
    version, probed_at = latest_probe(conn, adapter.id, binary)
    ev = load_evidence(adapter.id, testing_mode, version, env,
                       binary_changed=bool(version) and binary_changed_since(binary, probed_at))
    ev = apply_hold(ev, adapter.id, testing_mode, env)
    if (not ev.ok and ev.reason != "launch_held" and getattr(adapter, "implemented", True)
            and evidence_mode(conn, testing_mode) == "warn"):
        # Productize: evidence gaps are warnings by default; launch holds still refuse
        # (apply_hold only marks passing evidence, so check the hold table here too).
        held = held_milestone(adapter.id, testing_mode, env)
        if held:
            ev.reason, ev.milestone = "launch_held", held
        else:
            ev.ok, ev.warning = True, ev.reason
    return ev


def evidence_mode(conn: Optional[sqlite3.Connection], testing_mode: bool) -> str:
    """``adapter.evidence_mode``: ``warn`` (default outside testing mode) or ``strict``."""
    default = "strict" if testing_mode else "warn"
    if conn is None:
        return default
    try:
        row = conn.execute("SELECT value_json FROM settings WHERE key = 'adapter.evidence_mode';").fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    try:
        val = json.loads(row[0])
    except ValueError:
        return default
    return val if val in ("warn", "strict") else default


def _unheld(testing_mode: bool, env: Optional[Dict[str, str]] = None) -> set:
    if not testing_mode:
        return set()
    value = (env if env is not None else os.environ).get(UNHOLD_ENV) or ""
    return {x.strip() for x in value.split(",") if x.strip()}


def held_milestone(adapter_id: str, testing_mode: bool,
                   env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """L11: the milestone that lifts the launch hold on ``adapter_id`` (from
    :data:`LAUNCH_HOLDS`), or None when the adapter is not held. Outside testing mode a hold
    is never lifted; in testing mode ``MESH_TEST_LAUNCH_UNHOLD`` may lift it. Reads only the
    table and the env (no evidence, no DB), so the scheduler can re-check it at T6/T5 for
    tasks queued before the hold (M3 gate re-review P2)."""
    return effective_holds(testing_mode, env).get(adapter_id)


def _test_holds(testing_mode: bool, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    if not testing_mode:
        return {}
    value = (env if env is not None else os.environ).get(HOLD_ENV) or ""
    out: Dict[str, str] = {}
    for item in value.split(","):
        aid, _, ms = item.strip().partition(":")
        if aid.strip():
            out[aid.strip()] = ms.strip() or "M4"
    return out


def effective_holds(testing_mode: bool, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Held adapter id -> milestone: :data:`LAUNCH_HOLDS`, plus ``MESH_TEST_LAUNCH_HOLD`` and minus
    ``MESH_TEST_LAUNCH_UNHOLD`` in testing mode only. Outside testing mode exactly the table."""
    holds = dict(LAUNCH_HOLDS)
    holds.update(_test_holds(testing_mode, env))
    for aid in _unheld(testing_mode, env):
        holds.pop(aid, None)
    return holds


def apply_hold(ev: Evidence, adapter_id: str, testing_mode: bool,
               env: Optional[Dict[str, str]] = None) -> Evidence:
    """L11: turn passing evidence off for a held adapter (reason ``launch_held``, milestone from
    :data:`LAUNCH_HOLDS`). Failing evidence keeps its own reason; capability rows still show the
    evidence (N20). Testing mode may lift a hold via ``MESH_TEST_LAUNCH_UNHOLD``."""
    held = held_milestone(adapter_id, testing_mode, env)
    if held and ev.ok:
        ev.ok = False
        ev.reason = "launch_held"
        ev.milestone = held
    return ev


def launch_allowed(conn: Optional[sqlite3.Connection], adapter_id: str, testing_mode: bool,
                   binary_path: Optional[str] = None) -> Evidence:
    """By adapter id, for callers without an adapter instance. The result is falsy unless
    launch is allowed. The lead path (``lead_launch_check``) should call
    :func:`effective_launch` with the lead agent's own ``binary_path`` and raise
    ``AdapterUnsupported(..., reason=ev.reason)``; D4's vendor code gate still applies on top."""
    from mesh.adapters.registry import get_adapter

    return effective_launch(conn, get_adapter(adapter_id, testing_mode=testing_mode), testing_mode, binary_path)


def apply_to_probe(data: Dict[str, Any], adapter, ev: Evidence) -> Dict[str, Any]:
    """N20: per-capability ``verification`` / ``verified_at`` / ``verified_version`` /
    ``results_file`` from the latest results doc, status, and effective launch fields.
    ``data`` is ``Probe.to_dict()`` (changed in place and returned)."""
    vendor = getattr(adapter, "kind", "vendor") == "vendor"
    caps = data.get("capabilities") or {}
    rfile = ev.results_file()
    for name, cap in caps.items():
        if not isinstance(cap, dict):
            continue
        cap.setdefault("verified_at", None)
        cap.setdefault("verified_version", None)
        cap.setdefault("results_file", None)
        if not vendor or not ev.results_path:
            continue
        mapped = smoke_schema.cases_for_capability(name)
        passed = [cid for cid in mapped if (ev.cases.get(cid) or {}).get("status") == "pass"]
        if not mapped or not ev.probed_version or ev.reason == "evidence_invalid":
            continue
        if ev.results_version != ev.probed_version or ev.reason == "evidence_stale":
            if mapped and any(cid in ev.cases for cid in mapped):
                cap["verification"] = "stale"
                cap["verified_at"] = ev.captured_at_utc
                cap["verified_version"] = ev.results_version
                cap["results_file"] = rfile
            continue
        if passed:
            cap["verification"] = "smoke"
            cap["verified_at"] = ev.captured_at_utc
            cap["verified_version"] = ev.results_version
            cap["results_file"] = rfile
    # Only a successful live probe is re-graded; not_installed / error / unverified stay.
    if vendor and data.get("status") in ("partially_verified", "verified"):
        yes = [c for c in caps.values() if isinstance(c, dict) and c.get("supported") == "yes"]
        data["status"] = "verified" if yes and all(c.get("verification") == "smoke" for c in yes) else "partially_verified"
    data["launch_enabled"] = bool(ev.ok)
    if vendor:
        data["launch_milestone"] = None if ev.ok else (
            ev.milestone or getattr(adapter, "launch_milestone", None) or "M3")
    data["launch_reason"] = None if ev.ok else ev.reason
    data["evidence_warning"] = ev.warning
    data["results_file"] = rfile
    return data
