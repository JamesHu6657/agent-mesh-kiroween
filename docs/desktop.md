# Agent Mesh Desktop Client

Agent Mesh ships an optional chat-first desktop GUI shell (`mesh.desktop`) powered by Python 3.11+ and `pywebview` (Edge WebView2 on Windows, GTK/QT WebKit on Linux when available).

## Installation

Core `agent-mesh` has **zero** third-party runtime dependencies:

```bash
pip install agent-mesh
```

To install the optional desktop UI extras (`pywebview`):

```bash
pip install "agent-mesh[desktop]"
```

### Windows WebView2 Runtime Prerequisite

On Windows, `pywebview` requires the **Microsoft Edge WebView2 Runtime** (`gui="edgechromium"`). Note that WebView2 is **not** pre-installed on all clean Windows Server (e.g. Windows Server 2022) or older Windows 10 installations.

- Before calling `webview.start`, `mesh.desktop.app` inspects the EdgeUpdate client registry key `{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` (`pv` non-empty and `!= 0.0.0.0`) under `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients`, `HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients`, and their `HKCU` equivalents.
- If the WebView2 runtime is missing, `mesh-desktop` fails fast with a clear `MeshError` containing the official installer link (`https://go.microsoft.com/fwlink/p/?LinkId=2124703`) instead of silently falling back to legacy MSHTML.

## Running & First-Run Flow (Chat-First)

Start via the dedicated entry point or CLI subcommand:

```bash
mesh-desktop
mesh-desktop --home ~/.agent-mesh

# Or via main CLI:
mesh desktop --home ~/.agent-mesh
```

`mesh-desktop` is packaged under `[project.gui-scripts]` so launching it on Windows does not open a console window; use `mesh desktop --check` (or `python -m mesh.desktop.app --check`) when you want console JSON output for asset verification:

```bash
mesh-desktop --check
mesh desktop --check
```

### First-Run Graphic Wizard (Zero Terminal Required)

1. **Initialize & Unlock (`initialize_home`)**: When `$MESH_HOME/mesh.db` does not exist yet, the desktop app opens a 2-step opaque wizard. Enter a passphrase (>= 12 characters) with confirmation; a single submit runs `init_mesh_home`, automatically starts `meshd`, and unlocks your session in memory. (CLI `mesh setup` / `mesh.exe setup` remains supported as an alternative.)
2. **Pick Your Lead (`detect_local_clis`)**: The wizard scans installed local CLIs (`claude`, `codex`, `agy`, `droid`, `kiro`, `vibe`; excluding launch-held `devin` and stubs `hermes`/`openclaw`), groups them into **Ready** vs. **Not installed / Sign-in needed** with one-click copyable install/login command hints (never executing arbitrary shell commands from WebView), and previews the exact agents to be created before you click **开始对话** (Start Chat):
   - Automatically creates `<cli>-lead` (`can_lead=true`, `default_profile="readonly"`) and a separate `<cli>-worker` (`can_lead=false`, adapter's safe `default_profile` such as `dev` for `codex` and `edit` for `claude`/`droid`/`agy`/`kiro`/`vibe`) plus workers for other detected CLIs that expose at least one resolvable model preset or discovered model (any skipped CLI is surfaced in a notification toast and can be configured later in **AI 工具**) — preserving the `LEAD_CANNOT_BE_ASSIGNEE` invariant without ever broadening profiles.
   - Enables `lead.vendor_leads=true` when a vendor CLI is chosen as Lead and sets `lead.default_agent=<cli>-lead`.
   - If no local vendor CLI is installed yet, you can still start immediately using the built-in Inbox Lead (`mesh-lead`). Messages sent to an Inbox Lead display an immediate queued status explaining that an external agent/script must pick them up (along with a shortcut to rescan and connect a local CLI, plus a "still waiting" reminder if no reply arrives).
3. **Chat -> Inline Approval Cards -> Live Progress**:
   - Type directly in the bottom composer (`chat.send`); on a new conversation, your first message automatically creates a chat-first project (`plan_gate_mode="require_approval"`, default `permission_mode="plan"`) and wakes the Lead immediately (`lead.chat_wake_debounce_s=0`). Blank new-chat screens also offer starter prompt chips and an optional Git repo drawer.
   - **Inline Opaque Approval Cards (Zero Verbal Approval / Zero Short Codes)**: Chat text **never** approves or rejects anything. Every approval (`plan_dispatch`, `step_dispatch`, `permission_escalation`, `config`, `retry_over_limit`, plus external `approval.request` cards) renders as an inline opaque card in the chat thread — and is mirrored in a **sticky pending-approval strip** above the composer with jump-to-card and open-worker-tab links — driving buttons strictly from `available_actions` (**允许** `allow`, **始终允许** `always_allow` only for internal gated ops hydrated with non-empty `always_allow_effect.scopes` for standing-grant categories, or when `always_allow_effect` is non-null and not already `auto` for internal `plan_dispatch`/`step_dispatch`, and **拒绝** `reject`, with optional reason preserved across live thread re-renders; `config` and `external.*` cards offer `allow` / `reject` only, and `external.*` cards display a distinct **外部请求** badge). Every button calls `approval.decide` bound strictly to the exact `plan_hash` / `op_hash` shown, signing a real `via="direct"` + `user_mac` record inside `meshd`.
   - **Per-Project Permission Modes & Standing Grants**:
     - Switch any project's permission mode from the project header or Settings (`project.set_permission_mode`): **每步问我 (`supervised`)**, **计划批了就放手 (`plan`, default)**, or **全自动 (`auto`)**.
     - Clicking **始终允许** on a `plan_dispatch` or `step_dispatch` card displays an explicit confirmation dialog explaining that it switches the project to **全自动 (`auto`)** (resending with `confirm_auto_mode: true`). Clicking **始终允许** on `permission_escalation` or `retry_over_limit` creates narrowly-scoped persistent standing grant(s) (every scope enumerated in `always_allow_effect.scopes`) inspectable and revocable in **设置 → 永久授权** (`grant.list` / `grant.revoke`). `config` cards never offer `always_allow`; `permission_escalation` is never auto-approved by `auto` mode alone.
   - **Multi-Agent Workspace Ergonomics**:
     - **Sidebar Working Section**: Running projects fold into a collapsible **运行中 · Working** section with indented live worker sub-rows and a 2-second exit grace timer; projects that finish or need approval bubble to the top of **需要你**.
     - **Closable Worker Tabs & Daemon-Side Grouped Activity (`run.activity`)**: Clicking any task or worker sub-row opens a closable worker tab in the Right Panel beside the parent thread with a live status bar and grouped tool-call activity summaries extracted server-side across all 6 adapters (`claude`, `codex`, `agy`, `droid`, `kiro`, `vibe`) with redaction and size caps (client-side JSONL parsing remains as fallback, alongside a **原始日志 (Raw)** toggle).
     - **Read-Only Git Diff Tab (`task.diff` / `project.diff`)**: Inspect bounded, redacted unified diffs and file trees for either the active code task's worktree or the project's integration branch (`mesh/<prj>/integration` vs `base_branch`) directly in the Right Panel **差异** tab (strictly read-only `gitops.py` `diff`/`status`/`log` — zero push/rebase/reset/fetch).
     - **Follow-Up Queue & Lead Turn Interrupt/Steer (`lead.interrupt`, `chat.queue_edit`, `chat.queue_cancel`)**: While a Lead turn is running, `Enter` queues follow-up messages (editable/cancellable only while unconsumed and non-approval, preserving immutable audit history via `chat.edited`/`chat.cancelled` events), **停止** cleanly interrupts a read-only Lead turn without breaking leases or worker runs, and `Ctrl+Enter` (**改向发送**) interrupts and immediately steers the next Lead turn.
     - **Composer Lead/Model Switch with Search & Passive CLI Auth Readiness**: Filter Lead agents and their `allowed_models` directly inside the composer popover (never exposing profiles or binaries) with a Worker model hint; Step 2 onboarding runs strictly passive CLI auth readiness probes (never spawning logins).

## Portable Windows build & Per-User Installer

### Portable ZIP (`scripts/build_windows_portable.py`)

`python scripts/build_windows_portable.py` (Linux or Windows; needs `pip install ziglang` or a MinGW-w64 gcc for the native launchers) produces in `dist/`:

- `agent_mesh-<ver>-py3-none-any.whl` - `pip install "agent_mesh-<ver>-py3-none-any.whl[desktop]"`
- `AgentMesh-<ver>-win-x64-portable.zip` - official CPython 3.12 embeddable + the wheel + pywebview/pythonnet + pinned official Git for Windows MinGit (`mingit\`, downloaded and SHA256-verified at build time, never at runtime) + native icon-embedded launchers (`Agent Mesh.exe`, `mesh.exe`) and `.cmd` fallbacks. Pass `--no-mingit` to build a smaller archive without MinGit.
- `SHA256SUMS.txt`

The launchers (`packaging/windows/launcher.c` + `packaging/windows/launcher.rc`) start `python\pythonw.exe -P -s -m mesh.desktop.app` / `python\python.exe -P -s -m mesh` directly with `_MESH_ORIG_PYTHONPATH` preserved and `PYTHONPATH` cleared (eliminating the Windows double-spawn hop in `mesh.cli.entry`), so `sys.executable` stays a real interpreter and the UI can spawn `meshd` cleanly.

**Bundled MinGit preference**: When `<app_dir>\mingit\cmd\git.exe` (downloaded and SHA-256 verified at build time) is installed alongside the portable/Inno bundle, `meshd` and `mesh.core.gitops.run_git` prefer the pinned bundled MinGit over arbitrary `PATH` entries (preventing `PATH` hijacking) and fall back to system `git` on `PATH` when no bundled MinGit is present. On Windows, `run_git` also sets `core.longpaths=true` per-process via `GIT_CONFIG_COUNT` without touching `~/.gitconfig`.

### Per-User Windows Installer (Inno Setup 6)

`packaging/windows/agent-mesh.iss` defines an unsigned, zero-cost, per-user Windows installer (`PrivilegesRequired=lowest`, no administrator rights or UAC prompt):

- Installs to `%LOCALAPPDATA%\Programs\Agent Mesh` (isolated from `%USERPROFILE%\.agent-mesh`).
- Creates a Start Menu shortcut (plus optional Desktop shortcut and optional `HKCU\Environment\Path` entry for `mesh.exe`).
- Checks the Microsoft Edge WebView2 Runtime registry key and offers to open the official Microsoft download page if missing.
- The uninstaller **keeps `%USERPROFILE%\.agent-mesh` user data by default**, deleting it only if the user explicitly confirms in the opt-in prompt (or passes `/PURGEDATA` during silent uninstall).

Build the installer (on Windows with Inno Setup 6, or on Linux with Wine + Inno Setup 6):

```bash
# Build portable zip + stage and compile setup.exe (or prepare helper scripts if ISCC is absent on Linux):
python3 scripts/build_windows_portable.py --out dist
python3 scripts/build_windows_installer.py --out dist --portable-zip dist/AgentMesh-1.0.0.dev0-win-x64-portable.zip

# On Linux without Inno Setup installed locally, the command above writes:
#   dist/installer-stage/          (ready payload)
#   dist/build-installer.ps1       (run on Windows with Inno Setup 6)
#   dist/build-installer-wine.sh   (run on Linux with Wine + Inno Setup 6)
```

### Scoop, Winget Manifests & PowerShell Installer (`scripts/gen_win_manifests.py`, `scripts/install.ps1`)

- **Manifest generator (`scripts/gen_win_manifests.py`)**: Generates and validates the Scoop manifest (`packaging/scoop/agent-mesh.json`) and Winget v1.6 manifests (`packaging/winget/AgentMesh.AgentMesh*.yaml`, covering both the per-user Inno Setup `.exe` and portable `.zip`) from `dist/SHA256SUMS.txt`:
  ```bash
  python3 scripts/gen_win_manifests.py --version 1.0.0 --sha256sums dist/SHA256SUMS.txt --out-dir packaging
  python3 scripts/gen_win_manifests.py --check --out-dir packaging              # strict: rejects 000...0 placeholders
  python3 scripts/gen_win_manifests.py --check --allow-placeholders --out-dir packaging  # checked-in templates only
  ```
- **Checksum-verified PowerShell installer (`scripts/install.ps1`)**: PowerShell 5.1+ per-user installer that downloads `SHA256SUMS.txt` and `AgentMesh-<ver>-win-x64-portable.zip`, verifies SHA-256 strictly before extraction, installs atomically under `%LOCALAPPDATA%\AgentMesh\versions\<ver>`, writes a `mesh.cmd` shim in `%USERPROFILE%\.local\bin` (refusing to overwrite foreign `mesh.exe` / `mesh.cmd` binaries), creates a Start Menu shortcut for `Agent Mesh.exe`, checks WebView2 availability, and leaves `%USERPROFILE%\.agent-mesh` untouched.

## Architecture

```text
+-------------------------------------------------------+
| Static UI (mesh/desktop/assets/)                      |
|   index.html + styles.css (opaque themes) + app.js    |
|   2-pane Chat shell (280px/88px sidebar + 690px chat) |
|   + toggleable Right Inspector (collapsed by default) |
+--------------------------^----------------------------+
                           | pywebview js_api + evaluate_js
+--------------------------v----------------------------+
| Python Bridge (`mesh/desktop/bridge.py` : `DesktopBridge` / `DesktopJsApi`) |
|   - Narrow JS API facade & allowed RPC whitelist (`ALLOWED_RPC_METHODS`)  |
|   - Direct user auth {"type": "user", "passphrase"}   |
|   - First-run helpers: home_status / initialize_home / detect_local_clis  |
|   - Background supervisor: auto-start + reconnect     |
|   - Background event.tail stream + JS push/poll queue |
|   - CLI spawn (`mesh daemon start`, secrets stripped) |
+--------------------------^----------------------------+
                           | Named Pipe (Win SID check) / Unix Socket
+--------------------------v----------------------------+
| meshd Daemon + SQLite State (Schema v2: chat & plans) |
+-------------------------------------------------------+
```

### Transport & Security Guarantees

- **Local IPC Only**: All daemon calls go through `mesh.client.MeshClient` over the OS-native local channel (`\\.\pipe\...` with current-user SID verification on Windows; Unix domain socket `$MESH_HOME/run/mesh.sock` on Linux). No TCP sockets or local HTTP servers are opened by the bridge.
- **No Direct DB/Log Reads**: The UI never opens `mesh.db` or run log files directly; all state comes from whitelisted RPCs (`chat.send`, `chat.list`, `chat.queue_cancel`, `chat.queue_edit`, `lead.interrupt`, `plan.list`, `plan.show`, `plan.cancel`, `project.*`, `task.*`, `approval.*`, `grant.*`, `agent.*`, `adapter.*`, `config.*`, `run.*`, `workspace.*`, `event.*`, `audit.verify`, `daemon.*`). Lead-internal RPCs (`chat.reply`, `plan.propose`, `plan.dispatch`) are not exposed to WebView JS.
- **Inline Approval Cards, Permission Modes & Standing Grants**:
  - Approvals happen strictly through inline approval cards in chat (`approval.decide` with `action`: `"allow"` / `"always_allow"` / `"reject"`, direct user session only). Chat text never approves or rejects anything.
  - Per-project permission modes (`project.set_permission_mode`, direct user only): `"supervised"` (every dispatched step asks via card), `"plan"` (default: approving the plan card runs all non-escalated steps), and `"auto"` (no plan approval; `retry_over_limit` auto-allowed). Mode changes are forward-only: leaving `"supervised"` (`"supervised"` -> `"plan"`/`"auto"`, via `project.set_permission_mode` or confirmed `always_allow`) sweep-cancels pending `step_dispatch` cards with audited `approval.cancelled` events (`cancelled_step_approvals` returned) while preserving `permission_escalation` cards; entering `"auto"` additionally sweep-cancels obsolete pending `plan_dispatch` cards (`cancelled_plan_approvals` returned) without cancelling the proposed plan so it can be dispatched directly under `"auto"`; entering `"supervised"` does not retroactively re-gate already-`assigned` tasks. Legacy projects (`plan_gate_mode="off"`) behave as before.
  - `"allow"` and `"always_allow"` require an active project (`PROJECT_NOT_ACTIVE` on terminal/inactive projects) and a bound task that is neither terminal (`INVALID_TRANSITION` with `reason: "task_terminal"`) nor progressed past the op's expected gating state (`INVALID_TRANSITION` with `reason: "task_progressed"`, e.g. a `task.assign` card whose task already left `created` or a `task.retry` card whose task already left `failed`/`escalated`). `"always_allow"` is restricted strictly to internal gated ops (`task.assign`/`task.create`/`task.reassign`/`task.retry`/`task.reject` for standing grants; `plan.dispatch`/`task.assign` for `plan_dispatch`/`step_dispatch` auto-mode switch) and creates a narrowly-scoped persistent standing grant (`permission_escalation` -> exact project + agent + target profile, never broader; `retry_over_limit` -> project) or confirms switching the project to `"auto"` mode for `plan_dispatch` / `step_dispatch` (`confirm_auto_mode=true`; if the project is already in `"auto"` mode, skips the switch prompt and returns `already_auto: true`, `mode_changed: false` with truthful copy). `config` writes and all `external.*` (`approval.request`) cards never offer `"always_allow"` (`available_actions: ["allow", "reject"]`, `always_allow_effect: null`), and `approval.request` rejects internal-only categories `plan_dispatch` and `step_dispatch` with `INVALID_ARGUMENT`. On `supervised` gated projects, escalated `task.assign` cards enforce `step_dispatch` (`direct_only`) regardless of `approval.policy.permission_escalation`, and clicking `"always_allow"` lets the lead's `task.assign --approval <apr>` consume that exact card cleanly in one step while minting the standing grant for future steps. `permission_escalation` is never auto-approved by mode alone. `always_allow_effect.scopes` enumerates every scope that will be created, and `approval.decide` refuses `always_allow` if the preview cannot describe all scopes. `chat.list` and `plan.show`/`plan.list` share the same approval card serializer (`serialize_approval_card_summary`): pending dead cards caused by an inactive project or a terminal/progressed bound task expose `available_actions: ["reject"]` (`dismiss_only: true`, `approvable: false`, `decidable: true`, `undecidable_reason: null`, `unapprovable_reason: "project_not_active" | "task_terminal" | "task_progressed"`) so the UI renders only a muted reason badge plus a dismiss/reject button; truly non-actionable cards (expired unswept, terminal/non-proposed plan, already-`approved` dead cards, terminal statuses, or direct-`approved` cards with no supported `always_allow`) return `available_actions: []` (`decidable: false`, `dismiss_only: false`, `undecidable_reason` populated). Card messages do not freeze static `meta.actions` at emit time; `chat.list` hydrates each card's `item.meta.actions` directly from `approvals_by_id[aid].available_actions` (or `[]` when no approval is attached) so it never diverges. Deciding an expired card immediately persists `status="expired"` and cancels any bound plan before returning `APPROVAL_EXPIRED`.
  - Clicking `"allow"` on a relayed-approved card upgrades it to `direct_verified` (computing `user_mac` and emitting `approval.decided` with `upgraded_from: "relayed"`), and clicking `"reject"` on a relayed-rejected card (`available_actions: ["reject"]`) likewise upgrades it to a direct user-signed rejection (`direct_verified`, `upgraded_from: "relayed"`). Clicking `"always_allow"` on an unconsumed, unexpired `approved` card keeps the decision `approved` and creates the standing grant(s) (or switches to `"auto"` on confirmation). Agents and leads cannot retract an approved user-signed plan or approval; only pending ones, or the direct user, can cancel them.
  - Standing grants are inspected and revoked via `grant.list` and `grant.revoke` (direct user only); every card click, mode change, and grant creation/use/revocation is recorded in the hash-chained event log with `user_mac` signatures verified cryptographically by `audit.verify`, which also verifies `projects.plan_gate_mode` and `projects.permission_mode` history (detecting live or tamper-and-restore bypasses) and surfaces structured mismatch details in the desktop audit modal (runtime agent paths do not hold `k_user`, so runtime verification checks `user_mac` presence and event-row correspondence while `audit.verify` checks HMACs and hash-chain integrity). Note on lead changes: single-use approvals bind `requested_by` and must be re-requested by a new lead, whereas project-scoped standing grants survive lead changes until explicitly revoked via `grant.revoke`.
  - Relayed `chat.send` requires `relay` capability, project origin (`origin_principal_id`), current project readability with an active enabled lead, and that the relayer is neither the current lead nor a removed/former lead of the project; relayed messages are visibly marked (`relayed: true`, `via: "relayed"`, `relayed_by`, `relayed_by_name`) in `chat.list`, `chat.send`, and lead prompts (`[RELAYED via ...]` with JSON-escaped single-line bodies and sanitized headers), HTML-escaped with sanitized single-line relayed badges in the desktop transcript, cannot be edited/cancelled via `chat.queue_edit`/`chat.queue_cancel`, and carry zero approval authority. `chat.reply` is strictly for acting project leads (`role="lead"`) and rejects direct user callers (desktop UI uses `chat.send`).
- **In-Memory Passphrase (Default) & Opt-In Windows Credential Vault**: By default, the user passphrase (`{"type": "user", "passphrase": ...}`) is held exclusively in `DesktopBridge` memory, verified via `whoami`, redacted from errors, never written to `localStorage`, argv, env, or plaintext disk files, and cleared on lock or GUI quit. On Windows, users may optionally opt in (unchecked by default) to store the unlock passphrase in Windows Credential Manager (`mesh/platform/_win32_cred.py`, `CredWriteW` / `CredReadW` / `CredDeleteW`, local-machine DPAPI-encrypted per Windows user, with an explicit same-user trust-boundary warning and one-click wipe via "锁定并清除"; automatically wiped if `meshd` rejects a stale entry). `initialize_home(passphrase)` is strictly refused whenever `$MESH_HOME/mesh.db` already exists. `DesktopJsApi` uses a sealed closure-based facade (`__slots__ = ()` and `__getattribute__` filtering) so `pywebview` dotted-path JS resolution cannot reach `DesktopBridge`, `_passphrase`, or lifecycle methods.
- **Native Folder Picker & Read-Only Repo Inspection**: `pick_folder()` uses `pywebview`'s native OS folder dialog and `inspect_repo_folder(path)` inspects Git repositories strictly via allow-listed read-only `run_git` subcommands (`rev-parse`, `show-ref`), detects common test command candidates (`pytest`, `npm test`, `cargo test`, `go test ./...`) without executing repository code, defaults `test_command` to `"none"`, aligns subdirectories to Git toplevel on user click, and pre-checks `MESH_HOME` / install-root path overlaps.
- **System Tray, Single Instance & Non-Sensitive Window State**: On Windows, `mesh/desktop/tray_win.py` provides a zero-dependency Win32 `Shell_NotifyIconW` tray icon and per-home single-instance mutex (`Local\AgentMeshDesktop-<hash>`). Closing the window hides to the tray by default (keeping the in-memory session and background `meshd` alive) and persists non-sensitive window bounds (`%LOCALAPPDATA%\agent-mesh\desktop_state.json`) and layout preferences (`localStorage`, never credentials). Quitting the desktop app stops **nothing** in `meshd` unless the user explicitly chooses "退出并停止后台服务" (which still uses the authenticated `daemon.stop` RPC).
- **Content Security Policy & Command Guards**: `index.html` enforces a strict `<meta http-equiv="Content-Security-Policy">` (`default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'`) with zero inline scripts or inline `on*` handlers. The bridge rejects executable overrides (`binary`, `binary_path`, `notify.command`) from WebView JS callers and `detect_local_clis()` accepts no caller path arguments. Repo `test_command` (an argv array executed directly by `meshd` during merge gates without a shell) displays an explicit confirmation dialog showing the exact `argv` before submission.
- **Disconnected Read-Only Safety**: When `meshd` disconnects or the passphrase is locked, all mutating controls are disabled while an exponential backoff supervisor probes health, catches up missed events via `event.list(since=last_seq)`, and restarts `event.tail`.
- **Daemon Lifecycle & Auto-Start**: On launch and whenever `meshd` becomes unreachable later, if `$MESH_HOME/mesh.db` exists, `meshd.lock` is not held by a busy/draining process, and `meshd` is unreachable (`DAEMON_UNAVAILABLE`), the supervisor automatically runs `mesh daemon start` under a single-flight lock (capped at 3 attempts per 5-minute window to avoid crash loops) with secret environment variables stripped (`MESH_PASSPHRASE`, `MESH_PASSPHRASE_FILE`, `MESH_TOKEN`) and `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows so no console window appears even inside virtual environments. If `$MESH_HOME/run/meshd.lock` is still held while `meshd` is busy or draining, the supervisor does not spawn a second daemon and retries once the lock is released or `meshd` responds again. If `$MESH_HOME` is uninitialized, the UI opens the first-run setup wizard (displaying the exact CLI `setup` command for the current home/portable install as an optional fallback) without spawning `meshd` prematurely. Start errors and draining states are displayed as persistent bars with an in-memory diagnostics modal (never reading log files directly). Explicitly stopping `meshd` via the UI (`stop_daemon` / `daemon.stop`) or CLI (`mesh daemon stop`) writes `$MESH_HOME/run/autostart.hold` and suppresses automatic restarts across open desktop windows until the user starts `meshd` again, a different daemon instance comes online externally, or the desktop app is freshly launched. Closing the desktop window only exits the GUI client (clearing in-memory passphrases and closing IPC streams); `meshd` continues running in the background so active agent runs are not interrupted.
- **Rotating Diagnostic Logs (`meshd.log` & `desktop.log`)**: Alongside the daemon log (`$MESH_HOME/logs/meshd.log`), the desktop process and `DesktopBridge` write a rotating log file at `$MESH_HOME/logs/desktop.log` (10 MiB rotation keeping 4 archives, private `0600` permissions / Windows private ACL, symlink refusal, control-character escaping, and secret redaction via `mesh.core.logfile`). Passphrases, auth objects, and raw secret values are never written to `desktop.log`. Both log file paths are surfaced in the **"查看日志" (View Log)** diagnostics modal.
- **Agent Configuration Guardrails**: Create and Edit Agent dialogs populate `default_profile` strictly from each adapter's `supported_profiles` (returned by `adapter.list`/`adapter.show`), default to the adapter's safe profile (e.g. `dev` for `codex`, `edit` for `claude`/`droid`/`agy`/`kiro`/`vibe`, never `elevated` or `unrestricted`), read per-adapter `model_presets` from the daemon when `adapter.models` returns `null`, fold unavailable/held/stub adapters (`devin`, `hermes`, `openclaw`) into an opt-in advanced group, and require an explicit confirmation dialog naming the vendor CLI flag before saving a `default_profile` above `max_unapproved_profile`.

## Visual Design & Assets

- UI source assets live under `mesh/desktop/assets/` (`index.html`, `styles.css`, `app.js`), implementing a chat-first 2-pane primary shell (resizable `280px` project chat sidebar collapsible to an `88px` icon rail + centered `690px` transcript and `700px` composer dock) with a toggleable right inspector (`340px`, collapsed by default).
- **100% Opaque High-Contrast Surfaces**: All text surfaces, cards, bubbles, popovers, wizard panes, and modals use solid 6-digit hex colors in both Dark and Light themes — zero alpha fills, zero frosted glass filters, and zero see-through stacked cards (enforced by static unit tests).

### CI Verification Note

- Windows CI currently verifies static desktop assets and packaging metadata via `python -m mesh.desktop.app --check` without installing the optional `[desktop]` extra. As a follow-up CI enhancement, consider adding a smoke step on Windows that installs `agent-mesh[desktop]` (`pywebview>=6.0,<7`) and verifies `import webview` / window-creation startup.

## Capability Gates

`CAPABILITIES` (`mesh/desktop/bridge.py`) declares which desktop features have dedicated backend RPCs:

| Capability Key | Status | Desktop UI Behavior |
|---|---|---|
| `project_conversation` | `true` | Main stage renders structured project chat via `chat.send` / `chat.list` + live `event.tail` updates. |
| `confirm_plan` | `true` | Inline opaque Plan Cards and sticky pending-approval strips render structured Lead plans (`plan.list` / `plan.show`) with `允许 / 始终允许 / 拒绝` buttons (`approval.decide`) and nested live task progress. |
| `task_diff` | `true` | Right Inspector **差异 (Diff)** tab renders bounded, redacted read-only Git diffs and file trees via `task.diff` and `project.diff` (zero push/rebase/reset). |
| `task_log_tail` | `false` | While viewing logs of an active `running`/`merging` task in the Right Inspector, UI polls `task.log` every 2s. |
| `approval_policy_inline` | `true` | Daemon serves inline approval card protocol (`chat.list`, `approval.decide`, `grant.*`, `project.set_permission_mode`) with per-card `policy`, `available_actions`, and `always_allow_effect`. |
| `task_list_latest_run` | `false` | Live duration ticker joins `task.list` with `task.show.live_run` and `run.list` timestamps. |
