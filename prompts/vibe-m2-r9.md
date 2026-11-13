You are the coder for run m2-r9 of Agent Mesh v1, working ONLY in this worktree (branch wt/m2-r9). Commit with --author "vibe deepseek-v4.1-flash <vibe@localhost>", one commit per sub-step. Never push. Never edit docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, tests/fixtures/real/**, docs/coordination/**, prompts/**, scripts/coord/**, docs/*HANDOFF*.md. r7 (merge crash recovery: recovery.py, merge.py, tests/recovery/test_recovery_merge.py) runs in parallel in another worktree: do NOT edit those files; if you find you must, STOP and report. Work efficiently: read only what the sub-steps need, start editing early, keep scopes narrow, run focused tests after each sub-step, then the full bash scripts/test.sh once at the end (rc 0 required; the suite is near the 10-min A1 limit, so keep new tests lean, poll instead of sleeping, use testing-mode short timeouts). Already merged and usable: mesh/core/lead_prompt.py (render_lead_prompt) and mesh/core/lead_actions.py (parse_actions) from r2. At the end print commits, files, test commands + rc + counts + wall time, items done, anything not done. Then stop.

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
  - Tests run git ONLY through the helpers in `tests/gitfixture.py`, always with `cwd` inside a `/tmp/mh-*` directory the test created. Never run git without an explicit cwd. Never pass the Mesh checkout (`/workspace/mesh-fresh-v1`, `/home/user/workspace/mesh-fresh-v1`, any `mesh-wt-*`) or any directory outside `/tmp/mh-*` as `--repo`, as a repo path, or as a cwd for git.
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


## Run r9 (m2-r9, m2-leadturns): lead turns (parallel with r7)

[Common block.] Do NOT edit mesh/daemon/recovery.py or mesh/daemon/merge.py (r7 runs in parallel). If you must, STOP and report.
Items: **L1, L3, L6** (the "stay pending" clause in a real turn), **K13**. Coordinator decisions applied: Q8 (trigger rule), Q9 (testing-only backoff), Q11.
Sub-steps:
1. `feat(m2): lead run tokens and lead-turn authz`.
   - Issue a run token at lead-turn start with scope `{"role":"lead","run_id","project_id","lead_assignment_id"}` (§7.1).
   - authz: a lead run token gets the lead column of §7.3 for `scope.project_id` only. If `lead_assignment_id` is not the current assignment → rc 9 `LEAD_SUPERSEDED`, checked before the other lead checks.
   - The executor run-token branch and run-session binding stay unchanged.
   - Add matrix rows to the authz unit test.
2. `feat(m2): lead-turn scheduler, debounce, backoff and unreachable (ACC: L1, L3)`. New `mesh/daemon/lead_turns.py`, called from the scheduler tick:
   - Trigger (§12.4): a launch-mode lead; pending `lead_wake` messages with `next_attempt_at <= now`, at least one of a type other than `lead.action_failed` (Q8); the oldest ≥ `lead.wake_debounce_s` old, or ≥ 20 pending; `leadturn:<prj>` free; project active.
   - Build `LeadPromptInput` from the DB, render with r2's `render_lead_prompt` (limit = the lead adapter's `max_argv_prompt_bytes`).
   - In one tx: mark exactly `included_message_ids` (≤ 50) `delivering` with `delivering_run_id`; insert run `kind=lead_turn` with `lead_assignment_id`; issue the token; take the lease (holder meshd).
   - Effect: for repo projects, refresh `lead/<agent>/repo` when it is clean (`worktree remove` + `worktree add --detach`; dirty → keep and log); write `prompt.md`; spawn via the M1c spawn helper in `lead/<agent>/` with profile `lead.profile` and deadline `lead.turn_timeout_s`.
   - Exit (the supervisor routes `kind=lead_turn` here, never to T7/T8):
     - exit 0 → messages `acked`;
     - otherwise → `pending`, `attempts += 1`, `next_attempt_at = now + backoff[attempts-1]` (30/120/600 s; testing-only setting `lead.wake_backoff_s`, Q9, rejected outside testing mode like `auth.lockout_window_s`);
     - `attempts >= outbox.max_attempts` → `dead` and event `lead.unreachable`.
   - Then close the run, revoke the token, release the lease (release condition).
3. `test(m2): lead turn integration tests (ACC: L1, L3, L6, K13)`. New tests/integration/test_lead_turns.py. Lead `flead` = fake, launch, `--can-lead`; the project uses `--lead flead`; the FAKE spec goes in `--goal` (Q11); `lead.wake_debounce_s=2`.
   - L1: project creation plus 3 follow-up intake messages within 1 s → exactly one lead turn covers them; across the test no two `lead_turn` runs of the project overlap in time.
   - L3: goal `FAKE {"fail":{}}`, `lead.wake_backoff_s=[1,1,1]` → exactly 3 attempts, then the messages are `dead`, exactly one `lead.unreachable` each to the user and grok, and no 4th turn within 5 s.
   - L6 clause: 25 pending messages of ~6 KB → the turn's `delivering_run_id` rows are a strict subset; the others are still `pending` after the turn.
   - K13: repo project with `flead` and `FAKE {"sleep":{"seconds":4}}`; during the turn `worktree list --porcelain` shows `lead/<agent>/repo` detached and not equal to any task worktree.
4. `docs(m2): notes for r9`.
FILES: mesh/daemon/lead_turns.py (new), mesh/daemon/scheduler.py, mesh/daemon/supervisor.py, mesh/core/authz.py, mesh/daemon/server.py (run-token scope), mesh/core/settings.py (testing-only setting), mesh/core/lead_prompt.py (only an input helper, if needed), tests/integration/test_lead_turns.py (new), the authz-matrix unit test file (tests/unit/test_projects.py per M1c), tests/unit/test_settings.py (the testing-only setting), docs/impl-notes-m2-r9.md.

