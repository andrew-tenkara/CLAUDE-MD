"""USS Tenkara PRI-FLY — rendering helpers.

Pure functions for fuel gauges, tool icons, rich text formatting,
code fence rendering, and elapsed time formatting.
"""
from __future__ import annotations

import re as _re

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


# ── Fuel gauge ────────────────────────────────────────────────────────

def fuel_gauge(pct: int, width: int = 10, blink: bool = False) -> Text:
    if pct <= 20:
        style = "bold red"
    elif pct <= 50:
        style = "yellow"
    else:
        style = "green"

    bar = Text()
    filled = round(pct / 100 * width)
    empty = width - filled
    bar.append("━" * filled, style=style)
    bar.append("╌" * empty, style="grey37")
    bar.append(f" {pct}%", style=style)

    if pct <= 10:
        bar.append(" BINGO!", style="bold bright_red" if blink else "dim red")
    elif pct <= 30:
        bar.append(" ⚠", style="bold red")

    return bar


# ── Token formatting ──────────────────────────────────────────────────

def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# ── Elapsed time ──────────────────────────────────────────────────────

def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    minutes, secs = divmod(s, 60)
    hours, mins = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


# ── Tool icons ────────────────────────────────────────────────────────

def _tool_icon(tool_name: str) -> str:
    """Return a compact icon for a tool name, like Claude Code's display."""
    icons = {
        "Edit": "~",
        "Write": "+",
        "Read": "?",
        "Bash": "$",
        "Grep": "/",
        "Glob": "*",
        "Agent": ">",
    }
    return icons.get(tool_name, "#")


# ── Code fence / syntax highlighting ─────────────────────────────────

_CODE_FENCE_RE = _re.compile(
    r"```(\w*)\n(.*?)```",
    _re.DOTALL,
)

_EXT_TO_LANG = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".py": "python", ".rs": "rust", ".go": "go", ".sh": "bash", ".bash": "bash",
    ".css": "css", ".scss": "scss", ".html": "html", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".sql": "sql", ".md": "markdown",
    ".rb": "ruby", ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
}


def _guess_lang_from_path(file_path: str) -> str:
    """Guess the syntax language from a file path extension."""
    for ext, lang in _EXT_TO_LANG.items():
        if file_path.endswith(ext):
            return lang
    return "text"


# ── Rich text rendering ──────────────────────────────────────────────

def _render_assistant_content(log, content: str) -> None:
    """Render assistant text with syntax-highlighted code blocks.

    Splits content on ``` fences. Prose gets Rich Text with inline code,
    code blocks get Syntax with monokai theme in a bordered panel.
    """
    parts = _CODE_FENCE_RE.split(content)
    # parts alternates: [prose, lang, code, prose, lang, code, ...]
    i = 0
    while i < len(parts):
        if i + 2 < len(parts) and (i % 3) == 0:
            prose = parts[i].strip()
            if prose:
                _render_prose(log, prose)
            lang = parts[i + 1] or "text"
            code = parts[i + 2]
            if code.strip():
                try:
                    syn = Syntax(
                        code.strip(), lang,
                        theme="monokai", line_numbers=len(code.strip().splitlines()) > 3,
                        word_wrap=True, padding=(0, 1),
                    )
                    log.write(Panel(
                        syn, border_style="dim cyan", expand=True,
                        padding=(0, 0), title=lang if lang != "text" else None,
                        title_align="right",
                    ))
                except Exception:
                    log.write(Panel(code.strip(), border_style="dim cyan"))
            i += 3
        else:
            prose = parts[i].strip()
            if prose:
                _render_prose(log, prose)
            i += 1


def _render_prose(log, prose: str) -> None:
    """Render a prose block with markdown-like formatting."""
    t = Text()
    for line in prose.split("\n"):
        stripped = line.strip()
        if not stripped:
            t.append("\n")
            continue
        # Heading-like lines (## Foo)
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            t.append(f"  {heading}\n", style="bold #61afef")
        # Bullet points
        elif stripped.startswith(("- ", "* ", "• ")):
            bullet_content = stripped[2:]
            t.append("  • ", style="#c678dd")
            _append_inline_code(t, bullet_content)
            t.append("\n")
        # Numbered lists
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
            num = stripped[:2]
            rest = stripped[2:].strip()
            t.append(f"  {num} ", style="#c678dd")
            _append_inline_code(t, rest)
            t.append("\n")
        # Bold lines (**foo**)
        elif stripped.startswith("**") and stripped.endswith("**"):
            t.append(f"  {stripped[2:-2]}\n", style="bold #e5c07b")
        else:
            t.append("  ")
            _append_inline_code(t, stripped)
            t.append("\n")
    if t.plain.strip():
        log.write(t)


def _append_inline_code(t: Text, text: str) -> None:
    """Append text with inline `backtick` spans highlighted."""
    segments = text.split("`")
    for idx, seg in enumerate(segments):
        if idx % 2 == 1:
            t.append(f" {seg} ", style="bold #e6db74 on #272822")
        elif seg:
            t.append(seg, style="#abb2bf")


def _render_tool_detail(log, tool_name: str, tool_input: dict) -> None:
    """Render rich tool call details (file paths, diffs, commands)."""
    fp = tool_input.get("file_path", "")
    if fp:
        t = Text()
        t.append(f"  {fp}", style="dim cyan")
        log.write(t)

    if tool_name == "Edit":
        old_s = tool_input.get("old_string", "")
        new_s = tool_input.get("new_string", "")
        if old_s or new_s:
            lang = _guess_lang_from_path(fp) if fp else "text"
            diff_lines = []
            for line in old_s.split("\n")[:8]:
                diff_lines.append(f"- {line}")
            if len(old_s.split("\n")) > 8:
                diff_lines.append(f"  ... ({len(old_s.splitlines())} lines)")
            for line in new_s.split("\n")[:8]:
                diff_lines.append(f"+ {line}")
            if len(new_s.split("\n")) > 8:
                diff_lines.append(f"  ... ({len(new_s.splitlines())} lines)")
            diff_text = "\n".join(diff_lines)
            try:
                syn = Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="cyan", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(diff_text, border_style="cyan"))

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            try:
                syn = Syntax(cmd[:300], "bash", theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="green", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(f"$ {cmd[:300]}", border_style="green"))

    elif tool_name == "Write":
        content_preview = tool_input.get("content", "")
        if content_preview:
            lang = _guess_lang_from_path(fp) if fp else "text"
            lines = content_preview.split("\n")[:6]
            preview = "\n".join(lines)
            if len(content_preview.split("\n")) > 6:
                preview += f"\n  ... ({len(content_preview.splitlines())} total lines)"
            try:
                syn = Syntax(preview, lang, theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="green", title="write", title_align="left", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(preview, border_style="green"))

    elif tool_name == "Read":
        t = Text()
        t.append(f"  Reading file...", style="dim")
        log.write(t)

    elif tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            t = Text()
            t.append(f"  pattern: ", style="dim")
            t.append(pattern, style="bold yellow")
            log.write(t)
