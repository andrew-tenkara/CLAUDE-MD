const grid = document.getElementById('agent-grid');
const chipsRow = document.getElementById('stat-chips');
const headerStats = document.getElementById('header-stats');

// Track PR URLs by worktreePath (unique per agent)
const prUrls = {};

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function statusClass(status) {
  return status === 'WORKING' ? 'working' : status === 'PRE-REVIEW' ? 'pre-review' : 'done';
}

function modelClass(model) {
  const m = model.toLowerCase();
  if (m.includes('sonnet')) return 'model-sonnet';
  if (m.includes('haiku')) return 'model-haiku';
  if (m.includes('opus')) return 'model-opus';
  return 'model-unknown';
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

function renderHeaderStats(summary) {
  headerStats.textContent = `${summary.total} agents  ${summary.working} working  ${summary.preReview} pre-review  ${summary.done} done`;
}

function renderCard(agent) {
  const cls = statusClass(agent.status);
  const key = agentKey(agent);
  const lastLine =
    agent.lastProgress.length > 0
      ? esc(agent.lastProgress[agent.lastProgress.length - 1])
      : 'No progress yet';

  const subBadge = agent.isSubAgent ? `<span class="sub-agent-badge">sub</span>` : '';

  const safeKey = esc(key);
  let actionBtn = '';
  const prUrl = prUrls[key];
  const isGithubUrl =
    prUrl &&
    (() => {
      try {
        return new URL(prUrl).hostname.endsWith('github.com');
      } catch {
        return false;
      }
    })();
  if (isGithubUrl) {
    actionBtn = `<a href="${esc(prUrl)}" target="_blank" rel="noopener" class="btn-pr-link">View PR</a>`;
  } else if (agent.status === 'DONE') {
    actionBtn = `<button class="btn btn-pr" data-key="${safeKey}" data-action="pr">Open PR</button>`;
  } else {
    actionBtn = `<button class="btn btn-kill" data-key="${safeKey}" data-action="kill">Kill</button>`;
  }

  return `
    <div class="agent-card">
      <div class="card-status-strip ${cls}"></div>
      <div class="card-body">
        <div class="card-top">
          <span class="ticket-id ${cls}">${esc(agent.ticketId)}${subBadge}</span>
          <span class="model-badge ${modelClass(agent.model)}">${esc(agent.model)}</span>
        </div>
        <div class="card-title">${esc(agent.title)}</div>
        <div class="card-progress">${lastLine}</div>
        <div class="card-branch">${esc(agent.branch)}</div>
        <div class="card-footer">
          <span class="elapsed">${esc(agent.elapsedTime)}</span>
          ${actionBtn}
        </div>
      </div>
    </div>
  `;
}

// Current state reference for lookups
let currentState = null;

function findAgentByKey(key) {
  if (!currentState) return null;
  return currentState.agents.find((a) => agentKey(a) === key) || null;
}

function render(state) {
  currentState = state;
  renderChips(state.summary);
  renderHeaderStats(state.summary);

  if (state.agents.length === 0) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1">
        <h2>No active agents</h2>
        <p>Waiting for sortie agents to appear in .claude/worktrees/</p>
      </div>
    `;
    return;
  }

  // Sort: WORKING first, then PRE-REVIEW, then DONE
  const order = { WORKING: 0, 'PRE-REVIEW': 1, DONE: 2 };
  const sorted = [...state.agents].sort((a, b) => order[a.status] - order[b.status]);

  grid.innerHTML = sorted.map(renderCard).join('');
}

// Event delegation for buttons
grid.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const key = btn.dataset.key;
  const action = btn.dataset.action;
  const agent = findAgentByKey(key);
  if (!agent) return;

  if (action === 'kill') {
    btn.textContent = 'Killing...';
    btn.disabled = true;
    try {
      const res = await fetch(`/api/kill?path=${encodeURIComponent(key)}`, {
        method: 'POST',
      });
      const data = await res.json();
      if (!data.ok) {
        btn.textContent = data.error || 'Failed';
        setTimeout(() => {
          btn.textContent = 'Kill';
          btn.disabled = false;
        }, 3000);
      }
    } catch {
      btn.textContent = 'Error';
      setTimeout(() => {
        btn.textContent = 'Kill';
        btn.disabled = false;
      }, 3000);
    }
  }

  if (action === 'pr') {
    btn.textContent = 'Creating PR...';
    btn.disabled = true;
    try {
      const res = await fetch(`/api/pr?path=${encodeURIComponent(key)}`, {
        method: 'POST',
      });
      const data = await res.json();
      if (data.ok && data.url) {
        prUrls[key] = data.url;
      } else {
        btn.textContent = data.error || 'Failed';
        setTimeout(() => {
          btn.textContent = 'Open PR';
          btn.disabled = false;
        }, 3000);
      }
    } catch {
      btn.textContent = 'Error';
      setTimeout(() => {
        btn.textContent = 'Open PR';
        btn.disabled = false;
      }, 3000);
    }
  }
});

// SSE connection with auto-reconnect
function connectSSE() {
  const es = new EventSource('/events');

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
