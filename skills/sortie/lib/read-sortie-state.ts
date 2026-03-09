import { readdir, readFile, stat, exists } from "node:fs/promises";
import { join, basename, dirname } from "node:path";

export interface AgentState {
  ticketId: string;
  title: string;
  model: string;
  status: "WORKING" | "PRE-REVIEW" | "DONE";
  lastProgress: string[];
  branch: string;
  elapsedTime: string;
  worktreePath: string;
  isSubAgent: boolean;
  parentTicket?: string;
}

export interface DashboardState {
  agents: AgentState[];
  summary: {
    total: number;
    working: number;
    preReview: number;
    done: number;
  };
  timestamp: string;
}

// Derive repo root from this file's location:
// <repo>/.claude/skills/sortie/lib/read-sortie-state.ts
const WORKTREES_ROOT = join(dirname(import.meta.path), "..", "..", "..", "..", ".claude", "worktrees");

async function readTextFile(path: string): Promise<string> {
  try {
    return (await readFile(path, "utf-8")).trim();
  } catch {
    return "";
  }
}

async function fileExists(path: string): Promise<boolean> {
  try {
    return await exists(path);
  } catch {
    return false;
  }
}

function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function extractTitle(directive: string): string {
  const match = directive.match(/\*\*Title\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
}

function extractTicketId(directive: string): string {
  const match = directive.match(/\*\*ID\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
}

async function readAgent(sortieDir: string, worktreePath: string, isSubAgent = false, parentTicket?: string): Promise<AgentState | null> {
  const directivePath = join(sortieDir, "directive.md");
  if (!(await fileExists(directivePath))) return null;

  const directive = await readTextFile(directivePath);
  const model = await readTextFile(join(sortieDir, "model.txt"));
  const progressRaw = await readTextFile(join(sortieDir, "progress.md"));

  const hasPostReview = await fileExists(join(sortieDir, "post-review.done"));
  const hasPreReview = await fileExists(join(sortieDir, "pre-review.done"));

  const status: AgentState["status"] = hasPostReview
    ? "DONE"
    : hasPreReview
      ? "PRE-REVIEW"
      : "WORKING";

  const progressLines = progressRaw
    .split("\n")
    .filter((l) => l.trim().length > 0);
  const lastProgress = progressLines.slice(-5);

  // Get branch from git
  let branch = "";
  try {
    const proc = Bun.spawnSync(["git", "branch", "--show-current"], {
      cwd: worktreePath,
    });
    branch = new TextDecoder().decode(proc.stdout).trim();
  } catch {
    branch = "unknown";
  }

  // Elapsed time from worktree directory creation
  let elapsedTime = "0s";
  try {
    const dirStat = await stat(sortieDir);
    const elapsed = Date.now() - dirStat.birthtimeMs;
    elapsedTime = formatElapsed(elapsed);
  } catch {}

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
  };
}

export async function readSortieState(): Promise<DashboardState> {
  const agents: AgentState[] = [];

  let entries: string[] = [];
  try {
    entries = await readdir(WORKTREES_ROOT);
  } catch {
    return {
      agents: [],
      summary: { total: 0, working: 0, preReview: 0, done: 0 },
      timestamp: new Date().toISOString(),
    };
  }

  for (const entry of entries) {
    const worktreePath = join(WORKTREES_ROOT, entry);
    const sortieDir = join(worktreePath, ".sortie");

    // Check main agent
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

  return {
    agents,
    summary,
    timestamp: new Date().toISOString(),
  };
}
