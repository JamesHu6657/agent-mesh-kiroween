# m3-r7b implementation notes (run m3-r7b2)

Base: main `adebc01`, branch `wt/m3-r7b2`, worktree `~/workspace/mesh-wt-m3-r7b2`.
Coder: Quest (coder). This file replaces the earlier STOP notes from `wt/m3-r7b`
(`17eeff7`, base `59f6ea1`); that branch is superseded and was not reused.

## Live evidence

Real `bin/mesh adapter show <id>` for all six vendor adapters (and `adapter list`)
against a non-testing daemon in a disposable `/tmp/mh-r7b-*` home, 2026-11-10 15:33 UTC.
`MESH_TESTING`, `MESH_SMOKE*`, `MESH_PROBE*` and `MESH_TEST_SMOKE_RESULTS_DIR` were unset.
No model run, no probe or smoke tier. Note: daemon start probes installed vendor
binaries read-only (`--version`/`--help`) as part of normal `adapter show` data; nothing
else touched a vendor CLI. init/start/list/show/stop all rc 0; home and passphrase
file removed afterwards.

| Adapter | Version | Status | launch_enabled | launch_milestone | launch_reason | results_file |
|---|---|---|---|---|---|---|
| droid | 1.6.0 | verified | true | null | null | `tests/smoke/results/droid-1.6.0-2026-11-09-3.json` |
| agy | 1.5.4 | partially_verified | false | M3 | no_smoke_evidence | null |
| kiro | 3.1.0 | partially_verified | true | null | null | `tests/smoke/results/kiro-3.1.0-2026-11-10-2.json` |
| vibe | 3.1.0 | partially_verified | false | M3 | no_smoke_evidence | null |
| hermes | null | not_installed | false | M4 | no_smoke_evidence | null |
| openclaw | null | not_installed | false | M4 | no_smoke_evidence | null |

Capabilities (supported / verification): droid yes/smoke ×4, resume unknown/smoke;
agy yes/stale ×4, resume unknown/stale; kiro headless+stream yes/smoke,
model_selection+reasoning_effort yes/help, resume unknown/smoke; vibe yes/help,
yes/help, model_selection yes/**unverified**, reasoning_effort no/help, resume
unknown/help; hermes/openclaw unknown/none ×5.

Changes since the first attempt: kiro now has launch evidence (PR #12 + C-kiro smoke,
`kiro-3.1.0-2026-11-10-2.json`: S-X-1, S-X-2, S-X-6 pass, S-X-4 observed, rest not run),
and the installed agy is now **1.5.4** (fixtures exist for 1.5.2 and 1.5.3 only).

Offline flag diffs from committed fixtures (`mesh/adapters/helpdiff.py:diff`):
droid 1.3.0→1.6.0 `help.txt` and `exec-help.txt`, agy 1.5.2→1.5.3 `help.txt`: no flags
added or removed. All nine droid emitted flags (`helpdiff.emitted_flags`) are in the
1.6.0 help. Matches the C1 log entry (2026-11-09 16:45).

## What changed

- `docs/adapters.md`: header now M3-lite; status words section adds per-capability
  fields; status table has live version, status, launch_enabled, launch_reason, results
  file / "not run", milestone (last column); evidence-rule section (N19 reasons, D5);
  capability-row table; droid section (status, per-case results with fixtures, N13/N26,
  C1 flag diff); agy/vibe probe-only section (agy 1.5.4 stale); hermes/openclaw; "What
  works today" rewritten (droid+kiro launch, D4 lead refusal); deferred list (Part 2,
  updated for kiro's partial smoke). Kiro section, unsupported profiles, inbox
  participation and test-double sections kept.
- `docs/usage.md`: §1 launch paragraph rewritten from evidence; §4 launch-executor
  status cell; §9 adds the D4 refusal paragraph (`vendor_lead_m3_lite`); new §10
  "Vendor evidence: probe, smoke and launch (M3)" (evidence rule, D5 runtime read of
  `tests/`, probe tier, smoke consent/env vars/staging, coordinator-only promotion,
  reevaluate, no sandbox, idle timeout 0, resume rule, droid `dev` commits locally per
  N26). The pinned §4 launch-lead row (J7) is unchanged.
- `tests/unit/test_docs_m3.py` (new, ACC N9/N12/N27, 7 tests): exact six-row launch
  columns; results files exist with sidecar, adapter and version match; droid case rows
  equal the results file statuses and fixtures, fixtures exist with sidecars; every
  results/fixture path mentioned exists; capability-row table literal; N13/N26 results
  pass; deferred list phrases; no false `verified` for agy/kiro/vibe; usage.md phrases.
  Expected values are literals from the live run, plus an independent read of the
  committed JSON.

## Supersessions in `tests/unit/test_docs_m19.py`

Both in `TestDocsM19.test_n9_status_table_for_six_vendor_clis`; nothing else changed.

1. **droid status** (coordinator authorization, Quest coordinator 2026-11-10): expected
   `"droid": "partially_verified"` → `"verified"`. Evidence: live `adapter show droid`
   status `verified`; all four `supported: "yes"` capabilities `smoke` from
   `droid-1.6.0-2026-11-09-3.json`. Still strict: exact word, exactly one status word
   per row.
2. **launch milestone cell** (named launch-column assertion, allowed by the run section):
   `cells[-1] == ADAPTER_CLASSES[adapter].launch_milestone` (class attribute, "M3" for
   droid/kiro) → `cells[-1] == expected_milestone[adapter]` with literals droid `—`,
   agy `M3`, kiro `—`, vibe `M3`, hermes `M4`, openclaw `M4`. Evidence: live
   `launch_milestone` null for droid and kiro (launch enabled by evidence), M3/M4 for
   the rest. The `ADAPTER_CLASSES` import is now unused but left in place.

## Spec challenges and facts found

- Plan Part 2 says droid stays `partially_verified` with launch on; the code
  (`evidence.py:apply_to_probe`) does not grade profiles, so it prints `verified`.
  Resolved by the coordinator authorization above; docs explain it.
- Part 2/D1 call kiro "probe only", but kiro now has launch evidence and is launch
  enabled. Docs follow live evidence; the deferred list says kiro smoke beyond the
  launch cases is deferred instead of "kiro smoke".
- agy drifted to 1.5.4 on the box. N22 expects agy 1.5.3 fixtures (present). Docs
  state 1.5.4 live + stale rows; a 1.5.4 probe capture is coordinator work. The docs
  table therefore pins a live version that can drift again; tests check it only
  against literals, never live.
- Resume: rows show `smoke` for droid/kiro via S-X-2, but `supported` stays `unknown`,
  so `supervisor.py:verified_resume_session` never resumes a vendor run. Docs say so.
- vibe `model_selection` prints `unverified`, outside the addendum's N20 set
  {smoke, help, none, stale}. Reported, not changed (code is outside FILES).

## Tests

- Targeted: `python3 -m unittest tests.unit.test_docs_m3 tests.unit.test_docs_m19` →
  13 tests OK.
- Mutation checks (scratch copy under /tmp/mh-*): droid status word, S-X-6 case status,
  kiro launch_enabled, deferred phrase, results file name, usage D4 phrase → each
  caught.
- Full suite: `bash scripts/test.sh` in the worktree (run alone; checked no other suite running) rc 0: unit 607 OK (1 skipped) 56.3 s, integration 280 OK 366.6 s, recovery 49 OK 36.9 s, total wall 460 s. Before (main suite run just before, /tmp/mh-s7d.log): 456 s. No tripwire output. The docs and test commits were made before this run; it covers their final state.
- No test sets `MESH_SMOKE*`/`MESH_PROBE*`; the new tests start no process.

## Fix round m3-r7b2 (Quest + Linear REVISE)

- P2: `test_docs_m19` n9 keeps the milestone literals and now also runs the real code
  path per row: `load_evidence(<id>, testing_mode=False, <documented version>)` on the
  committed results, then `apply_to_probe` with `get_adapter(<id>)`, the same two calls
  `adapter show` makes. The doc milestone must equal the returned `launch_milestone`
  (null → "—") and the launch column must equal `launch_enabled`. No launch rule is
  restated in the test. Mutations agy `launch_milestone` M3→M4 and hermes M4→M99 now fail.
- P3: `test_docs_m3` pins the version column for all six rows (agy 1.5.4 → 1.5.3 fails)
  and asserts "so no vendor run resumes today" (adapters.md) and usage §1
  "Today that is `droid` 1.6.0 and `kiro` (kiro-cli 3.1.0)".
- P3 docs: the per-capability verification list now includes `unverified` (outside the
  addendum N20 set; vibe `model_selection`); vibe summary says help-verified except
  `model_selection` yes/unverified; kiro heading now "launch open by smoke evidence".
  The plan D1 "kiro probe only" vs code note above stays.
- Mutations (8, all killed): agy class M4, hermes class M99, agy version 1.5.3, vibe
  version 3.0.0, hermes version "1.0", resume claim flipped, usage adds agy, kiro
  milestone "—"→M3. Tests: test_docs_* 14/14 OK. Docs/tests only; no full suite.
