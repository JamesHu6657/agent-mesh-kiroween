# Implementation notes: m3-r7a (m3-e2e)

Base: `43ed1ee`, branch `wt/m3-r7a`. Scope: only
`tests/smoke/test_e2e.py` and this notes file.
Design: `docs/coordination/plans/m3-plan.md`, full m3-r7a section and common coder block.
N13 is in `docs/acceptance-checklist.md`; N25/N26 are in the approved plan and
`docs/coordination/m3-acceptance-addendum.md`, not the base checklist.

## What was built

- `DroidE2E.test_n13_readonly` (ACC: N13, N25): a real CLI-path scenario for a
  coordinator to run after consent. Creates a shim-free `MeshHarness(use_shims=False)`
  home under `/tmp/mh-*`, initializes it, starts its daemon, probes the absolute
  droid binary, creates an inbox lead and a single launch executor with an explicit
  allowed/default model and default `readonly` profile. Creates the exact token task
  with profile `readonly`, timeout `300s` and one maximum attempt. Uses `wait_for`
  for submission. Checks the deliverable token, excludes raw type-event lines in a
  fallback summary, and independently requires the adapter's parsed `final_text`
  to contain the token. Checks one task run, absolute argv[0], `--model`,
  `--log-group-id`, absence of autonomy/unsafe flags, and live doctor invariants.
- `DroidE2E.test_n26_code_task` (ACC: N26, N25): selected only for C5. Uses
  `GitFixture` primitives to initialize a hermetic `/tmp/mh-*` repo with repo-local
  fixture identity `t <t@example.invalid>` and **no remote**. Does not use
  `make_k0()`, which creates an origin. Uses `GitMeshHarness(use_shims=False)` to
  carry the fixture's hermetic git environment into the daemon and task.
  The project's tests command checks `hello.txt` has `MESH_DEMO_OK`.
  Runs the exact approved create-and-commit task with profile `dev`, timeout
  `600s`, one attempt, and verifies argv `--auto medium`. Waits for lease release
  before merging, allowing post-submit cleanup to finish.
  Requires clean submitted git state, at least one commit, the task branch tip
  matching the submitted HEAD, and a non-Mesh author. The inbox lead accepts and
  merges. Requires tests `passed`, task `merged`, exact ordered merge parents
  `[previous integration tip, agent task tip]`, and the file content on integration.
  Verifies no remote or remote-tracking refs before/after, unchanged `main`, live
  doctor invariants, and the plan's read-only checkout branch-list check.
- `stage_record`: successful scenarios write redacted stdout/stderr and a JSON
  e2e record under `MESH_SMOKE_OUT` through `tierlib.write_capture`, with checked
  hashes and provenance sidecars. Records are named `e2e-N13-...` / `e2e-N26-...`,
  not `droid-<version>-<date>.json`: they are e2e evidence, not adapter-capability
  smoke results, and cannot replace C3 launch evidence. Only the coordinator may
  review/promote them with `tierlib.promote`. No evidence was generated here.

## Gate and run cap

`setUp` first reads, but never changes, the environment. It calls the existing
`tests.smoke.harness.gate`: `MESH_SMOKE=1`, adapter selection and droid consent
must all be present. Next it requires `MESH_SMOKE_E2E=droid`.
All consent/selection gate exits occur before binary resolution, fixture/home creation,
daemon initialization, probe or any subprocess call.

Without `MESH_SMOKE_E2E_CODE=1`, only N13 is selected. With it, only N26 is
selected, so the exact C5 command with `MAX_RUNS=1` cannot spend its run on N13.
The existing `load_config` rejects missing/invalid run caps, `auto`/missing
models, non-absolute/non-executable/shim binaries, and unsafe output roots.
A module-wide counter reserves a slot before setup and never refunds failed
attempts; exhausted slots skip with `run_cap`. Each scenario creates one task
with `--max-attempts 1`. No launch lead or automatic retry can spend another run.

The coder did not set any `MESH_SMOKE*` variable, open either gate, execute
droid/another vendor CLI, or run either real scenario. Neither test is added to
the default suite. Harness cleanup stops the daemon and its own run children,
checks invariants/tripwires and removes temporary homes; the GitFixture cleans
its own repo. No cleanup helpers or iteration scripts were created.

## Exact C4 command (coordinator only, not executed)

Copied verbatim from the approved plan:

```sh
cd /workspace/mesh-fresh-v1
MESH_SMOKE=1 MESH_SMOKE_ADAPTERS=droid MESH_SMOKE_CONSENT=droid MESH_SMOKE_E2E=droid \
MESH_SMOKE_MODEL_DROID=claude-sonnet-5-5 MESH_SMOKE_MAX_RUNS=1 MESH_SMOKE_OUT=/tmp/mh-e2e-<utc> \
python3 -m unittest tests.smoke.test_e2e -v
```

The coordinator substitutes the UTC suffix as the plan intends. C4 requires
C3 S-X-1/S-X-6 passes, S-DROID-8 not hanging, droid 1.6.0, no droid coder running,
and recorded credits. C5 remains optional, separately consented after C4 and the
Factory quota check; its command adds `MESH_SMOKE_E2E_CODE=1` and a fresh output
directory. Nothing here asserts that C4 or C5 has passed.

## Compatibility, assertions and gaps

- No blocking CLI/helper gap found. `GitFixture` is in `tests/gitfixture.py`
  rather than `tests/helpers.py`; `MeshHarness` and `wait_for` are in helpers.
- There is no `--allowed-profile` agent flag. The design explicitly permits the
  alternative `max_unapproved_profile=dev`; the existing
  `--max-unapproved-profile dev` implements that alternative.
- `task create --timeout` uses the shared parent flag and forwards it into the
  task payload, so the approved `300s`/`600s` flags are available.
- No existing assertion, behavior, production file, fixture or smoke result
  was changed, weakened or deleted.
- The task-specific instruction permits only one final default-suite run. It
  overrides the common block's before-change baseline and per-substep reruns.
  N13 was staged separately before adding N26; both code commits are made only
  after the single final validation run. No before-change timing was measured.
- Git commits use the environment's configured identity, without overriding it.

## Validation

- New tests: `python3 -m unittest tests.smoke.test_e2e -v`, no `MESH_SMOKE*`
  variables: rc 0, 2 tests, both skipped at `MESH_SMOKE!=1`, unittest time
  `0.000s`. The gate exits before any process-start path.
- Existing suite: `bash scripts/test.sh` ran exactly once, rc 0, with
  `MESH_TEST_JOBS=2` chosen conservatively for the shared box. A Python subprocess
  wrapper set only that worker limit, checked that no `MESH_SMOKE*` names were
  present, and measured the command's wall time.

  | Tier | Tests | Wall time |
  | --- | ---: | ---: |
  | Unit | 365 | 73.181s |
  | Integration | 273 | 765.605s |
  | Recovery | 49 | 74.048s |
  | Total | 687 | 914.267s measured (runner: 914s) |

  All tests passed; both the suite and default tripwires were empty or absent,
  as checked by `scripts/test.sh`. Fixture hashes passed. The common block's
  under-10-minute timing target was **not met**. This was the required single
  run on a shared box with two workers, not an idle-box A1 measurement; no
  before-change run or concurrent-suite attribution is claimed. No performance
  changes outside the two-file scope or forbidden rerun were attempted.
- `git diff --check`: rc 0. The read-only
  `git branch --list 'mesh/*'` checkout check: rc 0, no output.
- N13/N26 are implemented but **not run in real form**; their acceptance evidence
  remains for C4/C5. N25's closed-gate behavior is exercised here; its broader
  existing offline checks remain in the default suite.

## Commits

- `8d48df1` `test(m3): opt-in real e2e for N13 (ACC: N13)`
- `bf3a825` `test(m3): opt-in droid code-task demo (ACC: N26)`
- Notes sub-step: `docs(m3): notes for r7a (ACC: N13, N26, N25)`.

The pre-existing untracked `prompts/droid-m3-r7a.md` was left untouched and
unstaged. No push, fetch, branch creation or merge was performed in this session.

## Round 2 (coordinator, after docs/coordination/reviews/linear-m3-r7a.md)
- MINOR fixed: with the smoke/E2E gates open but MESH_SMOKE_MAX_RUNS, MESH_SMOKE_MODEL_DROID or MESH_SMOKE_OUT absent, setUp now calls skipTest naming the missing variables before load_config (test_e2e.py REQUIRED_VARS). Present-but-invalid values still raise SmokeConfigError in load_config.
- New offline test tests/unit/test_smoke_e2e_gate.py (4 tests: each missing var skips, all missing skips, invalid values raise, closed gate skips first). On the round-1 test_e2e.py it fails with 2 errors: an AttributeError (REQUIRED_VARS does not exist in the round-1 module) and a SmokeConfigError from the all-missing test (load_config raises instead of skipping); with the fix 4/4 OK. No process is started.

## r7c (coordinator, 2026-11-10 04:45 CST, after the first C4 attempt)
- C4 attempt 1 failed in setup, before any model run: `agent create e2e-lead --adapter none --delivery inbox --can-lead` returned rc 2 INVALID_CONCURRENCY (max_concurrency None). Inbox agents need an explicit `--max-concurrency` (as in tests/integration/test_approvals.py:1301). Only `adapter probe droid` (no quota) had run; no droid exec started, no Factory credits used.
- Fix: the lead is created with `--max-concurrency 1`.
- Offline setup dry-run with the C4 env, `submit_task` patched to call `task create ... --dry-run` and stop: setup completes (daemon, probe, lead, token, executor, project) and `task create --dry-run` returns rc 0 (would: task -> assigned). No model run.
