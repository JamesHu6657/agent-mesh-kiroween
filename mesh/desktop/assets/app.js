(function () {
  "use strict";

  // =========================================================================
  // Constants & Authoritative Contracts
  // =========================================================================
  const ALL_TASK_STATES = [
    "created", "assigned", "queued", "running", "cancelling",
    "submitted", "rework", "accepted", "merging", "merge_failed",
    "merged", "failed", "escalated", "cancelled"
  ];

  const NEED_YOU_STATES = new Set([
    "submitted", "rework", "merge_failed", "failed", "escalated", "cancelling"
  ]);
  const IN_PROGRESS_STATES = new Set([
    "created", "assigned", "queued", "running", "merging"
  ]);
  const COMPLETED_STATES = new Set(["merged", "cancelled"]);

  const APPROVAL_POLICY_META = [
    ["config", "系统配置变更（仅允许本机直连确认）", "direct_only", true],
    ["lead_change", "变更或交接 Lead", "direct_or_relayed", false],
    ["project_control", "项目启停与取消控制", "direct_or_relayed", false],
    ["scope_change", "项目目标与验收标准变更", "direct_or_relayed", false],
    ["permission_escalation", "任务权限档位提升", "direct_only", false],
    ["retry_over_limit", "超限重试", "direct_or_relayed", false],
    ["delete", "删除内部工作区或资源", "direct_or_relayed", false],
    ["deliver_to_base", "交付至本地基准分支", "not_required", false],
    ["spend_money", "外部付费操作", "direct_or_relayed", false],
    ["deploy", "外部部署操作", "direct_or_relayed", false],
    ["delete_external", "删除外部资源", "direct_or_relayed", false],
    ["external_other", "其他外部副作用操作", "direct_or_relayed", false],
    ["plan_dispatch", "计划派发确认（仅允许本机直连确认）", "direct_only", true],
    ["step_dispatch", "单步派发确认（仅允许本机直连确认）", "direct_only", true]
  ];

  const GATE_CATEGORY_LABELS = {
    permission_escalation: "高危权限提升",
    config: "系统配置变更",
    retry_over_limit: "任务超出重试上限",
    plan_dispatch: "计划派发确认",
    step_dispatch: "单步派发确认",
    scope_change: "项目范围变更",
    project_control: "项目状态控制",
    lead_change: "项目 Lead 变更",
    delete: "删除资源",
    deliver_to_base: "交付至基准分支",
    spend_money: "外部付费操作",
    deploy: "外部部署操作",
    delete_external: "删除外部资源",
    external_other: "外部操作授权"
  };

  const PERMISSION_MODE_META = {
    supervised: {
      label: "每步问我 (supervised)",
      shortLabel: "每步问我",
      desc: "计划卡片需确认，派发后的每个步骤运行前也会弹出审批卡片"
    },
    plan: {
      label: "计划批了就放手 (plan)",
      shortLabel: "计划批了就放手",
      desc: "默认模式：批准计划卡片后，非提权步骤自动执行；提权仍需审批卡片"
    },
    auto: {
      label: "全自动 (auto)",
      shortLabel: "全自动",
      desc: "跳过计划审批并自动放行超限重试；高危提权 (permission_escalation) 仍需单独卡片授权"
    }
  };

  const ALWAYS_ALLOW_CATEGORIES = new Set([
    "plan_dispatch",
    "step_dispatch",
    "permission_escalation",
    "retry_over_limit"
  ]);

  const MAX_RETAINED_EVENTS = 500;
  const EVENT_TAIL_FETCH_LIMIT = 500;
  const APPROVAL_LIST_LIMIT = 200;
  const ACTIVE_RUN_LIST_LIMIT = 200;
  const PROJECT_RUN_LIST_LIMIT = 100;
  const APPROVAL_SURFACE_SETTLE_MS = 600;
  const APPROVAL_RESOLVED_PLACEHOLDER_MS = 900;
  const REFRESH_DEBOUNCE_MS = 180;
  const REFRESH_DEBOUNCE_MAX_WAIT_MS = 1000;
  const PLAN_STEP_SPEC_COLLAPSE_CHARS = 240;
  const PLAN_STEP_SPEC_COLLAPSE_LINES = 4;

  // Copyable install / login hints for A7 onboarding (never executed from WebView)
  const VENDOR_AUTH_HINTS = {
    claude: { install: "npm install -g @anthropic-ai/claude-code", login: "claude auth login" },
    codex: { install: "npm install -g @openai/codex", login: "codex login" },
    kiro: { install: "curl -fsSL https://cli.kiro.dev/install | bash", login: "kiro-cli login" },
    agy: { install: "参考 Antigravity CLI 官方安装指引", login: "agy login" },
    droid: { install: "curl -fsSL https://app.factory.ai/cli | sh", login: "droid login" },
    vibe: { install: "uv tool install mistral-vibe", login: "vibe login" }
  };

  // Grace period before moving a completed project out of the sidebar Working group
  const WORKING_EXIT_GRACE_MS = 2000;

  const TASK_STATE_LABELS = {
    created: "待授权 / 未指派",
    assigned: "已指派",
    queued: "排队中",
    running: "执行中",
    cancelling: "取消中",
    submitted: "待验收",
    rework: "待返工",
    accepted: "已验收",
    merging: "合并中",
    merge_failed: "合并失败",
    merged: "已完成",
    failed: "执行失败",
    escalated: "已升级",
    cancelled: "已取消"
  };

  const PLAN_STATUS_META = {
    proposed: { label: "待你确认", cls: "amber" },
    approved_pending_dispatch: { label: "已确认，等待派发", cls: "blue" },
    dispatched: { label: "执行中", cls: "blue" },
    revising: { label: "修订中", cls: "amber" },
    superseded: { label: "已修订", cls: "neutral" },
    rejected: { label: "已拒绝", cls: "red" },
    cancelled: { label: "已取消", cls: "neutral" }
  };

  const ERROR_LABELS = {
    TOKEN_MISSING: "请先输入口令解锁工作台",
    PASSPHRASE_INVALID: "口令不正确，请重试（连续 5 次错误将触发临时锁定）",
    PASSPHRASE_REQUIRED: "需要输入用户口令才能继续操作",
    TOKEN_INVALID: "当前登录态无效或已过期，请重新解锁",
    LOCKED_OUT: "连续口令错误次数过多，账户已临时锁定",
    DAEMON_UNAVAILABLE: "无法连接到后台服务，请启动服务或等待自动重连",
    DAEMON_ALREADY_RUNNING: "后台服务已在运行或正在完成收尾",
    HOME_NOT_INITIALIZED: "尚未初始化数据目录，请在引导向导中设置口令或运行 mesh setup",
    ALREADY_EXISTS: "目标已存在，无需重复创建",
    NO_LEAD: "尚未配置默认 Lead，请先在向导或 AI 工具中选择一个 Lead",
    LEAD_NOT_ELIGIBLE: "所选 Agent 暂不可担任 Lead（需启用 Lead 资格且未禁用）",
    LEAD_HAS_TASKS: "所选 Agent 在当前项目中有未完成任务，不能同时担任 Lead",
    ADAPTER_UNSUPPORTED: "所选 AI 工具暂不可作为 Lead 启动，请检查工具状态",
    PLAN_APPROVAL_REQUIRED: "当前项目需先在计划卡片上点击「允许」后才可派发任务",
    CHANNEL_NOT_ALLOWED: "此操作仅允许通过本机口令直连确认",
    SELF_APPROVAL: "审批发起人不能自行批准自己的请求",
    NOT_PROJECT_LEAD: "当前主体不是项目 Lead",
    CAPABILITY_MISSING: "缺少执行该操作所需的权限",
    OP_HASH_MISMATCH: "审批校验摘要已变化，请刷新后核对最新卡片再操作",
    APPROVAL_EXPIRED: "该审批请求已过期，状态已更新为已过期",
    APPROVAL_REJECTED: "该审批请求已被拒绝",
    APPROVAL_CONSUMED: "该审批请求已被使用，无需重复操作",
    APPROVAL_CANCELLED: "该审批请求已取消",
    PROJECT_NOT_ACTIVE: "当前项目未处于进行中状态（已暂停、已完成或已取消），无法执行此操作",
    DECISION_CONFLICT: "该审批卡片已有最终决定，请刷新查看最新状态",
    APPROVAL_REQUIRED: "该操作触发了安全门控，请在对话中的审批卡片上点击「允许 / 始终允许 / 拒绝」",
    AUDIT_MISMATCH: "审计校验未通过，检测到事件链或项目/审批状态存在篡改或不一致",
    MERGE_CONFLICT: "代码分支合并冲突，需要处理后重试合并",
    INVALID_ARGUMENT: "输入参数有误，请检查后重试",
    INVALID_TRANSITION: "当前状态不允许执行此操作",
    CONCURRENCY_LIMIT: "存在正在进行的并发操作，请稍后再试"
  };

  // Authoritative supported_profiles, profile_flags, and model_presets come from
  // adapter.list / adapter.show; fallbacks only apply before metadata loads.
  const FALLBACK_SUPPORTED_PROFILES = {
    droid: ["readonly", "edit", "dev", "elevated", "unrestricted"],
    agy: ["readonly", "edit", "unrestricted"],
    kiro: ["readonly", "edit", "unrestricted"],
    vibe: ["readonly", "edit", "dev", "unrestricted"],
    codex: ["readonly", "dev", "unrestricted"],
    claude: ["readonly", "edit", "unrestricted"],
    devin: ["unrestricted"],
    hermes: [],
    openclaw: [],
    none: ["readonly", "edit", "dev", "elevated", "unrestricted"],
    fake: ["readonly", "edit", "dev", "elevated", "unrestricted"]
  };
  const PROFILE_ORDER = ["readonly", "edit", "dev", "elevated", "unrestricted"];
  const SAFE_PROFILE_ORDER = ["edit", "dev", "readonly"];
  const PROFILE_LABELS = {
    readonly: "readonly (只读)",
    edit: "edit (工作区编辑)",
    dev: "dev (工作区开发与命令)",
    elevated: "elevated (高权限 · 仅 Lead 发起的任务需审批；你直接派发的任务不再确认)",
    unrestricted: "unrestricted (无限制 · 仅 Lead 发起的任务需审批；你直接派发的任务不再确认)"
  };
  const FALLBACK_HIGH_PROFILE_FLAGS = {
    droid: { elevated: ["--auto", "high"], unrestricted: ["--skip-permissions-unsafe"] },
    agy: { unrestricted: ["--dangerously-skip-permissions"] },
    kiro: { unrestricted: ["--trust-all-tools"] },
    vibe: { unrestricted: ["--agent", "auto-approve", "--auto-approve"] },
    codex: { unrestricted: ["--dangerously-bypass-approvals-and-sandbox"] },
    claude: { unrestricted: ["--permission-mode", "bypassPermissions"] },
    devin: { unrestricted: ["--permission-mode", "dangerous"] }
  };
  const UNAVAILABLE_ADAPTER_IDS = new Set(["devin", "hermes", "openclaw"]);
  const LEAD_WAIT_NOTICE_TIMEOUT_S = 25;

  const STATE = {
    bridgeReady: false,
    connected: false,
    daemonStarting: false,
    daemonStopping: false,
    userStopped: false,
    loadingWorkspace: false,
    hasLoadedWorkspace: false,
    auth: {
      auth_configured: false,
      auth_verified: false,
      principal: null,
      last_auth_error: null,
      stored_credential_supported: false,
      stored_credential_present: false
    },
    desktopPrefs: {
      close_to_tray: true,
      tray_active: false,
      tray_supported: false
    },
    pipe: {
      home: "",
      home_initialized: null,
      setup_hint: "mesh setup",
      endpoint: "",
      transport: "",
      daemon_version: "--",
      daemon_status: null,
      reconnect_attempt: 0,
      next_retry_s: 0,
      last_error: null,
      last_start_error: null,
      daemon_log_path: "",
      desktop_log_path: "",
      daemon_diag_log: []
    },
    capabilities: {
      confirm_plan: true,
      task_diff: true,
      project_conversation: true,
      task_log_tail: false,
      approval_policy_inline: true,
      task_list_latest_run: false
    },
    wizard: {
      active: false,
      step: 1, // 1 = init passphrase, 2 = pick your lead
      skipped: false,
      scanningClis: false,
      submitting: false,
      clis: [],
      cliModels: {},       // adapter_id -> string[] from adapter.models
      selectedLeadCli: "", // adapter_id or "__inbox__"
      selectedModels: {}   // adapter_id -> comma-separated string
    },
    ui: Object.assign(
      {
        theme: loadStoredTheme(),
        nav: "tasks",
        sidebarCollapsed: false,
        sidebarWidth: 280,
        inspectorCollapsed: true,
        inspectorWidth: 340,
        inspectorTab: "task",
        searchVisible: false,
        searchQuery: "",
        draftNewChat: false,
        repoDrawerOpen: false,
        repoInspection: null,
        repoInspecting: false,
        recentFolders: [],
        activeProjectByHome: {},
        collapsedProjectGroups: { need_you: false, working: false, idle: false, completed: true },
        rightTabsByProject: {},
        diffScope: "task",
        leadPopoverQuery: "",
        interruptingLead: false,
        logStreamFilter: "all",
        logViewMode: "grouped",
        logRunId: null,
        composerInlineNote: null,
        selectedLeadDraft: null,
        windowMaximized: false,
        sendingChat: false,
        decidingApprovalIds: new Set(),
        focusAdvancedApprovalId: null
      },
      loadStoredUiState()
    ),
    projects: [],
    activeProjectId: null,
    activeProjectDetail: null,
    chatProjectStatus: null,
    chatPlanGateMode: "require_approval",
    chatPermissionMode: "plan",
    chatMessages: [],
    chatHasMore: false,
    chatOldestSeq: null,
    chatLoadingOlder: false,
    chatPlansById: {},
    chatApprovalsById: {},
    chatNewestPageApprovalIds: new Set(),
    chatLeadStatus: null,
    activeLeadRun: null,
    leadFailureByProject: {},
    queuedUserMsgIdsByProject: {},
    sidebarTracker: {
      workingSince: {},
      graceUntil: {},
      returnedAt: {},
      graceTimers: {}
    },
    plans: [],
    tasks: [],
    activeTaskId: null,
    taskDetails: {},
    taskWorkspaces: {},
    taskLogs: {},
    taskActivities: {},
    taskDiffs: {},
    projectDiffs: {},
    runActivities: {},
    taskDeps: {},
    approvals: [],
    approvalsById: new Map(),
    approvalsByProjectId: new Map(),
    approvalsByTaskId: new Map(),
    grants: [],
    grantsIncludeRevoked: false,
    agents: [],
    agentDetails: {},
    adapters: [],
    adaptersLoaded: false,
    adaptersLoading: false,
    adapterDetails: {},
    runs: [],
    runsById: new Map(),
    runsByTaskId: new Map(),
    activeRunsByProjectId: new Map(),
    leadTurnLogs: {},
    events: [],
    eventsProjectId: null,
    minRetainedSeq: 0,
    maxSeenEventSeq: 0,
    config: {},
    configItems: [],
    doctor: null,
    seenSeqs: new Set()
  };

  let logPollTimer = null;
  let eventPollTimer = null;
  let eventPollInFlight = false;
  let refreshTimer = null;
  let refreshMaxWaitTimer = null;
  let durationTimer = null;
  let workspaceLoadSeq = 0;
  let projectLoadSeq = 0;
  let sidebarLoadSeq = 0;
  let projectSwitchGen = 0;
  let diffLoadSeq = 0;
  let taskLogSeq = 0;
  let leadOutlineSeq = 0;
  let taskDetailSeq = 0;
  let editAgentModalSeq = 0;
  const cancellingQueuedMsgIds = new Set();
  let confirmModalResolver = null;
  let confirmModalGuardTimer = null;
  let repoInspectTimer = null;
  let repoInspectSeq = 0;
  let modalPromptResolver = null;
  let lastChatThreadHtml = null;
  let lastChatMsgCount = 0;
  let lastChatTailId = null;
  let lastSettingsHtml = null;
  let settingsRenderDeferred = false;
  let indexedApprovalsRef = null;
  let indexedRunsRef = null;
  let stripApprovalOrder = [];
  let stripOrderProjectId = null;
  let inspectorApprovalOrder = [];
  let inspectorOrderProjectId = null;
  const recentlyResolvedApprovals = new Map();
  const approvalSurfaceGuards = {
    strip: { sig: "", changedAt: 0 },
    thread: { sig: "", changedAt: 0 },
    inspector: { sig: "", changedAt: 0 }
  };
  let pendingRefreshFlags = { full: false, project: false, sidebar: false, adapters: false };

  function bumpProjectSwitchGen() {
    projectSwitchGen += 1;
    diffLoadSeq += 1;
    taskLogSeq += 1;
    leadOutlineSeq += 1;
    taskDetailSeq += 1;
    return projectSwitchGen;
  }

  function bumpSidebarLoadSeq() {
    sidebarLoadSeq += 1;
    return sidebarLoadSeq;
  }

  function rebuildApprovalIndexes(items = STATE.approvals) {
    const list = Array.isArray(items) ? items : [];
    const byId = new Map();
    const byProject = new Map();
    const byTask = new Map();
    for (const apr of list) {
      if (!apr || !apr.id) continue;
      byId.set(apr.id, apr);
      if (apr.project_id) {
        let bucket = byProject.get(apr.project_id);
        if (!bucket) { bucket = []; byProject.set(apr.project_id, bucket); }
        bucket.push(apr);
      }
      if (apr.task_id) {
        let bucket = byTask.get(apr.task_id);
        if (!bucket) { bucket = []; byTask.set(apr.task_id, bucket); }
        bucket.push(apr);
      }
    }
    STATE.approvalsById = byId;
    STATE.approvalsByProjectId = byProject;
    STATE.approvalsByTaskId = byTask;
    indexedApprovalsRef = list;
  }

  function setApprovals(items) {
    STATE.approvals = Array.isArray(items) ? items : [];
    rebuildApprovalIndexes(STATE.approvals);
  }

  function ensureApprovalIndexes() {
    if (indexedApprovalsRef !== STATE.approvals) {
      rebuildApprovalIndexes(STATE.approvals);
    }
  }

  function getApprovalFromListById(approvalId) {
    if (!approvalId) return null;
    ensureApprovalIndexes();
    return STATE.approvalsById.get(approvalId) || null;
  }

  function getApprovalsForProject(projectId) {
    if (!projectId) return [];
    ensureApprovalIndexes();
    return STATE.approvalsByProjectId.get(projectId) || [];
  }

  function getApprovalsForTask(taskId) {
    if (!taskId) return [];
    ensureApprovalIndexes();
    return STATE.approvalsByTaskId.get(taskId) || [];
  }

  function rebuildRunIndexes(items = STATE.runs) {
    const list = Array.isArray(items) ? items : [];
    const byId = new Map();
    const byTask = new Map();
    const activeByProject = new Map();
    for (const run of list) {
      if (!run || !run.id) continue;
      byId.set(run.id, run);
      if (run.task_id) {
        let bucket = byTask.get(run.task_id);
        if (!bucket) { bucket = []; byTask.set(run.task_id, bucket); }
        bucket.push(run);
      }
      if (run.project_id && (run.status === "starting" || run.status === "running")) {
        let bucket = activeByProject.get(run.project_id);
        if (!bucket) { bucket = []; activeByProject.set(run.project_id, bucket); }
        bucket.push(run);
      }
    }
    STATE.runsById = byId;
    STATE.runsByTaskId = byTask;
    STATE.activeRunsByProjectId = activeByProject;
    indexedRunsRef = list;
  }

  function setRuns(items) {
    STATE.runs = Array.isArray(items) ? items : [];
    rebuildRunIndexes(STATE.runs);
  }

  function ensureRunIndexes() {
    if (indexedRunsRef !== STATE.runs) {
      rebuildRunIndexes(STATE.runs);
    }
  }

  function getRunsForTask(taskId) {
    if (!taskId) return [];
    ensureRunIndexes();
    return STATE.runsByTaskId.get(taskId) || [];
  }

  function getActiveRunsForProject(projectId) {
    if (!projectId) return [];
    ensureRunIndexes();
    return STATE.activeRunsByProjectId.get(projectId) || [];
  }

  function recordApprovalSurfaceLayout(surface, orderedIds, { scrolled = false, nowMs = Date.now() } = {}) {
    if (!surface || !approvalSurfaceGuards[surface]) return { changed: false, changedAt: 0 };
    const guard = approvalSurfaceGuards[surface];
    const ids = Array.isArray(orderedIds) ? orderedIds.filter(Boolean).map(String) : [];
    const sig = ids.join("|");
    const changed = sig !== guard.sig || Boolean(scrolled && ids.length > 0);
    if (changed) {
      guard.sig = sig;
      guard.changedAt = nowMs;
    }
    return { changed, changedAt: guard.changedAt, sig };
  }

  function resetApprovalSurfaceGuards() {
    Object.keys(approvalSurfaceGuards).forEach(k => {
      approvalSurfaceGuards[k] = { sig: "", changedAt: 0 };
    });
  }

  function checkApprovalClickGuard({
    surface = "",
    approvalId = "",
    action = "",
    category = "",
    planHash = "",
    opHash = "",
    nowMs = Date.now()
  } = {}) {
    if (!approvalId) {
      return { ok: false, reason: "missing_approval_id", message: "未找到审批卡片标识，请刷新后重试" };
    }
    if (recentlyResolvedApprovals.has(approvalId)) {
      return { ok: false, reason: "recently_resolved", message: "该审批卡片已完成处理" };
    }
    if (surface && approvalSurfaceGuards[surface]) {
      const guard = approvalSurfaceGuards[surface];
      if (guard.changedAt > 0 && (nowMs - guard.changedAt) < APPROVAL_SURFACE_SETTLE_MS) {
        return {
          ok: false,
          reason: "layout_shift_guard",
          remainingMs: APPROVAL_SURFACE_SETTLE_MS - (nowMs - guard.changedAt),
          message: "卡片列表刚刚变化，请核对内容后再次点击"
        };
      }
    }
    const hydrated = resolveHydratedApproval(approvalId);
    const effCategory = category || (hydrated && hydrated.category) || "";
    if (effCategory === "plan_dispatch") {
      if (!planHash) {
        return { ok: false, reason: "missing_plan_hash", message: "审批按钮缺少计划校验摘要 (plan_hash)，请刷新后重试" };
      }
      const matchedPlan = STATE.plans.find(p => p && p.approval_id === approvalId) ||
        Object.values(STATE.chatPlansById).find(p => p && p.approval_id === approvalId) || null;
      const livePlanHash = (matchedPlan && matchedPlan.plan_hash) || (hydrated && hydrated.plan_hash) || "";
      if (livePlanHash && livePlanHash !== planHash) {
        return { ok: false, reason: "plan_hash_mismatch", message: "计划内容已变化，请核对最新计划卡片后再操作" };
      }
    } else {
      if (!opHash) {
        return { ok: false, reason: "missing_op_hash", message: "审批按钮缺少校验摘要 (op_hash)，请刷新后重试" };
      }
      const liveOpHash = (hydrated && hydrated.op_hash) || "";
      if (liveOpHash && liveOpHash !== opHash) {
        return { ok: false, reason: "op_hash_mismatch", message: "审批载荷已变化，请刷新核对后再操作" };
      }
    }
    if (hydrated && Array.isArray(hydrated.available_actions) && action && !hydrated.available_actions.includes(action)) {
      return { ok: false, reason: "action_not_available", message: "该审批卡片状态已更新，当前操作已不可用" };
    }
    return { ok: true, reason: null, message: "" };
  }

  function rememberRecentlyResolvedApproval(apr, { plan = null, action = "", label = "", projectId = null, nowMs = Date.now() } = {}) {
    if (!apr || !apr.id) return;
    const aid = apr.id;
    const existing = recentlyResolvedApprovals.get(aid);
    if (existing && existing.timer && typeof window !== "undefined") {
      window.clearTimeout(existing.timer);
    }
    const statusLabel = label || (action === "reject" ? "已拒绝" : "已允许");
    const entry = {
      id: aid,
      apr: Object.assign({}, apr),
      plan: plan ? Object.assign({}, plan) : null,
      action,
      label: statusLabel,
      projectId: projectId !== null ? projectId : (apr.project_id || null),
      resolvedAt: nowMs,
      timer: null
    };
    if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
      entry.timer = window.setTimeout(() => {
        const cur = recentlyResolvedApprovals.get(aid);
        if (cur === entry) {
          recentlyResolvedApprovals.delete(aid);
          renderComposerApprovalStrip();
          if (!STATE.ui.inspectorCollapsed && STATE.ui.inspectorTab === "advanced") {
            renderInspector();
          }
        }
      }, APPROVAL_RESOLVED_PLACEHOLDER_MS);
    }
    recentlyResolvedApprovals.set(aid, entry);
  }

  function clearRecentlyResolvedApprovals() {
    recentlyResolvedApprovals.forEach(entry => {
      if (entry && entry.timer && typeof window !== "undefined") {
        window.clearTimeout(entry.timer);
      }
    });
    recentlyResolvedApprovals.clear();
  }

  function orderStripItemsStably(projectId, items) {
    if (stripOrderProjectId !== projectId) {
      stripOrderProjectId = projectId;
      stripApprovalOrder = [];
    }
    const list = Array.isArray(items) ? items : [];
    const byId = new Map();
    for (const it of list) {
      if (it && it.apr && it.apr.id) byId.set(it.apr.id, it);
    }
    const nextOrder = [];
    const seen = new Set();
    // Keep existing items in their exact previous positions
    for (const id of stripApprovalOrder) {
      if (byId.has(id) && !seen.has(id)) {
        nextOrder.push(id);
        seen.add(id);
      }
    }
    // Append newly arrived items (including new plans) at the end to prevent top-insertion shift
    for (const it of list) {
      const id = it && it.apr && it.apr.id;
      if (id && !seen.has(id)) {
        nextOrder.push(id);
        seen.add(id);
      }
    }
    stripApprovalOrder = nextOrder;
    return nextOrder.map(id => byId.get(id)).filter(Boolean);
  }

  function orderInspectorItemsStably(projectId, items) {
    if (inspectorOrderProjectId !== projectId) {
      inspectorOrderProjectId = projectId;
      inspectorApprovalOrder = [];
    }
    const list = Array.isArray(items) ? items : [];
    const getItemId = it => (it && it.raw && it.raw.id) || (it && it.apr && it.apr.id) || (it && it.id) || null;
    const byId = new Map();
    for (const it of list) {
      const id = getItemId(it);
      if (id) byId.set(id, it);
    }
    const nextOrder = [];
    const seen = new Set();
    for (const id of inspectorApprovalOrder) {
      if (byId.has(id) && !seen.has(id)) {
        nextOrder.push(id);
        seen.add(id);
      }
    }
    for (const it of list) {
      const id = getItemId(it);
      if (id && !seen.has(id)) {
        nextOrder.push(id);
        seen.add(id);
      }
    }
    inspectorApprovalOrder = nextOrder;
    return nextOrder.map(id => byId.get(id)).filter(Boolean);
  }

  // =========================================================================
  // Helpers & Formatting
  // =========================================================================
  function loadStoredTheme() {
    try {
      const saved = window.localStorage.getItem("mesh.desktop.theme");
      return saved === "light" ? "light" : "dark";
    } catch (_) {
      return "dark";
    }
  }

  function saveStoredTheme(theme) {
    try {
      window.localStorage.setItem("mesh.desktop.theme", theme);
    } catch (_) {
      // Ignore locked storage
    }
  }

  // S6: Persist strictly non-sensitive UI preferences only (never passphrases or tokens).
  function loadStoredUiState() {
    const out = {};
    try {
      const raw = window.localStorage.getItem("mesh.desktop.ui_state");
      if (!raw) return out;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return out;
      if (typeof parsed.sidebarCollapsed === "boolean") out.sidebarCollapsed = parsed.sidebarCollapsed;
      if (typeof parsed.sidebarWidth === "number" && Number.isFinite(parsed.sidebarWidth)) {
        out.sidebarWidth = Math.max(240, Math.min(400, Math.round(parsed.sidebarWidth)));
      }
      if (typeof parsed.inspectorCollapsed === "boolean") out.inspectorCollapsed = parsed.inspectorCollapsed;
      if (typeof parsed.inspectorWidth === "number" && Number.isFinite(parsed.inspectorWidth)) {
        out.inspectorWidth = Math.max(280, Math.min(480, Math.round(parsed.inspectorWidth)));
      }
      if (parsed.collapsedProjectGroups && typeof parsed.collapsedProjectGroups === "object") {
        out.collapsedProjectGroups = {
          need_you: Boolean(parsed.collapsedProjectGroups.need_you),
          working: Boolean(parsed.collapsedProjectGroups.working),
          idle: Boolean(parsed.collapsedProjectGroups.idle),
          completed: parsed.collapsedProjectGroups.completed !== undefined ? Boolean(parsed.collapsedProjectGroups.completed) : true
        };
      }
      if (parsed.activeProjectByHome && typeof parsed.activeProjectByHome === "object") {
        const cleanMap = {};
        Object.entries(parsed.activeProjectByHome).slice(0, 20).forEach(([k, v]) => {
          if (typeof k === "string" && k && typeof v === "string" && v) cleanMap[k] = v;
        });
        out.activeProjectByHome = cleanMap;
      }
      if (Array.isArray(parsed.recentFolders)) {
        out.recentFolders = parsed.recentFolders
          .filter(p => typeof p === "string" && p.trim().length > 0)
          .slice(0, 5);
      }
    } catch (_) {
      // Ignore malformed storage
    }
    return out;
  }

  function saveStoredUiState() {
    try {
      const payload = {
        sidebarCollapsed: Boolean(STATE.ui.sidebarCollapsed),
        sidebarWidth: Math.max(240, Math.min(400, Math.round(STATE.ui.sidebarWidth || 280))),
        inspectorCollapsed: Boolean(STATE.ui.inspectorCollapsed),
        inspectorWidth: Math.max(280, Math.min(480, Math.round(STATE.ui.inspectorWidth || 340))),
        collapsedProjectGroups: {
          need_you: Boolean(STATE.ui.collapsedProjectGroups && STATE.ui.collapsedProjectGroups.need_you),
          working: Boolean(STATE.ui.collapsedProjectGroups && STATE.ui.collapsedProjectGroups.working),
          idle: Boolean(STATE.ui.collapsedProjectGroups && STATE.ui.collapsedProjectGroups.idle),
          completed: Boolean(STATE.ui.collapsedProjectGroups && STATE.ui.collapsedProjectGroups.completed)
        },
        activeProjectByHome: STATE.ui.activeProjectByHome || {},
        recentFolders: Array.isArray(STATE.ui.recentFolders) ? STATE.ui.recentFolders.slice(0, 5) : []
      };
      window.localStorage.setItem("mesh.desktop.ui_state", JSON.stringify(payload));
    } catch (_) {
      // Ignore locked storage
    }
  }

  function rememberActiveProjectForHome(projectId) {
    const home = STATE.pipe.home;
    if (!home || !projectId) return;
    if (!STATE.ui.activeProjectByHome || typeof STATE.ui.activeProjectByHome !== "object") {
      STATE.ui.activeProjectByHome = {};
    }
    STATE.ui.activeProjectByHome[home] = projectId;
    saveStoredUiState();
  }

  function getSavedActiveProjectForHome() {
    const home = STATE.pipe.home;
    if (!home || !STATE.ui.activeProjectByHome) return null;
    return STATE.ui.activeProjectByHome[home] || null;
  }

  function addRecentFolder(folderPath) {
    const cleaned = String(folderPath || "").trim();
    if (!cleaned) return;
    const prev = Array.isArray(STATE.ui.recentFolders) ? STATE.ui.recentFolders : [];
    const next = [cleaned].concat(prev.filter(p => p !== cleaned)).slice(0, 5);
    STATE.ui.recentFolders = next;
    saveStoredUiState();
  }

  function applyTheme(theme) {
    STATE.ui.theme = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", STATE.ui.theme);
    saveStoredTheme(STATE.ui.theme);
  }

  function esc(val) {
    if (val === null || val === undefined) return "";
    return String(val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function $(id) {
    return document.getElementById(id);
  }

  function hasApi() {
    return Boolean(window.pywebview && window.pywebview.api);
  }

  function canMutate() {
    return Boolean(STATE.connected && STATE.auth.auth_verified);
  }

  function disabledReason() {
    if (!STATE.bridgeReady) return "桌面桥接尚未就绪";
    if (!STATE.connected) return "后台服务已断开 — 只读模式下无法修改";
    if (!STATE.auth.auth_verified) return "请先输入口令解锁";
    return "";
  }

  function formatErrorMessage(err) {
    if (!err) return "发生未知错误";
    const code = err.code || "INTERNAL_ERROR";
    if (code === "INVALID_TRANSITION" && err.details && err.details.reason === "task_terminal") {
      const ts = err.details.task_status;
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || (ts === "missing" ? "不存在" : ts)) : "已终止";
      return `关联任务已终止（${tsLabel}），无法批准该失效的审批卡片`;
    }
    if (code === "INVALID_TRANSITION" && err.details && err.details.reason === "task_progressed") {
      const ts = err.details.task_status;
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || ts) : "后续阶段";
      return `关联任务已推进至「${tsLabel}」阶段，该审批卡片已过时失效`;
    }
    const hint = ERROR_LABELS[code];
    const raw = err.message || "";
    if (hint && raw && !raw.includes(hint)) {
      return `${hint}：${raw}`;
    }
    return hint || raw || code;
  }

  function toast(text, level = "info") {
    const stack = $("toastStack");
    if (!stack) return;
    const item = document.createElement("div");
    item.className = "toast-item";
    const dotCls = level === "ok" ? "dot-green" : level === "warn" ? "dot-amber" : level === "err" ? "dot-red" : "dot-blue";
    item.innerHTML = `<div style="display:flex;align-items:center;gap:8px;min-width:0;"><span class="dot ${dotCls}"></span><span style="word-break:break-word;">${esc(text)}</span></div><button class="icon-btn" type="button" style="width:20px;height:20px;">&times;</button>`;
    const closeBtn = item.querySelector("button");
    const remove = () => {
      if (item.parentNode) item.parentNode.removeChild(item);
    };
    closeBtn.addEventListener("click", remove);
    stack.appendChild(item);
    window.setTimeout(remove, 4800);
  }

  function formatClock(isoStr) {
    if (!isoStr) return "";
    const ms = Date.parse(isoStr);
    if (Number.isNaN(ms)) return String(isoStr).slice(0, 16);
    const d = new Date(ms);
    return d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  }

  function formatDateTime(isoStr) {
    if (!isoStr) return "--";
    const ms = Date.parse(isoStr);
    if (Number.isNaN(ms)) return String(isoStr);
    return new Date(ms).toLocaleString("zh-CN", { hour12: false });
  }

  function formatDuration(secTotal) {
    const s = Math.max(0, Math.floor(secTotal));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(r).padStart(2, "0");
    return h > 0 ? `${String(h).padStart(2, "0")}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  // =========================================================================
  // A6: Client-Side Grouped Activity Summarizer
  // Maps tool-call log entries into grouped activity categories for the UI.
  // =========================================================================
  const ACTIVITY_KIND_LABELS = {
    read: "读取",
    edit: "编辑",
    command: "命令",
    search: "搜索",
    "mesh-action": "动作",
    thought: "思考",
    other: "工具"
  };

  function classifyToolActivityKind(name, rawType) {
    const n = String(name || rawType || "").toLowerCase();
    if (!n) return "other";
    if (n.includes("mesh") || n.startsWith("plan.") || n.startsWith("chat.") || n.startsWith("task.")) return "mesh-action";
    if (n.includes("read") || n.includes("view") || n.includes("cat") || n.includes("open") || n.includes("list_dir")) return "read";
    if (n.includes("edit") || n.includes("write") || n.includes("patch") || n.includes("file_change") || n.includes("create")) return "edit";
    if (n.includes("bash") || n.includes("shell") || n.includes("exec") || n.includes("command") || n.includes("run") || n.includes("terminal")) return "command";
    if (n.includes("grep") || n.includes("glob") || n.includes("search") || n.includes("find")) return "search";
    if (n.includes("think") || n.includes("reason")) return "thought";
    return "other";
  }

  function extractActivitySummaryTarget(obj) {
    if (!obj || typeof obj !== "object") return "";
    const inp = (obj.input && typeof obj.input === "object") ? obj.input
      : ((obj.arguments && typeof obj.arguments === "object") ? obj.arguments
        : ((obj.args && typeof obj.args === "object") ? obj.args : obj));
    for (const k of ["command", "cmd", "file_path", "path", "pattern", "query", "title", "url"]) {
      if (typeof inp[k] === "string" && inp[k].trim()) {
        return inp[k].trim().slice(0, 120);
      }
    }
    if (Array.isArray(inp.changes) && inp.changes.length) {
      return inp.changes.map(ch => ch && (ch.path || ch.file)).filter(Boolean).slice(0, 3).join(", ");
    }
    return "";
  }

  function parseActivityFromLogText(rawText) {
    if (typeof rawText !== "string" || !rawText.trim()) return [];
    const entries = [];
    const lines = rawText.split(/\r?\n/);

    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i].trim();
      if (!line || line[0] !== "{") continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch (_) {
        continue;
      }
      if (!obj || typeof obj !== "object") continue;

      // Claude stream-json assistant message with content[] blocks
      if (obj.type === "assistant" && obj.message && Array.isArray(obj.message.content)) {
        for (const block of obj.message.content) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "tool_use") {
            const name = block.name || "tool";
            const target = extractActivitySummaryTarget(block);
            entries.push({
              kind: classifyToolActivityKind(name, "tool_use"),
              name,
              target,
              detail: JSON.stringify(block.input || {}, null, 2).slice(0, 1600)
            });
          } else if (block.type === "thinking" && typeof block.thinking === "string" && block.thinking.trim()) {
            entries.push({
              kind: "thought",
              name: "thinking",
              target: block.thinking.trim().slice(0, 80),
              detail: block.thinking.trim().slice(0, 1600)
            });
          }
        }
        continue;
      }

      // Codex JSONL item events (item.completed / item.started)
      if ((obj.type === "item.completed" || obj.type === "item.started") && obj.item && typeof obj.item === "object") {
        const it = obj.item;
        const itType = String(it.type || "");
        if (itType === "command_execution" || itType === "file_change" || itType === "mcp_tool_call" || itType === "reasoning") {
          const name = it.tool || it.name || itType;
          const target = extractActivitySummaryTarget(it) || (typeof it.text === "string" ? it.text.slice(0, 80) : "");
          entries.push({
            kind: classifyToolActivityKind(name, itType),
            name,
            target,
            detail: JSON.stringify(it, null, 2).slice(0, 1600)
          });
        }
        continue;
      }

      // Generic tool_use / tool_call / function_call JSONL lines (agy / droid / kiro / vibe)
      const evType = String(obj.type || obj.event || "").toLowerCase();
      if (evType === "tool_use" || evType === "tool_call" || evType === "function_call" || obj.tool_name || (obj.tool && typeof obj.tool === "string")) {
        const name = obj.tool_name || obj.tool || obj.name || evType || "tool";
        const target = extractActivitySummaryTarget(obj);
        entries.push({
          kind: classifyToolActivityKind(name, evType),
          name,
          target,
          detail: JSON.stringify(obj, null, 2).slice(0, 1600)
        });
      }
    }

    // Also extract ```mesh-actions``` blocks from Lead output
    const actionBlockRe = /```mesh-actions\s*([\s\S]*?)```/g;
    let m;
    while ((m = actionBlockRe.exec(rawText)) !== null) {
      try {
        const parsed = JSON.parse(m[1].trim());
        const arr = Array.isArray(parsed) ? parsed : [];
        arr.forEach(act => {
          if (act && typeof act.cmd === "string") {
            entries.push({
              kind: "mesh-action",
              name: act.cmd,
              target: (act.args && typeof act.args === "object"
                ? String(act.args.title || act.args.text || act.args.plan_id || "")
                : "").slice(0, 90),
              detail: JSON.stringify(act, null, 2).slice(0, 1600)
            });
          }
        });
      } catch (_) {
        // Ignore partial streaming block
      }
    }

    return entries;
  }

  function summarizeActivityEntries(entries) {
    if (!Array.isArray(entries) || !entries.length) return "";
    const counts = { read: 0, edit: 0, command: 0, search: 0, "mesh-action": 0, thought: 0, other: 0 };
    entries.forEach(en => {
      const k = Object.prototype.hasOwnProperty.call(counts, en.kind) ? en.kind : "other";
      counts[k] += 1;
    });
    const parts = [];
    if (counts.read) parts.push(`读取 ${counts.read} 项`);
    if (counts.edit) parts.push(`编辑 ${counts.edit} 处`);
    if (counts.command) parts.push(`执行 ${counts.command} 条命令`);
    if (counts.search) parts.push(`搜索 ${counts.search} 次`);
    if (counts["mesh-action"]) parts.push(`${counts["mesh-action"]} 个协调动作`);
    if (counts.thought) parts.push(`${counts.thought} 条思考`);
    if (counts.other) parts.push(`${counts.other} 个工具调用`);
    return parts.join(" · ");
  }

  function renderGroupedActivityHtml(rawText, detailsPrefix, { emptyText = "(暂无输出)", showRawToggle = true } = {}) {
    const text = typeof rawText === "string" ? rawText : "";
    const optBag = arguments[2] && typeof arguments[2] === "object" ? arguments[2] : {};
    const structured = optBag.structuredActivity && typeof optBag.structuredActivity === "object" ? optBag.structuredActivity : null;
    const hasStructured = Boolean(structured && Array.isArray(structured.entries) && structured.entries.length > 0);
    const entries = hasStructured ? structured.entries : parseActivityFromLogText(text);
    const isTruncated = Boolean(hasStructured && structured.truncated);
    const mode = STATE.ui.logViewMode === "raw" ? "raw" : "grouped";
    const toggleHtml = showRawToggle ? `
      <div class="activity-summary-head">
        <span style="font-size:11.5px;color:var(--text-secondary);">${entries.length ? esc(summarizeActivityEntries(entries)) : "纯文本输出"}${isTruncated ? " · 已截断" : ""}</span>
        <div class="segmented">
          <button type="button" class="${mode === "grouped" ? "active" : ""}" data-log-view-mode="grouped">活动摘要</button>
          <button type="button" class="${mode === "raw" ? "active" : ""}" data-log-view-mode="raw">原始日志 (Raw)</button>
        </div>
      </div>` : "";

    if (mode === "raw" || !entries.length) {
      return `
        <div class="activity-summary-box">
          ${toggleHtml}
          <pre class="log-box" style="max-height:220px;">${esc(text || emptyText)}</pre>
        </div>`;
    }

    const summaryText = summarizeActivityEntries(entries);
    const rowsHtml = entries.map((en, idx) => {
      const kLabel = ACTIVITY_KIND_LABELS[en.kind] || "工具";
      const label = en.target ? `${en.name}: ${en.target}` : en.name;
      const statusBadge = en.status === "error"
        ? `<span class="status-pill red" style="margin-left:auto;font-size:10px;padding:0 5px;">错误</span>`
        : (en.status === "running" ? `<span class="status-pill blue" style="margin-left:auto;font-size:10px;padding:0 5px;">运行中</span>` : "");
      return `
        <details class="activity-entry-details" data-details-key="${esc(detailsPrefix)}:e:${idx}">
          <summary>
            <span class="activity-kind-badge ${esc(en.kind)}">${esc(kLabel)}</span>
            <span class="mono" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(label)}</span>
            ${statusBadge}
          </summary>
          <pre class="log-box" style="margin-top:6px;max-height:160px;">${esc(en.detail || label)}</pre>
        </details>`;
    }).join("");

    return `
      <div class="activity-summary-box">
        ${toggleHtml}
        <details class="activity-group-details" data-details-key="${esc(detailsPrefix)}:grp">
          <summary>${esc(summaryText)}（共 ${entries.length} 步${isTruncated ? "，已按上限截断" : ""}，点击展开工具调用）</summary>
          <div class="activity-entry-list">${rowsHtml}</div>
        </details>
      </div>`;
  }

  // Deterministic WCAG-AA opaque avatar palette (all >= 4.5:1 with #ffffff)
  const AVATAR_PALETTE = [
    "#2563eb", "#1e7e4f", "#6d4aff", "#b45309",
    "#0f766e", "#c9313d", "#7e22ce", "#be185d", "#475569"
  ];
  const ADAPTER_AVATAR_COLORS = {
    claude: "#6d4aff",
    droid: "#c9313d",
    codex: "#1e7e4f",
    agy: "#2563eb",
    kiro: "#b45309",
    devin: "#0f766e",
    vibe: "#7e22ce",
    hermes: "#be185d",
    openclaw: "#475569",
    none: "#475569"
  };

  function avatarPaletteColor(seed) {
    const key = String(seed || "").trim().toLowerCase();
    if (!key) return "#2563eb";
    if (Object.prototype.hasOwnProperty.call(ADAPTER_AVATAR_COLORS, key)) {
      return ADAPTER_AVATAR_COLORS[key];
    }
    for (const adName of Object.keys(ADAPTER_AVATAR_COLORS)) {
      if (adName !== "none" && key.includes(adName)) {
        return ADAPTER_AVATAR_COLORS[adName];
      }
    }
    let hash = 0;
    for (let i = 0; i < key.length; i += 1) {
      hash = ((hash * 31) + key.charCodeAt(i)) >>> 0;
    }
    return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
  }

  function avatarInitials(label, fallback = "AM") {
    const raw = String(label || "").trim();
    if (!raw) return fallback;
    const parts = raw.split(/[\s_\-/.:]+/).filter(Boolean);
    if (parts.length >= 2) {
      const a = parts[0].charAt(0);
      const b = parts[1].charAt(0);
      if (/[a-zA-Z0-9]/.test(a) && /[a-zA-Z0-9]/.test(b)) {
        return (a + b).toUpperCase();
      }
    }
    const alnum = raw.replace(/[^a-zA-Z0-9]/g, "");
    if (alnum.length >= 2) return alnum.slice(0, 2).toUpperCase();
    if (alnum.length === 1) return alnum.toUpperCase();
    return Array.from(raw).slice(0, 2).join("");
  }

  // Lightweight, 100% XSS-safe Markdown renderer for Lead bubbles
  function renderInlineMd(raw) {
    let s = esc(raw);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    return s;
  }

  function renderSafeMarkdown(rawText) {
    const text = String(rawText || "");
    if (!text) return "";
    const parts = text.split(/(```[\w-]*\n[\s\S]*?```)/g);
    return parts.map(part => {
      if (part.startsWith("```") && part.endsWith("```")) {
        const m = part.match(/^```([\w-]*)\n([\s\S]*?)```$/);
        const lang = (m && m[1]) || "code";
        const body = (m && m[2]) || "";
        return `<div class="md-code-wrap"><div class="md-code-head"><span class="mono">${esc(lang)}</span><button class="btn btn-sm btn-ghost" type="button" data-copy-code="${esc(body)}">复制</button></div><pre class="md-code-pre mono">${esc(body)}</pre></div>`;
      }
      const lines = part.split("\n");
      const out = [];
      let inUl = false;
      let inOl = false;
      const closeLists = () => {
        if (inUl) { out.push("</ul>"); inUl = false; }
        if (inOl) { out.push("</ol>"); inOl = false; }
      };
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
          closeLists();
          continue;
        }
        const hMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
        if (hMatch) {
          closeLists();
          out.push(`<h4>${renderInlineMd(hMatch[2])}</h4>`);
          continue;
        }
        const ulMatch = trimmed.match(/^[-*]\s+(.+)$/);
        if (ulMatch) {
          if (inOl) { out.push("</ol>"); inOl = false; }
          if (!inUl) { out.push("<ul>"); inUl = true; }
          out.push(`<li>${renderInlineMd(ulMatch[1])}</li>`);
          continue;
        }
        const olMatch = trimmed.match(/^\d+[.)]\s+(.+)$/);
        if (olMatch) {
          if (inUl) { out.push("</ul>"); inUl = false; }
          if (!inOl) { out.push("<ol>"); inOl = true; }
          out.push(`<li>${renderInlineMd(olMatch[1])}</li>`);
          continue;
        }
        closeLists();
        out.push(`<p>${renderInlineMd(trimmed)}</p>`);
      }
      closeLists();
      return out.join("");
    }).join("");
  }

  function updateWindowMaxButton(isMaximized) {
    const maxed = Boolean(isMaximized);
    STATE.ui.windowMaximized = maxed;
    if (document.body) {
      document.body.classList.toggle("window-maximized", maxed);
    }
    const dragEl = $("titlebarDrag");
    if (dragEl) {
      dragEl.classList.toggle("pywebview-drag-region", !maxed);
    }
    const btn = $("btnWinMax");
    if (!btn) return;
    btn.title = maxed ? "还原窗口" : "最大化窗口";
    btn.innerHTML = maxed
      ? `<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="4" width="12" height="12" rx="2"></rect><path d="M5 9v9a2 2 0 0 0 2 2h9"></path></svg>`
      : `<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="14" height="14" rx="2"></rect></svg>`;
  }

  function syncComposerTextareaHeight() {
    const ta = $("composerInput");
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(156, Math.max(40, ta.scrollHeight))}px`;
  }

  function scrollChatToBottom() {
    const sc = $("chatScroll");
    if (!sc) return;
    sc.scrollTop = sc.scrollHeight;
  }

  // =========================================================================
  // Bridge Transport Helpers
  // =========================================================================
  async function apiCall(method, params) {
    if (!hasApi()) {
      return { ok: false, error: { code: "NO_BRIDGE", message: "未检测到桌面桥接环境" } };
    }
    try {
      const res = await window.pywebview.api.call(method, params || {});
      if (res && !res.ok && res.error) {
        if (res.error.code === "DAEMON_UNAVAILABLE" || res.error.exit_code === 10) {
          STATE.connected = false;
          STATE.loadingWorkspace = false;
          closeModal();
          closeAllPopovers();
          renderAll();
        } else if (
          res.error.exit_code === 3 ||
          res.error.code === "TOKEN_MISSING" ||
          res.error.code === "PASSPHRASE_INVALID" ||
          res.error.code === "PASSPHRASE_REQUIRED" ||
          res.error.code === "TOKEN_INVALID" ||
          res.error.code === "LOCKED_OUT"
        ) {
          STATE.auth.auth_verified = false;
          STATE.auth.last_auth_error = res.error;
          STATE.loadingWorkspace = false;
          closeModal();
          closeAllPopovers();
          renderAll();
        }
      }
      return res || { ok: false, error: { code: "EMPTY_RESPONSE", message: "空响应" } };
    } catch (err) {
      return { ok: false, error: { code: "BRIDGE_ERROR", message: String(err) } };
    }
  }

  function applyConnectionSnapshot(snap) {
    if (!snap) return;
    STATE.connected = Boolean(snap.connected);
    STATE.daemonStarting = Boolean(snap.daemon_starting);
    STATE.daemonStopping = Boolean(snap.daemon_stopping);
    STATE.userStopped = Boolean(snap.user_stopped);
    STATE.auth.auth_configured = Boolean(snap.auth_configured);
    STATE.auth.auth_verified = Boolean(snap.auth_verified);
    STATE.pipe.home = snap.home || STATE.pipe.home;
    if (typeof snap.home_initialized === "boolean") {
      STATE.pipe.home_initialized = snap.home_initialized;
    }
    if (typeof snap.setup_hint === "string" && snap.setup_hint) {
      STATE.pipe.setup_hint = snap.setup_hint;
    }
    STATE.pipe.endpoint = snap.endpoint || STATE.pipe.endpoint;
    STATE.pipe.daemon_status = snap.daemon_status || null;
    STATE.pipe.daemon_version = snap.daemon_version || STATE.pipe.daemon_version;
    STATE.pipe.reconnect_attempt = snap.reconnect_attempt || 0;
    STATE.pipe.next_retry_s = snap.next_retry_s || 0;
    STATE.pipe.last_error = snap.last_error || null;
    STATE.pipe.last_start_error = snap.last_start_error || null;
    if (snap.daemon_log_path) STATE.pipe.daemon_log_path = snap.daemon_log_path;
    if (snap.desktop_log_path) STATE.pipe.desktop_log_path = snap.desktop_log_path;
    if (Array.isArray(snap.daemon_diag_log)) STATE.pipe.daemon_diag_log = snap.daemon_diag_log;
  }

  async function refreshConnectionAndAuth() {
    if (!hasApi()) return;
    const [connRes, authRes, pipeRes] = await Promise.all([
      window.pywebview.api.connection_state(),
      window.pywebview.api.auth_status(),
      window.pywebview.api.pipe_info()
    ]);
    if (connRes && connRes.ok) applyConnectionSnapshot(connRes.data);
    if (authRes && authRes.ok && authRes.data) STATE.auth = authRes.data;
    if (pipeRes && pipeRes.ok && pipeRes.data) {
      STATE.pipe.home = pipeRes.data.home || "";
      STATE.pipe.endpoint = pipeRes.data.endpoint || "";
      STATE.pipe.transport = pipeRes.data.transport || "";
      if (pipeRes.data.daemon_log_path) STATE.pipe.daemon_log_path = pipeRes.data.daemon_log_path;
      if (pipeRes.data.desktop_log_path) STATE.pipe.desktop_log_path = pipeRes.data.desktop_log_path;
    }
    if (typeof window.pywebview.api.desktop_prefs === "function") {
      try {
        const prefRes = await window.pywebview.api.desktop_prefs();
        if (prefRes && prefRes.ok && prefRes.data) {
          STATE.desktopPrefs = Object.assign({}, STATE.desktopPrefs, prefRes.data);
        }
      } catch (_) {
        // Keep defaults
      }
    }
  }

  async function bootstrapBridge() {
    applyTheme(STATE.ui.theme);
    if (!hasApi()) {
      renderAll();
      return;
    }
    STATE.bridgeReady = true;
    try {
      const caps = await window.pywebview.api.capabilities();
      if (caps && typeof caps === "object") {
        STATE.capabilities = Object.assign({}, STATE.capabilities, caps);
      }
    } catch (_) {
      // Keep defaults
    }

    if (typeof window.pywebview.api.home_status === "function") {
      try {
        const hs = await window.pywebview.api.home_status();
        if (hs && hs.ok && hs.data) {
          STATE.pipe.home = hs.data.home || STATE.pipe.home;
          STATE.pipe.home_initialized = Boolean(hs.data.home_initialized);
        }
      } catch (_) {
        // Fallback to connection_state
      }
    }

    startEventPollFallback();
    await drainPendingEvents();
    await refreshConnectionAndAuth();
    if (STATE.pipe.home_initialized === false) {
      openWizard(1);
      renderAll();
      return;
    }
    if (STATE.connected && STATE.auth.auth_verified) {
      STATE.loadingWorkspace = true;
      renderAll();
      await loadAllWorkspaceData();
    } else {
      renderAll();
    }
  }

  // =========================================================================
  // Data Loading & Enrichment
  // =========================================================================
  async function loadAdapters({ force = false } = {}) {
    if (!STATE.connected || !STATE.auth.auth_verified) return;
    if (STATE.adaptersLoading) return;
    if (STATE.adaptersLoaded && !force) return;
    STATE.adaptersLoading = true;
    try {
      const adapterRes = await apiCall("adapter.list", {});
      if (adapterRes && adapterRes.ok && adapterRes.data) {
        STATE.adapters = adapterRes.data.items || adapterRes.data.adapters || [];
        STATE.adaptersLoaded = true;
        if (STATE.ui.nav === "cli") renderCliView();
        else renderSidebar();
      }
    } finally {
      STATE.adaptersLoading = false;
    }
  }

  async function loadGrants({ includeRevoked } = {}) {
    if (!STATE.connected || !STATE.auth.auth_verified) return;
    if (typeof includeRevoked === "boolean") {
      STATE.grantsIncludeRevoked = includeRevoked;
    }
    const res = await apiCall("grant.list", { include_revoked: Boolean(STATE.grantsIncludeRevoked) });
    if (res && res.ok && res.data) {
      STATE.grants = Array.isArray(res.data.items) ? res.data.items : [];
    }
  }

  function collectOlderChatApprovalIds() {
    const newest = STATE.chatNewestPageApprovalIds instanceof Set ? STATE.chatNewestPageApprovalIds : new Set();
    const out = [];
    const seen = new Set();
    const addId = aid => {
      if (!aid || seen.has(aid) || newest.has(aid)) return;
      seen.add(aid);
      out.push(aid);
    };
    Object.keys(STATE.chatApprovalsById || {}).forEach(addId);
    for (const msg of STATE.chatMessages || []) {
      if (msg && msg.approval_id) addId(msg.approval_id);
      if (msg && msg.plan_id) {
        const pl = STATE.chatPlansById[msg.plan_id];
        if (pl && pl.approval_id) addId(pl.approval_id);
      }
    }
    return out.slice(0, 200);
  }

  function reconcileChatApprovalsWithList(listItems, { requestedOlderIds = [] } = {}) {
    const items = Array.isArray(listItems) ? listItems : [];
    const byId = new Map();
    for (const it of items) {
      if (it && it.id) byId.set(it.id, it);
    }
    Object.keys(STATE.chatApprovalsById || {}).forEach(aid => {
      const fresh = byId.get(aid);
      if (fresh && Array.isArray(fresh.available_actions)) {
        STATE.chatApprovalsById[aid] = Object.assign({}, STATE.chatApprovalsById[aid], fresh);
      }
    });
    if (Array.isArray(requestedOlderIds) && requestedOlderIds.length) {
      const newest = STATE.chatNewestPageApprovalIds instanceof Set ? STATE.chatNewestPageApprovalIds : new Set();
      for (const aid of requestedOlderIds) {
        if (newest.has(aid)) continue;
        if (!byId.has(aid) && STATE.chatApprovalsById[aid]) {
          delete STATE.chatApprovalsById[aid];
        }
      }
    }
  }

  async function fetchBoundedApprovals({ includeIds = [] } = {}) {
    const args = {
      actionable_only: true,
      include_card_fields: true,
      limit: APPROVAL_LIST_LIMIT
    };
    if (Array.isArray(includeIds) && includeIds.length) {
      args.include_ids = includeIds.slice(0, 200);
    }
    return apiCall("approval.list", args);
  }

  async function fetchBoundedRuns(activeProjectId = null) {
    const calls = [
      apiCall("run.list", { statuses: ["starting", "running"], limit: ACTIVE_RUN_LIST_LIMIT })
    ];
    if (activeProjectId) {
      calls.push(apiCall("run.list", { project: activeProjectId, limit: PROJECT_RUN_LIST_LIMIT }));
    }
    const [activeRes, prjRes] = await Promise.all(calls);
    const mergedById = new Map();
    if (prjRes && prjRes.ok && prjRes.data) {
      const prjRuns = prjRes.data.runs || prjRes.data.items || [];
      for (const r of prjRuns) {
        if (r && r.id) mergedById.set(r.id, r);
      }
    }
    if (activeRes && activeRes.ok && activeRes.data) {
      const actRuns = activeRes.data.runs || activeRes.data.items || [];
      for (const r of actRuns) {
        if (r && r.id) mergedById.set(r.id, r);
      }
    }
    const ok = Boolean((activeRes && activeRes.ok) || (prjRes && prjRes.ok));
    return {
      ok,
      data: { runs: Array.from(mergedById.values()) }
    };
  }

  async function loadAllWorkspaceData({ includeAdapters = false } = {}) {
    const seq = ++workspaceLoadSeq;
    bumpSidebarLoadSeq();
    const switchGen = projectSwitchGen;
    if (!STATE.connected || !STATE.auth.auth_verified) {
      STATE.loadingWorkspace = false;
      renderAll();
      return;
    }
    if (!STATE.hasLoadedWorkspace) {
      STATE.loadingWorkspace = true;
    }

    const curPid = STATE.ui.draftNewChat ? null : STATE.activeProjectId;
    const olderIds = collectOlderChatApprovalIds();
    const [prjRes, agentRes, cfgRes, aprRes, runRes, grantRes] = await Promise.all([
      apiCall("project.list", {}),
      apiCall("agent.list", {}),
      apiCall("config.list", {}),
      fetchBoundedApprovals({ includeIds: olderIds }),
      fetchBoundedRuns(curPid),
      apiCall("grant.list", { include_revoked: Boolean(STATE.grantsIncludeRevoked) })
    ]);
    if (seq !== workspaceLoadSeq || !STATE.connected || !STATE.auth.auth_verified) return;
    bumpSidebarLoadSeq();

    if (prjRes.ok && prjRes.data) {
      STATE.projects = prjRes.data.items || prjRes.data.projects || [];
    }
    if (agentRes.ok && agentRes.data) {
      STATE.agents = agentRes.data.items || agentRes.data.agents || [];
    }
    if (cfgRes.ok && cfgRes.data) {
      const list = cfgRes.data.settings || cfgRes.data.items || [];
      STATE.configItems = list;
      const cfgMap = {};
      list.forEach(item => { cfgMap[item.key] = item.value; });
      STATE.config = cfgMap;
    }
    if (aprRes.ok && aprRes.data) {
      setApprovals(aprRes.data.items || []);
      if (switchGen === projectSwitchGen) {
        reconcileChatApprovalsWithList(STATE.approvals, { requestedOlderIds: olderIds });
      }
    }
    if (runRes.ok && runRes.data) {
      setRuns(runRes.data.runs || runRes.data.items || []);
    }
    if (grantRes.ok && grantRes.data) {
      STATE.grants = Array.isArray(grantRes.data.items) ? grantRes.data.items : [];
    }
    syncSidebarWorkingTracker();

    // Choose active project unless user explicitly opened a blank new chat draft or switched projects mid-load
    if (switchGen === projectSwitchGen && !STATE.ui.draftNewChat) {
      if (!STATE.activeProjectId || !STATE.projects.some(p => p.id === STATE.activeProjectId)) {
        const savedPid = getSavedActiveProjectForHome();
        const savedMatch = savedPid ? STATE.projects.find(p => p.id === savedPid) : null;
        const active = savedMatch || STATE.projects.find(p => p.status === "active") || STATE.projects[0];
        STATE.activeProjectId = active ? active.id : null;
      }
      if (STATE.activeProjectId) rememberActiveProjectForHome(STATE.activeProjectId);
    }

    await loadActiveProjectData();
    if (seq !== workspaceLoadSeq || !STATE.connected || !STATE.auth.auth_verified) return;
    STATE.loadingWorkspace = false;
    STATE.hasLoadedWorkspace = true;

    // S2: if initialized & unlocked with zero agents (and user has not skipped), open Step 2 "Pick your Lead"
    if (STATE.agents.length === 0 && !STATE.wizard.skipped && !STATE.wizard.active) {
      openWizard(2);
    }
    renderAll();

    // Only call adapter.list once on first load (outside wizard), on AI Tools view, or when explicitly requested
    const shouldFetchAdapters = Boolean(
      includeAdapters ||
      (!STATE.adaptersLoaded && !STATE.adaptersLoading && (STATE.ui.nav === "cli" || (STATE.agents.length > 0 && !STATE.wizard.active)))
    );
    if (shouldFetchAdapters) {
      loadAdapters({ force: Boolean(includeAdapters) });
    }
    await enrichAgentCapabilities();
    if (seq !== workspaceLoadSeq || !STATE.connected || !STATE.auth.auth_verified) return;
    renderAll();
  }

  // Lightweight sidebar-only refresh for non-active project events (3 bounded RPCs)
  async function loadSidebarLight() {
    if (!STATE.connected || !STATE.auth.auth_verified) return;
    const sbSeq = bumpSidebarLoadSeq();
    const switchGen = projectSwitchGen;
    const [prjRes, aprRes, activeRunRes] = await Promise.all([
      apiCall("project.list", {}),
      fetchBoundedApprovals(),
      apiCall("run.list", { statuses: ["starting", "running"], limit: ACTIVE_RUN_LIST_LIMIT })
    ]);
    if (sbSeq !== sidebarLoadSeq || !STATE.connected || !STATE.auth.auth_verified) return;
    const curActivePid = STATE.ui.draftNewChat ? null : STATE.activeProjectId;
    if (prjRes.ok && prjRes.data) {
      STATE.projects = prjRes.data.items || prjRes.data.projects || [];
    }
    if (aprRes.ok && aprRes.data) {
      setApprovals(aprRes.data.items || []);
      if (switchGen === projectSwitchGen) {
        reconcileChatApprovalsWithList(STATE.approvals);
      }
    }
    if (activeRunRes.ok && activeRunRes.data) {
      const freshActive = activeRunRes.data.runs || activeRunRes.data.items || [];
      const mergedById = new Map();
      // Keep recent completed runs of the CURRENT active project while replacing global active runs
      for (const r of STATE.runs || []) {
        if (!r || !r.id) continue;
        if (curActivePid && r.project_id === curActivePid && r.status !== "starting" && r.status !== "running") {
          mergedById.set(r.id, r);
        }
      }
      for (const r of freshActive) {
        if (r && r.id) mergedById.set(r.id, r);
      }
      setRuns(Array.from(mergedById.values()));
    }
    syncSidebarWorkingTracker();
    renderSidebar();
  }

  function mergeBoundedEvents(existingEvents, seenSeqs, minRetainedSeq, incomingEvents, maxRetained = MAX_RETAINED_EVENTS) {
    const events = Array.isArray(existingEvents) ? existingEvents : [];
    const seen = seenSeqs instanceof Set ? seenSeqs : new Set();
    let minSeq = typeof minRetainedSeq === "number" && minRetainedSeq > 0 ? minRetainedSeq : 0;
    let maxSeq = events.length && typeof events[events.length - 1].seq === "number"
      ? events[events.length - 1].seq
      : 0;
    let needsSort = false;
    const list = Array.isArray(incomingEvents) ? incomingEvents : (incomingEvents ? [incomingEvents] : []);
    for (const ev of list) {
      if (!ev || typeof ev !== "object") continue;
      const seq = typeof ev.seq === "number" ? ev.seq : null;
      if (seq !== null) {
        if (seen.has(seq)) continue;
        if (events.length >= maxRetained && minSeq > 0 && seq < minSeq) continue;
        seen.add(seq);
        if (seq > maxSeq) maxSeq = seq;
        if (events.length > 0) {
          const lastSeq = typeof events[events.length - 1].seq === "number" ? events[events.length - 1].seq : 0;
          if (seq < lastSeq) needsSort = true;
        }
      }
      events.push(ev);
    }
    if (needsSort) {
      events.sort((a, b) => (a.seq || 0) - (b.seq || 0));
    }
    if (events.length > maxRetained) {
      const dropped = events.splice(0, events.length - maxRetained);
      for (const d of dropped) {
        if (d && typeof d.seq === "number") seen.delete(d.seq);
      }
    }
    if (events.length > 0 && typeof events[0].seq === "number") {
      minSeq = events[0].seq;
    }
    return { events, seenSeqs: seen, minRetainedSeq: minSeq, maxSeenEventSeq: maxSeq };
  }

  function appendProjectEventsBounded(incomingEvents) {
    const merged = mergeBoundedEvents(
      STATE.events,
      STATE.seenSeqs,
      STATE.minRetainedSeq,
      incomingEvents,
      MAX_RETAINED_EVENTS
    );
    STATE.events = merged.events;
    STATE.seenSeqs = merged.seenSeqs;
    STATE.minRetainedSeq = merged.minRetainedSeq;
    if (merged.maxSeenEventSeq > STATE.maxSeenEventSeq) {
      STATE.maxSeenEventSeq = merged.maxSeenEventSeq;
    }
  }

  async function fetchProjectEventsTail(pid, initialSince) {
    const params = { project: pid, limit: EVENT_TAIL_FETCH_LIMIT };
    if (typeof initialSince === "number" && initialSince > 0) {
      params.since = initialSince;
    }
    const res = await apiCall("event.list", params);
    if (!res || !res.ok || !res.data) {
      return { ok: false, error: res && res.error, items: [] };
    }
    const items = Array.isArray(res.data.items) ? res.data.items : [];
    return { ok: true, items: items.slice(-MAX_RETAINED_EVENTS) };
  }

  function mergeChatPage(resData, { isOlder = false } = {}) {
    if (!resData || typeof resData !== "object") return;
    const incoming = Array.isArray(resData.items) ? resData.items : [];
    const hadExisting = STATE.chatMessages.length > 0;
    const byKey = new Map();
    STATE.chatMessages.forEach(m => {
      if (m) byKey.set(m.id || `seq:${m.event_seq}`, m);
    });
    incoming.forEach(m => {
      if (m) byKey.set(m.id || `seq:${m.event_seq}`, m);
    });
    STATE.chatMessages = Array.from(byKey.values()).sort((a, b) => (a.event_seq || 0) - (b.event_seq || 0));

    let oldest = null;
    for (const m of STATE.chatMessages) {
      if (m && typeof m.event_seq === "number" && (oldest === null || m.event_seq < oldest)) {
        oldest = m.event_seq;
      }
    }
    STATE.chatOldestSeq = oldest;

    if (isOlder || !hadExisting) {
      STATE.chatHasMore = Boolean(resData.has_more);
    }
    if (resData.plans_by_id && typeof resData.plans_by_id === "object") {
      STATE.chatPlansById = Object.assign({}, STATE.chatPlansById, resData.plans_by_id);
    }
    if (resData.approvals_by_id && typeof resData.approvals_by_id === "object") {
      STATE.chatApprovalsById = Object.assign({}, STATE.chatApprovalsById, resData.approvals_by_id);
      if (!isOlder) {
        STATE.chatNewestPageApprovalIds = new Set(Object.keys(resData.approvals_by_id));
      }
    } else if (!isOlder) {
      STATE.chatNewestPageApprovalIds = new Set();
    }
    if (!isOlder) {
      STATE.activeLeadRun = resData.active_lead_run || null;
      if (typeof resData.project_status === "string" && resData.project_status) {
        STATE.chatProjectStatus = resData.project_status;
        if (STATE.activeProjectDetail && STATE.activeProjectDetail.id === resData.project_id) {
          STATE.activeProjectDetail = Object.assign({}, STATE.activeProjectDetail, {
            status: resData.project_status
          });
        }
        STATE.projects = STATE.projects.map(p => (
          p && p.id === resData.project_id
            ? Object.assign({}, p, { status: resData.project_status })
            : p
        ));
      }
      if (typeof resData.plan_gate_mode === "string" && resData.plan_gate_mode) {
        STATE.chatPlanGateMode = resData.plan_gate_mode;
        if (STATE.activeProjectDetail && STATE.activeProjectDetail.id === resData.project_id) {
          STATE.activeProjectDetail = Object.assign({}, STATE.activeProjectDetail, {
            plan_gate_mode: resData.plan_gate_mode
          });
        }
        STATE.projects = STATE.projects.map(p => (
          p && p.id === resData.project_id
            ? Object.assign({}, p, { plan_gate_mode: resData.plan_gate_mode })
            : p
        ));
      }
      if (typeof resData.permission_mode === "string" && resData.permission_mode) {
        STATE.chatPermissionMode = resData.permission_mode;
        if (STATE.activeProjectDetail && STATE.activeProjectDetail.id === resData.project_id) {
          STATE.activeProjectDetail = Object.assign({}, STATE.activeProjectDetail, {
            permission_mode: resData.permission_mode
          });
        }
        STATE.projects = STATE.projects.map(p => (
          p && p.id === resData.project_id
            ? Object.assign({}, p, { permission_mode: resData.permission_mode })
            : p
        ));
      }
      if (Object.prototype.hasOwnProperty.call(resData, "lead_status")) {
        STATE.chatLeadStatus = resData.lead_status || null;
      }
      if (Array.isArray(resData.queued_message_ids) && resData.project_id) {
        STATE.queuedUserMsgIdsByProject[resData.project_id] = new Set(resData.queued_message_ids);
      }
    }
  }

  async function loadOlderChatMessages() {
    const pid = STATE.ui.draftNewChat ? null : STATE.activeProjectId;
    const switchGen = projectSwitchGen;
    if (!pid || !STATE.connected || !STATE.auth.auth_verified) return;
    if (!STATE.chatHasMore || STATE.chatLoadingOlder || !STATE.chatOldestSeq) return;
    STATE.chatLoadingOlder = true;
    renderChatThread();
    const sc = $("chatScroll");
    const prevHeight = sc ? sc.scrollHeight : 0;
    const prevTop = sc ? sc.scrollTop : 0;
    try {
      const res = await apiCall("chat.list", { project_id: pid, limit: 150, before_seq: STATE.chatOldestSeq });
      if (switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || STATE.ui.draftNewChat) return;
      if (res && res.ok && res.data) {
        mergeChatPage(res.data, { isOlder: true });
      } else if (res && !res.ok) {
        toast(formatErrorMessage(res.error), "err");
      }
    } finally {
      STATE.chatLoadingOlder = false;
      if (switchGen === projectSwitchGen && pid === STATE.activeProjectId && !STATE.ui.draftNewChat) {
        renderChatThread();
        if (sc) {
          const delta = sc.scrollHeight - prevHeight;
          sc.scrollTop = Math.max(0, prevTop + delta);
        }
      }
    }
  }

  async function loadActiveProjectData() {
    const pid = STATE.ui.draftNewChat ? null : STATE.activeProjectId;
    const projSeq = ++projectLoadSeq;
    bumpSidebarLoadSeq();
    const switchGen = projectSwitchGen;
    if (!pid || !STATE.connected || !STATE.auth.auth_verified) {
      STATE.activeProjectDetail = null;
      STATE.chatProjectStatus = null;
      STATE.chatPlanGateMode = "require_approval";
      STATE.chatPermissionMode = "plan";
      STATE.chatMessages = [];
      STATE.chatHasMore = false;
      STATE.chatOldestSeq = null;
      STATE.chatLoadingOlder = false;
      STATE.chatPlansById = {};
      STATE.chatApprovalsById = {};
      STATE.chatNewestPageApprovalIds = new Set();
      STATE.chatLeadStatus = null;
      STATE.activeLeadRun = null;
      STATE.plans = [];
      STATE.tasks = [];
      STATE.activeTaskId = null;
      STATE.taskDetails = {};
      STATE.taskWorkspaces = {};
      STATE.taskLogs = {};
      STATE.taskActivities = {};
      STATE.taskDiffs = {};
      STATE.taskDeps = {};
      STATE.leadTurnLogs = {};
      STATE.events = [];
      STATE.eventsProjectId = null;
      STATE.minRetainedSeq = 0;
      STATE.maxSeenEventSeq = 0;
      STATE.seenSeqs = new Set();
      stripApprovalOrder = [];
      stripOrderProjectId = null;
      inspectorApprovalOrder = [];
      inspectorOrderProjectId = null;
      clearRecentlyResolvedApprovals();
      resetApprovalSurfaceGuards();
      return;
    }

    const isSameProject = STATE.eventsProjectId === pid;
    if (!isSameProject) {
      STATE.activeProjectDetail = null;
      STATE.chatProjectStatus = null;
      STATE.chatPlanGateMode = "require_approval";
      STATE.chatPermissionMode = "plan";
      STATE.chatMessages = [];
      STATE.chatHasMore = false;
      STATE.chatOldestSeq = null;
      STATE.chatLoadingOlder = false;
      STATE.chatPlansById = {};
      STATE.chatApprovalsById = {};
      STATE.chatNewestPageApprovalIds = new Set();
      STATE.chatLeadStatus = null;
      STATE.activeLeadRun = null;
      STATE.plans = [];
      STATE.tasks = [];
      STATE.activeTaskId = null;
      STATE.events = [];
      STATE.eventsProjectId = pid;
      STATE.minRetainedSeq = 0;
      STATE.maxSeenEventSeq = 0;
      STATE.seenSeqs = new Set();
      STATE.taskDetails = {};
      STATE.taskWorkspaces = {};
      STATE.taskLogs = {};
      STATE.taskActivities = {};
      STATE.taskDiffs = {};
      STATE.taskDeps = {};
      STATE.leadTurnLogs = {};
      lastChatThreadHtml = null;
      lastChatMsgCount = 0;
      lastChatTailId = null;
      stripApprovalOrder = [];
      stripOrderProjectId = pid;
      inspectorApprovalOrder = [];
      inspectorOrderProjectId = pid;
      clearRecentlyResolvedApprovals();
      resetApprovalSurfaceGuards();
    }

    const initialSince = (isSameProject && STATE.maxSeenEventSeq > 0) ? STATE.maxSeenEventSeq : 0;
    const olderIds = isSameProject ? collectOlderChatApprovalIds() : [];

    const coreCalls = [
      apiCall("project.show", { project_id: pid, history: true }),
      apiCall("chat.list", { project_id: pid, limit: 150 }),
      apiCall("plan.list", { project_id: pid }),
      apiCall("task.list", { project: pid }),
      fetchBoundedApprovals({ includeIds: olderIds }),
      fetchBoundedRuns(pid)
    ];
    if (initialSince > 0) {
      coreCalls.push(fetchProjectEventsTail(pid, initialSince));
    }
    const [showRes, chatRes, planRes, taskRes, aprRes, runRes, initialEvRes] = await Promise.all(coreCalls);
    if (projSeq !== projectLoadSeq || switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || STATE.ui.draftNewChat || !STATE.connected || !STATE.auth.auth_verified) return;

    let evRes = initialEvRes || null;
    if (!evRes) {
      let anchorSeq = 0;
      if (chatRes.ok && chatRes.data && Array.isArray(chatRes.data.items)) {
        for (const m of chatRes.data.items) {
          if (m && typeof m.event_seq === "number" && m.event_seq > anchorSeq) anchorSeq = m.event_seq;
        }
      }
      if (planRes.ok && planRes.data && Array.isArray(planRes.data.items)) {
        for (const p of planRes.data.items) {
          if (p && typeof p.proposed_event_seq === "number" && p.proposed_event_seq > anchorSeq) {
            anchorSeq = p.proposed_event_seq;
          }
        }
      }
      const tailSince = anchorSeq > EVENT_TAIL_FETCH_LIMIT ? (anchorSeq - EVENT_TAIL_FETCH_LIMIT) : 0;
      evRes = await fetchProjectEventsTail(pid, tailSince);
      if (projSeq !== projectLoadSeq || switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || STATE.ui.draftNewChat || !STATE.connected || !STATE.auth.auth_verified) return;
    }

    bumpSidebarLoadSeq();
    if (showRes.ok && showRes.data) {
      STATE.activeProjectDetail = showRes.data;
    }
    if (chatRes.ok && chatRes.data) {
      mergeChatPage(chatRes.data, { isOlder: false });
    }
    if (planRes.ok && planRes.data) {
      STATE.plans = Array.isArray(planRes.data.items) ? planRes.data.items : [];
      STATE.plans.forEach(p => {
        if (p && p.id) STATE.chatPlansById[p.id] = p;
      });
    }
    if (taskRes.ok && taskRes.data) {
      STATE.tasks = taskRes.data.items || [];
      const freshById = new Map(STATE.tasks.map(t => [t.id, t]));
      Object.keys(STATE.taskDetails).forEach(tid => {
        const fresh = freshById.get(tid);
        const cached = STATE.taskDetails[tid];
        if (!fresh || !cached || cached.version !== fresh.version || cached.status !== fresh.status) {
          delete STATE.taskDetails[tid];
        }
      });
      if (!STATE.activeTaskId || !STATE.tasks.some(t => t.id === STATE.activeTaskId)) {
        STATE.activeTaskId = STATE.tasks[0] ? STATE.tasks[0].id : null;
      }
    }
    if (evRes && Array.isArray(evRes.items) && evRes.items.length > 0) {
      appendProjectEventsBounded(evRes.items);
    }
    if (aprRes.ok && aprRes.data) {
      setApprovals(aprRes.data.items || []);
      reconcileChatApprovalsWithList(STATE.approvals, { requestedOlderIds: olderIds });
    }
    if (runRes.ok && runRes.data) {
      setRuns(runRes.data.runs || runRes.data.items || []);
    }
    syncSidebarWorkingTracker();

    if (!STATE.ui.inspectorCollapsed && STATE.ui.inspectorTab === "outline") {
      await fetchVisibleLeadTurnLogs();
    } else if (!STATE.ui.inspectorCollapsed && STATE.ui.inspectorTab === "diff") {
      await fetchActiveDiffData();
    }
    if (projSeq !== projectLoadSeq || switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || !STATE.connected || !STATE.auth.auth_verified) return;
    if (!STATE.ui.inspectorCollapsed && STATE.activeTaskId && STATE.ui.inspectorTab === "task") {
      await loadActiveTaskInspectorData(STATE.activeTaskId);
    }
  }

  // Lightweight event-driven refresh for active-project events (no adapter.list / CLI probing)
  async function loadActiveProjectLight() {
    const pid = STATE.ui.draftNewChat ? null : STATE.activeProjectId;
    if (!pid || !STATE.connected || !STATE.auth.auth_verified) return;
    // Never merge into a different project's cached transcript/cards; fall back to full project load
    if (STATE.eventsProjectId !== pid) {
      await loadActiveProjectData();
      renderAll();
      return;
    }
    const projSeq = ++projectLoadSeq;
    bumpSidebarLoadSeq();
    const switchGen = projectSwitchGen;
    const olderIds = collectOlderChatApprovalIds();
    const [chatRes, planRes, taskRes, aprRes, runRes] = await Promise.all([
      apiCall("chat.list", { project_id: pid, limit: 150 }),
      apiCall("plan.list", { project_id: pid }),
      apiCall("task.list", { project: pid }),
      fetchBoundedApprovals({ includeIds: olderIds }),
      fetchBoundedRuns(pid)
    ]);
    if (projSeq !== projectLoadSeq || switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || STATE.ui.draftNewChat || STATE.eventsProjectId !== pid || !STATE.connected || !STATE.auth.auth_verified) {
      return;
    }

    bumpSidebarLoadSeq();
    if (chatRes.ok && chatRes.data) {
      mergeChatPage(chatRes.data, { isOlder: false });
    }
    if (planRes.ok && planRes.data) {
      STATE.plans = Array.isArray(planRes.data.items) ? planRes.data.items : [];
      STATE.plans.forEach(p => {
        if (p && p.id) STATE.chatPlansById[p.id] = p;
      });
    }
    if (taskRes.ok && taskRes.data) {
      STATE.tasks = taskRes.data.items || [];
      const freshById = new Map(STATE.tasks.map(t => [t.id, t]));
      Object.keys(STATE.taskDetails).forEach(tid => {
        const fresh = freshById.get(tid);
        const cached = STATE.taskDetails[tid];
        if (!fresh || !cached || cached.version !== fresh.version || cached.status !== fresh.status) {
          delete STATE.taskDetails[tid];
        }
      });
      if (!STATE.activeTaskId || !STATE.tasks.some(t => t.id === STATE.activeTaskId)) {
        STATE.activeTaskId = STATE.tasks[0] ? STATE.tasks[0].id : null;
      }
    }
    if (aprRes.ok && aprRes.data) {
      setApprovals(aprRes.data.items || []);
      reconcileChatApprovalsWithList(STATE.approvals, { requestedOlderIds: olderIds });
    }
    if (runRes.ok && runRes.data) {
      setRuns(runRes.data.runs || runRes.data.items || []);
    }
    syncSidebarWorkingTracker();

    if (!STATE.ui.inspectorCollapsed && STATE.ui.inspectorTab === "outline") {
      await fetchVisibleLeadTurnLogs();
    }
    if (projSeq !== projectLoadSeq || switchGen !== projectSwitchGen || pid !== STATE.activeProjectId || !STATE.connected || !STATE.auth.auth_verified) return;
    if (!STATE.ui.inspectorCollapsed && STATE.activeTaskId && STATE.ui.inspectorTab === "task") {
      await loadActiveTaskInspectorData(STATE.activeTaskId);
    }
    renderAll();
  }

  async function enrichAgentCapabilities() {
    const validNames = new Set(STATE.agents.map(a => a && a.name).filter(Boolean));
    Object.keys(STATE.agentDetails).forEach(n => {
      if (!validNames.has(n)) delete STATE.agentDetails[n];
    });
    const names = STATE.agents
      .filter(a => {
        if (!a || !a.name) return false;
        const det = STATE.agentDetails[a.name];
        return !det || det.version !== a.version || det.disabled !== a.disabled;
      })
      .map(a => a.name);
    if (!names.length) return;
    await Promise.all(
      names.map(async name => {
        const res = await apiCall("agent.show", { name });
        if (res.ok && res.data) {
          STATE.agentDetails[name] = res.data;
        }
      })
    );
  }

  async function fetchVisibleLeadTurnLogs() {
    if (!STATE.connected || !STATE.auth.auth_verified) return;
    const pid = STATE.activeProjectId;
    if (!pid || STATE.ui.draftNewChat) return;
    const seq = ++leadOutlineSeq;
    const sentGen = projectSwitchGen;
    const leadRuns = STATE.runs.filter(r => r.project_id === pid && r.kind === "lead_turn");
    await Promise.all(
      leadRuns.slice(-10).map(async run => {
        const isTerminal = Boolean(run.ended_at);
        if (
          isTerminal &&
          Object.prototype.hasOwnProperty.call(STATE.leadTurnLogs, run.id) &&
          Object.prototype.hasOwnProperty.call(STATE.runActivities, run.id)
        ) {
          return;
        }
        const [res, actRes] = await Promise.all([
          apiCall("run.log", { run_id: run.id, tail: 160 }),
          apiCall("run.activity", { run_id: run.id, limit: 150 })
        ]);
        if (
          seq !== leadOutlineSeq ||
          sentGen !== projectSwitchGen ||
          pid !== STATE.activeProjectId ||
          STATE.ui.draftNewChat ||
          !STATE.connected ||
          !STATE.auth.auth_verified
        ) {
          return;
        }
        if (res.ok && res.data && res.data.streams) {
          const outLines = (res.data.streams.stdout && res.data.streams.stdout.lines) || [];
          const errLines = (res.data.streams.stderr && res.data.streams.stderr.lines) || [];
          const sections = [];
          if (outLines.length) sections.push(outLines.join("\n").trim());
          if (errLines.length) sections.push(`[stderr]\n${errLines.join("\n").trim()}`);
          STATE.leadTurnLogs[run.id] = sections.filter(Boolean).join("\n\n");
        }
        if (actRes.ok && actRes.data) {
          STATE.runActivities[run.id] = actRes.data;
        }
      })
    );
  }

  async function fetchActiveDiffData({ scope = null, taskId = null, projectId = null } = {}) {
    if (!STATE.connected || !STATE.auth.auth_verified) return null;
    const prj = getActiveProject();
    const pid = projectId || (prj && prj.id) || STATE.activeProjectId;
    if (!pid || STATE.ui.draftNewChat) return null;
    const activeTask = taskId ? STATE.tasks.find(t => t.id === taskId) : getActiveTask();
    let effScope = scope || STATE.ui.diffScope || "task";
    if (effScope === "task" && (!activeTask || (activeTask.kind || "general") !== "code")) {
      effScope = "project";
    }
    const seq = ++diffLoadSeq;
    const sentGen = projectSwitchGen;
    if (effScope === "task" && activeTask) {
      const res = await apiCall("task.diff", { task_id: activeTask.id });
      if (
        seq !== diffLoadSeq ||
        sentGen !== projectSwitchGen ||
        pid !== STATE.activeProjectId ||
        STATE.ui.draftNewChat ||
        !STATE.connected ||
        !STATE.auth.auth_verified
      ) {
        return null;
      }
      if (res.ok && res.data) {
        STATE.taskDiffs[activeTask.id] = res.data;
        return res.data;
      }
      STATE.taskDiffs[activeTask.id] = { available: false, error: res.error, files: [], stat: { files_changed: 0, additions: 0, deletions: 0 } };
      return STATE.taskDiffs[activeTask.id];
    }
    if (pid) {
      const res = await apiCall("project.diff", { project_id: pid });
      if (
        seq !== diffLoadSeq ||
        sentGen !== projectSwitchGen ||
        pid !== STATE.activeProjectId ||
        STATE.ui.draftNewChat ||
        !STATE.connected ||
        !STATE.auth.auth_verified
      ) {
        return null;
      }
      if (res.ok && res.data) {
        STATE.projectDiffs[pid] = res.data;
        return res.data;
      }
      STATE.projectDiffs[pid] = { available: false, error: res.error, files: [], stat: { files_changed: 0, additions: 0, deletions: 0 } };
      return STATE.projectDiffs[pid];
    }
    return null;
  }

  async function loadActiveTaskInspectorData(taskId, { logsOnly = false } = {}) {
    if (!taskId || !STATE.connected || !STATE.auth.auth_verified) return;
    const pid = STATE.activeProjectId;
    const seq = ++taskDetailSeq;
    const sentGen = projectSwitchGen;
    if (!logsOnly) {
      const [showRes, depRes, wsRes] = await Promise.all([
        apiCall("task.show", { task_id: taskId }),
        apiCall("dep.list", { task_id: taskId }),
        apiCall("workspace.show", { task_id: taskId })
      ]);
      if (
        seq !== taskDetailSeq ||
        sentGen !== projectSwitchGen ||
        (pid && pid !== STATE.activeProjectId) ||
        STATE.ui.draftNewChat ||
        !STATE.connected ||
        !STATE.auth.auth_verified
      ) {
        return;
      }
      if (showRes.ok && showRes.data) STATE.taskDetails[taskId] = showRes.data;
      if (depRes.ok && depRes.data) STATE.taskDeps[taskId] = depRes.data;
      if (wsRes.ok && wsRes.data) STATE.taskWorkspaces[taskId] = wsRes.data;
    }
    if (sentGen !== projectSwitchGen || !STATE.connected || !STATE.auth.auth_verified) return;
    await fetchTaskLogs(taskId);
  }

  async function fetchTaskLogs(taskId) {
    if (!taskId || !STATE.connected || !STATE.auth.auth_verified) return;
    const pid = STATE.activeProjectId;
    const seq = ++taskLogSeq;
    const sentGen = projectSwitchGen;
    const stream = STATE.ui.logStreamFilter;
    const runId = STATE.ui.logRunId;
    const cacheKey = `${taskId}:${runId || "latest"}:${stream}`;
    const actCacheKey = `${taskId}:${runId || "latest"}`;

    const logPromise = runId
      ? apiCall("run.log", stream !== "all" ? { run_id: runId, tail: 300, stream } : { run_id: runId, tail: 300 })
      : apiCall("task.log", stream !== "all" ? { task_id: taskId, tail: 300, stream } : { task_id: taskId, tail: 300 });
    const actPromise = runId
      ? apiCall("run.activity", { run_id: runId, limit: 200 })
      : apiCall("run.activity", { task_id: taskId, limit: 200 });

    const [res, actRes] = await Promise.all([logPromise, actPromise]);
    if (
      seq !== taskLogSeq ||
      sentGen !== projectSwitchGen ||
      (pid && pid !== STATE.activeProjectId) ||
      STATE.ui.draftNewChat ||
      !STATE.connected ||
      !STATE.auth.auth_verified
    ) {
      return;
    }
    if (res.ok && res.data) {
      STATE.taskLogs[cacheKey] = res.data;
    } else {
      STATE.taskLogs[cacheKey] = { error: res.error };
    }
    if (actRes.ok && actRes.data) {
      STATE.taskActivities[actCacheKey] = actRes.data;
      if (actRes.data.run_id) STATE.runActivities[actRes.data.run_id] = actRes.data;
    }
  }

  // =========================================================================
  // Real-Time Event Handling (event.tail push + poll fallback)
  // =========================================================================
  async function drainPendingEvents() {
    if (eventPollInFlight || !hasApi() || typeof window.pywebview.api.poll_events !== "function") {
      return;
    }
    eventPollInFlight = true;
    try {
      for (let i = 0; i < 5; i += 1) {
        const res = await window.pywebview.api.poll_events(200);
        if (!res || !res.ok || !res.data) break;
        const items = Array.isArray(res.data.items) ? res.data.items : [];
        if (items.length > 0) {
          handleIncomingEvents(items);
        }
        if (!res.data.has_more) break;
      }
    } catch (_) {
      // Ignore transient poll errors; next interval will retry
    } finally {
      eventPollInFlight = false;
    }
  }

  function startEventPollFallback() {
    if (eventPollTimer) return;
    eventPollTimer = window.setInterval(drainPendingEvents, 1500);
  }

  async function flushScheduledRefresh() {
    if (refreshTimer) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    if (refreshMaxWaitTimer) {
      window.clearTimeout(refreshMaxWaitTimer);
      refreshMaxWaitTimer = null;
    }
    const runFlags = pendingRefreshFlags;
    pendingRefreshFlags = { full: false, project: false, sidebar: false, adapters: false };
    if (runFlags.full) {
      await loadAllWorkspaceData({ includeAdapters: runFlags.adapters });
    } else if (runFlags.project && runFlags.sidebar) {
      await loadSidebarLight();
      await loadActiveProjectLight();
    } else if (runFlags.project) {
      await loadActiveProjectLight();
    } else if (runFlags.sidebar) {
      await loadSidebarLight();
    }
  }

  function scheduleRefresh(flags = {}) {
    if (flags.full) pendingRefreshFlags.full = true;
    if (flags.project) pendingRefreshFlags.project = true;
    if (flags.sidebar) pendingRefreshFlags.sidebar = true;
    if (flags.adapters) pendingRefreshFlags.adapters = true;
    if (refreshTimer) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(flushScheduledRefresh, REFRESH_DEBOUNCE_MS);
    if (!refreshMaxWaitTimer) {
      refreshMaxWaitTimer = window.setTimeout(flushScheduledRefresh, REFRESH_DEBOUNCE_MAX_WAIT_MS);
    }
  }

  function resetLockedWorkspaceState() {
    if (refreshTimer) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    if (refreshMaxWaitTimer) {
      window.clearTimeout(refreshMaxWaitTimer);
      refreshMaxWaitTimer = null;
    }
    pendingRefreshFlags = { full: false, project: false, sidebar: false, adapters: false };
    bumpProjectSwitchGen();
    workspaceLoadSeq += 1;
    projectLoadSeq += 1;
    bumpSidebarLoadSeq();
    cancellingQueuedMsgIds.clear();
    STATE.loadingWorkspace = false;
    STATE.hasLoadedWorkspace = false;
    STATE.auth.principal = null;
    STATE.auth.last_auth_error = null;
    STATE.projects = [];
    STATE.activeProjectId = null;
    STATE.activeProjectDetail = null;
    STATE.chatProjectStatus = null;
    STATE.chatPlanGateMode = "require_approval";
    STATE.chatPermissionMode = "plan";
    STATE.chatMessages = [];
    STATE.chatHasMore = false;
    STATE.chatOldestSeq = null;
    STATE.chatLoadingOlder = false;
    STATE.chatPlansById = {};
    STATE.chatApprovalsById = {};
    STATE.chatNewestPageApprovalIds = new Set();
    STATE.chatLeadStatus = null;
    STATE.activeLeadRun = null;
    STATE.leadFailureByProject = {};
    STATE.plans = [];
    STATE.tasks = [];
    STATE.activeTaskId = null;
    STATE.taskDetails = {};
    STATE.taskWorkspaces = {};
    STATE.taskLogs = {};
    STATE.taskActivities = {};
    STATE.taskDeps = {};
    STATE.taskDiffs = {};
    STATE.projectDiffs = {};
    STATE.runActivities = {};
    setApprovals([]);
    STATE.grants = [];
    STATE.queuedUserMsgIdsByProject = {};
    STATE.ui.rightTabsByProject = {};
    STATE.ui.decidingApprovalIds = new Set();
    STATE.ui.focusAdvancedApprovalId = null;
    STATE.ui.composerInlineNote = null;
    STATE.agents = [];
    STATE.agentDetails = {};
    STATE.adapters = [];
    STATE.adaptersLoaded = false;
    STATE.adaptersLoading = false;
    STATE.adapterDetails = {};
    setRuns([]);
    STATE.leadTurnLogs = {};
    STATE.events = [];
    STATE.eventsProjectId = null;
    STATE.minRetainedSeq = 0;
    STATE.maxSeenEventSeq = 0;
    STATE.config = {};
    STATE.configItems = [];
    STATE.seenSeqs = new Set();
    lastChatThreadHtml = null;
    lastChatMsgCount = 0;
    lastChatTailId = null;
    lastSettingsHtml = null;
    settingsRenderDeferred = false;
    stripApprovalOrder = [];
    stripOrderProjectId = null;
    inspectorApprovalOrder = [];
    inspectorOrderProjectId = null;
    clearRecentlyResolvedApprovals();
    resetApprovalSurfaceGuards();
    syncLogPollingTimer();
    closeModal();
    closeAllPopovers();
    const sp = $("settingsPassInput");
    if (sp) sp.value = "";
    const clearDomHtml = (id, hide = false) => {
      const el = $(id);
      if (!el) return;
      el.innerHTML = "";
      if (hide && el.classList) el.classList.add("hidden");
    };
    clearDomHtml("inspectorBody");
    clearDomHtml("inspectorTop");
    clearDomHtml("inspectorWorkerTabStrip", true);
    clearDomHtml("chatThread");
    clearDomHtml("composerApprovalStrip", true);
    clearDomHtml("cliPageScroll");
    clearDomHtml("settingsPageScroll");
    clearDomHtml("sidebarProjectsContainer");
    clearDomHtml("daemonPopover", true);
    clearDomHtml("leadPopover", true);
    clearDomHtml("projectActionPopover", true);
    const noteEl = $("composerInlineNote");
    if (noteEl) {
      noteEl.textContent = "";
      if (noteEl.classList) noteEl.classList.add("hidden");
    }
  }

  function handleIncomingEvents(items) {
    if (!Array.isArray(items) || items.length === 0) return;
    const refreshFlags = { full: false, project: false, sidebar: false, adapters: false };
    let immediateResync = false;
    let connectionChanged = false;
    let lockedNow = false;
    const wasConnected = STATE.connected;
    const wasVerified = STATE.auth.auth_verified;

    for (const ev of items) {
      if (!ev || typeof ev !== "object") continue;
      if (ev.type === "_desktop.window_state") {
        if (typeof ev.maximized === "boolean") {
          updateWindowMaxButton(ev.maximized);
        }
        continue;
      }
      if (ev.type === "_desktop.onboarding_required") {
        if (ev.connection) applyConnectionSnapshot(ev.connection);
        STATE.pipe.home_initialized = false;
        if (ev.home) STATE.pipe.home = ev.home;
        if (typeof ev.hint === "string" && ev.hint) STATE.pipe.setup_hint = ev.hint;
        STATE.connected = false;
        STATE.daemonStarting = false;
        STATE.daemonStopping = false;
        openWizard(1);
        connectionChanged = true;
        continue;
      }
      if (ev.type === "_desktop.connection") {
        const prevVerified = STATE.auth.auth_verified;
        if (ev.connection) {
          applyConnectionSnapshot(ev.connection);
        } else {
          STATE.connected = Boolean(ev.connected);
          if (typeof ev.starting === "boolean") STATE.daemonStarting = ev.starting;
          if (typeof ev.stopping === "boolean") STATE.daemonStopping = ev.stopping;
          if (typeof ev.user_stopped === "boolean") STATE.userStopped = ev.user_stopped;
          if (ev.error) STATE.pipe.last_error = ev.error;
          if (ev.auth_locked === true) {
            STATE.auth.auth_verified = false;
            STATE.auth.auth_configured = false;
          }
        }
        connectionChanged = true;
        if ((prevVerified && !STATE.auth.auth_verified) || ev.auth_locked === true) {
          lockedNow = true;
          resetLockedWorkspaceState();
        }
        if (STATE.connected && STATE.auth.auth_verified && (ev.resync || !wasConnected || !wasVerified)) {
          immediateResync = true;
        }
        continue;
      }
      if (typeof ev.seq === "number" && ev.project_id === STATE.activeProjectId) {
        appendProjectEventsBounded([ev]);
      }
      const evType = typeof ev.type === "string" ? ev.type : "";
      if (ev.project_id && ev.project_id === STATE.activeProjectId) {
        if ((evType === "approval.cancelled" || evType === "approval.expired") && ev.approval_id) {
          const nextStatus = evType === "approval.expired" ? "expired" : "cancelled";
          markApprovalsTerminalLocally([ev.approval_id], nextStatus);
          renderChatThread();
          renderComposer();
        }
        if (evType === "lead.turn_started") {
          delete STATE.queuedUserMsgIdsByProject[ev.project_id];
          STATE.activeLeadRun = {
            id: ev.run_id || "",
            agent_name: (ev.payload && ev.payload.agent) || (getActiveProject() && getActiveProject().lead) || "Lead",
            status: (ev.payload && ev.payload.status) || "running",
            started_at: ev.ts || new Date().toISOString()
          };
          if (STATE.chatLeadStatus) {
            STATE.chatLeadStatus = Object.assign({}, STATE.chatLeadStatus, { active_run: STATE.activeLeadRun });
          }
          renderChatThread();
          renderComposer();
        } else if (evType === "lead.turn_finished") {
          STATE.activeLeadRun = null;
          if (STATE.chatLeadStatus) {
            STATE.chatLeadStatus = Object.assign({}, STATE.chatLeadStatus, { active_run: null });
          }
          if (ev.payload && ev.payload.interrupted === true) {
            delete STATE.leadFailureByProject[ev.project_id];
            renderChatThread();
          } else if (ev.payload && ev.payload.ok === false) {
            STATE.leadFailureByProject[ev.project_id] = {
              kind: "turn_failed",
              exit_code: ev.payload.exit_code,
              run_id: ev.run_id || ev.payload.run_id || "",
              seq: typeof ev.seq === "number" ? ev.seq : 0,
              ts: ev.ts || new Date().toISOString()
            };
            renderChatThread();
          } else if (ev.payload && ev.payload.ok === true) {
            delete STATE.leadFailureByProject[ev.project_id];
            renderChatThread();
          }
          renderComposer();
        } else if (evType === "lead.interrupted") {
          delete STATE.leadFailureByProject[ev.project_id];
          renderComposer();
        } else if (evType === "chat.cancelled" || evType === "chat.edited") {
          refreshFlags.project = true;
        } else if (evType === "lead.unreachable") {
          STATE.activeLeadRun = null;
          if (STATE.chatLeadStatus) {
            STATE.chatLeadStatus = Object.assign({}, STATE.chatLeadStatus, { active_run: null });
          }
          STATE.leadFailureByProject[ev.project_id] = {
            kind: "unreachable",
            attempts: ev.payload && ev.payload.attempts,
            run_id: ev.run_id || (ev.payload && ev.payload.run_id) || "",
            seq: typeof ev.seq === "number" ? ev.seq : 0,
            ts: ev.ts || new Date().toISOString()
          };
          renderChatThread();
          renderComposer();
        } else if (evType === "chat.message" && ev.payload && ev.payload.role === "lead") {
          delete STATE.leadFailureByProject[ev.project_id];
          delete STATE.queuedUserMsgIdsByProject[ev.project_id];
          renderChatThread();
        }
      }
      if (evType.startsWith("adapter.")) {
        refreshFlags.full = true;
        refreshFlags.adapters = true;
      } else if (
        evType.startsWith("agent.") ||
        evType.startsWith("config.") ||
        evType.startsWith("grant.") ||
        (evType.startsWith("project.") && (!ev.project_id || ev.project_id === STATE.activeProjectId))
      ) {
        refreshFlags.full = true;
      } else if (ev.project_id && ev.project_id !== STATE.activeProjectId) {
        refreshFlags.sidebar = true;
      } else {
        refreshFlags.project = true;
      }
    }

    if (connectionChanged) {
      renderAll({ forceSettings: lockedNow });
      if (STATE.connected && !STATE.auth.auth_verified && hasApi()) {
        refreshConnectionAndAuth().then(() => {
          const sp = $("settingsPassInput");
          const userTypingPass = Boolean(sp && sp.value);
          renderAll({ forceSettings: lockedNow && !userTypingPass });
        });
      }
    }
    if (immediateResync) {
      loadAllWorkspaceData();
    } else if (refreshFlags.full || refreshFlags.project || refreshFlags.sidebar) {
      scheduleRefresh(refreshFlags);
    }
  }

  if (typeof window !== "undefined") {
    window.__meshOnEvents = function (batch) {
      handleIncomingEvents(batch);
    };
  }

  function syncLogPollingTimer() {
    if (logPollTimer) {
      window.clearInterval(logPollTimer);
      logPollTimer = null;
    }
    if (STATE.ui.inspectorCollapsed || STATE.ui.inspectorTab !== "task" || !STATE.activeTaskId) return;
    const t = getActiveTask();
    if (!t || (t.status !== "running" && t.status !== "merging")) return;
    logPollTimer = window.setInterval(async () => {
      if (!STATE.connected || !STATE.auth.auth_verified || STATE.ui.inspectorCollapsed) return;
      const cur = getActiveTask();
      if (!cur || (cur.status !== "running" && cur.status !== "merging")) return;
      await fetchTaskLogs(cur.id);
      if (STATE.connected && STATE.auth.auth_verified) {
        renderInspectorLogsBodyOnly(cur.id);
      }
    }, 2000);
  }

  function startDurationTicker() {
    if (durationTimer) return;
    durationTimer = window.setInterval(() => {
      document.querySelectorAll("[data-duration-start]").forEach(el => {
        const startIso = el.getAttribute("data-duration-start");
        const ms = Date.parse(startIso || "");
        if (!Number.isNaN(ms)) {
          el.textContent = formatDuration((Date.now() - ms) / 1000);
        }
      });
      document.querySelectorAll("[data-wait-start]").forEach(el => {
        const startIso = el.getAttribute("data-wait-start");
        const ms = Date.parse(startIso || "");
        if (!Number.isNaN(ms)) {
          const thresholdS = Number(el.getAttribute("data-wait-threshold-s")) || LEAD_WAIT_NOTICE_TIMEOUT_S;
          const elapsedS = (Date.now() - ms) / 1000;
          el.classList.toggle("hidden", elapsedS < thresholdS);
        }
      });
    }, 1000);
  }

  // =========================================================================
  // Domain Selectors & Enrichment
  // =========================================================================
  function getActiveProject() {
    if (STATE.ui.draftNewChat || !STATE.activeProjectId) return null;
    const base = STATE.projects.find(p => p.id === STATE.activeProjectId) || null;
    if (STATE.activeProjectDetail && STATE.activeProjectDetail.id === STATE.activeProjectId) {
      return Object.assign({}, base, STATE.activeProjectDetail);
    }
    return base;
  }

  function getActiveTask() {
    if (!STATE.activeTaskId) return null;
    const base = STATE.tasks.find(t => t.id === STATE.activeTaskId) || null;
    const detail = STATE.taskDetails[STATE.activeTaskId];
    if (base && detail && detail.id === STATE.activeTaskId) {
      const merged = Object.assign({}, detail, base);
      const keepLiveRun = merged.status === "running" && (!detail.version || !base.version || detail.version === base.version);
      if (!keepLiveRun) merged.live_run = null;
      return merged;
    }
    return base || detail || null;
  }

  function getAgentByName(name) {
    if (!name) return null;
    const base = STATE.agents.find(a => a.name === name) || null;
    const det = STATE.agentDetails[name];
    if (base && det) return Object.assign({}, det, base);
    return base || det || null;
  }

  function getLatestRunForTask(taskId) {
    const runs = getRunsForTask(taskId).slice();
    if (!runs.length) return null;
    runs.sort((a, b) => {
      const att = (b.attempt || 0) - (a.attempt || 0);
      if (att !== 0) return att;
      return (Date.parse(b.started_at || "") || 0) - (Date.parse(a.started_at || "") || 0);
    });
    return runs[0];
  }

  function resolveRunForTask(taskId, liveRun) {
    if (liveRun && liveRun.started_at) return liveRun;
    return getLatestRunForTask(taskId);
  }

  function isTaskScopedApproval(apr) {
    return Boolean(apr && apr.task_id);
  }

  function isApprovalActionableForProject(rawApr, prj = null) {
    if (!rawApr || !rawApr.id) return false;
    const apr = resolveHydratedApproval(rawApr.id) || rawApr;
    if (apr.status && apr.status !== "pending") return false;
    const projectStatus = (prj && prj.status) || null;
    const permissionMode = (prj && prj.permission_mode) || null;
    const task = apr.task_id ? (STATE.tasks.find(t => t && t.id === apr.task_id) || null) : null;
    const plan = apr.category === "plan_dispatch"
      ? (STATE.plans.find(p => p && p.approval_id === apr.id) || null)
      : null;
    const effApr = plan ? (resolvePlanApproval(plan) || apr) : apr;
    return shouldRenderApprovalActions(effApr, { plan, projectStatus, permissionMode, task });
  }

  function getPendingApprovalForTask(taskId, taskObj = null) {
    const t = taskObj || STATE.tasks.find(x => x && x.id === taskId) || null;
    if (t && isTaskTerminalForApproval(t.status, t.kind)) return null;
    const candidates = getApprovalsForTask(taskId).filter(a => a && a.status === "pending");
    if (!candidates.length) return null;
    const prj = getActiveProject();
    for (const raw of candidates) {
      const apr = resolveHydratedApproval(raw.id) || raw;
      const cardOpts = {
        projectStatus: prj && prj.status,
        permissionMode: prj && (prj.permission_mode || STATE.chatPermissionMode),
        task: t
      };
      if (shouldRenderApprovalActions(apr, cardOpts) && !isDismissOnlyApproval(apr, cardOpts)) {
        return apr;
      }
    }
    return null;
  }

  function getNonTaskPendingApprovals(activeProjectId) {
    return STATE.approvals.filter(a => {
      if (a.status !== "pending") return false;
      if (isTaskScopedApproval(a)) return false;
      return !a.project_id || a.project_id === activeProjectId;
    });
  }

  function getTaskCreatedEvent(taskId) {
    return STATE.events.find(
      ev => ev.task_id === taskId && (ev.type === "task.created" || ev.type === "task.assigned")
    ) || null;
  }

  function getTaskLatestPreview(task) {
    const evs = STATE.events.filter(ev => ev.task_id === task.id);
    if (evs.length) {
      let latest = evs[0];
      for (const ev of evs) {
        if ((ev.seq || 0) >= (latest.seq || 0)) latest = ev;
      }
      if (latest.payload && latest.payload.summary) return String(latest.payload.summary);
      if (latest.payload && latest.payload.message) return String(latest.payload.message);
      if (latest.reason) return String(latest.reason);
      if (latest.to_state) {
        return TASK_STATE_LABELS[latest.to_state] || latest.to_state;
      }
    }
    if (task.spec) return String(task.spec).replace(/\s+/g, " ").trim().slice(0, 72);
    return "";
  }

  function enrichTask(task) {
    const detail = STATE.taskDetails[task.id];
    const merged = (detail && detail.id === task.id) ? Object.assign({}, detail, task) : Object.assign({}, task);
    const keepLiveRun = merged.status === "running" && (!detail || !detail.version || !task.version || detail.version === task.version);
    if (!keepLiveRun) merged.live_run = null;
    const agent = merged.assignee ? getAgentByName(merged.assignee) : null;
    const latestRun = resolveRunForTask(merged.id, merged.live_run);
    merged.adapter = (agent && agent.adapter) || (latestRun && latestRun.adapter) || null;
    merged.latest_run = latestRun;
    merged.pending_approval = getPendingApprovalForTask(merged.id, merged);
    merged.created_event = getTaskCreatedEvent(merged.id);
    return merged;
  }

  function getApprovalOpType(apr) {
    if (!apr || typeof apr !== "object") return "";
    if (typeof apr.op_type === "string" && apr.op_type.trim()) return apr.op_type.trim();
    const opObj = apr.op && apr.op._redacted === true ? apr.op.op : apr.op;
    if (opObj && typeof opObj === "object" && typeof opObj.op === "string" && opObj.op.trim()) {
      return opObj.op.trim();
    }
    if (apr.op_json && typeof apr.op_json === "object" && typeof apr.op_json.op === "string" && apr.op_json.op.trim()) {
      return apr.op_json.op.trim();
    }
    return "";
  }

  function isExternalApprovalOp(apr) {
    const opType = getApprovalOpType(apr);
    return typeof opType === "string" && opType.startsWith("external.");
  }

  function formatApprovalOpName(apr) {
    if (!apr) return "--";
    const opType = getApprovalOpType(apr);
    if (opType) return opType;
    return apr.category || "--";
  }

  function formatApprovalOpJson(apr) {
    if (!apr || apr.op_json === undefined || apr.op_json === null) return "(无载荷)";
    if (typeof apr.op_json === "string") return apr.op_json;
    try {
      return JSON.stringify(apr.op_json, null, 2);
    } catch (_) {
      return String(apr.op_json);
    }
  }

  function resolveApprovalPolicy(apr, opts = {}) {
    if (!apr) return "direct_or_relayed";
    if (typeof apr.policy === "string" && apr.policy) {
      return apr.policy;
    }
    const cats = Array.isArray(apr.categories) && apr.categories.length ? apr.categories.slice() : [apr.category];
    const opType = getApprovalOpType(apr);
    if (opType === "task.assign" && !cats.includes("step_dispatch")) {
      const prj = apr.project_id
        ? (STATE.projects.find(p => p && p.id === apr.project_id) ||
          (STATE.activeProjectDetail && STATE.activeProjectDetail.id === apr.project_id ? STATE.activeProjectDetail : null))
        : null;
      const planGateMode = (opts && opts.planGateMode) ||
        (prj && prj.plan_gate_mode) ||
        (apr.project_id && STATE.activeProjectId === apr.project_id ? STATE.chatPlanGateMode : null);
      const permMode = (opts && opts.permissionMode) ||
        (prj && prj.permission_mode) ||
        (apr.project_id && STATE.activeProjectId === apr.project_id ? STATE.chatPermissionMode : null);
      if (planGateMode === "require_approval" && permMode === "supervised") {
        cats.push("step_dispatch");
      }
    }
    const rank = { not_required: 0, direct_or_relayed: 1, direct_only: 2 };
    let strictest = "not_required";
    for (const cat of cats) {
      if (!cat) continue;
      if (cat === "config" || cat === "plan_dispatch" || cat === "step_dispatch") return "direct_only";
      const val = STATE.config[`approval.policy.${cat}`] ||
        (cat === "permission_escalation" ? "direct_only" : (cat === "deliver_to_base" ? "not_required" : "direct_or_relayed"));
      if ((rank[val] || 0) > (rank[strictest] || 0)) strictest = val;
    }
    return strictest;
  }

  function getTaskDurationInfo(enriched) {
    const run = resolveRunForTask(enriched.id, enriched.live_run);
    if (enriched.status === "running" && run && run.started_at) {
      const sec = (Date.now() - Date.parse(run.started_at)) / 1000;
      return { text: formatDuration(sec), isLive: true, startIso: run.started_at, tip: `开始于 ${formatDateTime(run.started_at)}` };
    }
    if (run && run.started_at && run.ended_at) {
      const sec = (Date.parse(run.ended_at) - Date.parse(run.started_at)) / 1000;
      return { text: formatDuration(sec), isLive: false, startIso: null, tip: `耗时 ${formatDuration(sec)}` };
    }
    return { text: "--", isLive: false, startIso: null, tip: "尚无运行记录" };
  }

  function deriveOriginFromEvent(ev, fallbackActor) {
    if (!ev) {
      return { kind: "direct", short: "桌面端", tip: `actor=${fallbackActor || "--"}` };
    }
    if (ev.via === "relayed") {
      const who = ev.relayed_by || "外部转发";
      return { kind: "relayed", short: who, tip: `via=relayed · relayed_by=${who}` };
    }
    if (ev.via === "token" || ev.via === "run_token") {
      return { kind: "token", short: "Lead", tip: `via=${ev.via} · actor=${ev.actor_id || "--"}` };
    }
    return { kind: "direct", short: "桌面端", tip: `via=${ev.via || "direct"} · actor=${ev.actor_id || "--"}` };
  }

  function getStatusDotClass(enriched) {
    if (enriched.pending_approval) return "dot-amber";
    if (["failed", "merge_failed", "escalated"].includes(enriched.status)) return "dot-red";
    if (["running", "merging"].includes(enriched.status)) return "dot-blue";
    if (["submitted", "rework", "cancelling", "created"].includes(enriched.status)) return "dot-amber";
    if (["merged", "accepted"].includes(enriched.status)) return "dot-green";
    return "dot-neutral";
  }

  function getTaskHumanStatus(enriched) {
    if (enriched.pending_approval) return "待你确认";
    return TASK_STATE_LABELS[enriched.status] || enriched.status;
  }

  function taskGroupKey(enriched) {
    if (enriched.pending_approval || NEED_YOU_STATES.has(enriched.status)) return "need_you";
    if (enriched.status === "accepted" && (enriched.kind || "general") === "code") return "need_you";
    if (COMPLETED_STATES.has(enriched.status) || enriched.status === "accepted") return "completed";
    return "in_progress";
  }

  function getLeadEligibility(agent, activeProjectId) {
    const reasons = [];
    let warn = null;
    if (!agent.can_lead) reasons.push("未开启 Lead 资格");
    if (agent.disabled) reasons.push("Agent 已禁用");

    if (agent.delivery === "launch") {
      if (!STATE.config["lead.vendor_leads"]) {
        reasons.push("未开启本地 CLI Lead（lead.vendor_leads=false）");
      }
      const adapter = STATE.adapters.find(a => a.id === agent.adapter);
      if (adapter) {
        if (adapter.status === "not_installed") reasons.push("本机未安装该工具");
        else if (adapter.status === "error" || adapter.status === "unverified") reasons.push(`工具状态异常 (${adapter.status})`);
        if (adapter.launch_reason === "launch_held") {
          reasons.push(`工具处于暂停阶段 (${adapter.launch_milestone || "M5"})`);
        } else if (!adapter.launch_enabled) {
          reasons.push(`工具暂不可启动 (${adapter.launch_reason || "--"})`);
        } else if (adapter.status === "partially_verified") {
          warn = "当前版本尚未经过内置冒烟校验，可正常使用";
        }
      }
      if (agent.options && agent.options.binary_path) {
        warn = "自定义 binary_path";
      }
    }

    if (activeProjectId) {
      const hasOpen = STATE.tasks.some(
        t => t.project_id === activeProjectId && t.assignee === agent.name &&
          !["merged", "cancelled"].includes(t.status) && !(t.status === "accepted" && (t.kind || "general") === "general")
      );
      if (hasOpen) reasons.push("LEAD_HAS_TASKS: 该 Agent 在此项目中有未完成任务");
    }

    return { eligible: reasons.length === 0, reasons, warn };
  }

  // A1: Sidebar Working / Inbox Return Tracker with 2s grace timer
  // Tracks active project runs and briefly retains finished projects before returning them to the inbox.
  function syncSidebarWorkingTracker(nowMs = Date.now()) {
    const tr = STATE.sidebarTracker;
    const validIds = new Set();

    STATE.projects.forEach(prj => {
      if (!prj || !prj.id) return;
      const pid = prj.id;
      validIds.add(pid);
      if (prj.status === "completed" || prj.status === "cancelled" || prj.status === "paused") {
        delete tr.workingSince[pid];
        delete tr.graceUntil[pid];
        if (prj.status === "cancelled" || prj.status === "paused") {
          delete tr.returnedAt[pid];
        }
        if (tr.graceTimers[pid]) {
          window.clearTimeout(tr.graceTimers[pid]);
          delete tr.graceTimers[pid];
        }
        return;
      }

      const pendingAprs = getApprovalsForProject(pid).filter(a => a.status === "pending" && isApprovalActionableForProject(a, prj));
      const isCur = pid === STATE.activeProjectId && !STATE.ui.draftNewChat;
      const hasFailedTask = isCur && STATE.tasks.some(t => ["failed", "escalated", "merge_failed"].includes(t.status));
      const hasLeadFailure = Boolean(STATE.leadFailureByProject[pid]);
      const needsAttentionNow = pendingAprs.length > 0 || hasFailedTask || hasLeadFailure;

      const activeRuns = getActiveRunsForProject(pid);
      const hasLiveLead = isCur && Boolean(STATE.activeLeadRun);
      const isRunningNow = activeRuns.length > 0 || hasLiveLead;

      if (needsAttentionNow) {
        if (tr.workingSince[pid] || !tr.returnedAt[pid]) {
          tr.returnedAt[pid] = nowMs;
        }
        delete tr.workingSince[pid];
        delete tr.graceUntil[pid];
        if (tr.graceTimers[pid]) {
          window.clearTimeout(tr.graceTimers[pid]);
          delete tr.graceTimers[pid];
        }
        return;
      }

      if (isRunningNow) {
        if (!tr.workingSince[pid]) tr.workingSince[pid] = nowMs;
        delete tr.returnedAt[pid];
        delete tr.graceUntil[pid];
        if (tr.graceTimers[pid]) {
          window.clearTimeout(tr.graceTimers[pid]);
          delete tr.graceTimers[pid];
        }
        return;
      }

      // Exited running without error/approval: apply WORKING_EXIT_GRACE_MS (2s) before bubbling to top
      if (tr.workingSince[pid]) {
        if (!tr.graceUntil[pid]) {
          tr.graceUntil[pid] = nowMs + WORKING_EXIT_GRACE_MS;
          if (!tr.graceTimers[pid]) {
            tr.graceTimers[pid] = window.setTimeout(() => {
              delete tr.graceTimers[pid];
              syncSidebarWorkingTracker(Date.now());
              renderSidebar();
            }, WORKING_EXIT_GRACE_MS + 40);
          }
        } else if (nowMs >= tr.graceUntil[pid]) {
          delete tr.workingSince[pid];
          delete tr.graceUntil[pid];
          tr.returnedAt[pid] = nowMs;
        }
      }
    });

    Object.keys(tr.workingSince).forEach(pid => { if (!validIds.has(pid)) delete tr.workingSince[pid]; });
    Object.keys(tr.graceUntil).forEach(pid => { if (!validIds.has(pid)) delete tr.graceUntil[pid]; });
    Object.keys(tr.returnedAt).forEach(pid => { if (!validIds.has(pid)) delete tr.returnedAt[pid]; });
  }

  function getProjectRowMeta(prj) {
    const tr = STATE.sidebarTracker;
    const nowMs = Date.now();
    const pendingAprs = getApprovalsForProject(prj.id).filter(a => a.status === "pending" && isApprovalActionableForProject(a, prj));
    const activeRuns = getActiveRunsForProject(prj.id);
    const workerRuns = activeRuns.filter(r => r.kind !== "lead_turn");
    const isCur = prj.id === STATE.activeProjectId && !STATE.ui.draftNewChat;
    const hasFailedTask = isCur && STATE.tasks.some(t => ["failed", "escalated", "merge_failed"].includes(t.status));
    const hasLeadFailure = Boolean(STATE.leadFailureByProject[prj.id]);
    const inGrace = Boolean(tr.graceUntil[prj.id] && nowMs < tr.graceUntil[prj.id]);
    const isWorking = activeRuns.length > 0 || (isCur && Boolean(STATE.activeLeadRun)) || inGrace;
    const returnedAt = tr.returnedAt[prj.id] || 0;
    const isRecentlyFinished = !isWorking && returnedAt > 0 && pendingAprs.length === 0 && !hasFailedTask && !hasLeadFailure;

    let dotCls = "dot-neutral";
    let preview = prj.lead ? `Lead：${prj.lead}` : "点击开始对话";
    let previewCls = "";
    let needsAttention = false;

    if (pendingAprs.length > 0) {
      const hasPlan = pendingAprs.some(a => a.category === "plan_dispatch");
      dotCls = "dot-amber";
      preview = hasPlan ? "计划卡片待确认" : `有 ${pendingAprs.length} 项审批卡片待处理`;
      previewCls = "is-attention";
      needsAttention = true;
    } else if (hasFailedTask || hasLeadFailure) {
      dotCls = "dot-red";
      preview = hasLeadFailure ? "Lead 回合执行失败，需排查" : "有任务执行失败，需处理";
      previewCls = "is-attention";
      needsAttention = true;
    } else if (isWorking) {
      const leadRun = activeRuns.find(r => r.kind === "lead_turn") || (isCur ? STATE.activeLeadRun : null);
      dotCls = "dot-green";
      preview = workerRuns.length > 0
        ? `${workerRuns.length} 个 Worker 执行中…`
        : (leadRun ? `${prj.lead || "Lead"} 正在规划与回复…` : "正在收尾…");
      previewCls = "is-active";
    } else if (isRecentlyFinished) {
      dotCls = "dot-green";
      preview = "刚刚完成，点击查看结果";
      previewCls = "is-active";
      needsAttention = true;
    } else if (isCur && STATE.chatMessages.length > 0) {
      const lastMsg = STATE.chatMessages[STATE.chatMessages.length - 1];
      preview = (lastMsg.body || "").replace(/\s+/g, " ").trim().slice(0, 48) || preview;
    } else if (prj.goal) {
      preview = String(prj.goal).replace(/\s+/g, " ").trim().slice(0, 48);
    }

    if (prj.status === "completed") {
      dotCls = "dot-green";
    } else if (prj.status === "paused") {
      dotCls = "dot-amber";
    } else if (prj.status === "cancelled") {
      dotCls = "dot-neutral";
    }

    let group = "idle";
    if (prj.status === "completed" || prj.status === "cancelled" || prj.status === "paused") {
      group = "completed";
    } else if (needsAttention) {
      group = "need_you";
    } else if (isWorking) {
      group = "working";
    }

    const updatedMs = Date.parse(prj.updated_at || prj.created_at || "") || 0;
    const latestRunMs = activeRuns.reduce((acc, r) => Math.max(acc, Date.parse(r.started_at || "") || 0), 0);

    return {
      dotCls,
      preview,
      previewCls,
      group,
      isWorking,
      activeWorkerRuns: workerRuns,
      pendingCount: pendingAprs.length,
      isRecentlyFinished,
      returnedAt,
      sortTs: Math.max(updatedMs, latestRunMs, tr.workingSince[prj.id] || 0),
      timeText: formatClock(prj.updated_at || prj.created_at)
    };
  }

  // =========================================================================
  // S2 First-Run Wizard (Step 1: Init -> Step 2: Pick ONE Lead -> Chat)
  // =========================================================================
  function openWizard(step) {
    STATE.wizard.active = true;
    STATE.wizard.step = step === 2 ? 2 : 1;
    renderWizard();
    if (STATE.wizard.step === 2) {
      scanWizardClis();
    }
  }

  function closeWizard() {
    STATE.wizard.active = false;
    const overlay = $("wizardOverlay");
    if (overlay) overlay.classList.add("hidden");
  }

  function getWizardResolvedModels(cliObj) {
    if (!cliObj) return [];
    const aid = cliObj.id;
    const custom = STATE.wizard.selectedModels[aid];
    if (typeof custom === "string" && custom.trim()) {
      return parseModelList(custom);
    }
    const fetched = STATE.wizard.cliModels[aid];
    if (Array.isArray(fetched) && fetched.length) {
      return fetched.slice(0, 3);
    }
    const presets = cliObj.model_presets || getAdapterModelPresets(aid);
    if (presets) {
      if (Array.isArray(presets.default_allowed) && presets.default_allowed.length) {
        return presets.default_allowed.slice();
      }
      if (Array.isArray(presets.chips) && presets.chips.length) {
        return [presets.chips[0]];
      }
    }
    return [];
  }

  async function scanWizardClis() {
    if (!hasApi() || typeof window.pywebview.api.detect_local_clis !== "function") return;
    STATE.wizard.scanningClis = true;
    renderWizard();
    try {
      const res = await window.pywebview.api.detect_local_clis();
      if (res && res.ok && res.data) {
        const clis = Array.isArray(res.data.clis) ? res.data.clis : [];
        STATE.wizard.clis = clis;
        const eligible = clis.filter(c => c.can_lead_eligible);
        if (!STATE.wizard.selectedLeadCli || (!eligible.some(c => c.id === STATE.wizard.selectedLeadCli) && STATE.wizard.selectedLeadCli !== "__inbox__")) {
          STATE.wizard.selectedLeadCli = eligible.length ? eligible[0].id : "__inbox__";
        }
        eligible.forEach(c => {
          if (!STATE.wizard.selectedModels[c.id]) {
            const defaults = getWizardResolvedModels(c);
            if (defaults.length) {
              STATE.wizard.selectedModels[c.id] = defaults.join(", ");
            }
          }
        });
        STATE.wizard.scanningClis = false;
        renderWizard();

        // For installed CLIs without static model_presets (droid/agy/kiro), fetch adapter.models in background
        if (STATE.connected && STATE.auth.auth_verified) {
          const needModels = eligible.filter(c => !c.model_presets);
          await Promise.all(
            needModels.map(async c => {
              const mRes = await apiCall("adapter.models", { adapter_id: c.id });
              if (mRes && mRes.ok && mRes.data && Array.isArray(mRes.data.models) && mRes.data.models.length) {
                STATE.wizard.cliModels[c.id] = mRes.data.models;
                if (!STATE.wizard.selectedModels[c.id]) {
                  STATE.wizard.selectedModels[c.id] = mRes.data.models.slice(0, 2).join(", ");
                }
              }
            })
          );
          renderWizard();
        }
        return;
      }
    } catch (_) {
      // Fallback
    }
    STATE.wizard.scanningClis = false;
    renderWizard();
  }

  function renderWizard() {
    const overlay = $("wizardOverlay");
    if (!overlay) return;
    if (!STATE.wizard.active) {
      overlay.classList.add("hidden");
      return;
    }
    overlay.classList.remove("hidden");

    const step = STATE.wizard.step;
    const pill1 = $("wizardStepPill1");
    const pill2 = $("wizardStepPill2");
    if (pill1) {
      pill1.className = `wizard-step-pill ${step === 1 ? "active" : "done"}`;
    }
    if (pill2) {
      pill2.className = `wizard-step-pill ${step === 2 ? "active" : ""}`;
    }

    const s1Form = $("wizardStep1Form");
    const s2Pane = $("wizardStep2Pane");
    if (s1Form) s1Form.classList.toggle("hidden", step !== 1);
    if (s2Pane) s2Pane.classList.toggle("hidden", step !== 2);

    const homeEl = $("wizardHomePath");
    if (homeEl) {
      const hint = STATE.pipe.setup_hint || "mesh setup";
      homeEl.textContent = STATE.pipe.home
        ? `数据目录：${STATE.pipe.home} · CLI 初始化：${hint}`
        : `CLI 初始化：${hint}`;
    }

    const initBtn = $("btnWizardInit");
    if (initBtn) {
      initBtn.disabled = STATE.wizard.submitting;
      initBtn.textContent = STATE.wizard.submitting ? "正在初始化并启动服务..." : "初始化并继续";
    }

    if (step !== 2) return;

    const skipBtn = $("btnWizardSkip");
    if (skipBtn) {
      skipBtn.classList.toggle("hidden", STATE.pipe.home_initialized !== true);
    }

    const listEl = $("wizardCliList");
    const finishBtn = $("btnWizardFinish");
    if (!listEl) return;

    if (STATE.wizard.scanningClis) {
      listEl.innerHTML = `<div class="empty-state">正在扫描本机已安装的 AI CLI 工具（claude / codex / agy / droid / kiro / vibe）...</div>`;
      if (finishBtn) finishBtn.disabled = true;
      return;
    }

    const clis = STATE.wizard.clis || [];
    const eligible = clis.filter(c => c.can_lead_eligible);
    const unavailable = clis.filter(c => !c.can_lead_eligible);

    const cards = [];
    if (eligible.length) {
      cards.push(`<div class="wizard-section-heading">已就绪的本机 AI CLI (${eligible.length})</div>`);
    }
    eligible.forEach(c => {
      const selected = STATE.wizard.selectedLeadCli === c.id;
      const color = avatarPaletteColor(c.id);
      const workerProfile = c.default_safe_profile || "edit";
      const modelVal = STATE.wizard.selectedModels[c.id] || getWizardResolvedModels(c).join(", ");
      const presets = c.model_presets || getAdapterModelPresets(c.id);
      const chipPool = (STATE.wizard.cliModels[c.id] && STATE.wizard.cliModels[c.id].length)
        ? STATE.wizard.cliModels[c.id].slice(0, 8)
        : ((presets && Array.isArray(presets.chips)) ? presets.chips : []);
      const curList = parseModelList(modelVal);
      const chipsHtml = chipPool.length
        ? `<div class="model-chips-row">${chipPool.map(m => `<button class="model-chip ${curList.includes(m) ? "active" : ""}" type="button" data-wizard-model-chip="${esc(c.id)}" data-model="${esc(m)}">${esc(m)}</button>`).join("")}</div>`
        : "";
      const noteText = (presets && presets.note) ? presets.note : (chipPool.length ? "点击上方模型标签选择，或直接输入模型 ID" : "请输入该 CLI 可用的模型 ID（例如官方模型名）");
      const authHint = VENDOR_AUTH_HINTS[c.id];
      const authStatus = (c.auth_status && typeof c.auth_status === "object") ? c.auth_status : null;
      const authState = authStatus ? authStatus.state : "unknown";
      const authDetail = (authStatus && authStatus.detail) ? authStatus.detail : "";
      const pillClass = selected ? "blue" : (authState === "needs_login" ? "amber" : "green");
      const pillLabel = selected
        ? "已选为 Lead"
        : (authState === "ready" ? "已登录就绪" : (authState === "needs_login" ? "需登录" : "已就绪"));
      const showInlineLoginRow = Boolean(authHint && (selected || authState === "needs_login"));
      const loginHintHtml = showInlineLoginRow
        ? `<div class="wizard-auth-cmd-row" style="margin-top:4px;"><span>${authState === "needs_login" ? "检测到尚未登录，请在终端执行：" : "如尚未登录，可在终端执行："}<code class="mono">${esc(authHint.login)}</code></span><button class="btn btn-sm btn-ghost" type="button" data-copy-code="${esc(authHint.login)}">复制登录命令</button></div>`
        : "";
      cards.push(`
        <div class="wizard-cli-option ${selected ? "selected" : ""}" data-wizard-pick-cli="${esc(c.id)}">
          <div class="wizard-cli-top">
            <div class="wizard-cli-left">
              <span class="avatar avatar-row" style="background-color:${esc(color)};" aria-hidden="true">${esc(avatarInitials(c.id, "AI"))}</span>
              <div>
                <div class="wizard-cli-name">${esc(c.id)} ${c.version ? `<span class="mono" style="font-size:11.5px;color:var(--text-muted);">v${esc(c.version)}</span>` : ""}</div>
                <div class="wizard-cli-meta">Lead 只读规划 (<span class="mono">readonly</span>) · Worker 自动配好 (<span class="mono">${esc(workerProfile)}</span>)${authDetail ? ` · ${esc(authDetail)}` : ""}</div>
              </div>
            </div>
            <span class="status-pill ${pillClass}">${esc(pillLabel)}</span>
          </div>
          ${(!selected && authState === "needs_login" && loginHintHtml) ? `<div style="margin-top:6px;">${loginHintHtml}</div>` : ""}
          ${selected ? `
            <div class="wizard-cli-model-row">
              <label class="form-label" for="wizardModelInput_${esc(c.id)}">模型设置（可多选，逗号分隔）</label>
              <input class="compact-input mono" id="wizardModelInput_${esc(c.id)}" data-wizard-model-input="${esc(c.id)}" type="text" value="${esc(modelVal)}" placeholder="输入可用模型 ID..." />
              ${chipsHtml}
              <div style="font-size:11.5px;color:var(--text-muted);">${esc(noteText)}</div>
              ${loginHintHtml}
            </div>` : ""}
        </div>`);
    });

    if (!eligible.length) {
      const inboxSel = STATE.wizard.selectedLeadCli === "__inbox__";
      cards.push(`
        <div class="wizard-info-box">
          <strong>未在本机 PATH 中检测到已安装的 AI CLI</strong>
          <p>请先在终端安装并登录任一 AI CLI 后点击「重新扫描」，或先启用收件箱模式进入对话（稍后在「AI 工具」中添加）。</p>
        </div>
        <div class="wizard-cli-option ${inboxSel ? "selected" : ""}" data-wizard-pick-cli="__inbox__">
          <div class="wizard-cli-top">
            <div class="wizard-cli-left">
              <span class="avatar avatar-row" style="background-color:#475569;" aria-hidden="true">IN</span>
              <div>
                <div class="wizard-cli-name">内置收件箱协调模式（Inbox Lead）</div>
                <div class="wizard-cli-meta">创建 <span class="mono">mesh-lead</span>（外部代理或脚本通过收件箱接入）</div>
              </div>
            </div>
            <span class="status-pill ${inboxSel ? "blue" : "neutral"}">${inboxSel ? "已选择" : "可选"}</span>
          </div>
        </div>`);
    }

    // A7: Explicit 1-line preview of the exact agents about to be created
    const chosenId = STATE.wizard.selectedLeadCli;
    if (chosenId && chosenId !== "__inbox__") {
      const chosenObj = eligible.find(c => c.id === chosenId);
      if (chosenObj) {
        const wProf = chosenObj.default_safe_profile || "edit";
        const otherReady = eligible.filter(c => c.id !== chosenId).map(c => `${c.id}-worker (${c.default_safe_profile || "edit"})`);
        const extraTxt = otherReady.length ? `，并同步注册 ${otherReady.join("、")}` : "";
        cards.push(`
          <div class="wizard-preview-box">
            <strong>将为你创建：</strong><span class="mono">${esc(chosenId)}-lead (readonly)</span> + <span class="mono">${esc(chosenId)}-worker (${esc(wProf)})</span>${esc(extraTxt)}。Lead 与 Worker 角色分离，确保所有派发均受审批卡片保护。
          </div>`);
      }
    } else if (chosenId === "__inbox__") {
      cards.push(`
        <div class="wizard-preview-box">
          <strong>将为你创建：</strong><span class="mono">mesh-lead (inbox)</span>，用于接入外部代理或脚本协调。
        </div>`);
    }

    // A7: Group missing / needs-login CLIs with copyable install & auth commands
    if (unavailable.length) {
      cards.push(`
        <details class="config-raw-details" ${!eligible.length ? "open" : ""}>
          <summary>未安装或需登录的工具 (${esc(unavailable.length)} 项 · 点击复制安装 / 登录命令)</summary>
          <div style="margin-top:8px;display:flex;flex-direction:column;gap:6px;">
            ${unavailable.map(c => {
              const hint = VENDOR_AUTH_HINTS[c.id];
              const cmd = hint ? hint.login : "";
              const inst = hint ? hint.install : "";
              return `
                <div class="wizard-auth-cmd-row">
                  <div style="min-width:0;">
                    <div><span class="mono" style="color:var(--text-primary);font-weight:600;">${esc(c.id)}</span> · <span style="color:var(--text-secondary);">${esc(c.ineligible_reason || "未安装 / 暂不可用")}</span></div>
                    ${inst ? `<div style="font-size:11.5px;color:var(--text-muted);margin-top:2px;">安装：<code class="mono">${esc(inst)}</code>${cmd ? ` · 登录：<code class="mono">${esc(cmd)}</code>` : ""}</div>` : ""}
                  </div>
                  ${cmd ? `<button class="btn btn-sm btn-ghost" type="button" data-copy-code="${esc(cmd)}">复制登录</button>` : ""}
                </div>`;
            }).join("")}
          </div>
        </details>`);
    }

    listEl.innerHTML = cards.join("");
    if (finishBtn) {
      finishBtn.disabled = STATE.wizard.submitting || !STATE.wizard.selectedLeadCli;
      finishBtn.textContent = STATE.wizard.submitting ? "正在配置 Lead 与 Worker..." : "开始对话";
    }
  }

  async function handleWizardInitSubmit(e) {
    e.preventDefault();
    if (STATE.wizard.submitting) return;
    const errEl = $("wizardStep1Error");
    const showErr = msg => {
      if (errEl) {
        errEl.textContent = msg;
        errEl.classList.remove("hidden");
      }
    };
    if (errEl) errEl.classList.add("hidden");

    const passInp = $("wizardPassInput");
    const confInp = $("wizardPassConfirm");
    const pw = passInp ? passInp.value : "";
    const confirmPw = confInp ? confInp.value : "";

    if (pw.length < 12) {
      showErr("口令长度至少需要 12 个字符");
      return;
    }
    if (pw !== confirmPw) {
      showErr("两次输入的口令不一致，请检查后重试");
      return;
    }
    if (!hasApi() || typeof window.pywebview.api.initialize_home !== "function") {
      showErr("当前桌面桥接不支持图形化初始化");
      return;
    }

    // Clear DOM password inputs immediately before async IPC
    if (passInp) passInp.value = "";
    if (confInp) confInp.value = "";
    STATE.wizard.submitting = true;
    renderWizard();

    try {
      const res = await window.pywebview.api.initialize_home(pw);
      STATE.wizard.submitting = false;
      if (!res || !res.ok) {
        const errDetails = (res && res.error && res.error.details) || {};
        const errMsg = formatErrorMessage(res && res.error);
        await refreshConnectionAndAuth();
        if (
          errDetails.initialized === true ||
          errDetails.home_initialized === true ||
          STATE.pipe.home_initialized === true
        ) {
          STATE.pipe.home_initialized = true;
          closeWizard();
          renderAll();
          toast(errMsg, "err");
          return;
        }
        showErr(errMsg);
        renderWizard();
        return;
      }
      if (res.data && res.data.connection) applyConnectionSnapshot(res.data.connection);
      if (res.data && res.data.auth) STATE.auth = res.data.auth;
      STATE.pipe.home_initialized = true;
      await refreshConnectionAndAuth();
      toast("初始化成功，后台服务已启动并解锁", "ok");
      STATE.wizard.step = 2;
      renderWizard();
      await scanWizardClis();
    } catch (err) {
      STATE.wizard.submitting = false;
      showErr(`初始化失败：${err && err.message ? err.message : String(err)}`);
      renderWizard();
    }
  }

  async function handleWizardFinish() {
    if (STATE.wizard.submitting) return;
    const errEl = $("wizardStep2Error");
    const showErr = msg => {
      if (errEl) {
        errEl.textContent = msg;
        errEl.classList.remove("hidden");
      }
    };
    if (errEl) errEl.classList.add("hidden");

    if (!canMutate()) {
      showErr(disabledReason() || "请先完成解锁");
      return;
    }
    const chosen = STATE.wizard.selectedLeadCli;
    if (!chosen) {
      showErr("请选择一个 Lead");
      return;
    }

    STATE.wizard.submitting = true;
    renderWizard();
    const skippedWorkers = [];
    try {
      if (chosen === "__inbox__") {
        const leadName = "mesh-lead";
        if (!STATE.agents.some(a => a.name === leadName)) {
          const r = await apiCall("agent.create", {
            name: leadName,
            adapter: "none",
            delivery: "inbox",
            can_lead: true,
            max_concurrency: 1
          });
          if (!r.ok && (!r.error || r.error.code !== "ALREADY_EXISTS")) {
            throw new Error(formatErrorMessage(r.error));
          }
        }
        const cfgR = await apiCall("config.set", { key: "lead.default_agent", value: leadName });
        if (!cfgR.ok) throw new Error(formatErrorMessage(cfgR.error));
      } else {
        const cliObj = STATE.wizard.clis.find(c => c.id === chosen);
        if (!cliObj || !cliObj.can_lead_eligible) {
          throw new Error("所选 CLI 不可作为 Lead，请重新选择");
        }
        const models = getWizardResolvedModels(cliObj);
        if (!models.length) {
          throw new Error(`请为 ${chosen} 输入至少一个可用模型 ID`);
        }

        const leadName = `${chosen}-lead`;
        const workerName = `${chosen}-worker`;
        const safeProfile = cliObj.default_safe_profile || getAdapterDefaultSafeProfile(chosen) || "edit";

        // 1. Create <cli>-lead (readonly, can_lead=true)
        if (!STATE.agents.some(a => a.name === leadName)) {
          const rLead = await apiCall("agent.create", {
            name: leadName,
            adapter: chosen,
            delivery: "launch",
            can_lead: true,
            default_profile: "readonly",
            allowed_models: models,
            default_model: models[0],
            max_concurrency: 1
          });
          if (!rLead.ok && (!rLead.error || rLead.error.code !== "ALREADY_EXISTS")) {
            throw new Error(formatErrorMessage(rLead.error));
          }
        }

        // 2. Create separate <cli>-worker (can_lead=false, safe profile) to preserve LEAD_CANNOT_BE_ASSIGNEE
        if (!STATE.agents.some(a => a.name === workerName)) {
          const rWorker = await apiCall("agent.create", {
            name: workerName,
            adapter: chosen,
            delivery: "launch",
            can_lead: false,
            default_profile: safeProfile,
            allowed_models: models,
            default_model: models[0],
            max_concurrency: 2
          });
          if (!rWorker.ok && (!rWorker.error || rWorker.error.code !== "ALREADY_EXISTS")) {
            throw new Error(formatErrorMessage(rWorker.error));
          }
        }

        // 3. Also register any other detected eligible CLIs that have valid models as workers
        const others = STATE.wizard.clis.filter(c => c.can_lead_eligible && c.id !== chosen);
        for (const other of others) {
          const otherWorkerName = `${other.id}-worker`;
          if (STATE.agents.some(a => a.name === otherWorkerName)) continue;
          let otherModels = getWizardResolvedModels(other);
          if (!otherModels.length) {
            const mRes = await apiCall("adapter.models", { adapter_id: other.id });
            if (mRes && mRes.ok && mRes.data && Array.isArray(mRes.data.models) && mRes.data.models.length) {
              STATE.wizard.cliModels[other.id] = mRes.data.models;
              otherModels = mRes.data.models.slice(0, 3);
            }
          }
          if (!otherModels.length) {
            skippedWorkers.push({ id: other.id, reason: "未检测到可用模型" });
            continue;
          }
          const otherSafe = other.default_safe_profile || getAdapterDefaultSafeProfile(other.id) || "edit";
          const rOther = await apiCall("agent.create", {
            name: otherWorkerName,
            adapter: other.id,
            delivery: "launch",
            can_lead: false,
            default_profile: otherSafe,
            allowed_models: otherModels,
            default_model: otherModels[0],
            max_concurrency: 2
          });
          if (!rOther.ok && (!rOther.error || rOther.error.code !== "ALREADY_EXISTS")) {
            skippedWorkers.push({ id: other.id, reason: formatErrorMessage(rOther.error) });
          }
        }

        // 4. Enable lead.vendor_leads=true & set lead.default_agent=<cli>-lead
        const rVendor = await apiCall("config.set", { key: "lead.vendor_leads", value: true });
        if (!rVendor.ok) throw new Error(formatErrorMessage(rVendor.error));
        const rDef = await apiCall("config.set", { key: "lead.default_agent", value: leadName });
        if (!rDef.ok) throw new Error(formatErrorMessage(rDef.error));
      }

      STATE.wizard.submitting = false;
      STATE.wizard.skipped = true;
      closeWizard();
      STATE.ui.nav = "tasks";
      STATE.ui.draftNewChat = STATE.projects.length === 0;
      await loadAllWorkspaceData();
      if (chosen === "__inbox__") {
        toast("已启用内置收件箱模式（mesh-lead）：发送的消息将进入收件箱等待外部代理领取，也可随时重新扫描连接本机 CLI", "info");
      } else {
        toast("配置完成，直接开始对话吧！", "ok");
      }
      if (skippedWorkers.length) {
        const skippedSummary = skippedWorkers.map(s => `${s.id}（${s.reason}）`).join("、");
        toast(`已跳过自动创建 Worker：${skippedSummary}，可稍后在「AI 工具」中配置模型并添加`, "warn");
      }
      if ($("composerInput")) $("composerInput").focus();
    } catch (err) {
      STATE.wizard.submitting = false;
      showErr(err && err.message ? err.message : String(err));
      renderWizard();
    }
  }

  // =========================================================================
  // Render: Connection & Auth State
  // =========================================================================
  function renderConnectionState() {
    const dot = $("footerDaemonDot");
    const label = $("footerDaemonLabel");
    const disBar = $("daemonDisconnectedBar");
    const disDot = $("daemonStatusDot");
    const disText = $("daemonDisconnectedText");
    const disDetail = $("daemonDisconnectedDetail");
    const btnStart = $("btnInlineStartDaemon");
    const btnReconnect = $("btnInlineReconnect");
    const authBar = $("authUnlockBar");
    const authMsg = $("authUnlockMsg");
    const verEl = $("sidebarVersion");
    const titleDot = $("titlebarConnDot");
    const userAvatar = $("footerUserAvatar");
    const userNameEl = $("footerUserName");

    if (verEl) {
      verEl.textContent = STATE.pipe.daemon_version || "--";
    }

    if (STATE.connected) {
      if (dot) dot.className = "dot dot-green";
      if (titleDot) titleDot.className = "dot dot-green";
      const runs = STATE.pipe.daemon_status ? STATE.pipe.daemon_status.active_runs : 0;
      if (label) label.textContent = runs > 0 ? `运行中 · ${runs} 个活跃任务` : "后台服务在线";
      if (disBar) disBar.classList.add("hidden");
    } else if (STATE.pipe.home_initialized === false) {
      if (dot) dot.className = "dot dot-amber";
      if (titleDot) titleDot.className = "dot dot-amber";
      if (label) label.textContent = "等待首次初始化";
      if (disBar) disBar.classList.add("hidden");
    } else if (disBar) {
      disBar.classList.remove("hidden");
      disBar.classList.remove("is-starting", "is-stopping");
      const retryNote = STATE.pipe.reconnect_attempt > 0
        ? `重连 #${STATE.pipe.reconnect_attempt}，${STATE.pipe.next_retry_s}s 后重试`
        : "";

      if (STATE.daemonStarting) {
        if (dot) dot.className = "dot dot-blue";
        if (titleDot) titleDot.className = "dot dot-blue";
        if (disDot) disDot.className = "dot dot-blue";
        disBar.classList.add("is-starting");
        if (label) label.textContent = "后台服务启动中...";
        if (disText) disText.textContent = "正在启动并连接本地后台服务，请稍候...";
        if (disDetail) {
          disDetail.textContent = STATE.pipe.home ? `数据目录：${STATE.pipe.home}` : "";
          disDetail.classList.toggle("hidden", !STATE.pipe.home);
        }
        if (btnStart) { btnStart.disabled = true; btnStart.textContent = "启动中..."; }
        if (btnReconnect) btnReconnect.disabled = true;
      } else if (STATE.daemonStopping && !STATE.userStopped) {
        if (dot) dot.className = "dot dot-amber";
        if (titleDot) titleDot.className = "dot dot-amber";
        if (disDot) disDot.className = "dot dot-amber";
        disBar.classList.add("is-stopping");
        if (label) label.textContent = "后台服务收尾中...";
        if (disText) disText.textContent = "守护进程正在完成收尾，稍后自动重连";
        if (disDetail) {
          const detailText = retryNote
            ? `守护进程正在排空进行中的合并任务或处理繁忙请求（持有 meshd.lock，${retryNote}）`
            : "守护进程正在排空进行中的合并任务或处理繁忙请求（持有 meshd.lock），稍后将自动重连。";
          disDetail.textContent = detailText;
          disDetail.classList.remove("hidden");
        }
        if (btnStart) { btnStart.disabled = true; btnStart.textContent = "等待收尾..."; }
        if (btnReconnect) btnReconnect.disabled = false;
      } else if (STATE.userStopped && !STATE.pipe.last_start_error) {
        if (dot) dot.className = "dot dot-neutral";
        if (titleDot) titleDot.className = "dot dot-neutral";
        if (disDot) disDot.className = "dot dot-neutral";
        if (label) label.textContent = STATE.daemonStopping ? "后台服务停止中..." : "后台服务已停止";
        if (disText) disText.textContent = "meshd 已按你的请求停止";
        if (disDetail) {
          disDetail.textContent = STATE.daemonStopping
            ? "旧进程正在完成收尾并释放锁，不会自动重启；需要时可点击「启动服务」。"
            : "后台服务已停止，不会自动重启；点击「启动服务」可随时重新启动。";
          disDetail.classList.remove("hidden");
        }
        if (btnStart) { btnStart.disabled = false; btnStart.textContent = "启动服务"; }
        if (btnReconnect) btnReconnect.disabled = false;
      } else {
        if (dot) dot.className = "dot dot-red";
        if (titleDot) titleDot.className = "dot dot-red";
        if (disDot) disDot.className = "dot dot-red";
        if (label) label.textContent = "后台服务未连接";
        const startErr = STATE.pipe.last_start_error;
        if (startErr) {
          const failHeading = startErr.auto === true ? "自动启动后台服务失败" : "启动后台服务失败";
          if (disText) disText.textContent = `${failHeading} — 请查看日志或点击重试${retryNote ? ` (${retryNote})` : ""}`;
          if (disDetail) {
            disDetail.textContent = formatErrorMessage(startErr);
            disDetail.classList.remove("hidden");
          }
          if (btnStart) { btnStart.disabled = false; btnStart.textContent = "重试启动"; }
        } else {
          if (disText) disText.textContent = `后台服务已断开 — 当前为只读查看模式${retryNote ? ` (${retryNote})` : ""}`;
          if (disDetail) {
            const errMsg = STATE.pipe.last_error ? formatErrorMessage(STATE.pipe.last_error) : "";
            disDetail.textContent = errMsg;
            disDetail.classList.toggle("hidden", !errMsg);
          }
          if (btnStart) { btnStart.disabled = false; btnStart.textContent = "启动服务"; }
        }
        if (btnReconnect) btnReconnect.disabled = false;
      }
    }

    if (userAvatar && userNameEl) {
      if (STATE.auth.auth_verified) {
        const principalName = (STATE.auth.principal && STATE.auth.principal.name) || "user";
        userNameEl.textContent = principalName;
        userAvatar.textContent = avatarInitials(principalName, "US");
      } else {
        userNameEl.textContent = STATE.connected ? "未解锁" : "未连接";
        userAvatar.textContent = "AM";
      }
    }

    if (authBar) {
      if (STATE.connected && !STATE.auth.auth_verified && !STATE.wizard.active) {
        authBar.classList.remove("hidden");
        if (authMsg) {
          authMsg.textContent = STATE.auth.last_auth_error
            ? formatErrorMessage(STATE.auth.last_auth_error)
            : "请输入口令解锁工作台（默认仅保存在当前进程内存中）";
        }
        const remWrap = $("authInlineRememberWrap");
        const remCheck = $("authInlineRemember");
        const remWarn = $("authInlineRememberWarning");
        if (remWrap) {
          remWrap.classList.toggle("hidden", !STATE.auth.stored_credential_supported);
        }
        if (remCheck && !STATE.auth.stored_credential_supported) {
          remCheck.checked = false;
        }
        if (remWarn) {
          const showInlineWarn = Boolean(STATE.auth.stored_credential_supported && remCheck && remCheck.checked);
          remWarn.classList.toggle("hidden", !showInlineWarn);
        }
      } else {
        authBar.classList.add("hidden");
        const remWarn = $("authInlineRememberWarning");
        if (remWarn) remWarn.classList.add("hidden");
      }
    }

    const closeBtn = $("btnWinClose");
    if (closeBtn) {
      const hidesToTray = Boolean(STATE.desktopPrefs && STATE.desktopPrefs.tray_active && STATE.desktopPrefs.close_to_tray);
      closeBtn.title = hidesToTray ? "最小化到系统托盘" : "关闭窗口";
    }

    const allow = canMutate();
    const reason = disabledReason();
    document.querySelectorAll("[data-requires-auth='1']").forEach(el => {
      el.disabled = !allow;
      if (!allow && reason) {
        el.setAttribute("data-orig-title", el.getAttribute("data-orig-title") || el.title || "");
        el.title = reason;
      } else if (el.getAttribute("data-orig-title")) {
        el.title = el.getAttribute("data-orig-title");
      }
    });
  }

  // =========================================================================
  // Render: Left Sidebar (Project Chats + 88px Icon Rail)
  // =========================================================================
  function renderSidebar() {
    const shell = $("appShell");
    if (shell) {
      shell.classList.toggle("sidebar-collapsed", STATE.ui.sidebarCollapsed);
      shell.classList.toggle("inspector-collapsed", STATE.ui.inspectorCollapsed);
      document.documentElement.style.setProperty("--sidebar-width", `${STATE.ui.sidebarWidth}px`);
      document.documentElement.style.setProperty("--inspector-width", `${STATE.ui.inspectorWidth}px`);
    }
    const leftSidebar = $("leftSidebar");
    if (leftSidebar) leftSidebar.classList.toggle("collapsed", STATE.ui.sidebarCollapsed);
    const rightInspector = $("rightInspector");
    if (rightInspector) rightInspector.classList.toggle("collapsed", STATE.ui.inspectorCollapsed);
    const toggleInspBtn = $("btnToggleInspector");
    if (toggleInspBtn) toggleInspBtn.classList.toggle("active", !STATE.ui.inspectorCollapsed);

    document.querySelectorAll(".sidebar-nav-pill[data-nav]").forEach(btn => {
      btn.classList.toggle("active", btn.getAttribute("data-nav") === STATE.ui.nav);
    });
    if ($("navProjectCount")) $("navProjectCount").textContent = String(STATE.projects.length);
    if ($("navCliCount")) $("navCliCount").textContent = String(STATE.agents.length);

    const searchWrap = $("sidebarSearchWrap");
    if (searchWrap) searchWrap.classList.toggle("hidden", !STATE.ui.searchVisible);
    if ($("btnToggleSearch")) $("btnToggleSearch").classList.toggle("active", STATE.ui.searchVisible);

    const container = $("sidebarProjectsContainer");
    if (!container) return;

    if (!STATE.connected || !STATE.auth.auth_verified) {
      container.innerHTML = `<div class="empty-state sidebar-label">${STATE.connected ? "解锁后查看项目对话" : "等待后台服务连接"}</div>`;
      return;
    }
    if (STATE.loadingWorkspace || !STATE.hasLoadedWorkspace) {
      container.innerHTML = `<div class="empty-state sidebar-label">正在加载项目对话...</div>`;
      return;
    }

    const q = STATE.ui.searchQuery.trim().toLowerCase();
    const filtered = STATE.projects.filter(p => {
      if (!q) return true;
      const hay = `${p.name || ""} ${p.goal || ""} ${p.lead || ""} ${p.id || ""}`.toLowerCase();
      return hay.includes(q);
    });

    if (!filtered.length) {
      container.innerHTML = `<div class="empty-state sidebar-label">${STATE.projects.length ? "未找到匹配的项目对话" : "还没有项目对话，点右上角 + 开始"}</div>`;
      return;
    }

    const groups = [
      { key: "need_you", label: "需要你", items: [] },
      { key: "working", label: "运行中 · Working", items: [] },
      { key: "idle", label: "其他对话", items: [] },
      { key: "completed", label: "已完成 / 暂停", items: [] }
    ];
    const gmap = {
      need_you: groups[0].items,
      working: groups[1].items,
      idle: groups[2].items,
      completed: groups[3].items
    };
    filtered.forEach(p => {
      const meta = getProjectRowMeta(p);
      gmap[meta.group].push({ prj: p, meta });
    });

    // A1: Bubble recently returned / needs-approval items to top; sort working by latest activity
    gmap.need_you.sort((a, b) => (b.meta.returnedAt - a.meta.returnedAt) || (b.meta.pendingCount - a.meta.pendingCount) || (b.meta.sortTs - a.meta.sortTs));
    gmap.working.sort((a, b) => b.meta.sortTs - a.meta.sortTs);
    gmap.idle.sort((a, b) => b.meta.sortTs - a.meta.sortTs);

    container.innerHTML = groups.filter(g => g.items.length > 0).map(grp => {
      const collapsed = Boolean(STATE.ui.collapsedProjectGroups[grp.key]);
      const chevron = collapsed
        ? `<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m9 18 6-6-6-6"></path></svg>`
        : `<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m6 9 6 6 6-6"></path></svg>`;

      const rowsHtml = (collapsed && !STATE.ui.sidebarCollapsed) ? "" : grp.items.map(({ prj, meta }) => {
        const selected = !STATE.ui.draftNewChat && STATE.ui.nav === "tasks" && prj.id === STATE.activeProjectId ? "selected" : "";
        const label = prj.name || "未命名项目";
        const avatarColor = avatarPaletteColor(prj.lead || label);
        const initials = avatarInitials(label, "PR");
        const returnBadge = (!meta.isWorking && meta.isRecentlyFinished)
          ? `<span class="sidebar-return-badge">刚完成</span>`
          : "";
        const workerSubRowsHtml = (grp.key === "working" && meta.activeWorkerRuns.length > 0)
          ? `<div class="sidebar-worker-subrows">${meta.activeWorkerRuns.map(run => {
              const tObj = run.task_id ? STATE.tasks.find(t => t.id === run.task_id) : null;
              const taskLabel = (tObj && tObj.title) || run.task_id || "任务执行中";
              const startIso = run.started_at || "";
              const elapsed = startIso ? formatDuration((Date.now() - Date.parse(startIso)) / 1000) : "00:00";
              const liveAttr = startIso ? `data-duration-start="${esc(startIso)}"` : "";
              const isActTab = !STATE.ui.inspectorCollapsed && STATE.ui.inspectorTab === "task" && run.task_id && STATE.activeTaskId === run.task_id ? "active" : "";
              return `
                <button class="sidebar-worker-subrow ${isActTab}" type="button" data-open-worker-tab="${esc(run.task_id || "")}" data-worker-project="${esc(prj.id)}" title="${esc(run.agent || "Worker")} · ${esc(taskLabel)}">
                  <span class="sidebar-worker-subrow-left">
                    <span class="dot dot-blue"></span>
                    <span class="sidebar-worker-subrow-title">${esc(run.agent || "Worker")} · ${esc(taskLabel)}</span>
                  </span>
                  <span class="mono" ${liveAttr}>${esc(elapsed)}</span>
                </button>`;
            }).join("")}</div>`
          : "";
        return `
          <button class="project-row ${selected}" type="button" data-select-project="${esc(prj.id)}" title="${esc(label)} · ${esc(meta.preview)}">
            <span class="avatar avatar-row" style="background-color:${esc(avatarColor)};" aria-hidden="true">
              <span>${esc(initials)}</span>
              <span class="dot ${esc(meta.dotCls)} avatar-badge-dot"></span>
            </span>
            <span class="project-row-body">
              <span class="project-row-top">
                <span class="project-row-title">${esc(label)}</span>
                ${returnBadge || `<span class="project-row-time mono">${esc(meta.timeText)}</span>`}
              </span>
              <span class="project-row-preview ${esc(meta.previewCls)}">${esc(meta.preview)}</span>
            </span>
          </button>${workerSubRowsHtml}`;
      }).join("");

      return `
        <div class="project-group" data-prj-group="${esc(grp.key)}">
          <button class="project-group-header" type="button" data-toggle-prj-group="${esc(grp.key)}">
            <span class="project-group-left">${chevron}<span>${esc(grp.label)}</span></span>
            <span class="mono">${grp.items.length}</span>
          </button>
          ${rowsHtml}
        </div>`;
    }).join("");
  }

  // =========================================================================
  // Render: Center Header & Views
  // =========================================================================
  function renderCenterHeaderAndViews() {
    const nav = STATE.ui.nav;
    if ($("viewTasks")) $("viewTasks").classList.toggle("hidden", nav !== "tasks");
    if ($("viewCli")) $("viewCli").classList.toggle("hidden", nav !== "cli");
    if ($("viewSettings")) $("viewSettings").classList.toggle("hidden", nav !== "settings");

    const prj = getActiveProject();
    const titleEl = $("centerTitle");
    const avatarEl = $("centerTitleAvatar");
    const metaEl = $("centerMeta");
    const deliverBtn = $("btnDeliverBase");
    const actBtn = $("btnProjectActions");
    const modeSel = $("projectPermissionModeSelect");

    if (nav === "cli") {
      if (titleEl) titleEl.textContent = "AI 工具与 Agent 管理";
      if (avatarEl) { avatarEl.textContent = "AI"; avatarEl.style.backgroundColor = "#2563eb"; }
      if (metaEl) metaEl.textContent = `已配置 ${STATE.agents.length} 个 Agent`;
      if (deliverBtn) deliverBtn.classList.add("hidden");
      if (actBtn) actBtn.classList.add("hidden");
      if (modeSel) modeSel.classList.add("hidden");
      return;
    }
    if (nav === "settings") {
      if (titleEl) titleEl.textContent = "系统设置与安全策略";
      if (avatarEl) { avatarEl.textContent = "设"; avatarEl.style.backgroundColor = "#475569"; }
      if (metaEl) metaEl.textContent = STATE.pipe.home ? `数据目录：${STATE.pipe.home}` : "";
      if (deliverBtn) deliverBtn.classList.add("hidden");
      if (actBtn) actBtn.classList.add("hidden");
      if (modeSel) modeSel.classList.add("hidden");
      return;
    }

    if (prj) {
      const label = prj.name || "项目对话";
      if (titleEl) titleEl.textContent = label;
      if (avatarEl) {
        avatarEl.textContent = avatarInitials(label, "PR");
        avatarEl.style.backgroundColor = avatarPaletteColor(prj.lead || label);
      }
      const runningCount = STATE.tasks.filter(t => ["running", "merging"].includes(t.status)).length;
      const leadLabel = prj.lead || STATE.config["lead.default_agent"] || "未指定 Lead";
      const curMode = prj.permission_mode || STATE.chatPermissionMode || "plan";
      const modeMeta = PERMISSION_MODE_META[curMode] || PERMISSION_MODE_META.plan;
      const activityParts = [leadLabel];
      if (STATE.activeLeadRun) {
        activityParts.push("正在思考与规划…");
      } else if (runningCount > 0) {
        activityParts.push(`${runningCount} 个任务执行中`);
      } else if (prj.status === "completed") {
        activityParts.push("已完成");
      } else if (prj.status === "paused") {
        activityParts.push("已暂停");
      } else if (prj.status === "cancelled") {
        activityParts.push("已取消");
      }
      if (prj.repo && prj.base_branch) {
        activityParts.push(`分支 ${prj.base_branch}`);
      }
      if (metaEl) {
        metaEl.textContent = activityParts.join(" · ");
        metaEl.title = prj.id;
      }
      if (modeSel) {
        const isPrjActive = !prj.status || prj.status === "active";
        modeSel.classList.remove("hidden", "mode-supervised", "mode-plan", "mode-auto");
        modeSel.classList.add(`mode-${curMode}`);
        modeSel.value = curMode;
        modeSel.disabled = !canMutate() || !isPrjActive;
        modeSel.title = isPrjActive
          ? `权限模式：${modeMeta.label} — ${modeMeta.desc}`
          : `当前项目状态为 ${prj.status}，仅进行中项目可切换权限模式`;
      }
      if (actBtn) actBtn.classList.remove("hidden");
      if (deliverBtn) {
        if (prj.repo && prj.base_branch) {
          deliverBtn.classList.remove("hidden");
          deliverBtn.textContent = `交付至 ${prj.base_branch}`;
          deliverBtn.disabled = !canMutate() || prj.status === "cancelled";
        } else {
          deliverBtn.classList.add("hidden");
        }
      }
    } else {
      const defLead = STATE.ui.selectedLeadDraft || STATE.config["lead.default_agent"] || "未指定 Lead";
      if (titleEl) titleEl.textContent = "新建项目对话";
      if (avatarEl) { avatarEl.textContent = "AM"; avatarEl.style.backgroundColor = "#2563eb"; }
      if (metaEl) metaEl.textContent = `默认 Lead：${defLead}`;
      if (actBtn) actBtn.classList.add("hidden");
      if (deliverBtn) deliverBtn.classList.add("hidden");
      if (modeSel) modeSel.classList.add("hidden");
    }
  }

  // =========================================================================
  // Render: Chat-First Transcript (Bubbles, Inline Approval Cards, Nested Tasks)
  // =========================================================================
  function sanitizeSingleLineText(val) {
    if (val === null || val === undefined) return "";
    return String(val).replace(/[\r\n\u2028\u2029]+/g, " ");
  }

  function isRelayedChatMessage(msg) {
    return Boolean(msg && (msg.relayed === true || msg.via === "relayed"));
  }

  function formatRelayedBadgeText(msg) {
    if (!isRelayedChatMessage(msg)) return "";
    const meta = (msg && msg.meta && typeof msg.meta === "object") ? msg.meta : {};
    const rawName = msg.relayed_by_name || meta.relayed_by_name || msg.relayed_by || meta.relayed_by || "外部转发器";
    const name = sanitizeSingleLineText(rawName).trim() || "外部转发器";
    return `经由 ${name} 转发`;
  }

  function renderUserChatMessageHtml(msg, opts = {}) {
    const {
      isQueued = false,
      isCancelledQueued = false,
      isEditedQueued = false,
      canEditQueued = false
    } = (opts && typeof opts === "object") ? opts : {};
    if (!msg) return "";
    const isRelayed = isRelayedChatMessage(msg);
    const relayedBadgeText = isRelayed ? formatRelayedBadgeText(msg) : "";
    const safeBody = String(msg.body || "").replace(/[\u2028\u2029]/g, "\n");
    const allowQueueActions = Boolean(
      canEditQueued &&
      !isCancelledQueued &&
      !isRelayed &&
      !msg.approval_id &&
      !msg.plan_id &&
      !msg.task_id &&
      !msg.consumed_by_approval_id
    );
    const queueActionsHtml = allowQueueActions
      ? `<span class="queued-msg-actions">
               <button class="btn btn-sm btn-ghost" type="button" data-edit-queued-msg="${esc(msg.id || "")}">编辑</button>
               <button class="btn btn-sm btn-ghost" type="button" data-cancel-queued-msg="${esc(msg.id || "")}">取消</button>
             </span>`
      : "";
    const bubbleStateCls = isCancelledQueued
      ? "is-cancelled-queued"
      : (isQueued ? "is-queued-turn" : "");
    return `
          <div class="msg-user-wrap" data-msg-id="${esc(msg.id || "")}"${isRelayed ? ' data-relayed="1"' : ""}>
            <div class="msg-user-bubble ${bubbleStateCls} ${isRelayed ? "is-relayed-msg" : ""}">${esc(safeBody)}</div>
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
              ${relayedBadgeText ? `<span class="relayed-msg-badge">${esc(relayedBadgeText)}</span>` : ""}
              ${isCancelledQueued ? `<span class="cancelled-turn-pill">已取消排队</span>` : (isQueued ? `<span class="queued-turn-pill">等待下回合处理</span>` : "")}
              ${isEditedQueued && !isCancelledQueued ? `<span class="msg-meta-time">已编辑</span>` : ""}
              ${queueActionsHtml}
              <div class="msg-meta-time mono">${esc(formatClock(msg.created_at))}</div>
            </div>
          </div>`;
  }

  const TASK_REASSIGN_FROM_STATES = ["assigned", "queued", "rework", "failed", "escalated", "merge_failed"];

  function isTaskTerminalForApproval(taskStatus, taskKind = "general") {
    if (!taskStatus) return false;
    if (taskStatus === "missing") return true;
    if (taskStatus === "cancelled" || taskStatus === "merged" || taskStatus === "cancelling") return true;
    if (taskStatus === "accepted" && (taskKind || "general") === "general") return true;
    return false;
  }

  function expectedTaskStatusesForApproval(opType, category) {
    if (opType === "task.assign" || category === "step_dispatch") return ["created"];
    if (opType === "task.retry") return ["failed", "escalated"];
    if (opType === "task.reject") return ["submitted", "merge_failed"];
    if (opType === "task.reassign") {
      return category === "retry_over_limit" ? ["failed", "escalated"] : TASK_REASSIGN_FROM_STATES;
    }
    return null;
  }

  function isTaskProgressedForApproval(apr, taskStatus = null, taskKind = "general") {
    if (!apr) return false;
    if (
      apr.task_progressed === true ||
      apr.undecidable_reason === "task_progressed" ||
      apr.unapprovable_reason === "task_progressed"
    ) {
      return true;
    }
    const effStatus = taskStatus || apr.task_status || null;
    if (!effStatus) return false;
    if (isTaskTerminalForApproval(effStatus, taskKind || apr.task_kind || "general")) return false;
    const opObj = apr.op && apr.op._redacted === true ? apr.op.op : apr.op;
    const opType = apr.op_type || (opObj && typeof opObj === "object" ? opObj.op : null) || null;
    const expected = expectedTaskStatusesForApproval(opType, apr.category || null);
    if (!expected) return false;
    return !expected.includes(effStatus);
  }

  function isApprovalExpiredLocally(apr, nowMs = Date.now()) {
    if (!apr) return false;
    if (
      apr.status === "expired" ||
      apr.expired === true ||
      apr.undecidable_reason === "expired" ||
      apr.unapprovable_reason === "expired"
    ) {
      return true;
    }
    const status = apr.status || "pending";
    if ((status === "pending" || status === "approved") && typeof apr.expires_at === "string" && apr.expires_at) {
      const expMs = Date.parse(apr.expires_at);
      if (!Number.isNaN(expMs) && expMs < nowMs) return true;
    }
    return false;
  }

  function isPlanBlockedForApproval(apr, plan = null) {
    if (!apr) return false;
    const cat = apr.category || (plan ? "plan_dispatch" : "");
    const status = apr.status || "pending";
    if (apr.undecidable_reason && String(apr.undecidable_reason).startsWith("plan_")) return true;
    if (apr.unapprovable_reason && String(apr.unapprovable_reason).startsWith("plan_")) return true;
    if ((cat === "plan_dispatch" || plan) && plan && plan.status) {
      if (!["proposed", "approved_pending_dispatch"].includes(plan.status)) return true;
      if (status === "pending" && plan.status !== "proposed") return true;
    }
    return false;
  }

  function getApprovalUnapprovableReason(apr, opts = {}) {
    if (!apr) return null;
    const {
      plan = null,
      projectStatus = null,
      permissionMode = null,
      task = null,
      nowMs = Date.now()
    } = opts || {};
    const cat = apr.category || (plan ? "plan_dispatch" : "");
    const status = apr.status || "pending";
    const effMode = permissionMode || (plan && plan.permission_mode) || null;
    const linkedTask = task || (apr.task_id ? (STATE.tasks.find(x => x && x.id === apr.task_id) || null) : null);

    if (isApprovalExpiredLocally(apr, nowMs)) return "expired";
    if (status === "rejected" && apr.decided_via === "relayed") return "rejected";
    if (status && status !== "pending" && status !== "approved") return String(status);
    if (apr.undecidable_reason && String(apr.undecidable_reason).startsWith("plan_")) {
      return String(apr.undecidable_reason);
    }
    if (apr.unapprovable_reason && String(apr.unapprovable_reason).startsWith("plan_")) {
      return String(apr.unapprovable_reason);
    }
    if ((cat === "plan_dispatch" || plan) && plan && plan.status) {
      if (!["proposed", "approved_pending_dispatch"].includes(plan.status)) {
        return `plan_${plan.status}`;
      }
      if (status === "pending" && plan.status !== "proposed") {
        return `plan_${plan.status}`;
      }
    }
    if (
      apr.inactive_project === true ||
      apr.unapprovable_reason === "project_not_active" ||
      apr.undecidable_reason === "project_not_active" ||
      (projectStatus && projectStatus !== "active")
    ) {
      return "project_not_active";
    }
    if (
      apr.task_terminal === true ||
      apr.unapprovable_reason === "task_terminal" ||
      apr.undecidable_reason === "task_terminal"
    ) {
      return "task_terminal";
    }
    const effTaskStatus = (linkedTask && linkedTask.status) || apr.task_status || null;
    const effTaskKind = (linkedTask && linkedTask.kind) || apr.task_kind || "general";
    if (effTaskStatus && isTaskTerminalForApproval(effTaskStatus, effTaskKind)) {
      return "task_terminal";
    }
    if (isTaskProgressedForApproval(apr, effTaskStatus, effTaskKind)) {
      return "task_progressed";
    }
    if (apr.unapprovable_reason) {
      return String(apr.unapprovable_reason);
    }
    if (apr.decidable === false && apr.undecidable_reason) {
      return String(apr.undecidable_reason);
    }
    if ((cat === "plan_dispatch" || cat === "step_dispatch") && effMode === "auto" && status === "approved" && apr.decided_via === "direct") {
      return "already_auto_mode";
    }
    if (apr.undecidable_reason) {
      return String(apr.undecidable_reason);
    }
    return null;
  }

  function isDismissOnlyApproval(apr, opts = {}) {
    if (!apr || !apr.id) return false;
    const { plan = null, nowMs = Date.now() } = opts || {};
    const status = apr.status || "pending";
    if (status !== "pending") return false;
    if (apr.decidable === false) return false;
    if (isApprovalExpiredLocally(apr, nowMs)) return false;
    if (isPlanBlockedForApproval(apr, plan)) return false;
    if (Array.isArray(apr.available_actions) && !apr.available_actions.includes("reject")) {
      return false;
    }
    if (apr.dismiss_only === true) return true;
    const reason = getApprovalUnapprovableReason(apr, opts);
    return reason === "project_not_active" || reason === "task_terminal" || reason === "task_progressed";
  }

  function formatApprovalReasonBadgeLabel(reason) {
    if (reason === "expired") return "已过期";
    if (reason === "task_terminal") return "任务已终止";
    if (reason === "task_progressed") return "任务已推进";
    if (reason === "project_not_active") return "项目非进行中";
    if (reason && String(reason).startsWith("plan_")) return "计划已结束";
    return "暂不可操作";
  }

  function getApprovalUndecidableReason(apr, opts = {}) {
    if (!apr) return null;
    const { nowMs = Date.now() } = opts || {};
    const status = apr.status || "pending";
    if (apr.decidable === false) {
      return getApprovalUnapprovableReason(apr, opts) || (apr.undecidable_reason ? String(apr.undecidable_reason) : "undecidable");
    }
    if (isApprovalExpiredLocally(apr, nowMs)) return "expired";
    if (status === "rejected" && apr.decided_via === "relayed") {
      if (Array.isArray(apr.available_actions) && !apr.available_actions.includes("reject")) {
        return "rejected";
      }
      return null;
    }
    if (isDismissOnlyApproval(apr, opts)) {
      return null;
    }
    return getApprovalUnapprovableReason(apr, opts);
  }

  function formatUndecidableReasonHint(apr, opts = {}) {
    if (!apr) return "";
    const reason = getApprovalUndecidableReason(apr, opts);
    if (!reason) return "";
    if (reason === "expired") return "已过期（超过审批有效期，无法再操作）";
    if (reason === "project_not_active") return "当前项目未处于进行中状态（已暂停/已完成/已取消），无法操作此审批卡片";
    if (reason === "task_terminal") {
      const ts = (opts && opts.task && opts.task.status) || apr.task_status || "";
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || (ts === "missing" ? "不存在" : ts)) : "已终止";
      return `关联任务已终止（${tsLabel}），该审批卡片已失效`;
    }
    if (reason === "task_progressed") {
      const ts = (opts && opts.task && opts.task.status) || apr.task_status || "";
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || ts) : "后续阶段";
      return `关联任务已推进至「${tsLabel}」阶段，该审批卡片已过时失效`;
    }
    if (reason === "already_auto_mode") return "项目已处于全自动 (auto) 模式，无需再切换";
    if (reason.startsWith("plan_")) {
      const pst = reason.slice("plan_".length);
      const plLabel = (PLAN_STATUS_META[pst] && PLAN_STATUS_META[pst].label) || pst;
      return `关联计划已${plLabel}，无需再操作`;
    }
    if (reason === "consumed") return "该审批已被使用";
    if (reason === "rejected") return "该审批已被拒绝";
    if (reason === "cancelled") return "该审批已取消";
    if (reason === "already_decided") return "已完成审批";
    return "";
  }

  function formatUnapprovableReasonHint(apr, opts = {}) {
    if (!apr) return "";
    if (!isDismissOnlyApproval(apr, opts)) {
      return formatUndecidableReasonHint(apr, opts);
    }
    const reason = getApprovalUnapprovableReason(apr, opts);
    if (reason === "project_not_active") {
      return "当前项目未处于进行中状态（已暂停/已完成/已取消），无法批准；可点击「拒绝 / 清除」移除此卡片";
    }
    if (reason === "task_terminal") {
      const ts = (opts && opts.task && opts.task.status) || apr.task_status || "";
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || (ts === "missing" ? "不存在" : ts)) : "已终止";
      return `关联任务已终止（${tsLabel}），无法批准；可点击「拒绝 / 清除」移除此卡片`;
    }
    if (reason === "task_progressed") {
      const ts = (opts && opts.task && opts.task.status) || apr.task_status || "";
      const tsLabel = ts ? (TASK_STATE_LABELS[ts] || ts) : "后续阶段";
      return `关联任务已推进至「${tsLabel}」阶段，无法批准；可点击「拒绝 / 清除」移除此卡片`;
    }
    return "该审批卡片已失效，无法批准；可点击「拒绝 / 清除」移除此卡片";
  }

  function formatSweptApprovalsSuffix(cancelledSteps, cancelledPlans) {
    const sLen = Array.isArray(cancelledSteps) ? cancelledSteps.length : 0;
    const pLen = Array.isArray(cancelledPlans) ? cancelledPlans.length : 0;
    if (sLen > 0 && pLen > 0) {
      return `（已自动取消 ${sLen} 张待处理单步审批卡片与 ${pLen} 张待处理计划审批卡片）`;
    }
    if (sLen > 0) {
      return `（已自动取消 ${sLen} 张待处理的单步审批卡片）`;
    }
    if (pLen > 0) {
      return `（已自动取消 ${pLen} 张待处理的计划审批卡片，原计划可在全自动模式下直接派发）`;
    }
    return "";
  }

  function getApprovalCardActions(apr, opts = {}) {
    if (!apr || !apr.id) return [];
    const {
      plan = null,
      projectStatus = null,
      permissionMode = null,
      task = null,
      nowMs = Date.now()
    } = opts || {};
    const cat = apr.category || (plan ? "plan_dispatch" : "");
    const status = apr.status || "pending";
    const effMode = permissionMode || (plan && plan.permission_mode) || null;
    const linkedTask = task || (apr.task_id ? (STATE.tasks.find(x => x && x.id === apr.task_id) || null) : null);
    const hasHydratedActions = Array.isArray(apr.available_actions);

    // Strictly driven by backend-hydrated available_actions: never synthesize buttons when absent
    if (!hasHydratedActions) return [];

    // Hard suppression matching backend compute_approval_card_actions (PR #69 round 5)
    if (apr.decidable === false) return [];
    if (isApprovalExpiredLocally(apr, nowMs)) return [];

    // Relayed-rejected cards retain ["reject"] so direct users can upgrade to direct_verified
    if (status === "rejected" && apr.decided_via === "relayed") {
      return apr.available_actions.includes("reject") ? ["reject"] : [];
    }

    // Other terminal statuses (consumed / cancelled / expired / direct rejected) have zero actions
    if (status !== "pending" && status !== "approved") {
      return [];
    }

    // Plan-blocked cards have zero actions
    if (isPlanBlockedForApproval(apr, plan)) {
      return [];
    }

    const effTaskStatus = (linkedTask && linkedTask.status) || apr.task_status || null;
    const effTaskKind = (linkedTask && linkedTask.kind) || apr.task_kind || "general";
    const isInactiveProject =
      apr.inactive_project === true ||
      apr.unapprovable_reason === "project_not_active" ||
      apr.undecidable_reason === "project_not_active" ||
      Boolean(projectStatus && projectStatus !== "active");
    const isTaskTerminal =
      apr.task_terminal === true ||
      apr.unapprovable_reason === "task_terminal" ||
      apr.undecidable_reason === "task_terminal" ||
      Boolean(effTaskStatus && isTaskTerminalForApproval(effTaskStatus, effTaskKind));
    const isTaskProgressed = isTaskProgressedForApproval(apr, effTaskStatus, effTaskKind);

    // Dead cards (inactive project / terminal task / progressed task / dismiss_only):
    // Pending dead cards remain dismissible via ["reject"] only when hydrated available_actions includes "reject".
    if (isInactiveProject || isTaskTerminal || isTaskProgressed || apr.dismiss_only === true) {
      if (status === "pending") {
        return apr.available_actions.includes("reject") ? ["reject"] : [];
      }
      return [];
    }

    const opType = getApprovalOpType(apr);
    const isExternalOp = isExternalApprovalOp(apr);
    const isAutoModeCat = !isExternalOp && (
      (cat === "plan_dispatch" && (!opType || opType === "plan.dispatch")) ||
      (cat === "step_dispatch" && (!opType || opType === "task.assign"))
    );
    const alreadyAuto = isAutoModeCat && effMode === "auto";
    const eff = apr.always_allow_effect;
    const hasValidStandingGrantPreview = Boolean(
      !isExternalOp &&
      eff &&
      eff.kind === "standing_grant" &&
      Array.isArray(eff.scopes) &&
      eff.scopes.length > 0
    );

    const rawActions = apr.available_actions.slice();
    const disallowAlways =
      isExternalOp ||
      cat === "config" ||
      alreadyAuto ||
      eff === null ||
      (!isAutoModeCat && !hasValidStandingGrantPreview) ||
      (eff && eff.kind === "standing_grant" && (!Array.isArray(eff.scopes) || eff.scopes.length === 0));
    const valid = new Set(["allow", "always_allow", "reject"]);
    const seen = new Set();
    const out = [];
    for (const act of rawActions) {
      if (!valid.has(act) || seen.has(act)) continue;
      if (act === "always_allow" && disallowAlways) continue;
      seen.add(act);
      out.push(act);
    }
    return out;
  }

  function shouldRenderApprovalActions(apr, opts = {}) {
    return getApprovalCardActions(apr, opts).length > 0;
  }

  function formatSingleGrantScope(sc) {
    if (!sc || typeof sc !== "object") return "";
    if (sc.category === "permission_escalation") {
      const ag = sc.agent_name || sc.agent || sc.agent_id || "指定 Agent";
      const prof = sc.profile || "指定档位";
      const keyPart = sc.scope_key ? ` (${sc.scope_key})` : "";
      return `高危提权：${ag} · ${prof}${keyPart}`;
    }
    if (sc.category === "retry_over_limit") {
      const keyPart = sc.scope_key ? ` (${sc.scope_key})` : "";
      return `超限重试（本项目）${keyPart}`;
    }
    const label = GATE_CATEGORY_LABELS[sc.category] || sc.category || "授权";
    const parts = [label];
    const ag = sc.agent_name || sc.agent || sc.agent_id;
    if (ag) parts.push(ag);
    if (sc.profile) parts.push(sc.profile);
    if (sc.config_key) parts.push(sc.config_key);
    if (sc.scope_key) parts.push(`(${sc.scope_key})`);
    return parts.join(" · ");
  }

  function formatAlwaysAllowEffectHint(apr, plan = null) {
    if (!apr) return "";
    if (isExternalApprovalOp(apr)) return "";
    const cat = apr.category || (plan ? "plan_dispatch" : "");
    if (cat === "config") return "";
    const eff = apr.always_allow_effect;
    if (eff === null) return "";
    if (eff && eff.kind === "switch_permission_mode") {
      return "「始终允许」会将本项目权限模式切换为「全自动 (auto)」（弹窗二次确认；高危提权仍会单独弹窗）。";
    }
    if (eff && eff.kind === "standing_grant") {
      if (Array.isArray(eff.scopes) && eff.scopes.length > 0) {
        const items = eff.scopes.map(formatSingleGrantScope).filter(Boolean);
        if (!items.length) return "";
        if (items.length === 1) {
          return `「始终允许」将创建永久授权：${items[0]}（绝不放宽更高档位，可在「设置 → 永久授权」随时撤销）。`;
        }
        const numbered = items.map((item, idx) => `(${idx + 1}) ${item}`).join("；");
        return `「始终允许」将同时创建 ${items.length} 项永久授权：${numbered}（绝不放宽更高档位，可在「设置 → 永久授权」随时撤销）。`;
      }
      return "";
    }
    // Only plan_dispatch / step_dispatch have a safe unhydrated hint (the server's confirm_auto_mode
    // dialog previews the exact mode switch). Grant-minting categories never guess unhydrated scopes.
    if (cat === "plan_dispatch" || cat === "step_dispatch") {
      return "「始终允许」会将本项目权限模式切换为「全自动 (auto)」（弹窗二次确认；高危提权仍会单独弹窗）。";
    }
    return "";
  }

  function resolveHydratedApproval(approvalId) {
    if (!approvalId) return null;
    const fromChat = STATE.chatApprovalsById[approvalId] || null;
    const fromList = getApprovalFromListById(approvalId);
    if (!fromChat && !fromList) return null;
    // Prefer whichever hydrated source is freshest; approval.list with include_card_fields=true carries full card actions
    const listHasHydrated = Boolean(fromList && Array.isArray(fromList.available_actions));
    const chatHasHydrated = Boolean(fromChat && Array.isArray(fromChat.available_actions));
    const merged = (listHasHydrated && !chatHasHydrated)
      ? Object.assign({}, fromChat || {}, fromList || {})
      : Object.assign({}, fromList || {}, fromChat || {});
    if (
      fromChat && fromList &&
      fromChat.status && fromList.status &&
      fromChat.status !== fromList.status &&
      ["rejected", "cancelled", "expired", "consumed"].includes(fromList.status)
    ) {
      merged.status = fromList.status;
      merged.decided_via = fromList.decided_via || merged.decided_via || null;
      merged.always_allow_effect = null;
      merged.approvable = false;
      merged.dismiss_only = false;
      if (fromList.status === "rejected" && fromList.decided_via === "relayed") {
        merged.available_actions = Array.isArray(fromList.available_actions)
          ? fromList.available_actions.slice()
          : [];
        merged.decidable = merged.available_actions.length > 0;
        merged.undecidable_reason = merged.decidable ? null : "rejected";
        merged.unapprovable_reason = "rejected";
      } else {
        merged.available_actions = [];
        merged.decidable = false;
        merged.undecidable_reason = fromList.status;
        merged.unapprovable_reason = fromList.status;
        if (fromList.status === "expired") merged.expired = true;
      }
    }
    return merged;
  }

  function resolvePlanApproval(plan) {
    if (!plan || !plan.approval_id) return null;
    const fromList = getApprovalFromListById(plan.approval_id);
    const embedded = (plan.approval && typeof plan.approval === "object") ? plan.approval : null;
    const fromChat = STATE.chatApprovalsById[plan.approval_id] || null;
    if (!fromList && !embedded && !fromChat) {
      // Never fabricate available_actions when approval state has not been hydrated by the server
      return null;
    }
    const base = {
      id: plan.approval_id,
      category: "plan_dispatch",
      plan_id: plan.id,
      plan_version: plan.version,
      plan_hash: plan.plan_hash
    };
    const merged = Object.assign(base, fromList || {}, embedded || {}, fromChat || {});
    if (
      fromList &&
      fromList.status &&
      ["rejected", "cancelled", "expired", "consumed"].includes(fromList.status) &&
      merged.status !== fromList.status
    ) {
      merged.status = fromList.status;
      merged.decided_via = fromList.decided_via || merged.decided_via || null;
      merged.always_allow_effect = null;
      merged.approvable = false;
      merged.dismiss_only = false;
      if (fromList.status === "rejected" && fromList.decided_via === "relayed") {
        merged.available_actions = Array.isArray(fromList.available_actions)
          ? fromList.available_actions.slice()
          : [];
        merged.decidable = merged.available_actions.length > 0;
        merged.undecidable_reason = merged.decidable ? null : "rejected";
        merged.unapprovable_reason = "rejected";
      } else {
        merged.available_actions = [];
        merged.decidable = false;
        merged.undecidable_reason = fromList.status;
        merged.unapprovable_reason = fromList.status;
        if (fromList.status === "expired") merged.expired = true;
      }
    }
    return merged;
  }

  function markApprovalsTerminalLocally(approvalIds, nextStatus = "cancelled") {
    if (!Array.isArray(approvalIds) || !approvalIds.length) return;
    const idSet = new Set(approvalIds.filter(Boolean));
    if (!idSet.size) return;
    const patch = {
      status: nextStatus,
      available_actions: [],
      always_allow_effect: null,
      decidable: false,
      approvable: false,
      dismiss_only: false,
      undecidable_reason: nextStatus,
      unapprovable_reason: nextStatus,
      expired: nextStatus === "expired"
    };
    idSet.forEach(aid => {
      if (STATE.chatApprovalsById[aid]) {
        STATE.chatApprovalsById[aid] = Object.assign({}, STATE.chatApprovalsById[aid], patch);
      }
    });
    setApprovals(STATE.approvals.map(a => (
      a && idSet.has(a.id)
        ? Object.assign({}, a, patch)
        : a
    )));
    STATE.plans = STATE.plans.map(p => (
      p && p.approval_id && idSet.has(p.approval_id) && p.approval
        ? Object.assign({}, p, { approval: Object.assign({}, p.approval, patch) })
        : p
    ));
    Object.keys(STATE.chatPlansById).forEach(pid => {
      const p = STATE.chatPlansById[pid];
      if (p && p.approval_id && idSet.has(p.approval_id) && p.approval) {
        STATE.chatPlansById[pid] = Object.assign({}, p, { approval: Object.assign({}, p.approval, patch) });
      }
    });
  }

  function markApprovalsSuppressedLocally(approvalIds, { reason = "undecidable", taskStatus = null, taskTerminal = false, taskProgressed = false, inactiveProject = false } = {}) {
    if (!Array.isArray(approvalIds) || !approvalIds.length) return;
    const idSet = new Set(approvalIds.filter(Boolean));
    if (!idSet.size) return;
    const isDeadDismissReason =
      reason === "project_not_active" ||
      reason === "task_terminal" ||
      reason === "task_progressed";
    const buildPatch = existing => {
      const status = (existing && existing.status) || "pending";
      const dismissOnly = status === "pending" && isDeadDismissReason;
      const patch = {
        available_actions: dismissOnly ? ["reject"] : [],
        always_allow_effect: null,
        decidable: dismissOnly,
        approvable: false,
        dismiss_only: dismissOnly,
        undecidable_reason: dismissOnly ? null : reason,
        unapprovable_reason: reason
      };
      if (taskStatus) {
        patch.task_status = taskStatus;
      }
      if (taskTerminal || reason === "task_terminal") {
        patch.task_terminal = true;
      }
      if (taskProgressed || reason === "task_progressed") {
        patch.task_progressed = true;
      }
      if (inactiveProject || reason === "project_not_active") {
        patch.inactive_project = true;
      }
      return patch;
    };
    idSet.forEach(aid => {
      if (STATE.chatApprovalsById[aid]) {
        const cur = STATE.chatApprovalsById[aid];
        STATE.chatApprovalsById[aid] = Object.assign({}, cur, buildPatch(cur));
      }
    });
    setApprovals(STATE.approvals.map(a => (
      a && idSet.has(a.id)
        ? Object.assign({}, a, buildPatch(a))
        : a
    )));
    STATE.plans = STATE.plans.map(p => (
      p && p.approval_id && idSet.has(p.approval_id) && p.approval
        ? Object.assign({}, p, { approval: Object.assign({}, p.approval, buildPatch(p.approval)) })
        : p
    ));
    Object.keys(STATE.chatPlansById).forEach(pid => {
      const p = STATE.chatPlansById[pid];
      if (p && p.approval_id && idSet.has(p.approval_id) && p.approval) {
        STATE.chatPlansById[pid] = Object.assign({}, p, { approval: Object.assign({}, p.approval, buildPatch(p.approval)) });
      }
    });
  }

  function renderApprovalButtonsFooterHtml(apr, { plan = null, compact = false, projectStatus = null, permissionMode = null, task = null } = {}) {
    if (!apr || !apr.id) return "";
    const cardOpts = { plan, projectStatus, permissionMode, task };
    const actions = getApprovalCardActions(apr, cardOpts);
    if (!actions.length) return "";
    const cat = apr.category || (plan ? "plan_dispatch" : "");
    const allow = canMutate() && !STATE.ui.decidingApprovalIds.has(apr.id);
    const disAttr = allow ? "" : `disabled title="${esc(STATE.ui.decidingApprovalIds.has(apr.id) ? "正在提交审批..." : disabledReason())}"`;
    const hasAllow = actions.includes("allow");
    const hasAlways = actions.includes("always_allow");
    const hasReject = actions.includes("reject");

    const planHash = (plan && plan.plan_hash) || apr.plan_hash || "";
    const planId = (plan && plan.id) || apr.plan_id || "";
    const planVersion = (plan && plan.version !== undefined && plan.version !== null)
      ? String(plan.version)
      : (apr.plan_version !== undefined && apr.plan_version !== null ? String(apr.plan_version) : "");
    const opHash = apr.op_hash || "";

    const bindAttrs = [
      `data-decide-approval="${esc(apr.id)}"`,
      `data-decide-category="${esc(cat)}"`,
      opHash ? `data-op-hash="${esc(opHash)}"` : "",
      planHash ? `data-plan-hash="${esc(planHash)}"` : "",
      planId ? `data-plan-id="${esc(planId)}"` : "",
      planVersion ? `data-plan-version="${esc(planVersion)}"` : ""
    ].filter(Boolean).join(" ");

    const allowLabel = (apr.status === "approved" && apr.decided_via === "relayed")
      ? "升级为直连允许"
      : "允许";
    const isRelayedRejected = apr.status === "rejected" && apr.decided_via === "relayed";
    const isDismissOnly = isDismissOnlyApproval(apr, cardOpts);
    const rejectLabel = isRelayedRejected
      ? "升级为直连拒绝"
      : (isDismissOnly ? "拒绝 / 清除" : "拒绝");
    const hintText = (!compact && hasAlways) ? formatAlwaysAllowEffectHint(apr, plan) : "";
    const reasonInputHtml = (!compact && hasReject)
      ? `<input class="compact-input approval-reason-input" type="text" data-decide-reason="${esc(apr.id)}" aria-label="拒绝原因或备注（可选）" placeholder="拒绝原因或备注（可选）..." ${allow ? "" : "disabled"} />`
      : "";

    return `
      <div class="${compact ? "approval-btn-group" : "approval-card-actions"}">
        ${hintText ? `<div class="approval-scope-hint">${esc(hintText)}</div>` : ""}
        ${compact ? "" : `<div class="approval-card-controls">${reasonInputHtml}<div class="approval-btn-group">`}
          ${hasAllow ? `<button class="btn btn-sm btn-primary" type="button" ${bindAttrs} data-decide-action="allow" ${disAttr}>${esc(allowLabel)}</button>` : ""}
          ${hasAlways ? `<button class="btn btn-sm btn-always-allow" type="button" ${bindAttrs} data-decide-action="always_allow" ${disAttr}>始终允许</button>` : ""}
          ${hasReject ? `<button class="btn btn-sm btn-danger-text" type="button" ${bindAttrs} data-decide-action="reject" ${compact ? 'data-decide-prompt-reason="1"' : ""} ${disAttr}>${esc(rejectLabel)}</button>` : ""}
        ${compact ? "" : `</div></div>`}
      </div>`;
  }

  function renderPlanCardHtml(plan) {
    if (!plan) return "";
    const status = plan.status || "proposed";
    const planTasks = STATE.tasks.filter(t => t.plan_id === plan.id);
    const totalTasks = planTasks.length || (plan.progress && plan.progress.total) || 0;
    const doneTasks = planTasks.length
      ? planTasks.filter(t => t.status === "merged" || t.status === "cancelled" || (t.status === "accepted" && (t.kind || "general") === "general")).length
      : ((plan.progress && plan.progress.completed) || 0);
    const allDone = status === "dispatched" && totalTasks > 0 && doneTasks >= totalTasks;

    const planApr = plan.approval_id ? resolvePlanApproval(plan) : null;
    const activePrj = getActiveProject();
    const matchedPrj = (activePrj && activePrj.id === plan.project_id)
      ? activePrj
      : (STATE.projects.find(p => p && p.id === plan.project_id) || null);
    const effProjectStatus = (matchedPrj && matchedPrj.status) ||
      (plan.project_id && STATE.activeProjectId === plan.project_id ? STATE.chatProjectStatus : null) ||
      null;
    const effMode = plan.permission_mode ||
      (matchedPrj && matchedPrj.permission_mode) ||
      (plan.project_id && STATE.activeProjectId === plan.project_id ? STATE.chatPermissionMode : null) ||
      null;
    const planCardOpts = { plan, projectStatus: effProjectStatus, permissionMode: effMode };
    const planCanAct = Boolean(planApr && shouldRenderApprovalActions(planApr, planCardOpts));
    const planDismissOnly = Boolean(planApr && planCanAct && isDismissOnlyApproval(planApr, planCardOpts));
    const planUnapprovableReason = planApr ? getApprovalUnapprovableReason(planApr, planCardOpts) : null;
    const isAutoSweptProposed = Boolean(
      status === "proposed" &&
      !planCanAct &&
      (effMode === "auto" || (planApr && planApr.status === "cancelled"))
    );

    let stMeta = PLAN_STATUS_META[status] || { label: status, cls: "neutral" };
    if (allDone) {
      stMeta = { label: `全部完成 (${doneTasks}/${totalTasks})`, cls: "green" };
    } else if (status === "dispatched" && totalTasks > 0) {
      stMeta = { label: `执行中 (${doneTasks}/${totalTasks})`, cls: "blue" };
    } else if (isAutoSweptProposed) {
      stMeta = { label: "全自动待派发", cls: "blue" };
    } else if (status === "proposed" && planApr && isApprovalExpiredLocally(planApr)) {
      stMeta = { label: "审批已过期", cls: "neutral" };
    } else if (
      status === "proposed" &&
      planApr &&
      (planApr.inactive_project || planUnapprovableReason === "project_not_active" || planApr.undecidable_reason === "project_not_active")
    ) {
      stMeta = { label: "项目非进行中", cls: "neutral" };
    } else if (status === "rejected" && planApr && planApr.status === "rejected" && planApr.decided_via === "relayed" && planCanAct) {
      stMeta = { label: "转发已拒绝", cls: "amber" };
    }

    const cardCls = allDone ? "status-done" : `status-${status}`;
    const steps = Array.isArray(plan.steps) ? plan.steps : [];
    const stepIndexMap = new Map(steps.map((s, i) => [s.key, i + 1]));

    const stepsHtml = steps.map((st, idx) => {
      const stepKey = st.key || String(idx + 1);
      const deps = Array.isArray(st.depends_on) && st.depends_on.length
        ? `依赖 ${st.depends_on.map(k => `#${stepIndexMap.get(k) || k}`).join("、")}`
        : "";
      const workerChip = `${st.assignee || "Worker"}${st.profile ? ` · ${st.profile}` : ""}`;
      const metaChips = [`<span class="plan-worker-chip mono">${esc(workerChip)}</span>`];
      if (st.kind) {
        metaChips.push(`<span class="plan-worker-chip mono" data-step-kind="${esc(st.kind)}">${esc(st.kind)}</span>`);
      }
      if (st.model) {
        metaChips.push(`<span class="plan-worker-chip mono" data-step-model="${esc(st.model)}">model: ${esc(st.model)}</span>`);
      }
      if (st.effort) {
        metaChips.push(`<span class="plan-worker-chip mono" data-step-effort="${esc(st.effort)}">effort: ${esc(st.effort)}</span>`);
      }
      if (typeof st.timeout_s === "number" && st.timeout_s > 0) {
        metaChips.push(`<span class="plan-worker-chip mono">超时 ${esc(st.timeout_s)}s</span>`);
      }
      if (typeof st.idle_timeout_s === "number" && st.idle_timeout_s > 0) {
        metaChips.push(`<span class="plan-worker-chip mono">空闲超时 ${esc(st.idle_timeout_s)}s</span>`);
      }
      if (typeof st.max_attempts === "number" && st.max_attempts > 0) {
        metaChips.push(`<span class="plan-worker-chip mono">最多 ${esc(st.max_attempts)} 次</span>`);
      }
      if (typeof st.priority === "number" && st.priority !== 0) {
        metaChips.push(`<span class="plan-worker-chip mono">优先级 ${esc(st.priority)}</span>`);
      }
      const escChip = st.requires_escalation
        ? `<span class="plan-escalation-chip">需单独授权提权</span>`
        : "";
      const rawSpec = typeof st.spec === "string" ? st.spec : "";
      const specLines = rawSpec ? rawSpec.split(/\r?\n/) : [];
      const isLongSpec = rawSpec.length > PLAN_STEP_SPEC_COLLAPSE_CHARS || specLines.length > PLAN_STEP_SPEC_COLLAPSE_LINES;
      const specPreviewOneLine = rawSpec ? rawSpec.replace(/\s+/g, " ").trim().slice(0, 72) : "";
      const specSummaryLabel = rawSpec
        ? (isLongSpec
          ? `查看完整步骤指令 (spec · ${rawSpec.length} 字)：${specPreviewOneLine}${rawSpec.length > 72 ? "…" : ""}`
          : "步骤指令 (spec)")
        : "步骤指令 (spec：空)";
      const specDetailsHtml = rawSpec
        ? `<details class="plan-step-spec-details" data-details-key="plan:${esc(plan.id)}:step:${esc(stepKey)}"${isLongSpec ? "" : " open"}>
            <summary>${esc(specSummaryLabel)}</summary>
            <pre class="log-box plan-step-spec-pre">${esc(rawSpec)}</pre>
          </details>`
        : "";
      const warningsHtml = Array.isArray(st.warnings) && st.warnings.length
        ? st.warnings.map(w => `<div class="plan-step-warning-note">${esc(w)}</div>`).join("")
        : "";
      return `
        <div class="plan-step-row" data-plan-step-key="${esc(stepKey)}">
          <div class="plan-step-left">
            <span class="plan-step-num">${idx + 1}</span>
            <div class="plan-step-info">
              <div class="plan-step-title">${esc(st.title || st.key)}${st.key && st.title && st.title !== st.key ? ` <span class="mono" style="font-size:11px;color:var(--text-muted);">(${esc(st.key)})</span>` : ""}</div>
              <div class="plan-step-meta">
                ${metaChips.join("")}
                ${deps ? `<span>${esc(deps)}</span>` : ""}
                ${escChip}
              </div>
              ${warningsHtml}
              ${specDetailsHtml}
            </div>
          </div>
        </div>`;
    }).join("");

    const isFolded = ["superseded", "cancelled", "rejected"].includes(status);
    let bodySection = "";
    if (isFolded) {
      bodySection = `
        <details class="plan-folded-details" data-details-key="plan:${esc(plan.id)}">
          <summary>查看计划步骤 (${steps.length} 步)</summary>
          <div class="plan-steps-list" style="padding:10px 0 0;">${stepsHtml}</div>
        </details>`;
    } else if (status !== "dispatched" || !planTasks.length) {
      bodySection = `<div class="plan-steps-list">${stepsHtml}</div>`;
    }

    // Bottom section: Inline approval card footer (driven strictly by hydrated available_actions) OR nested live tasks
    let footerSection = "";
    if (status === "proposed" && plan.approval_id) {
      if (planApr && planCanAct) {
        if (planDismissOnly) {
          const dismissHint = formatUnapprovableReasonHint(planApr, planCardOpts) || "该计划审批卡片已失效，仅可拒绝 / 清除";
          footerSection = `
            <div class="plan-card-notice" data-dismiss-only="1" data-unapprovable-reason="${esc(planUnapprovableReason || "dismiss_only")}">
              ${esc(dismissHint)}
            </div>
            ${renderApprovalButtonsFooterHtml(planApr, planCardOpts)}`;
        } else {
          footerSection = renderApprovalButtonsFooterHtml(planApr, planCardOpts);
        }
      } else if (isAutoSweptProposed) {
        footerSection = `
          <div class="plan-card-notice">
            当前项目已切换为「全自动 (auto)」模式，原计划审批卡片已自动取消，Lead 可直接派发执行该计划。
          </div>`;
      } else if (planApr && !Array.isArray(planApr.available_actions)) {
        footerSection = `
          <div class="plan-card-notice" data-unhydrated-approval="1">
            审批状态加载中，请刷新加载后操作。
          </div>`;
      } else if (planApr) {
        const reasonHint = formatUndecidableReasonHint(planApr, planCardOpts) || "该计划审批卡片当前无法操作";
        footerSection = `
          <div class="plan-card-notice">
            ${esc(reasonHint)}
          </div>`;
      } else {
        footerSection = `
          <div class="plan-card-notice" data-unhydrated-approval="1">
            审批状态尚未同步，请刷新加载后操作。
          </div>`;
      }
    } else if (status === "proposed" && effMode === "auto") {
      footerSection = `
        <div class="plan-card-notice">
          当前项目处于「全自动 (auto)」模式，无需确认计划卡片，Lead 可直接派发执行。
        </div>`;
    } else if (status === "approved_pending_dispatch") {
      const upgradeFooter = planApr && shouldRenderApprovalActions(planApr, planCardOpts)
        ? renderApprovalButtonsFooterHtml(planApr, planCardOpts)
        : "";
      let noticeText = "计划已通过审批，正在等待 Lead 自动派发任务…";
      if (planApr && !upgradeFooter) {
        if (isApprovalExpiredLocally(planApr)) {
          noticeText = "原计划审批已过期，无法继续派发。";
        } else if (planApr.inactive_project || planApr.undecidable_reason === "project_not_active" || (effProjectStatus && effProjectStatus !== "active")) {
          noticeText = "计划已通过审批，但当前项目未处于进行中状态。";
        }
      }
      footerSection = `
        <div class="plan-card-notice">
          ${esc(noticeText)}
        </div>
        ${upgradeFooter}`;
    } else if (status === "rejected" && planApr && planApr.status === "rejected" && planApr.decided_via === "relayed" && planCanAct) {
      footerSection = `
        <div class="plan-card-notice">
          该计划已被外部转发拒绝（待直连确认），可点击下方按钮升级为本机直连签名确认。
        </div>
        ${renderApprovalButtonsFooterHtml(planApr, planCardOpts)}`;
    } else if (status === "dispatched") {
      const pct = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;
      const orderedTasks = [];
      const matchedIds = new Set();
      steps.forEach(st => {
        const hit = planTasks.find(t => t.plan_step_key === st.key);
        if (hit) {
          orderedTasks.push(hit);
          matchedIds.add(hit.id);
        }
      });
      planTasks.forEach(t => {
        if (!matchedIds.has(t.id)) orderedTasks.push(t);
      });

      const taskRowsHtml = orderedTasks.map((rawT, idx) => {
        const t = enrichTask(rawT);
        const dur = getTaskDurationInfo(t);
        const liveAttr = dur.isLive && dur.startIso ? `data-duration-start="${esc(dur.startIso)}"` : "";
        const selected = !STATE.ui.inspectorCollapsed && STATE.activeTaskId === t.id ? "selected" : "";
        const subText = getTaskLatestPreview(t) || `${t.assignee || "未指派"}`;
        return `
          <button class="plan-task-row ${selected}" type="button" data-select-task="${esc(t.id)}">
            <span class="plan-task-left">
              <span class="dot ${getStatusDotClass(t)}" style="margin-top:6px;"></span>
              <span class="plan-task-info">
                <span class="plan-task-title">#${idx + 1} ${esc(t.title || t.id)}</span>
                <span class="plan-task-sub">${esc(getTaskHumanStatus(t))} · ${esc(subText)}</span>
              </span>
            </span>
            <span class="plan-task-right">
              <span class="mono">${esc(t.assignee || "--")}</span>
              <span class="mono" ${liveAttr}>${esc(dur.text)}</span>
              <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>
            </span>
          </button>`;
      }).join("");

      footerSection = `
        <div class="plan-progress-bar-wrap">
          <div class="plan-progress-track"><div class="plan-progress-fill ${allDone ? "done" : ""}" style="width:${pct}%;"></div></div>
          <span class="plan-progress-label mono">${doneTasks}/${totalTasks} 已完成</span>
        </div>
        <div class="plan-tasks-list">${taskRowsHtml || `<div class="empty-state" style="padding:10px;">任务正在同步...</div>`}</div>`;
    }

    return `
      <div class="plan-card ${esc(cardCls)}" data-plan-id="${esc(plan.id)}" ${plan.approval_id ? `data-card-approval-id="${esc(plan.approval_id)}"` : ""}>
        <div class="plan-card-head">
          <div class="plan-card-title-wrap">
            <div class="plan-card-kicker">
              <span class="plan-version-tag">计划 v${esc(plan.version || 1)}</span>
              <span class="status-pill ${esc(stMeta.cls)}"${planDismissOnly && planUnapprovableReason ? ` data-unapprovable-reason="${esc(planUnapprovableReason)}"` : ""}>${esc(stMeta.label)}</span>
            </div>
            <div class="plan-card-title">${esc(plan.title || "执行计划")}</div>
          </div>
          <span class="msg-meta-time mono">${esc(formatClock(plan.created_at))}</span>
        </div>
        ${plan.summary ? `<div class="plan-card-summary">${esc(plan.summary)}</div>` : ""}
        ${bodySection}
        ${footerSection}
      </div>`;
  }

  function renderGateCardHtml(apr, fallbackBody, msgMeta = null) {
    if (!apr) {
      return `
        <div class="gate-card">
          <div class="gate-card-head"><span class="gate-card-title">审批卡片</span></div>
          <div class="gate-card-body">${esc(fallbackBody || "")}</div>
        </div>`;
    }
    const catLabel = GATE_CATEGORY_LABELS[apr.category] || apr.category || "安全确认";
    const isExternalOp = isExternalApprovalOp(apr);
    const externalBadgeHtml = isExternalOp
      ? `<span class="status-pill neutral" data-external-op-badge="1">外部请求</span>`
      : "";
    const policy = resolveApprovalPolicy(apr);
    const isDanger = apr.category === "permission_escalation" || apr.category === "config" || policy === "direct_only";
    const status = apr.status || (Array.isArray(apr.available_actions) ? "pending" : "unknown");
    const isUnhydrated = !Array.isArray(apr.available_actions);
    const linkedTask = apr.task_id ? STATE.tasks.find(x => x.id === apr.task_id) : null;
    const activePrj = getActiveProject();
    const aprPrj = apr.project_id
      ? ((activePrj && activePrj.id === apr.project_id) ? activePrj : (STATE.projects.find(p => p && p.id === apr.project_id) || null))
      : null;
    const effProjectStatus = apr.project_id
      ? ((aprPrj && aprPrj.status) || (STATE.activeProjectId === apr.project_id ? STATE.chatProjectStatus : null) || null)
      : null;
    const effPermMode = apr.project_id
      ? ((aprPrj && aprPrj.permission_mode) || (STATE.activeProjectId === apr.project_id ? STATE.chatPermissionMode : null) || null)
      : null;
    const cardOpts = {
      task: linkedTask,
      projectStatus: effProjectStatus,
      permissionMode: effPermMode
    };
    const canAct = shouldRenderApprovalActions(apr, cardOpts);
    const isExpired = isApprovalExpiredLocally(apr);
    const isDismissOnly = canAct && isDismissOnlyApproval(apr, cardOpts);
    const unapprovableReason = getApprovalUnapprovableReason(apr, cardOpts);
    const undecidableReason = canAct ? null : getApprovalUndecidableReason(apr, cardOpts);
    const isSuppressedPending = !isUnhydrated && status === "pending" && !canAct;

    const cardStateCls = isExpired
      ? "is-rejected"
      : (isUnhydrated
        ? ""
        : (status === "approved" || status === "consumed"
          ? "is-resolved"
          : (status === "rejected" || status === "cancelled" || status === "expired" || isSuppressedPending || isDismissOnly
            ? "is-rejected"
            : (isDanger ? "is-danger" : ""))));

    const isRelayedApproved = status === "approved" && apr.decided_via === "relayed" && !isExpired;
    const isRelayedRejected = status === "rejected" && apr.decided_via === "relayed" && !isExpired;
    const resolvedStatusLabel = isExpired
      ? "已过期"
      : (isUnhydrated
        ? "待同步"
        : (isRelayedApproved
          ? "已允许（经由转发，待直连确认）"
          : (isRelayedRejected
            ? "已拒绝（经由转发，待直连确认）"
            : ((status === "approved" || status === "consumed")
              ? "已允许"
              : (status === "rejected"
                ? "已拒绝"
                : (status === "cancelled"
                  ? "已取消"
                  : (status === "expired" ? "已过期" : "已结束")))))));

    const suppressedBadgeLabel = formatApprovalReasonBadgeLabel(undecidableReason);
    const dismissBadgeLabel = formatApprovalReasonBadgeLabel(unapprovableReason);

    const statusBadge = isExpired
      ? `<span class="status-pill neutral">已过期</span>`
      : (isUnhydrated
        ? `<span class="status-pill neutral">加载中</span>`
        : (isDismissOnly
          ? `<span class="status-pill neutral" data-unapprovable-reason="${esc(unapprovableReason || "dismiss_only")}">${esc(dismissBadgeLabel)}</span>`
          : (isSuppressedPending
            ? `<span class="status-pill neutral">${esc(suppressedBadgeLabel)}</span>`
            : (status === "pending"
              ? `<span class="status-pill ${isDanger ? "red" : "amber"}">待你审批</span>`
              : (isRelayedApproved
                ? `<span class="status-pill amber">转发已允许</span>`
                : (isRelayedRejected
                  ? `<span class="status-pill amber">转发已拒绝</span>`
                  : ((status === "approved" || status === "consumed")
                    ? `<span class="status-pill green">已允许</span>`
                    : `<span class="status-pill neutral">${esc(resolvedStatusLabel)}</span>`)))))));

    const effectiveTaskStatus = apr.task_status || (linkedTask && linkedTask.status) || null;
    const taskStatusSuffix = effectiveTaskStatus
      ? ` · ${TASK_STATE_LABELS[effectiveTaskStatus] || (effectiveTaskStatus === "missing" ? "已不存在" : effectiveTaskStatus)}`
      : "";
    const taskInfo = apr.task_id
      ? (linkedTask ? `关联任务：${linkedTask.title || linkedTask.id}${linkedTask.assignee ? ` (${linkedTask.assignee})` : ""}${taskStatusSuffix}` : `关联任务：${apr.task_id}${taskStatusSuffix}`)
      : "";
    const opObj = apr.op && apr.op._redacted === true ? apr.op.op : apr.op;
    const reqProfile =
      apr.requested_profile ||
      (opObj && opObj.params && typeof opObj.params.profile === "string" ? opObj.params.profile : "") ||
      (msgMeta && msgMeta.profile) ||
      "";
    const profileInfo = reqProfile ? `请求权限档位：${reqProfile}` : "";

    const undecidableHint = !canAct ? formatUndecidableReasonHint(apr, cardOpts) : "";
    const dismissHint = isDismissOnly ? formatUnapprovableReasonHint(apr, cardOpts) : "";
    let resolvedBarHtml = "";
    if (isUnhydrated) {
      resolvedBarHtml = `<div class="gate-resolved-bar" data-unhydrated-approval="1">审批状态尚未加载，请刷新加载后操作</div>`;
    } else if (isDismissOnly) {
      resolvedBarHtml = `<div class="gate-resolved-bar" data-dismiss-only="1" data-unapprovable-reason="${esc(unapprovableReason || "dismiss_only")}">${esc(dismissHint || "该审批卡片已失效，仅可拒绝 / 清除")}</div>`;
    } else if (isSuppressedPending) {
      resolvedBarHtml = `<div class="gate-resolved-bar" data-undecidable-reason="${esc(undecidableReason || "suppressed")}">${esc(undecidableHint || "该审批卡片当前不可操作")}</div>`;
    } else if (status !== "pending") {
      const extraHint = (status === "approved" && !canAct && undecidableHint && !["already_decided", "consumed"].includes(undecidableReason || ""))
        ? ` · ${undecidableHint}`
        : "";
      resolvedBarHtml = `<div class="gate-resolved-bar"${undecidableReason ? ` data-undecidable-reason="${esc(undecidableReason)}"` : ""}>${esc(resolvedStatusLabel)}${esc(extraHint)}${apr.decision_note ? ` · 备注：「${esc(apr.decision_note)}」` : ""}</div>`;
    }
    const actionsFooterHtml = canAct
      ? renderApprovalButtonsFooterHtml(apr, cardOpts)
      : "";
    const footerHtml = `${resolvedBarHtml}${actionsFooterHtml}`;

    const dotCls = (isExpired || isSuppressedPending || isDismissOnly)
      ? "dot-neutral"
      : (status === "pending"
        ? (isDanger ? "dot-red" : "dot-amber")
        : (status === "approved" || status === "consumed" ? "dot-green" : "dot-neutral"));

    return `
      <div class="gate-card ${esc(cardStateCls)}" data-gate-approval-id="${esc(apr.id)}" data-card-approval-id="${esc(apr.id)}"${isDismissOnly ? ' data-dismiss-only="1"' : ""}${isExternalOp ? ' data-external-op="1"' : ""}>
        <div class="gate-card-head">
          <div class="gate-card-title">
            <span class="dot ${dotCls}"></span>
            <span>${esc(catLabel)}</span>
            ${externalBadgeHtml}
            ${statusBadge}
          </div>
          <div style="display:flex;align-items:center;gap:4px;">
            ${apr.task_id ? `<button class="btn btn-sm btn-ghost" type="button" data-select-task="${esc(apr.task_id)}">打开 Worker 标签</button>` : ""}
            <button class="btn btn-sm btn-ghost" type="button" data-open-advanced-gate="${esc(apr.id)}">载荷详情</button>
          </div>
        </div>
        <div class="gate-card-body">
          <div class="gate-card-summary">${esc(apr.summary || fallbackBody || formatApprovalOpName(apr))}</div>
          ${profileInfo ? `<div class="mono">${esc(profileInfo)}</div>` : ""}
          ${taskInfo ? `<div>${esc(taskInfo)}</div>` : ""}
        </div>
        ${footerHtml}
      </div>`;
  }

  function getUnresolvedLeadFailure(projectId) {
    if (!projectId) return null;
    let latestFail = null;
    let latestRecoverSeq = -1;
    for (const ev of STATE.events) {
      if (!ev || ev.project_id !== projectId) continue;
      const seq = typeof ev.seq === "number" ? ev.seq : 0;
      if (ev.type === "lead.turn_finished" && ev.payload && (ev.payload.ok === true || ev.payload.interrupted === true)) {
        if (seq > latestRecoverSeq) latestRecoverSeq = seq;
      } else if (ev.type === "lead.interrupted") {
        if (seq > latestRecoverSeq) latestRecoverSeq = seq;
      } else if (ev.type === "chat.message" && ev.payload && ev.payload.role === "lead") {
        if (seq > latestRecoverSeq) latestRecoverSeq = seq;
      } else if (ev.type === "lead.turn_finished" && ev.payload && ev.payload.ok === false) {
        if (!latestFail || seq >= latestFail.seq) {
          latestFail = {
            kind: "turn_failed",
            exit_code: ev.payload.exit_code,
            run_id: ev.run_id || ev.payload.run_id || "",
            seq,
            ts: ev.ts || ""
          };
        }
      } else if (ev.type === "lead.unreachable") {
        if (!latestFail || seq >= latestFail.seq) {
          latestFail = {
            kind: "unreachable",
            attempts: ev.payload && ev.payload.attempts,
            run_id: ev.run_id || (ev.payload && ev.payload.run_id) || "",
            seq,
            ts: ev.ts || ""
          };
        }
      }
    }
    const cached = STATE.leadFailureByProject[projectId];
    if (cached && (!latestFail || (cached.seq || 0) >= latestFail.seq)) {
      latestFail = cached;
    }
    if (!latestFail) return null;
    if (latestFail.seq && latestRecoverSeq > latestFail.seq) return null;
    return latestFail;
  }

  function renderLeadFailureNoticeHtml(fail, leadName) {
    if (!fail) return "";
    if (fail.kind === "unreachable") {
      const tries = fail.attempts ? `已连续重试 ${fail.attempts} 次` : "多次重试后";
      return `
        <div class="gate-card is-danger lead-failure-notice" data-lead-failure="unreachable">
          <div class="gate-card-head">
            <div class="gate-card-title">
              <span class="dot dot-red"></span>
              <span>Lead 多次重试后仍无法响应</span>
            </div>
            <button class="btn btn-sm btn-ghost" type="button" data-open-outline="1">查看执行大纲</button>
          </div>
          <div class="gate-card-body">
            <div class="gate-card-summary">${esc(tries)}仍未能联系到 ${esc(leadName)}，后台已停止自动重试。请打开「执行大纲」查看原始输出（常见原因：CLI 未登录、额度不足或模型 ID 不可用），修复后重新发送消息即可。</div>
          </div>
        </div>`;
    }
    const exitStr = fail.exit_code !== undefined && fail.exit_code !== null ? `退出码 ${fail.exit_code}` : "非正常退出";
    return `
      <div class="gate-card is-danger lead-failure-notice" data-lead-failure="turn_failed">
        <div class="gate-card-head">
          <div class="gate-card-title">
            <span class="dot dot-red"></span>
            <span>Lead 回合执行失败（${esc(exitStr)}）</span>
          </div>
          <button class="btn btn-sm btn-ghost" type="button" data-open-outline="1">查看执行大纲</button>
        </div>
        <div class="gate-card-body">
          <div class="gate-card-summary">${esc(leadName)} 未能正常返回回复（常见原因：CLI 未登录、API 额度不足或模型 ID 配置有误）。后台会按退避策略自动重试，可点击右侧「查看执行大纲」排查原始报错。</div>
        </div>
      </div>`;
  }

  function resolveProjectLeadInfo(prj) {
    const ls = STATE.chatLeadStatus && typeof STATE.chatLeadStatus === "object" ? STATE.chatLeadStatus : null;
    const leadName = (ls && ls.agent_name) || (prj && prj.lead) || STATE.config["lead.default_agent"] || "Lead";
    const ag = getAgentByName(leadName);
    const delivery = (ls && ls.delivery) || (ag && ag.delivery) || (leadName === "mesh-lead" ? "inbox" : "launch");
    const adapter = (ls && ls.adapter) || (ag && ag.adapter) || (delivery === "inbox" ? "none" : "");
    const disabled = Boolean((ls && ls.disabled) || (ag && ag.disabled));
    const vendorLeadsEnabled = (ls && typeof ls.vendor_leads_enabled === "boolean")
      ? ls.vendor_leads_enabled
      : Boolean(STATE.config["lead.vendor_leads"]);
    const liveLeadRun = STATE.activeLeadRun
      || (ls && ls.active_run)
      || (prj ? STATE.runs.find(r => r.project_id === prj.id && r.kind === "lead_turn" && (r.status === "starting" || r.status === "running")) : null)
      || null;
    return {
      leadName,
      delivery,
      adapter,
      disabled,
      vendorLeadsEnabled,
      queuedCount: Number((ls && ls.queued_outbox_count) || 0),
      deliveringCount: Number((ls && ls.delivering_outbox_count) || 0),
      oldestQueuedAt: (ls && ls.oldest_queued_at) || null,
      liveLeadRun
    };
  }

  function getPendingUserMessageInfo(prj, leadInfo) {
    if (!prj || !Array.isArray(STATE.chatMessages) || !STATE.chatMessages.length) return null;
    let lastUserIdx = -1;
    for (let i = STATE.chatMessages.length - 1; i >= 0; i -= 1) {
      const m = STATE.chatMessages[i];
      if (m && m.role === "user" && !(m.meta && m.meta.queued_cancelled)) {
        lastUserIdx = i;
        break;
      }
    }
    if (lastUserIdx < 0) return null;
    const lastUserMsg = STATE.chatMessages[lastUserIdx];
    const lastUserSeq = typeof lastUserMsg.event_seq === "number" ? lastUserMsg.event_seq : 0;

    // If the Lead already replied or posted a plan card after this user message, it is no longer waiting.
    for (let i = lastUserIdx + 1; i < STATE.chatMessages.length; i += 1) {
      const m = STATE.chatMessages[i];
      if (!m) continue;
      if (m.role === "lead" || m.kind === "plan_card") {
        return null;
      }
    }
    // Also check plans proposed strictly after this user message (in case plan_card wasn't paginated yet).
    const hasNewerPlan = STATE.plans.some(
      p => p && p.project_id === prj.id && typeof p.proposed_event_seq === "number" && p.proposed_event_seq > lastUserSeq
    );
    if (hasNewerPlan) return null;

    // Check if a lead.turn_finished already ran after this user message (interrupted or ok=true without reply).
    let latestOkSeq = -1;
    let latestInterruptedSeq = -1;
    for (const ev of STATE.events) {
      if (!ev || ev.project_id !== prj.id) continue;
      const seq = typeof ev.seq === "number" ? ev.seq : 0;
      if (seq > lastUserSeq && ev.type === "lead.turn_finished" && ev.payload) {
        if (ev.payload.interrupted === true) {
          if (seq > latestInterruptedSeq) latestInterruptedSeq = seq;
        } else if (ev.payload.ok === true) {
          if (seq > latestOkSeq) latestOkSeq = seq;
        }
      }
    }
    const queuedSet = STATE.queuedUserMsgIdsByProject[prj.id];
    const hasPendingOutbox = Boolean(
      (leadInfo && (leadInfo.queuedCount > 0 || leadInfo.deliveringCount > 0)) ||
      (queuedSet && queuedSet.has(lastUserMsg.id))
    );
    if (latestInterruptedSeq > lastUserSeq && latestInterruptedSeq >= latestOkSeq && !hasPendingOutbox) {
      return null;
    }
    const finishedWithoutReply = latestOkSeq > lastUserSeq && latestOkSeq > latestInterruptedSeq;

    const sinceIso = lastUserMsg.created_at || (leadInfo && leadInfo.oldestQueuedAt) || new Date().toISOString();
    const parsedMs = Date.parse(sinceIso || "");
    const elapsedS = Number.isNaN(parsedMs) ? 0 : Math.max(0, (Date.now() - parsedMs) / 1000);
    return {
      message: lastUserMsg,
      sinceIso,
      elapsedS,
      finishedWithoutReply
    };
  }

  function renderLeadTurnAndWaitStatusHtml(prj, fallbackLeadName) {
    const info = resolveProjectLeadInfo(prj);
    const leadName = info.leadName || fallbackLeadName || "Lead";
    const unresolvedFail = getUnresolvedLeadFailure(prj.id);

    // 1. Active Lead Turn (vendor/launch lead currently starting or running)
    if (info.liveLeadRun) {
      const run = info.liveLeadRun;
      const lName = run.agent_name || run.agent || leadName;
      const runStatus = run.status === "starting" ? "starting" : "running";
      const statusLabel = runStatus === "starting" ? "回合启动中" : "回合运行中";
      const adapterSuffix = info.adapter && info.adapter !== "none" ? `（${info.adapter}）` : "";
      const startIso = run.started_at || new Date().toISOString();
      const parsedMs = Date.parse(startIso);
      const elapsedS = Number.isNaN(parsedMs) ? 0 : Math.max(0, (Date.now() - parsedMs) / 1000);
      const elapsed = formatDuration(elapsedS);
      const stillWaitingHidden = elapsedS < LEAD_WAIT_NOTICE_TIMEOUT_S ? "hidden" : "";
      const desc = runStatus === "starting"
        ? `消息已交给 ${lName}${adapterSuffix}，正在启动本机 CLI 进程并加载项目上下文…`
        : `${lName}${adapterSuffix} 正在思考与规划，完成后将直接在对话中回复并给出计划。`;
      return `
        <div class="lead-status-card is-running" id="leadTypingIndicator" data-lead-turn-status="${esc(runStatus)}">
          <div class="lead-status-head">
            <div class="lead-status-title-row">
              <div class="typing-dots" aria-hidden="true">
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
              </div>
              <span class="lead-status-title">${esc(lName)} · ${esc(statusLabel)}</span>
              <span class="status-pill blue">${esc(statusLabel)}</span>
            </div>
            <div class="lead-status-head-right">
              <span class="msg-meta-time mono" data-duration-start="${esc(startIso)}">${esc(elapsed)}</span>
              <button class="btn btn-sm btn-ghost" type="button" data-open-outline="1">查看执行大纲</button>
            </div>
          </div>
          <div class="lead-status-body">${esc(desc)}</div>
          <div class="lead-still-waiting-note ${stillWaitingHidden}" data-wait-start="${esc(startIso)}" data-wait-threshold-s="${LEAD_WAIT_NOTICE_TIMEOUT_S}">
            仍在等待 ${esc(lName)} 返回回复（已运行超过 ${LEAD_WAIT_NOTICE_TIMEOUT_S} 秒）：本地 CLI 处理复杂任务可能需要更长时间，可点击「查看执行大纲」查看实时输出，或检查 CLI 登录与网络状态。
          </div>
        </div>`;
    }

    // 2. Unresolved Lead Turn failure or unreachable error
    if (unresolvedFail) {
      return renderLeadFailureNoticeHtml(unresolvedFail, leadName);
    }

    // 3. Unanswered user message waiting for Lead pickup / turn start
    const pending = getPendingUserMessageInfo(prj, info);
    if (!pending) return "";

    const sinceIso = pending.sinceIso;
    const elapsed = formatDuration(pending.elapsedS);
    const stillWaitingHidden = pending.elapsedS < LEAD_WAIT_NOTICE_TIMEOUT_S ? "hidden" : "";

    // 3a. Built-in Inbox Lead (mesh-lead / delivery === "inbox")
    if (info.delivery === "inbox") {
      return `
        <div class="lead-status-card is-inbox" id="leadWaitStatusCard" data-lead-delivery="inbox">
          <div class="lead-status-head">
            <div class="lead-status-title-row">
              <span class="dot dot-amber"></span>
              <span class="lead-status-title">消息已排入 ${esc(leadName)} 收件箱，等待外部代理领取</span>
              <span class="status-pill amber">收件箱模式</span>
            </div>
            <span class="msg-meta-time mono" data-duration-start="${esc(sinceIso)}">${esc(elapsed)}</span>
          </div>
          <div class="lead-status-body">
            <div>你的消息已入队给 <span class="mono">${esc(leadName)}</span>。当前为<strong>收件箱协调模式（Inbox Lead）</strong>，桌面端不会自动启动本机 AI CLI 回复或生成计划，需要外部代理或脚本领取并回复。</div>
            <div class="lead-status-hint">外部代理接入命令：<span class="mono">mesh inbox wait --as ${esc(leadName)}</span>；或在本机安装任一 AI CLI（claude / codex / agy / droid / kiro / vibe）后点击下方按钮重新扫描并切换为自动 Lead。</div>
          </div>
          <div class="lead-still-waiting-note ${stillWaitingHidden}" data-wait-start="${esc(sinceIso)}" data-wait-threshold-s="${LEAD_WAIT_NOTICE_TIMEOUT_S}">
            仍在等待回复（已等待超过 ${LEAD_WAIT_NOTICE_TIMEOUT_S} 秒）：尚未检测到外部代理领取 ${esc(leadName)} 的收件箱消息。请确认外部代理正在运行，或安装本机 AI CLI 后点击「重新扫描并连接本机 CLI」切换为自动 Lead。
          </div>
          <div class="lead-status-actions">
            <button class="btn btn-sm btn-primary" type="button" data-open-wizard-step2="1">重新扫描并连接本机 CLI</button>
            <button class="btn btn-sm" type="button" data-nav-cli="1">打开 AI 工具</button>
          </div>
        </div>`;
    }

    // 3b. Launch Lead is disabled
    if (info.disabled) {
      return `
        <div class="lead-status-card is-warning" id="leadWaitStatusCard" data-lead-delivery="launch">
          <div class="lead-status-head">
            <div class="lead-status-title-row">
              <span class="dot dot-amber"></span>
              <span class="lead-status-title">消息已入队，但 ${esc(leadName)} 当前已禁用</span>
              <span class="status-pill amber">Lead 已禁用</span>
            </div>
            <span class="msg-meta-time mono" data-duration-start="${esc(sinceIso)}">${esc(elapsed)}</span>
          </div>
          <div class="lead-status-body">请在「AI 工具」中重新启用 <span class="mono">${esc(leadName)}</span>，或在下方切换为其他可用的 Lead。</div>
          <div class="lead-status-actions">
            <button class="btn btn-sm btn-primary" type="button" data-nav-cli="1">打开 AI 工具</button>
          </div>
        </div>`;
    }

    // 3c. Vendor Launch Lead configured, but lead.vendor_leads is OFF
    const isVendorAdapter = Boolean(info.adapter && info.adapter !== "none" && info.adapter !== "fake");
    if (isVendorAdapter && !info.vendorLeadsEnabled) {
      return `
        <div class="lead-status-card is-warning" id="leadWaitStatusCard" data-lead-delivery="launch">
          <div class="lead-status-head">
            <div class="lead-status-title-row">
              <span class="dot dot-amber"></span>
              <span class="lead-status-title">消息已入队，但本机 CLI Lead 调度未开启</span>
              <span class="status-pill amber">未开启调度</span>
            </div>
            <span class="msg-meta-time mono" data-duration-start="${esc(sinceIso)}">${esc(elapsed)}</span>
          </div>
          <div class="lead-status-body">当前项目 Lead 为 <span class="mono">${esc(leadName)}</span>（${esc(info.adapter)}），但 <span class="mono">lead.vendor_leads</span> 尚未开启，后台不会自动拉起 CLI 回合。请前往「设置」开启「允许本地 AI CLI 担任 Lead」或重新运行选 Lead 向导。</div>
          <div class="lead-status-actions">
            <button class="btn btn-sm btn-primary" type="button" data-nav-settings="1">打开设置</button>
            <button class="btn btn-sm" type="button" data-open-wizard-step2="1">重新选择 Lead</button>
          </div>
        </div>`;
    }

    // 3d. Lead turn already finished ok=true after this user message without emitting a visible reply
    if (pending.finishedWithoutReply) {
      return `
        <div class="lead-status-card is-warning" id="leadWaitStatusCard" data-lead-delivery="launch">
          <div class="lead-status-head">
            <div class="lead-status-title-row">
              <span class="dot dot-amber"></span>
              <span class="lead-status-title">${esc(leadName)} 上一回合已结束，但未返回可见回复</span>
              <span class="status-pill neutral">无回复</span>
            </div>
            <button class="btn btn-sm btn-ghost" type="button" data-open-outline="1">查看执行大纲</button>
          </div>
          <div class="lead-status-body">请打开「执行大纲」查看本机 CLI 的原始输出，或补充一条消息再次唤醒 Lead。</div>
        </div>`;
    }

    // 3e. Queued for Launch/Vendor Lead — waiting for meshd scheduler to launch lead_turn
    return `
      <div class="lead-status-card is-queued" id="leadWaitStatusCard" data-lead-delivery="launch">
        <div class="lead-status-head">
          <div class="lead-status-title-row">
            <div class="typing-dots" aria-hidden="true">
              <span class="typing-dot"></span>
              <span class="typing-dot"></span>
              <span class="typing-dot"></span>
            </div>
            <span class="lead-status-title">消息已排入队列，正在唤醒 ${esc(leadName)}…</span>
            <span class="status-pill blue">排队中</span>
          </div>
          <div class="lead-status-head-right">
            <span class="msg-meta-time mono" data-duration-start="${esc(sinceIso)}">${esc(elapsed)}</span>
            <button class="btn btn-sm btn-ghost" type="button" data-open-outline="1">查看执行大纲</button>
          </div>
        </div>
        <div class="lead-status-body">后台服务已收到你的消息并写入 Lead 调度队列，正在准备启动 ${esc(leadName)} 回合。</div>
        <div class="lead-still-waiting-note ${stillWaitingHidden}" data-wait-start="${esc(sinceIso)}" data-wait-threshold-s="${LEAD_WAIT_NOTICE_TIMEOUT_S}">
          仍在等待 ${esc(leadName)} 响应（已等待超过 ${LEAD_WAIT_NOTICE_TIMEOUT_S} 秒）：Lead 回合尚未开始或尚未返回回复，请点击「查看执行大纲」或「查看日志」检查本机 CLI 状态与后台调度日志。
        </div>
        <div class="lead-status-actions">
          <button class="btn btn-sm" type="button" data-open-outline="1">查看执行大纲</button>
          <button class="btn btn-sm" type="button" data-open-daemon-log="1">查看日志</button>
        </div>
      </div>`;
  }

  function snapshotApprovalReasonInputs(threadEl, activeEl = null) {
    const values = new Map();
    let activeId = null;
    let selectionStart = null;
    let selectionEnd = null;
    if (!threadEl || typeof threadEl.querySelectorAll !== "function") {
      return { values, activeId, selectionStart, selectionEnd };
    }
    const curActive = activeEl || (typeof document !== "undefined" ? document.activeElement : null);
    threadEl.querySelectorAll("input[data-decide-reason]").forEach(el => {
      const aid = el.getAttribute("data-decide-reason");
      if (!aid) return;
      if (typeof el.value === "string" && el.value.length > 0) {
        values.set(aid, el.value);
      }
      if (curActive && el === curActive) {
        activeId = aid;
        selectionStart = typeof el.selectionStart === "number" ? el.selectionStart : null;
        selectionEnd = typeof el.selectionEnd === "number" ? el.selectionEnd : null;
      }
    });
    return { values, activeId, selectionStart, selectionEnd };
  }

  function restoreApprovalReasonInputs(threadEl, snapshot) {
    if (!threadEl || !snapshot || typeof threadEl.querySelectorAll !== "function") return;
    const values = snapshot.values;
    const activeId = snapshot.activeId;
    if ((!values || !values.size) && !activeId) return;
    threadEl.querySelectorAll("input[data-decide-reason]").forEach(el => {
      const aid = el.getAttribute("data-decide-reason");
      if (!aid) return;
      if (values && values.has(aid)) {
        el.value = values.get(aid);
      }
      if (activeId && aid === activeId && !el.disabled) {
        try {
          if (typeof el.focus === "function") el.focus({ preventScroll: true });
          if (
            typeof el.setSelectionRange === "function" &&
            typeof snapshot.selectionStart === "number" &&
            typeof snapshot.selectionEnd === "number"
          ) {
            el.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd);
          }
        } catch (_) {
          // Ignore focus/selection errors on detached or non-selectable inputs
        }
      }
    });
  }

  function isSecondarySurfaceApprovalCandidate(apr) {
    if (!apr || !apr.id) return false;
    if (apr.status === "pending") return true;
    if (apr.status === "approved" && apr.decided_via === "relayed") return true;
    if (apr.status === "rejected" && apr.decided_via === "relayed") return true;
    if (
      apr.status === "approved" &&
      apr.decided_via === "direct" &&
      Array.isArray(apr.available_actions) &&
      apr.available_actions.length > 0
    ) {
      return true;
    }
    return false;
  }

  function renderChatThread() {
    const thread = $("chatThread");
    if (!thread) return;

    const sc = $("chatScroll");
    const wasNearBottom = sc ? (sc.scrollHeight - sc.scrollTop - sc.clientHeight) <= 80 : true;
    const openDetailKeys = new Set();
    const closedDetailKeys = new Set();
    thread.querySelectorAll("details[data-details-key]").forEach(el => {
      const k = el.getAttribute("data-details-key");
      if (!k) return;
      if (el.open) openDetailKeys.add(k);
      else closedDetailKeys.add(k);
    });
    const reasonInputSnapshot = snapshotApprovalReasonInputs(thread);

    const commitThreadHtml = (html, { stickIfNearBottom = false } = {}) => {
      if (html === lastChatThreadHtml) return;
      const prevScrollTop = sc ? sc.scrollTop : 0;
      lastChatThreadHtml = html;
      thread.setAttribute("data-approval-surface", "thread");
      thread.innerHTML = html;
      if (openDetailKeys.size || closedDetailKeys.size) {
        thread.querySelectorAll("details[data-details-key]").forEach(el => {
          const k = el.getAttribute("data-details-key");
          if (openDetailKeys.has(k)) {
            el.open = true;
          } else if (closedDetailKeys.has(k)) {
            el.open = false;
          }
        });
      }
      restoreApprovalReasonInputs(thread, reasonInputSnapshot);
      let didScroll = false;
      if (stickIfNearBottom && wasNearBottom) {
        scrollChatToBottom();
        if (sc && sc.scrollTop !== prevScrollTop) didScroll = true;
      }
      const actionsByApproval = new Map();
      const actionOrder = [];
      thread.querySelectorAll("[data-decide-approval]").forEach(btn => {
        const aid = btn.getAttribute("data-decide-approval");
        if (!aid) return;
        const act = btn.getAttribute("data-decide-action") || "";
        if (!actionsByApproval.has(aid)) {
          actionsByApproval.set(aid, []);
          actionOrder.push(aid);
        }
        if (act) actionsByApproval.get(aid).push(act);
      });
      const threadTokens = actionOrder.map(aid => `${aid}:${(actionsByApproval.get(aid) || []).join(",")}`);
      const stamp = recordApprovalSurfaceLayout("thread", threadTokens, { scrolled: didScroll });
      if (stamp.changedAt) {
        thread.setAttribute("data-surface-changed-at", String(stamp.changedAt));
      }
    };

    if (!STATE.connected || !STATE.auth.auth_verified) {
      commitThreadHtml(`<div class="empty-state">${STATE.connected ? "请在上方输入口令解锁，开始与你的 Lead 对话" : "后台服务未连接，请先启动或等待自动重连"}</div>`);
      return;
    }
    if (STATE.loadingWorkspace || !STATE.hasLoadedWorkspace) {
      commitThreadHtml(`<div class="empty-state">正在加载项目对话...</div>`);
      return;
    }

    const prj = getActiveProject();
    if (!prj || STATE.ui.draftNewChat) {
      const defLead = STATE.ui.selectedLeadDraft || STATE.config["lead.default_agent"] || "";
      const defLeadAgent = defLead ? getAgentByName(defLead) : null;
      const isInboxDefault = Boolean(defLeadAgent ? defLeadAgent.delivery === "inbox" : defLead === "mesh-lead");
      const workerCount = STATE.agents.filter(a => !a.disabled && a.name !== defLead).length;
      const starters = [
        "帮我梳理这个代码仓库的核心架构，并给出两步重构计划",
        "为登录接口补充单元测试和边界用例，先给执行计划再派发",
        "排查最近失败的测试并修复，先列出排查与修改方案"
      ];
      commitThreadHtml(`
        <div class="chat-welcome">
          <span class="avatar avatar-wizard" aria-hidden="true">AM</span>
          <div class="chat-welcome-title">今天想让 AI 团队完成什么？</div>
          <div class="chat-welcome-desc">
            ${isInboxDefault
              ? "当前使用内置收件箱模式（Inbox Lead）：首条消息会进入收件箱，由外部代理领取并回复。如需桌面端自动调用本机 AI CLI 规划执行，请安装 CLI 后点击下方重新扫描。"
              : "直接在下方说出你的想法，首条消息将自动创建项目对话。Lead 会先给出清晰计划卡片，经你点击「允许 / 始终允许 / 拒绝」后再派发给 Worker 执行。"}
          </div>
          <div class="chat-welcome-badges">
            <span class="status-pill ${isInboxDefault ? "amber" : "blue"}">当前 Lead：${esc(defLead || "尚未设置")}${isInboxDefault ? "（收件箱模式）" : ""}</span>
            <span class="status-pill neutral">可用 Worker：${esc(workerCount)} 个</span>
          </div>
          <div class="welcome-starters">
            ${starters.map(sp => `<button class="welcome-starter-chip" type="button" data-starter-prompt="${esc(sp)}">${esc(sp)}</button>`).join("")}
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:4px;">
            <button class="btn btn-sm" type="button" data-open-repo-drawer="1">绑定本地 Git 代码仓库</button>
            ${!defLead
              ? `<button class="btn btn-sm btn-primary" id="btnWelcomePickLead" type="button">选择我的 Lead</button>`
              : (isInboxDefault
                ? `<button class="btn btn-sm btn-primary" type="button" data-open-wizard-step2="1">重新扫描并连接本机 CLI</button>`
                : "")}
          </div>
        </div>`);
      return;
    }

    const blocks = [];
    const leadName = prj.lead || STATE.config["lead.default_agent"] || "Lead";
    const leadColor = avatarPaletteColor(leadName);
    const renderedPlanIds = new Set();
    const renderedApprovalIds = new Set();
    const queuedSet = STATE.queuedUserMsgIdsByProject[prj.id] || null;
    const activeLeadStartMs = STATE.activeLeadRun && STATE.activeLeadRun.started_at
      ? Date.parse(STATE.activeLeadRun.started_at)
      : NaN;

    if (STATE.chatHasMore) {
      blocks.push(`
        <div class="chat-load-more-wrap">
          <button class="btn btn-sm" id="btnLoadOlderChat" type="button" ${STATE.chatLoadingOlder ? "disabled" : ""}>${STATE.chatLoadingOlder ? "正在加载更早消息..." : "加载更早的对话记录"}</button>
        </div>`);
    }

    // Render structured chat_messages in chronological order
    STATE.chatMessages.forEach(msg => {
      if (msg.role === "user") {
        const msgMs = Date.parse(msg.created_at || "");
        const msgMeta = (msg.meta && typeof msg.meta === "object") ? msg.meta : {};
        const isCancelledQueued = Boolean(msgMeta.queued_cancelled);
        const isEditedQueued = Boolean(msgMeta.edited_at);
        const isServerQueued = Boolean(!isCancelledQueued && queuedSet && queuedSet.has(msg.id));
        const isQueued = Boolean(
          !isCancelledQueued && (
            isServerQueued ||
            (STATE.activeLeadRun && !Number.isNaN(activeLeadStartMs) && !Number.isNaN(msgMs) && msgMs > activeLeadStartMs)
          )
        );
        const canEditQueued = Boolean(
          isServerQueued &&
          canMutate() &&
          !isCancelledQueued &&
          !isRelayedChatMessage(msg) &&
          !msg.approval_id &&
          !msg.plan_id &&
          !msg.task_id &&
          !msg.consumed_by_approval_id
        );
        blocks.push(renderUserChatMessageHtml(msg, {
          isQueued,
          isCancelledQueued,
          isEditedQueued,
          canEditQueued
        }));
        return;
      }

      if (msg.kind === "plan_card" && msg.plan_id) {
        const plan = STATE.chatPlansById[msg.plan_id] || STATE.plans.find(p => p.id === msg.plan_id);
        if (plan) {
          renderedPlanIds.add(plan.id);
          if (plan.approval_id) renderedApprovalIds.add(plan.approval_id);
          blocks.push(renderPlanCardHtml(plan));
          return;
        }
      }

      if (msg.kind === "approval_card" && msg.approval_id) {
        const meta = (msg.meta && typeof msg.meta === "object") ? msg.meta : {};
        const hydrated = resolveHydratedApproval(msg.approval_id) || {};
        const synthetic = {
          id: msg.approval_id,
          project_id: prj.id,
          category: meta.category || "permission_escalation",
          op_type: meta.op || "",
          op_hash: meta.op_hash || "",
          task_id: msg.task_id || null,
          summary: msg.body || ""
        };
        if (!Array.isArray(hydrated.available_actions) && Array.isArray(meta.actions) && meta.actions.length === 0) {
          synthetic.available_actions = [];
        }
        const apr = Object.assign(synthetic, hydrated);
        renderedApprovalIds.add(msg.approval_id);
        blocks.push(renderGateCardHtml(apr, msg.body, meta));
        return;
      }

      if (msg.kind === "escalation_card") {
        if (msg.approval_id) {
          const meta = (msg.meta && typeof msg.meta === "object") ? msg.meta : {};
          const hydrated = resolveHydratedApproval(msg.approval_id) || {};
          const synthetic = {
            id: msg.approval_id,
            project_id: prj.id,
            category: meta.category || "permission_escalation",
            op_type: meta.op || "",
            op_hash: meta.op_hash || "",
            task_id: msg.task_id || null,
            summary: msg.body || ""
          };
          if (!Array.isArray(hydrated.available_actions) && Array.isArray(meta.actions) && meta.actions.length === 0) {
            synthetic.available_actions = [];
          }
          const apr = Object.assign(synthetic, hydrated);
          renderedApprovalIds.add(msg.approval_id);
          blocks.push(renderGateCardHtml(apr, msg.body, meta));
          return;
        }
        const t = msg.task_id ? STATE.tasks.find(x => x.id === msg.task_id) : null;
        blocks.push(`
          <div class="gate-card is-danger">
            <div class="gate-card-head">
              <div class="gate-card-title"><span class="dot dot-red"></span><span>任务需要你介入</span></div>
              ${t ? `<button class="btn btn-sm btn-ghost" type="button" data-select-task="${esc(t.id)}">打开 Worker 标签</button>` : ""}
            </div>
            <div class="gate-card-body">
              <div class="gate-card-summary">${esc(msg.body || (t && t.title) || "任务执行遇到阻塞")}</div>
            </div>
          </div>`);
        return;
      }

      if (msg.role === "system") {
        blocks.push(`
          <div class="chat-system-notice" data-msg-id="${esc(msg.id)}">
            <div class="chat-system-notice-body">
              <span class="dot dot-amber"></span>
              <span>${esc(msg.body || "")}</span>
            </div>
            <span class="msg-meta-time mono">${esc(formatClock(msg.created_at))}</span>
          </div>`);
        return;
      }

      // Lead visible reply (text / dispatch_notice / clarification)
      const isDispatch = msg.kind === "dispatch_notice";
      const isClarify = msg.kind === "clarification";
      const bubbleCls = isDispatch ? "msg-dispatch-notice" : (isClarify ? "msg-clarification" : "");
      const kindTag = isDispatch
        ? `<div class="msg-kind-tag">已开始派发执行</div>`
        : (isClarify ? `<div class="msg-kind-tag">需要你补充信息</div>` : "");
      const leadLogText = msg.run_id ? STATE.leadTurnLogs[msg.run_id] : "";
      const leadStructuredAct = msg.run_id ? STATE.runActivities[msg.run_id] : null;
      const leadActivityEntries = (leadStructuredAct && Array.isArray(leadStructuredAct.entries) && leadStructuredAct.entries.length)
        ? leadStructuredAct.entries
        : (leadLogText ? parseActivityFromLogText(leadLogText) : []);
      const leadActivitySummary = leadActivityEntries.length ? summarizeActivityEntries(leadActivityEntries) : "";
      const activityChipHtml = msg.run_id
        ? `<button class="lead-msg-activity-chip" type="button" data-open-outline="1">${esc(leadActivitySummary ? `${leadActivitySummary} · 展开大纲` : "查看本回合执行大纲")}</button>`
        : "";
      blocks.push(`
        <div class="msg-lead-wrap" data-msg-id="${esc(msg.id)}">
          <div class="msg-lead-head">
            <span class="avatar avatar-lead-msg" style="background-color:${esc(leadColor)};" aria-hidden="true">${esc(avatarInitials(leadName, "LD"))}</span>
            <span class="msg-lead-author">${esc(leadName)}</span>
            <span class="msg-meta-time mono">${esc(formatClock(msg.created_at))}</span>
          </div>
          <div class="msg-lead-bubble ${esc(bubbleCls)}">
            ${kindTag}
            <div class="md-prose">${renderSafeMarkdown(msg.body || "")}</div>
            ${activityChipHtml}
          </div>
        </div>`);
    });

    // Any project plans without a chat_message card in the current window (defensive).
    // When older pages remain unloaded (STATE.chatHasMore), skip older plans that predate
    // STATE.chatOldestSeq so they are not re-appended out of order at the bottom.
    STATE.plans.slice().reverse().forEach(p => {
      if (!p || !p.id || renderedPlanIds.has(p.id)) return;
      if (
        STATE.chatHasMore &&
        typeof STATE.chatOldestSeq === "number" &&
        typeof p.proposed_event_seq === "number" &&
        p.proposed_event_seq < STATE.chatOldestSeq
      ) {
        return;
      }
      renderedPlanIds.add(p.id);
      if (p.approval_id) renderedApprovalIds.add(p.approval_id);
      blocks.push(renderPlanCardHtml(p));
    });

    // Any pending, relayed-approved (needing direct upgrade), or hydrated direct-approved
    // upgrade-candidate non-plan_dispatch approvals shown inline
    const openGates = STATE.approvals.filter(a => {
      const apr = resolveHydratedApproval(a.id) || a;
      if (!isSecondarySurfaceApprovalCandidate(apr)) return false;
      if (apr.category === "plan_dispatch") return false;
      if (renderedApprovalIds.has(apr.id)) return false;
      return apr.project_id === prj.id || (!apr.project_id && apr.category === "config");
    });
    openGates.forEach(raw => {
      const apr = resolveHydratedApproval(raw.id) || raw;
      const linkedTask = apr.task_id ? STATE.tasks.find(t => t && t.id === apr.task_id) : null;
      if (!shouldRenderApprovalActions(apr, {
        projectStatus: prj.status,
        permissionMode: prj.permission_mode || STATE.chatPermissionMode,
        task: linkedTask
      })) {
        return;
      }
      renderedApprovalIds.add(apr.id);
      blocks.push(renderGateCardHtml(apr, apr.summary));
    });

    // Legacy project fallback (projects created before chat_messages existed)
    if (!blocks.length) {
      const legacyTasksHtml = STATE.tasks.length
        ? `<div class="plan-tasks-list" style="padding:10px 16px 14px;">${STATE.tasks.map((rawT, idx) => {
          const t = enrichTask(rawT);
          const dur = getTaskDurationInfo(t);
          return `
            <button class="plan-task-row" type="button" data-select-task="${esc(t.id)}">
              <span class="plan-task-left">
                <span class="dot ${getStatusDotClass(t)}" style="margin-top:6px;"></span>
                <span class="plan-task-info">
                  <span class="plan-task-title">#${idx + 1} ${esc(t.title || t.id)}</span>
                  <span class="plan-task-sub">${esc(getTaskHumanStatus(t))} · ${esc(t.assignee || "未指派")}</span>
                </span>
              </span>
              <span class="plan-task-right"><span class="mono">${esc(dur.text)}</span></span>
            </button>`;
        }).join("")}</div>`
        : "";
      blocks.push(`
        <div class="plan-card">
          <div class="plan-card-head">
            <div class="plan-card-title-wrap">
              <div class="plan-card-title">${esc(prj.name || "项目")}</div>
            </div>
            <span class="status-pill neutral">${esc(prj.status)}</span>
          </div>
          <div class="plan-card-summary" style="padding-bottom:12px;">${esc(prj.goal || "直接输入消息与 Lead 对话开始协作")}</div>
          ${legacyTasksHtml}
        </div>`);
    }

    // Live Lead turn state, immediate queued/inbox status, still-waiting notice, or unresolved failure
    const leadStatusHtml = renderLeadTurnAndWaitStatusHtml(prj, leadName);
    if (leadStatusHtml) {
      blocks.push(leadStatusHtml);
    }

    const curTail = STATE.chatMessages.length ? STATE.chatMessages[STATE.chatMessages.length - 1].id : null;
    const hasNewTail = curTail !== lastChatTailId || STATE.chatMessages.length > lastChatMsgCount;
    lastChatMsgCount = STATE.chatMessages.length;
    lastChatTailId = curTail;

    commitThreadHtml(blocks.join(""), {
      stickIfNearBottom: hasNewTail || Boolean(leadStatusHtml)
    });
  }

  function renderRepoInspectionUi() {
    const recentEl = $("recentFoldersBar");
    const statusEl = $("repoInspectStatus");
    const branchListEl = $("repoBranchDatalist");
    const branchChipsEl = $("repoBranchChips");
    const testChipsEl = $("repoTestCmdChips");
    const allow = canMutate() && !STATE.ui.sendingChat;

    if (recentEl) {
      const recents = Array.isArray(STATE.ui.recentFolders) ? STATE.ui.recentFolders : [];
      if (recents.length) {
        recentEl.innerHTML = `<span class="recent-folders-label">最近目录：</span>` + recents.map(p => {
          const base = p.split(/[\\/]+/).filter(Boolean).pop() || p;
          return `<button class="recent-folder-chip" type="button" data-recent-folder="${esc(p)}" title="${esc(p)}" ${allow ? "" : "disabled"}>${esc(base)}</button>`;
        }).join("");
        recentEl.classList.remove("hidden");
      } else {
        recentEl.innerHTML = "";
        recentEl.classList.add("hidden");
      }
    }

    const insp = STATE.ui.repoInspection;
    if (statusEl) {
      if (STATE.ui.repoInspecting) {
        statusEl.className = "repo-inspect-card";
        statusEl.innerHTML = `<span>正在只读检测仓库与测试配置...</span>`;
        statusEl.classList.remove("hidden");
      } else if (!insp) {
        statusEl.innerHTML = "";
        statusEl.classList.add("hidden");
      } else if (insp.path_conflict) {
        statusEl.className = "repo-inspect-card err";
        statusEl.innerHTML = `<span>${esc(insp.path_conflict)}</span>`;
        statusEl.classList.remove("hidden");
      } else if (!insp.git_available) {
        statusEl.className = "repo-inspect-card warn";
        statusEl.innerHTML = `<span>${esc(insp.git_error || "未检测到 Git，请安装 Git 或使用含内置 MinGit 的发行包")}</span>`;
        statusEl.classList.remove("hidden");
      } else if (!insp.is_git_repo) {
        statusEl.className = "repo-inspect-card warn";
        statusEl.innerHTML = `<span>${esc(insp.git_error || "所选目录不是 Git 仓库（留空路径可创建通用对话项目）")}</span>`;
        statusEl.classList.remove("hidden");
      } else if (!insp.is_toplevel && insp.toplevel) {
        statusEl.className = "repo-inspect-card warn";
        statusEl.innerHTML = `<span>当前所选为 Git 子目录，仓库根目录为 <code class="mono">${esc(insp.toplevel)}</code></span><button class="btn btn-sm btn-primary" id="btnUseRepoToplevel" type="button" data-use-toplevel="${esc(insp.toplevel)}" ${allow ? "" : "disabled"}>切换为仓库根目录</button>`;
        statusEl.classList.remove("hidden");
      } else if (insp.unborn_head && (!insp.branches || !insp.branches.length)) {
        statusEl.className = "repo-inspect-card warn";
        statusEl.innerHTML = `<span>该 Git 仓库尚无任何本地提交（unborn HEAD），请先完成至少一次初始 commit。</span>`;
        statusEl.classList.remove("hidden");
      } else {
        const bCount = Array.isArray(insp.branches) ? insp.branches.length : 0;
        const recB = insp.recommended_base_branch || "--";
        statusEl.className = "repo-inspect-card ok";
        statusEl.innerHTML = `<span>已识别 Git 仓库 <strong class="mono">${esc(insp.suggested_title || "")}</strong> · 检测到 ${bCount} 个本地分支，已自动选中 <code class="mono">${esc(recB)}</code></span>`;
        statusEl.classList.remove("hidden");
      }
    }

    const branches = (insp && Array.isArray(insp.branches)) ? insp.branches : [];
    const curBranch = ($("newPrjBaseBranch") && $("newPrjBaseBranch").value.trim()) || "";
    if (branchListEl) {
      branchListEl.innerHTML = branches.map(b => `<option value="${esc(b)}"></option>`).join("");
    }
    if (branchChipsEl) {
      if (branches.length) {
        branchChipsEl.innerHTML = branches.slice(0, 6).map(b =>
          `<button class="repo-quick-chip ${curBranch === b ? "active" : ""}" type="button" data-pick-branch="${esc(b)}" ${allow ? "" : "disabled"}>${esc(b)}</button>`
        ).join("");
        branchChipsEl.classList.remove("hidden");
      } else {
        branchChipsEl.innerHTML = "";
        branchChipsEl.classList.add("hidden");
      }
    }

    if (testChipsEl) {
      const repoVal = ($("newPrjRepo") && $("newPrjRepo").value.trim()) || "";
      const curTestVal = ($("newPrjTestCmd") && $("newPrjTestCmd").value.trim()) || "";
      const cands = (insp && Array.isArray(insp.test_candidates)) ? insp.test_candidates : [];
      if (repoVal) {
        const chips = [
          `<button class="repo-quick-chip ${curTestVal === "none" ? "active" : ""}" type="button" data-pick-test-cmd="none" title="合并阶段不运行自动测试" ${allow ? "" : "disabled"}>暂不测试 (none)</button>`
        ];
        cands.forEach(cand => {
          const jsonStr = JSON.stringify(cand.argv);
          chips.push(
            `<button class="repo-quick-chip ${curTestVal === jsonStr ? "active" : ""}" type="button" data-pick-test-cmd="${esc(jsonStr)}" title="${esc(cand.reason || jsonStr)}" ${allow ? "" : "disabled"}>${esc(cand.label)}</button>`
          );
        });
        testChipsEl.innerHTML = chips.join("");
        testChipsEl.classList.remove("hidden");
      } else {
        testChipsEl.innerHTML = "";
        testChipsEl.classList.add("hidden");
      }
    }
  }

  function applyRepoInspection(insp, { fillPath = false, autoFillDefaults = true, alignToplevel = false } = {}) {
    STATE.ui.repoInspection = insp || null;
    STATE.ui.repoInspecting = false;
    if (!insp) {
      renderComposer();
      return;
    }
    const repoInp = $("newPrjRepo");
    const branchInp = $("newPrjBaseBranch");
    const testInp = $("newPrjTestCmd");
    if (fillPath && repoInp) {
      repoInp.value = (alignToplevel && insp.toplevel) ? insp.toplevel : (insp.path || repoInp.value);
    }
    if (autoFillDefaults && insp.is_git_repo) {
      if (branchInp) {
        branchInp.value = insp.recommended_base_branch || "";
      }
      if (testInp && !testInp.value.trim()) {
        testInp.value = insp.default_test_command || "none";
      }
    }
    renderComposer();
  }

  async function handleInspectRepoPath(rawPath, { autoFillDefaults = true } = {}) {
    const cleaned = String(rawPath || "").trim();
    if (!cleaned) {
      STATE.ui.repoInspection = null;
      STATE.ui.repoInspecting = false;
      renderComposer();
      return null;
    }
    if (!hasApi() || typeof window.pywebview.api.inspect_repo_folder !== "function") {
      return null;
    }
    const seq = ++repoInspectSeq;
    STATE.ui.repoInspecting = true;
    renderRepoInspectionUi();
    try {
      const res = await window.pywebview.api.inspect_repo_folder(cleaned);
      if (seq !== repoInspectSeq) return null;
      if (res && res.ok && res.data) {
        applyRepoInspection(res.data, { fillPath: false, autoFillDefaults });
        return res.data;
      }
      STATE.ui.repoInspecting = false;
      STATE.ui.repoInspection = {
        path: cleaned,
        is_git_repo: false,
        git_available: true,
        git_error: formatErrorMessage(res && res.error)
      };
      renderComposer();
      return null;
    } catch (err) {
      if (seq !== repoInspectSeq) return null;
      STATE.ui.repoInspecting = false;
      renderComposer();
      return null;
    }
  }

  async function handlePickRepoFolder({ repoInputId = "newPrjRepo", branchInputId = "newPrjBaseBranch", testInputId = "newPrjTestCmd", titleInputId = null } = {}) {
    if (!hasApi() || typeof window.pywebview.api.pick_folder !== "function") {
      toast("当前环境不支持原生文件夹选择器，请手动粘贴绝对路径", "warn");
      return;
    }
    const curVal = ($(repoInputId) && $(repoInputId).value.trim()) || "";
    try {
      const res = await window.pywebview.api.pick_folder(curVal || null);
      if (!res || !res.ok) {
        toast(formatErrorMessage(res && res.error), "err");
        return;
      }
      if (res.data && res.data.cancelled) return;
      const chosen = res.data && res.data.path;
      const insp = res.data && res.data.inspection;
      if (!chosen) return;
      if (repoInputId === "newPrjRepo") {
        if ($("newPrjRepo")) $("newPrjRepo").value = chosen;
        if (insp) {
          applyRepoInspection(insp, { fillPath: false, autoFillDefaults: true });
        } else {
          await handleInspectRepoPath(chosen, { autoFillDefaults: true });
        }
      } else {
        const finalPath = (insp && insp.is_git_repo && insp.toplevel) ? insp.toplevel : chosen;
        if ($(repoInputId)) $(repoInputId).value = finalPath;
        if (insp && insp.is_git_repo) {
          if ($(branchInputId) && insp.recommended_base_branch) {
            $(branchInputId).value = insp.recommended_base_branch;
          }
          if ($(testInputId) && !$(testInputId).value.trim()) {
            $(testInputId).value = insp.default_test_command || "none";
          }
          if (titleInputId && $(titleInputId) && !$(titleInputId).value.trim() && insp.suggested_title) {
            $(titleInputId).value = insp.suggested_title;
          }
          if (insp.path_conflict) toast(insp.path_conflict, "err");
        } else if (insp && insp.git_error) {
          toast(insp.git_error, "warn");
        }
      }
    } catch (err) {
      toast(`选择文件夹失败: ${String(err)}`, "err");
    }
  }

  function getPendingApprovalsForActiveProject(prj) {
    if (!prj) return [];
    const seen = new Set();
    const out = [];
    const prjStatus = prj.status || STATE.chatProjectStatus || "active";
    const prjMode = prj.permission_mode || STATE.chatPermissionMode || "plan";

    // 1. Pending decidable proposed plan_dispatch cards on this project
    STATE.plans.forEach(plan => {
      if (!plan || plan.project_id !== prj.id || plan.status !== "proposed" || !plan.approval_id) return;
      if (seen.has(plan.approval_id)) return;
      const apr = resolvePlanApproval(plan);
      if (!apr || (apr.status && apr.status !== "pending")) return;
      if (!shouldRenderApprovalActions(apr, { plan, projectStatus: prjStatus, permissionMode: prjMode })) return;
      seen.add(plan.approval_id);
      out.push({ apr, plan });
    });

    // 2. Pending decidable step_dispatch / permission_escalation / config / retry_over_limit / other approvals
    STATE.approvals.forEach(raw => {
      if (!raw || raw.status !== "pending" || seen.has(raw.id)) return;
      const belongs = raw.project_id === prj.id || (!raw.project_id && raw.category === "config");
      if (!belongs) return;
      const apr = resolveHydratedApproval(raw.id) || raw;
      if (apr.status && apr.status !== "pending") return;
      const effProjectStatus = apr.project_id ? prjStatus : null;
      if (apr.category === "plan_dispatch") {
        const matchedPlan = STATE.plans.find(p => p && p.approval_id === apr.id) || null;
        if (!matchedPlan || matchedPlan.status !== "proposed") return;
        const planApr = resolvePlanApproval(matchedPlan) || apr;
        if (planApr.status && planApr.status !== "pending") return;
        if (!shouldRenderApprovalActions(planApr, { plan: matchedPlan, projectStatus: effProjectStatus, permissionMode: prjMode })) return;
        seen.add(planApr.id);
        out.push({ apr: planApr, plan: matchedPlan });
        return;
      }
      const linkedTask = apr.task_id ? STATE.tasks.find(t => t && t.id === apr.task_id) : null;
      if (!shouldRenderApprovalActions(apr, { projectStatus: effProjectStatus, permissionMode: prjMode, task: linkedTask })) return;
      seen.add(apr.id);
      out.push({ apr, plan: null });
    });

    return out;
  }

  // A3: Sticky Pending-Approval Strip above Composer (Allow / Always Allow / Reject + Jump / Worker Tab)
  function renderComposerApprovalStrip() {
    const strip = $("composerApprovalStrip");
    if (!strip) return;
    const prj = getActiveProject();
    if (!prj || STATE.ui.draftNewChat || !STATE.connected || !STATE.auth.auth_verified) {
      strip.classList.add("hidden");
      strip.innerHTML = "";
      recordApprovalSurfaceLayout("strip", []);
      return;
    }

    const pendingItems = getPendingApprovalsForActiveProject(prj);
    const pendingIdSet = new Set(pendingItems.map(it => it.apr.id));
    const resolvedPlaceholders = [];
    recentlyResolvedApprovals.forEach(entry => {
      if (!entry || pendingIdSet.has(entry.id)) return;
      const belongs = entry.projectId === prj.id || (!entry.projectId && entry.apr && entry.apr.category === "config");
      if (belongs) {
        resolvedPlaceholders.push({ apr: entry.apr, plan: entry.plan, resolvedPlaceholder: entry });
      }
    });
    const displayItems = orderStripItemsStably(prj.id, pendingItems.concat(resolvedPlaceholders));
    if (!displayItems.length) {
      strip.classList.add("hidden");
      strip.innerHTML = "";
      recordApprovalSurfaceLayout("strip", []);
      return;
    }

    const prjStatus = prj.status || STATE.chatProjectStatus || "active";
    const prjMode = prj.permission_mode || STATE.chatPermissionMode || "plan";
    const hasDanger = pendingItems.some(({ apr, plan }) => {
      const taskObj = apr.task_id ? STATE.tasks.find(t => t.id === apr.task_id) : null;
      const cardOpts = { plan, projectStatus: apr.project_id ? prjStatus : null, permissionMode: prjMode, task: taskObj };
      if (isDismissOnlyApproval(apr, cardOpts)) return false;
      return apr.category === "permission_escalation" || apr.category === "config";
    });
    strip.classList.remove("hidden");
    strip.classList.toggle("has-danger", hasDanger);
    strip.setAttribute("data-approval-surface", "strip");

    const layoutTokens = [];
    const itemsHtml = displayItems.map(({ apr, plan, resolvedPlaceholder }) => {
      if (resolvedPlaceholder) {
        layoutTokens.push(`${apr.id}:resolved`);
        const summary = plan
          ? `${plan.title || "执行计划"} (${Array.isArray(plan.steps) ? plan.steps.length : 0} 步)`
          : (apr.summary || formatApprovalOpName(apr));
        const resolvedLabel = resolvedPlaceholder.label || "已处理";
        return `
          <div class="composer-approval-item is-resolved-placeholder" data-strip-approval-id="${esc(apr.id)}" data-resolved-placeholder="1">
            <div class="composer-approval-item-top">
              <div class="composer-approval-item-info">
                <span class="dot dot-neutral"></span>
                <span class="status-pill neutral">${esc(resolvedLabel)}</span>
                <span class="composer-approval-item-summary" title="${esc(summary)}">${esc(summary)}</span>
              </div>
              <div style="display:flex;align-items:center;gap:4px;flex-shrink:0;">
                <button class="btn btn-sm btn-ghost" type="button" disabled>${esc(resolvedLabel)}</button>
              </div>
            </div>
            <div class="composer-approval-item-actions">
              <div class="approval-actions-row is-compact">
                <button class="btn btn-sm" type="button" disabled>${esc(resolvedLabel)}</button>
              </div>
            </div>
          </div>`;
      }
      const catLabel = GATE_CATEGORY_LABELS[apr.category] || apr.category || "待审批";
      const taskObj = apr.task_id ? STATE.tasks.find(t => t.id === apr.task_id) : null;
      const cardOpts = {
        plan,
        projectStatus: apr.project_id ? prjStatus : null,
        permissionMode: prjMode,
        task: taskObj
      };
      const actions = getApprovalCardActions(apr, cardOpts);
      layoutTokens.push(`${apr.id}:pending:${actions.join(",")}`);
      const isDismissOnly = isDismissOnlyApproval(apr, cardOpts);
      const isExternalOp = isExternalApprovalOp(apr);
      const unapprovableReason = isDismissOnly ? getApprovalUnapprovableReason(apr, cardOpts) : null;
      const sourceBadge = plan
        ? `计划 v${plan.version || 1}`
        : (taskObj ? (taskObj.assignee || taskObj.title || taskObj.id) : (isExternalOp ? "外部请求" : catLabel));
      const summary = plan
        ? `${plan.title || "执行计划"} (${Array.isArray(plan.steps) ? plan.steps.length : 0} 步)`
        : (apr.summary || formatApprovalOpName(apr));
      const isDanger = !isDismissOnly && (apr.category === "permission_escalation" || apr.category === "config");
      const dotCls = isDismissOnly ? "dot-neutral" : (isDanger ? "dot-red" : "dot-amber");
      const pillHtml = isDismissOnly
        ? `<span class="status-pill neutral" data-unapprovable-reason="${esc(unapprovableReason || "dismiss_only")}">${esc(formatApprovalReasonBadgeLabel(unapprovableReason))}</span>`
        : `<span class="status-pill ${isDanger ? "red" : "amber"}">${esc(catLabel)}</span>`;
      const externalTagHtml = isExternalOp
        ? `<span class="status-pill neutral" data-external-op-badge="1">外部请求</span>`
        : "";
      return `
        <div class="composer-approval-item" data-strip-approval-id="${esc(apr.id)}"${isDismissOnly ? ' data-dismiss-only="1"' : ""}${isExternalOp ? ' data-external-op="1"' : ""}>
          <div class="composer-approval-item-top">
            <div class="composer-approval-item-info">
              <span class="dot ${dotCls}"></span>
              ${pillHtml}
              ${externalTagHtml}
              <span class="mono" style="font-size:11.5px;color:var(--text-secondary);flex-shrink:0;">${esc(sourceBadge)}</span>
              <span class="composer-approval-item-summary" title="${esc(summary)}">${esc(summary)}</span>
            </div>
            <div style="display:flex;align-items:center;gap:4px;flex-shrink:0;">
              <button class="btn btn-sm btn-ghost" type="button" data-jump-approval-card="${esc(apr.id)}">定位卡片</button>
              ${apr.task_id ? `<button class="btn btn-sm btn-ghost" type="button" data-select-task="${esc(apr.task_id)}">打开 Worker</button>` : ""}
            </div>
          </div>
          <div class="composer-approval-item-actions">
            ${renderApprovalButtonsFooterHtml(apr, Object.assign({ compact: true }, cardOpts))}
          </div>
        </div>`;
    }).join("");

    const stamp = recordApprovalSurfaceLayout("strip", layoutTokens);
    if (stamp.changedAt) {
      strip.setAttribute("data-surface-changed-at", String(stamp.changedAt));
    }

    strip.innerHTML = `
      <div class="composer-approval-strip-title">
        <span>待审批卡片 (${pendingItems.length})</span>
        <span style="font-size:11.5px;font-weight:400;color:var(--text-secondary);">请使用按钮审批（聊天文本不会批准操作）</span>
      </div>
      ${itemsHtml}`;
  }

  function renderComposer() {
    const prj = getActiveProject();
    const isNewDraft = !prj || STATE.ui.draftNewChat;
    const leadName = STATE.ui.selectedLeadDraft || (prj && prj.lead) || STATE.config["lead.default_agent"] || "";
    const leadAgent = leadName ? getAgentByName(leadName) : null;
    const leadDetail = leadName ? STATE.agentDetails[leadName] : null;
    const leadModel = (leadDetail && leadDetail.default_model) || (leadAgent && leadAgent.default_model) || "";
    const leadPillText = leadName
      ? (leadModel ? `${leadName} · ${leadModel}` : `Lead: ${leadName}`)
      : "Lead: 未设置";
    if ($("leadPillLabel")) $("leadPillLabel").textContent = leadPillText;

    renderComposerApprovalStrip();

    const repoDrawer = $("composerRepoDrawer");
    const repoBtn = $("btnToggleRepoDrawer");
    const repoChip = $("composerRepoChip");
    if (repoDrawer) {
      repoDrawer.classList.toggle("hidden", !isNewDraft || !STATE.ui.repoDrawerOpen);
    }
    if (repoBtn) {
      repoBtn.classList.toggle("active", isNewDraft && STATE.ui.repoDrawerOpen);
      repoBtn.title = isNewDraft ? "绑定本地 Git 代码仓库（可选）" : (prj && prj.repo ? `已绑定仓库：${prj.repo}` : "当前为通用对话项目");
    }
    if (repoChip) {
      const draftRepo = ($("newPrjRepo") && $("newPrjRepo").value.trim()) || "";
      const activeRepo = isNewDraft ? draftRepo : (prj && prj.repo ? prj.repo : "");
      if (activeRepo) {
        const shortName = activeRepo.split(/[\\/]+/).filter(Boolean).pop() || activeRepo;
        repoChip.textContent = shortName;
        repoChip.title = activeRepo;
        repoChip.classList.remove("hidden");
      } else {
        repoChip.classList.add("hidden");
      }
    }

    // A4: Queued-turn feedback while Lead turn is active
    const isLeadBusy = Boolean(!isNewDraft && STATE.activeLeadRun);
    const queueBadge = $("composerQueueBadge");
    if (queueBadge) {
      queueBadge.classList.toggle("hidden", !isLeadBusy);
    }

    const pendingCards = !isNewDraft ? getPendingApprovalsForActiveProject(prj) : [];
    const ta = $("composerInput");
    if (ta) {
      if (isNewDraft) {
        ta.placeholder = "告诉 Lead 你想完成什么... (Enter 发送，首条消息将自动创建项目)";
      } else if (isLeadBusy) {
        ta.placeholder = "Lead 正在执行当前回合；Enter 排队发送，Ctrl+Enter 立即打断并改向...";
      } else if (pendingCards.length > 0) {
        ta.placeholder = "请点击上方审批卡片的「允许 / 始终允许 / 拒绝」按钮处理授权，或继续发消息与 Lead 沟通...";
      } else {
        ta.placeholder = `给 ${prj.name || "Lead"} 发消息... (Enter 发送，Shift+Enter 换行)`;
      }
    }

    const allow = canMutate() && !STATE.ui.sendingChat;
    const reason = disabledReason();
    ["composerInput", "newPrjRepo", "newPrjBaseBranch", "newPrjTestCmd", "btnPickRepoFolder", "btnInspectRepoFolder"].forEach(id => {
      const el = $(id);
      if (el) {
        el.disabled = !allow;
        if (!allow && reason) el.title = reason;
        else if (id !== "btnPickRepoFolder" && id !== "btnInspectRepoFolder") el.removeAttribute("title");
      }
    });
    const sendBtn = $("btnSendComposer");
    if (sendBtn) {
      sendBtn.disabled = !allow;
      sendBtn.title = isLeadBusy ? "排队发送（等待当前 Lead 回合结束后处理；Ctrl+Enter 可立即改向）" : "发送消息 (Enter)";
    }
    const stopBtn = $("btnStopLeadTurn");
    if (stopBtn) {
      stopBtn.classList.toggle("hidden", !isLeadBusy);
      stopBtn.disabled = !canMutate() || STATE.ui.interruptingLead;
      stopBtn.textContent = STATE.ui.interruptingLead ? "正在停止..." : "停止";
    }
    const steerBtn = $("btnSteerLeadTurn");
    if (steerBtn) {
      const hasDraft = Boolean(ta && ta.value.trim());
      steerBtn.classList.toggle("hidden", !isLeadBusy || !hasDraft);
      steerBtn.disabled = !allow || STATE.ui.interruptingLead;
    }
    if ($("btnLeadPill")) $("btnLeadPill").disabled = !canMutate();
    if (isNewDraft && STATE.ui.repoDrawerOpen) {
      renderRepoInspectionUi();
    }

    const noteEl = $("composerInlineNote");
    if (noteEl) {
      if (STATE.ui.composerInlineNote) {
        noteEl.className = `composer-inline-note ${STATE.ui.composerInlineNote.level || "warn"}`;
        noteEl.textContent = STATE.ui.composerInlineNote.text;
        noteEl.classList.remove("hidden");
      } else {
        noteEl.classList.add("hidden");
      }
    }
    syncComposerTextareaHeight();
  }

  // =========================================================================
  // Render: Right Inspector (Task Details / Outline / Running / Advanced)
  // =========================================================================
  function captureInspectorUiState(bodyEl) {
    const openDetails = new Set();
    const noteValues = {};
    let activeNoteKey = null;
    let selStart = null;
    let selEnd = null;
    if (!bodyEl) return { openDetails, noteValues, activeNoteKey, selStart, selEnd };
    bodyEl.querySelectorAll("details[open][data-details-key]").forEach(el => {
      openDetails.add(el.getAttribute("data-details-key"));
    });
    const activeEl = document.activeElement;
    bodyEl.querySelectorAll("input[data-apr-note]").forEach(inp => {
      const key = inp.getAttribute("data-apr-note");
      if (!key) return;
      noteValues[key] = inp.value;
      if (inp === activeEl) {
        activeNoteKey = key;
        selStart = inp.selectionStart;
        selEnd = inp.selectionEnd;
      }
    });
    return { openDetails, noteValues, activeNoteKey, selStart, selEnd };
  }

  function restoreInspectorUiState(bodyEl, snap) {
    if (!bodyEl || !snap) return;
    if (snap.openDetails && snap.openDetails.size) {
      bodyEl.querySelectorAll("details[data-details-key]").forEach(el => {
        if (snap.openDetails.has(el.getAttribute("data-details-key"))) {
          el.open = true;
        }
      });
    }
    if (snap.noteValues) {
      bodyEl.querySelectorAll("input[data-apr-note]").forEach(inp => {
        const key = inp.getAttribute("data-apr-note");
        if (key && Object.prototype.hasOwnProperty.call(snap.noteValues, key)) {
          inp.value = snap.noteValues[key];
          if (snap.activeNoteKey === key) {
            inp.focus();
            if (typeof snap.selStart === "number" && typeof snap.selEnd === "number") {
              try { inp.setSelectionRange(snap.selStart, snap.selEnd); } catch (_) {}
            }
          }
        }
      });
    }
  }

  // =========================================================================
  // A2: Right Panel Closable Multi-Worker Tabs
  // Per-project worker tab state for the right inspector panel.
  // =========================================================================
  function getProjectWorkerTabs(pid) {
    if (!pid) return [];
    const tabs = STATE.ui.rightTabsByProject[pid];
    return Array.isArray(tabs) ? tabs : [];
  }

  function openWorkerTab(taskId, projectId = null) {
    const pid = projectId || STATE.activeProjectId;
    if (!pid || !taskId) return;
    const cur = getProjectWorkerTabs(pid).slice();
    if (!cur.includes(taskId)) {
      cur.push(taskId);
      STATE.ui.rightTabsByProject[pid] = cur;
    }
    STATE.activeTaskId = taskId;
    STATE.ui.logRunId = null;
    STATE.ui.inspectorCollapsed = false;
    STATE.ui.inspectorTab = "task";
  }

  function closeWorkerTab(taskId, projectId = null) {
    const pid = projectId || STATE.activeProjectId;
    if (!pid || !taskId) return;
    const cur = getProjectWorkerTabs(pid).filter(id => id !== taskId);
    STATE.ui.rightTabsByProject[pid] = cur;
    if (STATE.activeTaskId === taskId) {
      STATE.activeTaskId = cur.length ? cur[cur.length - 1] : (STATE.tasks[0] ? STATE.tasks[0].id : null);
      if (!cur.length) {
        STATE.ui.inspectorTab = "outline";
      }
    }
  }

  function renderInspectorWorkerTabs() {
    const strip = $("inspectorWorkerTabStrip");
    if (!strip) return;
    const prj = getActiveProject();
    if (!prj || STATE.ui.draftNewChat) {
      strip.classList.add("hidden");
      strip.innerHTML = "";
      return;
    }
    const tabIds = getProjectWorkerTabs(prj.id).filter(tid => STATE.tasks.some(t => t.id === tid));
    STATE.ui.rightTabsByProject[prj.id] = tabIds;
    if (!tabIds.length) {
      strip.classList.add("hidden");
      strip.innerHTML = "";
      return;
    }
    strip.classList.remove("hidden");
    strip.innerHTML = tabIds.map(tid => {
      const raw = STATE.tasks.find(x => x.id === tid);
      const t = raw ? enrichTask(raw) : { id: tid, title: tid, status: "created" };
      const isAct = STATE.ui.inspectorTab === "task" && STATE.activeTaskId === tid;
      const label = t.assignee ? `${t.assignee}: ${t.title || tid}` : (t.title || tid);
      return `
        <div class="worker-tab-chip ${isAct ? "active" : ""}" data-worker-tab-id="${esc(tid)}">
          <button class="worker-tab-btn" type="button" data-activate-worker-tab="${esc(tid)}" title="${esc(label)}">
            <span class="dot ${getStatusDotClass(t)}"></span>
            <span class="worker-tab-label">${esc(label)}</span>
          </button>
          <button class="worker-tab-close" type="button" data-close-worker-tab="${esc(tid)}" title="关闭标签" aria-label="关闭标签">×</button>
        </div>`;
    }).join("");
  }

  function renderInspector() {
    const topEl = $("inspectorTop");
    const bodyEl = $("inspectorBody");
    if (!topEl || !bodyEl) return;

    renderInspectorWorkerTabs();
    const uiSnap = captureInspectorUiState(bodyEl);

    document.querySelectorAll(".tab-btn[data-tab]").forEach(btn => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === STATE.ui.inspectorTab);
    });

    const tab = STATE.ui.inspectorTab;
    const rawTask = getActiveTask();
    const allow = canMutate();
    const disAttr = allow ? "" : `disabled title="${esc(disabledReason())}"`;

    if (tab !== "task") {
      const headingMap = { diff: "代码差异 (Diff)", outline: "执行大纲", running: "运行中的 Worker", advanced: "高级工具与手动兜底" };
      topEl.innerHTML = `
        <div class="inspector-top-tools">
          <span class="inspector-top-heading">${esc(headingMap[tab] || "检查器")}</span>
          <button class="icon-btn" id="btnInspectorClose" type="button" title="收起右侧检查器">
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 6 6 18M6 6l12 12"></path></svg>
          </button>
        </div>`;
      if (tab === "diff") renderInspectorDiff(bodyEl);
      else if (tab === "outline") renderInspectorOutline(bodyEl);
      else if (tab === "running") renderInspectorRunning(bodyEl);
      else renderInspectorAdvanced(bodyEl);
      restoreInspectorUiState(bodyEl, uiSnap);
      return;
    }

    if (!rawTask) {
      const taskOptions = STATE.tasks.map(t => `<button class="plan-task-row" type="button" data-select-task="${esc(t.id)}"><span class="plan-task-left"><span class="dot ${getStatusDotClass(enrichTask(t))}" style="margin-top:6px;"></span><span class="plan-task-info"><span class="plan-task-title">${esc(t.title || t.id)}</span><span class="plan-task-sub">${esc(getTaskHumanStatus(enrichTask(t)))}</span></span></span></button>`).join("");
      topEl.innerHTML = `
        <div class="inspector-top-tools">
          <span class="inspector-top-heading">任务详情</span>
          <button class="icon-btn" id="btnInspectorClose" type="button" title="收起右侧检查器">
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 6 6 18M6 6l12 12"></path></svg>
          </button>
        </div>`;
      bodyEl.innerHTML = taskOptions
        ? `<div class="inspector-sec-label">选择当前项目的任务</div>${taskOptions}`
        : `<div class="empty-state">点击计划卡下的任意任务即可查看实时日志、变更文件与人工操作。</div>`;
      return;
    }

    const t = enrichTask(rawTask);
    const origin = deriveOriginFromEvent(t.created_event, t.created_by);
    const dur = getTaskDurationInfo(t);
    const liveAttr = dur.isLive && dur.startIso ? `data-duration-start="${esc(dur.startIso)}"` : "";

    const chevSvg = `<svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>`;
    const buttons = [];
    const taskKind = t.kind || "general";
    if (t.status === "submitted") {
      buttons.push(`<button class="inspector-action-row ok" type="button" data-task-op="accept" ${disAttr}><span class="inspector-action-left"><span>验收通过</span></span>${chevSvg}</button>`);
      buttons.push(`<button class="inspector-action-row danger" type="button" data-task-op="reject_task" ${disAttr}><span class="inspector-action-left"><span>打回返工</span></span>${chevSvg}</button>`);
    } else if (t.status === "merge_failed") {
      if (taskKind === "code") {
        buttons.push(`<button class="inspector-action-row accent" type="button" data-task-op="merge" ${disAttr}><span class="inspector-action-left"><span>重试合并</span></span>${chevSvg}</button>`);
      }
      buttons.push(`<button class="inspector-action-row danger" type="button" data-task-op="reject_task" ${disAttr}><span class="inspector-action-left"><span>打回返工</span></span>${chevSvg}</button>`);
    } else if (t.status === "accepted" && taskKind === "code") {
      buttons.push(`<button class="inspector-action-row accent" type="button" data-task-op="merge" ${disAttr}><span class="inspector-action-left"><span>合并分支</span></span>${chevSvg}</button>`);
    }
    if (t.status === "failed") {
      buttons.push(`<button class="inspector-action-row" type="button" data-task-op="escalate" ${disAttr}><span class="inspector-action-left"><span>升级异常</span></span>${chevSvg}</button>`);
    }
    if (["failed", "escalated"].includes(t.status)) {
      buttons.push(`<button class="inspector-action-row accent" type="button" data-task-op="retry" ${disAttr}><span class="inspector-action-left"><span>重试任务</span></span>${chevSvg}</button>`);
    }
    if (["created", "assigned", "queued", "rework", "failed", "escalated", "merge_failed"].includes(t.status)) {
      buttons.push(`<button class="inspector-action-row" type="button" data-task-op="assign" ${disAttr}><span class="inspector-action-left"><span>${t.status === "created" ? "指派执行者" : "改派执行者"}</span></span>${chevSvg}</button>`);
    }
    if (taskKind === "code" && ["accepted", "merge_failed", "failed", "cancelled"].includes(t.status) && !t.superseded_by) {
      buttons.push(`<button class="inspector-action-row" type="button" data-task-op="close" ${disAttr}><span class="inspector-action-left"><span>替代关闭</span></span>${chevSvg}</button>`);
    }
    const openTaskRun = STATE.runs.find(r => r.task_id === t.id && (r.status === "starting" || r.status === "running"));
    if (openTaskRun) {
      buttons.push(`<button class="inspector-action-row" type="button" data-task-op="release_run" ${disAttr}><span class="inspector-action-left"><span>释放运行锁</span></span>${chevSvg}</button>`);
    }
    if (!["merging", "merged", "cancelled", "cancelling"].includes(t.status) && !(t.status === "accepted" && taskKind === "general")) {
      buttons.push(`<button class="inspector-action-row danger" type="button" data-task-op="cancel" ${disAttr}><span class="inspector-action-left"><span>取消任务</span></span>${chevSvg}</button>`);
    }

    const humanStatus = getTaskHumanStatus(t);
    const taskPickerOpts = STATE.tasks.length > 1
      ? `<select class="compact-select" id="inspectorTaskPicker" style="min-height:28px;font-size:12px;">${STATE.tasks.map(x => `<option value="${esc(x.id)}" ${x.id === t.id ? "selected" : ""}>${esc(x.title || x.id)} (${esc(getTaskHumanStatus(enrichTask(x)))})</option>`).join("")}</select>`
      : "";

    topEl.innerHTML = `
      <div class="inspector-top-tools">
        <span class="inspector-top-heading">Worker 标签 · 任务详情</span>
        <div style="display:flex;align-items:center;gap:4px;">
          <button class="icon-btn" id="btnRefreshTask" type="button" ${disAttr} title="刷新任务详情">
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 1 1-2.64-6.36"></path><path d="M21 3v6h-6"></path></svg>
          </button>
          <button class="icon-btn" id="btnInspectorClose" type="button" title="收起右侧检查器">
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 6 6 18M6 6l12 12"></path></svg>
          </button>
        </div>
      </div>
      ${taskPickerOpts}
      <div class="worker-subagent-bar">
        <div class="worker-subagent-bar-left">
          <span class="dot ${getStatusDotClass(t)}"></span>
          <strong class="mono">${esc(t.assignee || "未指派")}</strong>
          <span class="mono">${esc(t.model || "默认模型")}</span>
          <span class="mono">${esc(t.profile || "默认档位")}</span>
          <span class="status-pill ${t.status === "merged" ? "green" : (["failed", "escalated", "merge_failed"].includes(t.status) ? "red" : (["running", "merging"].includes(t.status) ? "blue" : "neutral"))}">${esc(humanStatus)}</span>
        </div>
        <span class="mono" ${liveAttr}>${esc(dur.text)}</span>
      </div>
      <div class="inspector-title-block">
        <div class="inspector-title">${esc(t.title || t.id)}</div>
        <div class="inspector-id-row" title="${esc(t.id)} · ${esc(humanStatus)}">
          <span class="inspector-id-code mono">${esc(t.id)}</span>
          <span class="inspector-id-status">· ${esc(humanStatus)}</span>
        </div>
      </div>`;

    renderInspectorTaskBody(bodyEl, t, origin, buttons);
    restoreInspectorUiState(bodyEl, uiSnap);
  }

  function renderInspectorTaskBody(bodyEl, t, origin, actionButtons) {
    const blocks = [];

    if (t.pending_approval && t.pending_approval.category !== "plan_dispatch") {
      const apr = resolveHydratedApproval(t.pending_approval.id) || t.pending_approval;
      const catLabel = GATE_CATEGORY_LABELS[apr.category] || apr.category || "安全审批";
      blocks.push(`
        <div class="inspector-note-box amber">
          <strong>待审批 · ${esc(catLabel)}</strong>
          <div style="margin-top:4px;">${esc(apr.summary || formatApprovalOpName(apr))}</div>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <button class="btn btn-sm btn-primary" type="button" data-jump-approval-card="${esc(apr.id)}">定位左侧审批卡片</button>
            <button class="btn btn-sm btn-ghost" type="button" data-open-advanced-gate="${esc(apr.id)}">查看载荷详情</button>
          </div>
        </div>`);
    }

    if (t.failure) {
      blocks.push(`
        <div class="inspector-note-box red">
          <strong>最近失败原因</strong>
          <pre class="log-box" style="margin-top:6px;max-height:140px;">${esc(typeof t.failure === "string" ? t.failure : JSON.stringify(t.failure, null, 2))}</pre>
        </div>`);
    }

    if (actionButtons.length) {
      blocks.push(`
        <div class="inspector-card-group">
          <div class="inspector-sec-label">任务操作</div>
          <div class="inspector-card">${actionButtons.join("")}</div>
        </div>`);
    }

    const depData = STATE.taskDeps[t.id] || {};
    const depsList = depData.depends_on || t.depends_on || [];
    const depsText = Array.isArray(depsList) && depsList.length
      ? depsList.map(d => typeof d === "string" ? d : `${d.id}`).join(", ")
      : "无";

    blocks.push(`
      <div class="inspector-card-group">
        <div class="inspector-sec-label">执行信息</div>
        <div class="inspector-card">
          <div class="param-row"><span class="param-k">执行 Agent</span><span class="param-v">${esc(t.assignee || "未指派")}</span></div>
          <div class="param-row"><span class="param-k">模型 / 强度</span><span class="param-v mono">${esc(t.model || "默认")} · ${esc(t.effort || "默认")}</span></div>
          <div class="param-row"><span class="param-k">权限档位</span><span class="param-v mono">${esc(t.profile || "默认")}</span></div>
          <div class="param-row"><span class="param-k">类型 / 尝试</span><span class="param-v mono">${esc(t.kind || "general")} · ${esc(t.attempt || 0)}/${esc(t.max_attempts || "--")}</span></div>
          <div class="param-row"><span class="param-k">来源</span><span class="param-v muted">${esc(origin.short)}</span></div>
          <div class="param-row"><span class="param-k">依赖任务</span><span class="param-v mono muted">${esc(depsText)}</span></div>
          ${t.branch ? `<div class="param-row"><span class="param-k">工作分支</span><span class="param-v mono muted">${esc(t.branch)}</span></div>` : ""}
        </div>
      </div>`);

    blocks.push(`
      <div class="inspector-section">
        <div class="inspector-sec-label">任务说明</div>
        <pre class="log-box" style="max-height:180px;">${esc(t.spec || "(无说明)")}</pre>
      </div>`);

    // Git Diff Summary (with direct jump to B1 Diff tab)
    const ws = STATE.taskWorkspaces[t.id];
    const deliv = t.deliverable && typeof t.deliverable === "object" ? t.deliverable : null;
    const git = (deliv && deliv.git) || null;
    if (git || (t.kind || "general") === "code" || (ws && Array.isArray(ws.files) && ws.files.length)) {
      blocks.push(`
        <div class="inspector-section">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
            <span class="inspector-sec-label">代码变更摘要</span>
            ${(t.kind || "general") === "code" ? `<button class="btn btn-sm btn-ghost" type="button" data-open-diff-tab="task">查看实时 Diff →</button>` : ""}
          </div>
          ${git ? `
            <div class="kv-list">
              <div class="kv-row"><span class="kv-k">分支</span><span class="kv-v mono">${esc(git.branch || t.branch || "--")}</span></div>
              <div class="kv-row"><span class="kv-k">提交数</span><span class="kv-v mono">${esc(git.commit_count ?? 0)}</span></div>
            </div>
            ${git.diff_stat ? `<pre class="log-box" style="max-height:160px;">${esc(git.diff_stat)}</pre>` : ""}` : ""}
          ${ws && Array.isArray(ws.files) && ws.files.length ? `<details class="config-raw-details" data-details-key="ws:${esc(t.id)}"><summary>工作区文件 (${ws.files.length}${ws.files_truncated ? "+" : ""})</summary><div class="file-list" style="margin-top:6px;">${ws.files.map(f => `<div class="file-row">${esc(typeof f === "string" ? f : (f.path || ""))}</div>`).join("")}</div></details>` : ""}
        </div>`);
    }

    // Live Task Logs
    const taskRuns = STATE.runs.filter(r => r.task_id === t.id).sort((a, b) => (a.attempt || 0) - (b.attempt || 0));
    const stream = STATE.ui.logStreamFilter;
    const runId = STATE.ui.logRunId;
    const runOptions = [`<option value="">最新一次运行</option>`].concat(
      taskRuns.map(r => `<option value="${esc(r.id)}" ${runId === r.id ? "selected" : ""}>第 ${esc(r.attempt || 1)} 次 · ${esc(r.status)}</option>`)
    );
    blocks.push(`
      <div class="inspector-section">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap;">
          <span class="inspector-sec-label">运行日志</span>
          <div style="display:flex;align-items:center;gap:6px;">
            <div class="segmented">
              <button type="button" class="${stream === "all" ? "active" : ""}" data-log-stream="all">全部</button>
              <button type="button" class="${stream === "stdout" ? "active" : ""}" data-log-stream="stdout">标准输出</button>
              <button type="button" class="${stream === "stderr" ? "active" : ""}" data-log-stream="stderr">错误输出</button>
            </div>
            <button class="btn btn-sm" id="btnReloadLogs" type="button">刷新</button>
          </div>
        </div>
        ${taskRuns.length > 1 ? `<select class="compact-select" id="selectLogRun" style="min-height:28px;font-size:12px;">${runOptions.join("")}</select>` : ""}
        <div id="inspectorLogContent">${buildInspectorLogContentHtml(t)}</div>
      </div>`);

    bodyEl.innerHTML = blocks.join("");
  }

  function buildInspectorLogContentHtml(t) {
    const stream = STATE.ui.logStreamFilter;
    const runId = STATE.ui.logRunId;
    const cacheKey = `${t.id}:${runId || "latest"}:${stream}`;
    const actCacheKey = `${t.id}:${runId || "latest"}`;
    const logData = STATE.taskLogs[cacheKey];
    const structuredAct = STATE.taskActivities[actCacheKey] || (logData && logData.run_id ? STATE.runActivities[logData.run_id] : null);

    if (!logData) return `<div class="empty-state" style="padding:12px;">正在读取日志...</div>`;
    if (logData.error) return `<div class="inspector-note-box red">${esc(formatErrorMessage(logData.error))}</div>`;
    if (logData.streams) {
      const chunks = [];
      for (const sname of ["stdout", "stderr"]) {
        const sObj = logData.streams[sname];
        if (!sObj) continue;
        const lines = Array.isArray(sObj.lines) ? sObj.lines.join("\n") : "";
        if (sname === "stdout") {
          chunks.push(renderGroupedActivityHtml(lines, `tasklog:${t.id}:${runId || "latest"}`, {
            emptyText: sObj.available ? "(暂无输出)" : "(无日志文件)",
            showRawToggle: true,
            structuredActivity: structuredAct
          }));
        } else if (lines.trim()) {
          chunks.push(`<div class="inspector-sec-label" style="margin-top:6px;">stderr</div><pre class="log-box" style="max-height:180px;">${esc(lines)}</pre>`);
        } else if (stream === "stderr") {
          chunks.push(`<pre class="log-box" style="max-height:180px;">${esc(sObj.available ? "(暂无错误输出)" : "(无日志文件)")}</pre>`);
        }
      }
      return chunks.length ? chunks.join("") : `<div class="empty-state" style="padding:12px;">暂无日志内容</div>`;
    }
    return `<div class="empty-state" style="padding:12px;">暂无日志内容</div>`;
  }

  // B1: Pure vanilla JS opaque unified diff renderer and file tree viewer
  function renderUnifiedPatchHtml(patchText) {
    const raw = typeof patchText === "string" ? patchText : "";
    if (!raw.trim()) {
      return `<div class="empty-state" style="padding:10px;">(无差异内容)</div>`;
    }
    const lines = raw.split(/\r?\n/);
    const htmlLines = lines.map(line => {
      let cls = "diff-line";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff --git ") || line.startsWith("index ") || line.startsWith("rename ") || line.startsWith("new file ") || line.startsWith("deleted file ")) {
        cls = "diff-line meta";
      } else if (line.startsWith("@@")) {
        cls = "diff-line hunk";
      } else if (line.startsWith("+")) {
        cls = "diff-line add";
      } else if (line.startsWith("-")) {
        cls = "diff-line del";
      }
      return `<span class="${cls}">${esc(line)}</span>`;
    });
    return `<pre class="diff-code">${htmlLines.join("")}</pre>`;
  }

  function renderInspectorDiff(bodyEl) {
    const prj = getActiveProject();
    if (!prj) {
      bodyEl.innerHTML = `<div class="empty-state">打开一个绑定本地 Git 仓库的项目后可查看只读代码差异。</div>`;
      return;
    }
    if (!prj.repo) {
      bodyEl.innerHTML = `<div class="empty-state">当前项目未绑定本地 Git 代码仓库，暂无代码差异。</div>`;
      return;
    }

    const activeTask = getActiveTask();
    const hasCodeTask = Boolean(activeTask && (activeTask.kind || "general") === "code");
    const scope = (STATE.ui.diffScope === "task" && hasCodeTask) ? "task" : "project";
    const diffData = scope === "task"
      ? (activeTask ? STATE.taskDiffs[activeTask.id] : null)
      : STATE.projectDiffs[prj.id];

    const blocks = [];
    blocks.push(`
      <div class="diff-toolbar">
        <div class="segmented">
          <button type="button" class="${scope === "task" ? "active" : ""}" data-diff-scope="task" ${hasCodeTask ? "" : "disabled title=\"请先在任务列表中选择一个代码任务\""}>当前任务</button>
          <button type="button" class="${scope === "project" ? "active" : ""}" data-diff-scope="project">项目集成</button>
        </div>
        <div style="display:flex;align-items:center;gap:4px;">
          <button class="btn btn-sm btn-ghost" type="button" data-diff-expand-all="1">展开全部</button>
          <button class="btn btn-sm btn-ghost" type="button" data-diff-collapse-all="1">折叠全部</button>
          <button class="btn btn-sm" type="button" data-diff-refresh="1">刷新</button>
        </div>
      </div>`);

    if (!diffData) {
      blocks.push(`<div class="empty-state" style="padding:16px;">正在读取只读 Git 差异...</div>`);
      bodyEl.innerHTML = blocks.join("");
      return;
    }
    if (diffData.error) {
      blocks.push(`<div class="inspector-note-box red">${esc(formatErrorMessage(diffData.error))}</div>`);
      bodyEl.innerHTML = blocks.join("");
      return;
    }

    const stat = diffData.stat || { files_changed: 0, additions: 0, deletions: 0 };
    const files = Array.isArray(diffData.files) ? diffData.files : [];
    const wtPath = diffData.worktree_path || "";
    const reasonMap = {
      not_code_task: "当前任务不是代码任务（general），无 Git 工作树差异。",
      no_repo: "项目未绑定 Git 仓库。",
      repo_missing: "项目绑定的本地 Git 仓库目录不存在。",
      workspace_missing: "任务工作树尚未创建或已被清理，且未找到可比较的分支。",
      untrusted_worktree: "任务工作树路径校验未通过，已拒绝读取。",
      integration_branch_missing: "项目集成分支尚未创建。",
      base_branch_missing: "项目基准分支不存在。",
      diff_timeout: "读取只读 Git 差异超时，请稍后重试。",
      git_diff_failed: "执行只读 Git 差异命令失败，请稍后重试。"
    };

    blocks.push(`
      <div class="diff-header-card">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap;">
          <span class="mono" style="font-weight:600;color:var(--text-primary);">${esc(diffData.branch || "--")}</span>
          <span class="mono" style="font-size:11px;color:var(--text-muted);">vs ${esc((diffData.base_ref || "--").slice(0, 28))}</span>
        </div>
        ${wtPath ? `
          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
            <span class="mono" style="font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(wtPath)}">${esc(wtPath)}</span>
            <button class="btn btn-sm btn-ghost" type="button" data-copy-code="${esc(wtPath)}">复制路径</button>
          </div>` : ""}
        <div class="diff-stat-row">
          <span class="diff-stat-pill">${esc(stat.files_changed || 0)} 个文件</span>
          <span class="diff-stat-pill add">+${esc(stat.additions || 0)}</span>
          <span class="diff-stat-pill del">-${esc(stat.deletions || 0)}</span>
          ${typeof diffData.commits_ahead === "number" ? `<span class="diff-stat-pill">${esc(diffData.commits_ahead)} 个领先提交</span>` : ""}
          ${diffData.dirty ? `<span class="status-pill amber">含未提交变更</span>` : ""}
          ${diffData.available && diffData.truncated ? `<span class="status-pill amber">已按大小上限截断</span>` : ""}
        </div>
        ${scope === "project" && prj.repo && prj.status !== "cancelled" && (stat.files_changed > 0 || (diffData.commits_ahead || 0) > 0) ? `
          <div style="display:flex;justify-content:flex-end;margin-top:2px;">
            <button class="btn btn-sm btn-primary" type="button" data-prj-op="deliver" ${canMutate() ? "" : "disabled"}>交付到本地分支 (快进)</button>
          </div>` : ""}
      </div>`);

    if (!diffData.available) {
      blocks.push(`<div class="empty-state" style="padding:14px;">${esc(reasonMap[diffData.reason] || diffData.reason || "暂无可展示的代码差异")}</div>`);
      bodyEl.innerHTML = blocks.join("");
      return;
    }

    if (!files.length) {
      const emptyMsg = diffData.truncated
        ? "读取代码差异未完成或已达到上限，请点击「刷新」重试。"
        : "当前工作树与基准相比无代码差异。";
      blocks.push(`<div class="empty-state" style="padding:14px;">${esc(emptyMsg)}</div>`);
      bodyEl.innerHTML = blocks.join("");
      return;
    }

    // File tree index
    blocks.push(`
      <div class="diff-file-tree">
        ${files.map((f, idx) => {
          const st = f.status === "?" ? "U" : (f.status || "M").charAt(0).toUpperCase();
          const label = f.old_path ? `${f.old_path} → ${f.path}` : f.path;
          return `
            <button class="diff-file-row" type="button" data-diff-jump-file="diffFile_${idx}">
              <span class="diff-file-row-left">
                <span class="diff-status-badge st-${esc(st)}">${esc(f.status || "M")}</span>
                <span class="diff-file-path" title="${esc(label)}">${esc(label)}</span>
              </span>
              <span class="mono" style="font-size:11px;flex-shrink:0;">
                <span style="color:var(--sem-green);">+${esc(f.additions || 0)}</span>
                <span style="color:var(--sem-red);margin-left:4px;">-${esc(f.deletions || 0)}</span>
              </span>
            </button>`;
        }).join("")}
      </div>`);

    // Collapsible per-file unified diff blocks
    files.forEach((f, idx) => {
      const st = f.status === "?" ? "U" : (f.status || "M").charAt(0).toUpperCase();
      const label = f.old_path ? `${f.old_path} → ${f.path}` : f.path;
      const openByDefault = idx < 3 ? "open" : "";
      let bodyHtml;
      if (f.binary) {
        bodyHtml = `<div class="empty-state" style="padding:10px;">二进制文件变更（未展示文本补丁）</div>`;
      } else if (f.error && !(f.patch || "").trim()) {
        const errNote = f.error === "timeout"
          ? "已达到差异读取时间上限，未加载此文件补丁。"
          : "读取此文件补丁失败。";
        bodyHtml = `<div class="empty-state" style="padding:10px;">${esc(errNote)}</div>`;
      } else if (f.truncated && !(f.patch || "").trim()) {
        bodyHtml = `<div class="empty-state" style="padding:10px;">已达到差异大小上限，未加载此文件补丁。</div>`;
      } else {
        bodyHtml = renderUnifiedPatchHtml(f.patch || "");
      }
      blocks.push(`
        <details class="diff-file-details" id="diffFile_${idx}" data-details-key="diff:${esc(scope)}:${esc(f.path)}" ${openByDefault}>
          <summary>
            <span class="diff-file-row-left">
              <span class="diff-status-badge st-${esc(st)}">${esc(f.status || "M")}</span>
              <span class="diff-file-path" title="${esc(label)}">${esc(label)}</span>
              ${f.truncated ? `<span class="status-pill amber" style="font-size:10px;padding:0 5px;">已截断</span>` : ""}
            </span>
            <span class="mono" style="font-size:11px;flex-shrink:0;">+${esc(f.additions || 0)} -${esc(f.deletions || 0)}</span>
          </summary>
          ${bodyHtml}
        </details>`);
    });

    bodyEl.innerHTML = blocks.join("");
  }

  function renderInspectorLogsBodyOnly(taskId) {
    if (STATE.ui.inspectorCollapsed || STATE.ui.inspectorTab !== "task" || STATE.activeTaskId !== taskId) return;
    const contentEl = $("inspectorLogContent");
    const t = getActiveTask();
    if (!t || !contentEl) return;
    const openKeys = new Set();
    contentEl.querySelectorAll("details[open][data-details-key]").forEach(el => {
      openKeys.add(el.getAttribute("data-details-key"));
    });
    const boxScrollTops = Array.from(contentEl.querySelectorAll(".log-box")).map(el => el.scrollTop);
    contentEl.innerHTML = buildInspectorLogContentHtml(enrichTask(t));
    if (openKeys.size) {
      contentEl.querySelectorAll("details[data-details-key]").forEach(el => {
        if (openKeys.has(el.getAttribute("data-details-key"))) el.open = true;
      });
    }
    Array.from(contentEl.querySelectorAll(".log-box")).forEach((el, idx) => {
      if (idx < boxScrollTops.length) el.scrollTop = boxScrollTops[idx];
    });
  }

  function renderInspectorOutline(bodyEl) {
    const prj = getActiveProject();
    if (!prj) {
      bodyEl.innerHTML = `<div class="empty-state">打开一个项目对话后可查看 Lead 推理与执行大纲。</div>`;
      return;
    }
    const blocks = [];
    const leadRuns = STATE.runs.filter(r => r.project_id === prj.id && r.kind === "lead_turn").slice(-10).reverse();
    if (leadRuns.length) {
      blocks.push(`<div class="inspector-sec-label">Lead 思考与回合记录 (${leadRuns.length})</div>`);
      leadRuns.forEach(run => {
        const stdout = STATE.leadTurnLogs[run.id] || "";
        const dur = run.ended_at && run.started_at
          ? formatDuration((Date.parse(run.ended_at) - Date.parse(run.started_at)) / 1000)
          : (run.status === "running" ? "运行中" : "--");
        const actHtml = renderGroupedActivityHtml(stdout, `outline:${run.id}`, {
          emptyText: run.status === "running" ? "正在执行中..." : "(无原始输出)",
          showRawToggle: true,
          structuredActivity: STATE.runActivities[run.id] || null
        });
        blocks.push(`
          <details class="outline-item" data-details-key="outline:${esc(run.id)}">
            <summary>
              <span><span class="dot ${run.status === "running" ? "dot-blue" : run.exit_code === 0 ? "dot-green" : "dot-amber"}" style="margin-right:6px;"></span>${esc(run.agent || prj.lead || "Lead")} · ${esc(formatClock(run.started_at))}</span>
              <span class="mono" style="font-size:11px;color:var(--text-muted);">${esc(dur)}</span>
            </summary>
            <div class="outline-item-body">
              ${actHtml}
            </div>
          </details>`);
      });
    }

    const recentEvents = STATE.events.slice(-40).reverse();
    blocks.push(`<div class="inspector-sec-label" style="margin-top:4px;">最近项目事件 (${recentEvents.length})</div>`);
    if (recentEvents.length) {
      blocks.push(`<div class="inspector-card">${recentEvents.map(ev => `
        <div class="timeline-row">
          <div style="display:flex;justify-content:space-between;gap:8px;">
            <span class="mono" style="font-size:11.5px;color:var(--text-primary);">${esc(ev.type)}</span>
            <span class="mono" style="font-size:11px;color:var(--text-muted);">${esc(formatClock(ev.ts))}</span>
          </div>
          ${ev.reason ? `<div style="font-size:11.5px;color:var(--text-secondary);margin-top:2px;">${esc(ev.reason)}</div>` : ""}
        </div>`).join("")}</div>`);
    } else {
      blocks.push(`<div class="empty-state">暂无事件记录</div>`);
    }
    bodyEl.innerHTML = blocks.join("");
  }

  function renderInspectorRunning(bodyEl) {
    const prj = getActiveProject();
    const activeRuns = STATE.runs.filter(r =>
      (r.status === "starting" || r.status === "running") && (!prj || r.project_id === prj.id)
    );
    const activeTasks = STATE.tasks.filter(t => ["running", "merging", "queued", "assigned"].includes(t.status));

    const blocks = [];
    blocks.push(`<div class="inspector-sec-label">正在运行的 Worker (${activeRuns.length})</div>`);
    if (activeRuns.length) {
      blocks.push(`<div class="inspector-card">${activeRuns.map(r => {
        const elapsed = r.started_at ? formatDuration((Date.now() - Date.parse(r.started_at)) / 1000) : "00:00";
        const liveAttr = r.started_at ? `data-duration-start="${esc(r.started_at)}"` : "";
        const clickAttr = r.task_id ? `data-select-task="${esc(r.task_id)}"` : "";
        return `
          <button class="param-row" type="button" ${clickAttr}>
            <span style="display:flex;align-items:center;gap:7px;min-width:0;">
              <span class="dot dot-blue"></span>
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(r.agent || "--")} · ${esc(r.kind === "lead_turn" ? "Lead 回合" : (r.task_id || r.kind))}</span>
            </span>
            <span class="param-v mono" ${liveAttr}>${esc(elapsed)}</span>
          </button>`;
      }).join("")}</div>`);
    } else {
      blocks.push(`<div class="empty-state">当前没有正在运行的进程</div>`);
    }

    if (activeTasks.length) {
      blocks.push(`<div class="inspector-sec-label" style="margin-top:4px;">进行中的任务 (${activeTasks.length})</div>`);
      blocks.push(activeTasks.map(rawT => {
        const t = enrichTask(rawT);
        const dur = getTaskDurationInfo(t);
        const liveAttr = dur.isLive && dur.startIso ? `data-duration-start="${esc(dur.startIso)}"` : "";
        return `
          <button class="plan-task-row" type="button" data-select-task="${esc(t.id)}">
            <span class="plan-task-left">
              <span class="dot ${getStatusDotClass(t)}" style="margin-top:6px;"></span>
              <span class="plan-task-info">
                <span class="plan-task-title">${esc(t.title || t.id)}</span>
                <span class="plan-task-sub">${esc(getTaskHumanStatus(t))} · ${esc(t.assignee || "--")}</span>
              </span>
            </span>
            <span class="plan-task-right mono" ${liveAttr}>${esc(dur.text)}</span>
          </button>`;
      }).join(""));
    }
    bodyEl.innerHTML = blocks.join("");
  }

  function filterInspectorAdvancedApprovals(prj = getActiveProject()) {
    return STATE.approvals.filter(raw => {
      if (!raw || !raw.id) return false;
      const apr = resolveHydratedApproval(raw.id) || raw;
      if (!isSecondarySurfaceApprovalCandidate(apr)) return false;
      if (prj && prj.id) {
        return apr.project_id === prj.id || (!apr.project_id && apr.category === "config");
      }
      return !apr.project_id && apr.category === "config";
    });
  }

  function renderInspectorAdvanced(bodyEl) {
    const allow = canMutate();
    const disAttr = allow ? "" : `disabled title="${esc(disabledReason())}"`;
    const prj = getActiveProject();
    const allPendingAprs = filterInspectorAdvancedApprovals(prj);
    const pendingIdSet = new Set(allPendingAprs.map(a => a.id));
    const resolvedPlaceholders = [];
    recentlyResolvedApprovals.forEach(entry => {
      if (!entry || pendingIdSet.has(entry.id)) return;
      const belongs = prj && prj.id
        ? (entry.projectId === prj.id || (!entry.projectId && entry.apr && entry.apr.category === "config"))
        : (!entry.projectId && entry.apr && entry.apr.category === "config");
      if (belongs) resolvedPlaceholders.push(entry);
    });

    const focusAprId = STATE.ui.focusAdvancedApprovalId || null;
    const combinedItems = allPendingAprs.map(raw => ({ raw, resolvedPlaceholder: null }))
      .concat(resolvedPlaceholders.map(entry => ({ raw: entry.apr, resolvedPlaceholder: entry })));
    const renderItems = orderInspectorItemsStably(prj ? prj.id : null, combinedItems);
    const inspLayoutTokens = [];

    const aprListHtml = renderItems.length
      ? renderItems.map(({ raw, resolvedPlaceholder }) => {
        if (resolvedPlaceholder) {
          inspLayoutTokens.push(`${raw.id}:resolved`);
          return `
            <div class="inspector-note-box is-resolved-placeholder" data-advanced-approval-id="${esc(raw.id)}" data-resolved-placeholder="1">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
                <strong>${esc(GATE_CATEGORY_LABELS[raw.category] || raw.category || "审批")}</strong>
                <span class="status-pill neutral">${esc(resolvedPlaceholder.label || "已处理")}</span>
              </div>
              <div style="margin-top:4px;">${esc(raw.summary || formatApprovalOpName(raw))}</div>
              <div style="margin-top:8px;">
                <button class="btn btn-sm" type="button" disabled>${esc(resolvedPlaceholder.label || "已处理")}</button>
              </div>
            </div>`;
        }
        const matchedPlan = raw.category === "plan_dispatch"
          ? (STATE.plans.find(p => p && p.approval_id === raw.id) || null)
          : null;
        const apr = matchedPlan ? (resolvePlanApproval(matchedPlan) || resolveHydratedApproval(raw.id) || raw) : (resolveHydratedApproval(raw.id) || raw);
        const linkedTask = apr.task_id ? STATE.tasks.find(t => t && t.id === apr.task_id) : null;
        const aprPrj = apr.project_id ? (STATE.projects.find(p => p && p.id === apr.project_id) || (prj && prj.id === apr.project_id ? prj : null)) : null;
        const projectBadgeLabel = apr.project_id
          ? `项目：${(aprPrj && aprPrj.name) || apr.project_id}`
          : "全局配置";
        const cardOpts = {
          plan: matchedPlan,
          projectStatus: aprPrj && aprPrj.status,
          permissionMode: aprPrj && aprPrj.permission_mode,
          task: linkedTask
        };
        const catLabel = GATE_CATEGORY_LABELS[apr.category] || apr.category;
        const isExternalOp = isExternalApprovalOp(apr);
        const isUnhydrated = !Array.isArray(apr.available_actions);
        const actions = getApprovalCardActions(apr, cardOpts);
        const canAct = actions.length > 0;
        inspLayoutTokens.push(`${apr.id}:${canAct ? actions.join(",") : "readonly"}`);
        const isDismissOnly = canAct && isDismissOnlyApproval(apr, cardOpts);
        const isRelayedRejected = apr.status === "rejected" && apr.decided_via === "relayed" && !isApprovalExpiredLocally(apr);
        const unapprovableReason = isDismissOnly ? getApprovalUnapprovableReason(apr, cardOpts) : null;
        const undecidableHint = !canAct ? formatUndecidableReasonHint(apr, cardOpts) : "";
        const dismissHint = isDismissOnly ? formatUnapprovableReasonHint(apr, cardOpts) : "";
        const externalTagHtml = isExternalOp
          ? `<span class="status-pill neutral" data-external-op-badge="1">外部请求</span>`
          : "";
        const extraBadgeHtml = isDismissOnly
          ? `<span class="status-pill neutral" data-unapprovable-reason="${esc(unapprovableReason || "dismiss_only")}">${esc(formatApprovalReasonBadgeLabel(unapprovableReason))}</span>`
          : (isRelayedRejected
            ? `<span class="status-pill amber">转发已拒绝</span>`
            : "");
        const effPolicy = resolveApprovalPolicy(apr, {
          planGateMode: aprPrj && aprPrj.plan_gate_mode,
          permissionMode: aprPrj && aprPrj.permission_mode
        });
        const isFocused = Boolean(focusAprId && focusAprId === apr.id);
        return `
          <div class="inspector-note-box ${canAct && !isDismissOnly ? "amber" : ""}${isFocused ? " approval-card-highlight" : ""}" data-advanced-approval-id="${esc(apr.id)}"${isDismissOnly ? ' data-dismiss-only="1"' : ""}${isExternalOp ? ' data-external-op="1"' : ""}>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <strong>${esc(catLabel)}</strong>
                <span class="status-pill neutral" data-approval-project-label="1">${esc(projectBadgeLabel)}</span>
                ${externalTagHtml}
                ${extraBadgeHtml}
              </div>
              <span class="mono" style="font-size:11px;color:var(--text-muted);">${esc(effPolicy)}</span>
            </div>
            <div style="margin-top:4px;">${esc(apr.summary || formatApprovalOpName(apr))}</div>
            <details data-details-key="apr:${esc(apr.id)}"${isFocused ? " open" : ""} style="margin-top:5px;">
              <summary style="cursor:pointer;font-size:12px;">查看操作载荷 JSON</summary>
              <pre class="log-box" style="margin-top:4px;max-height:130px;">${esc(formatApprovalOpJson(apr))}</pre>
            </details>
            ${dismissHint ? `<div class="gate-resolved-bar" data-dismiss-only="1" data-unapprovable-reason="${esc(unapprovableReason || "dismiss_only")}" style="margin-top:6px;">${esc(dismissHint)}</div>` : ""}
            <div style="margin-top:8px;">
              ${canAct
                ? renderApprovalButtonsFooterHtml(apr, Object.assign({ compact: true }, cardOpts))
                : `<div class="gate-resolved-bar">${esc(isUnhydrated ? "审批状态加载中，请刷新加载后操作" : (undecidableHint || "该审批卡片当前不可操作"))}</div>`}
            </div>
          </div>`;
      }).join("")
      : `<div class="empty-state" style="padding:12px;">当前项目没有待处理的审批卡片</div>`;

    const cancellablePlans = prj
      ? STATE.plans.filter(p => ["proposed", "approved_pending_dispatch", "revising"].includes(p.status))
      : [];
    const cancellablePlansHtml = cancellablePlans.length
      ? `<div class="inspector-card-group">
          <div class="inspector-sec-label">未派发计划管理 (${cancellablePlans.length})</div>
          ${cancellablePlans.map(p => `
            <div class="inspector-note-box">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                <div>
                  <strong>计划 v${esc(p.version || 1)} · ${esc(p.title || "执行计划")}</strong>
                  <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;">状态：${esc((PLAN_STATUS_META[p.status] && PLAN_STATUS_META[p.status].label) || p.status)}</div>
                </div>
                <button class="btn btn-sm btn-danger-text" type="button" data-cancel-plan-id="${esc(p.id)}" ${disAttr}>取消计划</button>
              </div>
            </div>`).join("")}
        </div>`
      : "";

    const allTasksHtml = STATE.tasks.length
      ? STATE.tasks.map(rawT => {
        const t = enrichTask(rawT);
        return `
          <button class="plan-task-row" type="button" data-select-task="${esc(t.id)}">
            <span class="plan-task-left">
              <span class="dot ${getStatusDotClass(t)}" style="margin-top:6px;"></span>
              <span class="plan-task-info">
                <span class="plan-task-title">${esc(t.title || t.id)}</span>
                <span class="plan-task-sub">${esc(getTaskHumanStatus(t))} · ${esc(t.assignee || "未指派")}</span>
              </span>
            </span>
            <span class="plan-task-right mono">${esc(t.id.slice(-6))}</span>
          </button>`;
      }).join("")
      : `<div class="empty-state" style="padding:10px;">当前项目暂无任务</div>`;

    bodyEl.innerHTML = `
      <div class="inspector-card-group">
        <div class="inspector-sec-label">手工操作入口（日常建议直接使用对话）</div>
        <div class="inspector-card">
          <button class="inspector-action-row" id="btnNewTaskModal" type="button" ${disAttr}>
            <span class="inspector-action-left"><span>手工创建任务</span></span>
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>
          </button>
          <button class="inspector-action-row" id="btnManualNewProject" type="button" ${disAttr}>
            <span class="inspector-action-left"><span>完整表单新建项目</span></span>
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>
          </button>
          <button class="inspector-action-row" id="btnAdvancedOpenCli" type="button">
            <span class="inspector-action-left"><span>管理 AI 工具与 Agent</span></span>
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>
          </button>
          <button class="inspector-action-row" id="btnAdvancedAudit" type="button" ${disAttr}>
            <span class="inspector-action-left"><span>校验审计哈希链</span></span>
            <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m9 18 6-6-6-6"></path></svg>
          </button>
        </div>
      </div>

      ${cancellablePlansHtml}

      <div class="inspector-card-group" data-approval-surface="inspector">
        <div class="inspector-sec-label">${prj ? `当前项目待处理审批卡片 (${allPendingAprs.length})` : `待处理审批卡片 (${allPendingAprs.length})`}</div>
        ${aprListHtml}
      </div>

      ${prj ? `
        <div class="inspector-card-group">
          <div class="inspector-sec-label">本项目全部任务 (${STATE.tasks.length})</div>
          ${allTasksHtml}
        </div>` : ""}`;

    const inspStamp = recordApprovalSurfaceLayout("inspector", inspLayoutTokens);
    const inspSurface = bodyEl.querySelector('[data-approval-surface="inspector"]');
    if (inspSurface && inspStamp.changedAt) {
      inspSurface.setAttribute("data-surface-changed-at", String(inspStamp.changedAt));
    }

    if (focusAprId) {
      STATE.ui.focusAdvancedApprovalId = null;
      const escaped = (typeof window !== "undefined" && window.CSS && typeof window.CSS.escape === "function")
        ? window.CSS.escape(focusAprId)
        : focusAprId;
      const targetBox = bodyEl.querySelector(`[data-advanced-approval-id="${escaped}"]`);
      if (targetBox) {
        const det = targetBox.querySelector("details");
        if (det) det.open = true;
        if (typeof targetBox.scrollIntoView === "function") {
          targetBox.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        if (typeof window !== "undefined" && typeof window.setTimeout === "function") {
          window.setTimeout(() => targetBox.classList.remove("approval-card-highlight"), 1600);
        }
      }
    }
  }

  // =========================================================================
  // Render: AI Tools (CLI Adapters & Agents) + Settings
  // =========================================================================
  function formatAdapterFriendlyStatus(a) {
    if (a.status === "not_installed") return { text: "未安装", cls: "muted" };
    if (a.status === "error") return { text: "探测异常", cls: "red" };
    if (a.launch_reason === "launch_held") return { text: `暂停 (${a.launch_milestone || "M5"})`, cls: "amber" };
    if (a.launch_enabled && a.status === "verified") return { text: "已就绪", cls: "green" };
    if (a.launch_enabled && a.status === "partially_verified") return { text: "已就绪（新版本）", cls: "green" };
    if (a.launch_enabled) return { text: "已就绪", cls: "green" };
    return { text: "暂不可用", cls: "amber" };
  }

  function renderCliView() {
    const root = $("cliPageScroll");
    if (!root) return;
    if (!STATE.connected || !STATE.auth.auth_verified) {
      root.innerHTML = `<div class="page-container"><div class="empty-state">${STATE.connected ? "解锁后管理本机 AI 工具与 Agent" : "等待后台服务连接"}</div></div>`;
      return;
    }
    const allow = canMutate();
    const disAttr = allow ? "" : `disabled title="${esc(disabledReason())}"`;

    const activeRunsByAgent = {};
    STATE.runs.forEach(r => {
      if (r.status !== "starting" && r.status !== "running") return;
      if (r.agent) activeRunsByAgent[r.agent] = (activeRunsByAgent[r.agent] || 0) + 1;
    });

    const renderAdapterRow = a => {
      const st = formatAdapterFriendlyStatus(a);
      const ags = STATE.agents.filter(ag => ag.adapter === a.id);
      const busy = ags.reduce((sum, ag) => sum + (activeRunsByAgent[ag.name] || 0), 0);
      const cap = ags.reduce((sum, ag) => sum + (ag.max_concurrency || 1), 0);
      return `
        <tr>
          <td class="mono">${esc(a.id)}</td>
          <td class="mono">${esc(a.version || "--")}</td>
          <td><span class="status-inline ${st.cls}"><span class="dot dot-${st.cls === "muted" ? "neutral" : st.cls}"></span>${esc(st.text)}</span></td>
          <td class="mono">${ags.length ? `${esc(busy)} / ${esc(cap)} (${esc(ags.length)} 个)` : "0 个"}</td>
          <td style="text-align:right;">
            <button class="btn btn-sm" data-adapter-probe="${esc(a.id)}" ${disAttr}>探测</button>
            <button class="btn btn-sm btn-ghost" data-adapter-show="${esc(a.id)}">详情</button>
          </td>
        </tr>`;
    };

    const availableAdapters = STATE.adapters.filter(a => !isUnavailableAdapter(a));
    const unavailableAdapters = STATE.adapters.filter(a => isUnavailableAdapter(a));
    const unavailableAdapterSection = unavailableAdapters.length ? `
      <details class="config-raw-details" style="margin-top:12px;">
        <summary>暂不可用 / 高级 Adapter (${esc(unavailableAdapters.length)} 项 · held / stub)</summary>
        <table class="clean-table" style="margin-top:8px;">
          <thead><tr><th>工具</th><th>版本</th><th>状态</th><th>关联 Agent</th><th style="text-align:right;">操作</th></tr></thead>
          <tbody>${unavailableAdapters.map(renderAdapterRow).join("")}</tbody>
        </table>
      </details>` : "";

    const agentRows = STATE.agents.map(ag => {
      const activeCnt = activeRunsByAgent[ag.name] || 0;
      return `
        <tr>
          <td class="mono">${esc(ag.name)}</td>
          <td class="mono">${esc(ag.adapter)}</td>
          <td class="mono">${esc(ag.delivery)}</td>
          <td class="mono">${esc(ag.default_profile || "--")}</td>
          <td><button class="btn btn-sm" data-agent-toggle-lead="${esc(ag.name)}" ${disAttr}>${ag.can_lead ? "Lead: 开" : "Lead: 关"}</button></td>
          <td class="mono">${esc(ag.default_model || "--")}</td>
          <td class="mono">${esc(activeCnt)}/${esc(ag.max_concurrency)}</td>
          <td>${ag.disabled ? `<span class="status-inline red">已禁用</span>` : `<span class="status-inline green">启用</span>`}</td>
          <td style="text-align:right;">
            <button class="btn btn-sm" data-agent-edit="${esc(ag.name)}" ${disAttr}>编辑</button>
            <button class="btn btn-sm" data-agent-toggle-disabled="${esc(ag.name)}" ${disAttr}>${ag.disabled ? "启用" : "禁用"}</button>
            <button class="btn btn-sm btn-ghost" data-agent-show="${esc(ag.name)}">详情</button>
          </td>
        </tr>`;
    }).join("");

    root.innerHTML = `
      <div class="page-container">
        <div class="section-head">
          <div>
            <div class="section-title">本机 AI 工具（CLIs）</div>
            <div class="section-desc">自动识别已安装的命令行编程工具；如需重新走「选择 Lead」引导，可点右侧快捷入口。</div>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="btn btn-sm" id="btnCliOpenWizard" type="button" ${disAttr}>打开选 Lead 向导</button>
            <button class="btn btn-sm btn-primary" id="btnProbeAllAdapters" type="button" ${disAttr}>重新探测全部</button>
          </div>
        </div>
        <table class="clean-table">
          <thead><tr><th>工具</th><th>版本</th><th>状态</th><th>关联 Agent</th><th style="text-align:right;">操作</th></tr></thead>
          <tbody>${availableAdapters.map(renderAdapterRow).join("") || `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);">暂无可用工具</td></tr>`}</tbody>
        </table>
        ${unavailableAdapterSection}

        <div class="section-head" style="margin-top:26px;">
          <div>
            <div class="section-title">已注册 Agent (${esc(STATE.agents.length)})</div>
            <div class="section-desc">Lead 负责规划与验收，Worker 负责具体任务执行（两者自动隔离以保障审计安全）。</div>
          </div>
          <button class="btn btn-sm btn-primary" id="btnCreateAgentModal" type="button" ${disAttr}>新建 Agent</button>
        </div>
        <table class="clean-table">
          <thead><tr><th>名称</th><th>工具</th><th>模式</th><th>默认档位</th><th>Lead 资格</th><th>默认模型</th><th>并发</th><th>状态</th><th style="text-align:right;">操作</th></tr></thead>
          <tbody>${agentRows || `<tr><td colspan="9" style="text-align:center;color:var(--text-muted);">暂无已注册 Agent</td></tr>`}</tbody>
        </table>
      </div>`;
  }

  function shouldDeferSettingsRender(root) {
    if (!root) return false;
    const passInp = root.querySelector ? root.querySelector("#settingsPassInput") : null;
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (passInp && (passInp.value || active === passInp)) {
      return true;
    }
    if (active && root.contains && root.contains(active) && active.tagName === "SELECT") {
      return true;
    }
    return false;
  }

  function renderSettingsView({ force = false } = {}) {
    const root = $("settingsPageScroll");
    if (!root) return;
    if (!force && shouldDeferSettingsRender(root)) {
      settingsRenderDeferred = true;
      return;
    }
    settingsRenderDeferred = false;
    const allow = canMutate();
    const disAttr = allow ? "" : `disabled title="${esc(disabledReason())}"`;

    const leadCandidates = STATE.agents.filter(a => a.can_lead && !a.disabled);
    const curDefaultLead = STATE.config["lead.default_agent"] || "";
    const leadOpts = [`<option value="">(未设置默认 Lead)</option>`].concat(
      leadCandidates.map(a => `<option value="${esc(a.name)}" ${curDefaultLead === a.name ? "selected" : ""}>${esc(a.name)} (${esc(a.adapter)})</option>`)
    );

    const labelMap = Object.fromEntries(APPROVAL_POLICY_META.map(([cat, label, defVal, ro]) => [cat, { label, defVal, ro }]));
    const policyItemsFromConfig = STATE.configItems
      .filter(it => it && typeof it.key === "string" && it.key.startsWith("approval.policy."))
      .map(it => {
        const cat = it.key.slice("approval.policy.".length);
        const meta = labelMap[cat] || { label: cat, defVal: "direct_or_relayed", ro: cat === "config" || cat === "plan_dispatch" || cat === "step_dispatch" };
        return [it.key, cat, meta.label, it.value || meta.defVal, meta.ro];
      });
    const policyRowsData = policyItemsFromConfig.length
      ? policyItemsFromConfig
      : APPROVAL_POLICY_META.map(([cat, label, defVal, ro]) => [`approval.policy.${cat}`, cat, label, STATE.config[`approval.policy.${cat}`] || defVal, ro]);

    const policyRows = policyRowsData.map(([k, cat, label, val, isReadOnly]) => {
      const isDeliverBase = cat === "deliver_to_base";
      const isDirectOnlyFixed = cat === "config" || cat === "plan_dispatch" || cat === "step_dispatch";
      const ctrlDis = (isReadOnly || !allow) ? "disabled" : "";
      const titleAttr = isReadOnly
        ? (cat === "plan_dispatch"
          ? `title="计划派发确认固定为 direct_only，不可更改"`
          : (cat === "step_dispatch"
            ? `title="单步派发确认固定为 direct_only，不可更改"`
            : `title="系统配置策略固定为 direct_only，不可更改"`))
        : (!allow ? `title="${esc(disabledReason())}"` : "");
      return `
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">${esc(label)}</div>
            <div class="setting-hint mono">${esc(k)}</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" data-config-key="${esc(k)}" ${ctrlDis} ${titleAttr}>
              <option value="direct_only" ${val === "direct_only" ? "selected" : ""}>仅本机口令直连 (direct_only)</option>
              ${isDirectOnlyFixed ? "" : `<option value="direct_or_relayed" ${val === "direct_or_relayed" ? "selected" : ""}>直连或转发 (direct_or_relayed)</option>`}
              ${isDeliverBase ? `<option value="not_required" ${val === "not_required" ? "selected" : ""}>无需审批 (not_required)</option>` : ""}
            </select>
          </div>
        </div>`;
    }).join("");

    const activePrj = getActiveProject();
    const activePrjMode = (activePrj && (activePrj.permission_mode || STATE.chatPermissionMode)) || "plan";
    const activePrjActive = !activePrj || !activePrj.status || activePrj.status === "active";
    const activePrjModeDisAttr = (!allow || !activePrjActive)
      ? `disabled title="${esc(!allow ? disabledReason() : `当前项目状态为 ${activePrj.status}，仅进行中项目可切换权限模式`)}"`
      : "";
    const activePrjModeRow = activePrj ? `
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">当前项目权限模式（${esc(activePrj.name || activePrj.id)}）</div>
            <div class="setting-hint">${esc((PERMISSION_MODE_META[activePrjMode] && PERMISSION_MODE_META[activePrjMode].desc) || "")}</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" id="settingsProjectPermissionMode" data-project-mode-id="${esc(activePrj.id)}" ${activePrjModeDisAttr}>
              <option value="supervised" ${activePrjMode === "supervised" ? "selected" : ""}>每步问我 (supervised)</option>
              <option value="plan" ${activePrjMode === "plan" ? "selected" : ""}>计划批了就放手 (plan · 默认)</option>
              <option value="auto" ${activePrjMode === "auto" ? "selected" : ""}>全自动 (auto)</option>
            </select>
          </div>
        </div>` : "";

    const projNameById = Object.fromEntries(STATE.projects.map(p => [p.id, p.name || p.id]));
    const grantsList = Array.isArray(STATE.grants) ? STATE.grants : [];
    const formatGrantScope = g => {
      if (!g) return "--";
      if (g.category === "permission_escalation") {
        return `${g.scope_agent_name || g.scope_agent_id || "--"} · ${g.scope_profile || "--"}`;
      }
      if (g.category === "retry_over_limit") {
        return "本项目超限重试";
      }
      return g.scope_key || "--";
    };
    const grantsRowsHtml = grantsList.length
      ? grantsList.map(g => {
        const catLabel = GATE_CATEGORY_LABELS[g.category] || g.category || "--";
        const prjLabel = g.project_id ? (projNameById[g.project_id] || g.project_id) : "全局";
        const isActive = g.active !== false && !g.revoked_at;
        return `
          <tr>
            <td><span class="status-pill ${g.category === "permission_escalation" ? "red" : "amber"}">${esc(catLabel)}</span></td>
            <td>${esc(prjLabel)}</td>
            <td class="grant-scope-code">${esc(formatGrantScope(g))}</td>
            <td class="mono">${esc(formatDateTime(g.created_at))}</td>
            <td>${isActive ? `<span class="status-pill green">生效中</span>` : `<span class="status-pill neutral">已撤销</span>`}</td>
            <td style="text-align:right;">
              ${isActive
                ? `<button class="btn btn-sm btn-danger-text" type="button" data-revoke-grant-id="${esc(g.id)}" ${disAttr}>撤销</button>`
                : `<span style="font-size:11.5px;color:var(--text-muted);">${esc(g.revoked_reason || "已撤销")}</span>`}
            </td>
          </tr>`;
      }).join("")
      : `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);">暂无永久授权记录（点击审批卡片的「始终允许」后将在此列出，随时可撤销）</td></tr>`;

    const grantsSectionHtml = allow ? `
        <div class="section-head" style="margin-top:22px;">
          <div>
            <div class="section-title">永久授权（Standing Grants · ${grantsList.filter(g => g.active !== false && !g.revoked_at).length} 项生效）</div>
            <div class="section-desc">所有通过审批卡片「始终允许」创建的窄范围授权均记录在审计链中，可随时撤销。</div>
          </div>
          <div class="grants-toolbar">
            <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);cursor:pointer;">
              <input id="toggleIncludeRevokedGrants" type="checkbox" ${STATE.grantsIncludeRevoked ? "checked" : ""} />
              <span>显示已撤销</span>
            </label>
            <button class="btn btn-sm" id="btnRefreshGrants" type="button">刷新</button>
          </div>
        </div>
        <table class="clean-table" id="standingGrantsTable">
          <thead><tr><th>类型</th><th>项目</th><th>精确范围</th><th>授权时间</th><th>状态</th><th style="text-align:right;">操作</th></tr></thead>
          <tbody>${grantsRowsHtml}</tbody>
        </table>` : "";

    const vendorLeadsVal = Boolean(STATE.config["lead.vendor_leads"]);
    const evidenceModeVal = STATE.config["adapter.evidence_mode"] || "warn";
    const approvalModeVal = STATE.config["approval.mode"] || "passphrase";

    const capBadges = Object.entries(STATE.capabilities).map(([k, v]) =>
      `<span class="cap-chip ${v ? "supported" : ""}">${esc(k)}: ${v ? "true" : "false"}</span>`
    ).join(" ");

    const doc = STATE.doctor;
    const doctorHtml = doc ? `
      <div class="inspector-note-box ${doc.status === "healthy" ? "" : "amber"}" style="margin-top:8px;">
        <div><strong>诊断状态：</strong><span class="mono">${esc(doc.status)}</span></div>
        ${Array.isArray(doc.checks) ? `<div style="margin-top:6px;">${doc.checks.map(ch => `<div class="mono" style="font-size:11.5px;">${ch.ok ? "[OK]" : "[WARN]"} ${esc(ch.name)}: ${esc(ch.detail || "")}</div>`).join("")}</div>` : ""}
      </div>` : "";

    const credSupported = Boolean(STATE.auth.stored_credential_supported);
    const credPresent = Boolean(STATE.auth.stored_credential_present);
    const closeToTray = Boolean(STATE.desktopPrefs && STATE.desktopPrefs.close_to_tray);
    const trayActive = Boolean(STATE.desktopPrefs && STATE.desktopPrefs.tray_active);
    const traySupported = Boolean(STATE.desktopPrefs && STATE.desktopPrefs.tray_supported);

    const nextHtml = `
      <div class="page-container narrow">
        <div class="section-head">
          <div>
            <div class="section-title">身份认证与口令（默认仅内存保存）</div>
            <div class="section-desc">口令默认仅保存在当前进程内存中，绝不写入明文文件、环境变量或浏览器存储；关闭窗口最小化到托盘期间无需重复输入。</div>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">当前解锁状态</div>
            <div class="setting-hint">${STATE.auth.auth_verified ? `已认证：${esc((STATE.auth.principal && STATE.auth.principal.name) || "user")}` : "未解锁"}${credPresent ? " · 已启用 Windows 凭据托管" : ""}</div>
          </div>
          <div class="setting-control">
            <input class="compact-input" id="settingsPassInput" type="password" autocomplete="off" placeholder="输入口令..." style="width:180px;" />
            ${credSupported ? `<label class="inline-check" title="使用 Windows 凭据管理器加密保存（同用户信任边界）"><input id="settingsRememberCred" type="checkbox" /><span>记住解锁</span></label>` : ""}
            <button class="btn btn-sm btn-primary" id="btnSettingsUnlock" type="button">解锁</button>
            <button class="btn btn-sm" id="btnSettingsLock" type="button" ${(STATE.auth.auth_configured || credPresent) ? "" : "disabled"}>锁定并清除</button>
          </div>
        </div>
        ${credSupported ? `
          <div class="settings-security-note" id="settingsCredVaultWarning">
            <strong>安全边界提示（Windows 凭据托管为可选项，默认关闭）：</strong>勾选「记住解锁」后，口令将交由 Windows 凭据管理器（DPAPI）按当前 Windows 用户加密托管，不会写入明文文件、命令行参数、环境变量或 localStorage。但同一 Windows 用户账户下运行的其他进程（含本机 Agent）可以读取该凭据；运行不可信 Agent 时请勿启用，并可随时点击「锁定并清除」立即删除。
          </div>` : ""}

        <div class="section-head" style="margin-top:22px;">
          <div><div class="section-title">外观、托盘与协作设置</div></div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">界面主题</div>
            <div class="setting-hint">100% 不透明高对比度配色（深色 / 浅色）</div>
          </div>
          <div class="setting-control">
            <div class="segmented">
              <button type="button" id="btnThemeDark" class="${STATE.ui.theme === "dark" ? "active" : ""}">深色</button>
              <button type="button" id="btnThemeLight" class="${STATE.ui.theme === "light" ? "active" : ""}">浅色</button>
            </div>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">关闭窗口时最小化到系统托盘</div>
            <div class="setting-hint">${trayActive ? "托盘图标运行中：点击右上角 × 将隐藏窗口并保持登录态与后台服务在线" : (traySupported ? "仅在系统托盘激活时生效；退出界面不会停止后台服务，除非你明确选择停止" : "当前系统未启用 Windows 系统托盘（关闭窗口不会停止后台服务）")}</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" id="selectCloseToTray" ${traySupported ? "" : "disabled"}>
              <option value="true" ${closeToTray ? "selected" : ""}>最小化到托盘 (推荐)</option>
              <option value="false" ${!closeToTray ? "selected" : ""}>直接退出界面</option>
            </select>
            <button class="btn btn-sm" id="btnSettingsQuitApp" type="button">退出桌面端...</button>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">默认项目 Lead</div>
            <div class="setting-hint">新建项目对话时默认负责规划和调度的 Lead Agent</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" id="selectDefaultLead" ${disAttr}>${leadOpts.join("")}</select>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">允许本地 AI CLI 担任 Lead</div>
            <div class="setting-hint">开启后允许使用本地安装的 claude / codex / agy / droid / kiro / vibe 作为项目总管</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" data-config-key="lead.vendor_leads" data-config-type="bool" ${disAttr}>
              <option value="false" ${!vendorLeadsVal ? "selected" : ""}>关闭 (false)</option>
              <option value="true" ${vendorLeadsVal ? "selected" : ""}>开启 (true)</option>
            </select>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">工具版本校验模式</div>
            <div class="setting-hint">默认仅提示不拦截（warn）；严格模式仅放行通过内置冒烟验证的版本（strict）</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" data-config-key="adapter.evidence_mode" ${disAttr}>
              <option value="warn" ${evidenceModeVal === "warn" ? "selected" : ""}>警告放行 (warn)</option>
              <option value="strict" ${evidenceModeVal === "strict" ? "selected" : ""}>严格门控 (strict)</option>
            </select>
          </div>
        </div>
        ${activePrjModeRow}
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">审批模式</div>
            <div class="setting-hint">passphrase 仅允许本机口令直连确认；delegated 允许收件箱 Lead 代录非高危确认</div>
          </div>
          <div class="setting-control">
            <select class="compact-select" data-config-key="approval.mode" ${disAttr}>
              <option value="passphrase" ${approvalModeVal === "passphrase" ? "selected" : ""}>口令直连 (passphrase)</option>
              <option value="delegated" ${approvalModeVal === "delegated" ? "selected" : ""}>委派模式 (delegated)</option>
            </select>
          </div>
        </div>

        ${grantsSectionHtml}
        ${allow ? `<div class="section-head" style="margin-top:22px;"><div><div class="section-title">安全审批策略</div></div></div>${policyRows}` : ""}

        <div class="section-head" style="margin-top:22px;">
          <div><div class="section-title">后台服务与维护</div></div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">本地后台服务 (meshd)</div>
            <div class="setting-hint mono">${esc(STATE.pipe.endpoint || STATE.pipe.home || "--")}</div>
          </div>
          <div class="setting-control">
            <button class="btn btn-sm" id="btnSettingsStartDaemon" type="button" ${STATE.connected ? "disabled" : ""}>启动服务</button>
            <button class="btn btn-sm btn-danger-text" id="btnSettingsStopDaemon" type="button" ${allow ? "" : "disabled"}>停止服务</button>
          </div>
        </div>
        <div class="setting-row">
          <div class="setting-info">
            <div class="setting-name">环境诊断、日志与工作区清理</div>
            <div class="setting-hint">查看后台服务与桌面端日志路径（meshd.log / desktop.log），检查工具状态，校验事件哈希链或清理工作区</div>
          </div>
          <div class="setting-control">
            <button class="btn btn-sm" id="btnSettingsViewLog" type="button">查看日志</button>
            <button class="btn btn-sm" id="btnSettingsDoctor" type="button">环境诊断</button>
            <button class="btn btn-sm" id="btnSettingsAudit" type="button" ${disAttr}>审计校验</button>
            <button class="btn btn-sm" id="btnSettingsPruneWs" type="button" ${disAttr}>清理工作区</button>
          </div>
        </div>
        ${doctorHtml}

        <details class="config-raw-details" data-details-key="settings:dev-diag" style="margin-top:16px;">
          <summary>开发者诊断（能力状态与全部配置项）</summary>
          <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;">${capBadges}</div>
          ${STATE.configItems.length ? `
            <table class="clean-table" style="margin-top:10px;">
              <thead><tr><th>Key</th><th>Value</th><th>Default</th></tr></thead>
              <tbody>${STATE.configItems.map(it => `<tr><td class="mono">${esc(it.key)}</td><td class="mono">${esc(JSON.stringify(it.value))}</td><td class="mono">${esc(JSON.stringify(it.default))}</td></tr>`).join("")}</tbody>
            </table>` : ""}
        </details>
      </div>`;

    if (!force && nextHtml === lastSettingsHtml) {
      return;
    }
    const openKeys = new Set();
    if (root.querySelectorAll) {
      root.querySelectorAll("details[open][data-details-key]").forEach(el => {
        openKeys.add(el.getAttribute("data-details-key"));
      });
    }
    lastSettingsHtml = nextHtml;
    root.innerHTML = nextHtml;
    if (openKeys.size && root.querySelectorAll) {
      root.querySelectorAll("details[data-details-key]").forEach(el => {
        if (openKeys.has(el.getAttribute("data-details-key"))) {
          el.open = true;
        }
      });
    }
  }

  function renderAll({ forceSettings = false } = {}) {
    renderConnectionState();
    renderWizard();
    renderSidebar();
    renderCenterHeaderAndViews();
    if (STATE.ui.nav === "tasks") {
      renderChatThread();
      renderComposer();
    } else if (STATE.ui.nav === "cli") {
      renderCliView();
    } else if (STATE.ui.nav === "settings") {
      renderSettingsView({ force: forceSettings });
    }
    if (!STATE.ui.inspectorCollapsed) {
      renderInspector();
    }
    syncLogPollingTimer();
  }

  // =========================================================================
  // Modals & Popovers
  // =========================================================================
  function closeAllPopovers(exceptId) {
    ["daemonPopover", "leadPopover", "projectActionPopover"].forEach(id => {
      if (id === exceptId) return;
      const el = $(id);
      if (el) el.classList.add("hidden");
    });
  }

  function settleModalPrompt(value = null) {
    if (modalPromptResolver) {
      const resolve = modalPromptResolver;
      modalPromptResolver = null;
      resolve(value);
    }
  }

  function openModal(title, bodyHtml, footHtml) {
    closeConfirmModal(false);
    settleModalPrompt(null);
    if ($("modalTitle")) $("modalTitle").textContent = title;
    if ($("modalBody")) $("modalBody").innerHTML = bodyHtml;
    if ($("modalFoot")) $("modalFoot").innerHTML = footHtml || "";
    if ($("modalBackdrop")) $("modalBackdrop").classList.remove("hidden");
  }

  function closeConfirmModal(result = false) {
    if (confirmModalGuardTimer !== null) {
      clearTimeout(confirmModalGuardTimer);
      confirmModalGuardTimer = null;
    }
    const backdrop = $("confirmModalBackdrop");
    if (backdrop) backdrop.classList.add("hidden");
    if ($("confirmModalBody")) $("confirmModalBody").innerHTML = "";
    if ($("confirmModalFoot")) $("confirmModalFoot").innerHTML = "";
    if (confirmModalResolver) {
      const resolve = confirmModalResolver;
      confirmModalResolver = null;
      resolve(Boolean(result));
    }
  }

  function closeModal() {
    closeConfirmModal(false);
    settleModalPrompt(null);
    editAgentModalSeq += 1;
    if ($("modalBackdrop")) $("modalBackdrop").classList.add("hidden");
    if ($("modalBody")) $("modalBody").innerHTML = "";
    if ($("modalFoot")) $("modalFoot").innerHTML = "";
  }

  function toggleDaemonPopover() {
    const pop = $("daemonPopover");
    const wasHidden = pop.classList.contains("hidden");
    closeAllPopovers();
    if (!wasHidden) return;
    const allowStop = canMutate();
    pop.innerHTML = `
      <div class="popover-label">后台服务控制</div>
      <div style="padding:4px 8px;font-size:11.5px;color:var(--text-muted);" class="mono">
        ${STATE.connected ? "在线" : "离线"} · ${esc(STATE.pipe.home)}
      </div>
      <div class="popover-divider"></div>
      <button class="popover-item" type="button" id="popDaemonStatus">查看服务状态详情</button>
      <button class="popover-item" type="button" id="popDaemonLogs">查看日志与诊断</button>
      <button class="popover-item" type="button" id="popDaemonStart" ${STATE.connected ? "disabled" : ""}>启动后台服务</button>
      <button class="popover-item" type="button" id="popDaemonReconnect">立即重连</button>
      <button class="popover-item" type="button" id="popDaemonStop" ${allowStop ? "" : "disabled"}>停止后台服务</button>
      <div class="popover-divider"></div>
      <button class="popover-item" type="button" id="popQuitDesktop">退出桌面端...</button>`;
    pop.classList.remove("hidden");
  }

  function openQuitDesktopModal() {
    closeAllPopovers();
    const canStop = canMutate();
    openModal(
      "退出 Agent Mesh 桌面端",
      `<div class="form-stack">
        <div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">
          默认退出桌面界面<strong>不会停止后台服务 (meshd)</strong>，正在执行的 Agent 任务会继续在后台完成。如需同时优雅停止后台服务，请明确选择下方右侧按钮。
        </div>
      </div>`,
      `<button class="btn" id="modalQuitCancel" type="button">取消</button>
       <button class="btn btn-danger-text" id="modalQuitAndStop" type="button" ${canStop ? "" : "disabled title=\"需先解锁且后台服务在线\""}>退出并停止后台服务</button>
       <button class="btn btn-primary" id="modalQuitOnly" type="button">仅退出界面（保持后台运行）</button>`
    );
    $("modalQuitCancel").onclick = closeModal;
    $("modalQuitOnly").onclick = async () => {
      closeModal();
      if (hasApi() && typeof window.pywebview.api.quit_app === "function") {
        const res = await window.pywebview.api.quit_app(false);
        if (res && !res.ok) toast(formatErrorMessage(res.error), "err");
      }
    };
    $("modalQuitAndStop").onclick = async () => {
      if (!canMutate()) { toast(disabledReason(), "err"); return; }
      closeModal();
      if (hasApi() && typeof window.pywebview.api.quit_app === "function") {
        const res = await window.pywebview.api.quit_app(true);
        if (res && !res.ok) toast(formatErrorMessage(res.error), "err");
      }
    };
  }

  // A5 + B5: Lead + Model Quick Switch Popover with live search (strictly bounded to agent's allowed_models; no profile/binary fields)
  function toggleLeadPopover() {
    const pop = $("leadPopover");
    const wasHidden = pop.classList.contains("hidden");
    closeAllPopovers();
    if (!wasHidden) return;
    const prj = getActiveProject();
    const isNewDraft = !prj || STATE.ui.draftNewChat;
    const defaultLead = STATE.config["lead.default_agent"] || "";
    const curLead = STATE.ui.selectedLeadDraft || (prj && prj.lead) || defaultLead;
    const activePid = (!isNewDraft && prj) ? prj.id : null;

    if (!STATE.agents.length) {
      pop.innerHTML = `<div class="popover-label">暂无已配置 Agent（请前往「AI 工具」或选 Lead 向导）</div>`;
      pop.classList.remove("hidden");
      return;
    }

    const renderGroups = (queryStr) => {
      const q = String(queryStr || "").trim().toLowerCase();
      const grouped = new Map();
      STATE.agents.forEach(ag => {
        const grpKey = ag.delivery === "inbox" ? "inbox" : (ag.adapter || "other");
        if (!grouped.has(grpKey)) grouped.set(grpKey, []);
        grouped.get(grpKey).push(ag);
      });

      const groupBlocks = [];
      Array.from(grouped.entries()).forEach(([grpKey, agents]) => {
        const grpTitle = grpKey === "inbox" ? "收件箱协调 (Inbox)" : grpKey;
        const rows = [];
        agents.forEach(ag => {
          const elig = getLeadEligibility(ag, activePid);
          const det = STATE.agentDetails[ag.name] || null;
          const badges = [];
          if (ag.name === defaultLead) badges.push("默认");
          if (prj && ag.name === prj.lead) badges.push("当前项目");
          const curModel = (det && det.default_model) || ag.default_model || "";
          const curEffort = (det && det.default_effort) || ag.default_effort || "";
          const reasonStr = elig.reasons.length
            ? elig.reasons.join("；")
            : (elig.warn || `${ag.adapter} · ${ag.delivery}${curModel ? ` · ${curModel}` : ""}`);

          const allowedModels = Array.isArray((det && det.allowed_models) || ag.allowed_models)
            ? ((det && det.allowed_models) || ag.allowed_models).filter(Boolean)
            : [];
          const allowedEfforts = Array.isArray((det && det.allowed_efforts) || ag.allowed_efforts)
            ? ((det && det.allowed_efforts) || ag.allowed_efforts).filter(Boolean)
            : [];

          const agentHaystack = `${ag.name} ${ag.adapter || ""} ${ag.delivery || ""} ${curModel}`.toLowerCase();
          const matchingModels = q
            ? allowedModels.filter(m => m.toLowerCase().includes(q) || agentHaystack.includes(q))
            : allowedModels;
          if (q && !agentHaystack.includes(q) && matchingModels.length === 0) {
            return;
          }

          const showModelChips = ag.can_lead && !ag.disabled && ag.delivery === "launch" && matchingModels.length > 0;
          const modelChipsHtml = showModelChips
            ? `<div class="lead-pop-models">${matchingModels.map(m =>
                `<button class="lead-model-chip ${m === curModel ? "active" : ""}" type="button" data-switch-agent-model="${esc(ag.name)}" data-model="${esc(m)}" ${canMutate() ? "" : "disabled"}>${esc(m)}</button>`
              ).join("")}${allowedEfforts.map(ef =>
                `<button class="lead-model-chip ${ef === curEffort ? "active" : ""}" type="button" data-switch-agent-effort="${esc(ag.name)}" data-effort="${esc(ef)}" ${canMutate() ? "" : "disabled"}>effort:${esc(ef)}</button>`
              ).join("")}</div>`
            : "";

          rows.push(`
            <div style="display:flex;align-items:center;gap:4px;">
              <button class="popover-item ${ag.name === curLead ? "active" : ""}" style="flex:1;" type="button" data-pick-lead="${esc(ag.name)}" ${elig.eligible && canMutate() ? "" : "disabled"}>
                <div>${esc(ag.name)} ${badges.length ? `<span class="mono" style="font-size:10.5px;color:var(--text-muted);">[${esc(badges.join(" · "))}]</span>` : ""}</div>
                <div class="popover-sub">${esc(reasonStr)}</div>
              </button>
              ${ag.can_lead && !ag.disabled && canMutate() ? `<button class="btn btn-sm btn-ghost" type="button" data-set-default-lead="${esc(ag.name)}">设为默认</button>` : ""}
            </div>
            ${modelChipsHtml}`);
        });
        if (rows.length) {
          groupBlocks.push(`<div class="lead-pop-group"><div class="lead-pop-group-title">${esc(grpTitle)}</div>${rows.join("")}</div>`);
        }
      });

      return groupBlocks.length
        ? groupBlocks.join("")
        : `<div class="empty-state" style="padding:12px;">未找到匹配的 Lead 或模型</div>`;
    };

    STATE.ui.leadPopoverQuery = "";
    const workers = STATE.agents.filter(a => !a.can_lead && !a.disabled);
    const workerSummary = workers.length
      ? workers.slice(0, 3).map(w => `${w.name}${w.default_model ? `(${w.default_model})` : ""}`).join("、") + (workers.length > 3 ? ` 等 ${workers.length} 个` : "")
      : "暂无启用 Worker";
    pop.innerHTML = `
      <div class="popover-label">${isNewDraft ? "选择新对话的 Lead 与模型" : "切换当前项目 Lead 或 Lead 模型"}</div>
      <div class="lead-pop-search-wrap">
        <input class="compact-input" id="leadPopoverSearch" type="text" placeholder="搜索 Lead 或模型名称..." autocomplete="off" />
      </div>
      <div id="leadPopoverGroups">${renderGroups("")}</div>
      <div class="lead-pop-worker-hint">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(workerSummary)}">Worker 模型：${esc(workerSummary)}</span>
        <button class="btn btn-sm btn-ghost" type="button" data-nav-goto="cli" style="flex-shrink:0;">管理 Worker</button>
      </div>`;
    pop.classList.remove("hidden");
    const searchInp = $("leadPopoverSearch");
    if (searchInp) {
      searchInp.addEventListener("click", ev => ev.stopPropagation());
      searchInp.addEventListener("input", () => {
        STATE.ui.leadPopoverQuery = searchInp.value || "";
        const grpEl = $("leadPopoverGroups");
        if (grpEl) grpEl.innerHTML = renderGroups(STATE.ui.leadPopoverQuery);
      });
      searchInp.focus();
    }
  }

  function toggleProjectActionPopover() {
    const pop = $("projectActionPopover");
    const wasHidden = pop.classList.contains("hidden");
    closeAllPopovers();
    if (!wasHidden) return;
    const prj = getActiveProject();
    if (!prj) return;
    const allow = canMutate();
    const dis = allow ? "" : "disabled";
    const readDis = STATE.connected && STATE.auth.auth_verified ? "" : "disabled";
    const isTerminal = prj.status === "completed" || prj.status === "cancelled";
    pop.innerHTML = `
      <div class="popover-label">项目操作</div>
      <button class="popover-item" type="button" data-prj-op="leads" ${readDis}>Lead 交接历史</button>
      ${!isTerminal ? `<button class="popover-item" type="button" data-prj-op="new_task" ${dis}>高级：手工创建任务</button>` : ""}
      ${prj.status === "active" ? `<button class="popover-item" type="button" data-prj-op="pause" ${dis}>暂停项目</button>` : ""}
      ${prj.status === "paused" ? `<button class="popover-item" type="button" data-prj-op="resume" ${dis}>恢复项目</button>` : ""}
      ${!isTerminal ? `<button class="popover-item" type="button" data-prj-op="update" ${dis}>更新目标与验收标准</button>` : ""}
      ${prj.status === "active" ? `<button class="popover-item" type="button" data-prj-op="complete" ${dis}>提交完成报告</button>` : ""}
      ${prj.repo && prj.status !== "cancelled" ? `<button class="popover-item" type="button" data-prj-op="deliver" ${dis}>交付到本地分支</button>` : ""}
      ${!isTerminal ? `<button class="popover-item" type="button" data-prj-op="cancel" ${dis}>取消项目</button>` : ""}`;
    pop.classList.remove("hidden");
  }

  // =========================================================================
  // Business Actions (chat.send, daemon, tasks, projects, advanced forms)
  // =========================================================================
  async function openDaemonStatusModal() {
    if (hasApi()) {
      try {
        await window.pywebview.api.daemon_status();
        await refreshConnectionAndAuth();
      } catch (_) {
        // Use cached snapshot
      }
    }
    const ds = STATE.pipe.daemon_status;
    const rows = [
      ["状态", STATE.connected ? "运行中" : "离线"],
      ["版本", STATE.pipe.daemon_version || "--"],
      ["通信通道", STATE.pipe.transport || "--"],
      ["端点", STATE.pipe.endpoint || "--"],
      ["数据目录", (ds && ds.home) || STATE.pipe.home || "--"],
      ["PID", ds && ds.pid !== undefined ? String(ds.pid) : "--"],
      ["运行时长 (s)", ds && ds.uptime_s !== undefined ? String(ds.uptime_s) : "--"],
      ["boot_id", (ds && ds.boot_id) || "--"],
      ["testing_mode", ds && ds.testing_mode !== undefined ? String(Boolean(ds.testing_mode)) : "--"],
      ["active_runs", ds && ds.active_runs !== undefined ? String(ds.active_runs) : "--"],
      ["active_streams", ds && ds.active_streams !== undefined ? String(ds.active_streams) : "--"],
      ["active_waits", ds && ds.active_waits !== undefined ? String(ds.active_waits) : "--"]
    ];
    const killFailed = ds && ds.kill_failed_runs !== undefined ? ds.kill_failed_runs : [];
    const scheduler = ds && ds.scheduler !== undefined ? ds.scheduler : {};
    openModal(
      "后台服务状态详情",
      `<div class="form-stack">
        <div class="kv-list">${rows.map(([k, v]) => `<div class="kv-row"><span class="kv-k">${esc(k)}</span><span class="kv-v mono">${esc(v)}</span></div>`).join("")}</div>
        <div class="inspector-section"><div class="inspector-sec-label">kill_failed_runs</div><pre class="log-box" style="max-height:110px;">${esc(JSON.stringify(killFailed, null, 2))}</pre></div>
        <div class="inspector-section"><div class="inspector-sec-label">scheduler</div><pre class="log-box" style="max-height:130px;">${esc(JSON.stringify(scheduler, null, 2))}</pre></div>
      </div>`,
      `<button class="btn" type="button" data-close-modal="1">关闭</button>`
    );
  }

  async function handleUnlockPassphrase(pw, remember = false) {
    if (!pw) {
      toast("请输入口令", "warn");
      return;
    }
    const res = remember
      ? await window.pywebview.api.set_passphrase(pw, true, true)
      : await window.pywebview.api.set_passphrase(pw, true);
    if (res && res.ok) {
      STATE.auth = res.data;
      if (STATE.auth && STATE.auth.auth_verified) {
        STATE.connected = true;
        STATE.loadingWorkspace = true;
        STATE.hasLoadedWorkspace = false;
        renderAll();
      }
      toast("口令已解锁", "ok");
      await refreshConnectionAndAuth();
      if (STATE.connected && STATE.auth.auth_verified) {
        await loadAllWorkspaceData();
      } else {
        STATE.loadingWorkspace = false;
        renderAll();
      }
    } else {
      STATE.auth.auth_verified = false;
      STATE.auth.last_auth_error = (res && res.error) || null;
      STATE.loadingWorkspace = false;
      toast(formatErrorMessage(res && res.error), "err");
      renderAll();
    }
  }

  async function openDaemonLogModal() {
    if (hasApi()) {
      try { await refreshConnectionAndAuth(); } catch (_) {}
    }
    const diagLog = Array.isArray(STATE.pipe.daemon_diag_log) ? STATE.pipe.daemon_diag_log : [];
    const lastErr = STATE.pipe.last_start_error || STATE.pipe.last_error;
    const logLines = diagLog.length
      ? diagLog.map(item => `[${item.at || "--"}] [${(item.level || "info").toUpperCase()}] ${item.code || ""}: ${item.message || ""}`).join("\n")
      : "暂无本次会话的启动诊断记录。";
    const errJson = lastErr ? JSON.stringify(lastErr, null, 2) : "无";
    openModal(
      "后台服务与桌面端诊断日志",
      `<div class="form-stack">
        <div class="kv-list">
          <div class="kv-row"><span class="kv-k">数据目录</span><span class="kv-v mono">${esc(STATE.pipe.home || "--")}</span></div>
          <div class="kv-row"><span class="kv-k">已初始化</span><span class="kv-v mono">${esc(String(STATE.pipe.home_initialized))}</span></div>
          <div class="kv-row"><span class="kv-k">后台服务日志 (meshd.log)</span><span class="kv-v mono">${esc(STATE.pipe.daemon_log_path || "--")}</span></div>
          <div class="kv-row"><span class="kv-k">桌面端日志 (desktop.log)</span><span class="kv-v mono">${esc(STATE.pipe.desktop_log_path || "--")}</span></div>
        </div>
        <div class="inspector-section"><div class="inspector-sec-label">最近错误详情（已脱敏）</div><pre class="log-box" style="max-height:130px;">${esc(errJson)}</pre></div>
        <div class="inspector-section"><div class="inspector-sec-label">启动与桥接诊断记录（同时轮转写入 desktop.log，绝不记录口令或密钥）</div><pre class="log-box" style="max-height:150px;">${esc(logLines)}</pre></div>
        <div id="daemonLogDoctorBox"></div>
      </div>`,
      `<button class="btn" id="btnModalRunDoctor" type="button">运行环境诊断</button><button class="btn btn-primary" id="btnModalRetryStart" type="button" ${STATE.connected || STATE.daemonStarting ? "disabled" : ""}>重试启动</button><button class="btn" type="button" data-close-modal="1">关闭</button>`
    );
    if ($("btnModalRunDoctor")) {
      $("btnModalRunDoctor").onclick = async () => {
        if (!hasApi()) return;
        const box = $("daemonLogDoctorBox");
        if (box) box.innerHTML = `<div class="empty-state">正在运行环境诊断...</div>`;
        try {
          const res = await window.pywebview.api.doctor_report();
          if (res && res.ok) {
            STATE.doctor = res.data;
            if (box) box.innerHTML = `<div class="inspector-section"><div class="inspector-sec-label">诊断报告 (${esc(res.data.status || "--")})</div><pre class="log-box" style="max-height:170px;">${esc(JSON.stringify(res.data, null, 2))}</pre></div>`;
          } else if (box) {
            box.innerHTML = `<div class="inspector-note-box red">${esc(formatErrorMessage(res && res.error))}</div>`;
          }
        } catch (err) {
          if (box) box.innerHTML = `<div class="inspector-note-box red">环境诊断失败: ${esc(String(err))}</div>`;
        }
      };
    }
    if ($("btnModalRetryStart")) {
      $("btnModalRetryStart").onclick = async () => {
        closeModal();
        await handleStartDaemon();
      };
    }
  }

  async function handleStartDaemon() {
    if (!hasApi() || STATE.daemonStarting) return;
    STATE.daemonStarting = true;
    renderConnectionState();
    const res = await window.pywebview.api.start_daemon();
    STATE.daemonStarting = false;
    await refreshConnectionAndAuth();
    if (res && res.ok) {
      if (!(res.data && res.data.in_progress)) toast("后台服务已启动", "ok");
      if (STATE.connected && STATE.auth.auth_verified) {
        if (!STATE.hasLoadedWorkspace) {
          STATE.loadingWorkspace = true;
          renderAll();
        }
        await loadAllWorkspaceData();
      } else {
        renderAll();
      }
    } else {
      if (res && res.error && res.error.code === "HOME_NOT_INITIALIZED") {
        STATE.pipe.home_initialized = false;
        openWizard(1);
      }
      renderAll();
    }
  }

  async function handleStopDaemon() {
    const res = await window.pywebview.api.stop_daemon();
    if (res && res.ok) {
      toast("后台服务正在停止", "info");
      STATE.connected = false;
      STATE.userStopped = true;
      STATE.daemonStopping = true;
      await refreshConnectionAndAuth();
      renderAll();
    } else {
      toast(formatErrorMessage(res && res.error), "err");
    }
  }

  function validateRepoInputs(repo, baseBranch, rawTestCmd) {
    if (!repo) return { ok: true, parsedTestCmd: null };
    if (!baseBranch) return { ok: false, error: "绑定 Git 仓库时必须填写基准分支（如 main 或 master）" };
    if (!rawTestCmd) return { ok: false, error: "绑定 Git 仓库时必须填写测试命令（JSON 数组或 \"none\"）" };
    if (rawTestCmd === "none") return { ok: true, parsedTestCmd: "none" };
    let parsed;
    try {
      parsed = JSON.parse(rawTestCmd);
    } catch (_) {
      return { ok: false, error: "测试命令必须是合法的 JSON 字符串数组（如 [\"pytest\"]）或 \"none\"" };
    }
    if (!Array.isArray(parsed) || !parsed.length || parsed.some(x => typeof x !== "string" || !x)) {
      return { ok: false, error: "测试命令必须是非空字符串数组或 \"none\"" };
    }
    return { ok: true, parsedTestCmd: parsed };
  }

  // B2: Interrupt active read-only Lead turn (Stop) or Interrupt + Steer (Ctrl+Enter)
  async function handleInterruptLeadTurn({ steerText = null } = {}) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const prj = getActiveProject();
    if (!prj || STATE.ui.draftNewChat) return;
    if (STATE.ui.interruptingLead) return;

    const sentPid = prj.id;
    const sentGen = projectSwitchGen;
    const payload = { project_id: sentPid };
    if (STATE.activeLeadRun && STATE.activeLeadRun.id) {
      payload.run_id = STATE.activeLeadRun.id;
    }
    if (typeof steerText === "string" && steerText.trim()) {
      payload.steer_text = steerText.trim();
    }

    STATE.ui.interruptingLead = true;
    renderComposer();
    try {
      const res = await apiCall("lead.interrupt", payload);
      const contextChanged =
        projectSwitchGen !== sentGen ||
        STATE.ui.draftNewChat ||
        STATE.activeProjectId !== sentPid ||
        !STATE.connected ||
        !STATE.auth.auth_verified;
      if (!res.ok) {
        toast(formatErrorMessage(res.error), "err");
        return;
      }
      if (contextChanged) {
        toast(payload.steer_text ? "已打断原项目 Lead 回合并发送改向消息" : "已请求停止原项目 Lead 回合", "info");
        if (STATE.connected && STATE.auth.auth_verified) scheduleRefresh({ full: true });
        return;
      }
      if (payload.steer_text && $("composerInput")) {
        $("composerInput").value = "";
        syncComposerTextareaHeight();
      }
      if (res.data && res.data.message && res.data.message.id) {
        if (!STATE.chatMessages.some(m => m && m.id === res.data.message.id)) {
          STATE.chatMessages = STATE.chatMessages.concat([res.data.message]);
        }
      }
      if (res.data && Object.prototype.hasOwnProperty.call(res.data, "lead_status")) {
        STATE.chatLeadStatus = res.data.lead_status || null;
      }
      toast(payload.steer_text ? "已打断当前 Lead 回合并发送改向消息" : "已请求停止当前 Lead 回合", "info");
      await loadActiveProjectLight();
      if (projectSwitchGen === sentGen && STATE.activeProjectId === sentPid) {
        scrollChatToBottom();
      }
    } finally {
      STATE.ui.interruptingLead = false;
      renderComposer();
    }
  }

  // B2: Cancel an unconsumed non-approval queued follow-up user message
  async function handleCancelQueuedMessage(messageId) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const prj = getActiveProject();
    if (!prj || !messageId) return;
    if (cancellingQueuedMsgIds.has(messageId)) return;
    const msg = STATE.chatMessages.find(m => m && m.id === messageId);
    if (msg && (isRelayedChatMessage(msg) || msg.approval_id || msg.plan_id || msg.task_id || msg.consumed_by_approval_id)) {
      toast("该消息不可取消", "err");
      return;
    }
    const sentPid = prj.id;
    const sentGen = projectSwitchGen;
    cancellingQueuedMsgIds.add(messageId);
    try {
      const res = await apiCall("chat.queue_cancel", { project_id: sentPid, message_id: messageId });
      const sameContext = projectSwitchGen === sentGen && !STATE.ui.draftNewChat && STATE.activeProjectId === sentPid && STATE.connected && STATE.auth.auth_verified;
      if (!res.ok) {
        toast(formatErrorMessage(res.error), "err");
        if (sameContext) await loadActiveProjectLight();
        else if (STATE.connected && STATE.auth.auth_verified) scheduleRefresh({ full: true });
        return;
      }
      if (STATE.connected && STATE.auth.auth_verified && STATE.queuedUserMsgIdsByProject[sentPid]) {
        STATE.queuedUserMsgIdsByProject[sentPid].delete(messageId);
      }
      toast("已取消该排队消息", "ok");
      if (sameContext) await loadActiveProjectLight();
      else if (STATE.connected && STATE.auth.auth_verified) scheduleRefresh({ full: true });
    } finally {
      cancellingQueuedMsgIds.delete(messageId);
    }
  }

  // B2: Edit an unconsumed non-approval queued follow-up user message
  function handleEditQueuedMessage(messageId) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const prj = getActiveProject();
    if (!prj || !messageId) return;
    const msg = STATE.chatMessages.find(m => m && m.id === messageId);
    if (!msg) return;
    if (isRelayedChatMessage(msg) || msg.approval_id || msg.plan_id || msg.task_id || msg.consumed_by_approval_id) {
      toast("该消息不可编辑", "err");
      return;
    }
    const sentPid = prj.id;
    const sentGen = projectSwitchGen;
    openModal(
      "编辑排队中的消息",
      `<div class="form-stack">
        <div class="form-row">
          <label class="form-label" for="editQueuedMsgText">仅在 Lead 尚未读取该消息前可编辑（不会修改历史审计事件）</label>
          <textarea class="compact-textarea" id="editQueuedMsgText" rows="4">${esc(msg.body || "")}</textarea>
        </div>
      </div>`,
      `<button class="btn" id="modalEditQueuedCancel" type="button">取消</button>
       <button class="btn btn-primary" id="modalEditQueuedSave" type="button">保存修改</button>`
    );
    $("modalEditQueuedCancel").onclick = closeModal;
    let editQueuedSubmitting = false;
    $("modalEditQueuedSave").onclick = async () => {
      if (editQueuedSubmitting) return;
      const nextText = ($("editQueuedMsgText") && $("editQueuedMsgText").value || "").trim();
      if (!nextText) { toast("消息内容不能为空", "warn"); return; }
      editQueuedSubmitting = true;
      const saveBtn = $("modalEditQueuedSave");
      if (saveBtn) saveBtn.disabled = true;
      try {
        const res = await apiCall("chat.queue_edit", {
          project_id: sentPid,
          message_id: messageId,
          text: nextText
        });
        const sameContext = projectSwitchGen === sentGen && !STATE.ui.draftNewChat && STATE.activeProjectId === sentPid && STATE.connected && STATE.auth.auth_verified;
        if (!res.ok) {
          toast(formatErrorMessage(res.error), "err");
          if (sameContext) await loadActiveProjectLight();
          else if (STATE.connected && STATE.auth.auth_verified) scheduleRefresh({ full: true });
          return;
        }
        closeModal();
        toast("排队消息已更新", "ok");
        if (sameContext) await loadActiveProjectLight();
        else if (STATE.connected && STATE.auth.auth_verified) scheduleRefresh({ full: true });
      } finally {
        editQueuedSubmitting = false;
        if (saveBtn) saveBtn.disabled = false;
      }
    };
  }

  async function handleComposerSubmit() {
    STATE.ui.composerInlineNote = null;
    if (!canMutate()) {
      STATE.ui.composerInlineNote = { level: "err", text: disabledReason() };
      renderComposer();
      return;
    }
    if (STATE.ui.sendingChat) return;

    const ta = $("composerInput");
    const text = (ta && ta.value || "").trim();
    if (!text) return;

    const prj = getActiveProject();
    const isNewDraft = !prj || STATE.ui.draftNewChat;

    const payload = { text };
    let repo = "";
    let baseBranch = "";
    let parsedTestCmd = null;

    if (!isNewDraft && prj) {
      payload.project_id = prj.id;
    } else {
      const leadName = STATE.ui.selectedLeadDraft || STATE.config["lead.default_agent"] || null;
      if (leadName) payload.lead = leadName;
      repo = ($("newPrjRepo") && $("newPrjRepo").value.trim()) || "";
      baseBranch = ($("newPrjBaseBranch") && $("newPrjBaseBranch").value.trim()) || "";
      const rawTestCmd = ($("newPrjTestCmd") && $("newPrjTestCmd").value.trim()) || "";
      if (repo && STATE.ui.repoInspection && STATE.ui.repoInspection.path_conflict) {
        STATE.ui.composerInlineNote = { level: "err", text: STATE.ui.repoInspection.path_conflict };
        STATE.ui.repoDrawerOpen = true;
        renderComposer();
        return;
      }
      const check = validateRepoInputs(repo, baseBranch, rawTestCmd);
      if (!check.ok) {
        STATE.ui.composerInlineNote = { level: "warn", text: check.error };
        STATE.ui.repoDrawerOpen = true;
        renderComposer();
        return;
      }
      parsedTestCmd = check.parsedTestCmd;
      if (repo) {
        payload.repo = repo;
        payload.base_branch = baseBranch;
        payload.test_command = parsedTestCmd;
      }
    }

    const wasLeadBusyBeforeSend = Boolean(!isNewDraft && STATE.activeLeadRun);
    const sentPid = isNewDraft ? null : (prj && prj.id);
    const sentDraft = isNewDraft;
    const sentGen = projectSwitchGen;
    const doSend = async () => {
      STATE.ui.sendingChat = true;
      renderComposer();
      try {
        const res = await apiCall("chat.send", payload);
        const contextChanged =
          projectSwitchGen !== sentGen ||
          STATE.ui.draftNewChat !== sentDraft ||
          (!sentDraft && STATE.activeProjectId !== sentPid);
        if (!res.ok) {
          if (!contextChanged) {
            STATE.ui.composerInlineNote = { level: "err", text: formatErrorMessage(res.error) };
          }
          toast(formatErrorMessage(res.error), "err");
          return;
        }
        const newPid = res.data && (res.data.project_id || (res.data.project && res.data.project.id));
        if (contextChanged) {
          if (res.data && res.data.project && !STATE.projects.some(p => p && p.id === res.data.project.id)) {
            STATE.projects = [res.data.project].concat(STATE.projects);
          }
          if (wasLeadBusyBeforeSend && newPid && res.data && res.data.message && res.data.message.id) {
            if (!STATE.queuedUserMsgIdsByProject[newPid]) {
              STATE.queuedUserMsgIdsByProject[newPid] = new Set();
            }
            STATE.queuedUserMsgIdsByProject[newPid].add(res.data.message.id);
          }
          const targetPrj = newPid ? STATE.projects.find(p => p && p.id === newPid) : null;
          const targetLabel = (targetPrj && targetPrj.name) || newPid || "";
          toast(targetLabel ? `已发送至项目「${targetLabel}」` : "已发送", "ok");
          scheduleRefresh({ full: true });
          return;
        }

        if (ta) ta.value = "";
        if (isNewDraft) {
          if (repo) addRecentFolder(repo);
          if ($("newPrjRepo")) $("newPrjRepo").value = "";
          if ($("newPrjBaseBranch")) $("newPrjBaseBranch").value = "";
          if ($("newPrjTestCmd")) $("newPrjTestCmd").value = "";
          STATE.ui.repoInspection = null;
          STATE.ui.repoInspecting = false;
          STATE.ui.draftNewChat = false;
          STATE.ui.repoDrawerOpen = false;
          STATE.ui.selectedLeadDraft = null;
        }
        if (isNewDraft) {
          STATE.activeProjectDetail = null;
          STATE.chatProjectStatus = null;
          STATE.chatPlanGateMode = "require_approval";
          STATE.chatPermissionMode = "plan";
          STATE.chatMessages = [];
          STATE.chatHasMore = false;
          STATE.chatOldestSeq = null;
          STATE.chatLoadingOlder = false;
          STATE.chatPlansById = {};
          STATE.chatApprovalsById = {};
          STATE.chatNewestPageApprovalIds = new Set();
          STATE.chatLeadStatus = null;
          STATE.activeLeadRun = null;
          STATE.plans = [];
          STATE.tasks = [];
          STATE.activeTaskId = null;
          STATE.taskDetails = {};
          STATE.taskWorkspaces = {};
          STATE.taskLogs = {};
          STATE.taskActivities = {};
          STATE.taskDiffs = {};
          STATE.taskDeps = {};
          STATE.leadTurnLogs = {};
          STATE.events = [];
          STATE.eventsProjectId = null;
          STATE.minRetainedSeq = 0;
          STATE.maxSeenEventSeq = 0;
          STATE.seenSeqs = new Set();
          lastChatThreadHtml = null;
          lastChatMsgCount = 0;
          lastChatTailId = null;
          stripApprovalOrder = [];
          stripOrderProjectId = null;
          inspectorApprovalOrder = [];
          inspectorOrderProjectId = null;
          clearRecentlyResolvedApprovals();
          resetApprovalSurfaceGuards();
        }
        if (newPid) {
          STATE.activeProjectId = newPid;
          rememberActiveProjectForHome(newPid);
        }
        if (res.data && Object.prototype.hasOwnProperty.call(res.data, "lead_status")) {
          STATE.chatLeadStatus = res.data.lead_status || null;
          if (STATE.chatLeadStatus && STATE.chatLeadStatus.active_run) {
            STATE.activeLeadRun = STATE.chatLeadStatus.active_run;
          }
        }
        if (res.data && res.data.project && !STATE.projects.some(p => p && p.id === res.data.project.id)) {
          STATE.projects = [res.data.project].concat(STATE.projects);
          STATE.activeProjectDetail = res.data.project;
        }
        if (res.data && res.data.message && res.data.message.id) {
          if (wasLeadBusyBeforeSend && newPid) {
            if (!STATE.queuedUserMsgIdsByProject[newPid]) {
              STATE.queuedUserMsgIdsByProject[newPid] = new Set();
            }
            STATE.queuedUserMsgIdsByProject[newPid].add(res.data.message.id);
            toast("消息已排入队列，将在当前 Lead 回合结束后处理", "info");
          }
          if (!STATE.chatMessages.some(m => m && m.id === res.data.message.id)) {
            STATE.chatMessages = STATE.chatMessages.concat([res.data.message]);
          }
          renderChatThread();
          scrollChatToBottom();
        }

        if (isNewDraft) {
          await loadAllWorkspaceData();
        } else {
          await loadActiveProjectLight();
        }
        if (projectSwitchGen === sentGen) {
          scrollChatToBottom();
        }
      } finally {
        STATE.ui.sendingChat = false;
        renderComposer();
      }
    };

    if (isNewDraft && repo && Array.isArray(parsedTestCmd)) {
      openModal(
        "确认仓库自动测试命令",
        `<div class="form-stack">
          <div style="font-size:13px;color:var(--text-secondary);">合并验证阶段将直接按以下参数数组执行测试（不经过 shell）：</div>
          <div class="kv-list">
            <div class="kv-row"><span class="kv-k">仓库</span><span class="kv-v mono">${esc(repo)}</span></div>
            <div class="kv-row"><span class="kv-k">基准分支</span><span class="kv-v mono">${esc(baseBranch)}</span></div>
          </div>
          <div class="inspector-sec-label">待执行参数数组 (exact argv)</div>
          <pre class="log-box">${esc(JSON.stringify(parsedTestCmd, null, 2))}</pre>
        </div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmTestCmd" type="button">确认并发送</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmTestCmd").onclick = async () => {
        closeModal();
        await doSend();
      };
      return;
    }

    await doSend();
  }

  function openManualNewProjectModal() {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    openModal(
      "完整表单新建项目（高级）",
      `<div class="form-stack">
        <div class="form-row"><label class="form-label" for="manPrjTitle">项目标题 (必填)</label><input class="compact-input" id="manPrjTitle" type="text" placeholder="例如：优化登录接口限流" /></div>
        <div class="form-row"><label class="form-label" for="manPrjGoal">项目目标 (必填)</label><textarea class="compact-input" id="manPrjGoal" rows="3" placeholder="描述希望完成的目标..."></textarea></div>
        <div class="form-row"><label class="form-label" for="manPrjCriterion">验收标准 (必填，每行一条或分号分隔)</label><textarea class="compact-input" id="manPrjCriterion" rows="2" placeholder="例如：全部单元测试通过"></textarea></div>
        <div class="form-row-grid">
          <div class="form-row">
            <label class="form-label" for="manPrjRepo">Git 仓库路径 (可选)</label>
            <div style="display:flex;gap:6px;">
              <input class="compact-input mono" id="manPrjRepo" type="text" placeholder="/path/to/repo" style="flex:1;" />
              <button class="btn btn-sm" id="btnManPickRepoFolder" type="button">选择...</button>
            </div>
          </div>
          <div class="form-row"><label class="form-label" for="manPrjBaseBranch">基准分支 (填仓库时必填)</label><input class="compact-input mono" id="manPrjBaseBranch" type="text" value="" placeholder="选择仓库后自动识别" /></div>
        </div>
        <div class="form-row"><label class="form-label" for="manPrjTestCmd">测试命令 (填仓库时必填，JSON 数组或 none)</label><input class="compact-input mono" id="manPrjTestCmd" type="text" placeholder='例如：none 或 ["pytest"]' /></div>
        <div class="form-row-grid">
          <div class="form-row">
            <label class="form-label" for="manPrjGateMode">计划审批门控</label>
            <select class="compact-select" id="manPrjGateMode">
              <option value="require_approval" selected>先出计划，卡片确认后再派发（推荐）</option>
              <option value="off">关闭计划门控（传统直派模式）</option>
            </select>
          </div>
          <div class="form-row">
            <label class="form-label" for="manPrjPermMode">权限模式</label>
            <select class="compact-select" id="manPrjPermMode">
              <option value="supervised">每步问我 (supervised)</option>
              <option value="plan" selected>计划批了就放手 (plan · 默认)</option>
              <option value="auto">全自动 (auto)</option>
            </select>
          </div>
        </div>
      </div>`,
      `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalSaveManualProject" type="button">创建项目</button>`
    );
    $("modalCancel").onclick = closeModal;
    if ($("btnManPickRepoFolder")) {
      $("btnManPickRepoFolder").onclick = () => handlePickRepoFolder({
        repoInputId: "manPrjRepo",
        branchInputId: "manPrjBaseBranch",
        testInputId: "manPrjTestCmd",
        titleInputId: "manPrjTitle"
      });
    }
    let manualProjectSubmitting = false;
    $("modalSaveManualProject").onclick = async () => {
      if (manualProjectSubmitting) return;
      const title = ($("manPrjTitle").value || "").trim();
      const goal = ($("manPrjGoal").value || "").trim();
      const rawCrit = ($("manPrjCriterion").value || "").trim();
      const criteria = rawCrit.split(/[\n;；]+/).map(s => s.trim()).filter(Boolean);
      const repo = ($("manPrjRepo").value || "").trim();
      const baseBranch = ($("manPrjBaseBranch").value || "").trim();
      const rawTestCmd = ($("manPrjTestCmd").value || "").trim();
      const gateMode = ($("manPrjGateMode").value || "require_approval");
      const permMode = ($("manPrjPermMode") && $("manPrjPermMode").value) || "plan";
      if (!title || !goal || !criteria.length) {
        toast("请填写标题、目标和至少一条验收标准", "warn");
        return;
      }
      const check = validateRepoInputs(repo, baseBranch, rawTestCmd);
      if (!check.ok) {
        toast(check.error, "warn");
        return;
      }
      const payload = { title, goal, criterion: criteria, plan_gate_mode: gateMode, permission_mode: permMode };
      const defLead = STATE.config["lead.default_agent"];
      if (defLead) payload.lead = defLead;
      if (repo) {
        payload.repo = repo;
        payload.base_branch = baseBranch;
        payload.test_command = check.parsedTestCmd;
      }
      const saveBtn = $("modalSaveManualProject");
      manualProjectSubmitting = true;
      if (saveBtn) saveBtn.disabled = true;
      try {
        if (permMode === "auto" || gateMode === "off") {
          const warnLines = [];
          if (permMode === "auto") {
            warnLines.push("- 权限模式为「全自动 (auto)」：后续计划派发与超限重试将自动放行（高危提权 permission_escalation 仍需单独授权）。");
          }
          if (gateMode === "off") {
            warnLines.push("- 计划审批门控为「关闭 (off)」：Lead 将跳过计划确认卡片直接派发任务。");
          }
          const okMode = await showConfirmDialog(
            "确认以高自动化模式创建项目？",
            `即将为项目「${title}」启用以下设置：\n${warnLines.join("\n")}\n\n确认继续创建吗？`,
            "确认并继续"
          );
          if (!okMode) return;
        }
        if (repo && Array.isArray(check.parsedTestCmd)) {
          const ok = await showConfirmDialog(
            "确认仓库测试命令",
            `合并阶段将按精确参数数组执行（无 shell）：\n${JSON.stringify(check.parsedTestCmd, null, 2)}`,
            "确认并创建"
          );
          if (!ok) return;
        }
        const res = await apiCall("intake.submit", payload);
        if (!res.ok) { toast(formatErrorMessage(res.error), "err"); return; }
        closeModal();
        const newId = res.data && (res.data.id || res.data.project_id);
        bumpProjectSwitchGen();
        STATE.ui.draftNewChat = false;
        if (newId) STATE.activeProjectId = newId;
        toast("项目创建成功", "ok");
        await loadAllWorkspaceData();
      } finally {
        manualProjectSubmitting = false;
        if (saveBtn) saveBtn.disabled = false;
      }
    };
  }

  function showConfirmDialog(title, message, confirmLabel = "确认") {
    return new Promise(resolve => {
      closeConfirmModal(false);
      confirmModalResolver = resolve;
      $("confirmModalTitle").textContent = title;
      $("confirmModalBody").innerHTML = `<pre class="log-box" style="white-space:pre-wrap;">${esc(message)}</pre>`;
      $("confirmModalFoot").innerHTML = `<button class="btn" id="btnConfirmNo" type="button">取消</button><button class="btn btn-primary" id="btnConfirmYes" type="button">${esc(confirmLabel)}</button>`;
      $("confirmModalBackdrop").classList.remove("hidden");
      $("btnConfirmNo").onclick = () => closeConfirmModal(false);
      $("btnConfirmYes").onclick = () => closeConfirmModal(true);
    });
  }

  function promptOptionalRejectReason(title, context = null) {
    const ctx = (context && typeof context === "object") ? context : null;
    const targetCat = ctx && ctx.categoryLabel ? String(ctx.categoryLabel) : "";
    const targetSummary = ctx && ctx.summary ? String(ctx.summary) : "";
    const targetId = ctx && ctx.approvalId ? String(ctx.approvalId) : "";
    const targetBlock = (targetCat || targetSummary || targetId)
      ? `<div class="inspector-note-box" data-reject-target="1">
          ${targetCat ? `<div style="font-size:12px;font-weight:600;color:var(--text-primary);">${esc(targetCat)}${targetId ? ` <span class="mono" style="font-weight:400;color:var(--text-muted);">(${esc(targetId)})</span>` : ""}</div>` : (targetId ? `<div class="mono" style="font-size:12px;color:var(--text-muted);">${esc(targetId)}</div>` : "")}
          ${targetSummary ? `<div style="font-size:12.5px;color:var(--text-secondary);margin-top:4px;word-break:break-word;">${esc(targetSummary)}</div>` : ""}
        </div>`
      : "";
    return new Promise(resolve => {
      openModal(
        title || "拒绝该审批请求",
        `<div class="form-stack">
          ${targetBlock}
          <div style="font-size:13px;color:var(--text-secondary);">可填写拒绝原因或调整建议（留空将直接拒绝）：</div>
          <div class="form-row">
            <label class="form-label" for="modalRejectReasonInput">拒绝原因（可选）</label>
            <input class="compact-input" id="modalRejectReasonInput" type="text" placeholder="例如：请先补充第 2 步的单元测试..." />
          </div>
        </div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-danger-text" id="modalConfirmReject" type="button">确认拒绝</button>`
      );
      modalPromptResolver = resolve;
      const cancelBtn = $("modalCancel");
      const confirmBtn = $("modalConfirmReject");
      if (cancelBtn) {
        cancelBtn.onclick = () => { closeModal(); };
      }
      if (confirmBtn) {
        confirmBtn.onclick = () => {
          const val = ($("modalRejectReasonInput") && $("modalRejectReasonInput").value.trim()) || "";
          settleModalPrompt(val);
          closeModal();
        };
      }
      if ($("modalRejectReasonInput") && typeof $("modalRejectReasonInput").focus === "function") {
        $("modalRejectReasonInput").focus();
      }
    });
  }

  function formatApprovalDecisionToast(action, data, { category = "" } = {}) {
    const d = data && typeof data === "object" ? data : {};
    const upgradedFromRelayed = d.upgraded_from === "relayed";
    const upgradedToAlways = Boolean(d.upgraded_to_always_allow);
    const alreadyDecided = Boolean(d.already_decided);
    const cancelledSteps = Array.isArray(d.cancelled_step_approvals) ? d.cancelled_step_approvals : [];
    const cancelledPlans = Array.isArray(d.cancelled_plan_approvals) ? d.cancelled_plan_approvals : [];
    const allSwept = cancelledSteps.concat(cancelledPlans);
    const alreadyAuto = Boolean(d.already_auto === true || (d.permission_mode === "auto" && d.mode_changed === false));
    const switchedMode = Boolean(
      d.mode_changed === true ||
      (d.mode_changed === undefined && d.permission_mode === "auto" && !d.already_auto)
    );
    const createdGrant = Boolean(d.grant_created || d.grant || (Array.isArray(d.grants) && d.grants.length));
    const sweptSuffix = formatSweptApprovalsSuffix(cancelledSteps, cancelledPlans);

    let text = "";
    let level = "ok";
    if (action === "reject") {
      if (upgradedFromRelayed) {
        level = "ok";
        text = "已将该转发拒绝升级为本机直连确认 (direct_verified)";
      } else {
        level = "info";
        text = alreadyDecided
          ? "该审批请求此前已拒绝，状态已同步"
          : (category === "plan_dispatch" ? "已拒绝该计划" : "已拒绝该审批请求");
      }
    } else if (alreadyAuto && !switchedMode) {
      if (typeof d.message === "string" && d.message.trim()) {
        text = `${d.message.trim()}${sweptSuffix}`;
      } else if (upgradedFromRelayed) {
        text = `已将转发审批升级为本机直连确认；当前项目已处于「全自动 (auto)」模式，无需重复切换${sweptSuffix}`;
      } else if (upgradedToAlways || alreadyDecided) {
        text = `当前操作已批准，且项目已处于「全自动 (auto)」模式，无需重复切换${sweptSuffix}`;
      } else {
        text = `已允许当前操作；当前项目已处于「全自动 (auto)」模式，无需重复切换${sweptSuffix}`;
      }
      if (alreadyDecided && !upgradedFromRelayed) level = "info";
    } else if (upgradedFromRelayed && switchedMode) {
      text = `已将转发审批升级为本机直连确认，并切换本项目为「全自动 (auto)」模式${sweptSuffix}`;
    } else if (upgradedFromRelayed && createdGrant) {
      text = "已将转发审批升级为本机直连确认，并创建窄范围永久授权（可在「设置 → 永久授权」随时撤销）";
    } else if (upgradedFromRelayed) {
      text = "已将该转发审批升级为本机直连确认 (direct_verified)";
    } else if (upgradedToAlways && switchedMode) {
      text = `原审批保持通过，已切换本项目为「全自动 (auto)」模式${sweptSuffix}`;
    } else if (upgradedToAlways && createdGrant) {
      text = "原审批保持通过，已追加创建窄范围永久授权（可在「设置 → 永久授权」随时撤销）";
    } else if (switchedMode) {
      text = `已允许并切换本项目为「全自动 (auto)」模式${sweptSuffix}`;
    } else if (createdGrant) {
      text = "已允许并创建窄范围永久授权（可在「设置 → 永久授权」随时撤销）";
    } else if (alreadyDecided) {
      level = "info";
      text = "该审批请求此前已允许，状态已同步";
    } else {
      text = category === "plan_dispatch" ? "已允许计划，正在派发任务" : "已允许该审批请求";
    }

    return {
      text,
      level,
      switchedMode,
      alreadyAuto,
      createdGrant,
      upgradedFromRelayed,
      upgradedToAlways,
      alreadyDecided,
      allSwept
    };
  }

  function buildAutoModeConfirmCopy(confirmData) {
    const d = (confirmData && typeof confirmData === "object") ? confirmData : {};
    const targetPid = typeof d.project_id === "string" ? d.project_id : "";
    const matchedPrj = targetPid
      ? (STATE.projects.find(p => p && p.id === targetPid) ||
        (STATE.activeProjectDetail && STATE.activeProjectDetail.id === targetPid ? STATE.activeProjectDetail : null))
      : null;
    const rawName = (typeof d.project_name === "string" && d.project_name.trim())
      ? d.project_name.trim()
      : ((matchedPrj && matchedPrj.name) || "");
    const projectLabel = rawName
      ? (targetPid && rawName !== targetPid ? `${rawName} (${targetPid})` : rawName)
      : (targetPid || "目标项目");
    const title = `确认将项目「${projectLabel}」切换为「全自动 (auto)」模式？`;
    const body =
      `目标项目：${projectLabel}\n\n` +
      "确认后将：\n" +
      "1. 立即批准该审批卡片；\n" +
      `2. 将项目「${projectLabel}」的权限模式切换为「全自动 (auto)」；\n` +
      "3. 自动取消该项目待处理的单步/计划审批卡片（未派发计划本身保持可派发状态），后续计划派发与超限重试将自动放行，但高危提权 (permission_escalation) 仍需你通过卡片单独确认。\n\n" +
      "随时可在项目顶栏或设置中切回「计划批了就放手 (plan)」或「每步问我 (supervised)」。";
    return {
      projectId: targetPid,
      projectLabel,
      title,
      body,
      confirmLabel: "确认切换为全自动并允许"
    };
  }

  async function handleApprovalDecision({
    approvalId,
    action,
    category = "",
    planHash = "",
    planId = "",
    planVersion = "",
    opHash = "",
    reason = "",
    promptReason = false,
    surface = ""
  }) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    if (!approvalId || !["allow", "always_allow", "reject"].includes(action)) return;
    if (STATE.ui.decidingApprovalIds.has(approvalId)) return;

    const hydratedPre = resolveHydratedApproval(approvalId) || {};
    const preCategory = category || hydratedPre.category || "";
    const clickGuard = checkApprovalClickGuard({
      surface,
      approvalId,
      action,
      category: preCategory,
      planHash,
      opHash
    });
    if (!clickGuard.ok) {
      toast(clickGuard.message || "审批卡片状态已变化，请核对后重试", clickGuard.reason === "layout_shift_guard" ? "warn" : "err");
      return;
    }

    // Acquire duplicate-click guard BEFORE awaiting optional reject-reason modal
    STATE.ui.decidingApprovalIds.add(approvalId);
    renderChatThread();
    renderComposer();
    if (!STATE.ui.inspectorCollapsed) renderInspector();

    let switchedMode = false;
    let createdGrant = false;
    try {
      const hydrated = resolveHydratedApproval(approvalId) || {};
      const effCategory = category || hydrated.category || "";
      const matchedPlan = effCategory === "plan_dispatch"
        ? (STATE.plans.find(p => p && (p.id === planId || p.approval_id === approvalId)) || STATE.chatPlansById[planId] || null)
        : null;

      let finalReason = typeof reason === "string" ? reason.trim() : "";
      if (action === "reject" && !finalReason && promptReason) {
        const rejectTargetSummary = matchedPlan
          ? `计划 v${matchedPlan.version || 1}：${matchedPlan.title || "执行计划"}`
          : (hydrated.summary || formatApprovalOpName(hydrated));
        const prompted = await promptOptionalRejectReason(
          effCategory === "plan_dispatch" ? "拒绝执行计划" : "拒绝审批请求",
          {
            approvalId,
            categoryLabel: GATE_CATEGORY_LABELS[effCategory] || effCategory || "审批请求",
            summary: rejectTargetSummary
          }
        );
        if (prompted === null) return;
        finalReason = prompted;
      }

      const payload = {
        approval_id: approvalId,
        action
      };
      // Strictly bind to the button's data-* hashes rendered at click time — never borrow mutable state
      const effPlanHash = typeof planHash === "string" ? planHash.trim() : "";
      const effPlanId = typeof planId === "string" ? planId.trim() : "";
      const effPlanVer = planVersion !== "" && planVersion !== null && planVersion !== undefined
        ? Number(planVersion)
        : NaN;
      const effOpHash = typeof opHash === "string" ? opHash.trim() : "";

      if (effCategory === "plan_dispatch") {
        if (!effPlanHash) {
          toast("审批按钮缺少计划校验摘要 (plan_hash)，请刷新后重试", "err");
          return;
        }
        payload.plan_hash = effPlanHash;
        if (effPlanId) payload.plan_id = effPlanId;
        if (Number.isInteger(effPlanVer)) payload.plan_version = effPlanVer;
        if (effOpHash) payload.op_hash = effOpHash;
      } else {
        if (!effOpHash) {
          toast("审批按钮缺少审批校验摘要 (op_hash)，请刷新后重试", "err");
          return;
        }
        payload.op_hash = effOpHash;
      }
      if (finalReason) {
        payload.reason = finalReason;
      }

      const refreshOnTerminalError = async errObj => {
        const code = errObj && errObj.code;
        const details = (errObj && errObj.details && typeof errObj.details === "object") ? errObj.details : {};
        if (code === "INVALID_TRANSITION" && details.reason === "task_terminal") {
          markApprovalsSuppressedLocally([approvalId], {
            reason: "task_terminal",
            taskStatus: details.task_status || "cancelled",
            taskTerminal: true
          });
          await loadActiveProjectLight();
          return;
        }
        if (code === "INVALID_TRANSITION" && details.reason === "task_progressed") {
          markApprovalsSuppressedLocally([approvalId], {
            reason: "task_progressed",
            taskStatus: details.task_status || null,
            taskProgressed: true
          });
          await loadActiveProjectLight();
          return;
        }
        if (code === "PROJECT_NOT_ACTIVE") {
          markApprovalsSuppressedLocally([approvalId], {
            reason: "project_not_active",
            inactiveProject: true
          });
          await loadAllWorkspaceData();
          return;
        }
        if (["APPROVAL_EXPIRED", "APPROVAL_CONSUMED", "APPROVAL_CANCELLED", "APPROVAL_REJECTED", "DECISION_CONFLICT", "OP_HASH_MISMATCH"].includes(code)) {
          if (code === "APPROVAL_EXPIRED") markApprovalsTerminalLocally([approvalId], "expired");
          else if (code === "APPROVAL_CANCELLED") markApprovalsTerminalLocally([approvalId], "cancelled");
          else if (code === "APPROVAL_CONSUMED") markApprovalsTerminalLocally([approvalId], "consumed");
          else if (code === "APPROVAL_REJECTED") markApprovalsTerminalLocally([approvalId], "rejected");
          await loadActiveProjectLight();
        }
      };

      let res = await apiCall("approval.decide", payload);
      if (!res.ok) {
        toast(formatErrorMessage(res.error), "err");
        await refreshOnTerminalError(res.error);
        return;
      }

      // Two-step confirmation for always_allow on plan_dispatch / step_dispatch -> switches target project to 'auto'
      if (res.data && res.data.confirmation_required === true) {
        const copy = buildAutoModeConfirmCopy(res.data);
        const confirmed = await showConfirmDialog(
          copy.title,
          copy.body,
          copy.confirmLabel
        );
        if (!confirmed) {
          toast("已取消切换为全自动模式", "info");
          return;
        }
        res = await apiCall("approval.decide", Object.assign({}, payload, { confirm_auto_mode: true }));
        if (!res.ok) {
          toast(formatErrorMessage(res.error), "err");
          await refreshOnTerminalError(res.error);
          return;
        }
      }

      const d = res.data || {};
      bumpSidebarLoadSeq();
      const outcome = formatApprovalDecisionToast(action, d, { category: effCategory });
      switchedMode = outcome.switchedMode;
      createdGrant = outcome.createdGrant;
      if (outcome.allSwept.length) {
        markApprovalsTerminalLocally(outcome.allSwept, "cancelled");
      }
      rememberRecentlyResolvedApproval(
        Object.assign({ id: approvalId, category: effCategory }, hydrated),
        {
          plan: matchedPlan,
          action,
          label: action === "reject" ? "已拒绝" : "已允许",
          projectId: hydrated.project_id || (matchedPlan && matchedPlan.project_id) || STATE.activeProjectId || null
        }
      );
      toast(outcome.text, outcome.level);

      if (switchedMode || outcome.alreadyAuto || createdGrant || outcome.upgradedToAlways || outcome.allSwept.length) {
        await loadAllWorkspaceData();
      } else {
        await loadActiveProjectLight();
      }
    } finally {
      STATE.ui.decidingApprovalIds.delete(approvalId);
      renderAll();
    }
  }

  async function handleChangeProjectPermissionMode(projectId, nextMode) {
    if (!canMutate()) { toast(disabledReason(), "err"); renderAll(); return; }
    const prj = STATE.projects.find(p => p && p.id === projectId) || getActiveProject();
    if (!prj || !["supervised", "plan", "auto"].includes(nextMode)) return;
    const prevMode = prj.permission_mode || STATE.chatPermissionMode || "plan";
    if (prevMode === nextMode) return;

    if (nextMode === "auto") {
      const ok = await showConfirmDialog(
        "确认切换项目权限模式为「全自动 (auto)」？",
        `将项目「${prj.name || prj.id}」切换为「全自动 (auto)」后：\n` +
        "- 现有待处理的计划/单步审批卡片将自动取消（未派发计划本身保持可派发状态）；\n" +
        "- 后续计划派发无需等待计划卡片确认，Lead 将直接派发执行；\n" +
        "- 超限重试 (retry_over_limit) 将自动放行；\n" +
        "- 高危提权 (permission_escalation) 仍需你在审批卡片上单独授权。",
        "确认切换为全自动"
      );
      if (!ok) {
        renderAll();
        return;
      }
    }

    const res = await apiCall("project.set_permission_mode", {
      project_id: prj.id,
      permission_mode: nextMode,
      reason: `桌面端将权限模式切换为 ${nextMode}`
    });
    if (!res.ok) {
      toast(formatErrorMessage(res.error), "err");
      renderAll();
      return;
    }
    const cancelledSteps = Array.isArray(res.data && res.data.cancelled_step_approvals)
      ? res.data.cancelled_step_approvals
      : [];
    const cancelledPlans = Array.isArray(res.data && res.data.cancelled_plan_approvals)
      ? res.data.cancelled_plan_approvals
      : [];
    const allSwept = cancelledSteps.concat(cancelledPlans);
    if (allSwept.length) {
      markApprovalsTerminalLocally(allSwept, "cancelled");
    }
    STATE.chatPermissionMode = nextMode;
    const meta = PERMISSION_MODE_META[nextMode] || PERMISSION_MODE_META.plan;
    const sweptSuffix = formatSweptApprovalsSuffix(cancelledSteps, cancelledPlans);
    toast(`项目权限模式已切换为「${meta.label}」${sweptSuffix}`, "ok");
    await loadAllWorkspaceData();
  }

  async function handleQuickSwitchAgentModel(agentName, model) {
    closeAllPopovers();
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const ag = getAgentByName(agentName);
    if (!ag || !model) return;
    const det = STATE.agentDetails[agentName] || ag;
    const allowed = Array.isArray(det.allowed_models) ? det.allowed_models : [];
    if (allowed.length && !allowed.includes(model)) {
      toast(`模型 ${model} 不在 ${agentName} 允许的模型列表 (allowed_models) 中`, "err");
      return;
    }
    const payload = { name: agentName, default_model: model };
    if (typeof ag.version === "number") payload.if_version = ag.version;
    const res = await apiCall("agent.update", payload);
    if (!res.ok) {
      toast(formatErrorMessage(res.error), "err");
      return;
    }
    delete STATE.agentDetails[agentName];
    toast(`已将 ${agentName} 默认模型切换为 ${model}（下回合生效）`, "ok");
    await loadAllWorkspaceData();
  }

  async function handleQuickSwitchAgentEffort(agentName, effort) {
    closeAllPopovers();
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const ag = getAgentByName(agentName);
    if (!ag || !effort) return;
    const det = STATE.agentDetails[agentName] || ag;
    const allowed = Array.isArray(det.allowed_efforts) ? det.allowed_efforts : [];
    if (allowed.length && !allowed.includes(effort)) {
      toast(`推理强度 ${effort} 不在 ${agentName} 允许的列表 (allowed_efforts) 中`, "err");
      return;
    }
    const payload = { name: agentName, default_effort: effort };
    if (typeof ag.version === "number") payload.if_version = ag.version;
    const res = await apiCall("agent.update", payload);
    if (!res.ok) {
      toast(formatErrorMessage(res.error), "err");
      return;
    }
    delete STATE.agentDetails[agentName];
    toast(`已将 ${agentName} 推理强度切换为 ${effort}（下回合生效）`, "ok");
    await loadAllWorkspaceData();
  }

  async function handleRevokeGrant(grantId) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    if (!grantId) return;
    const res = await apiCall("grant.revoke", {
      grant_id: grantId,
      reason: "用户在桌面端设置中撤销永久授权"
    });
    if (!res.ok) {
      toast(formatErrorMessage(res.error), "err");
      return;
    }
    toast("已撤销该永久授权", "ok");
    await loadGrants();
    renderSettingsView();
  }

  function jumpToApprovalCard(approvalId) {
    if (!approvalId) return;
    const escaped = (window.CSS && typeof window.CSS.escape === "function") ? window.CSS.escape(approvalId) : approvalId;
    const card = document.querySelector(`[data-card-approval-id="${escaped}"]`);
    if (!card) {
      toast("对应审批卡片不在当前可见历史中，请向上滚动加载更早记录", "info");
      return;
    }
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("approval-card-highlight");
    window.setTimeout(() => card.classList.remove("approval-card-highlight"), 1600);
  }

  async function handleSelectLead(agentName) {
    closeAllPopovers();
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const prj = getActiveProject();
    if (!prj || STATE.ui.draftNewChat) {
      STATE.ui.selectedLeadDraft = agentName;
      renderCenterHeaderAndViews();
      renderComposer();
      return;
    }
    if (prj.lead === agentName) return;

    openModal(
      `切换项目 Lead 为 ${agentName}`,
      `<div class="form-stack">
        <div style="font-size:13px;color:var(--text-secondary);">将当前项目的 Lead 从 <span class="mono">${esc(prj.lead || "--")}</span> 切换为 <span class="mono">${esc(agentName)}</span>。</div>
        <div class="form-row"><label class="form-label">切换原因 (将记入审计记录)</label><input class="compact-input" id="modalLeadReason" type="text" value="桌面端切换项目 Lead" /></div>
      </div>`,
      `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmSetLead" type="button">确认切换</button>`
    );
    $("modalCancel").onclick = closeModal;
    $("modalConfirmSetLead").onclick = async () => {
      if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
      const reason = ($("modalLeadReason").value || "").trim() || "桌面端切换项目 Lead";
      const res = await apiCall("project.set_lead", { project_id: prj.id, agent: agentName, reason });
      closeModal();
      if (!res.ok) { toast(formatErrorMessage(res.error), "err"); return; }
      toast(`已切换 Lead 为 ${agentName}`, "ok");
      await loadAllWorkspaceData();
    };
  }

  async function handleTaskOperation(op) {
    const t = getActiveTask();
    if (!t || !canMutate()) return;

    if (op === "accept") {
      const res = await apiCall("task.accept", { task_id: t.id });
      if (res.ok) { toast("任务已验收通过", "ok"); await loadAllWorkspaceData(); }
      else toast(formatErrorMessage(res.error), "err");
      return;
    }

    if (op === "merge") {
      const res = await apiCall("task.merge", { task_id: t.id });
      if (res.ok) { toast("任务分支合并成功", "ok"); await loadAllWorkspaceData(); }
      else toast(formatErrorMessage(res.error), "err");
      return;
    }

    if (op === "retry") {
      const res = await apiCall("task.retry", { task_id: t.id, reason: "桌面端触发重试" });
      if (res.ok) { toast("任务已重新排队", "ok"); await loadAllWorkspaceData(); }
      else toast(formatErrorMessage(res.error), "err");
      return;
    }

    if (op === "reject_task" || op === "cancel" || op === "escalate") {
      const titleMap = { reject_task: "打回返工", cancel: "取消任务", escalate: "升级异常" };
      const rpcMap = { reject_task: "task.reject", cancel: "task.cancel", escalate: "task.escalate" };
      const defReason = op === "cancel" ? "用户在桌面端取消任务" : (op === "escalate" ? "用户在桌面端升级异常" : "");
      openModal(
        `${titleMap[op]} · ${t.title || t.id}`,
        `<div class="form-row"><label class="form-label">说明原因</label><textarea class="compact-input" id="modalReasonInput" rows="3" placeholder="说明原因或返工要点...">${esc(defReason)}</textarea></div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmReason" type="button">确认提交</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmReason").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const reason = ($("modalReasonInput").value || "").trim();
        if (!reason) { toast("请填写原因", "warn"); return; }
        const res = await apiCall(rpcMap[op], { task_id: t.id, reason });
        closeModal();
        if (res.ok) { toast("操作已执行", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
      return;
    }

    if (op === "close") {
      const mergedPeers = STATE.tasks.filter(x => x.project_id === t.project_id && x.status === "merged" && x.id !== t.id);
      const peerOpts = mergedPeers.map(x => `<option value="${esc(x.id)}">${esc(x.title || x.id)} (${esc(x.id)})</option>`).join("");
      openModal(
        `替代关闭任务`,
        `<div class="form-stack">
          <div style="font-size:12.5px;color:var(--text-secondary);">将此代码任务标记为由另一个已合并任务覆盖关闭。</div>
          ${mergedPeers.length
            ? `<div class="form-row"><label class="form-label">已合并替代任务</label><select class="compact-select" id="modalCloseSupSelect">${peerOpts}</select></div>`
            : `<div class="form-row"><label class="form-label">已合并替代任务 ID</label><input class="compact-input mono" id="modalCloseSupInput" type="text" placeholder="输入同项目已合并任务 tsk_..." /></div>`}
          <div class="form-row"><label class="form-label">备注 (可选)</label><input class="compact-input" id="modalCloseReason" type="text" placeholder="可选说明..." /></div>
        </div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmClose" type="button">确认关闭</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmClose").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const supEl = $("modalCloseSupSelect") || $("modalCloseSupInput");
        const supersededBy = supEl ? (supEl.value || "").trim() : "";
        if (!supersededBy) { toast("请指定替代任务 ID", "warn"); return; }
        const reason = ($("modalCloseReason") && $("modalCloseReason").value.trim()) || "";
        const payload = { task_id: t.id, superseded_by: supersededBy };
        if (reason) payload.reason = reason;
        const res = await apiCall("task.close", payload);
        closeModal();
        if (res.ok) { toast("任务已替代关闭", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
      return;
    }

    if (op === "release_run") {
      const openRun = STATE.runs.find(r => r.task_id === t.id && (r.status === "starting" || r.status === "running"));
      if (!openRun) { toast("未找到运行中的进程", "warn"); return; }
      const flagName = openRun.kind === "merge_test" ? "force_merge_held" : "force_seen_unusable";
      openModal(
        `释放卡住的运行锁`,
        `<div style="font-size:13px;color:var(--text-secondary);">将对 <span class="mono">${esc(openRun.id)}</span> 执行强制释放租约并标记为 lost，请确认对应进程已退出。</div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmRelease" type="button">确认释放</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmRelease").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const payload = { run_id: openRun.id };
        payload[flagName] = true;
        const res = await apiCall("run.release", payload);
        closeModal();
        if (res.ok) { toast("运行锁已释放", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
      return;
    }

    if (op === "assign") {
      const prj = getActiveProject();
      const isInitialAssign = t.status === "created";
      const workerOpts = STATE.agents.filter(a => !a.disabled).map(a => {
        const isLead = prj && prj.lead === a.name;
        return `<option value="${esc(a.name)}" ${t.assignee === a.name ? "selected" : ""} ${isLead ? "disabled" : ""}>${esc(a.name)} (${esc(a.adapter)})${isLead ? " [当前 Lead 不可指派]" : ""}</option>`;
      }).join("");
      openModal(
        `${isInitialAssign ? "指派执行者" : "改派执行者"}`,
        `<div class="form-stack">
          <div class="form-row"><label class="form-label">目标 Worker</label><select class="compact-select" id="modalAssignTo">${workerOpts}</select></div>
          ${!isInitialAssign ? `<div class="form-row"><label class="form-label">改派原因</label><input class="compact-input" id="modalAssignReason" type="text" value="桌面端人工改派" /></div>` : ""}
        </div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmAssign" type="button">确认</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmAssign").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const to = $("modalAssignTo").value;
        if (!to) { toast("请选择目标 Worker", "warn"); return; }
        let res;
        if (isInitialAssign) {
          res = await apiCall("task.assign", { task_id: t.id, to });
        } else {
          const reason = ($("modalAssignReason").value || "").trim() || "桌面端人工改派";
          res = await apiCall("task.reassign", { task_id: t.id, to, reason });
        }
        closeModal();
        if (res.ok) { toast("指派完成", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
    }
  }

  async function handleProjectAction(op) {
    closeAllPopovers();
    const prj = getActiveProject();
    if (!prj) return;

    if (op === "leads") {
      let items = Array.isArray(prj.lead_history) ? prj.lead_history : [];
      if (STATE.connected && STATE.auth.auth_verified) {
        const res = await apiCall("project.leads", { project_id: prj.id });
        if (res.ok && res.data && Array.isArray(res.data.items)) items = res.data.items;
      }
      const rowsHtml = items.length ? `
        <table class="clean-table">
          <thead><tr><th>Lead</th><th>来源</th><th>原因</th><th>开始</th><th>结束</th></tr></thead>
          <tbody>${items.map(it => `<tr><td class="mono">${esc(it.agent || "--")}</td><td class="mono">${esc(it.via || "--")}</td><td>${esc(it.reason || "--")}</td><td class="mono">${esc(formatDateTime(it.started_at))}</td><td class="mono">${it.ended_at ? esc(formatDateTime(it.ended_at)) : "当前在任"}</td></tr>`).join("")}</tbody>
        </table>` : `<div class="empty-state">暂无 Lead 交接记录</div>`;
      openModal(`Lead 交接历史`, rowsHtml, `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
      return;
    }

    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    if (op === "new_task") { openCreateTaskModal(); return; }

    if (op === "pause" || op === "resume") {
      const res = await apiCall(`project.${op}`, { project_id: prj.id });
      if (res.ok) { toast(op === "pause" ? "项目已暂停" : "项目已恢复", "ok"); await loadAllWorkspaceData(); }
      else toast(formatErrorMessage(res.error), "err");
      return;
    }

    if (op === "cancel" || op === "deliver") {
      const isCancel = op === "cancel";
      const desc = isCancel
        ? "取消项目将终止未完成任务与活跃 Lead 回合，并将项目标记为已取消。确定继续吗？"
        : `将集成分支 ${prj.integration_branch || "--"} 本地快进合并 (fast-forward) 至基准分支 ${prj.base_branch || "--"}（不会 push 到远端）。确定交付吗？`;
      openModal(
        isCancel ? "确认取消项目" : "确认交付到本地分支",
        `<div style="font-size:13px;color:var(--text-secondary);line-height:1.6;">${esc(desc)}</div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn ${isCancel ? "btn-danger-text" : "btn-primary"}" id="modalConfirmPrjDanger" type="button">${isCancel ? "确认取消项目" : "确认交付"}</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmPrjDanger").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const res = await apiCall(`project.${op}`, { project_id: prj.id });
        closeModal();
        if (res.ok) { toast(isCancel ? "项目已取消" : "已成功交付到本地分支", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
      return;
    }

    if (op === "update") {
      openModal(
        "更新项目目标与验收标准",
        `<div class="form-stack">
          <div class="form-row"><label class="form-label">项目目标</label><textarea class="compact-input" id="modalUpdGoal" rows="3">${esc(prj.goal || "")}</textarea></div>
          <div class="form-row"><label class="form-label">追加验收标准（可选，每行一条或分号分隔）</label><textarea class="compact-input" id="modalUpdCriteria" rows="2" placeholder="输入新的验收标准..."></textarea></div>
        </div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmUpd" type="button">保存</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmUpd").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const goal = ($("modalUpdGoal").value || "").trim();
        const rawCriteria = ($("modalUpdCriteria") && $("modalUpdCriteria").value) || "";
        const criteria = rawCriteria.split(/[\n;；]+/).map(s => s.trim()).filter(Boolean);
        if (!goal && !criteria.length) { toast("目标与新增验收标准不能同时为空", "warn"); return; }
        const payload = { project_id: prj.id };
        if (goal && (goal !== (prj.goal || "").trim() || !criteria.length)) payload.goal = goal;
        if (criteria.length) payload.criterion = criteria;
        const res = await apiCall("project.update", payload);
        closeModal();
        if (res.ok) { toast("项目已更新", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
      return;
    }

    if (op === "complete") {
      const criteria = prj.criteria || [];
      const rows = criteria.map((cr, idx) => `
        <div class="form-row">
          <label class="form-label">${esc(cr.text)}</label>
          <input class="compact-input" data-crit-id="${esc(cr.id)}" type="text" value="桌面端确认已达成 #${idx + 1}" />
        </div>`).join("");
      openModal(
        "提交项目完成报告",
        `<div class="form-stack">${rows || "<div>确认完成该项目</div>"}</div>`,
        `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalConfirmComplete" type="button">提交完成</button>`
      );
      $("modalCancel").onclick = closeModal;
      $("modalConfirmComplete").onclick = async () => {
        if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
        const report = {
          criteria: Array.from(document.querySelectorAll("[data-crit-id]")).map(inp => ({
            id: inp.getAttribute("data-crit-id"),
            met: true,
            evidence: (inp.value || "").trim() || "confirmed"
          }))
        };
        const res = await apiCall("project.complete", { project_id: prj.id, report });
        closeModal();
        if (res.ok) { toast("项目已标记完成", "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
      };
    }
  }

  // =========================================================================
  // S3 Adapter Profile & Model Preset Guardrails + Agent/Task Modals
  // =========================================================================
  function getAdapterSupportedProfiles(aid) {
    const det = STATE.adapterDetails[aid];
    if (det && Array.isArray(det.supported_profiles)) return det.supported_profiles.slice();
    const ad = STATE.adapters.find(a => a.id === aid);
    if (ad && Array.isArray(ad.supported_profiles)) return ad.supported_profiles.slice();
    if (Object.prototype.hasOwnProperty.call(FALLBACK_SUPPORTED_PROFILES, aid)) {
      return FALLBACK_SUPPORTED_PROFILES[aid].slice();
    }
    return [];
  }

  function getAdapterModelPresets(aid) {
    const det = STATE.adapterDetails[aid];
    if (det && det.model_presets && typeof det.model_presets === "object") return det.model_presets;
    const ad = STATE.adapters.find(a => a.id === aid);
    if (ad && ad.model_presets && typeof ad.model_presets === "object") return ad.model_presets;
    return null;
  }

  function getAdapterProfileFlags(aid, profile) {
    const det = STATE.adapterDetails[aid];
    if (det && det.profile_flags && Array.isArray(det.profile_flags[profile])) return det.profile_flags[profile].slice();
    const ad = STATE.adapters.find(a => a.id === aid);
    if (ad && ad.profile_flags && Array.isArray(ad.profile_flags[profile])) return ad.profile_flags[profile].slice();
    const fb = FALLBACK_HIGH_PROFILE_FLAGS[aid];
    if (fb && Array.isArray(fb[profile])) return fb[profile].slice();
    return [];
  }

  function isUnavailableAdapter(a) {
    if (!a) return false;
    if (UNAVAILABLE_ADAPTER_IDS.has(a.id)) return true;
    if (a.launch_reason === "launch_held") return true;
    return getAdapterSupportedProfiles(a.id).length === 0;
  }

  function isProfileAboveMaxUnapproved(profile, maxUnapproved) {
    const pIdx = PROFILE_ORDER.indexOf(profile);
    const rawIdx = PROFILE_ORDER.indexOf(maxUnapproved || "dev");
    const mIdx = rawIdx >= 0 ? rawIdx : PROFILE_ORDER.indexOf("dev");
    return pIdx >= 0 && mIdx >= 0 && pIdx > mIdx;
  }

  function confirmHighProfileSelection(aid, profile, maxUnapproved) {
    const maxProf = PROFILE_ORDER.includes(maxUnapproved) ? maxUnapproved : "dev";
    const flags = getAdapterProfileFlags(aid, profile);
    const flagsDisplay = flags.length ? flags.join(" ") : `(adapter=${aid}, profile=${profile})`;
    return new Promise(resolve => {
      closeConfirmModal(false);
      confirmModalResolver = resolve;
      $("confirmModalTitle").textContent = `确认高权限默认档位 · ${profile}`;
      $("confirmModalBody").innerHTML = `
        <div class="form-stack">
          <div class="inspector-note-box red">
            你正在将 Agent 的默认权限档位 <span class="mono">default_profile</span> 设为 <span class="mono">${esc(profile)}</span>，高于 <span class="mono">max_unapproved_profile=${esc(maxProf)}</span>。
          </div>
          <div style="font-size:12.5px;color:var(--text-secondary);line-height:1.55;">
            <strong>重要说明：</strong>仅 Lead 发起的任务会触发审批门控；<strong>你通过桌面端或 CLI 直接派发的任务不会再次确认</strong>，会直接使用以下厂商 CLI 参数运行：
          </div>
          <div class="kv-list">
            <div class="kv-row"><span class="kv-k">Adapter</span><span class="kv-v mono">${esc(aid)}</span></div>
            <div class="kv-row"><span class="kv-k">default_profile</span><span class="kv-v mono">${esc(profile)}</span></div>
            <div class="kv-row"><span class="kv-k">max_unapproved_profile</span><span class="kv-v mono">${esc(maxProf)}</span></div>
          </div>
          <div class="inspector-sec-label">厂商 CLI 权限参数 (vendor flag)</div>
          <pre class="log-box">${esc(flagsDisplay)}</pre>
        </div>`;
      $("confirmModalFoot").innerHTML = `
        <button class="btn" id="confirmModalCancel" type="button">取消</button>
        <button class="btn btn-danger-text" id="confirmModalOk" type="button" disabled>确认使用 ${esc(profile)}</button>`;
      $("confirmModalBackdrop").classList.remove("hidden");
      const cancelBtn = $("confirmModalCancel");
      const okBtn = $("confirmModalOk");
      const readyAt = Date.now() + 400;
      cancelBtn.onclick = () => closeConfirmModal(false);
      okBtn.onclick = () => {
        if (Date.now() < readyAt) return;
        closeConfirmModal(true);
      };
      if (typeof cancelBtn.focus === "function") {
        cancelBtn.focus();
      }
      confirmModalGuardTimer = setTimeout(() => {
        confirmModalGuardTimer = null;
        if (okBtn && okBtn.isConnected !== false) {
          okBtn.disabled = false;
        }
      }, 400);
    });
  }

  function getAdapterDefaultSafeProfile(aid) {
    const supported = getAdapterSupportedProfiles(aid);
    for (const p of SAFE_PROFILE_ORDER) {
      if (supported.includes(p)) return p;
    }
    return "";
  }

  function parseModelList(rawText) {
    return Array.from(new Set(String(rawText || "").split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean)));
  }

  function syncModelChipActiveState(chipsContainer, allowedInp) {
    if (!chipsContainer || !allowedInp) return;
    const cur = new Set(parseModelList(allowedInp.value));
    chipsContainer.querySelectorAll("button[data-chip-model]").forEach(btn => {
      btn.classList.toggle("active", cur.has(btn.getAttribute("data-chip-model")));
    });
  }

  function renderModelPresetChips(chipsContainer, models, allowedInp) {
    if (!chipsContainer) return;
    const list = Array.isArray(models) ? models.filter(Boolean) : [];
    if (!list.length) {
      chipsContainer.innerHTML = "";
      return;
    }
    chipsContainer.innerHTML = `<div class="model-chips-row">${list.map(m => `<button class="model-chip" type="button" data-chip-model="${esc(m)}">${esc(m)}</button>`).join("")}</div>`;
    chipsContainer.querySelectorAll("button[data-chip-model]").forEach(btn => {
      btn.addEventListener("click", () => {
        if (!allowedInp || allowedInp.disabled) return;
        const model = btn.getAttribute("data-chip-model");
        const cur = parseModelList(allowedInp.value);
        const idx = cur.indexOf(model);
        if (idx >= 0) cur.splice(idx, 1);
        else cur.push(model);
        allowedInp.value = cur.join(", ");
        syncModelChipActiveState(chipsContainer, allowedInp);
      });
    });
    syncModelChipActiveState(chipsContainer, allowedInp);
  }

  function populateProfileSelect(selectEl, warnEl, aid, { isCreate, currentStoredProfile } = {}) {
    if (!selectEl) return;
    const supported = getAdapterSupportedProfiles(aid);
    const safeDef = getAdapterDefaultSafeProfile(aid);
    const ad = STATE.adapters.find(a => a.id === aid);
    const isHeld = Boolean(ad && ad.launch_reason === "launch_held");

    if (!supported.length) {
      selectEl.innerHTML = `<option value="">(该工具不支持启动权限档位)</option>`;
      selectEl.disabled = true;
    } else {
      selectEl.disabled = false;
      const opts = [];
      if (!isCreate && currentStoredProfile && !supported.includes(currentStoredProfile)) {
        opts.push(`<option value="__keep__" selected>保持当前 (${esc(currentStoredProfile)} · 不受支持)</option>`);
      } else if (isCreate && !safeDef) {
        opts.push(`<option value="" selected>(请显式选择权限档位)</option>`);
      }
      supported.forEach(p => {
        const sel = isCreate
          ? (p === safeDef)
          : (p === currentStoredProfile);
        opts.push(`<option value="${esc(p)}" ${sel ? "selected" : ""}>${esc(PROFILE_LABELS[p] || p)}</option>`);
      });
      selectEl.innerHTML = opts.join("");
    }

    if (warnEl) {
      const notes = [];
      if (isHeld) notes.push(`工具 ${aid} 当前处于 ${ad.launch_milestone || "M5"} 暂停状态，不会自动启动。`);
      if (!safeDef && supported.length) notes.push(`工具 ${aid} 仅支持高权限档位 (${supported.join(", ")})，不会自动设为默认。`);
      if (!isCreate && currentStoredProfile && !supported.includes(currentStoredProfile)) {
        notes.push(`当前已保存档位 "${currentStoredProfile}" 不在支持列表内，建议切换为安全档位。`);
      }
      warnEl.textContent = notes.join(" ");
      warnEl.classList.toggle("hidden", !notes.length);
    }
  }

  function openCreateAgentModal() {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const availableAdapters = STATE.adapters.filter(a => !isUnavailableAdapter(a));
    const unavailableAdapters = STATE.adapters.filter(a => isUnavailableAdapter(a));
    const fmtAdapterOpt = (a, isUnavailable) => {
      const holdTag = a.launch_reason === "launch_held"
        ? ` · held ${a.launch_milestone || ""}`
        : (!getAdapterSupportedProfiles(a.id).length ? " · stub" : "");
      const unavailAttrs = isUnavailable ? ` data-unavailable-adapter="1" disabled` : "";
      return `<option value="${esc(a.id)}"${unavailAttrs}>${esc(a.id)} (${esc(formatAdapterFriendlyStatus(a).text)}${esc(holdTag)})</option>`;
    };
    const adapterOpts = [
      availableAdapters.length ? `<optgroup label="可用工具">${availableAdapters.map(a => fmtAdapterOpt(a, false)).join("")}</optgroup>` : "",
      unavailableAdapters.length ? `<optgroup label="暂不可用 / 高级 (held / stub)">${unavailableAdapters.map(a => fmtAdapterOpt(a, true)).join("")}</optgroup>` : ""
    ].join("");
    openModal(
      "新建 Agent",
      `<div class="form-stack">
        <div class="form-row-grid">
          <div class="form-row"><label class="form-label">名称 (必填)</label><input class="compact-input" id="newAgName" type="text" placeholder="例如：codex-worker" /></div>
          <div class="form-row">
            <label class="form-label">AI 工具 (Adapter)</label>
            <select class="compact-select" id="newAgAdapter">${adapterOpts}</select>
            ${unavailableAdapters.length ? `<label style="display:flex;align-items:center;gap:6px;font-size:11.5px;color:var(--text-muted);margin-top:4px;"><input type="checkbox" id="newAgShowAdvancedAdapters" /> 允许选择暂不可用/高级工具</label>` : ""}
          </div>
          <div class="form-row"><label class="form-label">投递模式</label><select class="compact-select" id="newAgDelivery"><option value="launch">launch</option><option value="inbox">inbox</option></select></div>
          <div class="form-row"><label class="form-label">最大并发</label><input class="compact-input" id="newAgConc" type="number" min="1" value="1" /></div>
        </div>
        <div class="form-row">
          <label class="form-label">默认权限档位 (仅显示受支持档位)</label>
          <select class="compact-select" id="newAgDefaultProfile"></select>
          <div class="inspector-note-box amber hidden" id="newAgProfileWarn"></div>
        </div>
        <div class="form-row">
          <label class="form-label" id="newAgAllowedModelsLabel">允许模型 allowed_models (launch 模式必填，逗号分隔)</label>
          <input class="compact-input mono" id="newAgAllowedModels" type="text" placeholder="点击下方快捷预设或自由输入模型 ID" />
          <div id="newAgModelChips"></div>
          <div id="newAgModelHints" style="font-size:11.5px;color:var(--text-muted);"></div>
        </div>
        <div class="form-row">
          <label class="form-label">默认模型 default_model (可选，须在允许模型内)</label>
          <input class="compact-input mono" id="newAgDefaultModel" type="text" placeholder="留空默认取第一项" />
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <input type="checkbox" id="newAgCanLead" /> 允许作为项目 Lead
        </label>
      </div>`,
      `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalSaveAgent" type="button">创建</button>`
    );

    const adapterSel = $("newAgAdapter");
    const advAdaptersChk = $("newAgShowAdvancedAdapters");
    const deliverySel = $("newAgDelivery");
    const profileSel = $("newAgDefaultProfile");
    const profileWarn = $("newAgProfileWarn");
    const allowedInp = $("newAgAllowedModels");
    const defaultInp = $("newAgDefaultModel");
    const chipsEl = $("newAgModelChips");
    const hintsEl = $("newAgModelHints");

    if (adapterSel && availableAdapters.length) adapterSel.value = availableAdapters[0].id;

    async function syncAdapterModelsUI() {
      const aid = adapterSel ? adapterSel.value : "";
      populateProfileSelect(profileSel, profileWarn, aid, { isCreate: true });
      if (aid === "none") {
        deliverySel.value = "inbox";
        deliverySel.disabled = true;
        allowedInp.disabled = true;
        defaultInp.disabled = true;
        allowedInp.value = "";
        defaultInp.value = "";
        renderModelPresetChips(chipsEl, [], allowedInp);
        if (hintsEl) hintsEl.textContent = "none 仅支持收件箱模式，无需填写模型";
        return;
      }
      deliverySel.disabled = false;
      allowedInp.disabled = false;
      defaultInp.disabled = false;
      renderModelPresetChips(chipsEl, [], allowedInp);
      if (hintsEl) hintsEl.textContent = "正在查询可用模型...";
      const res = await apiCall("adapter.models", { adapter_id: aid });
      if (!$("newAgAdapter") || $("newAgAdapter").value !== aid) return;
      const models = (res.ok && res.data && Array.isArray(res.data.models)) ? res.data.models : null;
      if (models && models.length) {
        if (!allowedInp.value.trim()) allowedInp.value = models.slice(0, 3).join(", ");
        renderModelPresetChips(chipsEl, models, allowedInp);
        if (hintsEl) hintsEl.textContent = `可选模型 (${models.length})：点击标签快速增删`;
      } else {
        const preset = getAdapterModelPresets(aid);
        if (preset) {
          const defAllowed = Array.isArray(preset.default_allowed) ? preset.default_allowed : [];
          if (!allowedInp.value.trim() && defAllowed.length) allowedInp.value = defAllowed.join(", ");
          renderModelPresetChips(chipsEl, preset.chips, allowedInp);
          if (hintsEl) hintsEl.textContent = preset.note || "";
        } else {
          renderModelPresetChips(chipsEl, [], allowedInp);
          if (hintsEl) hintsEl.textContent = "请手动输入模型 ID";
        }
      }
    }

    if (advAdaptersChk && adapterSel) {
      advAdaptersChk.addEventListener("change", () => {
        const allowAdv = Boolean(advAdaptersChk.checked);
        adapterSel.querySelectorAll("option[data-unavailable-adapter='1']").forEach(opt => {
          opt.disabled = !allowAdv;
        });
        const selectedOpt = adapterSel.options && adapterSel.selectedIndex >= 0
          ? adapterSel.options[adapterSel.selectedIndex]
          : null;
        const isSelectedUnavailable = Boolean(
          (selectedOpt && selectedOpt.getAttribute("data-unavailable-adapter") === "1")
          || (!selectedOpt && Array.from(adapterSel.querySelectorAll("option[data-unavailable-adapter='1']")).some(opt => opt.value === adapterSel.value))
        );
        if (!allowAdv && availableAdapters.length && isSelectedUnavailable) {
          adapterSel.value = availableAdapters[0].id;
          if (allowedInp) allowedInp.value = "";
          if (defaultInp) defaultInp.value = "";
          syncAdapterModelsUI();
        }
      });
    }
    if (allowedInp) allowedInp.addEventListener("input", () => syncModelChipActiveState(chipsEl, allowedInp));
    if (adapterSel) {
      adapterSel.addEventListener("change", () => {
        if (allowedInp) allowedInp.value = "";
        if (defaultInp) defaultInp.value = "";
        syncAdapterModelsUI();
      });
    }
    syncAdapterModelsUI();

    $("modalCancel").onclick = closeModal;
    $("modalSaveAgent").onclick = async () => {
      if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
      const name = ($("newAgName").value || "").trim();
      if (!name) { toast("请输入 Agent 名称", "warn"); return; }
      const adapter = $("newAgAdapter").value;
      if (!adapter) { toast("请选择可用的工具", "warn"); return; }
      const delivery = $("newAgDelivery").value;
      const defaultProfile = profileSel && !profileSel.disabled ? (profileSel.value || "").trim() : "";
      const allowedModels = parseModelList($("newAgAllowedModels").value);
      const defaultModel = ($("newAgDefaultModel").value || "").trim();

      if (delivery === "launch" && adapter !== "none" && !allowedModels.length) {
        toast("launch 模式必须填写至少一个模型", "warn");
        return;
      }
      if (defaultModel && allowedModels.length && !allowedModels.includes(defaultModel)) {
        toast("默认模型必须包含在允许模型列表中", "warn");
        return;
      }
      if (defaultModel && delivery === "inbox" && adapter !== "none" && !allowedModels.length) {
        allowedModels.push(defaultModel);
      }
      if (defaultProfile && isProfileAboveMaxUnapproved(defaultProfile, "dev")) {
        const confirmed = await confirmHighProfileSelection(adapter, defaultProfile, "dev");
        if (!confirmed) return;
      }

      const payload = {
        name,
        adapter,
        delivery,
        max_concurrency: parseInt($("newAgConc").value, 10) || 1,
        can_lead: Boolean($("newAgCanLead").checked)
      };
      if (defaultProfile) payload.default_profile = defaultProfile;
      if (adapter !== "none") {
        if (allowedModels.length) payload.allowed_models = allowedModels;
        if (defaultModel) payload.default_model = defaultModel;
      }

      const res = await apiCall("agent.create", payload);
      if (res.ok) {
        closeModal();
        toast(`Agent ${name} 创建成功`, "ok");
        await loadAllWorkspaceData();
      } else {
        toast(formatErrorMessage(res.error), "err");
      }
    };
  }

  async function openEditAgentModal(agentName) {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const showRes = await apiCall("agent.show", { name: agentName });
    if (!showRes.ok || !showRes.data) { toast(formatErrorMessage(showRes.error), "err"); return; }
    const ag = showRes.data;
    STATE.agentDetails[agentName] = ag;
    const aid = ag.adapter;
    const isNone = aid === "none";
    const allowedInit = Array.isArray(ag.allowed_models) ? ag.allowed_models.join(", ") : "";
    const defaultModelInit = ag.default_model || "";
    const editSeq = ++editAgentModalSeq;

    openModal(
      `编辑 Agent · ${ag.name}`,
      `<div class="form-stack">
        <div class="form-row-grid">
          <div class="form-row"><label class="form-label">名称</label><input class="compact-input mono" type="text" value="${esc(ag.name)}" disabled /></div>
          <div class="form-row"><label class="form-label">工具 / 模式</label><input class="compact-input mono" type="text" value="${esc(ag.adapter)} · ${esc(ag.delivery)}" disabled /></div>
          <div class="form-row"><label class="form-label">最大并发</label><input class="compact-input" id="editAgConc" type="number" min="1" value="${esc(String(ag.max_concurrency || 1))}" /></div>
          <div class="form-row"><label class="form-label">默认权限档位</label><select class="compact-select" id="editAgDefaultProfile"></select></div>
        </div>
        <div class="inspector-note-box amber hidden" id="editAgProfileWarn"></div>
        <div class="form-row">
          <label class="form-label">允许模型 allowed_models</label>
          <input class="compact-input mono" id="editAgAllowedModels" type="text" value="${esc(allowedInit)}" ${isNone ? "disabled" : ""} />
          <div id="editAgModelChips"></div>
          <div id="editAgModelHints" style="font-size:11.5px;color:var(--text-muted);"></div>
        </div>
        <div class="form-row">
          <label class="form-label">默认模型 default_model</label>
          <input class="compact-input mono" id="editAgDefaultModel" type="text" value="${esc(defaultModelInit)}" ${isNone ? "disabled" : ""} />
        </div>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <input type="checkbox" id="editAgCanLead" ${ag.can_lead ? "checked" : ""} /> 允许作为项目 Lead
        </label>
      </div>`,
      `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalUpdateAgent" type="button">保存修改</button>`
    );

    const profileSel = $("editAgDefaultProfile");
    const profileWarn = $("editAgProfileWarn");
    const allowedInp = $("editAgAllowedModels");
    const chipsEl = $("editAgModelChips");
    const hintsEl = $("editAgModelHints");

    populateProfileSelect(profileSel, profileWarn, aid, { isCreate: false, currentStoredProfile: ag.default_profile });
    if (allowedInp) allowedInp.addEventListener("input", () => syncModelChipActiveState(chipsEl, allowedInp));

    $("modalCancel").onclick = closeModal;
    $("modalUpdateAgent").onclick = async () => {
      if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
      const allowedModels = isNone ? [] : parseModelList($("editAgAllowedModels").value);
      let defaultModel = isNone ? "" : ($("editAgDefaultModel").value || "").trim();

      if (ag.delivery === "launch" && !isNone) {
        if (!allowedModels.length) { toast("launch 模式必须填写至少一个模型", "warn"); return; }
        if (!defaultModel) defaultModel = allowedModels[0];
        else if (!allowedModels.includes(defaultModel)) { toast("默认模型必须包含在允许模型中", "warn"); return; }
      } else if (!isNone && defaultModel && !allowedModels.includes(defaultModel)) {
        toast("默认模型必须包含在允许模型中", "warn");
        return;
      }

      const selectedProfile = profileSel && !profileSel.disabled ? (profileSel.value || "").trim() : "";
      const maxUnapproved = ag.max_unapproved_profile || "dev";
      if (selectedProfile && selectedProfile !== "__keep__" && isProfileAboveMaxUnapproved(selectedProfile, maxUnapproved)) {
        const confirmed = await confirmHighProfileSelection(aid, selectedProfile, maxUnapproved);
        if (!confirmed) return;
      }

      const payload = {
        name: ag.name,
        if_version: ag.version,
        max_concurrency: parseInt($("editAgConc").value, 10) || 1,
        can_lead: Boolean($("editAgCanLead").checked)
      };
      if (selectedProfile && selectedProfile !== "__keep__") payload.default_profile = selectedProfile;
      if (!isNone) {
        payload.allowed_models = allowedModels;
        payload.default_model = defaultModel || "";
      }

      const res = await apiCall("agent.update", payload);
      if (res.ok) {
        delete STATE.agentDetails[ag.name];
        closeModal();
        toast(`Agent ${ag.name} 已更新`, "ok");
        await loadAllWorkspaceData();
      } else {
        toast(formatErrorMessage(res.error), "err");
      }
    };

    if (!isNone) {
      if (hintsEl) hintsEl.textContent = "正在查询可用模型...";
      const modRes = await apiCall("adapter.models", { adapter_id: aid });
      if (editSeq !== editAgentModalSeq || !$("editAgAllowedModels")) return;
      const models = (modRes.ok && modRes.data && Array.isArray(modRes.data.models)) ? modRes.data.models : null;
      if (models && models.length) {
        renderModelPresetChips(chipsEl, models, allowedInp);
        if (hintsEl) hintsEl.textContent = `可选模型 (${models.length})`;
      } else {
        const preset = getAdapterModelPresets(aid);
        if (preset) {
          renderModelPresetChips(chipsEl, preset.chips, allowedInp);
          if (hintsEl) hintsEl.textContent = preset.note || "";
        } else {
          renderModelPresetChips(chipsEl, [], allowedInp);
          if (hintsEl) hintsEl.textContent = "请手动输入模型 ID";
        }
      }
    }
  }

  function openCreateTaskModal() {
    if (!canMutate()) { toast(disabledReason(), "err"); return; }
    const openProjects = STATE.projects.filter(p => p.status !== "completed" && p.status !== "cancelled");
    if (!openProjects.length) {
      toast("当前没有活跃项目，请先发起一个项目对话", "warn");
      return;
    }
    const activePrj = getActiveProject();
    const defaultPrj = (activePrj && activePrj.status !== "completed" && activePrj.status !== "cancelled")
      ? activePrj
      : openProjects[0];

    const projectOpts = openProjects.map(p =>
      `<option value="${esc(p.id)}" ${p.id === defaultPrj.id ? "selected" : ""}>${esc(p.name || p.id)}</option>`
    ).join("");
    const profileOpts = ["", "readonly", "edit", "dev", "elevated", "unrestricted"].map(pf =>
      `<option value="${esc(pf)}">${pf ? esc(pf) : "(使用 Agent 默认)"}</option>`
    ).join("");
    const effortOpts = ["", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode"].map(ef =>
      `<option value="${esc(ef)}">${ef ? esc(ef) : "(默认)"}</option>`
    ).join("");

    const hasRepo = Boolean(defaultPrj.repo);
    openModal(
      "手工创建任务（高级）",
      `<div class="form-stack">
        <div class="form-row"><label class="form-label" for="newTaskTitle">任务标题 (必填)</label><input class="compact-input" id="newTaskTitle" type="text" placeholder="简要描述要做的事..." /></div>
        <div class="form-row"><label class="form-label" for="newTaskSpec">详细说明 (必填)</label><textarea class="compact-input" id="newTaskSpec" rows="3" placeholder="具体要求与验收要点..."></textarea></div>
        <div class="form-row-grid">
          <div class="form-row"><label class="form-label" for="newTaskAssignee">执行 Worker</label><select class="compact-select" id="newTaskAssignee"></select></div>
          <div class="form-row"><label class="form-label" for="newTaskProject">所属项目</label><select class="compact-select" id="newTaskProject">${projectOpts}</select></div>
        </div>
        <details class="config-raw-details">
          <summary>高级参数（类型 / 档位 / 模型 / 超时 / 依赖）</summary>
          <div class="form-stack" style="margin-top:10px;">
            <div class="form-row-grid">
              <div class="form-row">
                <label class="form-label" for="newTaskKind">任务类型</label>
                <select class="compact-select" id="newTaskKind">
                  <option value="general" ${!hasRepo ? "selected" : ""}>通用任务 (general)</option>
                  <option value="code" ${hasRepo ? "selected" : ""}>代码任务 (code)</option>
                </select>
              </div>
              <div class="form-row"><label class="form-label" for="newTaskProfile">权限档位</label><select class="compact-select" id="newTaskProfile">${profileOpts}</select></div>
              <div class="form-row"><label class="form-label" for="newTaskModel">模型</label><input class="compact-input mono" id="newTaskModel" type="text" placeholder="留空使用默认" /></div>
              <div class="form-row"><label class="form-label" for="newTaskEffort">推理强度</label><select class="compact-select" id="newTaskEffort">${effortOpts}</select></div>
              <div class="form-row"><label class="form-label" for="newTaskPriority">优先级</label><input class="compact-input mono" id="newTaskPriority" type="number" min="-1000000" max="1000000" placeholder="0" /></div>
              <div class="form-row"><label class="form-label" for="newTaskMaxAttempts">最大重试次数</label><input class="compact-input mono" id="newTaskMaxAttempts" type="number" min="1" max="10" placeholder="3" /></div>
              <div class="form-row"><label class="form-label" for="newTaskTimeout">超时秒数</label><input class="compact-input mono" id="newTaskTimeout" type="number" min="1" max="604800" placeholder="3600" /></div>
              <div class="form-row"><label class="form-label" for="newTaskIdleTimeout">空闲超时秒数</label><input class="compact-input mono" id="newTaskIdleTimeout" type="number" min="0" max="604800" placeholder="0" /></div>
            </div>
            <div class="form-row"><label class="form-label" for="newTaskDependsOn">前置依赖任务 ID (逗号分隔)</label><input class="compact-input mono" id="newTaskDependsOn" type="text" placeholder="tsk_..." /></div>
          </div>
        </details>
      </div>`,
      `<button class="btn" id="modalCancel" type="button">取消</button><button class="btn btn-primary" id="modalSaveTask" type="button">创建任务</button>`
    );

    const prjSel = $("newTaskProject");
    const kindSel = $("newTaskKind");
    const assigneeSel = $("newTaskAssignee");

    function syncTaskContext() {
      const pid = prjSel ? prjSel.value : defaultPrj.id;
      const basePrj = STATE.projects.find(p => p.id === pid) || defaultPrj;
      const leadName = basePrj ? basePrj.lead : null;
      if (kindSel && basePrj && Object.prototype.hasOwnProperty.call(basePrj, "repo")) {
        const codeOpt = kindSel.querySelector('option[value="code"]');
        if (!basePrj.repo) {
          if (codeOpt) codeOpt.disabled = true;
          kindSel.value = "general";
        } else {
          if (codeOpt) codeOpt.disabled = false;
          kindSel.value = "code";
        }
      }
      if (assigneeSel) {
        const workers = STATE.agents.filter(a => !a.disabled && a.name !== leadName);
        const firstWorker = workers[0] ? workers[0].name : "";
        const opts = [`<option value="">(暂不指派)</option>`].concat(
          STATE.agents.filter(a => !a.disabled).map(a => {
            const isLead = Boolean(leadName && a.name === leadName);
            const sel = !isLead && a.name === firstWorker;
            return `<option value="${esc(a.name)}" ${sel ? "selected" : ""} ${isLead ? "disabled" : ""}>${esc(a.name)} (${esc(a.adapter)})${isLead ? " [当前 Lead 不可指派]" : ""}</option>`;
          })
        );
        assigneeSel.innerHTML = opts.join("");
      }
    }

    if (prjSel) prjSel.addEventListener("change", syncTaskContext);
    syncTaskContext();

    $("modalCancel").onclick = closeModal;
    $("modalSaveTask").onclick = async () => {
      if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
      const project = ($("newTaskProject").value || "").trim();
      const title = ($("newTaskTitle").value || "").trim();
      const spec = ($("newTaskSpec").value || "").trim();
      const kind = ($("newTaskKind").value || "").trim();
      const assignee = ($("newTaskAssignee").value || "").trim();
      const profile = ($("newTaskProfile").value || "").trim();
      const model = ($("newTaskModel").value || "").trim();
      const effort = ($("newTaskEffort").value || "").trim();
      const rawPriority = ($("newTaskPriority").value || "").trim();
      const rawTimeout = ($("newTaskTimeout").value || "").trim();
      const rawIdleTimeout = ($("newTaskIdleTimeout").value || "").trim();
      const rawMaxAttempts = ($("newTaskMaxAttempts").value || "").trim();
      const rawDeps = ($("newTaskDependsOn").value || "").trim();

      if (!project || !title || !spec) { toast("请填写项目、任务标题与说明", "warn"); return; }

      const payload = { project, title, spec };
      if (kind) payload.kind = kind;
      if (assignee) payload.assignee = assignee;
      if (profile) payload.profile = profile;
      if (model) payload.model = model;
      if (effort) payload.effort = effort;
      if (rawPriority !== "") {
        const prio = Number(rawPriority);
        if (!Number.isInteger(prio) || prio < -1000000 || prio > 1000000) { toast("优先级须为整数", "warn"); return; }
        payload.priority = prio;
      }
      if (rawTimeout !== "") {
        const to = Number(rawTimeout);
        if (!Number.isInteger(to) || to < 1 || to > 604800) { toast("超时秒数范围 1..604800", "warn"); return; }
        payload.timeout = to;
      }
      if (rawIdleTimeout !== "") {
        const ito = Number(rawIdleTimeout);
        if (!Number.isInteger(ito) || ito < 0 || ito > 604800) { toast("空闲超时秒数范围 0..604800", "warn"); return; }
        payload.idle_timeout = ito;
      }
      if (rawMaxAttempts !== "") {
        const ma = Number(rawMaxAttempts);
        if (!Number.isInteger(ma) || ma < 1 || ma > 10) { toast("最大重试次数范围 1..10", "warn"); return; }
        payload.max_attempts = ma;
      }
      if (rawDeps) {
        const deps = Array.from(new Set(rawDeps.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean)));
        if (deps.length) payload.depends_on = deps;
      }

      const res = await apiCall("task.create", payload);
      if (res.ok) {
        closeModal();
        bumpProjectSwitchGen();
        STATE.ui.draftNewChat = false;
        STATE.activeProjectId = project;
        if (res.data && res.data.id) STATE.activeTaskId = res.data.id;
        toast("任务创建成功", "ok");
        await loadAllWorkspaceData();
      } else {
        toast(formatErrorMessage(res.error), "err");
        if (res.error && res.error.code === "APPROVAL_REQUIRED") await loadAllWorkspaceData();
      }
    };
  }

  // =========================================================================
  // Frameless Window Resize / Drag & Resizable Sidebars
  // =========================================================================
  function bindWindowResizeHandles() {
    const handles = document.querySelectorAll(".resize-handle[data-resize-edge]");
    if (!handles.length) return;
    handles.forEach(handle => {
      handle.addEventListener("mousedown", startEvent => {
        if (startEvent.button !== 0) return;
        if (STATE.ui.windowMaximized || (document.body && document.body.classList.contains("window-maximized"))) return;
        if (!hasApi() || typeof window.pywebview.api.window_resize !== "function") return;
        const edge = handle.getAttribute("data-resize-edge");
        if (!edge) return;
        startEvent.preventDefault();

        const startScreenX = startEvent.screenX;
        const startScreenY = startEvent.screenY;
        const startWidth = window.outerWidth || window.innerWidth;
        const startHeight = window.outerHeight || window.innerHeight;
        let pending = null;
        let inFlight = false;
        let rafId = 0;
        let errored = false;
        let ending = false;
        let ended = false;
        let resizeStartPromise = null;

        document.body.classList.add("window-resizing");
        if (typeof window.pywebview.api.window_resize_start === "function") {
          resizeStartPromise = Promise.resolve(window.pywebview.api.window_resize_start(edge)).catch(() => null);
        }
        const finishResizeEnd = () => {
          if (ended) return;
          ended = true;
          if (hasApi() && typeof window.pywebview.api.window_resize_end === "function") {
            Promise.resolve(window.pywebview.api.window_resize_end()).catch(() => {});
          }
        };
        const flushResize = async () => {
          rafId = 0;
          if (inFlight || !pending) return;
          const next = pending;
          pending = null;
          inFlight = true;
          try {
            if (resizeStartPromise) { await resizeStartPromise; resizeStartPromise = null; }
            const res = await window.pywebview.api.window_resize(next.width, next.height, next.edge);
            if (res && res.ok) updateWindowMaxButton(false);
            else if (res && !res.ok && !errored) { errored = true; toast(formatErrorMessage(res.error), "err"); }
          } catch (err) {
            if (!errored) { errored = true; toast(`调整窗口大小失败: ${err && err.message ? err.message : String(err)}`, "err"); }
          } finally {
            inFlight = false;
            if (pending) rafId = window.requestAnimationFrame(flushResize);
            else if (ending) finishResizeEnd();
          }
        };
        const onMouseMove = moveEvent => {
          const dx = moveEvent.screenX - startScreenX;
          const dy = moveEvent.screenY - startScreenY;
          let targetWidth = startWidth;
          let targetHeight = startHeight;
          if (edge.includes("e")) targetWidth = startWidth + dx;
          else if (edge.includes("w")) targetWidth = startWidth - dx;
          if (edge.includes("s")) targetHeight = startHeight + dy;
          else if (edge.includes("n")) targetHeight = startHeight - dy;
          pending = { width: Math.round(targetWidth), height: Math.round(targetHeight), edge };
          if (!rafId && !inFlight) rafId = window.requestAnimationFrame(flushResize);
        };
        const stopResize = () => {
          if (ending) return;
          ending = true;
          document.body.classList.remove("window-resizing");
          window.removeEventListener("mousemove", onMouseMove);
          window.removeEventListener("mouseup", stopResize);
          window.removeEventListener("blur", stopResize);
          if (rafId) { window.cancelAnimationFrame(rafId); rafId = 0; }
          if (pending && !inFlight) flushResize();
          else if (!inFlight) {
            if (resizeStartPromise) { const p = resizeStartPromise; resizeStartPromise = null; p.finally(finishResizeEnd); }
            else finishResizeEnd();
          }
        };
        window.addEventListener("mousemove", onMouseMove);
        window.addEventListener("mouseup", stopResize);
        window.addEventListener("blur", stopResize);
      });
    });
  }

  function bindMaximizedTitlebarDrag() {
    const dragEl = $("titlebarDrag");
    if (!dragEl) return;
    dragEl.addEventListener("pointerdown", startEvent => {
      if (startEvent.button !== 0 || !STATE.ui.windowMaximized) return;
      if (!hasApi() || typeof window.pywebview.api.window_restore_for_drag !== "function") return;
      const pointerId = startEvent.pointerId;
      const startClientX = startEvent.clientX;
      const startClientY = startEvent.clientY;
      let lastScreenX = startEvent.screenX;
      let lastScreenY = startEvent.screenY;
      const ratioX = Math.max(0.05, Math.min(0.95, startClientX / Math.max(1, window.innerWidth || 1)));
      const offsetY = Math.max(4, Math.min(48, startClientY));
      let restoreStarted = false;
      let restored = false;
      let stopped = false;
      let inFlight = false;
      let rafId = 0;
      let pendingDx = 0;
      let pendingDy = 0;
      let captured = false;

      const flushMove = async () => {
        rafId = 0;
        if (stopped || !restored || inFlight || typeof window.pywebview.api.window_drag_move !== "function") return;
        const dx = pendingDx;
        const dy = pendingDy;
        pendingDx = 0;
        pendingDy = 0;
        inFlight = true;
        try { await window.pywebview.api.window_drag_move(dx, dy); } catch (_) {}
        finally {
          inFlight = false;
          if (!stopped && restored && (pendingDx !== 0 || pendingDy !== 0)) rafId = window.requestAnimationFrame(flushMove);
        }
      };
      const onPointerMove = async moveEvent => {
        if (stopped) return;
        if (!restoreStarted) {
          if (Math.hypot(moveEvent.clientX - startClientX, moveEvent.clientY - startClientY) < 5) return;
          restoreStarted = true;
          lastScreenX = moveEvent.screenX;
          lastScreenY = moveEvent.screenY;
          try { if (typeof dragEl.setPointerCapture === "function") { dragEl.setPointerCapture(pointerId); captured = true; } } catch (_) {}
          try {
            const res = await window.pywebview.api.window_restore_for_drag(ratioX, offsetY);
            if (res && res.ok) {
              updateWindowMaxButton(false);
              if (!stopped) restored = true;
              else if (hasApi() && typeof window.pywebview.api.window_drag_end === "function") Promise.resolve(window.pywebview.api.window_drag_end()).catch(() => {});
            } else if (res && !res.ok) { toast(formatErrorMessage(res.error), "err"); cleanup(); }
          } catch (err) { toast(`拖动还原窗口失败: ${err && err.message ? err.message : String(err)}`, "err"); cleanup(); }
          return;
        }
        if (!restored) return;
        pendingDx += moveEvent.screenX - lastScreenX;
        pendingDy += moveEvent.screenY - lastScreenY;
        lastScreenX = moveEvent.screenX;
        lastScreenY = moveEvent.screenY;
        if (!rafId && !inFlight) rafId = window.requestAnimationFrame(flushMove);
      };
      const cleanup = () => {
        if (stopped) return;
        stopped = true;
        if (rafId) { window.cancelAnimationFrame(rafId); rafId = 0; }
        if (captured && typeof dragEl.releasePointerCapture === "function") { try { dragEl.releasePointerCapture(pointerId); } catch (_) {} }
        window.removeEventListener("pointermove", onPointerMove);
        window.removeEventListener("pointerup", cleanup);
        window.removeEventListener("pointercancel", cleanup);
        window.removeEventListener("blur", cleanup);
        if (restored && hasApi() && typeof window.pywebview.api.window_drag_end === "function") {
          Promise.resolve(window.pywebview.api.window_drag_end()).catch(() => {});
        }
      };
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", cleanup);
      window.addEventListener("pointercancel", cleanup);
      window.addEventListener("blur", cleanup);
    });
  }

  function bindPaneResizers() {
    const sbResizer = $("sidebarResizer");
    if (sbResizer) {
      sbResizer.addEventListener("mousedown", e => {
        if (e.button !== 0 || STATE.ui.sidebarCollapsed) return;
        e.preventDefault();
        const startX = e.clientX;
        const startW = STATE.ui.sidebarWidth;
        const onMove = mv => {
          STATE.ui.sidebarWidth = Math.max(240, Math.min(400, startW + (mv.clientX - startX)));
          document.documentElement.style.setProperty("--sidebar-width", `${STATE.ui.sidebarWidth}px`);
        };
        const onUp = () => {
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
          saveStoredUiState();
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      });
    }
    const inspResizer = $("inspectorResizer");
    if (inspResizer) {
      inspResizer.addEventListener("mousedown", e => {
        if (e.button !== 0 || STATE.ui.inspectorCollapsed) return;
        e.preventDefault();
        const startX = e.clientX;
        const startW = STATE.ui.inspectorWidth;
        const onMove = mv => {
          STATE.ui.inspectorWidth = Math.max(280, Math.min(480, startW - (mv.clientX - startX)));
          document.documentElement.style.setProperty("--inspector-width", `${STATE.ui.inspectorWidth}px`);
        };
        const onUp = () => {
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
          saveStoredUiState();
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      });
    }
  }

  // =========================================================================
  // Event Binding & Delegation
  // =========================================================================
  function bindStaticHandlers() {
    bindWindowResizeHandles();
    bindMaximizedTitlebarDrag();
    bindPaneResizers();
    startDurationTicker();

    const reportWindowCtrlError = (actionLabel, err) => {
      const msg = err && err.message ? err.message : String(err || "未知错误");
      toast(`${actionLabel}失败: ${msg}`, "err");
    };

    if ($("btnWinMin")) {
      $("btnWinMin").addEventListener("click", async () => {
        if (hasApi() && typeof window.pywebview.api.window_minimize === "function") {
          try {
            const res = await window.pywebview.api.window_minimize();
            if (res && !res.ok) toast(formatErrorMessage(res.error), "err");
          } catch (err) { reportWindowCtrlError("最小化窗口", err); }
        }
      });
    }
    const toggleMaximize = async () => {
      if (hasApi() && typeof window.pywebview.api.window_toggle_maximize === "function") {
        try {
          const res = await window.pywebview.api.window_toggle_maximize();
          if (res && res.ok && res.data && typeof res.data.maximized === "boolean") updateWindowMaxButton(res.data.maximized);
          else if (res && !res.ok) toast(formatErrorMessage(res.error), "err");
        } catch (err) { reportWindowCtrlError("切换最大化", err); }
      }
    };
    if ($("btnWinMax")) $("btnWinMax").addEventListener("click", toggleMaximize);
    if ($("titlebarDrag")) $("titlebarDrag").addEventListener("dblclick", toggleMaximize);
    if ($("btnWinClose")) {
      $("btnWinClose").addEventListener("click", async () => {
        if (hasApi() && typeof window.pywebview.api.window_close === "function") {
          try {
            const res = await window.pywebview.api.window_close();
            if (res && !res.ok) toast(formatErrorMessage(res.error), "err");
          } catch (err) { reportWindowCtrlError("关闭窗口", err); }
        }
      });
    }
    if ($("btnFooterSettings")) {
      $("btnFooterSettings").addEventListener("click", async () => {
        STATE.ui.nav = "settings";
        renderAll();
        if (STATE.connected && STATE.auth.auth_verified) {
          await loadGrants();
          renderSettingsView();
        }
      });
    }

    $("btnQuickTheme").addEventListener("click", () => {
      applyTheme(STATE.ui.theme === "dark" ? "light" : "dark");
      if (STATE.ui.nav === "settings") renderSettingsView();
    });

    $("btnCollapseSidebar").addEventListener("click", () => {
      STATE.ui.sidebarCollapsed = !STATE.ui.sidebarCollapsed;
      saveStoredUiState();
      renderSidebar();
    });
    $("btnToggleInspector").addEventListener("click", async () => {
      STATE.ui.inspectorCollapsed = !STATE.ui.inspectorCollapsed;
      saveStoredUiState();
      renderSidebar();
      if (!STATE.ui.inspectorCollapsed) {
        renderInspector();
        if (STATE.connected && STATE.auth.auth_verified) {
          if (STATE.ui.inspectorTab === "outline") await fetchVisibleLeadTurnLogs();
          else if (STATE.ui.inspectorTab === "diff") await fetchActiveDiffData();
          else if (STATE.ui.inspectorTab === "task" && STATE.activeTaskId) await loadActiveTaskInspectorData(STATE.activeTaskId);
          if (STATE.connected && STATE.auth.auth_verified && !STATE.ui.inspectorCollapsed) {
            renderInspector();
            syncLogPollingTimer();
          }
        }
      } else {
        syncLogPollingTimer();
      }
    });

    $("btnToggleSearch").addEventListener("click", () => {
      STATE.ui.searchVisible = !STATE.ui.searchVisible;
      renderSidebar();
      if (STATE.ui.searchVisible && $("projectSearchInput")) $("projectSearchInput").focus();
    });
    if ($("projectSearchInput")) {
      $("projectSearchInput").addEventListener("input", e => {
        STATE.ui.searchQuery = e.target.value || "";
        renderSidebar();
      });
    }

    $("btnNewChat").addEventListener("click", () => {
      bumpProjectSwitchGen();
      STATE.ui.nav = "tasks";
      STATE.ui.draftNewChat = true;
      STATE.ui.repoDrawerOpen = false;
      STATE.ui.composerInlineNote = null;
      renderAll();
      if ($("composerInput")) $("composerInput").focus();
    });

    $("btnToggleRepoDrawer").addEventListener("click", () => {
      const prj = getActiveProject();
      if (!prj || STATE.ui.draftNewChat) {
        STATE.ui.repoDrawerOpen = !STATE.ui.repoDrawerOpen;
        renderComposer();
      } else if (prj.repo) {
        toast(`已绑定仓库：${prj.repo}（分支 ${prj.base_branch || "--"}）`, "info");
      } else {
        toast("当前项目为通用对话项目；点左侧 + 新建对话可绑定本地 Git 仓库", "info");
      }
    });
    if ($("btnCloseRepoDrawer")) {
      $("btnCloseRepoDrawer").addEventListener("click", () => {
        STATE.ui.repoDrawerOpen = false;
        renderComposer();
      });
    }
    if ($("btnPickRepoFolder")) {
      $("btnPickRepoFolder").addEventListener("click", () => handlePickRepoFolder());
    }
    if ($("btnInspectRepoFolder")) {
      $("btnInspectRepoFolder").addEventListener("click", () => {
        const cur = ($("newPrjRepo") && $("newPrjRepo").value.trim()) || "";
        handleInspectRepoPath(cur, { autoFillDefaults: true });
      });
    }
    if ($("newPrjRepo")) {
      $("newPrjRepo").addEventListener("input", () => {
        renderComposer();
        if (repoInspectTimer) window.clearTimeout(repoInspectTimer);
        const val = ($("newPrjRepo") && $("newPrjRepo").value.trim()) || "";
        if (!val) {
          STATE.ui.repoInspection = null;
          STATE.ui.repoInspecting = false;
          renderRepoInspectionUi();
          return;
        }
        repoInspectTimer = window.setTimeout(() => {
          handleInspectRepoPath(val, { autoFillDefaults: !($("newPrjBaseBranch") && $("newPrjBaseBranch").value.trim()) });
        }, 420);
      });
    }
    if ($("newPrjBaseBranch")) {
      $("newPrjBaseBranch").addEventListener("input", renderRepoInspectionUi);
    }
    if ($("newPrjTestCmd")) {
      $("newPrjTestCmd").addEventListener("input", renderRepoInspectionUi);
    }

    $("btnDaemonMenu").addEventListener("click", e => { e.stopPropagation(); toggleDaemonPopover(); });
    if ($("btnDaemonMenuDots")) $("btnDaemonMenuDots").addEventListener("click", e => { e.stopPropagation(); toggleDaemonPopover(); });
    $("btnLeadPill").addEventListener("click", e => { e.stopPropagation(); toggleLeadPopover(); });
    $("btnProjectActions").addEventListener("click", e => { e.stopPropagation(); toggleProjectActionPopover(); });
    $("btnDeliverBase").addEventListener("click", () => handleProjectAction("deliver"));

    document.querySelectorAll(".sidebar-nav-pill[data-nav]").forEach(btn => {
      btn.addEventListener("click", async () => {
        STATE.ui.nav = btn.getAttribute("data-nav");
        renderAll();
        if (STATE.ui.nav === "cli" && STATE.connected && STATE.auth.auth_verified) {
          loadAdapters({ force: true });
        } else if (STATE.ui.nav === "settings" && STATE.connected && STATE.auth.auth_verified) {
          await loadGrants();
          renderSettingsView();
        }
      });
    });

    document.querySelectorAll(".tab-btn[data-tab]").forEach(btn => {
      btn.addEventListener("click", async () => {
        STATE.ui.inspectorTab = btn.getAttribute("data-tab");
        renderInspector();
        syncLogPollingTimer();
        if (STATE.ui.inspectorTab === "outline") {
          await fetchVisibleLeadTurnLogs();
          if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        } else if (STATE.ui.inspectorTab === "diff") {
          await fetchActiveDiffData();
          if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        } else if (STATE.ui.inspectorTab === "task" && STATE.activeTaskId) {
          await loadActiveTaskInspectorData(STATE.activeTaskId);
          if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        }
      });
    });

    // Global shortcuts: Ctrl+B toggles sidebar rail; Escape closes topmost modal cleanly
    document.addEventListener("keydown", e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        STATE.ui.sidebarCollapsed = !STATE.ui.sidebarCollapsed;
        renderSidebar();
      } else if (e.key === "Escape") {
        const confirmBackdrop = $("confirmModalBackdrop");
        const modalBackdrop = $("modalBackdrop");
        if (confirmBackdrop && !confirmBackdrop.classList.contains("hidden")) {
          e.preventDefault();
          closeConfirmModal(false);
        } else if (modalBackdrop && !modalBackdrop.classList.contains("hidden")) {
          e.preventDefault();
          closeModal();
        }
      }
    });

    // S2 Wizard bindings
    if ($("wizardStep1Form")) {
      $("wizardStep1Form").addEventListener("submit", handleWizardInitSubmit);
    }
    const syncWizardPassHints = () => {
      const p1 = ($("wizardPassInput") && $("wizardPassInput").value) || "";
      const p2 = ($("wizardPassConfirm") && $("wizardPassConfirm").value) || "";
      const lenHint = $("wizardPassLenHint");
      const matchHint = $("wizardPassMatchHint");
      if (lenHint) {
        lenHint.textContent = p1.length >= 12 ? `口令长度符合要求 (${p1.length} 位)` : `当前长度：${p1.length} / 12`;
        lenHint.className = `wizard-field-hint ${p1.length >= 12 ? "ok" : ""}`;
      }
      if (matchHint) {
        if (!p2) { matchHint.textContent = ""; matchHint.className = "wizard-field-hint"; }
        else if (p1 === p2) { matchHint.textContent = "两次输入的口令一致"; matchHint.className = "wizard-field-hint ok"; }
        else { matchHint.textContent = "两次输入的口令不一致"; matchHint.className = "wizard-field-hint err"; }
      }
    };
    if ($("wizardPassInput")) $("wizardPassInput").addEventListener("input", syncWizardPassHints);
    if ($("wizardPassConfirm")) $("wizardPassConfirm").addEventListener("input", syncWizardPassHints);
    if ($("btnWizardRecheck")) {
      $("btnWizardRecheck").addEventListener("click", async () => {
        if (!hasApi()) return;
        const res = await window.pywebview.api.reconnect_now();
        if (res && res.ok) applyConnectionSnapshot(res.data);
        await refreshConnectionAndAuth();
        if (STATE.pipe.home_initialized) {
          closeWizard();
          if (STATE.connected && STATE.auth.auth_verified) await loadAllWorkspaceData();
        }
        renderAll();
      });
    }
    if ($("btnWizardDoctor")) {
      $("btnWizardDoctor").addEventListener("click", async () => {
        if (!hasApi()) return;
        try {
          const res = await window.pywebview.api.doctor_report();
          if (res && res.ok) {
            STATE.doctor = res.data;
            openModal("环境诊断", `<pre class="log-box">${esc(JSON.stringify(res.data, null, 2))}</pre>`, `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
          } else toast(formatErrorMessage(res && res.error), "err");
        } catch (err) { toast(`环境诊断失败: ${String(err)}`, "err"); }
      });
    }
    if ($("btnWizardRescanClis")) $("btnWizardRescanClis").addEventListener("click", scanWizardClis);
    if ($("btnWizardFinish")) $("btnWizardFinish").addEventListener("click", handleWizardFinish);
    if ($("btnWizardSkip")) {
      $("btnWizardSkip").addEventListener("click", async () => {
        STATE.wizard.skipped = true;
        closeWizard();
        if (STATE.connected && STATE.auth.auth_verified) {
          await loadAllWorkspaceData();
        } else {
          STATE.loadingWorkspace = false;
          renderAll();
        }
      });
    }

    // Daemon & Auth inline bars
    if ($("btnInlineViewLog")) $("btnInlineViewLog").addEventListener("click", openDaemonLogModal);
    $("btnInlineStartDaemon").addEventListener("click", handleStartDaemon);
    $("btnInlineReconnect").addEventListener("click", async () => {
      if (!hasApi()) return;
      const res = await window.pywebview.api.reconnect_now();
      if (res && res.ok) applyConnectionSnapshot(res.data);
      await refreshConnectionAndAuth();
      if (STATE.connected && STATE.auth.auth_verified) await loadAllWorkspaceData();
      else renderAll();
    });

    if ($("authInlineRemember")) {
      $("authInlineRemember").addEventListener("change", () => {
        const remWarn = $("authInlineRememberWarning");
        if (remWarn) {
          const show = Boolean(STATE.auth.stored_credential_supported && $("authInlineRemember").checked);
          remWarn.classList.toggle("hidden", !show);
        }
      });
    }

    $("authInlineForm").addEventListener("submit", async e => {
      e.preventDefault();
      const pw = $("authInlineInput").value;
      const remember = Boolean($("authInlineRemember") && $("authInlineRemember").checked);
      $("authInlineInput").value = "";
      if ($("authInlineRemember")) $("authInlineRemember").checked = false;
      if ($("authInlineRememberWarning")) $("authInlineRememberWarning").classList.add("hidden");
      await handleUnlockPassphrase(pw, remember);
    });

    // Composer submit: Enter sends/queues (with IME guard), Shift+Enter inserts newline, Ctrl+Enter steers when Lead is running
    $("btnSendComposer").addEventListener("click", handleComposerSubmit);
    if ($("btnStopLeadTurn")) {
      $("btnStopLeadTurn").addEventListener("click", () => handleInterruptLeadTurn());
    }
    if ($("btnSteerLeadTurn")) {
      $("btnSteerLeadTurn").addEventListener("click", () => {
        const draft = ($("composerInput") && $("composerInput").value || "").trim();
        if (draft) handleInterruptLeadTurn({ steerText: draft });
      });
    }
    $("composerInput").addEventListener("input", () => {
      syncComposerTextareaHeight();
      const steerBtn = $("btnSteerLeadTurn");
      if (steerBtn) {
        const prj = getActiveProject();
        const isLeadBusy = Boolean(prj && !STATE.ui.draftNewChat && STATE.activeLeadRun);
        const hasDraft = Boolean($("composerInput").value.trim());
        steerBtn.classList.toggle("hidden", !isLeadBusy || !hasDraft);
      }
    });
    $("composerInput").addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        const prj = getActiveProject();
        const isLeadBusy = Boolean(prj && !STATE.ui.draftNewChat && STATE.activeLeadRun);
        const draft = ($("composerInput") && $("composerInput").value || "").trim();
        if ((e.ctrlKey || e.metaKey) && isLeadBusy && draft) {
          handleInterruptLeadTurn({ steerText: draft });
        } else {
          handleComposerSubmit();
        }
      }
    });

    // Modals
    $("btnModalClose").addEventListener("click", closeModal);
    $("modalBackdrop").addEventListener("click", e => { if (e.target === $("modalBackdrop")) closeModal(); });
    if ($("btnConfirmModalClose")) $("btnConfirmModalClose").addEventListener("click", () => closeConfirmModal(false));
    if ($("confirmModalBackdrop")) $("confirmModalBackdrop").addEventListener("click", e => { if (e.target === $("confirmModalBackdrop")) closeConfirmModal(false); });

    // Delegated clicks & inputs
    document.addEventListener("input", e => {
      const t = e.target;
      if (t && t.hasAttribute && t.hasAttribute("data-wizard-model-input")) {
        const aid = t.getAttribute("data-wizard-model-input");
        STATE.wizard.selectedModels[aid] = t.value;
      }
    });

    document.addEventListener("click", async e => {
      const target = e.target;

      // Wizard CLI pick & model chips
      const wizChip = target.closest("[data-wizard-model-chip]");
      if (wizChip) {
        e.stopPropagation();
        const aid = wizChip.getAttribute("data-wizard-model-chip");
        const model = wizChip.getAttribute("data-model");
        const cur = parseModelList(STATE.wizard.selectedModels[aid] || "");
        const idx = cur.indexOf(model);
        if (idx >= 0) cur.splice(idx, 1);
        else cur.push(model);
        STATE.wizard.selectedModels[aid] = cur.join(", ");
        renderWizard();
        return;
      }
      // Copy code button inside Markdown / Wizard / Diff header (must run before wizard card pick)
      const copyBtn = target.closest("[data-copy-code]");
      if (copyBtn) {
        const text = copyBtn.getAttribute("data-copy-code") || "";
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(() => toast("已复制到剪贴板", "ok")).catch(() => {});
        }
        return;
      }

      const wizPick = target.closest("[data-wizard-pick-cli]");
      if (wizPick && !target.closest("input, button, [data-copy-code]")) {
        const cid = wizPick.getAttribute("data-wizard-pick-cli");
        if (STATE.wizard.selectedLeadCli !== cid) {
          STATE.wizard.selectedLeadCli = cid;
          renderWizard();
        }
        return;
      }

      // B2: Queued follow-up message edit / cancel
      const cancelQBtn = target.closest("[data-cancel-queued-msg]");
      if (cancelQBtn) {
        await handleCancelQueuedMessage(cancelQBtn.getAttribute("data-cancel-queued-msg"));
        return;
      }
      const editQBtn = target.closest("[data-edit-queued-msg]");
      if (editQBtn) {
        handleEditQueuedMessage(editQBtn.getAttribute("data-edit-queued-msg"));
        return;
      }

      // B1: Diff tab quick open, scope toggle, refresh, expand/collapse all, jump to file
      const openDiffBtn = target.closest("[data-open-diff-tab]");
      if (openDiffBtn) {
        STATE.ui.diffScope = openDiffBtn.getAttribute("data-open-diff-tab") || "task";
        STATE.ui.inspectorCollapsed = false;
        STATE.ui.inspectorTab = "diff";
        renderSidebar();
        renderInspector();
        await fetchActiveDiffData({ scope: STATE.ui.diffScope });
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }
      const diffScopeBtn = target.closest("[data-diff-scope]");
      if (diffScopeBtn) {
        STATE.ui.diffScope = diffScopeBtn.getAttribute("data-diff-scope") || "project";
        renderInspector();
        await fetchActiveDiffData({ scope: STATE.ui.diffScope });
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }
      if (target.closest("[data-diff-refresh]")) {
        await fetchActiveDiffData({ scope: STATE.ui.diffScope });
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }
      if (target.closest("[data-diff-expand-all]")) {
        document.querySelectorAll("#inspectorBody details.diff-file-details").forEach(el => { el.open = true; });
        return;
      }
      if (target.closest("[data-diff-collapse-all]")) {
        document.querySelectorAll("#inspectorBody details.diff-file-details").forEach(el => { el.open = false; });
        return;
      }
      const jumpFileBtn = target.closest("[data-diff-jump-file]");
      if (jumpFileBtn) {
        const fid = jumpFileBtn.getAttribute("data-diff-jump-file");
        const detEl = fid ? $(fid) : null;
        if (detEl) {
          detEl.open = true;
          detEl.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        return;
      }

      // B5: Quick navigation shortcut (e.g. from Lead popover to AI Tools)
      const navGotoBtn = target.closest("[data-nav-goto]");
      if (navGotoBtn) {
        closeAllPopovers();
        STATE.ui.nav = navGotoBtn.getAttribute("data-nav-goto") || "cli";
        renderAll();
        if (STATE.ui.nav === "cli" && STATE.connected && STATE.auth.auth_verified) {
          loadAdapters({ force: true });
        }
        return;
      }

      // A7: Welcome starter prompt chips & quick repo attach
      const starterBtn = target.closest("[data-starter-prompt]");
      if (starterBtn) {
        const promptText = starterBtn.getAttribute("data-starter-prompt") || "";
        const ta = $("composerInput");
        if (ta) {
          ta.value = promptText;
          syncComposerTextareaHeight();
          ta.focus();
        }
        return;
      }
      if (target.closest("[data-open-repo-drawer]")) {
        STATE.ui.repoDrawerOpen = true;
        renderComposer();
        if ($("newPrjRepo")) $("newPrjRepo").focus();
        return;
      }

      // STEP 1 + A3: Inline Approval Card & Sticky Strip buttons (允许 / 始终允许 / 拒绝)
      const decideBtn = target.closest("[data-decide-approval]");
      if (decideBtn) {
        e.stopPropagation();
        const surfaceEl = decideBtn.closest("[data-approval-surface]");
        const surface = surfaceEl ? (surfaceEl.getAttribute("data-approval-surface") || "") : "";
        const approvalId = decideBtn.getAttribute("data-decide-approval");
        const action = decideBtn.getAttribute("data-decide-action");
        const category = decideBtn.getAttribute("data-decide-category") || "";
        const planHash = decideBtn.getAttribute("data-plan-hash") || "";
        const planId = decideBtn.getAttribute("data-plan-id") || "";
        const planVersion = decideBtn.getAttribute("data-plan-version") || "";
        const opHash = decideBtn.getAttribute("data-op-hash") || "";
        const promptReason = decideBtn.getAttribute("data-decide-prompt-reason") === "1";
        const escapedId = (window.CSS && typeof window.CSS.escape === "function") ? window.CSS.escape(approvalId) : approvalId;
        const reasonInput = document.querySelector(`[data-decide-reason="${escapedId}"]`);
        const reason = reasonInput ? reasonInput.value.trim() : "";
        await handleApprovalDecision({
          approvalId,
          action,
          category,
          planHash,
          planId,
          planVersion,
          opHash,
          reason,
          promptReason,
          surface
        });
        return;
      }

      // A3: Jump-to-card from sticky strip or inspector
      const jumpCardBtn = target.closest("[data-jump-approval-card]");
      if (jumpCardBtn) {
        jumpToApprovalCard(jumpCardBtn.getAttribute("data-jump-approval-card"));
        return;
      }

      // A1: Sidebar Working worker sub-row -> switch project if needed & open worker tab in right panel
      const openWorkerRow = target.closest("[data-open-worker-tab]");
      if (openWorkerRow) {
        const wTaskId = openWorkerRow.getAttribute("data-open-worker-tab");
        const wPrjId = openWorkerRow.getAttribute("data-worker-project");
        const wasDraft = STATE.ui.draftNewChat;
        STATE.ui.nav = "tasks";
        STATE.ui.draftNewChat = false;
        if (wPrjId && (wPrjId !== STATE.activeProjectId || wasDraft)) {
          bumpProjectSwitchGen();
          STATE.activeProjectId = wPrjId;
          await loadActiveProjectData();
        }
        if (wTaskId) {
          openWorkerTab(wTaskId, wPrjId || STATE.activeProjectId);
          renderAll();
          await loadActiveTaskInspectorData(wTaskId);
          if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        } else {
          STATE.ui.inspectorCollapsed = false;
          STATE.ui.inspectorTab = "running";
          renderAll();
        }
        return;
      }

      // A2: Right Panel Closable Worker Tabs
      const closeTabBtn = target.closest("[data-close-worker-tab]");
      if (closeTabBtn) {
        e.stopPropagation();
        closeWorkerTab(closeTabBtn.getAttribute("data-close-worker-tab"));
        renderInspector();
        syncLogPollingTimer();
        if (STATE.ui.inspectorTab === "task" && STATE.activeTaskId) {
          await loadActiveTaskInspectorData(STATE.activeTaskId);
          if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        }
        return;
      }
      const actTabBtn = target.closest("[data-activate-worker-tab]");
      if (actTabBtn) {
        const tid = actTabBtn.getAttribute("data-activate-worker-tab");
        openWorkerTab(tid);
        renderChatThread();
        renderInspector();
        syncLogPollingTimer();
        await loadActiveTaskInspectorData(tid);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }

      // Sidebar project selection & group toggle
      const selProj = target.closest("[data-select-project]");
      if (selProj) {
        const nextPid = selProj.getAttribute("data-select-project");
        delete STATE.sidebarTracker.returnedAt[nextPid];
        bumpProjectSwitchGen();
        STATE.ui.nav = "tasks";
        STATE.ui.draftNewChat = false;
        STATE.activeProjectId = nextPid;
        rememberActiveProjectForHome(STATE.activeProjectId);
        STATE.activeTaskId = null;
        await loadActiveProjectData();
        renderAll();
        scrollChatToBottom();
        return;
      }
      const togPrjGrp = target.closest("[data-toggle-prj-group]");
      if (togPrjGrp) {
        const key = togPrjGrp.getAttribute("data-toggle-prj-group");
        STATE.ui.collapsedProjectGroups[key] = !STATE.ui.collapsedProjectGroups[key];
        saveStoredUiState();
        renderSidebar();
        return;
      }

      // S4: Recent folders, toplevel alignment, branch chips, and test command chips
      const recentBtn = target.closest("[data-recent-folder]");
      if (recentBtn) {
        const folderPath = recentBtn.getAttribute("data-recent-folder") || "";
        if ($("newPrjRepo")) $("newPrjRepo").value = folderPath;
        await handleInspectRepoPath(folderPath, { autoFillDefaults: true });
        return;
      }
      const topBtn = target.closest("[data-use-toplevel]");
      if (topBtn) {
        const topPath = topBtn.getAttribute("data-use-toplevel") || "";
        if ($("newPrjRepo")) $("newPrjRepo").value = topPath;
        await handleInspectRepoPath(topPath, { autoFillDefaults: true });
        return;
      }
      const branchChip = target.closest("[data-pick-branch]");
      if (branchChip) {
        const b = branchChip.getAttribute("data-pick-branch") || "";
        if ($("newPrjBaseBranch")) $("newPrjBaseBranch").value = b;
        renderRepoInspectionUi();
        return;
      }
      const testChip = target.closest("[data-pick-test-cmd]");
      if (testChip) {
        const tc = testChip.getAttribute("data-pick-test-cmd") || "none";
        if ($("newPrjTestCmd")) $("newPrjTestCmd").value = tc;
        renderRepoInspectionUi();
        return;
      }

      if (target.closest("#btnWelcomePickLead") || target.closest("#btnCliOpenWizard") || target.closest("[data-open-wizard-step2]")) {
        openWizard(2);
        return;
      }

      if (target.closest("[data-nav-cli]")) {
        STATE.ui.nav = "cli";
        renderAll();
        loadAdapters();
        return;
      }

      if (target.closest("[data-nav-settings]")) {
        STATE.ui.nav = "settings";
        renderAll();
        return;
      }

      if (target.closest("[data-open-daemon-log]") || target.closest("#popDaemonLogs") || target.closest("#btnSettingsViewLog")) {
        closeAllPopovers();
        await openDaemonLogModal();
        return;
      }

      if (target.closest("#btnLoadOlderChat")) {
        await loadOlderChatMessages();
        return;
      }

      if (target.closest("[data-open-outline]")) {
        STATE.ui.inspectorCollapsed = false;
        STATE.ui.inspectorTab = "outline";
        renderSidebar();
        renderInspector();
        await fetchVisibleLeadTurnLogs();
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }

      // Open gate details in Right Inspector Advanced tab (scroll to & expand target approval)
      const openGate = target.closest("[data-open-advanced-gate]");
      if (openGate) {
        const targetAprId = openGate.getAttribute("data-open-advanced-gate") || null;
        STATE.ui.inspectorCollapsed = false;
        STATE.ui.inspectorTab = "advanced";
        STATE.ui.focusAdvancedApprovalId = targetAprId;
        renderSidebar();
        renderInspector();
        return;
      }

      // Task selection (from plan card nested task row or inspector -> A2 opens closable worker tab)
      const selTask = target.closest("[data-select-task]");
      if (selTask) {
        const tid = selTask.getAttribute("data-select-task");
        openWorkerTab(tid);
        renderSidebar();
        renderChatThread();
        renderInspector();
        syncLogPollingTimer();
        await loadActiveTaskInspectorData(tid);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }

      const pickLead = target.closest("[data-pick-lead]");
      if (pickLead) { await handleSelectLead(pickLead.getAttribute("data-pick-lead")); return; }
      const switchModelBtn = target.closest("[data-switch-agent-model]");
      if (switchModelBtn) {
        e.stopPropagation();
        await handleQuickSwitchAgentModel(
          switchModelBtn.getAttribute("data-switch-agent-model"),
          switchModelBtn.getAttribute("data-model")
        );
        return;
      }
      const switchEffortBtn = target.closest("[data-switch-agent-effort]");
      if (switchEffortBtn) {
        e.stopPropagation();
        await handleQuickSwitchAgentEffort(
          switchEffortBtn.getAttribute("data-switch-agent-effort"),
          switchEffortBtn.getAttribute("data-effort")
        );
        return;
      }
      const setDefLead = target.closest("[data-set-default-lead]");
      if (setDefLead) {
        const name = setDefLead.getAttribute("data-set-default-lead");
        closeAllPopovers();
        const res = await apiCall("config.set", { key: "lead.default_agent", value: name });
        if (res.ok) { toast(`已将 ${name} 设为默认 Lead`, "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
        return;
      }

      const prjOp = target.closest("[data-prj-op]");
      if (prjOp) { await handleProjectAction(prjOp.getAttribute("data-prj-op")); return; }

      if (target.closest("#popDaemonStatus")) { closeAllPopovers(); await openDaemonStatusModal(); return; }
      if (target.closest("#popDaemonStart")) { closeAllPopovers(); await handleStartDaemon(); return; }
      if (target.closest("#popDaemonStop")) { closeAllPopovers(); await handleStopDaemon(); return; }
      if (target.closest("#popQuitDesktop") || target.closest("#btnSettingsQuitApp")) {
        openQuitDesktopModal();
        return;
      }
      if (target.closest("#popDaemonReconnect")) {
        closeAllPopovers();
        if (hasApi()) await window.pywebview.api.reconnect_now();
        await refreshConnectionAndAuth();
        if (STATE.connected && STATE.auth.auth_verified) await loadAllWorkspaceData();
        else renderAll();
        return;
      }

      // Settings: Standing grants revoke & refresh
      const revokeGrantBtn = target.closest("[data-revoke-grant-id]");
      if (revokeGrantBtn) {
        await handleRevokeGrant(revokeGrantBtn.getAttribute("data-revoke-grant-id"));
        return;
      }
      if (target.closest("#btnRefreshGrants")) {
        await loadGrants();
        renderSettingsView();
        return;
      }

      // A6: Toggle Grouped Activity Summary vs Raw Logs
      const logViewBtn = target.closest("[data-log-view-mode]");
      if (logViewBtn) {
        STATE.ui.logViewMode = logViewBtn.getAttribute("data-log-view-mode") === "raw" ? "raw" : "grouped";
        renderInspector();
        return;
      }

      // Inspector Advanced / Cancel undispatched plan (plan.cancel)
      const cancelPlanBtn = target.closest("[data-cancel-plan-id]");
      if (cancelPlanBtn) {
        if (!canMutate()) { toast(disabledReason(), "err"); return; }
        const planId = cancelPlanBtn.getAttribute("data-cancel-plan-id");
        const prj = getActiveProject();
        if (!prj || !planId) return;
        const plan = STATE.plans.find(p => p.id === planId);
        openModal(
          `取消执行计划${plan ? ` v${plan.version || 1}` : ""}`,
          `<div class="form-stack">
            <div style="font-size:13px;color:var(--text-secondary);">将取消未派发的计划「${esc((plan && plan.title) || planId)}」及其关联的待确认门控。</div>
            <div class="form-row"><label class="form-label">取消原因</label><input class="compact-input" id="modalCancelPlanReason" type="text" value="用户在高级面板取消计划" /></div>
          </div>`,
          `<button class="btn" id="modalCancel" type="button">返回</button><button class="btn btn-danger-text" id="modalConfirmCancelPlan" type="button">确认取消计划</button>`
        );
        $("modalCancel").onclick = closeModal;
        let cancelPlanSubmitting = false;
        $("modalConfirmCancelPlan").onclick = async () => {
          if (cancelPlanSubmitting) return;
          if (!canMutate()) { toast(disabledReason(), "err"); closeModal(); return; }
          const btn = $("modalConfirmCancelPlan");
          cancelPlanSubmitting = true;
          if (btn) btn.disabled = true;
          try {
            const reason = ($("modalCancelPlanReason").value || "").trim() || "用户在高级面板取消计划";
            const res = await apiCall("plan.cancel", { project_id: prj.id, plan_id: planId, reason });
            closeModal();
            if (res.ok) { toast("计划已取消", "ok"); await loadActiveProjectLight(); }
            else toast(formatErrorMessage(res.error), "err");
          } finally {
            cancelPlanSubmitting = false;
            if (btn) btn.disabled = false;
          }
        };
        return;
      }

      const taskOp = target.closest("[data-task-op]");
      if (taskOp) { await handleTaskOperation(taskOp.getAttribute("data-task-op")); return; }
      if (target.closest("#btnInspectorClose")) { STATE.ui.inspectorCollapsed = true; renderSidebar(); return; }
      if (target.closest("#btnRefreshTask") && STATE.activeTaskId) {
        await loadActiveTaskInspectorData(STATE.activeTaskId);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }
      if (target.closest("#btnNewTaskModal")) { openCreateTaskModal(); return; }
      if (target.closest("#btnManualNewProject")) { openManualNewProjectModal(); return; }
      if (target.closest("#btnAdvancedOpenCli")) {
        STATE.ui.nav = "cli";
        renderAll();
        if (STATE.connected && STATE.auth.auth_verified) loadAdapters({ force: true });
        return;
      }
      if (target.closest("#btnAdvancedAudit") || target.closest("#btnSettingsAudit")) {
        const res = await apiCall("audit.verify", {});
        if ((res && res.ok && res.data) || (res && !res.ok && res.error && res.error.code === "AUDIT_MISMATCH")) {
          const modalTitle = res.ok ? "审计哈希链校验（通过）" : "审计哈希链校验（检测到异常）";
          openModal(modalTitle, renderAuditVerifyModalBodyHtml(res), `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
        } else toast(formatErrorMessage(res && res.error), "err");
        return;
      }

      const logStreamBtn = target.closest("[data-log-stream]");
      if (logStreamBtn) {
        STATE.ui.logStreamFilter = logStreamBtn.getAttribute("data-log-stream");
        if (STATE.activeTaskId) await fetchTaskLogs(STATE.activeTaskId);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }
      if (target.closest("#btnReloadLogs") && STATE.activeTaskId) {
        await fetchTaskLogs(STATE.activeTaskId);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
        return;
      }

      // CLI / Agent management actions
      if (target.closest("#btnProbeAllAdapters")) {
        toast("正在探测本机 AI 工具...", "info");
        const res = await apiCall("adapter.probe", {});
        if (res.ok) { toast("工具探测完成", "ok"); await loadAllWorkspaceData({ includeAdapters: true }); }
        else toast(formatErrorMessage(res.error), "err");
        return;
      }
      const probeBtn = target.closest("[data-adapter-probe]");
      if (probeBtn) {
        const aid = probeBtn.getAttribute("data-adapter-probe");
        const res = await apiCall("adapter.probe", { adapter_id: aid });
        if (res.ok) { toast(`${aid} 探测完成`, "ok"); await loadAllWorkspaceData({ includeAdapters: true }); }
        else toast(formatErrorMessage(res.error), "err");
        return;
      }
      const adShow = target.closest("[data-adapter-show]");
      if (adShow) {
        const aid = adShow.getAttribute("data-adapter-show");
        const [showRes, modRes] = await Promise.all([
          apiCall("adapter.show", { adapter_id: aid }),
          apiCall("adapter.models", { adapter_id: aid })
        ]);
        openModal(`工具详情 · ${aid}`, `<pre class="log-box">${esc(JSON.stringify({ show: showRes.ok ? showRes.data : showRes.error, models: modRes.ok ? modRes.data : modRes.error }, null, 2))}</pre>`, `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
        return;
      }
      if (target.closest("#btnCreateAgentModal")) { openCreateAgentModal(); return; }
      const agEdit = target.closest("[data-agent-edit]");
      if (agEdit) { await openEditAgentModal(agEdit.getAttribute("data-agent-edit")); return; }
      const togLead = target.closest("[data-agent-toggle-lead]");
      if (togLead) {
        const name = togLead.getAttribute("data-agent-toggle-lead");
        const ag = getAgentByName(name);
        if (!ag) return;
        const res = await apiCall("agent.update", { name, can_lead: !ag.can_lead });
        if (res.ok) { delete STATE.agentDetails[name]; toast(`已更新 ${name} Lead 资格`, "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
        return;
      }
      const togDis = target.closest("[data-agent-toggle-disabled]");
      if (togDis) {
        const name = togDis.getAttribute("data-agent-toggle-disabled");
        const ag = getAgentByName(name);
        if (!ag) return;
        const res = await apiCall(ag.disabled ? "agent.enable" : "agent.disable", { name });
        if (res.ok) { delete STATE.agentDetails[name]; toast(`已${ag.disabled ? "启用" : "禁用"} ${name}`, "ok"); await loadAllWorkspaceData(); }
        else toast(formatErrorMessage(res.error), "err");
        return;
      }
      const agShow = target.closest("[data-agent-show]");
      if (agShow) {
        const name = agShow.getAttribute("data-agent-show");
        const res = await apiCall("agent.show", { name });
        openModal(`Agent 详情 · ${name}`, `<pre class="log-box">${esc(JSON.stringify(res.ok ? res.data : res.error, null, 2))}</pre>`, `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
        return;
      }

      // Settings actions
      if (target.closest("#btnSettingsUnlock")) {
        const inp = $("settingsPassInput");
        const pw = inp ? inp.value : "";
        const remEl = $("settingsRememberCred");
        const remember = Boolean(remEl && remEl.checked);
        if (inp) inp.value = "";
        if (remEl) remEl.checked = false;
        await handleUnlockPassphrase(pw, remember);
        renderSettingsView({ force: true });
        return;
      }
      if (target.closest("#btnSettingsLock")) {
        if (hasApi()) await window.pywebview.api.clear_passphrase();
        await refreshConnectionAndAuth();
        resetLockedWorkspaceState();
        toast("已清除内存口令并锁定", "info");
        renderAll({ forceSettings: true });
        return;
      }
      if (target.closest("#btnThemeDark")) { applyTheme("dark"); renderSettingsView({ force: true }); return; }
      if (target.closest("#btnThemeLight")) { applyTheme("light"); renderSettingsView({ force: true }); return; }
      if (target.closest("#btnSettingsStartDaemon")) { await handleStartDaemon(); return; }
      if (target.closest("#btnSettingsStopDaemon")) { await handleStopDaemon(); return; }
      if (target.closest("#btnSettingsDoctor") && hasApi()) {
        try {
          const res = await window.pywebview.api.doctor_report();
          if (res && res.ok) { STATE.doctor = res.data; renderSettingsView({ force: true }); }
          else toast(formatErrorMessage(res && res.error), "err");
        } catch (err) { toast(`环境诊断失败: ${String(err)}`, "err"); }
        return;
      }
      if (target.closest("#btnSettingsPruneWs")) {
        if (!canMutate()) { toast(disabledReason(), "err"); return; }
        const res = await apiCall("workspace.prune", {});
        if (res.ok && res.data) {
          openModal("工作区清理结果", `<pre class="log-box">${esc(JSON.stringify(res.data, null, 2))}</pre>`, `<button class="btn" type="button" data-close-modal="1">关闭</button>`);
          await loadAllWorkspaceData();
        } else toast(formatErrorMessage(res.error), "err");
        return;
      }

      if (target.closest("[data-close-modal]")) { closeModal(); return; }
      if (!target.closest(".popover-menu")) closeAllPopovers();
    });

    document.addEventListener("change", async e => {
      const t = e.target;
      if (t.id === "projectPermissionModeSelect") {
        const prj = getActiveProject();
        if (prj) await handleChangeProjectPermissionMode(prj.id, t.value);
      } else if (t.id === "settingsProjectPermissionMode") {
        const pid = t.getAttribute("data-project-mode-id");
        if (pid) await handleChangeProjectPermissionMode(pid, t.value);
      } else if (t.id === "toggleIncludeRevokedGrants") {
        await loadGrants({ includeRevoked: Boolean(t.checked) });
        renderSettingsView({ force: true });
      } else if (t.id === "inspectorTaskPicker" && t.value) {
        openWorkerTab(t.value);
        await loadActiveTaskInspectorData(t.value);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
      } else if (t.id === "selectLogRun" && STATE.activeTaskId) {
        STATE.ui.logRunId = t.value || null;
        await fetchTaskLogs(STATE.activeTaskId);
        if (STATE.connected && STATE.auth.auth_verified) renderInspector();
      } else if (t.id === "selectCloseToTray") {
        if (hasApi() && typeof window.pywebview.api.set_desktop_prefs === "function") {
          const res = await window.pywebview.api.set_desktop_prefs({ close_to_tray: t.value === "true" });
          if (res && res.ok && res.data) {
            STATE.desktopPrefs = Object.assign({}, STATE.desktopPrefs, res.data);
            toast("托盘偏好已保存", "ok");
            renderSettingsView({ force: true });
            renderAll();
          } else {
            toast(formatErrorMessage(res && res.error), "err");
            renderSettingsView({ force: true });
          }
        }
      } else if (t.id === "selectDefaultLead") {
        if (!canMutate()) { toast(disabledReason(), "err"); renderSettingsView({ force: true }); return; }
        const val = t.value || null;
        const res = await apiCall("config.set", { key: "lead.default_agent", value: val });
        if (res.ok) { toast(`默认 Lead 已更新为 ${val || "未设置"}`, "ok"); await loadAllWorkspaceData(); }
        else { toast(formatErrorMessage(res.error), "err"); renderSettingsView({ force: true }); }
      } else if (t.hasAttribute("data-config-key")) {
        if (!canMutate()) { toast(disabledReason(), "err"); renderSettingsView({ force: true }); return; }
        const key = t.getAttribute("data-config-key");
        const valType = t.getAttribute("data-config-type");
        const value = valType === "bool" ? (t.value === "true") : t.value;
        const res = await apiCall("config.set", { key, value });
        if (res.ok) { toast("配置已保存", "ok"); await loadAllWorkspaceData(); }
        else { toast(formatErrorMessage(res.error), "err"); renderSettingsView({ force: true }); }
      }
    });

    if ($("settingsPageScroll")) {
      $("settingsPageScroll").addEventListener("focusout", () => {
        if (!settingsRenderDeferred || STATE.ui.nav !== "settings") return;
        window.setTimeout(() => {
          if (settingsRenderDeferred && STATE.ui.nav === "settings") {
            renderSettingsView();
          }
        }, 0);
      });
    }
  }

  function formatAuditMismatchTag(key) {
    const k = String(key || "");
    if (k.startsWith("project_plan_gate:")) return "计划门控模式 (plan_gate_mode) 异常";
    if (k.startsWith("project_mode:")) return "权限模式 (permission_mode) 异常";
    if (k.startsWith("grant:")) return "永久授权 (standing_grants) 异常";
    if (k.startsWith("plan:") || k.startsWith("plan_tasks:") || k.startsWith("task_plan:")) return "计划与派发绑定异常";
    if (k.startsWith("chat:")) return "对话记录异常";
    if (k.startsWith("project:")) return "项目状态异常";
    if (k.startsWith("task:")) return "任务状态异常";
    if (k.startsWith("credential:")) return "凭据状态异常";
    if (k.startsWith("i4:")) return "状态机不变量异常";
    return k || "状态异常";
  }

  function renderAuditVerifyModalBodyHtml(res) {
    if (res && res.ok && res.data) {
      const d = res.data;
      const summaryLine = `已校验 ${Number(d.events_verified || 0)} 条事件、${Number(d.approvals_verified || 0)} 条审批（直连 ${Number(d.direct_decisions || 0)} · 转发 ${Number(d.relayed_decisions || 0)} · 委派 ${Number(d.delegated_decisions || 0)}）`;
      return `
        <div class="inspector-note-box" data-audit-status="ok" style="margin-bottom:10px;">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="status-pill green">校验通过</span>
            <strong>事件哈希链、审批签名、plan_gate_mode 与 permission_mode 历史一致</strong>
          </div>
          <div style="margin-top:6px;font-size:12.5px;color:var(--text-secondary);">${esc(summaryLine)}</div>
        </div>
        <pre class="log-box">${esc(JSON.stringify(d, null, 2))}</pre>`;
    }
    const err = (res && res.error) || {};
    const details = (err.details && typeof err.details === "object") ? err.details : {};
    const brokenEvents = Array.isArray(details.broken_event_seqs) ? details.broken_event_seqs : [];
    const brokenApprovals = Array.isArray(details.broken_approval_ids) ? details.broken_approval_ids : [];
    const stateMismatches = Array.isArray(details.state_mismatches) ? details.state_mismatches : [];
    const reasons = Array.isArray(details.reasons) ? details.reasons : [];
    const mismatchTagsHtml = stateMismatches.length
      ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;">${stateMismatches.map(k => `<span class="status-pill red" data-audit-mismatch-key="${esc(k)}">${esc(formatAuditMismatchTag(k))} · ${esc(k)}</span>`).join("")}</div>`
      : "";
    const reasonsHtml = reasons.length
      ? `<ul style="margin:8px 0 0 18px;padding:0;font-size:12.5px;">${reasons.map(r => `<li style="margin-bottom:4px;">${esc(r)}</li>`).join("")}</ul>`
      : "";
    return `
      <div class="inspector-note-box amber" data-audit-status="mismatch" style="margin-bottom:10px;">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <span class="status-pill red">校验失败</span>
          <strong>${esc(err.message || ERROR_LABELS.AUDIT_MISMATCH)}</strong>
        </div>
        <div style="margin-top:6px;font-size:12.5px;color:var(--text-secondary);">
          异常事件 ${brokenEvents.length} 条 · 异常审批 ${brokenApprovals.length} 条 · 状态不一致 ${stateMismatches.length} 项
        </div>
        ${mismatchTagsHtml}
        ${reasonsHtml}
      </div>
      <pre class="log-box">${esc(JSON.stringify(details, null, 2))}</pre>`;
  }

  // =========================================================================
  // Exports (Node unit tests) & Browser Startup
  // =========================================================================
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      STATE,
      ALWAYS_ALLOW_CATEGORIES,
      MAX_RETAINED_EVENTS,
      EVENT_TAIL_FETCH_LIMIT,
      APPROVAL_LIST_LIMIT,
      ACTIVE_RUN_LIST_LIMIT,
      PROJECT_RUN_LIST_LIMIT,
      APPROVAL_SURFACE_SETTLE_MS,
      APPROVAL_RESOLVED_PLACEHOLDER_MS,
      REFRESH_DEBOUNCE_MS,
      REFRESH_DEBOUNCE_MAX_WAIT_MS,
      bumpProjectSwitchGen,
      bumpSidebarLoadSeq,
      setApprovals,
      setRuns,
      getApprovalFromListById,
      getApprovalsForProject,
      getApprovalsForTask,
      getRunsForTask,
      getActiveRunsForProject,
      recordApprovalSurfaceLayout,
      resetApprovalSurfaceGuards,
      checkApprovalClickGuard,
      rememberRecentlyResolvedApproval,
      clearRecentlyResolvedApprovals,
      orderStripItemsStably,
      orderInspectorItemsStably,
      collectOlderChatApprovalIds,
      reconcileChatApprovalsWithList,
      fetchBoundedApprovals,
      fetchBoundedRuns,
      loadAllWorkspaceData,
      loadActiveProjectData,
      loadActiveProjectLight,
      loadSidebarLight,
      scheduleRefresh,
      flushScheduledRefresh,
      handleIncomingEvents,
      formatErrorMessage,
      getApprovalOpType,
      isExternalApprovalOp,
      resolveApprovalPolicy,
      sanitizeSingleLineText,
      isRelayedChatMessage,
      formatRelayedBadgeText,
      renderUserChatMessageHtml,
      formatAuditMismatchTag,
      renderAuditVerifyModalBodyHtml,
      isTaskTerminalForApproval,
      expectedTaskStatusesForApproval,
      isTaskProgressedForApproval,
      isApprovalExpiredLocally,
      getApprovalUnapprovableReason,
      isDismissOnlyApproval,
      formatApprovalReasonBadgeLabel,
      getApprovalUndecidableReason,
      formatUndecidableReasonHint,
      formatUnapprovableReasonHint,
      formatSweptApprovalsSuffix,
      formatApprovalDecisionToast,
      buildAutoModeConfirmCopy,
      getApprovalCardActions,
      shouldRenderApprovalActions,
      isSecondarySurfaceApprovalCandidate,
      filterInspectorAdvancedApprovals,
      resolveHydratedApproval,
      resolvePlanApproval,
      markApprovalsSuppressedLocally,
      formatSingleGrantScope,
      formatAlwaysAllowEffectHint,
      renderApprovalButtonsFooterHtml,
      renderGateCardHtml,
      renderPlanCardHtml,
      renderChatThread,
      renderInspector,
      renderInspectorAdvanced,
      renderComposerApprovalStrip,
      shouldDeferSettingsRender,
      renderSettingsView,
      renderAll,
      snapshotApprovalReasonInputs,
      restoreApprovalReasonInputs,
      openModal,
      closeModal,
      showConfirmDialog,
      closeConfirmModal,
      promptOptionalRejectReason,
      openCreateTaskModal,
      handleComposerSubmit,
      handleApprovalDecision,
      handleCancelQueuedMessage,
      resetLockedWorkspaceState,
      getUnresolvedLeadFailure,
      getPendingUserMessageInfo,
      renderLeadTurnAndWaitStatusHtml,
      fetchVisibleLeadTurnLogs,
      fetchActiveDiffData,
      fetchTaskLogs,
      loadActiveTaskInspectorData,
      renderInspectorDiff,
      renderWizard,
      bindStaticHandlers,
      parseActivityFromLogText,
      mergeBoundedEvents
    };
  }
  if (typeof document !== "undefined" && typeof window !== "undefined") {
    bindStaticHandlers();
    if (hasApi()) {
      bootstrapBridge();
    } else {
      renderAll();
      window.addEventListener("pywebviewready", bootstrapBridge, { once: true });
    }
  }
})();
