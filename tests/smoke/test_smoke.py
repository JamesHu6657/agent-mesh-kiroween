"""Opt-in smoke cases S-X-1..7 plus S-DROID-8 / S-KIRO-8 / S-AGY-8 / S-VIBE-8 (plan §14.10;
m3-r3, m3-kiro-real, m3-smoke-agy-vibe; ACC: N11). Coordinator only (C2 droid; kiro, agy and
vibe after James's consent).

Skipped unless the smoke gate is open for the adapter (``MESH_SMOKE=1``,
``MESH_SMOKE_ADAPTERS`` and ``MESH_SMOKE_CONSENT`` contain ``droid`` / ``kiro`` / ``agy`` /
``vibe``). Never part of ``scripts/test.sh``. Profile readonly (droid: no autonomy flag; kiro:
``--trust-tools=``; agy: ``--mode plan``; vibe: ``--agent ask``); model from
``MESH_SMOKE_MODEL_DROID`` (C2: ``claude-sonnet-5-5``) / ``MESH_SMOKE_MODEL_KIRO`` (name a
cheap model; build() otherwise defaults to claude-opus-5.5) / ``MESH_SMOKE_MODEL_AGY`` /
``MESH_SMOKE_MODEL_VIBE``; 6 model runs, one test method per
run, selected with ``MESH_SMOKE_CASES=R1`` / ``R2,R3,R4,R5,R6`` (default all). Kiro credit
rule (James): MESH_SMOKE_MAX_RUNS=1 per stage, ``/usage`` before and after:

* R1 -> S-X-1, S-X-2, S-X-4   "Reply with exactly MESH_SMOKE_OK"
* R2 -> S-X-3                 resume R1's session (this process, or the R1 capture staged in
                               ``MESH_SMOKE_OUT``); no id -> ``blocked(no_session_id)``
* R3 -> S-X-5                 ``MESH_SMOKE_EFFORT_<ADAPTER>`` (default ``low``; ``none`` ->
                               ``not_applicable(effort_not_listed)``, no run; an effort the
                               adapter's build() refuses (vibe: no effort flag; agy: a model id
                               that already ends in an effort suffix) ->
                               ``not_applicable(effort_refused_by_adapter)``, no run)
* R4 -> S-X-6                 SIGTERM to the group after the first stdout byte or 5 s; pass if
                               no process with that pgid/sid remains within 15 s
* R5 -> S-X-7                 "create file x.txt"; records whether it appears
* R6 -> S-<ADAPTER>-8         "run `touch y.txt`" under readonly; deny/fail/hang, y.txt, stderr
                               (kiro: the stderr ``[denied]`` count; agy: ``tool_errors`` (tool
                               steps in state ERROR) and ``denied_actions`` from the result;
                               vibe: ``failed_effects`` / ``denied_effects`` from effect lines)

Kiro stream facts (session id, ``metadata.modelId``, ``reasoning.effort``) come from
``KiroAdapter.stream_facts``; S-X-6 signals kiro after 4 stdout lines (runStarted and two
metadata lines come before the model call). agy and vibe stream facts come from
``AgyAdapter.stream_facts`` / ``VibeAdapter.stream_facts``; S-X-6 signals agy after 3 lines
(``init`` and the ``user_input`` step come before the model call) and vibe after 2 (its user
message line comes first).

At class teardown the cases of this stage are merged onto any results already staged for
the adapter at this version in ``MESH_SMOKE_OUT`` (later stage wins per case id), cases never run are
``not_run(not_selected)``, and one new results file is written (Q4 name).

The run functions take a :class:`tests.smoke.harness.SmokeSession`; offline tests
(tests/unit/test_smoke_cases.py) drive them with /tmp/mh-* stubs and explicit env dicts.
"""

import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mesh.adapters import smoke_schema
from mesh.adapters.base import EffortNotAllowed, EffortUnsupported
from tests.smoke import harness
from tests.smoke import reevaluate as ev

RUNS = ("R1", "R2", "R3", "R4", "R5", "R6")
DROID_CASES = ("S-X-1", "S-X-2", "S-X-3", "S-X-4", "S-X-5", "S-X-6", "S-X-7", "S-DROID-8")
KIRO_CASES = ("S-X-1", "S-X-2", "S-X-3", "S-X-4", "S-X-5", "S-X-6", "S-X-7", "S-KIRO-8")
AGY_CASES = ("S-X-1", "S-X-2", "S-X-3", "S-X-4", "S-X-5", "S-X-6", "S-X-7", "S-AGY-8")
VIBE_CASES = ("S-X-1", "S-X-2", "S-X-3", "S-X-4", "S-X-5", "S-X-6", "S-X-7", "S-VIBE-8")
ADAPTER_CASES: Dict[str, Tuple[str, ...]] = {"droid": DROID_CASES, "kiro": KIRO_CASES, "agy": AGY_CASES,
                                             "vibe": VIBE_CASES}
# S-X-6: stdout lines to wait for before SIGTERM (past the pre-model events): droid init (1);
# kiro runStarted + 2 metadata (3); agy 1.5.3 init + user_input step_update (2,
# stream-step1-denied.jsonl:1-2); vibe 3.1.0 the user message line (1,
# streaming-terminated.jsonl:1). Each value is that count plus one.
SIGNAL_MIN_LINES = {"droid": 2, "kiro": 4, "agy": 3, "vibe": 2}
PROMPTS = {
    "R1": "Reply with exactly MESH_SMOKE_OK",
    "R2": "What token did you reply with?",
    "R3": "Reply with exactly MESH_SMOKE_OK",
    "R4": "Count from 1 to 2000, one number per line, with no other text.",
    "R5": "Create a file named x.txt containing the word hello in the current directory.",
    "R6": "Run the shell command `touch y.txt` in the current directory.",
}
S_X_6_FIRST_BYTE_S = 5.0
S_X_6_GONE_WITHIN_S = 15.0


def selected_runs(env: Mapping[str, str]) -> List[str]:
    raw = [p.strip().upper() for p in (env.get("MESH_SMOKE_CASES") or "").split(",") if p.strip()]
    if not raw:
        return list(RUNS)
    bad = [r for r in raw if r not in RUNS]
    if bad:
        raise harness.SmokeConfigError(f"MESH_SMOKE_CASES: unknown runs {bad}")
    return [r for r in RUNS if r in raw]


def _cap(s: harness.SmokeSession, case_ids) -> None:
    for cid in case_ids:
        s.record(cid, "not_run", reason="run_cap")


def run_r1(s: harness.SmokeSession) -> Optional[str]:
    """R1 -> S-X-1, S-X-2, S-X-4. Returns the session id (or None)."""
    o = s.run("S-X-1", PROMPTS["R1"])
    if o is None:
        _cap(s, ("S-X-1", "S-X-2", "S-X-4"))
        return None
    parsed = s.parse(o)
    st, reason, obs = ev.eval_sx1(o.stdout, o.rc, o.timed_out, parsed)
    s.record("S-X-1", st, reason=reason, outcome=o, observation=obs)
    aid = s.adapter.id
    st, reason, obs = ev.eval_sx2(o.stdout, o.rc, o.timed_out, parsed, adapter_id=aid)
    obs = dict(obs or {}) if not isinstance(obs, str) else {"value": obs}
    # Kiro r3 finding 5: where R1 ran, so a stage-2 resume failure can be attributed.
    obs["workspace"] = s.workspace
    obs["init_cwd"] = ev.stream_facts(o.stdout, aid)["cwd"]
    s.record("S-X-2", st, reason=reason, outcome=o, observation=obs)
    sx2_pass = st == "pass"
    st, reason, obs = ev.eval_sx4(o.stdout, o.rc, o.timed_out, s.config.model, adapter_id=aid)
    s.record("S-X-4", st, reason=reason, outcome=o, observation=obs)
    if not sx2_pass or parsed is None:
        # S-X-2 passes only for rc 0, no timeout and no vendor error (eval_sx2), so a failed R1
        # is never resumed; a droid S-X-1 parse_pending alone does not block it (unchanged).
        return None
    return parsed.session_id


def staged_session_id(s: harness.SmokeSession) -> Optional[str]:
    """R1's session id from results staged earlier in ``MESH_SMOKE_OUT``.

    Only the latest staged R1 counts (the newest document whose S-X-2 was run). Its session is
    resumed only when that R1 run succeeded in one capture (Linear review A MINOR 1): S-X-2 pass
    with a session id and no vendor error, and S-X-1 from the same fixture (sha256) with rc 0 and
    not failed for a vendor error or timeout. S-X-1 fail(parse_pending) (a final-text parse
    gap, droid) does not block it, as before. An older good R1 is never used behind a newer
    failed one."""
    for doc in reversed(s.staged_documents()):
        by_id = {c.get("id"): c for c in doc.get("cases", []) if isinstance(c, dict)}
        s2 = by_id.get("S-X-2")
        if not s2 or s2.get("status") == "not_run":
            continue
        s1 = by_id.get("S-X-1") or {}
        obs = s2.get("observation")
        same_capture = bool(s2.get("fixture")) and s1.get("fixture") == s2.get("fixture") \
            and s1.get("fixture_sha256") == s2.get("fixture_sha256")
        s1_run_ok = s1.get("rc") == 0 and (s1.get("status") == "pass" or s1.get("reason") == "parse_pending")
        if s1_run_ok and s2.get("status") == "pass" and same_capture \
                and isinstance(obs, dict) and obs.get("session_id") and not obs.get("vendor_exit_meaning"):
            return obs["session_id"]
        return None
    return None


def run_r2(s: harness.SmokeSession, session_id: Optional[str]) -> None:
    """R2 -> S-X-3: resume R1's session; no id -> blocked, no spawn."""
    if not session_id:
        s.record("S-X-3", "blocked", reason="no_session_id")
        return
    o = s.run("S-X-3", PROMPTS["R2"], resume_session_id=session_id)
    if o is None:
        _cap(s, ("S-X-3",))
        return
    st, reason, obs = ev.eval_sx3(o.stdout, o.rc, o.timed_out, s.parse(o))
    obs = dict(obs or {}) if not isinstance(obs, str) else {"value": obs}
    obs["resumed_session_id"] = session_id
    obs["cwd"] = s.workspace
    s.record("S-X-3", st, reason=reason, outcome=o, observation=obs)


def run_r3(s: harness.SmokeSession, effort: str) -> None:
    """R3 -> S-X-5 with ``effort``; ``none`` means C1 found no effort for the model."""
    if not effort or effort.lower() == "none":
        s.record("S-X-5", "not_applicable", reason="effort_not_listed")
        return
    try:
        o = s.run("S-X-5", PROMPTS["R3"], effort=effort)
    except (EffortNotAllowed, EffortUnsupported) as exc:
        # build() refused before anything spawned (only agy and vibe raise these).
        s.record("S-X-5", "not_applicable", reason="effort_refused_by_adapter",
                 observation={"error": exc.code, "effort": effort, "model": s.config.model})
        return
    if o is None:
        _cap(s, ("S-X-5",))
        return
    st, reason, obs = ev.with_effort(ev.eval_sx5(o.stdout, o.stderr, o.rc, o.timed_out, effort,
                                                 adapter_id=s.adapter.id), effort)
    s.record("S-X-5", st, reason=reason, outcome=o, observation=obs)


def run_r4(s: harness.SmokeSession, gone_within_s: float = S_X_6_GONE_WITHIN_S,
           wait_first_byte_s: float = S_X_6_FIRST_BYTE_S) -> None:
    """R4 -> S-X-6: SIGTERM the group; pass if no pgid/sid member remains within the window."""
    o = s.run_and_signal("S-X-6", PROMPTS["R4"], wait_first_byte_s=wait_first_byte_s, gone_within_s=gone_within_s,
                         min_lines=SIGNAL_MIN_LINES.get(s.adapter.id, 2))
    if o is None:
        _cap(s, ("S-X-6",))
        return
    # exited before the signal (Kiro r3 finding 1), its own error exit by rc+stderr (Kiro r5
    # finding 4) or by an in-stream vendor error at rc 0 (Linear review A MAJOR 2): ev.eval_sx6
    st, reason, obs = ev.eval_sx6(dict(o.signal_info), o.stdout, o.stderr, s.adapter.id)
    s.record("S-X-6", st, reason=reason, outcome=o, observation=obs)


def run_r5(s: harness.SmokeSession) -> None:
    """R5 -> S-X-7 (observation): does x.txt appear under readonly?"""
    target = os.path.join(s.workspace, "x.txt")
    existed = os.path.exists(target)
    o = s.run("S-X-7", PROMPTS["R5"])
    if o is None:
        _cap(s, ("S-X-7",))
        return
    s.record("S-X-7", "observed", outcome=o,
             observation={"file_created": (not existed) and os.path.exists(target), "rc": o.rc,
                          "timed_out": o.timed_out, "stderr_excerpt": ev.excerpt(o.stderr)})


def case8(adapter_id: str) -> str:
    """The adapter's shell-under-readonly case id (S-DROID-8, S-KIRO-8, S-AGY-8, S-VIBE-8)."""
    return f"S-{adapter_id.upper()}-8"


def _json_lines(stdout: str) -> List[Dict[str, Any]]:
    out = []
    for line in (stdout or "").splitlines():
        try:
            ev_ = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(ev_, dict):
            out.append(ev_)
    return out


def agy_tool_denials(stdout: str) -> Dict[str, Any]:
    """agy 1.5.3: tool steps that ended in state ERROR (stream-fixC-denied.jsonl:14, a denied
    run_command) and the last result's ``denied_actions`` count (None without a result, and None
    when that result carries no ``denied_actions`` list: absent is not "zero denied")."""
    errors, denied = 0, None
    for e in _json_lines(stdout):
        if e.get("event") == "step_update" and isinstance(e.get("step_update"), dict):
            st = e["step_update"]
            if st.get("step_type") == "tool" and st.get("state") == "ERROR":
                errors += 1
        elif e.get("event") == "result" and isinstance(e.get("result"), dict):
            da = e["result"].get("denied_actions")
            denied = len(da) if isinstance(da, list) else None
    return {"tool_errors": errors, "denied_actions": denied}


def vibe_effect_failures(stdout: str) -> Dict[str, int]:
    """vibe 3.1.0: ``effect`` lines whose ``state.status`` is ``failed`` (``failed_effects``) and
    those whose ``state.error.code`` is ``tool_denied`` (``denied_effects``).
    streaming-terminated.jsonl:5 and :7 are such denials (decision ``skip``, approvalSource
    ``never``, outside_workdir = "deny")."""
    failed = denied = 0
    for e in _json_lines(stdout):
        if e.get("type") != "effect" or not isinstance(e.get("state"), dict):
            continue
        st = e["state"]
        if st.get("status") == "failed":
            failed += 1
        err = st.get("error")
        if isinstance(err, dict) and err.get("code") == "tool_denied":
            denied += 1
    return {"failed_effects": failed, "denied_effects": denied}


def run_r6(s: harness.SmokeSession) -> None:
    """R6 -> S-DROID-8 / S-KIRO-8 / S-AGY-8 / S-VIBE-8 (observation): shell `touch y.txt` under
    readonly."""
    cid = case8(s.adapter.id)
    target = os.path.join(s.workspace, "y.txt")
    o = s.run(cid, PROMPTS["R6"])
    if o is None:
        _cap(s, (cid,))
        return
    exists = os.path.exists(target)
    if o.timed_out:
        outcome = "hang"
    elif exists:
        outcome = "executed"
    elif o.rc not in (0, None):
        outcome = "fail"
    else:
        outcome = "deny"
    observation = {"outcome": outcome, "y_txt_exists": exists, "rc": o.rc, "stderr_excerpt": ev.excerpt(o.stderr)}
    if s.adapter.id == "kiro":
        from mesh.adapters.kiro import DENIED_MARKER
        observation["denied_lines"] = (o.stderr or "").count(DENIED_MARKER)
    elif s.adapter.id == "agy":
        observation.update(agy_tool_denials(o.stdout))
    elif s.adapter.id == "vibe":
        observation.update(vibe_effect_failures(o.stdout))
    s.record(cid, "observed", outcome=o, observation=observation)


def not_selected(case_id: str) -> Dict[str, Any]:
    return {"id": case_id, "status": "not_run", "reason": "not_selected", "rc": None, "duration_s": 0.0,
            "fixture": None, "fixture_sha256": None, "observation": None, "evaluated_from": None,
            "carried_from": None}


RUN_CASES: Dict[str, Tuple[str, ...]] = {
    "R1": ("S-X-1", "S-X-2", "S-X-4"),
    "R2": ("S-X-3",),
    "R3": ("S-X-5",),
    "R4": ("S-X-6",),
    "R5": ("S-X-7",),
    "R6": ("S-DROID-8",),
}


def run_cases(run: str, adapter_id: str) -> Tuple[str, ...]:
    return (case8(adapter_id),) if run == "R6" else RUN_CASES[run]


def finish_stage(s: harness.SmokeSession, runs: Optional[Sequence[str]] = None) -> str:
    """Write this stage's results doc. A case of a selected run (``runs``) with no record (the
    run raised after spending) is ``blocked(harness_error)``. ``SmokeSession.finish`` carries
    every case of the latest staged doc this stage did not record (``carried_from``, r2b); any
    other case of the adapter is ``not_run(not_selected)``, so the doc always lists all of
    ``ADAPTER_CASES[adapter]``."""
    all_cases = ADAPTER_CASES[s.adapter.id]
    recorded = {c["id"] for c in s.cases}
    for run in runs or ():
        for cid in run_cases(run, s.adapter.id):
            if cid not in recorded:
                case = not_selected(cid)
                case["status"], case["reason"] = "blocked", "harness_error"
                s.cases.append(case)
                recorded.add(cid)
    staged = s.staged_documents()
    known = recorded | {c["id"] for c in (staged[-1]["cases"] if staged else [])}
    for cid in all_cases:
        if cid not in known:
            s.cases.append(not_selected(cid))
    order = {cid: i for i, cid in enumerate(all_cases)}
    s.cases.sort(key=lambda c: order.get(c["id"], len(order)))
    return s.finish()


def _mesh_commit() -> str:
    try:
        from mesh.core.gitops import run_git
        res = run_git(["rev-parse", "HEAD"], cwd=harness.REPO_ROOT)
        return res.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class _VendorSmoke(unittest.TestCase):
    """Shared R1..R6 methods. Methods run in name order (R1 first), sharing one session.
    ``ADAPTER``/``BINARY`` None: this base class skips."""

    ADAPTER: Optional[str] = None
    BINARY: Optional[str] = None
    session: Optional[harness.SmokeSession] = None
    session_id: Optional[str] = None

    @classmethod
    def setUpClass(cls) -> None:
        if not cls.ADAPTER:
            raise unittest.SkipTest("base class")
        env = dict(os.environ)  # the only os.environ read in the smoke tier (C2)
        reason = harness.gate(env, cls.ADAPTER)
        if reason:
            raise unittest.SkipTest(f"smoke tier closed: {reason}")
        cls.runs = selected_runs(env)
        found = shutil.which(cls.BINARY, path=harness.shim_free_path(env))
        cls.session = harness.SmokeSession(env, cls.ADAPTER, os.path.abspath(found) if found else "",
                                           grace_s=harness.PRODUCTION_GRACE_S, mesh_commit=_mesh_commit())
        cls.effort = env.get(f"MESH_SMOKE_EFFORT_{cls.ADAPTER.upper()}", "low")
        cls.previous_handlers = harness.install_interrupt_handlers()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls.session is not None:
                try:
                    path = finish_stage(cls.session, cls.runs)
                except BaseException as exc:
                    print(f"smoke finish failed: {exc!r}; captures stay in {cls.session.config.out}")
                    raise
                print(f"smoke results: {path}")
        finally:
            cls.session = None
            harness.restore_interrupt_handlers(getattr(cls, "previous_handlers", None))

    def want(self, run: str) -> None:
        if run not in self.runs:
            self.skipTest(f"{run} not selected (MESH_SMOKE_CASES)")

    def test_r1(self) -> None:
        """ACC: N11 - R1: S-X-1, S-X-2, S-X-4."""
        self.want("R1")
        type(self).session_id = run_r1(self.session)

    def test_r2(self) -> None:
        """ACC: N11 - R2: S-X-3 resume."""
        self.want("R2")
        run_r2(self.session, self.session_id or staged_session_id(self.session))

    def test_r3(self) -> None:
        """ACC: N11 - R3: S-X-5 reasoning effort."""
        self.want("R3")
        run_r3(self.session, self.effort)

    def test_r4(self) -> None:
        """ACC: N11 - R4: S-X-6 termination."""
        self.want("R4")
        run_r4(self.session)

    def test_r5(self) -> None:
        """ACC: N11 - R5: S-X-7 file creation (observation)."""
        self.want("R5")
        run_r5(self.session)

    def test_r6(self) -> None:
        """ACC: N11 - R6: S-<ADAPTER>-8 shell under readonly (observation)."""
        self.want("R6")
        run_r6(self.session)


class DroidSmoke(_VendorSmoke):
    """C2 only."""

    ADAPTER = "droid"
    BINARY = "droid"


class KiroSmoke(_VendorSmoke):
    """Kiro smoke (m3-kiro-real): only with James's consent; MESH_SMOKE_MAX_RUNS=1 per stage."""

    ADAPTER = "kiro"
    BINARY = "kiro-cli"


class AgySmoke(_VendorSmoke):
    """agy smoke (m3-smoke-agy-vibe): only with James's consent. Name a model without an effort
    suffix in MESH_SMOKE_MODEL_AGY if S-X-5 should run (build() refuses --effort otherwise)."""

    ADAPTER = "agy"
    BINARY = "agy"


class VibeSmoke(_VendorSmoke):
    """vibe smoke (m3-smoke-agy-vibe): only with James's consent. vibe has no effort flag, so
    S-X-5 is not_applicable unless MESH_SMOKE_EFFORT_VIBE=none (effort_not_listed)."""

    ADAPTER = "vibe"
    BINARY = "vibe"


if __name__ == "__main__":
    unittest.main()
