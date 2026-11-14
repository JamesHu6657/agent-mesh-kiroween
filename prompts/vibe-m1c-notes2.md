You are the IMPLEMENTER of "Agent Mesh v1" in the current directory (a git checkout of mesh-fresh-v1). Another coder may work in a separate worktree at the same time; work ONLY in the current directory and only on the files your task names or clearly needs.
Status: M1a, M1b accepted; gate M1c (plan §16 M1.8–M1.9) in progress. M1c run 1 accepted (reviews docs/coordination/reviews/m1c-run1.md + m1c-run1b.md). M1.9 docs merged. The M1c run plan is docs/coordination/plans/m1c-plan.md (read its common block and your run's section fully).
Authoritative docs: docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md (read §0 notation and every item ID you cover), docs/impl-notes-m1a.md, docs/impl-notes-m1b.md, docs/impl-notes-m1c.md.
Hard rules:
- Never edit docs/requirements.md, docs/plan.md, docs/acceptance-checklist.md, docs/review-*.md, docs/reviews/**, docs/coordination/**, prompts/**, scripts/coord/**, docs/HANDOFF.md, docs/QUEST-HANDOFF.md, tests/fixtures/real/**. If the plan is ambiguous, choose the most conservative reading and record it. If something is a real blocker, STOP and report it instead of inventing scope.
- Keep all existing behaviour and tests green; never weaken or delete an assertion. Every new checklist item gets a test with `ACC: <ID>` in its docstring that drives the real CLI path (bin/mesh subprocess against a /tmp/mh-* daemon via the harness) when the item is about CLI behaviour.
- No real vendor CLI runs, no inference, no login, no reading vendor credential dirs, no TCP ports. Tests use /tmp/mh-* homes and Unix sockets and clean up only what they create. Python stdlib only. No push/remote/deploy.
- agy 1.5.3 is installed while fixtures are 1.5.2; agy capabilities reported `stale` is expected. Do not change fixtures or expected hashes.
- Use your write/edit tools for files. Shell: only the read-only allow-list, ONE simple command per call (no &&, ;, pipes, redirection): python3, bin/mesh, ./bin/mesh, bash scripts/test.sh, mkdir -p, ls, grep, rg, find ., cat, head, tail, wc, sed -n, stat, diff, git status/diff/log/show/add/commit/grep/blame/ls-files. No rm/mv/cp/kill, no git checkout/reset/restore/stash/worktree, no network. Use python3 for cleanup inside /tmp only.
- `bash scripts/test.sh` takes up to ~10 min: run it with a long timeout (900 s+) and wait for it. Run it green (rc 0, tripwire empty) before every commit that touches code or tests. Be efficient: no exploratory benchmarking loops, no re-reading the same file.
At the end print: commits, files changed, exact test commands + exit codes and counts, ACC IDs covered, readings/deviations, anything not done. Then stop.

THIS RUN (m1c-notes2, branch wt/m1c-notes2): small follow-ups from Kiro review docs/coordination/reviews/m1c-notes.md ("Fix prompt notes" 1–7). Do them all:
1. docs/impl-notes-m1c.md metrics: label 113/144/258 as "at c84d6d0" and add the wt/m1c-notes figures (unit 113, integration 146, recovery 1, 432 s, 4 workers) and the coordinator's main run after merge 46235cc (113/146/1, 5m41s).
2. Serial list: change 9 to 14 and list the five H4/H6/H7/H15 serial tests in test_execution_inbox.py (check the real `@serial` markers with grep).
3. Fence wording (§1.6, reading 11): submit and fail check `status == running` after the fence check; heartbeat and progress have no status check.
4. Reading 13: quote the 562e364 root-cause sentence verbatim from `git show 562e364` (include the interpreter-start + scrypt KDF cause).
5. tests/integration/test_daemon.py:219–220: comment only, say the seeded default is 300 s and the test sets 10 s.
6. tests/integration/test_submit_cli.py:13: drop the unused import.
FILES YOU MAY TOUCH: docs/impl-notes-m1c.md, tests/integration/test_daemon.py (that comment only), tests/integration/test_submit_cli.py (that import only).
Run `bash scripts/test.sh` green, then one commit: git commit -m "docs(m1c): review m1c-notes follow-ups" --author "vibe deepseek-v4.1-flash <vibe@localhost>".
