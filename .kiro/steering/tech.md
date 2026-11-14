---
inclusion: always
---

# Tech stack and commands

Consolidated on 2026-11-09 from docs/plan.md (§2, §4, §5, §13–§15, §17), scripts/test.sh, tests/run_tier.py, docs/HANDOFF.md §10.1 and the M2/M3 run plans under docs/coordination/plans/.

## Runtime
- **Python ≥ 3.11, standard library only**, at runtime and in tests (plan §2, §0 rule 2). No `pip install`, no third-party imports (`test_stdlib_only` in tests/unit/test_skeleton.py, checklist A2). The plan was written on Python 3.13.5; the current machine runs Python 3.11.2 (HANDOFF §10.1).
- **SQLite** through `sqlite3`: WAL, `synchronous=FULL`, `STRICT` tables, explicit `BEGIN IMMEDIATE`, migrations in `mesh/core/schema.py` (plan §5.2–§5.5).
- **asyncio** daemon `meshd` on a Unix socket `$MESH_HOME/run/mesh.sock` (0600, `SO_PEERCRED` check), NDJSON request/response; no `AF_INET`, no `http.server` (plan §4, §15.3; `test_no_network_code`, checklist C1–C2).
- Mesh's own processes run as `python3 -P -s -m …` with `PYTHONPATH` set to the install root (plan §7.6 mitigation 8, checklist C15).
- Git is called only through `mesh/core/gitops.py`, which allow-lists subcommands; no push, rebase, reset, checkout or force (plan §10.5, checklist A6; `test_gitops_allowlist`, `test_git_never_spawned_outside_gitops`).

## CLI conventions (plan §13)
`mesh <resource> <verb>`, JSON envelope on stdout by default, exit codes 0–14 (plan §13.2), `--input -` JSON on stdin, `--idempotency-key`, `--dry-run`, layered `--help` with `Examples:`. Secrets never go in argv; tokens come from `MESH_TOKEN` or a 0600 `--token-file`, the passphrase from `/dev/tty` or `--passphrase-fd` (plan §7.1, §15.1).

## Tests
| Tier | Command | Notes |
|---|---|---|
| full default suite | `bash scripts/test.sh` | unit + integration + recovery, tripwire shims on PATH, must be rc 0 with both tripwire files empty and under 600 s (checklist A1, A3) |
| unit only | `python3 -m unittest discover -s tests/unit -t .` | pure, fast |
| one module | `python3 -m unittest tests.unit.test_redact` | what `.kiro/hooks/scripts/affected-tests.sh` runs |
| integration / recovery | `python3 tests/run_tier.py tests/integration` | parallel runner; `MESH_TEST_JOBS=N`, `@serial` marker in tests/helpers.py (m1c-plan run 1) |
| probe (opt-in) | `MESH_PROBE=1 python3 -m unittest discover -s tests/probe -t .` | read-only real `--help`/`--version`; coordinator only (m3-plan C1) |
| smoke (opt-in, quota) | `MESH_SMOKE=1 … python3 -m unittest discover -s tests/smoke -t .` | real model sessions; only with James's consent, coordinator only (m3-plan C2, C4) |

- `tests/helpers.py` `MeshHarness` makes short homes `/tmp/mh-<rand>` and drives the real `bin/mesh` against a testing-mode daemon (`MESH_TESTING=1`); `tests/gitfixture.py` makes hermetic git repos under `/tmp/mh-*` (m2-plan r1).
- Every test that covers a checklist item has `ACC: <ID>` in its docstring (acceptance-checklist §0).
- Real CLI fixtures live in `tests/fixtures/real/` and are pinned by sha256 (`test_fixture_hashes`); synthetic fixtures are named `synthetic_*` and never count as evidence (plan §14.10–§14.11).

## Kiro tooling in this repo
- Hooks: `.kiro/hooks/*.kiro.hook`, backed by `.kiro/hooks/scripts/{affected-tests.sh,secret-scan.sh,acceptance-check.py}`, which also run from the shell.
- Agent profiles: `.kiro/agents/mesh-reviewer.json` (reviews/plans), `.kiro/agents/mesh-specwriter.json` (this scaffold), `.kiro/agents/mesh-coordinator.json` (retired runner, HANDOFF §11.1).
