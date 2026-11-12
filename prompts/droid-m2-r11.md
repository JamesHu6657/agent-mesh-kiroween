You are the coder for run m2-r11 of Agent Mesh v1 (Python). Work ONLY in this git worktree (branch wt/m2-r11). Never edit docs/coordination/**, prompts/**, scripts/coord/**, docs/*HANDOFF*.md (in addition to the never-edit list below). Measure the full suite before and after. At the end print the DONE list and stop.

## Common block for every M2 run (Quest pastes it at the top of each prompt)

You are the IMPLEMENTER of "Agent Mesh v1" in the current directory (a git checkout of mesh-fresh-v1, usually a worktree on branch `wt/<name>`). Another coder may work in a separate worktree at the same time: work ONLY in the current directory and only on the files your run names or clearly needs.
Status: M1a, M1b and M1c are ACCEPTED (M1 exit criteria met; see docs/m1c-gate-report.md once it exists). Now implement gate **M2: Git workspaces, merge, lead turns** (plan §16 M2): repo projects (§10.2), worktrees (§10.3), submission git info (§10.4), the merge pipeline T14–T18 (§10.5), `project deliver` (§10.6), `workspace prune/delete` (§10.7), merge crash recovery (§11.7 step 5), lead turns with debounce (§12.4), the `mesh-actions` protocol, the lead prompt (§12.6) and resume plumbing (exercised with the fake adapter's `emit_session`). M2 items: checklist sections **K (K1–K14)** and **L (L1–L6)**. Every M1 item must still pass (checklist §0), including A1 (`scripts/test.sh` < 10 min), A3 (tripwire empty), A6 (gitops allow-list) and A8 (`doctor --check-invariants` clean, now with I9). B11 is superseded by section K. Verbs of M2 not yet built at a commit keep returning rc 2 `NOT_IMPLEMENTED_IN_M1` with `details.milestone:"M2"`.
The run plan is docs/coordination/plans/m2-plan.md. Read the planner notes and your run's section fully. The open questions there were decided by the coordinator as stated in your run's section.
Authoritative docs: docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md (read §0 notation and every item ID you cover), docs/impl-notes-m1a.md, docs/impl-notes-m1b.md, docs/impl-notes-m1c.md (and docs/impl-notes-m1c-run2.md if present), and the docs/impl-notes-m2-r*.md files of earlier M2 runs.
Hard rules:
- Never edit docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, docs/coordination/**, prompts/**, scripts/coord/**, .kiro/**, docs/HANDOFF.md, docs/QUEST-HANDOFF.md, docs/release-requirements.md, docs/m1*-*.md, docs/impl-notes-m1*.md, logs/**, tests/fixtures/real/**. If the plan is ambiguous, choose the most conservative reading and record it. If something in the plan is a real blocker, STOP and report it instead of inventing scope.
- Keep ALL existing behaviour and tests green. Never weaken or delete an assertion. The only assertions you may change are the ones your run section names (stub milestones superseded by a built verb). List each one with its reason in your notes file.
- Every checklist item you cover gets a test with `ACC: <ID>` in its docstring. Where the item is about CLI behaviour, the test drives the REAL CLI path (bin/mesh subprocess against a /tmp/mh-* daemon via the harness). A test that covers only part of an item still gets the tag, and your notes list the item as partial.
- **Git safety.**
  - Mesh product code runs git ONLY through `mesh/core/gitops.py` (A6; `test_no_git_subprocesses_outside_gitops` must stay green). Mesh never pushes, never sets git identity or config, never rebases, resets, checks out, or forces.
  - Tests run git ONLY through the helpers in `tests/gitfixture.py`, always with `cwd` inside a `/tmp/mh-*` directory the test created. Never run git without an explicit cwd. Never pass the Mesh checkout (`/workspace/mesh-fresh-v1`, `/home/node/workspace/mesh-fresh-v1`, any `mesh-wt-*`) or any directory outside `/tmp/mh-*` as `--repo`, as a repo path, or as a cwd for git.
  - Do not put `GIT_AUTHOR_*` or `GIT_COMMITTER_*` into meshd's env. A test that needs a commit in a task worktree makes it with `git -c user.name=t -c user.email=t@example.invalid commit …` via the fixture helper, unless the K0 repo-local identity applies.
- Mock adapters and fixtures ONLY. Tasks and lead turns launch only on the `fake` adapter (testing mode); inbox agents use `none`. Vendor adapters stay `launch_enabled:false` (exit 13, milestone M3). No real vendor CLI runs, no inference, no login, no network, no TCP ports, no reading vendor credential or config dirs. Python stdlib only.
- Tests use /tmp/mh-* homes, /tmp/mh-* git fixtures and Unix sockets only, and clean up only what they created: homes, fixture repos, daemons, and processes whose environ carries the `MESH_RUN_ID` of a run in their own home. Never kill by name or pattern. No push (also not to the fixture's bare remote), no remote creation outside the fixture, no deploy.
- Authority is enforced by credentials and capabilities in the daemon, never by a role string. Lead run tokens act only for their project and only while their lead assignment is current.
- **Test budget.**
  - Group the Check clauses of one item, or of closely related items, into one scenario test with one harness.
  - Create only the agents the test needs.
  - Use the inbox executor `ibx` with test-made commits wherever the item does not name `fx`.
  - Use testing-mode settings instead of fixed sleeps, and poll with `wait_for` instead of `sleep`.
  - Mark `@serial` only what truly depends on wall-clock timing.
  - Report the suite wall time before your first change and after your last.
- Commit after each sub-step: `git commit -m "<type>(m2): <what> (ACC: …)" --author "agy gemini-3.8-flash-high <agy@localhost>"`, type = feat/test/docs. Run `bash scripts/test.sh` (rc 0, tripwire empty) before every commit that touches code or tests.
- agy 1.5.3 is installed while fixtures are 1.5.2; agy capabilities reported `stale` is the honest expected result. Do not change fixtures or expected hashes. If `test_fixture_hashes` fails, STOP and report.
Shell permission note: any command outside the allow-list is auto-denied and ENDS your run.
- Allowed prefixes only: `python3` (incl. `python3 -m unittest`), `bin/mesh`, `./bin/mesh`, `bash scripts/test.sh`, `scripts/test.sh`, `./scripts/test.sh`, `mkdir -p`, `ls`, `grep`, `rg`, `sha256sum`, `find .` (must literally start with `find .`; never `-delete` or `-exec`), `cat`, `head`, `tail`, `wc`, `pwd`, `sed -n`, `stat`, `file`, `diff`, `tree`, `du -sh`, `which`, `git status`, `git diff`, `git log`, `git show`, `git add`, `git commit`, `git grep`, `git blame`, `git ls-files`, `git rev-parse`, `git cat-file`, `git branch --list`.
- NOT allowed: `git checkout`/`restore`/`reset`/`stash`/`clean`/`switch`/`worktree`/`clone`/`config`/`merge` (to see an old file use `git show <ref>:<path>`; to undo your own edit, edit the file back), `rm`, `mv`, `cp`, `kill`, `pkill`, `ps`, `sleep`, `env`, `cd`, `timeout`, and `bash`/`sh` with any other script. Your shell never runs git against a temp repo; tests do that through python3.
- You cannot `chmod` new files, so do not create new executable scripts; run tools with `python3 <file>`.
- ONE simple command per call: no `&&`, `;`, `|`, redirection or `$(…)`. Prefer the file and search tools. Use python3 for cleanup only inside /tmp/mh-* paths your tests created.
- `bash scripts/test.sh` takes up to ~10 min: run it with a long timeout (900 s+) in the same call and wait for it. Never end your turn while "waiting".
Write large files in several edits of at most ~200 lines each. Be efficient: read each file you need once, no exploratory benchmarking loops.
Notes: write your readings, deviations, changed assertions (with reason), ACC coverage (full/partial, test names), the files you changed and the suite wall times into a NEW file `docs/impl-notes-m2-<run>.md` (e.g. `docs/impl-notes-m2-r3.md`). Do not edit another run's notes file.
DONE for every run, in addition to the run's own list:
1. One commit per sub-step, each with the author above.
2. The final `bash scripts/test.sh` is rc 0 with both tripwire files empty, and the total wall time is printed and under 10 minutes.
3. `grep -rn "ACC: <ID>" tests/` finds a passing test for each ID of the run.
4. No existing assertion was weakened, and every changed assertion is listed with its reason.
5. `git branch --list 'mesh/*'` in the current checkout prints nothing (no test touched the Mesh repo).
6. `docs/impl-notes-m2-<run>.md` exists.
At the end print: commits, files changed, exact test commands with exit codes, test counts and the wall time of `bash scripts/test.sh` (before and after), the ACC IDs covered (full or partial), readings/deviations, and anything not done. Then stop.

---

## Run r11 (m2-r11, m2-resume): session ids and resume plumbing

[Common block.] Items: **L5**. Coordinator decision applied: Q14.
Sub-steps:
1. `feat(m2): fake session parse and resume flag`.
   - `FakeAdapter.parse` returns the `session_id` of the last stdout line that is a JSON object with `session_id`.
   - `FakeAdapter.build` adds `--fake-resume <id>` when `resume_session_id` is set.
   - fake_agent.py accepts and records `--fake-resume`.
   - tests/unit/test_adapters.py: add golden cases for fake with and without resume. The existing fake golden cases are unchanged.
2. `feat(m2): store session ids and resume when verified (ACC: L5)`.
   - On every task and lead-turn exit, store `runs.session_id`. Task runs also set `tasks.last_session_id`; lead turns set `lead_assignments.session_id`.
   - At T5 for rework/retry and at lead-turn start: pass `resume_session_id` only if the adapter's `resume` capability is verified (fake option `resume_verified`) and an id exists.
   - The prompt is always the full prompt, including rework feedback (Q14).
3. `test(m2): resume plumbing tests (ACC: L5)`. New tests/integration/test_resume.py, two fake agents (`--option resume_verified=true` and without), executor and lead, each with `emit_session` in a `steps` list: the session ids are stored; with the option, the rework run's `argv_redacted_json` has `--fake-resume <id>`; without it, the argv lacks it and `prompt.md` contains the reject reason; the same check for the next lead turn.
4. `docs(m2): notes for r11`.
FILES: mesh/adapters/fake.py, mesh/testing/fake_agent.py, mesh/daemon/supervisor.py, mesh/daemon/lead_turns.py, tests/unit/test_adapters.py (new cases only), tests/integration/test_resume.py (new), docs/impl-notes-m2-r11.md.


ADDITIONS FOR THIS RUN (coordinator):
A. M5, RunRequest options (from Kiro's M3 plan review). At T5 (supervisor.py ~498-505) and at lead-turn start, build RunRequest(..., options={"binary": agents.binary_path if set, "run_id": <run id>, **json.loads(agents.adapter_options_json)}). Today agents.binary_path never reaches build() and droid gets `--log-group-id run_default` (droid.py:80). Add a unit test on the fake and droid argv (droid argv contains --log-group-id <real run id>; binary_path is argv[0] when set). Add mesh/adapters/droid.py to FILES only if build() must read options["run_id"] differently; tests in tests/unit/test_adapters.py (new cases only).
B. r10b review N1 MINOR (durable queued lead merges): lead_turns.py ~932 keeps _QUEUED_MERGES in memory only. On daemon start (or first supervisor tick), re-queue every lead.action_result with cmd task.merge, ok, status queued, that has no task.merging event and no phase:merge_start result with the same request_id; TX1 re-checks everything, so re-queue is safe. Recovery test in tests/integration/test_resume.py or a new tests/recovery/test_recovery_lead_merge.py (allowed, new file): kill meshd after the close commit and before the merge starts (use an existing testing hold hook if available; if none exists without touching other files, test the re-queue function directly in a unit test instead).
C. r10b review N2 NIT: fix the supervisor._watch_lead_turn docstring (supervisor.py ~1376), which still says the deadline is the only termination reason.
Keep new tests lean (idle budget 600 s, now ~455-470 s); poll, no fixed sleeps inside token-bearing turns.

COORDINATOR RULING (after your first attempt STOPPED, correctly): the existing fake golden cases at tests/unit/test_adapters.py ~918 and ~969-994 pass resume_session_id="sess_1" and expect no resume flag; that expectation encoded the pre-r11 behaviour that r11 intentionally changes. You are authorized to update ONLY those fake golden expected argvs to include --fake-resume sess_1 (keep every other assertion in those cases, and keep or add a separate case without resume_session_id that expects no flag). Record this in the r11 notes as a coordinator-approved expectation change. Baseline suite already measured (897 s, loaded box); do not re-run the baseline.
