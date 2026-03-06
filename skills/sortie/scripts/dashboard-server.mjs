import http from "node:http";
import { readFile } from "node:fs/promises";
import { join, extname, dirname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readSortieState } from "../lib/read-sortie-state.mjs";

const execFileAsync = promisify(execFile);

const PORT = 4242;
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
  // Use lsof to find claude processes whose CWD matches the exact worktree path.
  // This avoids prefix collisions (e.g. ENG-1 matching ENG-10).
  try {
    const { stdout } = await execFileAsync(
      "lsof",
      ["+D", worktreePath, "-c", "claude", "-a", "-d", "cwd", "-t"],
      { encoding: "utf-8", timeout: 5000 }
    );
    const pid = parseInt(stdout.trim().split("\n")[0], 10);
    if (!isNaN(pid)) return pid;
  } catch {
    // lsof may not find anything — fall through
  }

  // Fallback: pgrep with exact path boundary match (trailing / or end-of-string)
  try {
    const { stdout } = await execFileAsync("pgrep", ["-f", `claude`], {
      encoding: "utf-8",
      timeout: 5000,
    });
    for (const line of stdout.trim().split("\n")) {
      const pid = parseInt(line, 10);
      if (isNaN(pid)) continue;
      try {
        const { stdout: cwdLink } = await execFileAsync(
          "lsof",
          ["-p", String(pid), "-d", "cwd", "-Fn"],
          { encoding: "utf-8", timeout: 5000 }
        );
        // lsof -Fn outputs lines like "n/path/to/cwd"
        const cwdMatch = cwdLink.match(/^n(.+)$/m);
        if (cwdMatch && cwdMatch[1] === worktreePath) return pid;
      } catch {}
    }
  } catch {}
  return null;
}

async function findAgentByWorktree(worktreePath) {
  const state = await readSortieState();
  return state.agents.find((a) => a.worktreePath === worktreePath) ?? null;
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
    "Access-Control-Allow-Origin": "http://localhost:4242",
  });
  res.end(JSON.stringify(obj));
}

async function serveStatic(res, filePath) {
  try {
    const content = await readFile(filePath);
    const ext = extname(filePath);
    res.writeHead(200, {
      "Content-Type": MIME_TYPES[ext] || "application/octet-stream",
      "Access-Control-Allow-Origin": "http://localhost:4242",
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
      "Access-Control-Allow-Origin": "http://localhost:4242",
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
    if (!filePath.startsWith(STATIC_DIR + sep) && filePath !== STATIC_DIR) {
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

  // SSE: Event stream
  if (path === "/events" && req.method === "GET") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      "Access-Control-Allow-Origin": "http://localhost:4242",
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

    const pid = await findPidByWorktree(agent.worktreePath);
    if (!pid) return sendJson(res, 404, { error: "Process not found" });

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
      const { stdout } = await execFileAsync(
        "gh",
        ["pr", "create", "--base", "dev", "--head", agent.branch, "--fill"],
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
  console.log(`sortie dashboard running at http://localhost:${PORT}`);
});
