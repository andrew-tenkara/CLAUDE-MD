import { readSortieState } from "../lib/read-sortie-state";
import { join, dirname } from "node:path";
import { readdir } from "node:fs/promises";

const PORT = 4242;
const STATIC_DIR = join(import.meta.dir, "..", "static");
// Derive repo root from this file's location:
// <repo>/.claude/skills/sortie/scripts/dashboard-server.ts
const WORKTREES_ROOT = join(dirname(import.meta.path), "..", "..", "..", "..", ".claude", "worktrees");

const MIME_TYPES: Record<string, string> = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "application/javascript",
  ".json": "application/json",
};

function findPidByWorktree(worktreePath: string): number | null {
  try {
    // Find claude processes whose CWD matches the worktree path
    const proc = Bun.spawnSync(["pgrep", "-f", `claude.*${worktreePath}`]);
    const output = new TextDecoder().decode(proc.stdout).trim();
    if (!output) return null;
    const pid = parseInt(output.split("\n")[0], 10);
    return isNaN(pid) ? null : pid;
  } catch {
    return null;
  }
}

async function findWorktreeForTicket(ticketId: string): Promise<string | null> {
  const state = await readSortieState();
  const agent = state.agents.find((a) => a.ticketId === ticketId);
  return agent?.worktreePath ?? null;
}

async function findBranchForTicket(ticketId: string): Promise<string | null> {
  const state = await readSortieState();
  const agent = state.agents.find((a) => a.ticketId === ticketId);
  return agent?.branch ?? null;
}

const sseClients = new Set<ReadableStreamDefaultController>();

// Push state to all SSE clients every 3 seconds
setInterval(async () => {
  const state = await readSortieState();
  const data = `data: ${JSON.stringify(state)}\n\n`;
  for (const controller of sseClients) {
    try {
      controller.enqueue(new TextEncoder().encode(data));
    } catch {
      sseClients.delete(controller);
    }
  }
}, 3000);

const server = Bun.serve({
  port: PORT,
  async fetch(req) {
    const url = new URL(req.url);
    const path = url.pathname;

    // CORS headers for local dev
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    };

    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers });
    }

    // Serve index.html at root
    if (path === "/" || path === "/index.html") {
      const file = Bun.file(join(STATIC_DIR, "index.html"));
      return new Response(file, {
        headers: { ...headers, "Content-Type": "text/html" },
      });
    }

    // Static files
    if (path.startsWith("/static/")) {
      const filePath = join(STATIC_DIR, path.replace("/static/", ""));
      const file = Bun.file(filePath);
      const ext = path.substring(path.lastIndexOf("."));
      return new Response(file, {
        headers: {
          ...headers,
          "Content-Type": MIME_TYPES[ext] || "application/octet-stream",
        },
      });
    }

    // API: Get current state
    if (path === "/api/state" && req.method === "GET") {
      const state = await readSortieState();
      return Response.json(state, { headers });
    }

    // SSE: Event stream
    if (path === "/events" && req.method === "GET") {
      const stream = new ReadableStream({
        start(controller) {
          sseClients.add(controller);
          // Send initial state immediately
          readSortieState().then((state) => {
            try {
              controller.enqueue(
                new TextEncoder().encode(
                  `data: ${JSON.stringify(state)}\n\n`
                )
              );
            } catch {}
          });
        },
        cancel(controller) {
          sseClients.delete(controller);
        },
      });

      return new Response(stream, {
        headers: {
          ...headers,
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }

    // API: Kill agent
    if (path.startsWith("/api/kill/") && req.method === "POST") {
      const ticketId = decodeURIComponent(path.replace("/api/kill/", ""));
      const worktreePath = await findWorktreeForTicket(ticketId);
      if (!worktreePath) {
        return Response.json(
          { error: "Agent not found" },
          { status: 404, headers }
        );
      }

      const pid = findPidByWorktree(worktreePath);
      if (!pid) {
        return Response.json(
          { error: "Process not found" },
          { status: 404, headers }
        );
      }

      try {
        process.kill(pid, "SIGTERM");
        return Response.json({ ok: true, pid }, { headers });
      } catch (err: any) {
        return Response.json(
          { error: err.message },
          { status: 500, headers }
        );
      }
    }

    // API: Create PR
    if (path.startsWith("/api/pr/") && req.method === "POST") {
      const ticketId = decodeURIComponent(path.replace("/api/pr/", ""));
      const branch = await findBranchForTicket(ticketId);
      if (!branch) {
        return Response.json(
          { error: "Agent/branch not found" },
          { status: 404, headers }
        );
      }

      const worktreePath = await findWorktreeForTicket(ticketId);
      if (!worktreePath) {
        return Response.json(
          { error: "Worktree not found" },
          { status: 404, headers }
        );
      }

      try {
        const proc = Bun.spawnSync(
          ["gh", "pr", "create", "--base", "dev", "--head", branch, "--fill"],
          { cwd: worktreePath }
        );
        const stdout = new TextDecoder().decode(proc.stdout).trim();
        const stderr = new TextDecoder().decode(proc.stderr).trim();

        if (proc.exitCode !== 0) {
          // Check if PR already exists
          if (stderr.includes("already exists")) {
            const urlMatch = stderr.match(/(https:\/\/github\.com\S+)/);
            return Response.json(
              { ok: true, url: urlMatch?.[1] || stderr },
              { headers }
            );
          }
          return Response.json(
            { error: stderr || "gh pr create failed" },
            { status: 500, headers }
          );
        }

        return Response.json({ ok: true, url: stdout }, { headers });
      } catch (err: any) {
        return Response.json(
          { error: err.message },
          { status: 500, headers }
        );
      }
    }

    return new Response("Not Found", { status: 404, headers });
  },
});

console.log(`sortie dashboard running at http://localhost:${PORT}`);
