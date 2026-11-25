"""The committed agy 1.5.12 smoke evidence is the passing rerun, and the docs say so (PR #61).

ACC: N6, N9, N11, N12, N19, N20, N22, N27

Offline only: reads tests/smoke/results/agy-1.5.12-2026-11-19.json, the captures under
tests/fixtures/real/agy/1.5.12/, README.md and docs/. No agy binary runs; the one process a
test starts is a stub written under /tmp/mh-agy1512-* that prints the committed 1.5.12 help
and model captures.

Two real agy 1.5.12 smoke runs were made on 2026-11-19:

* 11:20:59Z at Mesh commit 94436d1, the earlier argv (``--print --output-format stream-json
  ... <prompt last>``): every run exited 2 before a model call with ``--print took
  "--output-format" as its prompt``. Results sha256 41ff8c55...
* 14:08:47Z at Mesh commit d7467e2, the argv ``AgyAdapter.build()`` makes since that commit
  (``--print <prompt> --output-format stream-json ...``): S-X-1 to S-X-4 pass, S-X-6 is
  recorded as a failure (agy exits 1 on SIGTERM). Results sha256 9f796c0d...

The second replaced the first in the tree under the same file name. These tests fail if the
first one comes back, if a capture or the results file changes, or if a page again says the
argv was never run on real agy.
"""

import sys
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest

from mesh.adapters import evidence, smoke_schema
from mesh.adapters.agy import AgyAdapter
from mesh.adapters.base import EXPECTED_HELP_HASHES, RunRequest
from mesh.platform.paths import temp_root
from tests import tierlib
from tests.smoke import reevaluate as ev
from tests.smoke import test_smoke as cases
from tests.stubs import make_executable

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS_REL = "tests/smoke/results/agy-1.5.12-2026-11-19.json"
RESULTS = os.path.join(ROOT, *RESULTS_REL.split("/"))
REAL_AGY = os.path.join(ROOT, "tests", "fixtures", "real", "agy")
CAPTURES = os.path.join(REAL_AGY, "1.5.12")

RERUN_SHA256 = "9f796c0de7e1d5b21aa0b7a0ba58b8bc648433afc699cd5a5cb992ed4562a462"
RERUN_MESH_COMMIT = "d7467e2b7c6e2c4a0642edeaccd8244c689ffa81"
FIRST_ATTEMPT_SHA256 = "41ff8c55766dd4e38097040b963c44fb47a16dda44f1b63c85a0e2ae66ef321f"
FIRST_ATTEMPT_MESH_COMMIT = "94436d1f325d188c6f98ec14816ab5840033e534"
MODEL = "gemini-3.8-flash-low"
SESSION = "a4b9aab8-e897-4cf2-8ec3-911a85a5d155"

#: case id -> (status, reason, rc, capture the case reads)
EXPECTED_CASES = {
    "S-X-1": ("pass", None, 0, "smoke-S-X-1-stdout.jsonl"),
    "S-X-2": ("pass", None, 0, "smoke-S-X-1-stdout.jsonl"),
    "S-X-3": ("pass", None, 0, "smoke-S-X-3-stdout.jsonl"),
    "S-X-4": ("pass", None, 0, "smoke-S-X-1-stdout.jsonl"),
    "S-X-5": ("not_applicable", "effort_not_listed", None, None),
    "S-X-6": ("fail", "exited_with_error", 1, "smoke-S-X-6-stdout.jsonl"),
    "S-X-7": ("observed", None, 0, "smoke-S-X-7-stdout.jsonl"),
    "S-AGY-8": ("observed", None, 0, "smoke-S-AGY-8-stdout.jsonl"),
}
#: run -> (the case whose captures it wrote, resumed session id)
RUNS = {"R1": ("S-X-1", None), "R2": ("S-X-3", SESSION), "R4": ("S-X-6", None), "R5": ("S-X-7", None),
        "R6": ("S-AGY-8", None)}
REJECTION = "as its prompt"

#: Statements about agy 1.5.12 that the evidence makes false (normalized, lower case).
STALE_AGY_PHRASES = (
    "has not yet been run against a real agy",
    "not yet been run against a real agy",
    "the next consented agy smoke is the check",
    "offline unit tests only",
    "not a promoted result",
    "no 1.5.12 probe or fixture is committed",
    "still record the 1.5.4 probe",
    "neither has a promoted smoke result",
    "agy 1.5.12 failed every launch case",
    "smoke run failed",
    "adapter fix in a separate",
    "`agy` and `vibe` have no smoke results",
    "agy and vibe have no promoted results",
)


def read(rel):
    with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def normalize(text):
    return " ".join(text.split())


def load_doc():
    with open(RESULTS, encoding="utf-8") as handle:
        return json.load(handle)


def by_id(doc):
    return {c["id"]: c for c in doc["cases"]}


def capture(name):
    """Text of a committed 1.5.12 capture, after checking it against its sidecar."""
    path = os.path.join(CAPTURES, name)
    data = read_bytes(path)
    assert tierlib.read_sidecar(path)["sha256"] == tierlib.sha256_bytes(data), name
    return data.decode("utf-8")


def section(doc, heading):
    """A page section from ``heading`` up to the next heading of the same or a higher level."""
    start = doc.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    nxt = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE).search(doc, start + len(heading))
    return doc[start:nxt.start()] if nxt else doc[start:]


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_rows(text):
    return [cells(line) for line in text.splitlines() if line.startswith("|")]


class AgyRerunResultsTest(unittest.TestCase):
    """The results file and captures in the tree are the 14:08:47Z rerun, byte for byte."""

    def test_results_doc_is_the_passing_rerun(self):
        """ACC: N11, N12 - the latest agy results file is agy-1.5.12-2026-11-19.json with the rerun's sha256 (9f796c0d..., not the first attempt's 41ff8c55...), captured 2026-11-19T14:08:47Z at mesh_commit d7467e2 (not 94436d1) with gemini-3.8-flash-low under readonly; S-X-1, S-X-2, S-X-3 and S-X-4 pass, S-X-5 is not_applicable, S-X-6 fails with exited_with_error, S-X-7 and S-AGY-8 are observed; the document validates against the repository."""
        results_dir = os.path.dirname(RESULTS)
        self.assertEqual(smoke_schema.load_latest(results_dir, "agy"), RESULTS)
        self.assertEqual([n for n in sorted(os.listdir(results_dir)) if n.startswith("agy-")],
                         ["agy-1.5.12-2026-11-19.json", "agy-1.5.12-2026-11-19.json.meta.json"])
        sha = tierlib.sha256_bytes(read_bytes(RESULTS))
        self.assertEqual(sha, RERUN_SHA256)
        self.assertNotEqual(sha, FIRST_ATTEMPT_SHA256)
        meta = tierlib.read_sidecar(RESULTS)
        self.assertEqual(meta["sha256"], RERUN_SHA256)
        self.assertEqual(meta["command"], ["python3", "-m", "tests.smoke.harness", "agy"])
        self.assertEqual(meta["captured_at_utc"], "2026-11-19T14:09:32Z")

        doc = load_doc()
        self.assertEqual(smoke_schema.validate(doc, ROOT), [])
        self.assertIsNone(smoke_schema._check_sidecar(RESULTS, root=results_dir))
        head = {k: v for k, v in doc.items() if k != "cases"}
        self.assertEqual(head, {
            "schema": "mesh.smoke.v1", "adapter": "agy", "version": "1.5.12",
            "binary": "/home/user/.local/bin/agy", "captured_at_utc": "2026-11-19T14:08:47Z",
            "mesh_commit": RERUN_MESH_COMMIT, "model": MODEL, "effort": None, "profile": "readonly",
            "timeout_s": 180.0, "consent_ref": "james-2026-11-19-2036",
        })
        self.assertTrue(doc["mesh_commit"].startswith("d7467e2"))
        self.assertNotEqual(doc["mesh_commit"], FIRST_ATTEMPT_MESH_COMMIT)
        self.assertEqual([c["id"] for c in doc["cases"]], list(cases.AGY_CASES))
        for cid, (status, reason, rc, name) in EXPECTED_CASES.items():
            with self.subTest(case=cid):
                case = by_id(doc)[cid]
                fixture = smoke_schema.fixture_rel("agy", "1.5.12", name) if name else None
                self.assertEqual((case["status"], case["reason"], case["rc"], case["fixture"]),
                                 (status, reason, rc, fixture))
                self.assertIsNone(case["carried_from"])
                self.assertIsNone(case["evaluated_from"])
        c = by_id(doc)
        self.assertEqual(c["S-X-1"]["observation"], {"raw_token_seen": True})
        self.assertEqual(c["S-X-2"]["observation"]["session_id"], SESSION)
        self.assertEqual(c["S-X-2"]["observation"]["init_cwd"], c["S-X-2"]["observation"]["workspace"])
        self.assertEqual(c["S-X-3"]["observation"]["resumed_session_id"], SESSION)
        self.assertEqual(c["S-X-4"]["observation"], {"init_model": MODEL})
        self.assertEqual(c["S-X-7"]["observation"]["file_created"], False)
        self.assertEqual(c["S-AGY-8"]["observation"], {
            "denied_actions": None, "outcome": "deny", "rc": 0, "stderr_excerpt": "", "tool_errors": 0,
            "y_txt_exists": False})

    def test_captures_are_the_rerun_and_no_others(self):
        """ACC: N12 - the 1.5.12 fixture directory holds the help and models probe captures and the stdout and stderr capture of the five rerun runs (R1, R2, R4, R5, R6), each with a sidecar whose sha256 matches; every stderr capture is empty except S-X-6's "error: interrupted"; no capture carries the first attempt's `--print took ... as its prompt` rejection."""
        expected = {"help.txt", "models.txt"}
        for case_id, _ in RUNS.values():
            expected |= {f"smoke-{case_id}-stdout.jsonl", f"smoke-{case_id}-stderr.txt"}
        names = sorted(os.listdir(CAPTURES))
        self.assertEqual(sorted(n for n in names if not n.endswith(".meta.json")), sorted(expected))
        self.assertEqual(sorted(n for n in names if n.endswith(".meta.json")),
                         sorted(n + ".meta.json" for n in expected))
        for name in sorted(expected):
            with self.subTest(name=name):
                text = capture(name)
                self.assertNotIn(REJECTION, text)
                self.assertIsNone(AgyAdapter.parse_stderr_error(text))
                if name.endswith("-stderr.txt"):
                    self.assertEqual(text, "error: interrupted\n" if name == "smoke-S-X-6-stderr.txt" else "")
        doc = load_doc()
        for case in doc["cases"]:
            if case["fixture"]:
                self.assertEqual(case["fixture_sha256"],
                                 tierlib.sha256_bytes(read_bytes(os.path.join(ROOT, case["fixture"]))), case["id"])

    def test_recorded_commands_are_what_build_makes_today(self):
        """ACC: N6, N11 - each capture's sidecar records `agy --print <prompt> --output-format stream-json --model gemini-3.8-flash-low --mode plan [--conversation <id>] --print-timeout 180s` with that run's smoke prompt directly after --print, and AgyAdapter.build() makes the same argv today for the same request, so the review commit after d7467e2 did not change what was run."""
        agy = AgyAdapter()
        tmp = tempfile.mkdtemp(prefix="mh-agy1512-", dir=temp_root())
        self.addCleanup(shutil.rmtree, tmp, True)
        for run, (case_id, resumed) in RUNS.items():
            with self.subTest(run=run):
                prompt_path = os.path.join(tmp, f"{run}.md")
                with open(prompt_path, "w", encoding="utf-8") as handle:
                    handle.write(cases.PROMPTS[run])
                spec = agy.build(RunRequest(prompt_path=prompt_path, workspace=tmp, model=MODEL, effort=None,
                                            profile="readonly", resume_session_id=resumed, timeout_s=180,
                                            options={"binary": "/opt/stub/agy"}))
                built = [os.path.basename(spec.argv[0])] + spec.argv[1:]
                expected = ["agy", "--print", cases.PROMPTS[run], "--output-format", "stream-json", "--model",
                            MODEL, "--mode", "plan"]
                expected += (["--conversation", resumed] if resumed else []) + ["--print-timeout", "180s"]
                self.assertEqual(built, expected)
                for kind in ("stdout.jsonl", "stderr.txt"):
                    path = os.path.join(CAPTURES, f"smoke-{case_id}-{kind}")
                    self.assertEqual(tierlib.read_sidecar(path)["command"], built, path)

    def test_reevaluation_reproduces_the_recorded_statuses(self):
        """ACC: N11, N12 - re-evaluating the committed document offline (no spawn) with the current parser gives the same status and reason for every case: S-X-1, S-X-2 and S-X-4 are re-parsed from the real capture and pass again; parse() reads the reply `MESH_SMOKE_OK` and the session id from the S-X-1 stream."""
        doc = load_doc()
        new = ev.reevaluate_doc(doc, os.path.basename(RESULTS), os.path.join(ROOT, "tests", "fixtures", "real"))
        old_cases, new_cases = by_id(doc), by_id(new)
        self.assertEqual(list(new_cases), list(old_cases))
        for cid, case in old_cases.items():
            with self.subTest(case=cid):
                self.assertEqual((new_cases[cid]["status"], new_cases[cid]["reason"]),
                                 (case["status"], case["reason"]))
        for cid in ("S-X-1", "S-X-2", "S-X-4"):
            self.assertEqual(new_cases[cid]["evaluated_from"], RESULTS_REL, cid)
            self.assertEqual(new_cases[cid]["status"], "pass", cid)
        run_dir = tempfile.mkdtemp(prefix="mh-agy1512-", dir=temp_root())
        self.addCleanup(shutil.rmtree, run_dir, True)
        for name, log in (("smoke-S-X-1-stdout.jsonl", "stdout.log"), ("smoke-S-X-1-stderr.txt", "stderr.log")):
            shutil.copyfile(os.path.join(CAPTURES, name), os.path.join(run_dir, log))
        parsed = AgyAdapter().parse(run_dir)
        self.assertEqual((parsed.final_text, parsed.session_id, parsed.vendor_exit_meaning),
                         ("MESH_SMOKE_OK\n", SESSION, None))
        facts = AgyAdapter.stream_facts(capture("smoke-S-X-1-stdout.jsonl"))
        self.assertEqual((facts["session_id"], facts["model"], facts["stream_error"]), (SESSION, MODEL, None))
        self.assertEqual(AgyAdapter.stream_facts(capture("smoke-S-X-3-stdout.jsonl"))["session_id"], SESSION)

    def test_sx6_is_an_orderly_exit_the_evaluator_records_as_an_error(self):
        """ACC: N11, N19 - S-X-6 is the one failed launch case, and it is a termination verdict, not a launch failure: agy was alive at the SIGTERM (sent at the 5 s fallback with 2 stdout lines), its group was gone 0.208 s later, and it exited 1 with stderr "error: interrupted" and a result event of status ERROR / error "interrupted"; eval_sx6 gives fail(exited_with_error) for that exit and would pass the same stream at rc -15 or 143."""
        case = by_id(load_doc())["S-X-6"]
        self.assertEqual(case["observation"], {
            "alive_at_term": True, "first_byte": True, "group_gone_after_term": True, "lines_at_term": 2,
            "rc": 1, "seconds_to_gone": 0.208, "stderr_excerpt": "error: interrupted", "term_after_s": 5.012,
            "vendor_exit_meaning": "run_error"})
        self.assertLess(case["observation"]["lines_at_term"], cases.SIGNAL_MIN_LINES["agy"])
        stdout, stderr = capture("smoke-S-X-6-stdout.jsonl"), capture("smoke-S-X-6-stderr.txt")
        events = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([e["event"] for e in events], ["init", "step_update", "result"])
        self.assertEqual(events[1]["step_update"]["step_type"], "user_input")
        result = events[2]["result"]
        self.assertEqual((result["status"], result["error"], result["finished_reason"]),
                         ("ERROR", "interrupted", "interrupted"))
        info = {k: case["observation"][k] for k in ("alive_at_term", "first_byte", "group_gone_after_term",
                                                    "lines_at_term", "rc", "seconds_to_gone", "term_after_s")}
        status, reason, observation = ev.eval_sx6(info, stdout, stderr, "agy")
        self.assertEqual((status, reason), ("fail", "exited_with_error"))
        self.assertEqual(observation, case["observation"])
        for rc in ev.SX6_TERM_RCS:
            self.assertEqual(ev.eval_sx6(dict(info, rc=rc), stdout, stderr, "agy")[:2], ("pass", None), rc)
        # The other launch case passed, so S-X-6 alone keeps strict launch off.
        raw = evidence.load_evidence("agy", False, "1.5.12", env={})
        self.assertEqual((raw.ok, raw.reason, raw.errors), (False, "launch_cases_failed", []))
        self.assertEqual({cid: raw.cases[cid]["status"] for cid in smoke_schema.LAUNCH_CASES},
                         {"S-X-1": "pass", "S-X-6": "fail"})

    def test_write_and_shell_cases_record_no_denial(self):
        """ACC: N11, N12 - S-X-7 and S-AGY-8 are observations: neither stream has a tool step in state ERROR or a denied action; the one tool step in each is `write_file` on agy's own plan file under ~/.gemini/antigravity-cli/brain/<conversation id>/, outside the smoke workspace; "deny" is the harness word for "y.txt absent at rc 0"."""
        for name in ("smoke-S-X-7-stdout.jsonl", "smoke-S-AGY-8-stdout.jsonl"):
            with self.subTest(name=name):
                stdout = capture(name)
                self.assertEqual(cases.agy_tool_denials(stdout), {"tool_errors": 0, "denied_actions": None})
                events = [json.loads(line) for line in stdout.splitlines()]
                init = events[0]["init"]
                tools = [e["step_update"] for e in events
                         if e["event"] == "step_update" and e["step_update"]["step_type"] == "tool"]
                self.assertEqual([(t["tool_name"], t["state"]) for t in tools],
                                 [("write_file", "ACTIVE"), ("write_file", "DONE")])
                for tool in tools:
                    path = tool["tool_info"]["parameters"]["Path"]
                    self.assertRegex(path, r"/\.gemini/antigravity-cli/brain/[0-9a-f-]{36}/plan\.md\Z")
                    self.assertFalse(path.startswith(init["cwd"] + os.sep), path)
                self.assertEqual(events[-1]["result"]["status"], "SUCCESS")
                self.assertNotIn("denied_actions", events[-1]["result"])


class AgyHelpAndCapabilityTest(unittest.TestCase):
    """What the committed 1.5.12 probe captures and results give, computed offline."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mh-agy1512-", dir=temp_root())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def stub_agy(self):
        """A stub that answers ``--version``, ``--help`` and ``models`` from the committed
        1.5.12 captures and refuses anything else (rc 97)."""
        if sys.platform == "win32":
            py_path = os.path.join(self.tmp, "agy_stub.py")
            with open(py_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "import os, sys\n"
                    f"cap = {CAPTURES!r}\n"
                    "arg = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                    "if arg == '--version':\n"
                    "    print('1.5.12')\n"
                    "elif arg == '--help':\n"
                    "    sys.stdout.buffer.write(open(os.path.join(cap, 'help.txt'), 'rb').read())\n"
                    "elif arg == 'models':\n"
                    "    sys.stdout.buffer.write(open(os.path.join(cap, 'models.txt'), 'rb').read())\n"
                    "else:\n"
                    "    sys.exit(97)\n"
                )
            path = os.path.join(self.tmp, "agy.cmd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f'@echo off\r\n"{sys.executable}" -P -s "{py_path}" %*\r\n')
            return path
        path = os.path.join(self.tmp, "agy")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         'case "$1" in\n'
                         "  --version) echo 1.5.12 ;;\n"
                         f"  --help) exec cat '{os.path.join(CAPTURES, 'help.txt')}' ;;\n"
                         f"  models) exec cat '{os.path.join(CAPTURES, 'models.txt')}' ;;\n"
                         "  *) exit 97 ;;\n"
                         "esac\n")
        make_executable(path)
        return path

    def test_help_text_is_byte_identical_in_1_5_2_1_5_3_and_1_5_12(self):
        """ACC: N22 - the committed help.txt of agy 1.5.2, 1.5.3 and 1.5.12 are the same bytes (sha256 EXPECTED_HELP_HASHES["agy"]); the 1.5.12 capture is a real `agy --help` capture with a matching sidecar; docs/adapters.md names that file and that sha256."""
        hashes = {v: hashlib.sha256(read_bytes(os.path.join(REAL_AGY, v, "help.txt"))).hexdigest()
                  for v in ("1.5.2", "1.5.3", "1.5.12")}
        self.assertEqual(set(hashes.values()), {EXPECTED_HELP_HASHES["agy"]}, hashes)
        self.assertEqual(read_bytes(os.path.join(REAL_AGY, "1.5.2", "help.txt")),
                         read_bytes(os.path.join(CAPTURES, "help.txt")))
        meta = tierlib.read_sidecar(os.path.join(CAPTURES, "help.txt"))
        self.assertEqual((meta["command"], meta["sha256"]), (["agy", "--help"], EXPECTED_HELP_HASHES["agy"]))
        self.assertEqual(tierlib.read_sidecar(os.path.join(CAPTURES, "models.txt"))["command"], ["agy", "models"])
        self.assertIn(MODEL, AgyAdapter().parse_models_from_output(capture("models.txt")))
        prompt_arg = normalize(section(read("docs/adapters.md"), "#### Antigravity prompt argument (agy 1.5.12)"))
        self.assertIn("`tests/fixtures/real/agy/1.5.12/help.txt`", prompt_arg)
        self.assertIn(f"`{EXPECTED_HELP_HASHES['agy']}`", prompt_arg)
        self.assertIn("byte-identical to the 1.5.2 and 1.5.3 fixtures", prompt_arg)

    def test_status_and_capability_rows_follow_the_probe_and_evidence_code(self):
        """ACC: N9, N20, N27 - a probe of a stub that prints the committed 1.5.12 help and model captures, graded by evidence.apply_to_probe with the committed results, gives partially_verified, launch off with launch_cases_failed, headless / stream_output / model_selection `yes / smoke`, reasoning_effort `yes / stale` and resume `unknown / smoke`; the agy rows of docs/adapters.md say exactly that, and no longer show 1.5.4."""
        agy = AgyAdapter()
        probe = agy.probe(binary_path=self.stub_agy())
        data = probe.to_dict()
        self.assertEqual((data["version"], data["status"], data["help_sha256"], data.get("flag_diff")),
                         ("1.5.12", "partially_verified", EXPECTED_HELP_HASHES["agy"], None))
        self.assertIn(MODEL, data["models"])
        graded = evidence.apply_to_probe(data, agy, evidence.load_evidence("agy", False, data["version"], env={}))
        self.assertEqual((graded["status"], graded["launch_enabled"], graded["launch_reason"],
                          graded["launch_milestone"], graded["results_file"]),
                         ("partially_verified", False, "launch_cases_failed", "M3", RESULTS_REL))
        order = ("headless", "stream_output", "model_selection", "reasoning_effort", "resume")
        computed = [f"{graded['capabilities'][k]['supported']} / {graded['capabilities'][k]['verification']}"
                    for k in order]
        self.assertEqual(computed, ["yes / smoke", "yes / smoke", "yes / smoke", "yes / stale", "unknown / smoke"])
        for name in ("headless", "stream_output", "model_selection", "resume"):
            cap = graded["capabilities"][name]
            self.assertEqual((cap["verified_version"], cap["verified_at"], cap["results_file"]),
                             ("1.5.12", "2026-11-19T14:08:47Z", RESULTS_REL), name)

        doc = read("docs/adapters.md")
        capability = section(doc, "### Capability rows")
        rows = {row[0]: row[1:] for row in table_rows(capability)}
        self.assertEqual(rows["Adapter"], list(order))
        self.assertEqual(rows["agy 1.5.12"], computed)
        self.assertFalse([name for name in rows if name.startswith("agy") and name != "agy 1.5.12"], rows)
        self.assertIn("The agy row is not copied from a live `adapter show`", normalize(capability))
        status = [row for row in table_rows(section(doc, "## Status of the six vendor CLIs")) if "(`agy`)" in row[0]]
        self.assertEqual(status, [["Antigravity (`agy`)", "yes", graded["version"], graded["status"], "false",
                                   graded["launch_reason"], RESULTS_REL,
                                   "Allowed with evidence warning and supported profile"]])


class AgyDocsFollowTheRerunTest(unittest.TestCase):
    """README.md and docs/ describe the rerun, name the first attempt as the reason for the fix,
    and claim nothing the results do not record."""

    def test_adapters_md_no_longer_says_the_argv_was_never_run_on_real_agy(self):
        """ACC: N12, N27 - docs/adapters.md, docs/usage.md, docs/plan.md and README.md have none of the statements the rerun made false ("has not yet been run against a real agy", "not a promoted result", "no 1.5.12 probe or fixture is committed", "Neither has a promoted smoke result", "smoke run failed", ...); adapters.md says the form has been run against real agy 1.5.12, names the results file, the rerun commit d7467e2, the review commit 7be01ec as not changing the argv, and the first attempt at 94436d1 as the reason for the change."""
        for page in ("docs/adapters.md", "docs/usage.md", "docs/plan.md", "README.md"):
            text = normalize(read(page)).lower()
            for phrase in STALE_AGY_PHRASES:
                with self.subTest(page=page, phrase=phrase):
                    self.assertNotIn(phrase, text)
        prompt_arg = normalize(section(read("docs/adapters.md"), "#### Antigravity prompt argument (agy 1.5.12)"))
        for phrase in (
            "This form has been run against real agy 1.5.12",
            f"`{RESULTS_REL}`",
            "first smoke attempt of 2026-11-19 (captured 11:20:59Z at Mesh commit `94436d1`, before this change)",
            "exited 2 in every run before any model call",
            '`--print took "--output-format" as its prompt`',
            "That attempt is the reason for this change.",
            "replaced, under the same file name, by the rerun in the next item.",
            "rerun with this form (captured 14:08:47Z the same day at Mesh commit `d7467e2`",
            "S-X-1 to S-X-4 pass",
            "It was captured at `d7467e2`.",
            "The later review commit `7be01ec` only narrowed which stderr text counts as `cli_usage_error`; "
            "it did not change the argv",
            "No recorded run on 1.5.3 or 1.5.4 used this form with `--print` spelled out",
            "The earlier trailing-prompt form has no recorded success on any agy version.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt_arg)
        doc = load_doc()
        self.assertTrue(doc["mesh_commit"].startswith("d7467e2"))
        self.assertEqual(doc["captured_at_utc"], "2026-11-19T14:08:47Z")
        self.assertTrue(FIRST_ATTEMPT_MESH_COMMIT.startswith("94436d1"))

    def test_case_table_quotes_the_results_file(self):
        """ACC: N12, N27 - the "Antigravity smoke results (agy 1.5.12)" table has one row per recorded case with that case's status and fixture (an existing capture with a sidecar), the recorded model and profile, and the S-X-6 facts as recorded (exited_with_error, rc 1, `error: interrupted`, 0.208 s, SIGTERM at 5.012 s with 2 stdout lines); the text calls S-X-6 a termination verdict and S-X-7 / S-AGY-8 observations with no recorded denial."""
        doc = load_doc()
        text = section(read("docs/adapters.md"), "#### Antigravity smoke results (agy 1.5.12)")
        flat = normalize(text)
        self.assertIn(f"`{RESULTS_REL}`: model `{doc['model']}`, profile `{doc['profile']}` (`--mode plan`)", flat)
        self.assertIn("five runs on 2026-11-19", flat)
        rows = table_rows(text)
        self.assertEqual(rows[0], ["Case", "Result", "Recorded", "Fixture"])
        seen = {}
        for row in rows[2:]:
            self.assertEqual(len(row), 4, row)
            cid = re.match(r"S-[A-Z]+-\d+", row[0]).group(0)
            case = by_id(doc)[cid]
            seen[cid] = row
            with self.subTest(case=cid):
                self.assertEqual(row[1], case["status"])
                if case["fixture"] is None:
                    self.assertEqual(row[3], "—")
                else:
                    self.assertEqual(row[3], f"`{case['fixture']}`")
                    tierlib.read_sidecar(os.path.join(ROOT, case["fixture"]))
                if case["reason"]:
                    self.assertIn(f"`{case['reason']}`", row[2])
                if case["rc"] is not None:
                    self.assertIn(f"rc {case['rc']}", " ".join(seen[rid][2] for rid in seen
                                                               if by_id(doc)[rid]["fixture"] == case["fixture"]))
        self.assertEqual(list(seen), [c["id"] for c in doc["cases"]])
        sx6 = by_id(doc)["S-X-6"]["observation"]
        self.assertIn(f"`{sx6['stderr_excerpt']}`", seen["S-X-6"][2])
        self.assertIn(f"process group gone {sx6['seconds_to_gone']} s later", seen["S-X-6"][2])
        self.assertIn(f"`init.model` is `{doc['model']}`", seen["S-X-4"][2])
        shell = by_id(doc)["S-AGY-8"]["observation"]
        self.assertIn(f"outcome `{shell['outcome']}`", seen["S-AGY-8"][2])
        self.assertIn(f"{shell['tool_errors']} tool errors", seen["S-AGY-8"][2])
        self.assertIn("no `denied_actions`", seen["S-AGY-8"][2])
        self.assertIn("no `y.txt`", seen["S-AGY-8"][2])
        self.assertIn("no `x.txt`", seen["S-X-7"][2])
        for phrase in (
            "S-X-6 fails as a termination case, not as a launch.",
            f"The SIGTERM went out at the 5 s fallback ({sx6['term_after_s']} s), when agy had printed "
            f"{sx6['lines_at_term']} stdout lines and no model output.",
            "wrote a `result` event with status `ERROR` and error `interrupted`, printed `error: interrupted` "
            "and exited 1",
            f"its process group was gone {sx6['seconds_to_gone']} s after the signal",
            "(rc -15 or 143)",
            "recorded as `exited_with_error`, like vibe's exit 3 with stderr",
            "known evaluator issue",
            "S-X-6 keeps agy at `launch_cases_failed` in strict mode",
            "S-X-7 and S-AGY-8 are observations, not permission guarantees.",
            "`~/.gemini/antigravity-cli/brain/<conversation id>/`, outside the workspace",
            "No tool step ended in state `ERROR`.",
            "no denial was recorded",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, flat)

    def test_status_text_readme_usage_and_plan_follow_the_rerun(self):
        """ACC: N9, N12, N19 - the paragraph under the adapters.md status table, the "Antigravity and Vibe" paragraph, the Deferred list, the README agy row, usage.md section 1 and the plan.md update all say agy 1.5.12 passed S-X-1 to S-X-4 (headless, session id, resume, model) and that S-X-6 is recorded as a failure because agy exits 1 on SIGTERM; the README row keeps the evidence-warning launch cell and does not say "denied" (no denial was recorded); vibe's statements stay as its results record them (S-X-1 pass, S-X-6 fail at rc 3)."""
        doc, adapters = load_doc(), read("docs/adapters.md")
        with open(smoke_schema.load_latest(os.path.dirname(RESULTS), "vibe"), encoding="utf-8") as handle:
            vibe = by_id(json.load(handle))
        self.assertEqual((vibe["S-X-1"]["status"], vibe["S-X-6"]["status"], vibe["S-X-6"]["rc"]), ("pass", "fail", 3))
        self.assertEqual(by_id(doc)["S-X-6"]["rc"], 1)

        status = normalize(section(adapters, "## Status of the six vendor CLIs"))
        for phrase in (
            "agy 1.5.12 passed headless, session id, resume and model selection (S-X-1 to S-X-4) with the prompt "
            "directly after `--print`",
            "S-X-6 is recorded as a failure because agy exits 1 with `error: interrupted` on SIGTERM (its process "
            "group was gone in 0.2 s)",
            "A first attempt the same day, with the earlier argv, exited before any model call in every case; it "
            "is the reason for the argv change",
            "vibe 3.1.0 passed headless, session id and resume, and S-X-6 is recorded as a failure because vibe "
            "exits 3 on SIGTERM",
        ):
            with self.subTest(where="status", phrase=phrase):
                self.assertIn(phrase, status)

        both = normalize(section(adapters, "### Antigravity and Vibe").split("####")[0])
        for phrase in (
            f"Its smoke evidence is the 1.5.12 run of 2026-11-19, `{RESULTS_REL}`",
            "The 1.5.2, 1.5.3 and 1.5.12 help texts are byte-identical",
            "its smoke evidence is `tests/smoke/results/vibe-3.1.0-2026-11-19.json`",
            "Both results pass S-X-1 and fail S-X-6, so warn-mode launch is possible (with an evidence warning) "
            "and strict-mode launch is not.",
        ):
            with self.subTest(where="Antigravity and Vibe", phrase=phrase):
                self.assertIn(phrase, both)

        deferred = normalize(section(adapters, "## Deferred"))
        for phrase in (
            "the agy 1.5.12 and vibe 3.1.0 results (2026-11-19) fail a launch case, S-X-6: both pass S-X-1, and "
            "each CLI exits non-zero on SIGTERM (agy 1, vibe 3) with its process group gone in 0.2 s",
            "No agy effort run was made (S-X-5 `not_applicable`).",
            "No vendor resume capability is both supported and smoke-verified.",
        ):
            with self.subTest(where="Deferred", phrase=phrase):
                self.assertIn(phrase, deferred)

        row = next(cells(line) for line in read("README.md").splitlines()
                   if line.startswith("|") and "(`agy`)" in line)
        self.assertEqual((row[0], row[1], row[3]), ("Antigravity (`agy`)", doc["version"],
                                                    "yes, with an evidence warning"))
        for phrase in ("smoke cases (readonly, prompt passed directly after `--print`)",
                       "headless, session id, resume, model pass",
                       "SIGTERM recorded as fail (agy exits 1 with `error: interrupted`, the process group was "
                       "gone in 0.2 s)",
                       "the file-write and `touch y.txt` prompts left no file",
                       "real stream fixtures from 1.5.3 for parsing"):
            with self.subTest(where="README", phrase=phrase):
                self.assertIn(phrase, row[2])
        self.assertNotIn("denied", row[2].lower())
        self.assertNotIn("failed", row[2].lower())
        self.assertFalse(os.path.exists(os.path.join(REAL_AGY, "1.5.2", "stream-step1-denied.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(REAL_AGY, "1.5.3", "stream-step1-denied.jsonl")))

        usage = normalize(read("docs/usage.md"))
        self.assertIn("the `agy` 1.5.12 and `vibe` 3.1.0 smoke results pass the headless case and fail the SIGTERM "
                      "case (S-X-6), so both launch with a warning in `warn` mode and are refused in `strict` mode",
                      usage)
        plan = normalize(read("docs/plan.md"))
        self.assertIn("A rerun with that argv on real agy 1.5.12 the same day (Mesh commit `d7467e2`) passed S-X-1 "
                      "to S-X-4, so mode B is the verified mode on 1.5.12; S-X-6 is recorded as a failure because "
                      f"agy exits 1 on SIGTERM (`{RESULTS_REL}`)", plan)


if __name__ == "__main__":
    unittest.main()
