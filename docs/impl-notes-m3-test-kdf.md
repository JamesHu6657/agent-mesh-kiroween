# m3-test-kdf: cheap scrypt for testing homes, strictly gated

Branch `grok/m3-test-kdf` from main be54a3c. Product change, small. Worker 2.

## Why

Quest measured scrypt at the production cost (n=2^15, r=8, p=1) at about 66 ms per call
against about 1.7 ms at n=2^10 (re-measured here: 66.6 ms vs 1.75 ms). The suite does
about 3,400 user verifications per tier, roughly 225 s of CPU and 30-40 s of wall time.
Quest and Linear both said "unsafe as written" because `kdf_params` are stored at init and
reused for the life of the home, so cheap params must never be able to reach a home that
is later used for real work. The change below adds the cheap path only behind guards a)-d).

## What changed

- `mesh/core/auth.py`
  - `SCRYPT_N_TESTING = 2**10`; `SCRYPT_N_MIN = SCRYPT_N` (2^15) and `SCRYPT_R_MIN = 8`.
  - `derive_user_keys(passphrase, salt=None, testing_kdf=False)`: production cost by default;
    `testing_kdf=True` uses n=2^10 and adds `"testing": true` to `kdf_params` (the
    testing marker, stored in the home's `credentials.kdf_params` with the params).
  - `kdf_params_weak(params)`: true when n < 2^15, r < 8, the testing marker is set, or the
    params are unreadable (not a dict, n/r not ints). Params without `n` (none exist today)
    keep the old meaning (production default).
  - `check_kdf_params_unlockable(params, testing_mode=None)` (first version; replaced by `parse_kdf_params` in the fix round): outside testing mode a weak
    home raises `KDF_TOO_WEAK` (rc 3) with a clear message (n, the floor, "created in
    testing mode", how to fix). `testing_mode=None` reads `MESH_TESTING == "1"` only; no
    other variable relaxes it.
  - `verify_user_passphrase(..., testing_mode=None)` calls the check first, before any
    scrypt work, so a refused unlock costs nothing and is not an `auth_failures` row
    (it cannot feed the lockout).
- `mesh/core/init.py` (guard a): `MESH_TESTING=1` on the default home (`realpath` equal to
  `~/.agent-mesh`, so a symlink alias is caught too) is refused with `INVALID_ARGUMENT`
  before anything is created, matching meshd's existing refusal. Testing mode on any
  other home uses `testing_kdf=True`; otherwise production cost.
- `mesh/core/paths.py`: `default_home_path()` (same definition as the one in server.py,
  which is left alone).
- `mesh/cli/commands/doctor.py`: outside testing mode `mesh doctor` refuses a weak home with
  the same `KDF_TOO_WEAK` instead of reporting it as healthy. Doctor never unlocks; this
  only stops it from blessing such a home.
- `mesh/constants.py`: new code `KDF_TOO_WEAK` (exit 3 group). `docs/usage.md` §2.1: one
  paragraph.

### server.py (fix round)

The first version left `mesh/daemon/server.py` alone; the fix round changes two places
(review MAJOR 3 and the MINOR on order), still no #20 files (merge, recovery,
supervisor, terminate):

- `DaemonServer.__init__(home, testing_mode=None)`: the mode is decided once here. An
  explicit `True`/`False` wins over the environment; only `None` (what `mesh daemon run`
  passes) reads `MESH_TESTING == "1"`. Before the fix an explicit `False` was overridden
  by `MESH_TESTING=1`, and the unlock re-read the env through auth.py.
- `authenticate_and_authorize` (user auth): the credential row is read and its KDF params
  go through `parse_kdf_params(raw, testing_mode=self.testing_mode)` BEFORE the lockout
  count, so a weak or invalid home answers `KDF_TOO_WEAK` / `KDF_PARAMS_INVALID` even
  with 5 recent failures. Garbled JSON no longer raises a raw `json.loads` error at the
  old line 822; it is the same `KDF_PARAMS_INVALID` as doctor. `verify_user_passphrase`
  gets the daemon's mode explicitly.

Known limit, not changed here: other testing-mode switches (the fake adapter in
`supervisor.py`/`registry.py`, settings) still read `MESH_TESTING` themselves; only the
KDF decision follows the daemon's stored mode. In production both come from the same env
(`mesh daemon run` passes no argument), so they agree. The daemon's own default-home
refusal for testing mode (`setup_storage`) still compares realpath only; init now does
the identity compare.

## Fix round (Quest ACCEPT, Linear REVISE, PR #22)

Merged main 716e95b first. All three MAJORs and all MINORs:

1. **Bind-mount alias (init guard a).** New `mesh.core.paths.is_default_home(home)`:
   realpath string compare as before, plus filesystem identity. If the home exists,
   `(st_dev, st_ino)` of `os.stat(home)` vs the default home; if it does not exist yet,
   the parent's `(st_dev, st_ino)` plus the basename (so an alias of `~` plus
   `.agent-mesh` is caught). init refuses testing mode when it matches. Tests: a second
   path to the default home and an alias of its parent, simulated by patching `os.stat`
   identity (the box runs the suite as uid 1000, no bind mounts); a root-only test does a
   real `mount --bind` and is skipped elsewhere. Out of scope: anyone with mount rights
   who mounts over the default home after the check, or swaps directories between check
   and init.
2. **Doctor fail-open.** One helper, `mesh.core.auth.parse_kdf_params(raw, testing_mode)`,
   used by doctor and the daemon. Doctor reads the raw stored value first (before the
   settings/repo reads that may fail) and parses it outside the old `except: pass`, so
   garbled JSON, SQL NULL, empty, JSON null all FAIL. Codes, picked and documented:
   `KDF_PARAMS_INVALID` (rc 3) for anything that is not exactly what init writes, in every
   mode; `KDF_TOO_WEAK` (rc 3) for valid-but-weak params outside testing mode.
3. **Mode authority.** Daemon decides once (above); doctor decides once at its start by the
   same rule (`MESH_TESTING == "1"`, doctor has no explicit flag). `parse_kdf_params` and
   `verify_user_passphrase` take `testing_mode` as a required keyword (a non-bool raises
   `TypeError`); `_testing_env()` is gone, auth.py reads no env for this.

MINORs:

- Full validation before scrypt: all five keys present and no unknown key (only the
  optional marker, which must be `true`); every value a real int (no bool/float/string);
  `n` a power of two in [2^10, 2^20] and >= 2^15 outside testing mode; `r` in [8, 32];
  `p` in [1, 16]; `dklen` exactly 64 (pinned: init writes 64 and the code splits it into
  a 32-byte verifier and a 32-byte K_user); `maxmem` between what scrypt needs
  (128*r*(n+p+2)) and 256 MiB (init writes 64 MiB). JSON `NaN`/`Infinity` rejected. The
  old "params without n are an old home" leniency is removed: every home ever created
  stores all five keys.
- KDF check before lockout, tested at daemon level.
- Threat model split by attacker (below).

Quest P3s: doctor swallowing garbled JSON is MAJOR 2. Guard a trusts `$HOME`
(`~/.agent-mesh` is resolved from `HOME`); a caller who changes `HOME` moves what
"default home" means. That is a limit of guard a only; guard b still refuses the
resulting weak home in any non-testing daemon or doctor, wherever it is.

## Fix round 2 (Quest ACCEPT, Linear REVISE on bcafd66)

- **Doctor fails closed** (`mesh/cli/commands/doctor.py`, MAJOR). The DB read no longer
  swallows exceptions. `_read_home_state` opens mesh.db read-only, runs
  `PRAGMA quick_check`, checks the schema version (SCHEMA_TOO_NEW first), and reads the
  user passphrase credential, `workspaces.root` and the project repo paths with its own
  queries (no `project_repo_paths`, which returns [] on error). Any open / query /
  decode error, a quick_check result other than `ok`, a missing table or column, or a
  missing user passphrase credential row gives `DB_INTEGRITY_FAILED` (rc 1) in every
  mode and for both `mesh doctor` and `mesh doctor --check-invariants`; it is never
  reported healthy. KDF params are then checked by `parse_kdf_params` as before
  (rc 3). A home with no mesh.db at all is still "not initialised yet" and unchanged.
- **SQLite URI escaping** (`mesh/core/db.py`, real product bug). The read-only URI was
  `f"file:{path}?mode=ro"`, so a home containing `?` or `#` was cut there (doctor
  opened a different file, or none, and the old except swallowed it) and `%` was
  decoded. It is now `sqlite_ro_uri(path)`: `pathlib.Path(abspath).as_uri() + "?mode=ro"`,
  which percent-encodes `?`, `#`, `%` and spaces. Only the read-only path used a URI;
  read-write connects take the plain path.
- **One default-home guard** (P3). `mesh.core.paths.refuse_testing_on_default_home(home,
  testing_mode)` (realpath plus filesystem identity via `is_default_home`) is now called
  by `mesh init`, meshd's `setup_storage` (was a realpath-only compare at server.py:520;
  that local `default_home_path` is removed) and `mesh doctor`. Doctor with
  `MESH_TESTING=1` on the default home or an alias of it is refused with
  `INVALID_ARGUMENT` (rc 2) before it reads anything, so it can never accept weak params
  there.
- **Threat model** corrected: deleting the marker does not make the verifier mismatch;
  the refusal comes from the production n floor (see above).

Tests: `tests/unit/test_doctor_fail_closed.py` (21 tests), real CLI (`bin/mesh` in a
subprocess) for doctor: homes named `h?mode=rw`, `h#frag`, `h%41b`, `h with space`,
`h ?#%3F mixed` go through init, meshd start + user `whoami` (on two of them), testing
doctor healthy, production doctor and `--check-invariants` rc 3 `KDF_TOO_WEAK` (the
Linear repro); an in-process production meshd refuses the same home with `KDF_TOO_WEAK`;
a production init on a hostile name is healthy. Fail-closed CLI cases in both modes and
both doctor forms: missing credential row, renamed `kdf_params` column, dropped column,
dropped `settings` / `projects` table, garbage file, corrupted pages, mesh.db being a
directory (open error): all rc 1 `DB_INTEGRITY_FAILED`; NULL params still rc 3
`KDF_PARAMS_INVALID`. Read-only connect opens the exact file next to a decoy at the
truncated path. Default-home guard: doctor testing mode on the default home and a
symlink to it (CLI) and on an identity alias (helper) refused; meshd `setup_storage` on
an identity alias refused; all three entry points call the shared helper.
Independent KDF cost oracle: frozen known-answer vectors (passphrase
`known-vector-passphrase-m3`, salt bytes 0..15, n=2^15 and n=2^10, r=8, p=1, dklen=64)
are checked against `hashlib.scrypt` and against `derive_user_keys`, and the verifier
stored by a real production and testing `init_mesh_home` must equal `hashlib.scrypt`
with literal production / testing params (and differ from the other cost). Mutation
check: deriving with n=2^10 while writing n=2^15 metadata fails 2 oracle tests; with the
round-2 source reverted (only the URI helper kept, with the old unescaped string) 37
failures and 2 errors in the new file. `tests/unit/test_doctor.py` now builds its clean
and I5-violation DBs with a real `mesh init` (an `init_db`-only DB has no credential and
is correctly not healthy any more).

## Fix round 3 (Quest/Linear REVISE on 179c131)

- **Root fix: one home resolution, one way to open SQLite.** `mesh.core.paths.resolve_home`
  is called once at every entry point: the CLI (`--home` / `MESH_HOME`, `mesh/cli/main.py`),
  `init_mesh_home`, `DaemonServer.__init__` and `mesh doctor`. It is
  `realpath(join(getcwd(), home))`. Quest asked for `realpath(abspath(home))`, but
  `abspath` folds `a/link/..` to `a` lexically *before* realpath sees the symlink (that
  is item 2's bug), so the cwd is joined without normalising and realpath does the
  rest (same result as `realpath(abspath())` for every path without `..` after a link).
  Every SQLite connection, read and write, goes through `mesh.core.db.sqlite_uri(path,
  mode)`: `Path(resolve_home(path)).as_uri() + "?mode=ro|rwc"` with `uri=True`;
  `init_db` resolves its path the same way. No raw `sqlite3.connect(path)` is left in
  `mesh/` (grep in the PR comment).
- **Item 1 (P2) relative homes.** `MESH_TESTING=1 mesh --home file:rel init` now creates
  `./file:rel/mesh.db` (nothing outside the home), `--home 'file:q?mode=memory'` works
  (was INTERNAL_ERROR: the system SQLite is built with URI filenames on, so a raw
  relative `file:...` name was parsed as a URI). Testing doctor reports the home
  healthy, production doctor and `--check-invariants` give `KDF_TOO_WEAK` (rc 3) on that
  same DB, and a meshd started with the relative home unlocks the user init wrote there.
- **Item 2 (MAJOR) symlink + `..`.** With `a/link -> ../b/child`, `--home a/link/..`
  (relative and absolute) is `b`: doctor and `--check-invariants` read b's weak testing
  DB and fail `KDF_TOO_WEAK` in production mode instead of reading strong `a`.
- **Item 3 (MAJOR) access errors.** Doctor's presence checks use `os.lstat`: only
  `FileNotFoundError` means absent; `PermissionError` / any other `OSError` is
  `DB_INTEGRITY_FAILED` (rc 1). A chmod 000 home is no longer "uninitialised, healthy".
  A really absent home is still healthy / 0 violations.
- **Item 4 strict doctor.** No mesh.db in a home that has any of `run`, `runs`,
  `workspaces`, `logs`, `merges`, `mesh.db-wal`, `mesh.db-shm` fails (rc 1). The schema
  version is read by `_strict_schema_version` (not the lenient migration reader):
  missing meta table, missing `schema_version` row, or a value that is not a plain
  integer string fails rc 1 (meshd also refuses these at startup, server.py:559-569);
  newer than supported is still SCHEMA_TOO_NEW. Used by both doctor forms.

Tests: `tests/unit/test_home_resolution_r3.py` (13 tests, real CLI with explicit cwd).
The quick_check case writes a wrong freelist page count into the file header: the DB
stays fully readable, only `PRAGMA quick_check` reports it (a CHECK-constraint
violation is not used because quick_check on a `mode=ro` connection does not check
CHECK constraints). Mutation check (each alone, file run, reverted): skip quick_check
(1 failure), resolve with `realpath(abspath())` (3) or `abspath()` (3), lexists-style
"any OSError = absent" (4), lenient missing meta (4) / row (4) / non-integer (16),
ignore init markers (4), raw `sqlite3.connect(path)` write path (2 errors): all killed.
Removing only the CLI-level resolve is not killed, since init, meshd and doctor resolve
again themselves (intended redundancy).

(Round 3 caveat, fixed in round 4: `mesh doctor --fix-perms` on a chmod 000 home
failed with rc 1 before repairing modes.)

### Fix round 3 suite run

Full `scripts/test.sh` on box2 (shared box, 1-minute load average about 3.5 at
the start): unit 564 OK (1 skipped: the root-only bind mount test) in 39.5 s,
integration 280 OK in 128.3 s, recovery 49 OK in 22.6 s; 893 run, 892 passed, 0 failed,
1 skipped; total wall 191 s. Tripwire clean.

## Threat model

What a testing home is: a home whose verifier can be brute-forced about 38x faster than
production. The risk is not the testing home itself (it holds fake test data) but a
testing home, or its params, ending up used for real work. By attacker:

- **Misuse / mistakes (the case these guards are for).** Production `mesh init` (no
  `MESH_TESTING=1`) can only write n=2^15. Cheap params need the explicit testing env at
  init time and are refused on the default home and its visible aliases (guard a). A
  testing home opened by a non-testing meshd or doctor is refused (`KDF_TOO_WEAK`), with
  no env switch or setting to override it outside testing mode; the fix is a new home.
  Raising `n` by hand without removing the marker is still refused (marker alone counts).
- **DB-write-only attacker** (can edit `mesh.db`, has not changed Mesh's code and has not
  captured the passphrase or K_user; usage §5 "What `mesh audit verify` detects"). Editing
  `kdf_params` gains nothing. Lowering `n` makes the stored verifier stop matching (it
  was derived with the old `n`). Deleting the marker from a testing home does NOT make
  the verifier mismatch (the marker is metadata, not a scrypt input); that home is still
  refused outside testing mode because of the production floor (n=2^10 < 2^15 gives
  `KDF_TOO_WEAK`), and raising `n` to pass the floor makes the verifier mismatch. Writing
  a new verifier for a passphrase of their own yields
  a different K_user, so forged direct approvals still fail `mesh audit verify`. What
  they can do is deny service (garbled or weak params now refuse unlock with a clear
  code instead of a crash, before any scrypt work and without touching the lockout), and
  copy the verifier for an offline guess, which these guards do not change. The guards do
  not weaken the DB-only tamper detection usage.md promises.
- **Same-UID code execution** (can run code as the user, change Mesh's code, read memory
  or the passphrase). Out of scope, as usage §5 "Same-uid limits" already says: such a
  process can start a testing-mode meshd, patch the check out, or read K_user. The guards
  do not claim anything against it.
- **Mount rights** (can bind-mount or mount over paths). Out of scope for guard a: such a
  user can mount over `~/.agent-mesh` after init's check or present an alias init cannot
  see. Guard b still applies to whatever home a non-testing daemon then opens.

Old homes: all existing homes were created with n=2^15, all five keys and no marker, so
they keep working.

## Tests (guard c)

- `tests/unit/test_test_kdf.py` (15 tests): default params are production and not weak;
  testing params are cheap, marked and verify in testing mode; weak params + no testing
  env refused (also for `MESH_TESTING` = "0", "", "true", "yes", "1 ", and for an explicit
  non-testing daemon even with `MESH_TESTING=1` in the env); refused before scrypt runs;
  hand-made weak params without marker refused (n=2^14, r=4, n as string, bool, list,
  None); marker alone refused; production params unlock outside testing; testing init on
  another home -> n=2^10 + marker; non-testing init (unset or "0") -> n=2^15; testing init
  on the default home (and through a symlink to it) refused with nothing created; doctor
  refuses the weak home outside testing mode and accepts it in testing mode.
- `tests/integration/test_test_kdf.py` (2 tests, real CLI + meshd): production path end to
  end, `mesh init` outside testing mode stores n=2^15 with no marker, a non-testing meshd
  unlocks it with `whoami --user` and rejects a wrong passphrase; and a non-testing meshd
  refuses a harness (testing) home with `KDF_TOO_WEAK`, records no `auth_failures`, and
  leaks no passphrase.
- `tests/unit/test_auth.py` still pins the production default (n=32768 etc.).
- Existing tests that start a non-testing meshd on a harness home
  (`test_adapters.py` N1/N3, `test_adapter_evidence.py` N19) now create that home with
  `mesh init` outside testing mode (`MESH_TESTING=0`, production KDF). Their checks make no
  user-authenticated call, but teardown's `daemon stop --user` does, and against a testing
  home it would be refused and fall back to SIGTERM after 2 s. These two are also further
  production-cost init + unlock coverage.
- Fix round, added to `tests/unit/test_test_kdf.py`: helpers never read the env (explicit
  False with `MESH_TESTING=1` refused, explicit True with no env accepted) and
  `testing_mode` is required; `TestKdfParamsValidation` (NULL, empty, whitespace, garbled,
  JSON null/list/string/number/bool, bytes not UTF-8, NaN/Infinity, each key missing,
  unknown key, the old no-`n` shape, n not a power of two / too small / too big / string /
  float / bool / null, r, p, dklen, maxmem out of range or wrong type, marker not `true`:
  all `KDF_PARAMS_INVALID` in both modes with scrypt never called; accepted shapes incl.
  bytes and stronger params); init alias by identity (existing home) and by parent
  identity + basename (new home), plus a root-only real bind mount (skipped on the box);
  `TestDoctorFailClosed` (garbled, NULL, empty, JSON null, list, partial dict fail in both
  modes); `TestDaemonModeAuthority`, through `DaemonServer.authenticate_and_authorize`:
  an explicit non-testing daemon with `MESH_TESTING=1` refuses the weak home; mode fixed
  at construction (env changes later change nothing either way); explicit True without
  env unlocks; with 5 recent failures a non-testing daemon gives `KDF_TOO_WEAK` (not
  `LOCKED_OUT`) while a testing daemon on the same home gives `LOCKED_OUT`; invalid params
  give the same `KDF_PARAMS_INVALID` / rc 3 from daemon and doctor, no `auth_failures`.

## Suite time

Measured on box2 (8 vCPU), full `scripts/test.sh`, run back to back right after
Quest's review jobs on this box had finished (no other test process running; 1-minute
load average 4-6 from the box's own background services at the start of each run):

| run | tree | unit | integration | recovery | wall |
|---|---|---|---|---|---|
| before | main be54a3c | 503 OK (29.1 s) | 278 OK (188.8 s) | 49 OK (23.8 s) | 242 s |
| after 1 | this branch | 518 OK (28.8 s) | 280 OK (141.3 s) | 49 OK (22.3 s) | 193 s |
| after 2 | this branch | 518 OK (29.2 s) | 280 OK (139.9 s) | 49 OK (22.4 s) | 192 s |

About 49 s (20%) saved here, almost all in the integration tier, in line with Quest's
estimate of 30-40 s wall from ~225 s of scrypt CPU. The counts grow by 15 unit and 2
integration tests (the new guard tests). This box is faster than the A1 target box, so
absolute numbers differ from Quest's A1 measurements (main 559 s there); the A1 figure
should be re-measured on base. Tripwire clean in all three runs.

### Fix round suite run

Full `scripts/test.sh` on box2 after merging main 716e95b and the fixes (no other
test process running, 1-minute load average about 2.8 at the start): unit 530 OK
(1 skipped: the root-only bind mount test) in 28.7 s, integration 280 OK in 127.9 s,
recovery 49 OK in 22.5 s, total wall 179 s. Tripwire clean.

Mutation check of the new tests (each mutation applied alone, `tests.unit.test_test_kdf`
run, then reverted; all six killed): daemon lets `MESH_TESTING=1` override an explicit
False (2 failures); identity compare removed from `is_default_home` (2); doctor swallows
`KDF_PARAMS_INVALID` (20); KDF check moved after the lockout check (1); dklen not
checked (6); power-of-two check removed (4).

### Fix round 2 suite run

Full `scripts/test.sh` on box2 (Python 3.13) with the round-2 changes: unit 551 OK
(1 skipped: the root-only bind mount test) in 34.7 s, integration 280 OK in 126.5 s,
recovery 49 OK in 22.3 s, total wall 184 s. Tripwire clean.

## Fix round 4 (Quest REVISE on 4524962)

1. SQLite wording (P2). The quick_check test asserted "Freelist"; SQLite 3.40.1 (base,
   US VPS, the A1 target) reports "Main freelist: size is 0 but should be 7", newer
   ones "Freelist: ...". The test now matches "freelist" case-insensitively. A grep of
   tests/ for other exact SQLite message asserts (freelist, locked, no such table,
   constraint failed, not a database, readonly, ...) found none; doctor itself only
   compares quick_check to [("ok",)].
2. `doctor --fix-perms` regression. The repair now runs FIRST, then the fail-closed
   checks, so a chmod 000 home or chmod 000 mesh.db is repaired with rc 0 again (plan
   §5.1, impl-notes-m1a). The walk is owner-only and lstat-based: the home itself, the
   files init creates (mesh.db, -wal, -shm, -journal) and, recursively, the directories
   init creates (run, runs, workspaces, logs, merges); symlinks are never followed,
   sockets/special files and entries not owned by the current uid are skipped, other
   top-level entries are left alone. Directories get 0700 before they are listed (so a
   000 directory is walked), regular files 0600. Without --fix-perms an unreadable
   home still fails (rc 1). If the checks still fail after the repair (e.g. a weak KDF
   in production mode), the rc is that failure's.
3. Empty / root home (P3). `mesh.core.paths.resolve_home` (the single entry) now
   refuses an empty or whitespace-only home and "/" or anything resolving to "/"
   (e.g. "//", "/tmp/..", "../../.."), INVALID_ARGUMENT rc 2 with details.param
   "home". The CLI reports it as a normal error envelope; doctor no longer turns an
   empty --home into MESH_HOME / the default. SQLite file paths use the new
   `resolve_path` (same resolution, no home rules).

Tests: `tests/unit/test_doctor_fix_perms_r4.py` (8 tests, real CLI; chmod cases skip
as root): 000 home, 000 mesh.db (and the production doctor still says KDF_TOO_WEAK after
the repair), 000 known subdir with nested 0755/0644 entries, symlinks to outside
files/dirs (also a known dir replaced by a symlink) and an unknown file left untouched,
absent home no-op, resolve_home refusals, CLI init/doctor/doctor --fix-perms refusals
creating nothing in the cwd. Mutation check: repair moved back after the checks (2
failures), empty/"/" checks removed (34 failures, 1 error); both killed.

### Fix round 4 suite runs

Full `scripts/test.sh` on box2 (Python 3.13, 1-minute load average about 3.5 to
5.4, shared box), run twice back to back:

| SQLite | unit | integration | recovery | wall |
|---|---|---|---|---|
| 3.46.1 (system) | 572 OK, 1 skipped (43.5 s) | 280 OK (129.9 s) | 49 OK (22.5 s) | 196 s |
| 3.40.1 (built from the official 3400100 amalgamation, LD_LIBRARY_PATH) | 572 OK, 1 skipped (42.5 s) | 280 OK (133.1 s) | 49 OK (22.5 s) | 198 s |

901 run, 900 passed, 0 failed, 1 skipped (the root-only bind mount test) in each.
Tripwire clean. Under 3.40.1 the corrupted-freelist DB reports "Main freelist: size is
0 but should be 7" (checked directly), so the round-3 assert would have failed there.

## Fix round 5 (Quest/Linear REVISE on 8a5a99f)

1. Order (Linear P2). `handle_doctor` now runs resolve_home, the default-home testing
   guard and `check_install_path_secure(home=...)` (home, $home/runs and the default
   $home/workspaces against the install root) BEFORE `--fix-perms` writes anything;
   then the repair; then the fail-closed DB/KDF checks; then the install-path check
   again with the DB's workspaces.root and repo paths (which can only be read after the
   repair). An install dir (or the mesh/ package dir in it) used as the home gives rc 2
   INSTALL_PATH_INSECURE with its mode unchanged (0500 stays 0500).
2. TOCTOU (Linear + Quest). The walk is fd-based. The home is opened
   O_PATH|O_NOFOLLOW|O_DIRECTORY; every entry is opened O_PATH|O_NOFOLLOW relative to
   its parent's fd and checked with fstat (type, uid); the chmod acts on that fd. An
   O_PATH fd is not accepted by fchmod on Linux and a 000 file cannot be opened for
   reading, so the chmod goes through /proc/self/fd/N (which names the open inode, not
   the directory entry); without /proc it falls back to
   `os.chmod(name, mode, dir_fd=parent_fd, follow_symlinks=False)`, and if that is
   unsupported the entry is skipped and reported. A directory is listed through a
   readable fd reopened from its O_PATH fd and checked to be the same dev/ino.
   The walk is iterative and holds one directory fd at a time: it descends by fd and
   climbs back with openat(cur, "..", O_NOFOLLOW), checking the parent's recorded
   dev/ino (a directory moved mid-walk stops the walk and is reported). So depth is
   bounded by neither the recursion limit nor RLIMIT_NOFILE (a 1500-level tree is
   repaired). Test hook `doctor._RACE_HOOK(stage, rel)` (None in production) runs
   between the check and the chmod / listing.
3. Quest P3s. Files under workspaces/ keep the owner's bits (`(mode & 0o700) | 0o600`,
   so a checked-out script stays executable for the owner); group/other bits are
   removed. Elsewhere regular files still get 0600, as on main (main used 0600 for
   every file, which stripped +x in workspaces). Entries skipped because they are
   symlinks, owned by another uid, or could not be chmodded safely now give
   `{"fixed": false, "skipped": [{"path", "reason"}], "reason": ...}` with rc 0
   (sockets/FIFOs/devices are still left alone silently, as on main). `MESH_HOME=""`
   counts as unset and falls back to the default ~/.agent-mesh (documented in
   `mesh help env`); an empty `--home` is still refused.

Tests: `tests/unit/test_doctor_fix_perms_r5.py` (11 tests): install dir and mesh/ dir as
home (rc 2, mode unchanged, with and without --fix-perms, via a private copy of the
install); race tests that swap a leaf, an ancestor directory, a directory between its
chmod and its listing, and a known top-level dir for a symlink to an outside 0755 dir
holding a 0400 file (outside untouched; the checked inode got the chmod); other uid
reported; 1500-level tree with a 000 deepest dir; workspaces vs runs modes; symlink
reported as fixed:false; MESH_HOME="" fallback and its help text. Mutation check (each
alone): chmod by name following symlinks (2 failures), listing by name without the
inode check (2), the pre-repair install-path check removed (2); all killed.

### Fix round 5 suite runs

Full `scripts/test.sh` on box2 (Python 3.13, shared box), run twice back to back:

| SQLite | unit | integration | recovery | wall |
|---|---|---|---|---|
| 3.46.1 (system) | 583 OK, 1 skipped (41.6 s) | 280 OK (130.7 s) | 49 OK (22.3 s) | 195 s |
| 3.40.1 (official 3400100 amalgamation, LD_LIBRARY_PATH) | 583 OK, 1 skipped (42.8 s) | 280 OK (129.6 s) | 49 OK (22.3 s) | 196 s |

912 run, 911 passed, 0 failed, 1 skipped (the root-only bind mount test) in each.

## Fix round 6 (Linear/Quest REVISE on dfdabd0)

1. Base red on Python 3.11 (P2). `test_deep_tree_is_walked_iteratively` left a
   1500-level tree and `shutil.rmtree` in the harness teardown recursed once per level
   on Python 3.11 (RecursionError is not an OSError, so `ignore_errors` did not catch
   it). New `tests.helpers.rmtree_deep` removes any tree iteratively (one dir fd held,
   descend by fd, climb with `openat("..")`, 0700 before listing, never follows
   symlinks, errors ignored). The test removes its tree in its own `finally`, and the
   harness teardown and `_CwdCase` cleanup use `rmtree_deep`. The full suite now runs
   on Python 3.11.2 / SQLite 3.40.1 (Debian 12).
2. Home open bound to the precheck (Linear P2). `handle_doctor` records the home's
   (st_dev, st_ino) right after resolve_home and the path checks; `_fix_perms` opens
   the home from "/" one component at a time with `O_PATH|O_NOFOLLOW|O_DIRECTORY`
   (a symlinked ancestor fails instead of being followed) and requires the fd's
   identity to equal the recorded one, else nothing is chmodded and the home is
   reported `fixed: false`.
3. Hard links (Linear P2, Quest P3). Regular files with `st_nlink > 1` are skipped
   with reason `"hardlinked"` (fixed: false), so an outside file linked into the home
   keeps its mode.
4. Re-verify after the walk (Linear P2). Every repaired entry's identity and target
   mode are recorded; after the walk a second fd-based walk (same one-fd iterative
   walker) checks that each path still names that inode (O_NOFOLLOW open + fstat) with
   that mode. Any mismatch, or a repaired entry that is not found again, makes doctor
   fail with rc 2 `INSECURE_PERMISSIONS`, `details.fixed = false` and `details.unsafe`
   (path + reason); children of an unsafe directory are not repeated.
5. No by-name fallback (Linear). `_chmod_fd` tries `fchmod` and `/proc/self/fd/N`
   only; without them the entry is skipped with reason `"no fd-bound chmod"`.
6. fd leaks (Linear). The listing fd is closed if its fstat raises; the walk closes
   the child fd if fstat/listdir raises and closes its current fd on every path; any
   OSError in the walk is reported as a skip instead of escaping.
7. Quest P3. The per-entry uid check is tested by faking fstat's `st_uid`. The install
   overlap check now refuses an install root equal to or anywhere inside the home
   (e.g. `$MESH_HOME/logs/install`), checked after the more specific runs/workspaces/
   repo checks so their conflict is still the one named.

Tests: `tests/unit/test_doctor_fix_perms_r6.py` (17 tests): iterative removal of a
1500-level tree with a 000 deepest dir (no fd leak, symlink not followed); an ancestor
swapped for a symlink between the precheck and the open, and the home replaced by
another real directory (outside untouched); handle_doctor passes the precheck identity;
hardlinked 0400/0444 outside file untouched; a dir swapped for a new 0777 dir mid-walk
and a mode loosened after repair are reported unsafe (and doctor fails rc 2 in
process); no /proc gives "no fd-bound chmod" with no by-name chmod; every fstat / open
/ listdir call failing in turn leaves the fd count unchanged; the per-entry uid check;
install root nested in the home (unit and via a copied install under `$home/logs`).
Mutation check, each alone, all killed: no identity check (2 failures), plain home open
without the identity check (3), no nlink check (2), no re-verify (3), by-name fallback
restored (2), child fd not closed on listdir error (1), listing fd not closed on fstat
error (1), no per-entry uid check (1), home exempt from direction 1 (3).
