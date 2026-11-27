(function () {
  "use strict";

  const STORAGE_KEY = "mesh.ui.lang";
  const VALID_PREFS = ["auto", "en", "zh-CN"];

  function normalizePref(raw) {
    if (typeof raw !== "string") return null;
    const s = raw.trim();
    if (!s) return null;
    const low = s.toLowerCase().replace(/_/g, "-");
    if (low === "auto") return "auto";
    if (low === "en" || low.startsWith("en-")) return "en";
    if (low === "zh" || low.startsWith("zh-")) return "zh-CN";
    return null;
  }

  function detectRuntimeLang() {
    const isNode = typeof process !== "undefined" && Boolean(process && process.versions && process.versions.node);
    if (isNode) {
      if (process.env && process.env.MESH_UI_LANG) {
        const envNorm = normalizePref(process.env.MESH_UI_LANG);
        if (envNorm === "en" || envNorm === "zh-CN") return envNorm;
      }
      if (typeof window !== "undefined" && window && window.navigator) {
        const nav = window.navigator;
        const candidates = [];
        if (Array.isArray(nav.languages)) {
          for (const item of nav.languages) {
            if (typeof item === "string" && item.trim()) candidates.push(item.trim());
          }
        }
        if (typeof nav.language === "string" && nav.language.trim()) {
          candidates.push(nav.language.trim());
        }
        if (candidates.length > 0) {
          const first = candidates[0].toLowerCase().replace(/_/g, "-");
          return first.startsWith("zh") ? "zh-CN" : "en";
        }
      }
      // Headless Node environment without explicit MESH_UI_LANG / window.navigator defaults to zh-CN
      return "zh-CN";
    }
    if (typeof navigator !== "undefined" && navigator) {
      const candidates = [];
      if (Array.isArray(navigator.languages)) {
        for (const item of navigator.languages) {
          if (typeof item === "string" && item.trim()) candidates.push(item.trim());
        }
      }
      if (typeof navigator.language === "string" && navigator.language.trim()) {
        candidates.push(navigator.language.trim());
      }
      if (candidates.length > 0) {
        const first = candidates[0].toLowerCase().replace(/_/g, "-");
        return first.startsWith("zh") ? "zh-CN" : "en";
      }
      return "en";
    }
    return "zh-CN";
  }

  function loadStoredPref() {
    try {
      if (typeof localStorage !== "undefined" && localStorage) {
        const saved = normalizePref(localStorage.getItem(STORAGE_KEY));
        if (saved) return saved;
      }
    } catch (_err) {
      // ignore storage restrictions
    }
    return "auto";
  }

  function saveStoredPref(pref) {
    try {
      if (typeof localStorage !== "undefined" && localStorage) {
        localStorage.setItem(STORAGE_KEY, pref);
      }
    } catch (_err) {
      // ignore storage restrictions
    }
  }

  let currentPref = loadStoredPref();
  let systemLangOverride = null;

  function setSystemLang(lang) {
    const norm = normalizePref(lang);
    systemLangOverride = (norm === "en" || norm === "zh-CN") ? norm : null;
    return getLang();
  }

  function resolveLang(pref = currentPref) {
    const norm = normalizePref(pref);
    if (norm === "en" || norm === "zh-CN") return norm;
    if (systemLangOverride === "en" || systemLangOverride === "zh-CN") {
      return systemLangOverride;
    }
    return detectRuntimeLang();
  }

  function getPref() {
    return currentPref;
  }

  function getLang() {
    return resolveLang(currentPref);
  }

  function isEn() {
    return getLang() === "en";
  }

  function setPref(pref, { persist = true } = {}) {
    const norm = normalizePref(pref) || "auto";
    currentPref = norm;
    if (persist) saveStoredPref(norm);
    return getLang();
  }

  function toggleLang({ persist = true } = {}) {
    const next = isEn() ? "zh-CN" : "en";
    return setPref(next, { persist });
  }

  function tr(zh, en) {
    return isEn() ? en : zh;
  }

  // Static DOM strings for index.html (strictly textContent / attribute values, zero innerHTML)
  const DOM_STRINGS = {
    "titlebar.theme": { zh: "切换深浅主题", en: "Toggle dark / light theme" },
    "titlebar.langTitle": { zh: "切换界面语言 (中文 / English)", en: "Switch UI language (English / 中文)" },
    "titlebar.langBtn": { zh: "EN", en: "中文" },
    "titlebar.min": { zh: "最小化", en: "Minimize" },
    "titlebar.max": { zh: "最大化 / 还原", en: "Maximize / Restore" },
    "titlebar.maxAria": { zh: "最大化或还原", en: "Maximize or restore" },
    "titlebar.close": { zh: "关闭窗口", en: "Close window" },

    "sidebar.collapseTitle": { zh: "收起 / 展开侧边栏 (Ctrl+B)", en: "Collapse / Expand sidebar (Ctrl+B)" },
    "sidebar.searchTitle": { zh: "搜索项目对话", en: "Search project chats" },
    "sidebar.newChatTitle": { zh: "新建项目对话", en: "New project chat" },
    "sidebar.searchPlaceholder": { zh: "搜索项目名称或 Lead...", en: "Search project name or Lead..." },
    "sidebar.searchAria": { zh: "搜索项目名称或 Lead", en: "Search project name or Lead" },
    "sidebar.navChatsTitle": { zh: "项目对话", en: "Project chats" },
    "sidebar.navChats": { zh: "对话", en: "Chats" },
    "sidebar.navCliTitle": { zh: "高级：AI 工具（CLIs）与 Agent 管理", en: "Advanced: AI Tools (CLIs) & Agents" },
    "sidebar.navCli": { zh: "AI 工具", en: "AI Tools" },
    "sidebar.navSettingsTitle": { zh: "系统设置与诊断", en: "Settings & Diagnostics" },
    "sidebar.navSettings": { zh: "设置", en: "Settings" },
    "sidebar.daemonMenuTitle": { zh: "当前账号与后台服务状态", en: "Account & daemon status" },
    "sidebar.lockedUser": { zh: "未解锁", en: "Locked" },
    "sidebar.checkingDaemon": { zh: "检测后台服务...", en: "Checking daemon..." },
    "sidebar.daemonMenuDotsTitle": { zh: "后台服务菜单", en: "Daemon menu" },

    "header.permModeTitle": { zh: "切换当前项目权限模式", en: "Switch project permission mode" },
    "header.permSupervised": { zh: "每步问我 (supervised)", en: "Ask every step (supervised)" },
    "header.permPlan": { zh: "计划批了就放手 (plan)", en: "Approve plan, run steps (plan)" },
    "header.permAuto": { zh: "全自动 (auto)", en: "Fully automatic (auto)" },
    "header.deliverBaseTitle": { zh: "将集成分支本地快进合并至基准分支", en: "Fast-forward merge integration branch into base branch locally" },
    "header.deliverBase": { zh: "交付到本地分支", en: "Deliver to Base" },
    "header.projectActionsTitle": { zh: "项目操作与高级菜单", en: "Project actions & menu" },
    "header.toggleInspectorTitle": { zh: "展开 / 收起右侧检查器与高级面板", en: "Toggle right inspector panel" },

    "daemon.disconnected": { zh: "后台服务已断开 — 当前为只读查看模式", en: "Daemon disconnected — read-only mode" },
    "daemon.viewLogTitle": { zh: "查看启动诊断与日志路径", en: "View startup diagnostics and log paths" },
    "daemon.viewLog": { zh: "查看日志", en: "View Logs" },
    "daemon.reconnectTitle": { zh: "立即尝试重新连接", en: "Retry connection now" },
    "daemon.reconnect": { zh: "重试连接", en: "Reconnect" },
    "daemon.startTitle": { zh: "启动本地后台服务", en: "Start local daemon" },
    "daemon.start": { zh: "启动服务", en: "Start Daemon" },

    "auth.unlockMsg": { zh: "请输入口令解锁工作台（口令仅保存在当前进程内存中）", en: "Enter passphrase to unlock workspace (held in memory only)" },
    "auth.passPlaceholder": { zh: "输入用户口令...", en: "Enter passphrase..." },
    "auth.rememberTitle": {
      zh: "默认关闭（仅保存在内存中）；勾选后由 Windows 凭据管理器（DPAPI）托管，同一 Windows 用户下的其他进程可读取",
      en: "Off by default (in-memory only); when enabled, stored in Windows Credential Manager (DPAPI), readable by other processes under the same Windows user"
    },
    "auth.rememberLabel": { zh: "记住本机解锁", en: "Remember on this PC" },
    "auth.unlockBtn": { zh: "解锁", en: "Unlock" },
    "auth.rememberWarnStrong": { zh: "安全边界提示：", en: "Trust boundary notice: " },
    "auth.rememberWarnBody": {
      zh: "勾选「记住本机解锁」后，口令将由 Windows 凭据管理器（DPAPI）按当前 Windows 用户加密托管（不写入明文文件、环境变量或 localStorage）；但同一 Windows 用户账户下运行的其他进程（含本机 Agent）可以读取该凭据，运行不可信 Agent 时请勿启用。",
      en: "Enabling \"Remember on this PC\" stores your passphrase in Windows Credential Manager (DPAPI, encrypted per Windows user — never plaintext files, env vars, or localStorage). However, other processes running as the same Windows user (including local agents) can read this credential. Do not enable when running untrusted agents."
    },

    "repo.drawerTitle": { zh: "绑定本地 Git 代码仓库（可选，留空则为通用对话项目）", en: "Attach local Git repository (optional — leave empty for a general chat project)" },
    "repo.closeTitle": { zh: "收起代码仓库设置", en: "Close repository settings" },
    "repo.pathPlaceholder": { zh: "点击右侧选择文件夹，或粘贴本地仓库绝对路径...", en: "Click Browse to pick a folder, or paste an absolute local repo path..." },
    "repo.pickTitle": { zh: "打开系统文件夹选择器并自动识别分支与测试命令", en: "Open OS folder picker and auto-detect branches & test commands" },
    "repo.pickBtn": { zh: "选择文件夹...", en: "Browse..." },
    "repo.inspectTitle": { zh: "重新检测所填路径的 Git 分支与测试命令", en: "Re-inspect Git branches and test commands for this path" },
    "repo.inspectBtn": { zh: "检测", en: "Inspect" },
    "repo.baseBranchLabel": { zh: "基准分支（已自动识别，可切换）", en: "Base branch (auto-detected, editable)" },
    "repo.baseBranchPlaceholder": { zh: "选择仓库后自动识别", en: "Auto-detected after selecting repo" },
    "repo.testCmdLabel": { zh: "自动测试命令（默认 none，可一键选择）", en: "Test command (default: none)" },
    "repo.testCmdPlaceholder": { zh: 'none 或 ["pytest"]', en: 'none or ["pytest"]' },

    "composer.aria": { zh: "输入消息给 Lead", en: "Message the Lead" },
    "composer.placeholder": { zh: "告诉 Lead 你想完成什么... (Enter 发送，Shift+Enter 换行)", en: "Tell your Lead what to build... (Enter to send, Shift+Enter for newline)" },
    "composer.repoBtnTitle": { zh: "绑定或查看本地代码仓库", en: "Attach or view local Git repository" },
    "composer.queueBadge": { zh: "排队发送 · 等待当前回合完成", en: "Queued · waiting for current turn" },
    "composer.leadPillTitle": { zh: "查看或切换当前项目 Lead", en: "View or switch project Lead" },
    "composer.steerTitle": { zh: "打断当前只读 Lead 回合并立即按新消息改向 (Ctrl+Enter)", en: "Interrupt current read-only Lead turn and steer immediately (Ctrl+Enter)" },
    "composer.steerBtn": { zh: "改向发送", en: "Steer" },
    "composer.stopTitle": { zh: "停止当前运行中的 Lead 回合", en: "Stop current running Lead turn" },
    "composer.stopBtn": { zh: "停止", en: "Stop" },
    "composer.sendTitle": { zh: "发送消息 (Enter)", en: "Send message (Enter)" },

    "inspector.tabTask": { zh: "任务详情", en: "Task" },
    "inspector.tabDiff": { zh: "差异", en: "Diff" },
    "inspector.tabOutline": { zh: "执行大纲", en: "Outline" },
    "inspector.tabRunning": { zh: "运行中", en: "Running" },
    "inspector.tabAdvanced": { zh: "高级", en: "Advanced" },

    "wizard.title": { zh: "欢迎使用 Agent Mesh", en: "Welcome to Agent Mesh" },
    "wizard.subtitle": { zh: "几秒钟完成本机初始化，选一个 Lead 就能开始对话协作", en: "Initialize locally in seconds, pick a Lead, and start collaborating" },
    "wizard.step1Pill": { zh: "1. 设置口令", en: "1. Set Passphrase" },
    "wizard.step2Pill": { zh: "2. 选择你的 Lead", en: "2. Choose Your Lead" },
    "wizard.passTitle": { zh: "本地安全口令", en: "Local Security Passphrase" },
    "wizard.passDesc": {
      zh: "Agent Mesh 仅在本机运行，所有审批签名均依赖你的口令。口令只在当前进程内存中保存，绝不写入磁盘、环境变量或浏览器存储。",
      en: "Agent Mesh runs strictly locally, and every approval signature is bound to your passphrase. By default it is kept in process memory only — never written to disk, env vars, or browser storage."
    },
    "wizard.passInputLabel": { zh: "设置口令（至少 12 个字符）", en: "Set passphrase (at least 12 characters)" },
    "wizard.passInputPlaceholder": { zh: "输入至少 12 位安全口令...", en: "Enter a passphrase of at least 12 characters..." },
    "wizard.passLenZero": { zh: "当前长度：0 / 12", en: "Length: 0 / 12" },
    "wizard.passConfirmLabel": { zh: "再次确认口令", en: "Confirm passphrase" },
    "wizard.passConfirmPlaceholder": { zh: "再次输入同一口令...", en: "Re-enter the same passphrase..." },
    "wizard.recheckBtn": { zh: "重新检测", en: "Recheck" },
    "wizard.doctorBtn": { zh: "环境诊断", en: "Diagnostics" },
    "wizard.initBtn": { zh: "初始化并继续", en: "Initialize & Continue" },
    "wizard.leadTitle": { zh: "选择你的项目总管（Lead）", en: "Choose Your Project Lead" },
    "wizard.leadDescPre": {
      zh: "在对话中由它制定计划，经你在审批卡片上点击确认后派发任务；系统会自动为你配好独立执行者（Worker）。选择本机 AI CLI 作为 Lead 时，将自动开启本机 CLI Lead 调度（",
      en: "Your Lead plans work in chat and dispatches tasks after you approve the inline plan card; a separate Worker is paired automatically. Choosing a local AI CLI as Lead enables local CLI Lead execution ("
    },
    "wizard.leadDescPost": { zh: "）。", en: ")." },
    "wizard.rescanBtn": { zh: "重新扫描", en: "Rescan" },
    "wizard.skipBtn": { zh: "跳过，稍后配置", en: "Skip for now" },
    "wizard.finishBtn": { zh: "开始对话", en: "Start Chatting" },

    "modal.defaultTitle": { zh: "详情", en: "Details" },
    "modal.closeTitle": { zh: "关闭", en: "Close" },
    "confirmModal.defaultTitle": { zh: "确认操作", en: "Confirm Action" }
  };

  const APPROVAL_POLICY_LABELS_EN = {
    config: "System configuration changes (local direct confirmation only)",
    lead_change: "Change or hand off project Lead",
    project_control: "Project start, pause, or cancellation",
    scope_change: "Project goal & acceptance criteria changes",
    permission_escalation: "Task permission profile escalation",
    retry_over_limit: "Retry beyond retry limit",
    delete: "Delete internal workspace or resource",
    deliver_to_base: "Deliver to local base branch",
    spend_money: "External paid operation",
    deploy: "External deployment operation",
    delete_external: "Delete external resource",
    external_other: "Other external side-effect operation",
    plan_dispatch: "Plan dispatch approval (local direct confirmation only)",
    step_dispatch: "Step dispatch approval (local direct confirmation only)"
  };

  const GATE_CATEGORY_LABELS_EN = {
    permission_escalation: "Permission Escalation",
    config: "Configuration Change",
    retry_over_limit: "Retry Over Limit",
    plan_dispatch: "Plan Dispatch Approval",
    step_dispatch: "Step Dispatch Approval",
    scope_change: "Scope Change",
    project_control: "Project Control",
    lead_change: "Project Lead Change",
    delete: "Delete Resource",
    deliver_to_base: "Deliver to Base Branch",
    spend_money: "External Paid Action",
    deploy: "External Deployment",
    delete_external: "Delete External Resource",
    external_other: "External Action Approval"
  };

  const PERMISSION_MODE_META_EN = {
    supervised: {
      label: "Ask every step (supervised)",
      shortLabel: "Ask every step",
      desc: "Plan cards require approval, and every dispatched step also prompts for approval before running"
    },
    plan: {
      label: "Approve plan, run steps (plan)",
      shortLabel: "Approve plan",
      desc: "Default: once you approve the plan card, non-escalated steps run automatically; escalations still require approval"
    },
    auto: {
      label: "Fully automatic (auto)",
      shortLabel: "Auto",
      desc: "Skips plan approval and auto-allows over-limit retries; permission_escalation still requires explicit card approval"
    }
  };

  const TASK_STATE_LABELS_EN = {
    created: "Awaiting approval / Unassigned",
    assigned: "Assigned",
    queued: "Queued",
    running: "Running",
    cancelling: "Cancelling",
    submitted: "Awaiting review",
    rework: "Needs rework",
    accepted: "Accepted",
    merging: "Merging",
    merge_failed: "Merge failed",
    merged: "Merged",
    failed: "Failed",
    escalated: "Escalated",
    cancelled: "Cancelled"
  };

  const PLAN_STATUS_LABELS_EN = {
    proposed: "Needs approval",
    approved_pending_dispatch: "Approved · dispatching",
    dispatched: "In progress",
    revising: "Revising",
    superseded: "Superseded",
    rejected: "Rejected",
    cancelled: "Cancelled"
  };

  const ERROR_LABELS_EN = {
    TOKEN_MISSING: "Enter your passphrase to unlock the workspace first",
    PASSPHRASE_INVALID: "Incorrect passphrase — please try again (5 consecutive failures trigger a temporary lockout)",
    PASSPHRASE_REQUIRED: "User passphrase is required to continue",
    TOKEN_INVALID: "Session is invalid or expired — please unlock again",
    LOCKED_OUT: "Too many failed passphrase attempts — account temporarily locked",
    DAEMON_UNAVAILABLE: "Cannot connect to daemon — start the service or wait for auto-reconnect",
    DAEMON_ALREADY_RUNNING: "Daemon is already running or finishing shutdown",
    HOME_NOT_INITIALIZED: "Data directory is not initialized — set a passphrase in the wizard or run mesh setup",
    ALREADY_EXISTS: "Target already exists",
    NO_LEAD: "No default Lead configured — choose a Lead in the wizard or AI Tools",
    LEAD_NOT_ELIGIBLE: "Selected agent cannot act as Lead (must have can_lead enabled and be active)",
    LEAD_HAS_TASKS: "Selected agent has unfinished tasks in this project and cannot simultaneously be Lead",
    ADAPTER_UNSUPPORTED: "Selected AI tool cannot be launched as Lead — check tool status",
    PLAN_APPROVAL_REQUIRED: "Click \"Allow\" on the plan card before dispatching tasks",
    CHANNEL_NOT_ALLOWED: "This action requires direct local confirmation with your passphrase",
    SELF_APPROVAL: "An approval requester cannot approve their own request",
    NOT_PROJECT_LEAD: "Current principal is not the project Lead",
    CAPABILITY_MISSING: "Missing required capability for this action",
    OP_HASH_MISMATCH: "Approval digest changed — refresh and review the latest card before deciding",
    APPROVAL_EXPIRED: "This approval request has expired",
    APPROVAL_REJECTED: "This approval request has already been rejected",
    APPROVAL_CONSUMED: "This approval request has already been consumed",
    APPROVAL_CANCELLED: "This approval request has been cancelled",
    PROJECT_NOT_ACTIVE: "Project is not active (paused, completed, or cancelled), so this action cannot run",
    DECISION_CONFLICT: "This approval card already has a final decision — refresh to view latest status",
    APPROVAL_REQUIRED: "This action is gated — use the inline approval card buttons (Allow / Always allow / Reject)",
    AUDIT_MISMATCH: "Audit verification failed: hash-chain or project/approval state mismatch detected",
    MERGE_CONFLICT: "Git merge conflict — resolve it and retry merging",
    INVALID_ARGUMENT: "Invalid input argument — check values and retry",
    INVALID_TRANSITION: "This action is not allowed in the current state",
    CONCURRENCY_LIMIT: "Another concurrent operation is in progress — please retry shortly"
  };

  const PROFILE_LABELS_EN = {
    readonly: "readonly (Read-only)",
    edit: "edit (Workspace edit)",
    dev: "dev (Workspace dev & commands)",
    elevated: "elevated (High privilege · Lead-dispatched tasks require approval; direct user tasks do not)",
    unrestricted: "unrestricted (Full access · Lead-dispatched tasks require approval; direct user tasks do not)"
  };

  const ACTIVITY_KIND_LABELS_EN = {
    read: "Read",
    edit: "Edit",
    command: "Command",
    search: "Search",
    "mesh-action": "Action",
    thought: "Thought",
    other: "Tool"
  };

  const VENDOR_AUTH_HINTS_EN = {
    agy: { install: "See the official Antigravity CLI installation guide", login: "agy login" }
  };

  const ADAPTER_MODEL_NOTES_EN = {
    vibe: "Vibe 3.1.0 includes the built-in alias mistral-medium-3.5; other model names must match [[models]] aliases in ~/.vibe/config.toml (inspect with /model in vibe).",
    codex: "Mesh does not fetch Codex model lists automatically; available models depend on your account plan (gpt-5.6-luna is smoke-tested but may not be enabled for all plans). Pick a preset chip or enter any model ID.",
    devin: "Devin CLI is currently on M5 launch hold and only supports unrestricted; confirm carefully before selecting or entering a model ID.",
    claude: "Claude Code supports short aliases (sonnet/opus/haiku) or full model IDs. Click a preset chip or type any model ID."
  };

  function localizeAdapterModelNote(adapterId, rawNote) {
    const note = String(rawNote || "").trim();
    if (!note) return "";
    if (isEn() && adapterId && ADAPTER_MODEL_NOTES_EN[adapterId]) {
      return ADAPTER_MODEL_NOTES_EN[adapterId];
    }
    return note;
  }

  function localizeDaemonCardBody(apr, rawBody) {
    const body = String(rawBody || "").trim();
    if (!isEn() || !body) return body;

    // Match standard daemon boilerplate bodies from mesh/core/approvals.py & mesh/daemon/handlers/plan.py
    let m;
    if ((m = body.match(/^外部请求确认\s*\[([^\]]+)\]：([\s\S]*?)。请使用卡片按钮（允许\s*\/\s*拒绝）决定。$/))) {
      return `External approval request [${m[1]}]: ${m[2]}. Use the card buttons (Allow / Reject) to decide.`;
    }
    if ((m = body.match(/^需要提权确认：步骤\s*'([^']+)'\s*\(([\s\S]*?)\)\s*请求为\s*'([^']+)'\s*提升权限至\s*'([^']+)'。请使用卡片按钮（允许\s*\/\s*始终允许\s*\/\s*拒绝）决定。$/))) {
      return `Permission escalation required: step '${m[1]}' (${m[2]}) requests escalating '${m[3]}' to profile '${m[4]}'. Use the card buttons (Allow / Always allow / Reject) to decide.`;
    }
    if ((m = body.match(/^单步执行确认：步骤\s*'([^']+)'\s*\(([\s\S]*?)\)\s*将由\s*'([^']+)'\s*以\s*'([^']+)'\s*执行。请使用卡片按钮（允许\s*\/\s*始终允许\s*\/\s*拒绝）决定。$/))) {
      return `Step dispatch confirmation: step '${m[1]}' (${m[2]}) will run on '${m[3]}' with profile '${m[4]}'. Use the card buttons (Allow / Always allow / Reject) to decide.`;
    }
    if ((m = body.match(/^需要提权确认：([\s\S]*?)。请使用卡片按钮（允许\s*\/\s*始终允许\s*\/\s*拒绝）决定。$/))) {
      return `Permission escalation required: ${m[1]}. Use the card buttons (Allow / Always allow / Reject) to decide.`;
    }
    if ((m = body.match(/^需要配置变更确认：([\s\S]*?)。配置写入仅限用户直接确认，请使用卡片按钮（允许\s*\/\s*拒绝）决定。$/))) {
      return `Configuration change confirmation: ${m[1]}. Configuration writes require direct user confirmation — use the card buttons (Allow / Reject) to decide.`;
    }
    if ((m = body.match(/^准备运行步骤：([\s\S]*?)。请使用卡片按钮（允许\s*\/\s*始终允许\s*\/\s*拒绝）决定。$/))) {
      return `Ready to run step: ${m[1]}. Use the card buttons (Allow / Always allow / Reject) to decide.`;
    }
    if ((m = body.match(/^任务已达重试上限：([\s\S]*?)。请使用卡片按钮（允许\s*\/\s*始终允许\s*\/\s*拒绝）决定。$/))) {
      return `Task reached retry limit: ${m[1]}. Use the card buttons (Allow / Always allow / Reject) to decide.`;
    }
    if ((m = body.match(/^需要确认\s*\[([^\]]+)\]：([\s\S]*?)。请使用卡片按钮决定。$/))) {
      return `Approval required [${m[1]}]: ${m[2]}. Use the card buttons to decide.`;
    }
    return body;
  }

  function localizeDaemonMessage(msg) {
    const text = String(msg || "").trim();
    if (!isEn() || !text) return text;
    if (text === "完成用户对话目标并经 Lead 验收") {
      return "Complete the user's conversation goal with Lead sign-off";
    }
    if (text === "已批准当前操作，并将项目权限模式切换为「全自动 (auto)」。") {
      return "Approved and switched project permission mode to Auto (auto).";
    }
    if (text === "已批准当前操作；当前项目已处于「全自动 (auto)」模式，无需重复切换。") {
      return "Approved; project is already in Auto (auto) mode.";
    }
    if (text === "当前操作已批准，且项目已处于「全自动 (auto)」模式，无需重复切换。") {
      return "Already approved, and project is already in Auto (auto) mode.";
    }
    const m = text.match(/^选择「始终允许」会将项目「([^」]+)」的权限模式切换为「全自动 \(auto\)」：/);
    if (m) {
      return `Choosing "Always allow" will switch project "${m[1]}" to Auto (auto) mode: future plan dispatches and over-limit retries will be auto-allowed (permission_escalation still requires separate approval).`;
    }
    if (text.includes("(click 允许 on the plan card)")) {
      return text.replace(/\(click\s+允许\s+on\s+the\s+plan\s+card\)/g, '(click "Allow" on the plan card)');
    }
    return text;
  }

  function getDomString(key) {
    const entry = DOM_STRINGS[key];
    if (!entry) return "";
    return isEn() ? entry.en : entry.zh;
  }

  function isOpenModalTitle(el) {
    if (!el || typeof el.closest !== "function") return false;
    if (el.id !== "modalTitle" && el.id !== "confirmModalTitle") return false;
    return Boolean(el.closest(".modal-backdrop:not(.hidden), .confirm-modal-backdrop:not(.hidden)"));
  }

  function applyToDom(root) {
    const doc = root || (typeof document !== "undefined" ? document : null);
    if (!doc) return;
    const lang = getLang();
    if (doc.documentElement) {
      if (typeof doc.documentElement.setAttribute === "function") {
        doc.documentElement.setAttribute("lang", lang);
      } else {
        doc.documentElement.lang = lang;
      }
    }
    if (typeof doc.querySelectorAll !== "function") return;

    doc.querySelectorAll("[data-i18n]").forEach((el) => {
      if (isOpenModalTitle(el)) return;
      const key = el.getAttribute("data-i18n");
      const val = getDomString(key);
      if (val) el.textContent = val;
    });
    doc.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const key = el.getAttribute("data-i18n-title");
      const val = getDomString(key);
      if (val) {
        el.title = val;
        if (typeof el.removeAttribute === "function") {
          el.removeAttribute("data-orig-title");
        }
      }
    });
    doc.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      const val = getDomString(key);
      if (val) el.placeholder = val;
    });
    doc.querySelectorAll("[data-i18n-aria-label]").forEach((el) => {
      const key = el.getAttribute("data-i18n-aria-label");
      const val = getDomString(key);
      if (val) el.setAttribute("aria-label", val);
    });
  }

  const api = {
    STORAGE_KEY,
    VALID_PREFS,
    normalizePref,
    detectRuntimeLang,
    setSystemLang,
    resolveLang,
    getPref,
    getLang,
    isEn,
    setPref,
    toggleLang,
    tr,
    getDomString,
    applyToDom,
    DOM_STRINGS,
    APPROVAL_POLICY_LABELS_EN,
    GATE_CATEGORY_LABELS_EN,
    PERMISSION_MODE_META_EN,
    TASK_STATE_LABELS_EN,
    PLAN_STATUS_LABELS_EN,
    ERROR_LABELS_EN,
    PROFILE_LABELS_EN,
    ACTIVITY_KIND_LABELS_EN,
    VENDOR_AUTH_HINTS_EN,
    ADAPTER_MODEL_NOTES_EN,
    localizeAdapterModelNote,
    localizeDaemonCardBody,
    localizeDaemonMessage
  };

  if (typeof window !== "undefined") {
    window.MeshI18n = api;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})();
