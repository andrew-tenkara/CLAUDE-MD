import { readdir, readFile, stat, access } from "node:fs/promises";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

async function getWorktreesRoot() {
  const { stdout } = await execFileAsync("git", ["rev-parse", "--show-toplevel"], {
    encoding: "utf-8",
    timeout: 5000,
  });
  return join(stdout.trim(), ".claude", "worktrees");
}

async function readTextFile(path) {
  try {
    return (await readFile(path, "utf-8")).trim();
  } catch {
    return "";
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

function extractTitle(directive) {
  const match = directive.match(/\*\*Title\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
}

function extractTicketId(directive) {
  const match = directive.match(/\*\*ID\*\*:\s*(.+)/);
  return match ? match[1].trim() : "Unknown";
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

export async function readSortieState() {
  const agents = [];

  let worktreesRoot;
  try {
    worktreesRoot = await getWorktreesRoot();
  } catch {
    return {
      agents: [],
      summary: { total: 0, working: 0, preReview: 0, done: 0 },
      timestamp: new Date().toISOString(),
    };
  }

  let entries = [];
  try {
    entries = await readdir(worktreesRoot);
  } catch {
    return {
      agents: [],
      summary: { total: 0, working: 0, preReview: 0, done: 0 },
      timestamp: new Date().toISOString(),
    };
  }

  for (const entry of entries) {
    if (entry.startsWith(".")) continue;
    const worktreePath = join(worktreesRoot, entry);
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
