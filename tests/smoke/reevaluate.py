"""Case evaluators and offline re-evaluation of smoke results (plan §14.10; m3-r3; ACC: N12, N24, N11).

The evaluators are pure: they turn a captured run (stdout text, rc, timeout flag, the
adapter's parsed ``RunResult``) into ``(status, reason, observation)``. The capture tier
(tests/smoke/test_smoke.py) and the re-evaluation use the same functions.

Re-evaluation (Q5) never spawns:

    python3 -m tests.smoke.reevaluate <results.json> --out /tmp/mh-<staging> [--fixture-root DIR]

* the document must ``validate`` clean (sidecar sha256 of every fixture; no ``synthetic_*``);
* S-X-1, S-X-2, S-X-4, S-X-5 with a fixture: the fixture is copied into a temp run dir as
  ``stdout.log`` and the current ``adapter.parse()`` re-runs with the recorded rc;
  ``evaluated_from`` = the source results file;
* every other case (S-X-3, S-X-6, S-X-7, S-<ADAPTER>-8, and cases without a fixture) is copied
  unchanged with ``carried_from`` = the source results file;
* a new results file (Q4 name, never overwriting) is written with a sidecar under
  ``<out>/results/``, ready for ``python3 -m tests.tierlib promote``.

Stream facts are adapter-aware (m3-kiro-real): droid reads its ``system/init`` event; kiro
reads ``KiroAdapter.stream_facts`` (first ``sessionId``, ``metadata.modelId`` when present,
last ``metadata.reasoning.effort``); agy and vibe (m3-smoke-agy-vibe) read
``AgyAdapter.stream_facts`` (init ``conversation_id`` / ``model`` / ``cwd``) and
``VibeAdapter.stream_facts`` (first ``sessionId`` only). The evaluators take ``adapter_id``
(default ``droid``). vibe's export.json is not part of a smoke capture, so a re-parsed vibe
S-X-1 sees the stream only (no export outcome).
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mesh.adapters import smoke_schema
from mesh.adapters.registry import ADAPTER_CLASSES
from tests import tierlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOKEN = "MESH_SMOKE_OK"
OUTPUT_DERIVED = ("S-X-1", "S-X-2", "S-X-4", "S-X-5")
EXCERPT_CHARS = 400

Verdict = Tuple[str, Optional[str], Any]


class ReevaluateError(Exception):
    pass


def init_event(stdout: str) -> Optional[Dict[str, Any]]:
    """The first ``{"type":"system","subtype":"init"}`` JSON line, or None."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    return None


def stream_facts(stdout: str, adapter_id: str = "droid") -> Dict[str, Any]:
    """``{session_id, model, effort, cwd, vendor_exit_meaning}`` seen in a captured stream.

    droid: the init event (``reasoning_effort`` / ``reasoningEffort``); kiro: KiroAdapter.stream_facts.
    agy / vibe: ``AgyAdapter.stream_facts`` / ``VibeAdapter.stream_facts`` (m3-smoke-agy-vibe):
    agy ``stream_error`` is an explicit error in the last result event, ``vendor_exit_meaning``
    also covers a stream without any result event; vibe has neither in its stream (always None).
    Any other adapter: all None."""
    if adapter_id in ("agy", "vibe"):
        if adapter_id == "agy":
            from mesh.adapters.agy import AgyAdapter as _Adapter
        else:
            from mesh.adapters.vibe import VibeAdapter as _Adapter
        f = _Adapter.stream_facts(stdout or "")
        return {"session_id": f["session_id"], "model": f["model"], "effort": f["effort"], "cwd": f["cwd"],
                "vendor_exit_meaning": f["vendor_exit_meaning"], "stream_error": f["stream_error"]}
    if adapter_id == "kiro":
        from mesh.adapters.kiro import KiroAdapter
        f = KiroAdapter.stream_facts(stdout or "")
        return {"session_id": f["session_id"], "model": f["model"], "effort": f["effort"], "cwd": None,
                "vendor_exit_meaning": f["vendor_exit_meaning"], "stream_error": f["stream_error"]}
    if adapter_id == "droid":
        ev = init_event(stdout) or {}
        return {"session_id": ev.get("session_id"), "model": ev.get("model"),
                "effort": ev.get("reasoning_effort", ev.get("reasoningEffort")), "cwd": ev.get("cwd"),
                "vendor_exit_meaning": None, "stream_error": None}
    return {"session_id": None, "model": None, "effort": None, "cwd": None, "vendor_exit_meaning": None,
            "stream_error": None}


def excerpt(text: str) -> str:
    text = (text or "").strip()
    return text[-EXCERPT_CHARS:]


def _failed_run(rc: Optional[int], timed_out: bool) -> Optional[Verdict]:
    if timed_out:
        return "fail", "timeout", "hang"
    if rc is None:
        return "fail", "no_rc", None
    if rc != 0:
        return "fail", "rc_nonzero", None
    return None


def _vendor_error(stdout: str, parsed, adapter_id: str) -> Optional[str]:
    """The in-stream vendor error of an rc-0 run: ``parse().vendor_exit_meaning`` when a parse
    was given, else the stream's (kiro runError / refusal; agy error result or no result event;
    vibe: none in the stream). droid: always None (no such field)."""
    vem = getattr(parsed, "vendor_exit_meaning", None) if parsed is not None else None
    return vem or stream_facts(stdout, adapter_id).get("vendor_exit_meaning") or None


def eval_sx1(stdout: str, rc: Optional[int], timed_out: bool, parsed, synthetic: bool = False) -> Verdict:
    """S-X-1 headless + stream: rc 0, no vendor error in the stream, and ``parse().final_text``
    contains the token."""
    bad = _failed_run(rc, timed_out)
    if bad:
        return bad
    raw_seen = TOKEN in (stdout or "")
    if synthetic:
        return "fail", "synthetic_input", {"raw_token_seen": raw_seen}
    vem = getattr(parsed, "vendor_exit_meaning", None) if parsed is not None else None
    if vem:
        # kiro can exit 0 after a runError (auth, stream error); that is not a headless pass.
        return "fail", "vendor_error", {"raw_token_seen": raw_seen, "vendor_exit_meaning": vem}
    if parsed is not None and TOKEN in (parsed.final_text or ""):
        return "pass", None, {"raw_token_seen": raw_seen}
    return "fail", "parse_pending", {"raw_token_seen": raw_seen}


def eval_sx2(stdout: str, rc: Optional[int], timed_out: bool, parsed, synthetic: bool = False,
             adapter_id: str = "droid") -> Verdict:
    """S-X-2 session id: ``parse()`` returns the stream's session id (droid init event, kiro
    first ``sessionId``)."""
    bad = _failed_run(rc, timed_out)
    if bad:
        return bad
    sid = parsed.session_id if parsed is not None else None
    vem = _vendor_error(stdout, parsed, adapter_id)
    if vem and not synthetic:
        # Linear review A MINOR 1: a session that ended in a vendor error is not one to resume.
        return "fail", "vendor_error", {"session_id": sid, "vendor_exit_meaning": vem}
    if adapter_id == "droid":
        ev = init_event(stdout)
        consistent = ev is None or ev.get("session_id") == sid  # unchanged droid rule
    else:
        seen = stream_facts(stdout, adapter_id)["session_id"]
        consistent = seen is None or seen == sid
    if synthetic:
        return "fail", "synthetic_input", {"session_id": sid}
    if sid and consistent:
        return "pass", None, {"session_id": sid}
    return "fail", "no_session_id", {"session_id": sid}


def eval_sx4(stdout: str, rc: Optional[int], timed_out: bool, requested_model: str, synthetic: bool = False,
             adapter_id: str = "droid") -> Verdict:
    """S-X-4 model selection: the stream's model (droid init ``model``, kiro ``metadata.modelId``)
    equals the requested id; absent -> ``observed(not_observable)``."""
    bad = _failed_run(rc, timed_out)
    if bad:
        return bad
    model = stream_facts(stdout, adapter_id)["model"]
    vem = _vendor_error(stdout, None, adapter_id)
    if vem and not synthetic:
        return "fail", "vendor_error", {"init_model": model, "vendor_exit_meaning": vem}
    if synthetic:
        return "fail", "synthetic_input", {"init_model": model}
    if model is None:
        return "observed", "not_observable", {"init_model": None}
    if model == requested_model:
        return "pass", None, {"init_model": model}
    return "fail", "model_mismatch", {"init_model": model, "requested": requested_model}


def eval_sx5(stdout: str, stderr: str, rc: Optional[int], timed_out: bool, effort: str,
             synthetic: bool = False, adapter_id: str = "droid") -> Verdict:
    """S-X-5 reasoning effort: the init event echoes ``reasoning_effort == effort``; a
    rejection is ``not_applicable(model_or_effort_rejected)`` with a stderr excerpt."""
    if timed_out:
        return "fail", "timeout", "hang"
    if rc is not None and rc != 0:
        # Kiro r3 finding 6: only a rejection that names effort/model; auth, network or quota
        # failures are plain failures.
        low = (stderr or "").lower()
        if "effort" in low or "model" in low:
            return "not_applicable", "model_or_effort_rejected", {"stderr_excerpt": excerpt(stderr)}
        return "fail", "rc_nonzero", {"stderr_excerpt": excerpt(stderr)}
    if rc is None:
        return "fail", "no_rc", None
    echoed = stream_facts(stdout, adapter_id)["effort"]
    vem = _vendor_error(stdout, None, adapter_id)
    if vem and not synthetic:
        # Linear review A MINOR 2: kiro echoes the effort in metadata before a runError, so an
        # rc-0 auth failure must not verify the effort.
        return "fail", "vendor_error", {"echoed": echoed, "vendor_exit_meaning": vem}
    if synthetic:
        return "fail", "synthetic_input", {"echoed": echoed}
    if echoed == effort:
        return "pass", None, {"echoed": echoed}
    if echoed is None:
        return "observed", "not_echoed", {"echoed": None}
    return "fail", "effort_mismatch", {"echoed": echoed, "requested": effort}


#: S-X-6 exit codes that mean "ended by the harness's SIGTERM": -15 (Popen: killed by signal 15)
#: and 143 (128+15, a shell or wrapper mapping the same signal). The harness sends only SIGTERM
#: inside the window; it SIGKILLs only after the window, and then group_gone_after_term is False.
#: So -9/137 inside the window, -6 (abort), 130 (SIGINT), ... are never the harness's doing.
SX6_TERM_RCS = (-15, 143)


def eval_sx6(info: Dict[str, Any], stdout: str, stderr: str, adapter_id: str = "droid") -> Verdict:
    """S-X-6 termination (Linear review A MAJOR 2; PR #12 round 2): pass only when the process was
    alive at the SIGTERM, the group was gone within the window, and its exit is the SIGTERM's.

    * rc None -> fail(no_rc): the exit is unknown, so it is not proven to be the SIGTERM's.
    * rc in SX6_TERM_RCS -> pass (a stream error written while handling the SIGTERM is not judged).
    * rc < 0 or rc > 128 otherwise -> fail(unexpected_signal) with the signal number.
    * rc 0..128: an explicit in-stream error (``stream_error``: kiro runError, agy last result with
      status != SUCCESS or an error; vibe has none in its stream) -> fail(exited_with_error);
      rc > 0 with stderr -> fail(exited_with_error) (Kiro r5 finding 4); otherwise pass (the process
      handled the SIGTERM and exited itself; a missing runFinished alone is expected after a SIGTERM).
    """
    info = dict(info or {})
    if not info.get("alive_at_term"):
        return "fail", "exited_before_signal", info
    if not info.get("group_gone_after_term"):
        return "fail", "group_survived_sigterm", info
    rc = info.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool):
        return "fail", "no_rc", info
    if rc in SX6_TERM_RCS:
        return "pass", None, info
    if rc < 0 or rc > 128:
        info["signal"] = -rc if rc < 0 else rc - 128
        if (stderr or "").strip():
            info["stderr_excerpt"] = excerpt(stderr)
        return "fail", "unexpected_signal", info
    stream_error = stream_facts(stdout, adapter_id).get("stream_error")
    if stream_error:
        info["vendor_exit_meaning"] = stream_error
        if (stderr or "").strip():
            info["stderr_excerpt"] = excerpt(stderr)
        return "fail", "exited_with_error", info
    if rc > 0 and (stderr or "").strip():
        info["stderr_excerpt"] = excerpt(stderr)
        return "fail", "exited_with_error", info
    return "pass", None, info


def eval_sx3(stdout: str, rc: Optional[int], timed_out: bool, parsed) -> Verdict:
    """S-X-3 resume: the resumed run reports the token (raw or parsed)."""
    bad = _failed_run(rc, timed_out)
    if bad:
        return bad
    raw = TOKEN in (stdout or "")
    parsed_seen = parsed is not None and TOKEN in (parsed.final_text or "")
    if raw or parsed_seen:
        return "pass", None, {"raw_token_seen": raw, "parsed_token_seen": parsed_seen}
    return "fail", "token_missing", {"raw_token_seen": raw, "parsed_token_seen": parsed_seen}


def default_fixture_root(results_path: str, adapter: str, version: str) -> str:
    """A staged results file (``<S>/results/x.json`` with ``<S>/<adapter>/<version>/``) uses its
    staging tree; anything else uses ``<repo>/tests/fixtures/real``."""
    stage = os.path.dirname(os.path.dirname(os.path.abspath(results_path)))
    if os.path.basename(os.path.dirname(os.path.abspath(results_path))) == "results" \
            and os.path.isdir(os.path.join(stage, adapter, version)) \
            and os.path.realpath(stage) != os.path.realpath(os.path.join(REPO_ROOT, "tests", "smoke")):
        return stage
    return os.path.join(REPO_ROOT, "tests", "fixtures", "real")


def _fixture_path(root: str, rel: str) -> str:
    parts = rel.split("/")
    return os.path.join(root, parts[3], parts[4], parts[5])


def reevaluate_doc(doc: Dict[str, Any], source_name: str, fixture_root: str) -> Dict[str, Any]:
    """A new document: output-derived cases re-parsed from their fixtures, the rest carried."""
    errors = smoke_schema.validate(doc, REPO_ROOT, fixture_root=fixture_root)
    if errors:
        raise ReevaluateError(f"source results invalid: {errors}")
    adapter = ADAPTER_CLASSES[doc["adapter"]]()
    source = f"tests/smoke/results/{source_name}"
    new_cases: List[Dict[str, Any]] = []
    tmp = tempfile.mkdtemp(prefix="mh-reeval-")
    try:
        for case in doc["cases"]:
            out = dict(case)
            fx = case.get("fixture")
            if case["id"] not in OUTPUT_DERIVED or not fx:
                out["carried_from"] = source
                out["evaluated_from"] = None
                new_cases.append(out)
                continue
            if os.path.basename(fx).startswith("synthetic_"):
                raise ReevaluateError(f"synthetic fixture refused: {fx}")
            src = _fixture_path(fixture_root, fx)
            if tierlib.read_sidecar(src)["sha256"] != case["fixture_sha256"]:
                raise ReevaluateError(f"fixture sha256 mismatch: {fx}")
            run_dir = tempfile.mkdtemp(prefix=f"{case['id']}-", dir=tmp)
            shutil.copyfile(src, os.path.join(run_dir, "stdout.log"))
            with open(src, encoding="utf-8", errors="replace") as f:
                stdout = f.read()
            parsed = adapter.parse(run_dir)
            rc = case.get("rc")
            obs = case.get("observation")
            timed_out = obs == "hang" or (isinstance(obs, dict) and obs.get("value") == "hang")
            cid = case["id"]
            aid = doc["adapter"]
            if cid == "S-X-1":
                verdict = eval_sx1(stdout, rc, timed_out, parsed)
            elif cid == "S-X-2":
                verdict = eval_sx2(stdout, rc, timed_out, parsed, adapter_id=aid)
            elif cid == "S-X-4":
                verdict = eval_sx4(stdout, rc, timed_out, doc["model"], adapter_id=aid)
            else:
                effort = _effort_of(case) or doc.get("effort")
                if case.get("status") == "not_applicable" or not effort:
                    out["carried_from"] = source
                    out["evaluated_from"] = None
                    new_cases.append(out)
                    continue
                verdict = with_effort(eval_sx5(stdout, "", rc, timed_out, effort, adapter_id=aid), effort)
            out["status"], out["reason"], out["observation"] = verdict
            out["evaluated_from"] = source
            out["carried_from"] = None
            new_cases.append(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    new = dict(doc)
    new["cases"] = new_cases
    return new


def with_effort(verdict: Verdict, effort: str) -> Verdict:
    """Keep the requested effort in the S-X-5 observation (re-evaluation reads it back)."""
    status, reason, obs = verdict
    obs = dict(obs) if isinstance(obs, dict) else {"value": obs}
    obs["requested_effort"] = effort
    return status, reason, obs


def _effort_of(case: Dict[str, Any]) -> Optional[str]:
    obs = case.get("observation")
    if isinstance(obs, dict):
        return obs.get("requested_effort")
    return None


def reevaluate(results_path: str, out: str, fixture_root: Optional[str] = None,
               extra_existing: Iterable[str] = ()) -> str:
    """Re-evaluate ``results_path`` and write the new document under ``<out>/results/``."""
    if not os.path.isabs(out) or not out.startswith("/tmp/mh-") \
            or not os.path.realpath(out).startswith(os.path.realpath("/tmp") + os.sep + "mh-"):
        raise ReevaluateError(f"--out must be under /tmp/mh-*, got {out!r}")
    name = os.path.basename(results_path)
    if name.startswith("synthetic_") or "/synthetic/" in os.path.abspath(results_path):
        raise ReevaluateError(f"synthetic results refused: {results_path}")
    tierlib.read_sidecar(results_path)
    with open(results_path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or smoke_schema.parse_results_name(str(doc.get("adapter")), name) is None:
        raise ReevaluateError(f"not a results file name: {name}")
    root = fixture_root or default_fixture_root(results_path, doc["adapter"], doc["version"])
    new = reevaluate_doc(doc, name, root)
    errors = smoke_schema.validate(new, REPO_ROOT, fixture_root=root)
    if errors:
        raise ReevaluateError(f"re-evaluated document invalid: {errors}")
    rdir = os.path.join(out, "results")
    existing = set(extra_existing) | set(os.listdir(os.path.dirname(os.path.abspath(results_path))))
    if os.path.isdir(rdir):
        existing |= set(os.listdir(rdir))
    committed = os.path.join(REPO_ROOT, "tests", "smoke", "results")
    if os.path.isdir(committed):
        existing |= set(os.listdir(committed))
    new_name = smoke_schema.results_name(doc["adapter"], doc["version"], time.strftime("%Y-%m-%d", time.gmtime()), existing)
    data = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return tierlib.write_capture(out, f"results/{new_name}", data,
                                 ["python3", "-m", "tests.smoke.reevaluate", name])


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tests.smoke.reevaluate")
    parser.add_argument("results")
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixture-root", default=None)
    args = parser.parse_args(argv)
    try:
        path = reevaluate(os.path.abspath(args.results), args.out, args.fixture_root)
    except (ReevaluateError, tierlib.TierError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "results": path}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
