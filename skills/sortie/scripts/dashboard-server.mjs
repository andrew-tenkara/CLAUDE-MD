#!/usr/bin/env node
/**
 * dashboard-server.mjs — SSE web dashboard for sortie agent monitoring
 *
 * Serves static files from ../static/ and pushes agent state via SSE every 3s.
 * Includes agent kill, PR creation, and context tracking APIs.
 * Usage: node dashboard-server.mjs [port]
 */

import http from "node:http";
import { readFile, realpath } from "node:fs/promises";
import { join, extname, dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readSortieState } from "../lib/read-sortie-state.mjs";

const execFileAsync = promisify(execFile);

const PORT = parseInt(process.argv[2] || "4242", 10);
const __dirname = dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = resolve(join(__dirname, "..", "static"));

const MIME_TYPES = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "application/javascript",
  ".json": "application/json",
};

const ALLOWED_ORIGIN = `http://localhost:${PORT}`;

function isValidOrigin(req) {
  const origin = req.headers.origin;
  return !origin || origin === ALLOWED_ORIGIN;
}

async function findPidByWorktree(worktreePath) {
  // Try pgrep with escaped path
  const escapedPath = worktreePath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  try {
    const { stdout } = await execFileAsync(
      "pgrep",
      ["-f", `claude.*${escapedPath}`],
      { encoding: "utf-8", timeout: 5000 }
    );
    const output = stdout.trim();
    if (output) {
      const pids = output.split("\n").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));
      // Ambiguous — more than one matching process; refuse to kill blindly
      if (pids.length > 1) return { ambiguous: true, count: pids.length };
      if (pids.length === 1) return pids[0];
    }
  } catch {}
  return null;
}

async function findAgentByWorktree(worktreePath) {
  const state = await readSortieState();
  return state.agents.find((a) => a.worktreePath === worktreePath) ?? null;
}

function extractFieldFromDirective(directive, fieldName) {
  const pattern = new RegExp(`\\*\\*${fieldName}\\*\\*:\\s*(.+?)(?=\\n|$)`, "i");
  const match = directive.match(pattern);
  return match ? match[1].trim() : "";
}

function extractLabels(directive) {
  const labels = extractFieldFromDirective(directive, "Labels");
  if (!labels) return [];
  return labels.split(",").map((l) => l.trim()).filter((l) => l.length > 0);
}

function inferTypeFromLabels(labels) {
  const labelsLower = labels.map((l) => l.toLowerCase());
  if (labelsLower.some((l) => l.includes("feature"))) return "feat";
  if (labelsLower.some((l) => l.includes("bug"))) return "fix";
  return "chore";
}

// SSE clients
const sseClients = new Set();

setInterval(async () => {
  if (sseClients.size === 0) return;
  try {
    const state = await readSortieState();
    const data = `data: ${JSON.stringify(state)}\n\n`;
    for (const res of sseClients) {
      try {
        res.write(data);
      } catch {
        sseClients.delete(res);
      }
    }
  } catch {
    // State read failed — skip this tick, clients will get next one
  }
}, 3000);

function sendJson(res, statusCode, obj) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
  });
  res.end(JSON.stringify(obj));
}

// Prevent path traversal — resolved path must be STATIC_DIR or inside it
const isInside = (p) => p === STATIC_DIR || p.startsWith(STATIC_DIR + sep);

async function serveStatic(res, filePath) {
  try {
    // Double-check after symlink resolution
    const real = await realpath(filePath);
    if (!isInside(real)) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }
    const content = await readFile(real);
    const ext = extname(real);
    res.writeHead(200, {
      "Content-Type": MIME_TYPES[ext] || "application/octet-stream",
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    });
    res.end(content);
  } catch {
    res.writeHead(404);
    res.end("Not Found");
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  // CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    });
    res.end();
    return;
  }

  // Serve index.html at root
  if (path === "/" || path === "/index.html") {
    return serveStatic(res, join(STATIC_DIR, "index.html"));
  }

  // Static files (with separator-safe path traversal protection)
  if (path.startsWith("/static/")) {
    const filePath = resolve(STATIC_DIR, path.replace("/static/", ""));
    if (!isInside(filePath)) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }
    return serveStatic(res, filePath);
  }

  // API: Get current state
  if (path === "/api/state" && req.method === "GET") {
    const state = await readSortieState();
    return sendJson(res, 200, state);
  }

  // SSE: Event stream (support both /events and /api/events paths)
  if ((path === "/events" || path === "/api/events") && req.method === "GET") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    });

    sseClients.add(res);

    // Send initial state
    try {
      const state = await readSortieState();
      res.write(`data: ${JSON.stringify(state)}\n\n`);
    } catch {}

    req.on("close", () => sseClients.delete(res));
    return;
  }

  // API: Kill agent (uses worktreePath query param for unique identification)
  if (path === "/api/kill" && req.method === "POST") {
    if (!isValidOrigin(req)) return sendJson(res, 403, { error: "Forbidden" });
    const worktreePath = url.searchParams.get("path");
    if (!worktreePath) return sendJson(res, 400, { error: "Missing path param" });
    const agent = await findAgentByWorktree(worktreePath);
    if (!agent) return sendJson(res, 404, { error: "Agent not found" });

    const pidResult = await findPidByWorktree(agent.worktreePath);
    if (!pidResult) return sendJson(res, 404, { error: "Process not found" });
    if (typeof pidResult === "object" && pidResult.ambiguous) {
      return sendJson(res, 409, { error: `Ambiguous: ${pidResult.count} matching processes` });
    }

    const pid = pidResult;
    try {
      process.kill(pid, "SIGTERM");
      return sendJson(res, 200, { ok: true, pid });
    } catch {
      return sendJson(res, 500, { error: "Failed to send signal" });
    }
  }

  // API: Create PR (uses worktreePath query param for unique identification)
  if (path === "/api/pr" && req.method === "POST") {
    if (!isValidOrigin(req)) return sendJson(res, 403, { error: "Forbidden" });
    const worktreePath = url.searchParams.get("path");
    if (!worktreePath) return sendJson(res, 400, { error: "Missing path param" });
    const agent = await findAgentByWorktree(worktreePath);
    if (!agent) return sendJson(res, 404, { error: "Agent/branch not found" });

    try {
      // Read directive.md to extract PR title information
      const directivePath = join(worktreePath, ".sortie", "directive.md");
      let directiveContent = "";
      try {
        directiveContent = await readFile(directivePath, "utf-8");
      } catch {
        return sendJson(res, 500, { error: "Cannot read directive.md" });
      }

      const ticketId = extractFieldFromDirective(directiveContent, "ID");
      const title = extractFieldFromDirective(directiveContent, "Title");
      const labels = extractLabels(directiveContent);
      const type = inferTypeFromLabels(labels);

      if (!ticketId || !title) {
        return sendJson(res, 500, { error: "Missing ticket ID or title in directive" });
      }

      const prTitle = `(${ticketId}) ${type}: ${title}`;

      const { stdout } = await execFileAsync(
        "gh",
        ["pr", "create", "--base", "dev", "--head", agent.branch, "--title", prTitle],
        { cwd: agent.worktreePath, encoding: "utf-8", timeout: 30000 }
      );
      return sendJson(res, 200, { ok: true, url: stdout.trim() });
    } catch (err) {
      const stderr = err.stderr?.toString() || "";
      if (stderr.includes("already exists")) {
        const urlMatch = stderr.match(/(https:\/\/github\.com\S+)/);
        return sendJson(res, 200, { ok: true, url: urlMatch?.[1] || "PR already exists" });
      }
      return sendJson(res, 500, { error: "Failed to create PR" });
    }
  }

  res.writeHead(404);
  res.end("Not Found");
});

// Bind to loopback only — this is a local dev tool, not a network service
server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`Sortie dashboard: http://localhost:${PORT}\n`);
});
