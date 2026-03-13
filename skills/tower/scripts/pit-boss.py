from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


SYSTEM_PROMPT = """You are the Pit Boss on USS Tenkara. Your job is to analyze engineering specs and tickets,
then generate clear, actionable directives for developer agents.

Rules:
1. Each directive should be self-contained — an agent should be able to complete it independently
2. If the work naturally splits into 2-4 parallel streams, create separate directives
3. Don't over-split — if it's one coherent task, keep it as one directive
4. Each directive must include: what to do, which files to touch, acceptance criteria
5. Suggest a model: opus for complex architecture, sonnet for standard features, haiku for simple fixes

Output format (JSON array):
[
  {
    "title": "Short title",
    "directive": "Full directive text with details...",
    "model": "sonnet",
    "priority": 2
  }
]"""


def _call_claude(prompt: str, model: str = "haiku") -> str:
    """Call Claude CLI to generate directives. Uses haiku for speed."""
    result = subprocess.run(
        ["claude", "--model", model, "-p", prompt, "--no-input"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude call failed: {result.stderr}")
    return result.stdout.strip()


def _extract_json(text: str) -> list[dict]:
    """Extract and parse JSON array from Claude output, stripping markdown fences if present."""
    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try to find a JSON array in the text if there's surrounding prose
    array_match = re.search(r"\[[\s\S]*\]", text)
    if array_match:
        text = array_match.group(0)

    return json.loads(text)


def _build_prompt(spec_content: str, ticket_id: str = "", suggested_model: str = "sonnet") -> str:
    parts = [SYSTEM_PROMPT, ""]
    if ticket_id:
        parts.append(f"Ticket: {ticket_id}")
        parts.append("")
    parts.append(f"Suggested model tier: {suggested_model}")
    parts.append("")
    parts.append("Spec/Ticket content:")
    parts.append(spec_content)
    return "\n".join(parts)


def generate_directive(
    spec_content: str,
    ticket_id: str = "",
    model: str = "sonnet",
) -> list[dict]:
    """Analyze spec/ticket content and return a list of agent directives.

    Args:
        spec_content: Raw spec or ticket text.
        ticket_id: Optional ticket identifier (e.g. ENG-123).
        model: Suggested model tier passed to Claude as a hint.

    Returns:
        List of directive dicts with keys: title, directive, model, priority.
    """
    prompt = _build_prompt(spec_content, ticket_id, model)

    raw = _call_claude(prompt, model="haiku")

    try:
        return _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        # Retry once with a stricter instruction
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Your previous response could not be parsed as JSON. "
            "Return ONLY a valid JSON array with no surrounding text or markdown."
        )
        raw = _call_claude(retry_prompt, model="haiku")
        try:
            return _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"Failed to parse directives from Claude output after retry.\n"
                f"Raw output:\n{raw}"
            ) from exc


def generate_directive_from_file(
    file_path: str,
    model: str = "sonnet",
) -> list[dict]:
    """Read a spec file and generate directives from its contents.

    Args:
        file_path: Absolute or relative path to the spec file.
        model: Suggested model tier.

    Returns:
        List of directive dicts.
    """
    content = Path(file_path).read_text()
    ticket_id = Path(file_path).stem  # Use filename stem as a fallback ticket ID
    return generate_directive(content, ticket_id=ticket_id, model=model)


def generate_directive_from_linear(
    ticket_data: dict,
    model: str = "sonnet",
) -> list[dict]:
    """Generate directives from a Linear ticket dict (as returned by MCP).

    Args:
        ticket_data: Linear ticket dict; expects keys like id, title, description,
                     priority, state, assignee, labels, etc.
        model: Suggested model tier.

    Returns:
        List of directive dicts.
    """
    lines: list[str] = []

    ticket_id = ticket_data.get("identifier") or ticket_data.get("id", "")
    if ticket_id:
        lines.append(f"Ticket: {ticket_id}")

    title = ticket_data.get("title", "")
    if title:
        lines.append(f"Title: {title}")

    state = ticket_data.get("state", {})
    state_name = state.get("name", "") if isinstance(state, dict) else str(state)
    if state_name:
        lines.append(f"Status: {state_name}")

    priority = ticket_data.get("priority")
    if priority is not None:
        lines.append(f"Priority: {priority}")

    labels = ticket_data.get("labels", [])
    if labels:
        label_names = [
            (lb.get("name", str(lb)) if isinstance(lb, dict) else str(lb))
            for lb in labels
        ]
        lines.append(f"Labels: {', '.join(label_names)}")

    assignee = ticket_data.get("assignee", {})
    if isinstance(assignee, dict) and assignee.get("name"):
        lines.append(f"Assignee: {assignee['name']}")

    description = ticket_data.get("description", "").strip()
    if description:
        lines.append("")
        lines.append("Description:")
        lines.append(description)

    comments = ticket_data.get("comments", [])
    if comments:
        lines.append("")
        lines.append("Comments:")
        for comment in comments:
            body = comment.get("body", "") if isinstance(comment, dict) else str(comment)
            author = (
                comment.get("user", {}).get("name", "unknown")
                if isinstance(comment, dict)
                else "unknown"
            )
            lines.append(f"  [{author}]: {body}")

    formatted = "\n".join(lines)
    return generate_directive(formatted, ticket_id=ticket_id, model=model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pit Boss — Directive Generator")
    parser.add_argument("spec", help="Path to spec file or '-' for stdin")
    parser.add_argument("--ticket", default="", help="Ticket ID")
    parser.add_argument("--model", default="sonnet", help="Suggested model")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.spec == "-":
        content = sys.stdin.read()
    else:
        content = Path(args.spec).read_text()

    directives = generate_directive(content, args.ticket, args.model)

    if args.json:
        print(json.dumps(directives, indent=2))
    else:
        for i, d in enumerate(directives, 1):
            print(f"\n{'='*60}")
            print(f"DIRECTIVE {i}: {d['title']}")
            print(f"Model: {d['model']} | Priority: {d['priority']}")
            print(f"{'='*60}")
            print(d['directive'])
