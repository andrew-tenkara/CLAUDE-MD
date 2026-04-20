#!/usr/bin/env python3
"""
Token Savings TUI: unified RTK + Headroom dashboard.
Cross-platform (Windows, macOS, Linux). Requires: rich (pip install rich)

Usage: python3 /path/to/tui.py
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Error: 'rich' package not found. Install with one of:")
    print("  pip install rich")
    print("  pipx install rich")
    print("  python3 -m pip install --user rich")
    sys.exit(1)

HEADROOM_URL = "http://localhost:8787"
REFRESH_INTERVAL = 2
MAX_RECENT = 30
NARROW_THRESHOLD = 200  # columns; below this, stack vertically and hide extras
console = Console()

# Headroom log path — same as Tower's headroom-monitor.py
HEADROOM_LOG = "/tmp/uss-tenkara/headroom.log"

# Regex to parse PERF lines from headroom log
_PERF_RE = None
def _perf_re():
    global _PERF_RE
    if _PERF_RE is None:
        _PERF_RE = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*PERF "
            r"model=(\S+) msgs=\d+ "
            r"tok_before=(\d+) tok_after=(\d+) tok_saved=(\d+)"
            r".*transforms=(.+)$"
        )
    return _PERF_RE


def fetch_recent_from_log(n=MAX_RECENT):
    """Parse last N PERF lines from headroom log. No entry limit."""
    try:
        with open(HEADROOM_LOG, "r") as f:
            lines = f.readlines()[-300:]
        results = []
        pat = _perf_re()
        for line in lines:
            m = pat.match(line)
            if not m:
                continue
            ts_str, model, before, after, saved, transforms = m.groups()
            before, after, saved = int(before), int(after), int(saved)
            pct = (saved / before * 100) if before > 0 else 0
            results.append({
                "ts": ts_str[11:19],
                "model": model,
                "before": before,
                "after": after,
                "saved": saved,
                "pct": pct,
                "transforms": transforms.strip(),
            })
        return results[-n:]
    except (FileNotFoundError, PermissionError):
        return []


def fmt(n):
    if n is None:
        return "-"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def get_rtk_stats():
    try:
        r = subprocess.run(["rtk", "gain"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        out = r.stdout
        stats = {"version": "?", "total_commands": 0, "tokens_saved": 0, "savings_pct": 0.0, "top": []}
        ver = subprocess.run(["rtk", "--version"], capture_output=True, text=True, timeout=5)
        if ver.returncode == 0:
            stats["version"] = ver.stdout.strip()
        for line in out.splitlines():
            if "Total commands" in line:
                stats["total_commands"] = int(line.split()[-1].replace(",", ""))
            elif "Tokens saved" in line:
                for p in line.split():
                    if p.endswith("M"):
                        stats["tokens_saved"] = float(p.rstrip("M")) * 1e6
                    elif p.endswith("B"):
                        stats["tokens_saved"] = float(p.rstrip("B")) * 1e9
                    elif p.endswith("K"):
                        stats["tokens_saved"] = float(p.rstrip("K")) * 1e3
                    elif p.startswith("(") and p.endswith("%)"):
                        stats["savings_pct"] = float(p.strip("(%)"))
        for line in out.splitlines():
            s = line.strip()
            if s and s[0].isdigit() and "." in s[:4]:
                parts = s.split()
                if len(parts) >= 6:
                    stats["top"].append({
                        "cmd": " ".join(parts[1:3]),
                        "count": parts[3],
                        "saved": parts[4],
                        "pct": parts[5],
                    })
        return stats
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def get_headroom_stats():
    try:
        req = urllib.request.Request(f"{HEADROOM_URL}/stats")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return None


def get_headroom_version():
    try:
        req = urllib.request.Request(f"{HEADROOM_URL}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read().decode()).get("version", "?")
    except Exception:
        return None


def build_rtk_panel(stats, narrow=False):
    if stats is None:
        return Panel(
            Text("NOT INSTALLED\n\nInstall: brew install rtk-ai/tap/rtk", style="yellow"),
            title="[bold]RTK[/bold]" if narrow else "[bold]RTK: Command Output Filtering[/bold]",
            border_style="yellow",
        )
    t = Table(show_header=False, box=None, padding=(0, 1 if narrow else 2))
    t.add_column("k", style="dim", width=12 if narrow else 16, no_wrap=True)
    t.add_column("v", no_wrap=True, overflow="ellipsis")
    t.add_row("Status", f"[green]ACTIVE[/green] ({stats['version']})")
    t.add_row("Tokens saved", f"[cyan]{fmt(stats['tokens_saved'])}[/cyan]")
    t.add_row("Commands", f"{stats['total_commands']:,}")
    pct = stats["savings_pct"] / 100
    bar_width = 15 if narrow else 25
    bar = "█" * int(pct * bar_width) + "░" * (bar_width - int(pct * bar_width))
    t.add_row("CLI reduction", f"[green]{bar}[/green] {stats['savings_pct']:.1f}%")
    if stats["top"]:
        t.add_row("", "")
        t.add_row("[bold]Top Commands[/bold]", "")
        limit = 4 if narrow else 6
        for c in stats["top"][:limit]:
            t.add_row(f"  {c['cmd']}", f"{c['count']:>6} {c['saved']:>7} {c['pct']:>5}")
    return Panel(t, title="[bold]RTK[/bold]" if narrow else "[bold]RTK: Command Output Filtering[/bold]", border_style="green")


def build_headroom_panel(stats, narrow=False):
    title = "[bold]Headroom[/bold]" if narrow else "[bold]Headroom: Session Compression[/bold]"
    if stats is None:
        ver = get_headroom_version()
        if ver:
            return Panel(Text(f"RUNNING (v{ver}) but /stats unavailable", style="yellow"),
                         title=title, border_style="yellow")
        return Panel(Text("NOT RUNNING\n\nStart: headroom proxy --port 8787", style="yellow"),
                     title=title, border_style="yellow")

    t = Table(show_header=False, box=None, padding=(0, 1 if narrow else 2))
    t.add_column("k", style="dim", width=12 if narrow else 16, no_wrap=True)
    t.add_column("v", no_wrap=True, overflow="ellipsis")

    ver = get_headroom_version() or "?"
    t.add_row("Status", f"[green]LIVE on :8787[/green] (v{ver})")

    comp = stats.get("summary", {}).get("compression", {})
    t.add_row("Compressed", f"[cyan]{fmt(comp.get('total_tokens_removed', 0))}[/cyan] ({comp.get('avg_compression_pct', 0):.1f}%)")

    cache = stats.get("prefix_cache", {}).get("totals", {})
    rate = cache.get("hit_rate", 0)
    rate = rate / 100 if rate > 1 else rate
    if narrow:
        t.add_row("Cache", f"{rate:.0%} hit, ${cache.get('savings_usd', 0):.2f}")
    else:
        t.add_row("Prefix cache", f"{rate:.0%} hit ({fmt(cache.get('cache_read_tokens', 0))} reads, ${cache.get('savings_usd', 0):.2f})")
        t.add_row("", "[dim]Anthropic-side cache; Headroom keeps prefix stable for hits[/dim]")

    overhead = stats.get("overhead", {})
    t.add_row("Overhead", f"{overhead.get('average_ms', 0):.0f}ms avg")
    t.add_row("Requests", f"{stats.get('summary', {}).get('api_requests', 0):,}")

    # Per-model
    per_model = stats.get("cost", {}).get("per_model", {})
    if per_model:
        t.add_row("", "")
        t.add_row("[bold]Per-Model[/bold]", "")
        for model, d in sorted(per_model.items()):
            short = model.replace("claude-", "")
            for sfx in ["-20250514", "-20250929", "-20251001", "-20251101", "-20260101"]:
                short = short.replace(sfx, "")
            if narrow:
                t.add_row(f"  {short[:10]}", f"{fmt(d.get('tokens_saved', 0))} saved {d.get('reduction_pct', 0):.0f}%")
            else:
                t.add_row(f"  {short}", f"{fmt(d.get('tokens_sent', 0)):>7} sent {fmt(d.get('tokens_saved', 0)):>7} saved {d.get('reduction_pct', 0):>5.1f}% ({d.get('requests', 0)})")

    return Panel(t, title=title, border_style="blue")


def build_footer(rtk, hr):
    rtk_saved = rtk["tokens_saved"] if rtk else 0
    hr_comp = hr.get("savings", {}).get("by_layer", {}).get("compression", {}).get("tokens", 0) if hr else 0
    hr_total = hr.get("savings", {}).get("total_tokens", 0) if hr else 0
    cost = hr.get("cost", {}) if hr else {}
    cache_usd = hr.get("prefix_cache", {}).get("totals", {}).get("savings_usd", 0) if hr else 0

    text = Text()
    text.append("Combined: ", style="bold")
    text.append(f"{rtk_saved + hr_comp:,.0f} tokens filtered", style="bold green")
    text.append(f"  (RTK: {fmt(rtk_saved)} + Headroom compression: {fmt(hr_comp)})")
    if cache_usd > 0:
        text.append(f"\n  Prefix cache (Anthropic-side): ${cache_usd:.2f} saved", style="dim")
    if cost.get("savings_usd", 0) > 0:
        text.append(f"  |  Total cost savings: ${cost['savings_usd']:.2f}")
    text.append(f"\n  Last refresh: {datetime.now().strftime('%H:%M:%S')}  |  Every {REFRESH_INTERVAL}s  |  Ctrl+C to exit")

    return Panel(text, border_style="bright_white")


def build_recent_panel(stats, narrow=False):
    # Read from log file (same as Tower) — no 10-entry API limit
    log_recent = fetch_recent_from_log(MAX_RECENT)

    # Fallback to API if log not available
    if not log_recent and stats:
        api_recent = stats.get("recent_requests", [])
        for r in api_recent:
            model = r.get("model", "?")
            log_recent.append({
                "ts": r.get("timestamp", "")[-8:],
                "model": model,
                "before": r.get("input_tokens_original", 0),
                "after": r.get("input_tokens_optimized", 0),
                "saved": r.get("tokens_saved", 0),
                "pct": r.get("savings_percent", 0),
                "transforms": ", ".join(r.get("transforms_applied", [])[:3]),
            })

    if not log_recent:
        return Panel(Text("No traffic yet", style="dim"), title="[bold]Recent Requests[/bold]", border_style="dim")

    t = Table(box=None, padding=(0, 1), expand=True)
    if narrow:
        # Compact: time, model, after (original), before (compressed), saved, pct
        # Headroom log: tok_before = compressed, tok_after = original (with proxy overhead)
        t.add_column("time", style="dim", width=5, no_wrap=True)
        t.add_column("model", width=10, no_wrap=True, overflow="ellipsis")
        t.add_column("before", justify="right", width=7, no_wrap=True)
        t.add_column("after", justify="right", width=7, no_wrap=True)
        t.add_column("saved", justify="right", width=7, no_wrap=True)
        t.add_column("pct", justify="right", width=4, no_wrap=True)
    else:
        t.add_column("time", style="dim", width=8, no_wrap=True)
        t.add_column("model", width=14, no_wrap=True, overflow="ellipsis")
        t.add_column("before", justify="right", width=8, no_wrap=True)
        t.add_column("after", justify="right", width=8, no_wrap=True)
        t.add_column("saved", justify="right", width=8, no_wrap=True)
        t.add_column("pct", justify="right", width=5, no_wrap=True)
        t.add_column("transforms", no_wrap=True, overflow="ellipsis")

    limit = 15 if narrow else MAX_RECENT
    for r in reversed(log_recent[-limit:]):
        model = r["model"].replace("claude-", "")
        for sfx in ["-20250514", "-20250929", "-20251001", "-20251101", "-20260101"]:
            model = model.replace(sfx, "")

        pct = r["pct"]
        if pct >= 40:
            pct_style = "bold green"
        elif pct >= 10:
            pct_style = "green"
        elif pct > 0:
            pct_style = "yellow"
        else:
            pct_style = "dim"

        is_noop = r["after"] >= r["before"]
        noop_marker = " [dim]noop[/dim]" if is_noop else ""

        if narrow:
            transforms = r.get("transforms", "")
            t.add_row(
                r["ts"][-5:],
                model[:10],
                fmt(r["before"]),
                fmt(r["after"]),
                fmt(r["saved"]),
                Text(f"{pct:.0f}%", style=pct_style) if not is_noop else Text("noop", style="dim"),
            )
            t.add_row(
                "",
                Text(transforms[:30] if transforms else "—", style="dim italic"),
                "", "", "", "",
            )
        else:
            t.add_row(
                r["ts"],
                model,
                fmt(r["before"]),
                fmt(r["after"]),
                fmt(r["saved"]),
                Text(f"{pct:.0f}%", style=pct_style) if not is_noop else Text("noop", style="dim"),
                Text(r["transforms"][:35], style="dim"),
            )

    footer = Text()
    footer.append(f"  {len(log_recent)} entries", style="dim")
    if not narrow:
        footer.append("  |  noop rows with after > before = proxy metadata overhead (~0.3%)", style="dim")

    content = Table(box=None, padding=0, expand=True)
    content.add_column(ratio=1)
    content.add_row(t)
    content.add_row(footer)

    return Panel(content, title="[bold]Recent Requests[/bold]", border_style="cyan")


def build_layout(rtk, hr):
    narrow = console.width < NARROW_THRESHOLD
    layout = Layout()

    if narrow:
        # Stacked vertical layout for half-screen / small terminals
        layout.split_column(
            Layout(name="header", size=1),
            Layout(build_rtk_panel(rtk, narrow=True), name="rtk", ratio=1),
            Layout(build_headroom_panel(hr, narrow=True), name="headroom", ratio=1),
            Layout(build_recent_panel(hr, narrow=True), name="recent", ratio=1),
            Layout(name="footer", size=3),
        )
    else:
        # Side-by-side layout for wide terminals
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="main"),
            Layout(name="footer", size=4),
        )
        layout["main"].split_row(
            Layout(name="stats", ratio=1),
            Layout(build_recent_panel(hr, narrow=False), name="recent", ratio=1),
        )
        layout["stats"].split_column(
            Layout(build_rtk_panel(rtk, narrow=False), name="rtk"),
            Layout(build_headroom_panel(hr, narrow=False), name="headroom"),
        )

    layout["header"].update(Text(" TOKEN SAVINGS MONITOR", style="bold white on dark_green", justify="center"))
    layout["footer"].update(build_footer(rtk, hr))
    return layout


def main():
    try:
        with Live(console=console, refresh_per_second=1, screen=True) as live:
            while True:
                rtk = get_rtk_stats()
                hr = get_headroom_stats()
                live.update(build_layout(rtk, hr))
                time.sleep(REFRESH_INTERVAL)
    except KeyboardInterrupt:
        console.clear()
        console.print("[bold]Monitor stopped.[/bold]")


if __name__ == "__main__":
    main()
