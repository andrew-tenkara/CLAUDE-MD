/**
 * Parse Claude Code JSONL session logs to extract agent activity metrics.
 *
 * Session files live at:
 *   ~/.claude/projects/<encoded-path>/<session-uuid>.jsonl
 *   ~/.claude/projects/<encoded-path>/<session-uuid>/subagents/*.jsonl
 *
 * Path encoding: replace all '/' and '.' with '-'.
 * Each line is a JSON record. We care about:
 *   - type:"assistant" — contains tool_use blocks (tool calls) and usage data
 *   - type:"user"      — contains tool_result blocks (errors when is_error=true)
 */

import { createReadStream } from "node:fs";
import { readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import readline from "node:readline";

const CLAUDE_PROJECTS_DIR = join(homedir(), ".claude", "projects");

/** Safely coerce a potentially-malformed token count value to an integer. */
const si = (v) => (Number.isFinite(Number(v)) ? Math.trunc(Number(v)) : 0);

/**
 * Encode an absolute path the same way Claude Code does for its project dirs.
 * Both '/' and '.' are replaced with '-'.
 */
export function encodeProjectPath(absPath) {
  return absPath.replace(/[/.]/g, "-");
}

/**
 * Recursively find all .jsonl files under a directory (for subagent logs).
 */
async function findAllJsonlFiles(dir) {
  const files = [];
  try {
    const entries = await readdir(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = join(dir, entry.name);
      if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        files.push(fullPath);
      } else if (entry.isDirectory()) {
        const nested = await findAllJsonlFiles(fullPath);
        files.push(...nested);
      }
    }
  } catch {}
  return files;
}

/**
 * Find the most recently modified top-level JSONL session file for a given worktree path.
 * Returns null if none found.
 */
export async function findLatestSessionFile(worktreePath) {
  const encoded = encodeProjectPath(worktreePath);
  const projectDir = join(CLAUDE_PROJECTS_DIR, encoded);

  let files;
  try {
    files = await readdir(projectDir);
  } catch {
    return null;
  }

  const jsonlFiles = files.filter((f) => f.endsWith(".jsonl"));
  if (jsonlFiles.length === 0) return null;

  const withStats = await Promise.all(
    jsonlFiles.map(async (f) => {
      const p = join(projectDir, f);
      try {
        const s = await stat(p);
        return { path: p, mtime: s.mtimeMs };
      } catch {
        return null;
      }
    })
  );

  const valid = withStats.filter(Boolean);
  if (valid.length === 0) return null;
  valid.sort((a, b) => b.mtime - a.mtime);
  return valid[0].path;
}

/**
 * Parse a single JSONL file and accumulate metrics into shared accumulators.
 *
 * @param {string} sessionFile
 * @param {Record<string, number>} toolCallCounts  mutated in place
 * @param {Array} timeline  mutated in place
 * @param {object} acc  mutated in place
 */
async function parseSingleFile(sessionFile, toolCallCounts, timeline, acc) {
  try {
    const rl = readline.createInterface({
      input: createReadStream(sessionFile, { encoding: "utf-8" }),
      crlfDelay: Infinity,
    });

    for await (const line of rl) {
      if (!line.trim()) continue;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue;
      }

      if (!acc.sessionId && obj.sessionId) {
        acc.sessionId = obj.sessionId;
      }

      if (obj.type === "assistant") {
        // Token usage — coerce to number to guard against malformed JSONL values
        const usage = obj.message?.usage ?? {};
        acc.inputTokens       += si(usage.input_tokens);
        acc.outputTokens      += si(usage.output_tokens);
        acc.cacheWriteTokens  += si(usage.cache_creation_input_tokens);
        acc.cacheReadTokens   += si(usage.cache_read_input_tokens);

        const content = obj.message?.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block?.type === "tool_use") {
              const name = block.name || "unknown";
              toolCallCounts[name] = (toolCallCounts[name] || 0) + 1;
              if (name === "Agent") acc.agentSpawns++;
              timeline.push({ tool: name, timestamp: obj.timestamp });
              acc.lastActivityAt = obj.timestamp;
            }
          }
        }
      }

      if (obj.type === "user") {
        const content = obj.message?.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block?.type === "tool_result" && block.is_error === true) {
              acc.errorCount++;
            }
          }
        }
      }
    }
  } catch {}
}

/**
 * Load JSONL metrics for a worktree path.
 * Scans ALL session files (including subagent logs) for token usage.
 * Returns null if no session files exist.
 *
 * @param {string} worktreePath
 * @returns {Promise<JsonlMetrics|null>}
 */
export async function parseJsonlMetrics(worktreePath) {
  const encoded = encodeProjectPath(worktreePath);
  const projectDir = join(CLAUDE_PROJECTS_DIR, encoded);

  const allFiles = await findAllJsonlFiles(projectDir);
  if (allFiles.length === 0) return null;

  // Latest top-level session for metadata
  const topLevelFiles = allFiles.filter(
    (f) => f.startsWith(projectDir) && !f.slice(projectDir.length + 1).includes("/")
  );
  let latestSessionFile = null;
  if (topLevelFiles.length > 0) {
    const withStats = await Promise.all(
      topLevelFiles.map(async (f) => {
        try { return { path: f, mtime: (await stat(f)).mtimeMs }; }
        catch { return null; }
      })
    );
    const valid = withStats.filter(Boolean);
    if (valid.length > 0) {
      valid.sort((a, b) => b.mtime - a.mtime);
      latestSessionFile = valid[0].path;
    }
  }

  /** @type {Record<string, number>} */
  const toolCallCounts = {};
  /** @type {Array<{tool: string, timestamp: string}>} */
  const timeline = [];
  const acc = {
    sessionId: null,
    inputTokens: 0,
    outputTokens: 0,
    cacheWriteTokens: 0,
    cacheReadTokens: 0,
    agentSpawns: 0,
    errorCount: 0,
    lastActivityAt: null,
  };

  // Parse latest session file first so sessionId comes from the correct session
  if (latestSessionFile) {
    await parseSingleFile(latestSessionFile, toolCallCounts, timeline, acc);
  }
  for (const file of allFiles) {
    if (file === latestSessionFile) continue;
    await parseSingleFile(file, toolCallCounts, timeline, acc);
  }

  // Sort timeline by timestamp so cross-file ordering is correct, then derive
  // lastActivityAt from the true latest event rather than traversal order.
  // Parse to epoch so ISO 8601 timestamps with offsets sort correctly.
  timeline.sort((a, b) => {
    const ta = a.timestamp ? (Date.parse(a.timestamp) || 0) : 0;
    const tb = b.timestamp ? (Date.parse(b.timestamp) || 0) : 0;
    return ta - tb;
  });
  if (timeline.length > 0) {
    acc.lastActivityAt = timeline[timeline.length - 1].timestamp;
  }

  const totalToolCalls = Object.values(toolCallCounts).reduce((a, b) => a + b, 0);
  const errorRate = totalToolCalls > 0
    ? Math.round((acc.errorCount / totalToolCalls) * 100) / 100
    : 0;

  const totalTokens = acc.inputTokens + acc.outputTokens + acc.cacheWriteTokens + acc.cacheReadTokens;

  return {
    sessionId: acc.sessionId,
    sessionFile: latestSessionFile,
    toolCallCounts,
    totalToolCalls,
    errorCount: acc.errorCount,
    errorRate,
    agentSpawns: acc.agentSpawns,
    lastActivityAt: acc.lastActivityAt,
    recentTimeline: timeline.slice(-10),
    inputTokens: acc.inputTokens,
    outputTokens: acc.outputTokens,
    cacheWriteTokens: acc.cacheWriteTokens,
    cacheReadTokens: acc.cacheReadTokens,
    totalTokens,
  };
}
