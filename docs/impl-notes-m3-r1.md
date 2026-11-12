# Implementation notes: m3-r1 (m3-probe): probe tier, help diff, capture writer

Coder: Quest. Branch `wt/m3-r1`, from main 6f8e9b8 (the worktree was created at 81033c5
and fast-forwarded to main before any change). Items: N10 (build; the pass is C1), N21
(writer, promote, committed-tree check), N22 (diff).

## What was built

### `mesh/adapters/helpdiff.py` (new, pure)
- `flag_tokens(text)`: every `-x` / `--long` token. It uses the probe's boundary rule
  (`base.is_flag_present`), and every returned token satisfies `is_flag_present`.
- `diff(old, new)`: `{"added": [...], "removed": [...]}`, both sorted.
- `emitted_flags(adapter)`: the flags `build()` emits over profile (5) × resume (2) ×
  effort (`high`, none). It skips `ProfileUnsupported`, `EffortUnsupported`,
  `EffortNotAllowed` and `PromptTooLong`.
  - `EffortNotAllowed` was added to the plan's list: agy raises it for effort-suffixed
    models.
  - The prompt is a short file in a `mh-helpdiff-*` TemporaryDirectory, because kiro and
    agy stat the prompt.
- `newest_fixture_dir(adapter_id, root=None)`: compares versions numerically (1.10.0 >
  1.9.2) and ignores non-version names and plain files.
- `fixture_root()`: `<repo>/tests/fixtures/real`.

Not imported by meshd (base.py: runtime never reads tests/fixtures). Only the probe tier
and tests use it.

### `tests/tierlib.py` (new)
- `write_capture(staging, rel_path, data, command)`:
  - `staging` must be absolute.
  - `rel_path` is `<adapter>/<version>/<file>` or `results/<file>`.
  - The data file and `<file>.meta.json` are written `O_EXCL|O_NOFOLLOW`. The sidecar
    holds exactly `{command, captured_at_utc, sha256}`.
  - Refused: `synthetic_*` in any component, `..`, absolute paths, sidecar names, other
    layouts, an existing file or sidecar.
  - The sidecar `command` is the binary *name* plus args (Q16), never its path.
- `read_sidecar(path)`: exact keys, a non-empty string-list command, a
  `YYYY-MM-DDTHH:MM:SSZ` timestamp, and the sha256 recomputed from the file.
- `plan_promotion` / `promote(staging, repo_root)`:
  - Everything is validated before anything is written: every data file has a valid,
    matching sidecar; no orphan sidecars; no symlinks; no synthetic names; layout only;
    no destination (file or sidecar) already exists; staging is outside the repo;
    `repo_root` holds `tests/fixtures/real`.
  - Copies go to `tests/fixtures/real/<a>/<v>/<file>` or `tests/smoke/results/<file>`
    only, `O_EXCL`, and each sha256 is re-verified at the destination.
- CLI: `python3 -m tests.tierlib promote --from S --to R` prints JSON. rc 0 on success,
  rc 1 on refusal.

### `tests/helpers.py` (additive)
- `MeshHarness(..., use_shims=True)` is the default, and the default env is unchanged
  (the test asserts that shims are first on PATH and `MESH_TESTING=1`).
- With `use_shims=False`, `tests/shims` is removed from PATH (by realpath) and
  `MESH_TESTING` is not set.

### `tests/probe/__init__.py`, `tests/probe/test_probe.py` (new, opt-in tier)
- `ProbeTier.test_probe_adapters` is skipped unless `MESH_PROBE=1`. Adapters come from
  `MESH_PROBE_ADAPTERS` (default `droid,agy,kiro,vibe`). `MESH_PROBE_CAPTURE` must start
  with `/tmp/mh-`.
- Per adapter:
  - `shutil.which` on PATH with `tests/shims` removed; a missing binary is printed as
    `not_installed`.
  - `m adapter probe <id> --binary <abs>` on a `use_shims=False` harness daemon.
  - It asserts a version, and that the latest `adapter_probes` row has (version,
    binary).
  - Then `probe_report`, printed as JSON, and `check_report`.
  - `list_models()` must be non-empty for droid, agy and kiro, and `None` for vibe.
- `probe_report(adapter_id, binary, version, capture_dir=None, fixture_root=None)`:
  - The binary must be absolute.
  - Each command in `HELP_COMMANDS` is run through `run_readonly_cmd` (the probe's
    read-only runner: minimal env, stdin devnull, timeout).
  - Commands per adapter:
    - droid: `--help`, `exec --help`
    - kiro: `--help`, `chat --help`, `chat --list-models -f json`
    - agy: `--help`, `models`
    - vibe: `--help`
  - It compares the sha256 of the normalized text with the fixture at
    `planned_version` and at the probed version.
  - It records the flag diff versus `planned_version` for help files, and
    `missing_emitted_flags = emitted_flags - flag_tokens(live help)`.
  - It captures (all successful outputs, same file names) when `capture_dir` is set and
    the version dir is missing or any sha differs.
- `check_report`: fails if a help command failed or an emitted flag is missing. Added
  flags are only reported.

## Tests (all offline, `/tmp/mh-*` only, no MESH_PROBE*/MESH_SMOKE* written to os.environ)

- **tests/unit/test_helpdiff.py** (6 tests):
  - the token rule;
  - sorted added/removed;
  - each fixture against itself gives an empty diff;
  - emitted flags are all in the planned fixtures: for all four vendors the result is
    empty;
  - a fixture dir newer than `planned_version` gets its diff printed, not asserted. None
    exists yet, so `{}` is printed; after C1 it prints droid 1.3.0→1.6.0 and agy
    1.5.2→1.5.3;
  - numeric ordering of `newest_fixture_dir`.
- **tests/unit/test_tierlib.py** (8 tests):
  - sidecar keys and hash;
  - no overwrite, including a stray sidecar;
  - 14 refused paths;
  - promote copies and re-verifies, and a second promote is refused;
  - one existing destination refuses everything, with nothing written;
  - 8 bad staging trees, plus a non-checkout target and staging inside the repo;
  - CLI rc 0 and rc 1;
  - **committed trees**: every file under `tests/fixtures/real` and
    `tests/smoke/results`, apart from the 13 pre-M3 files (12 hashed fixtures + README),
    must have a matching exact-key sidecar, with no `synthetic_*`. This is the in-suite
    half of N21's check (today: 0 such files). The `git diff --name-status` half is a
    coordinator check, because tests never point git at the checkout.
- **tests/unit/test_probe_offline.py** (6 tests), stubs under `/tmp/mh-*`:
  - a `create_test_stubs` droid stub (1.3.0): shas match, empty diff, no missing flag,
    nothing captured, tripwire empty;
  - a 1.6.0 stub with an added flag: reported, passes, captured with sidecars, and
    promotable into a temp repo;
  - a 1.6.0 stub without `--log-group-id`: `missing_emitted_flags == ["--log-group-id"]`,
    `check_report` fails, the probe's own `flag_diff` agrees, nothing captured;
  - the shim-free daemon path through the real CLI (`adapter probe droid --binary
    <stub>`): row (1.6.0, stub), no `fake` adapter, default harness env unchanged;
  - a relative binary is refused;
  - the tier is skipped without opt-in.

## Assertions changed
None. No existing test or assertion touched; `test_fixture_hashes` untouched.

## Deviations / readings
- **Plan test (a) dropped** (it repeats N15) and **(d) dropped** (it depends on r5), as
  the plan says. The planned-fixture emitted-flag test is kept: it is cheap, and it is
  the baseline that the tier's live check builds on.
- **Full suite:** `scripts/test.sh` idle (no other suite at start), wall 8m32s (512 s), rc 0.
- **Commit split:** sub-steps 1–3 were written, then the full suite was run once on the
  combined tree, then committed as three commits plus notes. The intermediate commits
  were not suite-run separately; each new test module only imports the earlier ones.
- **Shell:** I am not bound by the coder allow-list (Quest coder). I ran no vendor
  binary and never set MESH_PROBE/MESH_SMOKE; the probe tier was only run without
  opt-in (skipped).

## For C1 (coordinator)
`MESH_PROBE=1 MESH_PROBE_CAPTURE=/tmp/mh-probe-<ts> python3 -m unittest discover -s tests/probe -t .`
then `python3 -m tests.tierlib promote --from /tmp/mh-probe-<ts> --to <checkout>`.
- The JSON report per adapter shows the diff, the shas and `missing_emitted_flags`.
- Captures land only for a new version or a changed sha. kiro 3.1.0 and vibe 3.1.0 with
  unchanged help capture nothing; a changed sha at the same version would be refused at
  promote.

## r1b: fixes for Kiro's REVISE (review-m3-r1.md)

Suite wall time: before r1b 8m32s (512 s, the r1 run); after r1b 7m33s (453 s). Both runs idle, rc 0, tripwires empty.

1. **MAJOR 1, `helpdiff.emitted_flags`:** the matrix now also covers:
   - model ∈ {None, `mesh-helpdiff-model`};
   - options ∈ {base, `trust_workdir`, `sandbox`, `agent`, `max_turns`+`max_price_usd`+`max_tokens`}, each merged with `run_id`;
   - prompt ∈ {short, `max_argv_prompt_bytes+1` bytes}.

   The flags it now finds:
   - vibe: `--model --trust --max-turns --max-price --max-tokens`;
   - agy: `--add-dir --sandbox --agent`;
   - kiro: `--agent`.

   All of them are in the planned fixtures, and `test_emitted_flags_in_planned_fixtures` is unchanged and green.
2. **test_helpdiff:** new `EmittedMatrixTest`. It checks the three superset assertions, and that the flags from rebuilt golden-shape argvs are a subset of `emitted_flags`. The argvs are vibe with a model and all options, and agy with a model, effort, sandbox, agent and a long prompt.
3. **test_probe_offline, shim-free daemon test:** `extra_path` now points at a `create_test_stubs` guard dir under `/tmp/mh-*` with its own tripwire file. The test asserts the guard dir is first on PATH and the tripwire file is absent at the end. All existing assertions are kept.
4. **tierlib:**
   - The staging-vs-repo check uses `realpath`, and a symlinked staging dir is refused.
   - `_check_dest_parent` requires each destination's real parent to lie under the real `tests/fixtures/real` or `tests/smoke/results`. It runs for every pair at plan time, again before `makedirs` and again after it, so no directory is created outside those trees.
   - New tests: a directory symlink inside staging; staging inside the repo reached through a symlinked alias; a symlinked staging dir; a symlinked `fixtures/real/<adapter>` destination parent. Nothing is written in any of these cases.
5. **Minor 5, coordinator decision:** `probe_report` captures only files whose `<version>/<file>` doesn't exist yet. A changed sha at an existing version is listed in `same_version_drift` and not captured. New offline test: a 1.3.0 stub with drifted exec help gives `same_version_drift == ["exec-help.txt"]`, nothing captured, and the check passes.
6. **Commit subjects:** they now use the `<type>(m3): … (ACC: …)` form, and the notes record wall time before and after.

Assertions changed: none.
