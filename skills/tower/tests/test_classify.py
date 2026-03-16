"""Unit tests for the rule-based agent activity classifier."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import pytest
from classify import classify, AIRBORNE, ON_APPROACH, HOLDING, PREFLIGHT


# ── Helpers ───────────────────────────────────────────────────────────

def assistant(*tools: tuple[str, dict], text: str = "") -> dict:
    """Build a mock assistant JSONL event."""
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    if text:
        content.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def user_error(msg: str = "error") -> dict:
    """Build a mock user event with a tool error."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": msg}
        ]},
    }


def user_ok(result: str = "ok") -> dict:
    """Build a mock user event with a successful tool result."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": False, "content": result}
        ]},
    }


# ── Write tools → AIRBORNE ────────────────────────────────────────────

class TestWriteTools:
    def test_write(self):
        s, p = classify([assistant(("Write", {"file_path": "/app/auth.ts"}))])
        assert s == AIRBORNE
        assert "auth.ts" in p

    def test_edit(self):
        s, p = classify([assistant(("Edit", {"file_path": "/src/components/Button.tsx"}))])
        assert s == AIRBORNE
        assert "Button.tsx" in p

    def test_multi_edit(self):
        s, p = classify([assistant(("MultiEdit", {"file_path": "/app/api/users.ts"}))])
        assert s == AIRBORNE
        assert "users.ts" in p

    def test_notebook_edit(self):
        s, p = classify([assistant(("NotebookEdit", {"notebook_path": "/notebooks/analysis.ipynb"}))])
        assert s == AIRBORNE
        assert "analysis.ipynb" in p

    def test_write_no_path(self):
        s, p = classify([assistant(("Write", {}))])
        assert s == AIRBORNE

    def test_todo_write(self):
        s, p = classify([assistant(("TodoWrite", {}))])
        assert s == AIRBORNE


# ── Read tools → HOLDING ─────────────────────────────────────────────

class TestReadTools:
    """Read-only windows with no prior writes → PREFLIGHT (not HOLDING)."""

    def test_read(self):
        s, p = classify([assistant(("Read", {"file_path": "/app/auth.ts"}))])
        assert s == PREFLIGHT

    def test_glob(self):
        s, p = classify([assistant(("Glob", {"pattern": "**/*.ts"}))])
        assert s == PREFLIGHT

    def test_grep(self):
        s, p = classify([assistant(("Grep", {"pattern": "useState", "path": "/src"}))])
        assert s == PREFLIGHT

    def test_web_fetch(self):
        s, p = classify([assistant(("WebFetch", {"url": "https://docs.example.com"}))])
        assert s == PREFLIGHT

    def test_web_search(self):
        s, p = classify([assistant(("WebSearch", {"query": "react hooks"}))])
        assert s == PREFLIGHT

    def test_todo_read(self):
        s, p = classify([assistant(("TodoRead", {}))])
        assert s == PREFLIGHT

    def test_ls(self):
        s, p = classify([assistant(("LS", {"path": "/app"}))])
        assert s == PREFLIGHT


# ── Agent tool → AIRBORNE ─────────────────────────────────────────────

class TestAgentTool:
    def test_agent_with_description(self):
        s, p = classify([assistant(("Agent", {"description": "run tests"}))])
        assert s == AIRBORNE
        assert "sub-agent" in p

    def test_agent_with_prompt(self):
        s, p = classify([assistant(("Agent", {"prompt": "refactor auth module"}))])
        assert s == AIRBORNE
        assert "sub-agent" in p


# ── Bash: test runners → ON_APPROACH ─────────────────────────────────

class TestBashTestRunners:
    @pytest.mark.parametrize("cmd", [
        "jest",
        "npx jest",
        "npx jest --coverage",
        "vitest",
        "npx vitest run",
        "pytest",
        "pytest tests/",
        "python -m pytest",
        "python -m unittest discover",
        "npm test",
        "npm run test",
        "pnpm test",
        "pnpm run test",
        "yarn test",
        "mocha",
        "go test ./...",
        "cargo test",
        "dotnet test",
        "bundle exec rspec",
        "karma start",
        "cypress run",
        "npx playwright test",
        "jasmine",
        "rake test",
    ])
    def test_test_runner(self, cmd):
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == ON_APPROACH, f"Expected ON_APPROACH for: {cmd!r}"
        assert cmd[:30] in p or "running" in p


# ── Bash: build commands → ON_APPROACH ───────────────────────────────

class TestBashBuildCommands:
    @pytest.mark.parametrize("cmd", [
        "tsc",
        "tsc --noEmit",
        "next build",
        "nuxt build",
        "npm run build",
        "pnpm build",
        "pnpm run build",
        "yarn build",
        "vite build",
        "webpack",
        "webpack --mode production",
        "rollup -c",
        "esbuild src/index.ts --bundle",
        "parcel build src/index.html",
        "cargo build",
        "go build ./...",
        "make",
        "make all",
        "cmake ..",
        "gradle build",
        "mvn package",
    ])
    def test_build_command(self, cmd):
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == ON_APPROACH, f"Expected ON_APPROACH for: {cmd!r}"


# ── Bash: git finish → ON_APPROACH ───────────────────────────────────

class TestBashGitFinish:
    @pytest.mark.parametrize("cmd", [
        "git commit -m 'fix auth'",
        "git commit --amend",
        "git push",
        "git push origin main",
        "git push -u origin feature/foo",
        "gh pr create --title 'Fix bug'",
        "gh pr merge 123",
        "gh pr close 123",
        "git tag v1.2.3",
        "git merge feature/foo",
        "git rebase main",
        "git add -A",
        "git add .",
        "git add src/app.ts",
    ])
    def test_git_finish(self, cmd):
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == ON_APPROACH, f"Expected ON_APPROACH for: {cmd!r}"


# ── Bash: git info → PREFLIGHT (read-only window) ───────────────────

class TestBashGitInfo:
    @pytest.mark.parametrize("cmd", [
        "git log --oneline",
        "git status",
        "git diff",
        "git diff HEAD",
        "git show HEAD",
        "git blame src/auth.ts",
        "git branch -a",
        "git remote -v",
        "git fetch",
        "git pull",
        "git stash list",
        "git rev-parse HEAD",
        "git ls-files",
        "git shortlog -sn",
        "git describe --tags",
        "git reflog",
        "git checkout main",
        "gh pr list --head feature/auth",
        "gh pr view 123",
        "gh pr status",
        "gh pr checks 123",
        "gh pr diff 123",
    ])
    def test_git_info(self, cmd):
        """Solo git info commands in a read-only window → PREFLIGHT."""
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == PREFLIGHT, f"Expected PREFLIGHT for: {cmd!r}"


# ── Bash: installs → AIRBORNE ─────────────────────────────────────────

class TestBashInstall:
    @pytest.mark.parametrize("cmd", [
        "npm install",
        "npm i",
        "npm ci",
        "pnpm install",
        "pnpm add react",
        "yarn install",
        "yarn add lodash",
        "pip install requests",
        "pip3 install -r requirements.txt",
        "brew install ripgrep",
        "cargo add serde",
        "apt-get install vim",
        "gem install bundler",
    ])
    def test_install(self, cmd):
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == AIRBORNE, f"Expected AIRBORNE for: {cmd!r}"


# ── Bash: generic active commands → AIRBORNE ─────────────────────────

class TestBashGeneric:
    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat package.json",
        "curl https://api.example.com",
        "node scripts/seed.js",
        "python scripts/migrate.py",
        "docker compose up",
        "chmod +x deploy.sh",
    ])
    def test_generic_bash(self, cmd):
        s, p = classify([assistant(("Bash", {"command": cmd}))])
        assert s == AIRBORNE, f"Expected AIRBORNE for: {cmd!r}"
        assert cmd[:40] in p


# ── Multi-tool turns: priority resolution ─────────────────────────────

class TestMultiToolPriority:
    def test_write_beats_read(self):
        s, p = classify([assistant(
            ("Read", {"file_path": "/app/auth.ts"}),
            ("Write", {"file_path": "/app/auth.ts"}),
        )])
        assert s == AIRBORNE
        assert "auth.ts" in p

    def test_write_then_bash_test_in_same_turn(self):
        # Write appears before Bash in content[] — Bash(test) is last → ON_APPROACH
        s, p = classify([assistant(
            ("Write", {"file_path": "/app/auth.ts"}),
            ("Bash", {"command": "pnpm test"}),
        )])
        assert s == ON_APPROACH

    def test_bash_test_after_read_in_same_turn(self):
        s, p = classify([assistant(
            ("Read", {"file_path": "/app/auth.ts"}),
            ("Bash", {"command": "npm test"}),
        )])
        assert s == ON_APPROACH

    def test_bash_test_after_git_info_in_same_turn(self):
        s, p = classify([assistant(
            ("Bash", {"command": "git status"}),
            ("Bash", {"command": "npm test"}),
        )])
        assert s == ON_APPROACH

    def test_read_heavy_turn_returns_preflight(self):
        """All-read turn with no prior writes → PREFLIGHT."""
        s, p = classify([assistant(
            ("Read", {"file_path": "/app/a.ts"}),
            ("Glob", {"pattern": "**/*.ts"}),
            ("Grep", {"pattern": "useState"}),
        )])
        assert s == PREFLIGHT

    def test_agent_after_read_in_same_turn(self):
        s, p = classify([assistant(
            ("Read", {"file_path": "/app/auth.ts"}),
            ("Agent", {"description": "run migration"}),
        )])
        assert s == AIRBORNE

    def test_last_write_in_turn_sets_phase(self):
        # Both writes in same turn — last one sets phase
        s, p = classify([assistant(
            ("Write", {"file_path": "/app/a.ts"}),
            ("Write", {"file_path": "/app/b.ts"}),
        )])
        assert s == AIRBORNE
        assert "b.ts" in p


# ── Multi-event batches ───────────────────────────────────────────────

class TestMultiEventBatches:
    def test_multiple_assistant_events(self):
        events = [
            assistant(("Read", {"file_path": "/app/auth.ts"})),
            assistant(("Glob", {"pattern": "**/*.ts"})),
            assistant(("Write", {"file_path": "/app/auth.ts"})),
        ]
        s, p = classify(events)
        assert s == AIRBORNE

    def test_later_read_lowers_status_to_holding(self):
        # Last action wins — Read after Write → HOLDING (reading now, not writing)
        events = [
            assistant(("Write", {"file_path": "/app/auth.ts"})),
            assistant(("Read", {"file_path": "/app/types.ts"})),
        ]
        s, p = classify(events)
        assert s == HOLDING

    def test_only_read_events(self):
        """Multiple read events with no writes → PREFLIGHT."""
        events = [
            assistant(("Read", {"file_path": "/app/a.ts"})),
            assistant(("Read", {"file_path": "/app/b.ts"})),
            assistant(("Grep", {"pattern": "auth"})),
        ]
        s, p = classify(events)
        assert s == PREFLIGHT


# ── Error density ─────────────────────────────────────────────────────

class TestErrorDensity:
    def test_two_errors_tags_phase(self):
        events = [
            assistant(("Bash", {"command": "node server.js"})),
            user_error("ENOENT: file not found"),
            assistant(("Bash", {"command": "node server.js"})),
            user_error("SyntaxError: unexpected token"),
        ]
        s, p = classify(events)
        assert s == AIRBORNE
        assert "error" in p.lower()
        assert "2" in p

    def test_one_error_no_tag(self):
        events = [
            assistant(("Bash", {"command": "node server.js"})),
            user_error("ENOENT: file not found"),
        ]
        s, p = classify(events)
        assert s == AIRBORNE
        assert "error" not in p.lower()

    def test_errors_with_preflight_no_tag(self):
        # Errors only tag when agent is AIRBORNE (actively working)
        # Read-only window → PREFLIGHT, not HOLDING
        events = [
            assistant(("Read", {"file_path": "/app/auth.ts"})),
            user_error("permission denied"),
            user_error("permission denied"),
        ]
        s, p = classify(events)
        assert s == PREFLIGHT
        assert "error" not in p.lower()

    def test_successful_results_not_counted(self):
        events = [
            assistant(("Bash", {"command": "ls"})),
            user_ok("file1.ts\nfile2.ts"),
            user_ok("file3.ts"),
        ]
        s, p = classify(events)
        assert "error" not in p.lower()

    def test_multiple_error_blocks_in_single_message(self):
        # One user message with 3 error blocks counts as 3 errors — pins current behavior.
        # Use a generic AIRBORNE command so the error annotation fires.
        events = [
            assistant(("Bash", {"command": "node server.js"})),
            {
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "is_error": True, "content": "err1"},
                    {"type": "tool_result", "is_error": True, "content": "err2"},
                    {"type": "tool_result", "is_error": True, "content": "err3"},
                ]},
            },
        ]
        s, p = classify(events)
        assert s == AIRBORNE
        assert "3" in p
        assert "error" in p.lower()


# ── Narration text signals ────────────────────────────────────────────

class TestNarration:
    def test_narration_promotes_holding_to_on_approach(self):
        events = [assistant(text="All tests pass, opening PR now.")]
        s, p = classify(events)
        assert s == ON_APPROACH

    def test_wrap_up_narration_after_write_is_on_approach(self):
        # Text block appears after Write in content[] — last-action-wins means
        # narration "creating PR now" correctly overrides to ON_APPROACH.
        # Agent wrote the file AND then said it's wrapping up.
        events = [assistant(
            ("Write", {"file_path": "/app/auth.ts"}),
            text="All tests pass, creating PR now.",
        )]
        s, p = classify(events)
        assert s == ON_APPROACH

    def test_neutral_narration_stays_holding(self):
        events = [assistant(text="Let me read the codebase to understand the structure.")]
        s, p = classify(events)
        assert s == HOLDING

    @pytest.mark.parametrize("text", [
        "All tests pass, time to push.",
        "Creating a PR for this change.",
        "Final check before submitting pr.",
        "LGTM, ready to merge.",
        "Let me verify nothing broke.",
        "git push origin feature/auth",
    ])
    def test_on_approach_narration_signals(self, text):
        events = [assistant(text=text)]
        s, p = classify(events)
        assert s == ON_APPROACH, f"Expected ON_APPROACH for narration: {text!r}"


# ── Edge cases ────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_events(self):
        s, p = classify([])
        assert s == HOLDING
        assert p == "idle"

    def test_unknown_tool(self):
        s, p = classify([assistant(("SomeFutureTool", {"arg": "val"}))])
        assert s == AIRBORNE

    def test_progress_events_ignored(self):
        events = [
            {"type": "progress", "data": {"type": "hook_progress"}},
            {"type": "progress", "data": {"type": "hook_progress"}},
        ]
        s, p = classify(events)
        assert s == HOLDING

    def test_system_events_ignored(self):
        events = [
            {"type": "system", "subtype": "turn_duration", "durationMs": 5000},
        ]
        s, p = classify(events)
        assert s == HOLDING

    def test_last_prompt_event_ignored(self):
        events = [{"type": "last-prompt", "lastPrompt": "do something"}]
        s, p = classify(events)
        assert s == HOLDING

    def test_malformed_content_ignored(self):
        events = [{"type": "assistant", "message": {"content": [None, "oops", 42]}}]
        s, p = classify(events)
        assert s == HOLDING

    def test_bash_empty_command(self):
        s, p = classify([assistant(("Bash", {"command": ""}))])
        assert s == AIRBORNE  # Bash is active by default even if empty

    def test_write_no_file_path(self):
        s, p = classify([assistant(("Write", {}))])
        assert s == AIRBORNE

    def test_read_with_url(self):
        s, p = classify([assistant(("WebFetch", {"url": "https://docs.react.dev"}))])
        assert s == PREFLIGHT

    def test_user_message_no_content_list(self):
        # user message with string content (initial prompt) — no crash
        events = [{
            "type": "user",
            "message": {"role": "user", "content": "Read the directive and follow it."},
        }]
        s, p = classify(events)
        assert s == HOLDING

    def test_narration_case_insensitive(self):
        # All-caps or mixed case should still trigger ON_APPROACH
        events = [assistant(text="All Tests Pass, Creating PR Now.")]
        s, p = classify(events)
        assert s == ON_APPROACH

    def test_read_after_narration_text_shows_holding(self):
        # Narration at step 0, then Read at step 1 — last-action-wins → HOLDING
        event = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "All tests pass, creating PR now."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/app/a.ts"}},
            ]},
        }
        s, p = classify([event])
        assert s == HOLDING


# ── Error loop detection ──────────────────────────────────────────────

class TestErrorLoopDetection:
    def _bash_fail(self, cmd: str) -> list[dict]:
        """One bash call + one error result."""
        return [
            assistant(("Bash", {"command": cmd})),
            user_error(f"error running {cmd}"),
        ]

    def _bash_ok(self, cmd: str) -> list[dict]:
        """One bash call + one success result."""
        return [
            assistant(("Bash", {"command": cmd})),
            user_ok("ok"),
        ]

    def test_three_identical_bash_failures_detected(self):
        events = (
            self._bash_fail("node server.js") * 3
        )
        s, p = classify(events)
        assert s == AIRBORNE
        assert "stuck" in p
        assert "3x" in p

    def test_two_failures_not_enough(self):
        events = self._bash_fail("node server.js") * 2
        s, p = classify(events)
        assert "stuck" not in p

    def test_success_resets_loop_count(self):
        events = (
            self._bash_fail("node server.js") * 2
            + self._bash_ok("node server.js")
            + self._bash_fail("node server.js") * 2
        )
        s, p = classify(events)
        assert "stuck" not in p  # reset by success, only 2 after

    def test_loop_on_different_commands_not_detected(self):
        # Errors on different commands don't count toward same loop
        events = (
            self._bash_fail("npm test") * 2
            + self._bash_fail("node server.js")
        )
        s, p = classify(events)
        assert "stuck" not in p

    def test_loop_promotes_holding_to_airborne(self):
        # Agent is only reading + stuck in a loop → should be AIRBORNE not HOLDING
        events = (
            [assistant(("Read", {"file_path": "/app/auth.ts"}))]
            + self._bash_fail("cat missing.txt") * 3
        )
        s, p = classify(events)
        assert s == AIRBORNE
        assert "stuck" in p

    def test_write_tool_loop_detected(self):
        path = "/app/auth.ts"
        events = (
            [assistant(("Edit", {"file_path": path})), user_error("parse error")] * 3
        )
        s, p = classify(events)
        assert "stuck" in p
        assert "auth.ts" in p

    def test_loop_label_shows_command(self):
        cmd = "python migrate.py"
        events = self._bash_fail(cmd) * 3
        s, p = classify(events)
        assert cmd[:20] in p

    def test_loop_overrides_generic_error_count_phase(self):
        # Loop detection takes priority over generic "(N errors)" label
        events = self._bash_fail("npm run build") * 4
        s, p = classify(events)
        assert "stuck" in p
        assert "4x" in p
        # Should not have both "stuck" and "(N errors)"
        assert "errors)" not in p

    def test_no_loop_empty_events(self):
        s, p = classify([])
        assert "stuck" not in p

    def test_different_tool_success_does_not_reset_failing_tool_count(self):
        # A fails 2×, unrelated B succeeds, A fails 1× more → 3 total A failures.
        # B's success only resets B's count, not A's. This is intentional:
        # failing at the same thing 3× with unrelated work in between = stuck.
        events = (
            self._bash_fail("npm run build") * 2
            + self._bash_ok("git status")
            + self._bash_fail("npm run build")
        )
        s, p = classify(events)
        assert "stuck" in p

    def test_read_tool_loop_promotes_to_airborne(self):
        # A read tool failing 3× is a loop — status promoted from HOLDING to AIRBORNE
        path = "/app/missing-file.ts"
        events = (
            [assistant(("Read", {"file_path": path})), user_error("file not found")] * 3
        )
        s, p = classify(events)
        assert s == AIRBORNE
        assert "stuck" in p

    def test_interleaved_successes_prevent_loop(self):
        cmd = "npm test"
        events = (
            self._bash_fail(cmd)
            + self._bash_ok(cmd)
            + self._bash_fail(cmd)
            + self._bash_ok(cmd)
            + self._bash_fail(cmd)
        )
        s, p = classify(events)
        assert "stuck" not in p  # never 3 consecutive failures
