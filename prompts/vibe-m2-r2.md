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

## Run r2 prompt (m2-r2, m2-leadblocks): FULL. EARLY: may run while M1c is still in progress (and in parallel with r1)

[Common block above]

STATUS OVERRIDE for this run: M1c is NOT finished yet. Another coder is working on M1c in a separate worktree. This run creates ONLY new files: mesh/core/lead_actions.py, mesh/core/lead_prompt.py, tests/unit/test_lead_actions.py, tests/unit/test_lead_prompt.py, docs/impl-notes-m2-r2.md. Do not edit any existing file.

THIS RUN (m2-r2): two pure, daemon-free modules used later by the lead-turn runs (r9, r10). Nothing calls them yet.

Items: **L6** (`test_lead_prompt_cap` is the checklist's Check; the integration clause "messages that did not fit stay `pending`" is confirmed in r9), **L2** (partial: parsing and the allowed-command list; execution is r10).

START
1. `git status` clean; record HEAD. Baseline `bash scripts/test.sh` (rc, counts, wall time).
2. Read: plan §12.4 (actions protocol, allowed cmds, ≤ 50, idempotency key), §12.5 (deliverable block parsing, for the same "last fenced block" rule), §12.6 (lead prompt contents and size cap), §8 (handover contents), checklist L2 and L6; mesh/adapters/base.py (`max_argv_prompt_bytes`), mesh/adapters/kiro.py (class attributes only).

SUB-STEPS

(1) `mesh/core/lead_actions.py`. Commit `feat(m2): mesh-actions parser and allowed command list (ACC: L2)`.
- `ALLOWED_ACTION_CMDS`: exactly the 15 cmds of §12.4: `task.create`, `task.assign`, `task.reassign`, `task.accept`, `task.reject`, `task.retry`, `task.escalate`, `task.cancel`, `task.merge`, `dep.add`, `dep.remove`, `project.note`, `project.complete`, `project.deliver`, `approval.request`. `MAX_ACTIONS = 50`.
- `parse_actions(final_text: str) -> ParsedActions` (dataclass with `actions: list[Action]`, `error: str | None`, `block_found: bool`). Take the **last** fenced block whose info string is exactly `mesh-actions` and whose body parses as a JSON **array**; blocks that do not parse, or are not arrays, are skipped, same as the deliverable rule in §12.5. No valid block → `actions=[]`, `block_found=False`, no error. More than 50 items → `actions=[]`, `error="too_many_actions"` (conservative: the whole block is refused; record).
- `Action` (dataclass): `index` (0-based position in the block), `cmd`, `args` (dict), `error` (None or one of `invalid_item` for a non-object item, a missing or non-string `cmd`, or a non-object `args`; `cmd_not_allowed` for a cmd outside the list, e.g. `config.set`). Item errors do not stop the other items; the executor (r10) reports each failed item as `lead.action_failed`.
- `action_idempotency_key(run_id: str, index: int) -> str` returns `act:<run_id>:<index>` and checks the result against the `--idempotency-key` charset of plan §13.1 (`[A-Za-z0-9._:-]`, 1–128 chars).
- tests/unit/test_lead_actions.py (`ACC: L2` in the class docstring, "parser part"): the last valid block wins over an earlier one; an invalid-JSON last block falls back to the previous valid one; a non-array block is ignored; `config.set` → item error `cmd_not_allowed` while the `task.create` item in the same block stays valid; 51 items → `too_many_actions`; the key format and charset; a text without blocks → `block_found False`.

(2) `mesh/core/lead_prompt.py`. Commit `feat(m2): lead prompt renderer with size cap (ACC: L6)`.
- Input dataclass `LeadPromptInput`: `project` (id, name, goal), `criteria` (list of `{id, text}`), `tasks` (list of `{id, title, status, assignee}`), `approvals` (open ones: `{id, category, summary}`), `notes` (list of `{ts, text}`, oldest first), `events` (list of `{seq, ts, type, task_id, reason}`, oldest first), `messages` (pending lead messages: `{id, type, created_at, payload}` oldest first), `authority` (dict: categories needing user approval, the allowed models per assignable agent, `lead_cannot_be_assignee: True`), `limit_bytes` (int).
- `render_lead_prompt(inp) -> RenderedLeadPrompt` (dataclass: `text`, `included_message_ids`, `omitted` dict with counts per dropped part, `bytes`). Sections in this order:
  1. Role line and project header.
  2. The goal **on its own line** (plan note Q11: the fake test double selects behaviour from a line `^FAKE {…}$`).
  3. Criteria with ids.
  4. Task table.
  5. Open approvals.
  6. Last notes.
  7. Recent events.
  8. Pending messages as JSON.
  9. The authority rules.
  10. The `mesh-actions` schema with `ALLOWED_ACTION_CMDS` imported from lead_actions.py, and the `project complete` report format (§8).
  11. An "Omitted:" line naming what was dropped.
- Cap (§12.6): if the UTF-8 size exceeds `limit_bytes`, drop in this order until it fits:
  1. the oldest events;
  2. the oldest notes;
  3. terminal tasks (`accepted`, `merged`, `cancelled`), replaced by per-status counts;
  4. fewer pending messages: keep the oldest first, at least one. If even one message does not fit, cut its body to 4000 chars and add a pointer to `mesh inbox list`.
  The "Omitted:" line states each drop with its count. The function never raises for size and never returns a prompt over the limit. If the fixed parts alone exceed the limit, cut the goal and criteria texts as a last resort and say so; record it.
- tests/unit/test_lead_prompt.py:
  - `test_lead_prompt_cap` (`ACC: L6`): 50 pending messages (each ~1500 bytes), 200 events, 100 notes, 300 tasks (half terminal); `limit_bytes = KiroAdapter.max_argv_prompt_bytes` (the kiro readonly lead case of the checklist). Assert: `bytes <= limit`; the "Omitted:" line names events, notes, terminal tasks and messages with counts; `included_message_ids` is a non-empty strict prefix of the input order (the rest "stay pending", since the function does not touch them); no exception.
  - A small input with nothing omitted renders every section and no "Omitted:" drops.
  - One 300 KB message body → that single message is kept, cut to 4000 chars, with the `mesh inbox list` pointer.
  - The goal line equals the goal text exactly (Q11).
  - The `mesh-actions` section lists exactly `ALLOWED_ACTION_CMDS`.

(3) Notes. Commit `docs(m2): notes for r2`.
- docs/impl-notes-m2-r2.md: the parsing rules and every reading (`too_many_actions` refuses the whole block; item errors; drop order details; last-resort cut); how r9/r10 are meant to call the two modules; suite wall time.

FILES: exactly the five new files named above.

TESTS: `python3 -m unittest tests.unit.test_lead_actions tests.unit.test_lead_prompt -v`; `bash scripts/test.sh` before each commit.

DONE (in addition to the common list): both modules are pure (no DB, no sockets, no subprocess; a grep for `subprocess`, `sqlite3`, `socket` in them finds nothing), and `git diff --stat <base>..HEAD` lists only the five new files.

---


Commit with --author "vibe deepseek-v4.1-flash <vibe@localhost>" (overrides the author above). Use your file tools to edit; shell only the read-only allow-list, one simple command per call. At the end print commits, files, test commands + rc + counts, ACC IDs, readings, anything not done. Then stop.
