"""Smoke harness core (plan §14.10; m3-r2; ACC: N11, N25, N21). Coordinator tier only.

Nothing here runs from ``scripts/test.sh``. Every entry point takes an explicit ``env``
mapping and an absolute ``binary``; only :func:`main` reads ``os.environ`` and resolves the
binary with ``shutil.which`` on a PATH without ``tests/shims``.

Gate (all from ``env``; otherwise :class:`SmokeSkip`, nothing spawns):

* ``MESH_SMOKE=1``; the adapter in ``MESH_SMOKE_ADAPTERS`` and in ``MESH_SMOKE_CONSENT``
  (comma lists).

Config (a :class:`SmokeConfigError` before any spawn):

* ``MESH_SMOKE_MAX_RUNS`` int >= 1 (model runs; at the cap the rest are ``not_run(run_cap)``);
* ``MESH_SMOKE_MODEL_<ADAPTER>`` (e.g. ``MESH_SMOKE_MODEL_DROID``), never ``auto``;
* ``MESH_SMOKE_TIMEOUT_S`` (default 180); ``MESH_SMOKE_OUT`` under ``/tmp/mh-*``;
* ``MESH_SMOKE_CONSENT_REF`` (optional; defaults to the consent list).

Output layout under ``MESH_SMOKE_OUT`` is the promotion layout of ``tests/tierlib.py``:
``<adapter>/<version>/smoke-<case>-stdout.jsonl`` / ``smoke-<case>-stderr.txt`` and
``results/<adapter>-<version>-<date>.json``, each with a sidecar. Raw output only exists in
a private run dir under ``MESH_SMOKE_OUT/.runs`` and is replaced by its redacted form.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mesh.adapters import smoke_schema
from mesh.adapters.base import RunRequest
from mesh.adapters.registry import ADAPTER_CLASSES
from mesh.core import redact as redact_mod
from mesh.core.proc import get_proc_stat, is_tgid_alive
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_TIMEOUT_S = 180.0
PRODUCTION_GRACE_S = 15.0
POLL_S = 0.05


class SmokeSkip(Exception):
    """The gate is closed for this adapter; nothing was spawned."""


class SmokeConfigError(Exception):
    """The gate is open but the configuration is incomplete or unsafe; nothing was spawned."""


def _csv(env: Mapping[str, str], name: str) -> List[str]:
    return [p.strip() for p in (env.get(name) or "").split(",") if p.strip()]


def gate(env: Mapping[str, str], adapter_id: str) -> Optional[str]:
    """None when the smoke gate is open for ``adapter_id``, else the skip reason."""
    if env.get("MESH_SMOKE") != "1":
        return "MESH_SMOKE!=1"
    if adapter_id not in _csv(env, "MESH_SMOKE_ADAPTERS"):
        return f"{adapter_id} not in MESH_SMOKE_ADAPTERS"
    if adapter_id not in _csv(env, "MESH_SMOKE_CONSENT"):
        return f"no consent for {adapter_id} (MESH_SMOKE_CONSENT)"
    return None


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


@dataclass
class SmokeConfig:
    adapter_id: str
    binary: str
    model: str
    max_runs: int
    timeout_s: float
    out: str
    consent_ref: str


def load_config(env: Mapping[str, str], adapter_id: str, binary: str) -> SmokeConfig:
    """Gate, then config. Raises SmokeSkip / SmokeConfigError; never spawns."""
    reason = gate(env, adapter_id)
    if reason:
        raise SmokeSkip(reason)
    if adapter_id not in ADAPTER_CLASSES:
        raise SmokeConfigError(f"unknown adapter {adapter_id!r}")
    if not isinstance(binary, str) or not os.path.isabs(binary):
        raise SmokeConfigError(f"binary must be an absolute path, got {binary!r}")
    if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise SmokeConfigError(f"binary is not an executable file: {binary}")
    if _inside(binary, os.path.join(REPO_ROOT, "tests", "shims")):
        raise SmokeConfigError("binary resolves into tests/shims")
    raw = env.get("MESH_SMOKE_MAX_RUNS")
    try:
        max_runs = int(raw) if raw is not None else 0
    except ValueError:
        max_runs = 0
    if max_runs < 1:
        raise SmokeConfigError("MESH_SMOKE_MAX_RUNS is required (int >= 1)")
    model_var = f"MESH_SMOKE_MODEL_{adapter_id.upper()}"
    model = (env.get(model_var) or "").strip()
    if not model:
        raise SmokeConfigError(f"{model_var} is required")
    if model.lower() == "auto":
        raise SmokeConfigError(f"{model_var}=auto is refused; name a model")
    try:
        timeout_s = float(env.get("MESH_SMOKE_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        raise SmokeConfigError("MESH_SMOKE_TIMEOUT_S must be a number")
    if timeout_s <= 0:
        raise SmokeConfigError("MESH_SMOKE_TIMEOUT_S must be > 0")
    out = env.get("MESH_SMOKE_OUT") or ""
    if not out or not os.path.isabs(out) or not out.startswith("/tmp/mh-") \
            or not os.path.realpath(out).startswith(os.path.realpath("/tmp") + os.sep + "mh-"):
        raise SmokeConfigError(f"MESH_SMOKE_OUT must be under /tmp/mh-*, got {out!r}")
    if _inside(out, REPO_ROOT):
        raise SmokeConfigError("MESH_SMOKE_OUT is inside the repository")
    consent_ref = (env.get("MESH_SMOKE_CONSENT_REF") or "").strip() or "MESH_SMOKE_CONSENT=" + env.get("MESH_SMOKE_CONSENT", "")
    return SmokeConfig(adapter_id, binary, model, max_runs, timeout_s, out, consent_ref)


def child_env(env: Mapping[str, str]) -> Dict[str, str]:
    """The vendor child's env: ``env`` minus every ``MESH_*`` name."""
    return {k: v for k, v in env.items() if not k.startswith("MESH_")}


@dataclass
class RunOutcome:
    case_id: str
    argv: List[str]
    rc: Optional[int]
    duration_s: float
    stdout: str  # redacted
    stderr: str  # redacted
    timed_out: bool
    run_dir: str
    stdout_fixture: str  # repo-relative, for the results doc
    stdout_sha256: str
    stderr_fixture: str
    group_gone: bool


def session_pgids(sid: int) -> List[int]:
    """Every distinct live process group in session ``sid`` (``start_new_session``: sid ==
    leader pid), including members that left the leader's group with ``setpgid``."""
    found = set()
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                st = get_proc_stat(pid)
                if st and (st["session"] == sid or st["pgrp"] == sid) and is_tgid_alive(pid, st):
                    found.add(st["pgrp"])
            except OSError:
                continue
    found.add(sid)
    return sorted(found)


def signal_session(sid: int, sig: int) -> None:
    """Send ``sig`` to every process group of session ``sid``."""
    for pg in session_pgids(sid):
        try:
            os.killpg(pg, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _group_alive(pgid: int) -> bool:
    """True while a live (non-zombie) process has process group or session ``pgid``. Uses /proc
    (mesh/core/proc.py) so an unreaped zombie does not count; falls back to killpg(0)."""
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                st = get_proc_stat(pid)
                if st and (st["pgrp"] == pgid or st["session"] == pgid) and is_tgid_alive(pid, st):
                    return True
            except OSError:
                continue
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_group_gone(pgid: int, timeout_s: float) -> bool:
    """Poll until no process is left in group ``pgid`` (True) or ``timeout_s`` passes."""
    deadline = time.monotonic() + timeout_s
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    return True


def _line_count(path: str) -> int:
    """Newline-terminated lines in ``path`` (0 if unreadable)."""
    try:
        with open(path, "rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0


def terminate_group(proc: "subprocess.Popen[bytes]", grace_s: float) -> bool:
    """SIGTERM every group of the session, poll up to ``grace_s``, then SIGKILL them. True if
    nothing of the session is left."""
    pgid = proc.pid
    signal_session(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None and not _group_alive(pgid):
            break
        time.sleep(POLL_S)
    signal_session(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    return wait_group_gone(pgid, 10.0)


class SmokeSession:
    """One adapter's smoke capture: run cases, record results, write the results doc.

    ``grace_s`` is the SIGTERM->SIGKILL wait on timeout (production 15 s; tests 1 s).
    ``version`` defaults to ``adapter.probe(binary_path=binary).version``."""

    def __init__(self, env: Mapping[str, str], adapter_id: str, binary: str, *, grace_s: float = PRODUCTION_GRACE_S,
                 version: Optional[str] = None, mesh_commit: str = "unknown", effort: Optional[str] = None):
        self.config = load_config(env, adapter_id, binary)
        self.env = dict(env)
        self.adapter = ADAPTER_CLASSES[adapter_id]()
        self.grace_s = float(grace_s)
        self.effort = effort
        self.mesh_commit = mesh_commit or "unknown"
        self.exact_values = redact_mod.collect_exact_values(self.env)
        if version is None:
            version = self.adapter.probe(binary_path=binary).version
        if not version:
            raise SmokeConfigError(f"could not read a version from {binary}")
        if not smoke_schema.version_ok(version):
            raise SmokeConfigError(f"version {version!r} is not accepted by the results schema")
        self.version = version
        self.runs_used = 0
        self.cases: List[Dict[str, Any]] = []
        os.makedirs(self.config.out, mode=0o700, exist_ok=True)
        self.runs_root = os.path.join(self.config.out, ".runs")
        os.makedirs(self.runs_root, mode=0o700, exist_ok=True)
        self.workspace = tempfile.mkdtemp(prefix="mh-smoke-", dir="/tmp")
        if _inside(self.workspace, REPO_ROOT):
            raise SmokeConfigError("workspace inside the repository")
        self.captured_at_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # -- running -------------------------------------------------------------------------
    @property
    def cap_reached(self) -> bool:
        return self.runs_used >= self.config.max_runs

    def redact(self, text: str) -> str:
        return redact_mod.redact(text, self.exact_values)

    def run(self, case_id: str, prompt: str, *, effort: Optional[str] = None,
            resume_session_id: Optional[str] = None, timeout_s: Optional[float] = None) -> Optional[RunOutcome]:
        """One model run. Returns None (nothing spawned) at the run cap."""
        if case_id not in smoke_schema.CASE_CAPS:
            raise ValueError(f"unknown case {case_id!r}")
        if self.cap_reached:
            return None
        self.refuse_existing_capture(case_id)
        run_dir = tempfile.mkdtemp(prefix=f"{case_id}-", dir=self.runs_root)
        prompt_path = os.path.join(run_dir, "prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        run_id = f"smoke-{case_id}-{os.path.basename(run_dir)}"
        req = RunRequest(prompt_path=prompt_path, workspace=self.workspace, model=self.config.model,
                         effort=effort if effort is not None else self.effort, profile="readonly",
                         resume_session_id=resume_session_id, timeout_s=int(timeout_s or self.config.timeout_s),
                         options={"binary": self.config.binary, "run_id": run_id})
        spec = self.adapter.build(req)
        if spec.argv[0] != self.config.binary:
            raise SmokeConfigError(f"build() did not use the absolute binary: {spec.argv[0]!r}")
        if _inside(spec.cwd, REPO_ROOT):
            raise SmokeConfigError(f"cwd inside the repository: {spec.cwd}")
        limit = float(timeout_s or self.config.timeout_s)
        raw_out, raw_err = os.path.join(run_dir, "raw-stdout"), os.path.join(run_dir, "raw-stderr")
        self.runs_used += 1
        started = time.monotonic()
        timed_out = False
        proc: Optional["subprocess.Popen[bytes]"] = None
        try:
            with open(raw_out, "wb") as fo, open(raw_err, "wb") as fe:
                proc = subprocess.Popen(spec.argv, cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                        env=child_env(self.env), start_new_session=True)
                try:
                    proc.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                if timed_out:
                    group_gone = terminate_group(proc, self.grace_s)
                else:
                    # The leader exited; nothing of its session may outlive the run.
                    signal_session(proc.pid, signal.SIGKILL)
                    group_gone = wait_group_gone(proc.pid, 10.0)
            duration = round(time.monotonic() - started, 3)
            stdout = self.redact(_read_text(raw_out))
            stderr = self.redact(_read_text(raw_err))
        except BaseException:
            # Ctrl-C or any error: the vendor child is in its own session and would keep
            # running (and spending) otherwise.
            if proc is not None:
                terminate_group(proc, self.grace_s)
            raise
        finally:
            for raw in (raw_out, raw_err):
                if os.path.lexists(raw):
                    os.unlink(raw)
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(stdout)  # redacted copy; adapter.parse() reads stdout.log
        cmd = [self.redact(a) for a in [os.path.basename(spec.argv[0])] + spec.argv[1:]]
        out_name, err_name = f"smoke-{case_id}-stdout.jsonl", f"smoke-{case_id}-stderr.txt"
        base = f"{self.adapter.id}/{self.version}"
        out_path = tierlib.write_capture(self.config.out, f"{base}/{out_name}", stdout.encode("utf-8"), cmd)
        tierlib.write_capture(self.config.out, f"{base}/{err_name}", stderr.encode("utf-8"), cmd)
        return RunOutcome(case_id=case_id, argv=cmd, rc=None if timed_out else proc.returncode, duration_s=duration,
                          stdout=stdout, stderr=stderr, timed_out=timed_out, run_dir=run_dir,
                          stdout_fixture=smoke_schema.fixture_rel(self.adapter.id, self.version, out_name),
                          stdout_sha256=tierlib.sha256_bytes(_read_bytes(out_path)),
                          stderr_fixture=smoke_schema.fixture_rel(self.adapter.id, self.version, err_name),
                          group_gone=group_gone)

    def capture_names(self, case_id: str) -> List[str]:
        base = os.path.join(self.config.out, self.adapter.id, self.version)
        names = [f"smoke-{case_id}-stdout.jsonl", f"smoke-{case_id}-stderr.txt"]
        return [os.path.join(base, n + suffix) for n in names for suffix in ("", tierlib.SIDECAR_SUFFIX)]

    def refuse_existing_capture(self, case_id: str) -> None:
        """A case already captured in this ``MESH_SMOKE_OUT`` is refused before spawning
        (promote never overwrites; a retry needs a fresh staging dir)."""
        taken = [p for p in self.capture_names(case_id) if os.path.lexists(p)]
        if taken:
            raise SmokeConfigError(f"{case_id} already captured in {self.config.out}: {taken[0]}")

    def run_and_signal(self, case_id: str, prompt: str, *, wait_first_byte_s: float = 5.0,
                       gone_within_s: float = 15.0, min_lines: int = 2) -> Optional[RunOutcome]:
        """S-X-6 shape (m3-r3, additive): spawn like :meth:`run`, SIGTERM every group of the run's
        session after the first stdout byte or ``wait_first_byte_s`` (polled), then poll up to
        ``gone_within_s`` until no live process has the run's pgid or sid (``_group_alive``, the
        r2b rule; ``find_run_processes`` repeats that test because ``child_env`` strips
        ``MESH_RUN_ID``). A child that calls ``setsid()`` leaves both and is NOT detected; that
        is within the plan's pgid/sid criterion. Leftovers are SIGKILLed afterwards. Same guards
        as ``run``: cap, duplicate-capture refusal before spawn, child terminated on any
        exception, ``raw-*`` removed in ``finally``.
        ``outcome.signal_info`` = ``{first_byte, term_after_s, alive_at_term, rc, lines_at_term,
        group_gone_after_term, seconds_to_gone}``. The SIGTERM goes out once stdout has a second
        line (past droid's init event; ``min_lines``, default 2, kiro passes 4 to get past
        runStarted and its two pre-model metadata lines), or at ``wait_first_byte_s``, or when the leader exits;
        ``alive_at_term`` is False when the leader had already exited before the SIGTERM.
        Returns None (nothing spawned) at the run cap.
        """
        from mesh.core.proc import find_run_processes

        if case_id not in smoke_schema.CASE_CAPS:
            raise ValueError(f"unknown case {case_id!r}")
        if self.cap_reached:
            return None
        self.refuse_existing_capture(case_id)
        run_dir = tempfile.mkdtemp(prefix=f"{case_id}-", dir=self.runs_root)
        prompt_path = os.path.join(run_dir, "prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        run_id = f"smoke-{case_id}-{os.path.basename(run_dir)}"
        req = RunRequest(prompt_path=prompt_path, workspace=self.workspace, model=self.config.model,
                         effort=self.effort, profile="readonly", timeout_s=int(self.config.timeout_s),
                         options={"binary": self.config.binary, "run_id": run_id})
        spec = self.adapter.build(req)
        if spec.argv[0] != self.config.binary:
            raise SmokeConfigError(f"build() did not use the absolute binary: {spec.argv[0]!r}")
        if _inside(spec.cwd, REPO_ROOT):
            raise SmokeConfigError(f"cwd inside the repository: {spec.cwd}")
        raw_out, raw_err = os.path.join(run_dir, "raw-stdout"), os.path.join(run_dir, "raw-stderr")
        self.runs_used += 1
        started = time.monotonic()
        proc: Optional["subprocess.Popen[bytes]"] = None
        try:
            with open(raw_out, "wb") as fo, open(raw_err, "wb") as fe:
                proc = subprocess.Popen(spec.argv, cwd=spec.cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe,
                                        env=child_env(self.env), start_new_session=True)
                pgid = proc.pid
                deadline = started + wait_first_byte_s
                first_byte = False
                # Signal once stdout is past its first line (droid's init event comes out before
                # the model call; Kiro r5 finding 4), or at the deadline, or when the leader exits.
                while time.monotonic() < deadline:
                    if os.path.getsize(raw_out) > 0:
                        first_byte = True
                        if _line_count(raw_out) >= max(1, int(min_lines)):
                            break
                    if proc.poll() is not None:
                        break
                    time.sleep(POLL_S)
                lines_at_term = _line_count(raw_out)
                term_after = round(time.monotonic() - started, 3)
                alive_at_term = proc.poll() is None  # Kiro r3 finding 1: an exited leader proves nothing
                signal_session(pgid, signal.SIGTERM)
                t_term = time.monotonic()
                gone_deadline = t_term + gone_within_s
                gone = False
                while True:
                    proc.poll()
                    if not _group_alive(pgid) and not find_run_processes(run_id, pgid):
                        gone = True
                        break
                    if time.monotonic() >= gone_deadline:
                        break
                    time.sleep(POLL_S)
                seconds_to_gone = round(time.monotonic() - t_term, 3) if gone else None
                if not gone:
                    for pid in find_run_processes(run_id, pgid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    signal_session(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
                group_gone = wait_group_gone(pgid, 10.0)
            duration = round(time.monotonic() - started, 3)
            stdout = self.redact(_read_text(raw_out))
            stderr = self.redact(_read_text(raw_err))
        except BaseException:
            if proc is not None:
                terminate_group(proc, self.grace_s)
            raise
        finally:
            for raw in (raw_out, raw_err):
                if os.path.lexists(raw):
                    os.unlink(raw)
        with open(os.path.join(run_dir, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(stdout)
        cmd = [self.redact(a) for a in [os.path.basename(spec.argv[0])] + spec.argv[1:]]
        out_name, err_name = f"smoke-{case_id}-stdout.jsonl", f"smoke-{case_id}-stderr.txt"
        base = f"{self.adapter.id}/{self.version}"
        out_path = tierlib.write_capture(self.config.out, f"{base}/{out_name}", stdout.encode("utf-8"), cmd)
        tierlib.write_capture(self.config.out, f"{base}/{err_name}", stderr.encode("utf-8"), cmd)
        outcome = RunOutcome(case_id=case_id, argv=cmd, rc=proc.returncode, duration_s=duration, stdout=stdout,
                             stderr=stderr, timed_out=False, run_dir=run_dir,
                             stdout_fixture=smoke_schema.fixture_rel(self.adapter.id, self.version, out_name),
                             stdout_sha256=tierlib.sha256_bytes(_read_bytes(out_path)),
                             stderr_fixture=smoke_schema.fixture_rel(self.adapter.id, self.version, err_name),
                             group_gone=group_gone)
        outcome.signal_info = {"first_byte": first_byte, "term_after_s": term_after,  # type: ignore[attr-defined]
                               "alive_at_term": alive_at_term, "rc": proc.returncode,
                               "lines_at_term": lines_at_term,
                               "group_gone_after_term": gone, "seconds_to_gone": seconds_to_gone}
        return outcome

    def staged_documents(self) -> List[Dict[str, Any]]:
        """Results docs already in ``MESH_SMOKE_OUT/results`` for this adapter and version,
        oldest first (Q4 order), sidecars checked. ``finish()`` carries the latest one."""
        rdir = os.path.join(self.config.out, "results")
        found = []
        if os.path.isdir(rdir):
            for name in os.listdir(rdir):
                parsed = smoke_schema.parse_results_name(self.adapter.id, name)
                if parsed is None or parsed[0] != self.version:
                    continue
                tierlib.read_sidecar(os.path.join(rdir, name))
                with open(os.path.join(rdir, name), encoding="utf-8") as f:
                    found.append(((parsed[1], parsed[2]), json.load(f)))
        return [d for _, d in sorted(found, key=lambda x: x[0])]

    def parse(self, outcome: RunOutcome):
        return self.adapter.parse(outcome.run_dir)

    # -- recording -----------------------------------------------------------------------
    def record(self, case_id: str, status: str, *, reason: Optional[str] = None, outcome: Optional[RunOutcome] = None,
               observation: Any = None) -> Dict[str, Any]:
        if status not in smoke_schema.STATUSES:
            raise ValueError(f"bad status {status!r}")
        if outcome is not None and outcome.timed_out and observation is None:
            observation = "hang"
        if outcome is not None and not outcome.group_gone:
            if isinstance(observation, dict):
                observation = dict(observation, leftover_processes=True)
            else:
                observation = {"value": observation, "leftover_processes": True}
        case = {"id": case_id, "status": status, "reason": reason,
                "rc": outcome.rc if outcome else None,
                "duration_s": outcome.duration_s if outcome else 0.0,
                "fixture": outcome.stdout_fixture if outcome else None,
                "fixture_sha256": outcome.stdout_sha256 if outcome else None,
                "observation": observation, "evaluated_from": None, "carried_from": None}
        self.cases.append(case)
        return case

    def run_or_cap(self, case_id: str, prompt: str, **kw) -> Optional[RunOutcome]:
        """``run``; at the cap records ``not_run(run_cap)`` and returns None."""
        outcome = self.run(case_id, prompt, **kw)
        if outcome is None:
            self.record(case_id, "not_run", reason="run_cap")
        return outcome

    def document(self) -> Dict[str, Any]:
        return {"schema": smoke_schema.SCHEMA, "adapter": self.adapter.id, "binary": self.config.binary,
                "version": self.version, "captured_at_utc": self.captured_at_utc, "mesh_commit": self.mesh_commit,
                "model": self.config.model, "effort": self.effort, "profile": "readonly",
                "timeout_s": self.config.timeout_s, "consent_ref": self.config.consent_ref, "cases": list(self.cases)}

    def carry_staged(self) -> List[str]:
        """Staged C2 (Kiro r2 finding 4): copy every case of the latest results doc already in
        ``MESH_SMOKE_OUT/results`` for this adapter and version that this session did not
        record, with ``carried_from = tests/smoke/results/<that file>`` (a case carried
        earlier keeps its original ``carried_from``). The new doc is then complete, so the
        latest file holds S-X-1 and S-X-6. Cases end in CASE_IDS order. Returns carried ids."""
        rdir = os.path.join(self.config.out, "results")
        latest = smoke_schema.load_latest(rdir, self.adapter.id)
        if latest is None:
            return []
        parsed = smoke_schema.parse_results_name(self.adapter.id, os.path.basename(latest))
        if not parsed or parsed[0] != self.version:
            return []
        tierlib.read_sidecar(latest)
        with open(latest, encoding="utf-8") as f:
            prior = json.load(f)
        errors = smoke_schema.validate(prior, REPO_ROOT, fixture_root=self.config.out)
        if errors:
            raise SmokeConfigError(f"staged results {latest} invalid: {errors}")
        mine = {c["id"] for c in self.cases}
        source = "tests/smoke/results/" + os.path.basename(latest)
        carried = []
        for case in prior.get("cases", []):
            if case["id"] in mine:
                continue
            c = dict(case)
            c["carried_from"] = case.get("carried_from") or source
            self.cases.append(c)
            carried.append(c["id"])
        order = {cid: i for i, cid in enumerate(smoke_schema.CASE_IDS)}
        self.cases.sort(key=lambda c: order.get(c["id"], len(order)))
        return carried

    def finish(self) -> str:
        """Validate against the staging tree and write ``results/<name>`` (O_EXCL, sidecar).
        Removes the workspace and the private run dirs. Returns the results path."""
        self.carry_staged()
        doc = self.document()
        errors = smoke_schema.validate(doc, REPO_ROOT, fixture_root=self.config.out)
        if errors:
            raise SmokeConfigError(f"results doc invalid: {errors}")
        rdir = os.path.join(self.config.out, "results")
        existing = os.listdir(rdir) if os.path.isdir(rdir) else []
        name = smoke_schema.results_name(self.adapter.id, self.version, self.captured_at_utc[:10], existing)
        data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
        path = tierlib.write_capture(self.config.out, f"results/{name}", data,
                                     ["python3", "-m", "tests.smoke.harness", self.adapter.id])
        self.cleanup()
        return path

    def cleanup(self) -> None:
        if self.workspace.startswith("/tmp/mh-smoke-"):
            shutil.rmtree(self.workspace, ignore_errors=True)
        if self.runs_root.startswith("/tmp/mh-"):
            shutil.rmtree(self.runs_root, ignore_errors=True)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_text(path: str) -> str:
    return _read_bytes(path).decode("utf-8", errors="replace")


def _raise_interrupt(signum, frame):  # pragma: no cover - exercised via os.kill in tests
    # Ignore further SIGTERM/SIGHUP so a second signal cannot abort the cleanup this one starts
    # (Kiro r5 finding 7); restore_interrupt_handlers puts the previous handlers back.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    raise KeyboardInterrupt(f"signal {signum}")


def install_interrupt_handlers() -> Dict[int, Any]:
    """SIGTERM/SIGHUP -> KeyboardInterrupt, so ``run``'s ``except BaseException`` kills the
    vendor session and ``finally`` removes ``raw-*`` (Kiro r3 finding 3: default actions end
    the process without unwinding). Returns the previous handlers for
    :func:`restore_interrupt_handlers`. Main thread only."""
    previous: Dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, _raise_interrupt)
    return previous


def restore_interrupt_handlers(previous: Optional[Dict[int, Any]]) -> None:
    for sig, handler in (previous or {}).items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def shim_free_path(env: Mapping[str, str]) -> str:
    shims = os.path.realpath(os.path.join(REPO_ROOT, "tests", "shims"))
    return os.pathsep.join(p for p in (env.get("PATH") or "").split(os.pathsep) if p and os.path.realpath(p) != shims)


def main(argv: Sequence[str]) -> int:
    """``python3 -m tests.smoke.harness <adapter> --prompt TEXT [--case S-X-1]``: one gated run
    (coordinator tool; the case suite lives in m3-r3). Reads ``os.environ`` here only."""
    parser = argparse.ArgumentParser(prog="python3 -m tests.smoke.harness")
    parser.add_argument("adapter")
    parser.add_argument("--case", default="S-X-1")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--grace-s", type=float, default=PRODUCTION_GRACE_S)
    args = parser.parse_args(list(argv))
    env = dict(os.environ)
    adapter = ADAPTER_CLASSES.get(args.adapter)
    if adapter is None:
        print(json.dumps({"ok": False, "error": f"unknown adapter {args.adapter}"}))
        return 2
    found = shutil.which(adapter().binary_names[0], path=shim_free_path(env))
    try:
        session = SmokeSession(env, args.adapter, os.path.abspath(found) if found else "", grace_s=args.grace_s)
    except SmokeSkip as exc:
        print(json.dumps({"ok": True, "skipped": str(exc)}))
        return 0
    except SmokeConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    previous = install_interrupt_handlers()
    try:
        outcome = session.run_or_cap(args.case, args.prompt)
        if outcome is not None:
            session.record(args.case, "observed", outcome=outcome)
        print(json.dumps({"ok": True, "results": session.finish()}))
    finally:
        restore_interrupt_handlers(previous)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
