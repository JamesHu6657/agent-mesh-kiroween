# Implementation notes: M3-lite summary (m3-r8)

Summary of the M3 runs: historical baseline main `8602d33` (all M3 PRs #11–#22 merged; #11 m3-r7d as `2370129`, 2026-11-11 02:58); current base main `f66ae8a` adds #23 m3-p3-cleanup (merge `9c02997`) plus the gate fix run m3-gatefix (merged as `a1f0301`). Per-run detail stays in `docs/impl-notes-m3-*.md`; this file does not replace them. Gate status and evidence: [docs/m3-gate-report.md](m3-gate-report.md); open items in its section 6.

## 1. Architecture changes, by run

| Run (merge) | What changed | Files |
|---|---|---|
| m3-r1 probe (in `de8b6b9`) | Help flag diff over the golden matrix; capture writer with `.meta.json` sidecars `{command, captured_at_utc, sha256}` and O_EXCL promote into `tests/fixtures/real/` or `tests/smoke/results/` only; opt-in probe tier; `MeshHarness(use_shims=False)`. | `mesh/adapters/helpdiff.py`, `tests/tierlib.py`, `tests/probe/test_probe.py`, `tests/helpers.py` |
| C1 | droid 1.6.0 and agy 1.5.3 help promoted; no flag diff, no missing emitted flag. | `tests/fixtures/real/droid/1.6.0/`, `agy/1.5.3/` |
| m3-r2 smoke core (in `de8b6b9`) | Results schema `mesh.smoke.v1`, `CASE_CAPS`, `LAUNCH_CASES = (S-X-1, S-X-6)`, `validate()`, Q4 naming; harness with double opt-in, run cap, explicit model, absolute binary, `/tmp/mh-*` staging, redaction, group kill with `grace_s`. | `mesh/adapters/smoke_schema.py`, `tests/smoke/harness.py` |
| m3-r3 smoke cases (in `de8b6b9`) | droid R1–R6 (S-X-1…7, S-DROID-8); offline re-evaluation (`evaluated_from` / `carried_from`). r5b: S-X-6 signals after the second stdout line and fails `exited_with_error`; harness ignores a second SIGTERM during cleanup. | `tests/smoke/test_smoke.py`, `tests/smoke/reevaluate.py` |
| m3-r5 evidence (in `de8b6b9`) | `effective_launch()` decides vendor launch from the newest good probe row for the agent's binary + latest results at that version + `validate()` + launch cases. Reasons `no_probe_version`, `no_smoke_evidence`, `evidence_stale`, `evidence_invalid`, `launch_cases_failed`. Checked at AG step 4, `task retry`, `task reject`. `adapter list/show/probe` print per-capability `verification`, `launch_enabled/milestone/reason`, `results_file`. | `mesh/adapters/evidence.py`, `base.py`, `handlers/adapter.py`, `handlers/task.py` |
| m3-r4..r4e (`bd0929e`) | Adopted runs: seen map of (pid, starttime) persisted in `<run_dir>/seen.json`, passed to `run_targets`/`begin`; group stays trusted after wrapper exit while a seen member lives. Unusable seen state → persisted hold (`seen_hold:<run_id>` meta row), `run.seen_unusable` event, `run release --force-seen-unusable` applied after COMMIT. Vendor lead turns refused first in `lead_launch_check` (`vendor_lead_m3_lite`). | `supervisor.py`, `terminate.py`, `lead_turns.py`, `handlers/run.py`, `server.py`, `fake_agent.py` |
| m3-r9..r9d (`0a11c6c`, `1fbfe9a`, `b8d102c`, `2869403`) | `base_commit` from `refs/heads/B`; deliver CAS with pinned shas; base_moved on descendant; git errors kept; E17 / lead-action test stabilisation. | `mesh/core/repos.py`, `mesh/daemon/merge.py` and tests |
| C2 | droid 1.6.0 smoke, `claude-sonnet-5-5`, readonly: S-X-1 `parse_pending`, S-X-2..6 pass. | `droid-1.6.0-2026-11-09{,-2}.json` |
| m3-r6 (`f9e7214`) | Real droid `parse()`: session id from the first `system/init`, `final_text` from the last `completion.finalText`, usage from that completion; D6 demotion of class `reasoning_effort` to `help` (droid.py:43, kiro.py:43). Built by Kiro from a Kiro spec. | `mesh/adapters/droid.py`, `kiro.py` |
| C3 (`43ed1ee`) | Offline re-evaluation → `droid-1.6.0-2026-11-09-3.json`, all launch cases pass; droid `verified`, launch on. | results |
| m3-r7a, r7c (`29de59c`, `9d2c08f`) | Opt-in real e2e (N13) and droid `dev` code-task demo (N26); inbox lead needs `--max-concurrency 1`. | `tests/smoke/test_e2e.py` |
| C4, C5 (`2f66528`, `50e9ae2`) | N13 and N26 pass on droid 1.6.0. | e2e records |
| m3-parse-fixtures (`9de1d32`) | Real agy 1.5.3 and vibe 3.1.0 output promoted from coordinator logs. | `tests/fixtures/real/agy/1.5.3/`, `vibe/3.1.0/` |
| m3-agyparse(+p3), m3-vibeparse(+p3) (`de3ed0e`, `e51a34d`, `628de39`, `db8ab40`) | Real `parse()` for agy (stream-json `result` event; no `result` → `run_error`; stderr `AGY_ERROR`) and vibe (last assistant message; export.json session/usage/outcome → `vendor_exit_meaning`). Worker 2 as parallel coder. | `mesh/adapters/agy.py`, `vibe.py` |
| m3-kiro-real #12 (`fe761cc`, merge `ca11c1e`) | Real kiro `parse()`, kiro smoke cases, rc-0 vendor error = T8 `vendor_error`; launch opened by evidence `kiro-3.1.0-2026-11-10-2.json`. | `mesh/adapters/kiro.py`, `tests/smoke/` |
| m3-smoke-agy-vibe #17 (`d9f2d25`) | Offline agy/vibe smoke cases; no real runs. | `tests/smoke/test_smoke.py`, `tests/unit/test_smoke_agy.py`, `test_smoke_vibe.py` |
| m3-r7b redo + r7b2 (`053bf26`, `eb2834d`) | `docs/adapters.md`, `docs/usage.md` from live evidence; `tests/unit/test_docs_m3.py`. | docs |
| flake-hunt #18, parse-polish2 #19, a1-speed #21 | Load-flake fixes; parse polish; suite speed (543 → 518 s on the same tree). | tests |
| test-kdf #22 (`f4d637b`) | init/doctor hardening; cheap scrypt only in testing mode. | `mesh/core/`, doctor |
| runtargets-race #20 (`2cefb8c`) | Two-scan rule for `run_targets`; scan→begin identity handoff; recovery handoffs. | `mesh/daemon/terminate.py`, `supervisor.py` |
| m3-r7d PR #11 (head `d38bfed`, merge `2370129`) | Reap leaked `/tmp/mh-*` test daemons: pidfd-pinned stop, per-harness token, positional launcher argv parser, effective home. Quest final ACCEPT r7d7, Linear final ACCEPT after 4 REVISE rounds. | `scripts/coord/reap_daemons.py`, `scripts/test.sh`, `tests/helpers.py` |
| m3-p3-cleanup PR #23 (head `0bd6e06`, merge `9c02997`) | Reap launcher no-match rows, pidfd cleanup, main guard at EOF, a1-speed notes. Coder: droid `gemini-4-carbon` on box2. | `tests/integration/test_daemon.py`, `tests/unit/test_reap_daemons.py`, `test_runtargets_race.py`, `docs/impl-notes-m3-a1-speed.md` |
| m3-gatefix (gate review fixes, merge `a1f0301`) | Recovery adopt records the group members before returning (P1-1); merge test step scans before reaping the wrapper (`waitid` WNOWAIT, P1-2). | `mesh/daemon/recovery.py`, `supervisor.py`, `merge.py`, `tests/unit/test_m3_gatefix.py` |

## 2. Decisions

| # | Decision | Source |
|---|---|---|
| G | Gate adapter droid 1.6.0; kiro probe-only and reviewer. | James 2026-11-09 12:44 |
| D1 | M3-lite scope: droid only; kiro/agy/vibe smoke, kiro/agy `dev`, idle timeout, vendor lead turns deferred; N11 scoped pass. | 12:44 |
| D2 | Consent C2/C4 on droid, `claude-sonnet-5-5`, staged; Kiro ceiling 3000. 13:18: weekly quota used freely, +1 point stop dropped. | 12:44, 13:18 |
| D3 | C5/N26 optional stretch after C4 on droid `dev`. | plan (12:45); log lists "to M4" at 12:44 (gate report C-2) |
| D4 | Vendor lead turns code-gated off; r4b ruling: vendor rule first, regardless of evidence. | 12:44; log 20:58 |
| D5 | Runtime evidence read from the checkout's `tests/` tree. | 12:44 |
| D6 | Class `reasoning_effort` demoted `smoke` → `help`. | 12:44 |
| D7 | vibe only after C4 — superseded in practice by offline parse runs (m3-vibeparse) without a vibe real run. | 12:44; 2026-11-10 12:34 |
| D8 | Reject → rework gated by evidence at `task reject` (scheduler T13 not touched); gate at T13/T5 is M4. | log 2026-11-09 19:10 |
| D9 | No single-case C2 retries; a re-capture is a full new C2. | log 2026-11-09 16:45 |
| D10 | Conservative unusable-seen rule; persisted hold, never released on timeout, never kill by pgid alone. | log 21:42, 21:50; r4e notes |
| D11 | Droid paused for coding (08:26); real runs move to kiro (09:12); Sol work to the Quest coder (09:16). | James 2026-11-10 |
| D12 | Review routing v2: Linear free chat is review of record per PR; merge.sh refuses without `VERDICT: ACCEPT` on record. | 2026-11-09 21:40, 21:51 |
| D13 | Worker 2 as parallel coder for independent M3 tasks. | 2026-11-10 12:34 |
| D14 | M3 gate = free Linear chat + Kiro exec checks + ClickUp Opus 5.5; no Linear Loop. | 2026-11-10 13:36, 13:45 |
| D15 | M3 gate changed: Kiro dropped (no Kiro exec checks; kiro N13 deferred to M4); gate = Linear free chat + ClickUp Opus 5.5 + droid/Carbon (reasoning high) final review on a separate review-only host (droid 1.7.1; base stays 1.6.0). A1/A3 = coordinator suite runs on base. vibe `unverified`, N11 partial, L2, L8 accepted. | James 2026-11-11 08:11, 08:38 |
| D16 | M3 gate order: Linear free chat → droid + Carbon (`gemini-4-carbon`, high) on box2, new Factory account (review-only; base droid 1.6.0 stays, paused) → ClickUp Brain Opus 5.5 last. | James 2026-11-11 10:07 |

## 3. Known limitations

1. `run_targets` setsid race fixed by #20 (two-scan rule). Residual: pids vanishing between listdir/stat; `env -i` escapees outside the group.
2. Pending `run.seen_unusable` events are memory-only; a crash before the flush can lose the event (hold is persisted).
3. Evidence not re-checked at T5.
4. Adopted lead turns have no seen tracking.
5. agy/vibe parse rest on limited real output (agy `final_text` always `""` in captures; one vibe export, `terminated`).
6. The N1 integration test's stubs report droid 1.3.0, so droid shows `evidence_stale` there.
7. `lead_launch_check` does not call `effective_launch` yet (unreachable for vendors in M3-lite).
8. Commit author metadata does not reliably name the coder.

## 4. Open follow-ups to M4

- F1: residual run_targets limits (vanished pids, `env -i` escapees).
- F2: persist pending `run.seen_unusable` events (limitation 2).
- F3: evidence gate at T5 / scheduler T13 instead of `task reject` (D8).
- F4: seen tracking for adopted lead turns.
- F5: wire `effective_launch` into `lead_launch_check` before vendor lead turns open; lift D4 only by decision.
- F6: kiro/agy `dev` profile verification; idle timeout; resume use only where S-X-3 passed.
- F7: `tests/stubs.py` droid stub at the evidenced version so N1 can show `true` (named supersession).
- F8: real agy/vibe captures (completed, turn limit, error) to close the parse evidence gaps; vibe `trust_workdir` only from S-VIBE-9.
- F9: N14 hermes/openclaw if installed.
- F10: repo-level git identity per coder so authors are accurate.
