# M4 report (polish only)

Scope (James 2026-11-12 21:04): M4 is polish only, with no new features. The plan §16 M4 feature items (Hermes/OpenClaw adapters, `custom` adapter, `notify.command`, `mesh user passwd`, per-project run budgets) are deferred as future work. License: MIT (James 2026-11-12 21:05).

## m4-kiro-n13: kiro launch hold lifted, kiro N13 passed

- **Hold lifted.** `evidence.LAUNCH_HOLDS` is empty. The hold tool stays (`apply_hold`, the T6/T5 re-check, reason `launch_held`). The new testing-only `MESH_TEST_LAUNCH_HOLD` (`"kiro:M4,agy"`, default milestone M4) adds holds in testing mode so the hold tests still exercise it. It is ignored outside testing mode, the same as `MESH_TEST_LAUNCH_UNHOLD`.
- **N13 kiro (real e2e).** One run on base with James's consent (2026-11-12 21:04). Command: `MESH_SMOKE=1 MESH_SMOKE_ADAPTERS=kiro MESH_SMOKE_CONSENT=kiro MESH_SMOKE_E2E=kiro MESH_SMOKE_MAX_RUNS=1 MESH_SMOKE_MODEL_KIRO=claude-haiku-5.5 MESH_SMOKE_OUT=/tmp/mh-kn13 python3 -m unittest tests.smoke.test_e2e.KiroE2E`. kiro-cli 3.1.0.
  - Result: **pass**. Task `submitted`, deliverable summary "Replied with MESH_SMOKE_OK as instructed…", argv has `--model claude-haiku-5.5` and `--trust-tools=` (readonly), exit 0, end_reason `exited`. It ran in about 5 s.
  - Record: `tests/smoke/results/e2e-N13-kiro-3.1.0-run_a1cfe019a5dgqkf4tu7kwjs7sf.json` plus fixtures under `tests/fixtures/real/kiro/3.1.0/` (promoted with `python3 -m tests.tierlib promote`).
  - Kiro usage (tokscale): claude-haiku-5.5 went from 12,896 to 26,576 tokens (+13,680), cost under $0.01. A first attempt with `MESH_SMOKE_OUT=/tmp/kn13` was refused by `load_config` before any run (it has to be under `/tmp/mh-*`), so no model run was spent there.
  - Note: the prompt is passed without `--` (known P3 from M3, being fixed in a helper agent's m4-p3-cleanup).
