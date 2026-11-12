# Implementation notes — m4-p3-cleanup

Run by a Quest coder (a helper agent declined the batch, 2026-11-12 21:06). Scope: the five
P3 items carried from the M3 gate into M4 polish. No new features.

1. **kiro/agy prompt position (Carbon r10 P3).** Neither real help fixture documents `--`
   (`tests/fixtures/real/kiro/3.1.0/chat-help.txt` shows `[OPTIONS] [INPUT]`; agy 1.5.x uses
   Go-style `Usage of agy:` with no `--` line), so the code does not add `--`. Instead
   `mesh.adapters.base.positional_prompt` gives a prompt that starts with "-" a leading
   newline, so the last argv item can never parse as an option. Mesh's own prompts start
   with "# Agent Mesh task" or "Read the file", so golden argv tests are unchanged.
2. **F7.** `tests/stubs.py` droid stub prints `droid 1.6.0` and serves the 1.6.0 help
   fixtures. N1 (`tests/integration/test_adapters.py`) still compares `launch_enabled` with
   `effective_launch` and now also asserts droid is launch-enabled by evidence. The two
   probe-version assertions moved to 1.6.0.
3. **L4 guard + C-5.** `lead_launch_check` keeps the vendor rule first; after it, the gate
   is `evidence.effective_launch(conn, adapter, testing_mode, binary_path)` (the lead
   agent's `binary_path` from `plan_lead_turn`). Today only non-vendor kinds reach it, where
   it returns the class value, so behaviour is unchanged. New guard test: every vendor
   adapter, with evidence forced to pass, is refused `vendor_lead_m3_lite` and evidence is
   never consulted. L4 itself (seen tracking / close-safety for adopted lead turns) stays a
   known limitation; vendor leads stay off.
4. **`fail_held_launch_tasks`.** A failing task is logged and the pass continues. Fail
   closed per task: `supervisor.spawn_launch_runs` now drops every candidate whose adapter
   is held (`evidence.held_milestone`), so an errored held task stays queued and is never
   spawned. If the whole pass raises, T5 still starts nothing that tick.
5. **docs/adapters.md** header no longer cites `adebc01`; it points to the code and the
   committed evidence.

Tests: `tests/unit/test_m4_p3_cleanup.py` (new), updated N1 and probe-version asserts.
Coordination: m4-kiro-n13 empties `LAUNCH_HOLDS`; the new tests patch the table, and the
spawn filter uses `held_milestone`, so it follows whatever hold source that run keeps.
