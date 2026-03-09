/**
 * read-sortie-state.mjs — Read sortie agent state from all worktrees
 *
 * Returns agent objects with status, model, progress, context usage,
 * JSONL metrics, and sub-agent information. Used by dashboard-server.mjs.
 */

import { readdir, readFile, stat, access } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { parseJsonlMetrics } from "./parse-jsonl-metrics.mjs";

const execFileAsync = promisify(execFile);

// Derive repo root from this file's location:
// <repo>/.claude/skills/sortie/lib/read-sortie-state.mjs
const __lib_dirname = dirname(fileURLToPath(import.meta.url));
const WORKTREES_ROOT = join(__lib_dirname, "..", "..", "..", "..", ".claude", "worktrees");

async function readTextFile(path) {
  try {
    return (await readFile(path, "utf-8")).trim();
  } catch {
    return "";
  }
}

async function readJsonSafe(filePath) {
  try {
    const content = await readFile(filePath, 'utf8');
    return JSON.parse(content);
  } catch {
    return null;
  }
}

async function fileExists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

function formatElapsed(ms) {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function extractTasks(directive, progressRaw) {
  const tasks = [];
  const checkboxPattern = /^- \[( |x)\] (.+)$/gim;
  let match;
  while ((match = checkboxPattern.exec(directive)) !== null) {
    tasks.push({ text: match[2].trim(), checked: match[1].toLowerCase() === "x" });
  }
  if (tasks.length === 0) return [];

  const progressLines = progressRaw.split("\n").filter((l) => l.trim().length > 0);

  return tasks.map((task) => {
    if (task.checked) return { text: task.text, status: "done" };

    // Check if task keywords appear in any progress line
    const keywords = task.text
      .toLowerCase()
      .split(/\W+/)
      .filter((w) => w.length > 4);
    const mentioned =
      keywords.length > 0 &&
      progressLines.some((line) => {
        const lower = line.toLowerCase();
        return keywords.some((kw) => lower.includes(kw));
      });

    return { text: task.text, status: mentioned ? "in-progress" : "pending" };
  });
}

function extractTitle(directive) {
  const match = directive.match(/\*\*Title\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
}

function extractTicketId(directive) {
  const match = directive.match(/\*\*ID\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
}

/**
 * Read context usage from context.json (ENG-133 context % tracking).
 */
async function readContext(sortieDir) {
  const ctx = await readJsonSafe(join(sortieDir, 'context.json'));
  if (!ctx) {
    return {
      used_percentage: null,
      context_window_size: null,
      total_input_tokens: null,
      total_output_tokens: null,
      model: null,
      timestamp: null,
      stale: true,
    };
  }

  // Mark as stale if context.json hasn't been updated in 60s
  const age = Math.floor(Date.now() / 1000) - (ctx.timestamp || 0);
  return { ...ctx, stale: age > 60 };
}

async function readAgent(sortieDir, worktreePath, isSubAgent = false, parentTicket) {
  const directivePath = join(sortieDir, "directive.md");
  if (!(await fileExists(directivePath))) return null;

  const directive = await readTextFile(directivePath);
  const model = await readTextFile(join(sortieDir, "model.txt"));
  const progressRaw = await readTextFile(join(sortieDir, "progress.md"));

  const hasPostReview = await fileExists(join(sortieDir, "post-review.done"));
  const hasPreReview = await fileExists(join(sortieDir, "pre-review.done"));

  const status = hasPostReview ? "DONE" : hasPreReview ? "PRE-REVIEW" : "WORKING";

  const progressLines = progressRaw.split("\n").filter((l) => l.trim().length > 0);
  const lastProgress = progressLines.slice(-5);

  let branch = "";
  try {
    const { stdout } = await execFileAsync("git", ["branch", "--show-current"], {
      cwd: worktreePath,
      encoding: "utf-8",
      timeout: 5000,
    });
    branch = stdout.trim();
  } catch {
    branch = "unknown";
  }

  let elapsedTime = "0s";
  try {
    const dirStat = await stat(sortieDir);
    const origin = dirStat.birthtimeMs > 0 ? dirStat.birthtimeMs : dirStat.mtimeMs;
    const elapsed = Date.now() - origin;
    elapsedTime = formatElapsed(elapsed);
  } catch {}

  // ENG-133: context % tracking from statusline API
  const context = await readContext(sortieDir);

  // ENG-134: JSONL metrics (token usage, tool calls, errors, timeline)
  const jsonlMetrics = await parseJsonlMetrics(worktreePath).catch(() => null);

  return {
    ticketId: extractTicketId(directive),
    title: extractTitle(directive),
    model: model || "unknown",
    status,
    lastProgress,
    branch,
    elapsedTime,
    worktreePath,
    isSubAgent,
    parentTicket,
    tasks: extractTasks(directive, progressRaw),
    context,
    jsonlMetrics,
  };
}

/**
 * Read state for all active sortie agents.
 * @param {string} [targetTicket] — optional ticket ID to filter to
 * @returns {Promise<Object>} { agents, summary, timestamp }
 */
export async function readSortieState(targetTicket) {
  const agents = [];

  let entries = [];
  try {
    entries = await readdir(WORKTREES_ROOT);
  } catch {
    return {
      agents: [],
      summary: { total: 0, working: 0, preReview: 0, done: 0 },
      timestamp: new Date().toISOString(),
    };
  }

  if (targetTicket) {
    entries = entries.filter((d) => d === targetTicket);
  }

  for (const entry of entries) {
    if (entry.startsWith(".")) continue;
    const worktreePath = join(WORKTREES_ROOT, entry);
    const sortieDir = join(worktreePath, ".sortie");

    const agent = await readAgent(sortieDir, worktreePath);
    if (agent) agents.push(agent);

    // Check sub-agents (sub-* directories)
    try {
      const subEntries = await readdir(worktreePath);
      for (const sub of subEntries) {
        if (!sub.startsWith("sub-")) continue;
        const subPath = join(worktreePath, sub);
        const subSortie = join(subPath, ".sortie");
        const subAgent = await readAgent(subSortie, subPath, true, agent?.ticketId);
        if (subAgent) agents.push(subAgent);
      }
    } catch {}
  }

  const summary = {
    total: agents.length,
    working: agents.filter((a) => a.status === "WORKING").length,
    preReview: agents.filter((a) => a.status === "PRE-REVIEW").length,
    done: agents.filter((a) => a.status === "DONE").length,
  };

  return { agents, summary, timestamp: new Date().toISOString() };
}
