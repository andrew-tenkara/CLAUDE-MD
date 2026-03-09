/** Sortie Dashboard — SSE client with context meters + JSONL metrics + token badges */

// Context meter helpers (from ENG-133)
function meterColor(pct) {
  if (pct >= 80) return 'red';
  if (pct >= 50) return 'yellow';
  return 'green';
}

function renderContextMeter(ctx, compact) {
  if (!ctx) ctx = {};
  const pct = ctx.used_percentage;
  if (pct === null || pct === undefined) {
    if (compact) return '<span class="sub-pct">--</span>';
    return '<div class="context-meter"><div class="meter-label"><span>Context</span><span class="pct">N/A</span></div><div class="meter-bar"><div class="meter-fill green" style="width:0%"></div></div><div class="meter-stale">No statusline data</div></div>';
  }

  const color = meterColor(pct);

  if (compact) {
    return `<div class="mini-meter"><div class="meter-fill ${color}" style="width:${pct}%"></div></div><span class="sub-pct ${color}">${pct}%</span>`;
  }

  const staleNote = ctx.stale ? '<div class="meter-stale">Stale (agent may be idle)</div>' : '';
  return `<div class="context-meter"><div class="meter-label"><span>Context</span><span class="pct ${color}">${pct}%</span></div><div class="meter-bar"><div class="meter-fill ${color}" style="width:${pct}%"></div></div>${staleNote}</div>`;
}

function renderSubAgents(agent) {
  if (!agent.subAgents || agent.subAgents.length === 0) return '';
  const subs = agent.subAgents
    .map(
      (s) =>
        `<div class="sub-agent"><span class="sub-name">${esc(s.name)}</span><span class="status-badge ${s.status}">${s.status}</span>${renderContextMeter(s.context, true)}</div>`
    )
    .join('');
  return `<div class="sub-agents"><h4>Sub-agents</h4>${subs}</div>`;
}

// Token formatting helper — converts raw count to compact "12.4k" format
function formatTokenCount(count) {
  if (!count || count === 0) return "0";
  if (count >= 1_000_000) return (count / 1_000_000).toFixed(1) + "M";
  if (count >= 1_000) return (count / 1_000).toFixed(1) + "k";
  return String(count);
}

const mainView = document.getElementById("main-view");
const chipsRow = document.getElementById("stat-chips");
const headerStats = document.getElementById("header-stats");
const viewToggle = document.getElementById("view-toggle");

// Track PR URLs by worktreePath (unique per agent)
const prUrls = {};

let currentView = "agent"; // "agent" | "task"
let currentState = null;

function esc(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

function statusClass(status) {
  return status === "WORKING"
    ? "working"
    : status === "PRE-REVIEW"
      ? "pre-review"
      : "done";
}

const MODEL_FAMILIES = [
  { match: "opus",   css: "model-opus",    family: "opus" },
  { match: "sonnet", css: "model-sonnet",  family: "sonnet" },
  { match: "haiku",  css: "model-haiku",   family: "haiku" },
];

function modelLookup(model) {
  const m = model.toLowerCase();
  return MODEL_FAMILIES.find((f) => m.includes(f.match));
}

function modelClass(model) {
  return modelLookup(model)?.css || "model-unknown";
}

function modelFamily(model) {
  return modelLookup(model)?.family || "other";
}

function agentKey(agent) {
  return agent.worktreePath;
}

function renderChips(summary) {
  chipsRow.innerHTML = `
    <span class="chip chip-working">${summary.working} Working</span>
    <span class="chip chip-pre-review">${summary.preReview} Pre-review</span>
    <span class="chip chip-done">${summary.done} Done</span>
    <span class="chip chip-total">${summary.total} Total</span>
  `;
}

function renderHeaderStats(summary, agents) {
  const agentLine = `${summary.total} agents  ${summary.working} working  ${summary.preReview} pre-review  ${summary.done} done`;

  // Aggregate tokens by model family across all agents
  const byModel = {};
  for (const agent of agents) {
    const m = agent.jsonlMetrics;
    if (!m || (!m.inputTokens && !m.outputTokens)) continue;
    const family = modelFamily(agent.model);
    if (!byModel[family]) byModel[family] = { input: 0, output: 0 };
    byModel[family].input += m.inputTokens || 0;
    byModel[family].output += m.outputTokens || 0;
  }

  const tokenParts = Object.entries(byModel)
    .sort((a, b) => (b[1].input + b[1].output) - (a[1].input + a[1].output))
    .map(([family, t]) => `${family}: ${formatTokenCount(t.input)} in / ${formatTokenCount(t.output)} out`);

  const tokenLine = tokenParts.length > 0 ? tokenParts.join(" | ") : "";

  headerStats.innerHTML = tokenLine
    ? `<div>${esc(agentLine)}</div><div class="header-token-stats">${esc(tokenLine)}</div>`
    : esc(agentLine);
}

function renderTokenBadge(m) {
  if (!m || (!m.inputTokens && !m.outputTokens)) return "";
  return `<span class="metric-tokens">${formatTokenCount(m.inputTokens)} in / ${formatTokenCount(m.outputTokens)} out</span>`;
}

function renderMetrics(m) {
  if (!m) return "";

  // Top 3 tools by call count
  const toolEntries = Object.entries(m.toolCallCounts || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);
  const toolsHtml = toolEntries
    .map(([name, count]) => `<span class="metric-tool">${esc(name)}<span class="metric-count">${count}</span></span>`)
    .join("");

  const errBadge = m.errorCount > 0
    ? `<span class="metric-errors">${m.errorCount} err</span>`
    : "";
  const spawnBadge = m.agentSpawns > 0
    ? `<span class="metric-spawns">${m.agentSpawns} sub</span>`
    : "";
  const tokenBadge = renderTokenBadge(m);

  return `
    <div class="card-metrics">
      <span class="metric-total">${m.totalToolCalls} calls</span>
      ${errBadge}${spawnBadge}${tokenBadge}
      <span class="metric-tools">${toolsHtml}</span>
    </div>
  `;
}

function renderCard(agent) {
  const cls = statusClass(agent.status);
  const key = agentKey(agent);
  const lastLine =
    agent.lastProgress.length > 0
      ? esc(agent.lastProgress[agent.lastProgress.length - 1])
      : "No progress yet";

  const subBadge = agent.isSubAgent
    ? `<span class="sub-agent-badge">sub</span>`
    : "";

  const isAlert = agent.context && agent.context.used_percentage != null && agent.context.used_percentage >= 80;
  const alertClass = isAlert ? ' alert-high' : '';

  const safeKey = esc(key);
  let actionBtn = "";
  const prUrl = prUrls[key];
  const isGithubUrl = prUrl && (() => {
    try { const u = new URL(prUrl); return u.protocol === "https:" && (u.hostname === "github.com" || u.hostname === "www.github.com"); } catch { return false; }
  })();
  if (isGithubUrl) {
    actionBtn = `<a href="${esc(prUrl)}" target="_blank" rel="noopener" class="btn-pr-link">View PR</a>`;
  } else if (agent.status === "DONE") {
    actionBtn = `<button class="btn btn-pr" data-key="${safeKey}" data-action="pr">Open PR</button>`;
  } else {
    actionBtn = `<button class="btn btn-kill" data-key="${safeKey}" data-action="kill">Kill</button>`;
  }

  return `
    <div class="agent-card${alertClass}">
      <div class="card-status-strip ${cls}"></div>
      <div class="card-body">
        <div class="card-top">
          <span class="ticket-id ${cls}">${esc(agent.ticketId)}${subBadge}</span>
          <span class="model-badge ${modelClass(agent.model)}">${esc(agent.model)}</span>
        </div>
        <div class="card-title">${esc(agent.title)}</div>
        ${renderContextMeter(agent.context, false)}
        <div class="card-progress">${lastLine}</div>
        ${renderMetrics(agent.jsonlMetrics)}
        ${renderSubAgents(agent)}
        <div class="card-branch">${esc(agent.branch)}</div>
        <div class="card-footer">
          <span class="elapsed">${esc(agent.elapsedTime)}</span>
          ${actionBtn}
        </div>
      </div>
    </div>
  `;
}

function renderTaskCard(task) {
  return `
    <div class="task-card">
      <div class="task-card-text">${esc(task.text)}</div>
      <div class="task-card-agent">${esc(task.agentTicketId)}</div>
    </div>
  `;
}

function renderKanbanColumn(title, tasks, statusKey) {
  const cards = tasks.length > 0
    ? tasks.map(renderTaskCard).join("")
    : `<div class="task-card-empty">No tasks</div>`;
  return `
    <div class="kanban-column">
      <div class="kanban-column-header">
        <span class="kanban-column-title ${statusKey}">${esc(title)}</span>
        <span class="kanban-count">${tasks.length}</span>
      </div>
      <div class="kanban-cards">${cards}</div>
    </div>
  `;
}

function renderAgentView(state) {
  mainView.className = "agent-grid";

  if (state.agents.length === 0) {
    mainView.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1">
        <h2>No active agents</h2>
        <p>Waiting for sortie agents to appear in .claude/worktrees/</p>
      </div>
    `;
    return;
  }

  const order = { WORKING: 0, "PRE-REVIEW": 1, DONE: 2 };
  const sorted = [...state.agents].sort(
    (a, b) => order[a.status] - order[b.status]
  );

  mainView.innerHTML = sorted.map(renderCard).join("");
}

function renderTaskView(state) {
  mainView.className = "kanban-board";

  const allTasks = state.agents.flatMap((agent) =>
    (agent.tasks || []).map((task) => ({ ...task, agentTicketId: agent.ticketId }))
  );

  if (allTasks.length === 0) {
    mainView.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1">
        <h2>No tasks found</h2>
        <p>Tasks are parsed from <code>- [ ]</code> checkbox items in directive.md files.</p>
      </div>
    `;
    return;
  }

  const pending = allTasks.filter((t) => t.status === "pending");
  const inProgress = allTasks.filter((t) => t.status === "in-progress");
  const done = allTasks.filter((t) => t.status === "done");

  mainView.innerHTML =
    renderKanbanColumn("Pending", pending, "pending") +
    renderKanbanColumn("In Progress", inProgress, "in-progress") +
    renderKanbanColumn("Done", done, "done");
}

function findAgentByKey(key) {
  if (!currentState) return null;
  return currentState.agents.find((a) => agentKey(a) === key) || null;
}

function render(state) {
  currentState = state;
  renderChips(state.summary);
  renderHeaderStats(state.summary, state.agents);

  if (currentView === "agent") {
    renderAgentView(state);
  } else {
    renderTaskView(state);
  }
}

// View toggle
viewToggle.addEventListener("click", (e) => {
  const btn = e.target.closest(".toggle-btn");
  if (!btn) return;
  const view = btn.dataset.view;
  if (view === currentView) return;

  currentView = view;
  viewToggle.querySelectorAll(".toggle-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });

  if (currentState) render(currentState);
});

// Event delegation for agent card buttons
mainView.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const key = btn.dataset.key;
  const action = btn.dataset.action;
  const agent = findAgentByKey(key);
  if (!agent) return;

  async function btnAction(endpoint, label, onSuccess) {
    btn.textContent = `${label}...`;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/${endpoint}?path=${encodeURIComponent(key)}`, { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        if (onSuccess) { onSuccess(data); } else { btn.textContent = label; btn.disabled = false; }
        return;
      }
      btn.textContent = data.error || "Failed";
    } catch {
      btn.textContent = "Error";
    }
    setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 3000);
  }

  if (action === "kill") {
    btnAction("kill", "Kill");
  }

  if (action === "pr") {
    btnAction("pr", "Open PR", (data) => {
      if (data.url) { prUrls[key] = data.url; if (currentState) render(currentState); }
    });
  }
});

// SSE connection with auto-reconnect
function connectSSE() {
  const es = new EventSource("/events");

  es.onmessage = (event) => {
    try {
      const state = JSON.parse(event.data);
      render(state);
    } catch {}
  };

  es.onerror = () => {
    es.close();
    setTimeout(connectSSE, 2000);
  };
}

connectSSE();
