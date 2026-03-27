#!/usr/bin/env python3
"""headroom-monitor.py — Live token metrics pane for USS Tenkara.

Usage:
  python3 headroom-monitor.py [--port 8787] [--interval 2]

Keep open in a split pane alongside agent sessions.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from argparse import ArgumentParser
from typing import Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


LOG_PATH = "/tmp/uss-tenkara/headroom.log"
_PERF_RE = None

def _perf_re():
    global _PERF_RE
    if _PERF_RE is None:
        import re
        _PERF_RE = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*PERF "
            r"model=(\S+) msgs=\d+ "
            r"tok_before=(\d+) tok_after=(\d+) tok_saved=(\d+)"
            r".*transforms=(.+)$"
        )
    return _PERF_RE


def fetch_recent_from_log(n: int = 20) -> list:
    """Parse the last N PERF lines from the headroom log — bypasses 2-entry API limit."""
    try:
        with open(LOG_PATH, "r") as f:
            # Read last 200 lines efficiently
            lines = f.readlines()[-200:]
        results = []
        pat = _perf_re()
        for line in lines:
            m = pat.match(line)
            if not m:
                continue
            ts_str, model, before, after, saved, transforms = m.groups()
            before, after, saved = int(before), int(after), int(saved)
            total = before
            pct = (saved / total * 100) if total > 0 else 0
            results.append({
                "ts": ts_str[11:19],  # HH:MM:SS
                "model": model.replace("claude-", "").replace("-20251001", ""),
                "before": before,
                "after": after,
                "saved": saved,
                "pct": pct,
                "transforms": transforms.strip(),
            })
        return results[-n:]
    except Exception:
        return []


def fetch_stats(port: int) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/stats",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_display(data: Optional[dict], port: int, last_updated: float) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── Header ────────────────────────────────────────────────────────────
    age = time.time() - last_updated
    status = f"[green]LIVE[/green]  [dim]updated {age:.0f}s ago[/dim]" if data else "[red]OFFLINE[/red]"
    layout["header"].update(Panel(
        Text.from_markup(f"  ⚡ [bold yellow]HEADROOM TOKEN MONITOR[/bold yellow]  ·  port {port}  ·  {status}"),
        box=box.HEAVY_HEAD,
        style="yellow",
    ))

    # ── Footer ────────────────────────────────────────────────────────────
    layout["footer"].update(Text.from_markup(
        "  [dim]q[/dim] quit  ·  refreshes every 2s",
        justify="left",
    ))

    if not data:
        layout["left"].update(Panel("[red]Headroom is not running on port {}[/red]\n\nStart with:\n[dim]headroom proxy --port {}[/dim]".format(port, port), title="Status"))
        layout["right"].update(Panel("", title=""))
        return layout

    s        = data.get("summary", {})
    savings  = data.get("savings", {})
    cli      = data.get("cli_filtering", {})
    cc       = data.get("compression_cache", {})
    pc       = data.get("prefix_cache", {})
    pc_tot   = pc.get("totals", {})
    req      = data.get("requests", {})
    cost     = data.get("cost", {})
    comp     = s.get("compression", {})
    by_layer = savings.get("by_layer", {})
    recent   = data.get("recent_requests", [])

    total_saved  = savings.get("total_tokens", 0)
    cli_tokens   = by_layer.get("cli_filtering", {}).get("tokens", 0)
    cli_avg      = cli.get("avg_savings_pct", 0)
    cli_cmds     = cli.get("total_commands", 0)
    comp_tokens  = by_layer.get("compression", {}).get("tokens", 0)
    pc_tokens    = pc_tot.get("cache_read_tokens", 0)
    pc_hit_rate  = pc_tot.get("hit_rate", 0)
    pc_hit_reqs  = pc_tot.get("hit_requests", 0)
    pc_tot_reqs  = pc_tot.get("requests", 0)
    cc_entries   = cc.get("total_entries", 0)
    cc_hits      = cc.get("total_hits", 0)
    cc_misses    = cc.get("total_misses", 0)
    cc_hit_rate  = cc.get("hit_rate", 0)
    avg_pct      = comp.get("avg_compression_pct", 0)
    session_sent = cost.get("total_input_tokens", 0)
    per_model    = cost.get("per_model", {})
    session_saved = sum(m.get("tokens_saved", 0) for m in per_model.values())
    session_total = session_sent + session_saved
    session_pct   = (session_saved / session_total * 100) if session_total > 0 else 0

    # ── Left pane: totals + by layer ──────────────────────────────────────
    left_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    left_table.add_column("label", style="dim", width=22)
    left_table.add_column("value", justify="right")

    left_table.add_row("[bold white]TOTAL SAVED[/bold white]", f"[bold green]{fmt_tokens(total_saved)}[/bold green]")
    left_table.add_row("", "")
    left_table.add_row("[yellow]By layer[/yellow]", "")
    left_table.add_row(
        "RTK filtering",
        f"[cyan]{fmt_tokens(cli_tokens)}[/cyan]  [dim]{cli_avg:.0f}% avg · {cli_cmds:,} cmds[/dim]",
    )
    if pc_tokens:
        left_table.add_row(
            "Prefix cache",
            f"[cyan]{fmt_tokens(pc_tokens)}[/cyan]  [dim]{pc_hit_rate:.0f}% hit · {pc_hit_reqs}/{pc_tot_reqs} reqs[/dim]",
        )
    left_table.add_row(
        "ML compression",
        f"[cyan]{fmt_tokens(comp_tokens)}[/cyan]  [dim]{cc_entries} blocks · {cc_hit_rate:.0f}% cache hit[/dim]",
    )

    if session_sent:
        left_table.add_row("", "")
        left_table.add_row("[yellow]This session[/yellow]", "")
        left_table.add_row("Sent",  f"[white]{fmt_tokens(session_sent)}[/white]")
        left_table.add_row(
            "Saved",
            f"[green]{fmt_tokens(session_saved)}[/green]  [dim]({session_pct:.1f}%)[/dim]",
        )
        if per_model:
            left_table.add_row("", "")
            for model_name, m in sorted(per_model.items()):
                label = model_name.replace("claude-", "").replace("-20251001", "")
                sent  = m.get("tokens_sent", 0)
                saved = m.get("tokens_saved", 0)
                pct   = m.get("reduction_pct", 0)
                reqs  = m.get("requests", 0)
                left_table.add_row(
                    f"  [dim]{label}[/dim]",
                    f"[dim]{fmt_tokens(sent)} sent · {fmt_tokens(saved)} saved · {pct:.0f}% · {reqs}r[/dim]",
                )

    layout["left"].update(Panel(left_table, title="[bold]Savings[/bold]", border_style="yellow"))

    # ── Right pane: recent requests feed (from log, not API — no 2-entry limit) ──
    log_recent = fetch_recent_from_log(20)

    feed_table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1), expand=True)
    feed_table.add_column("time",    style="dim",    width=8)
    feed_table.add_column("model",   style="cyan",   width=10)
    feed_table.add_column("before",  justify="right", width=7)
    feed_table.add_column("after",   justify="right", width=7)
    feed_table.add_column("saved",   justify="right", style="green", width=7)
    feed_table.add_column("pct",     justify="right", width=6)
    feed_table.add_column("transforms", style="dim")

    for r in reversed(log_recent):
        pct = r["pct"]
        pct_color = "green" if pct > 20 else "yellow" if pct > 5 else "dim"
        feed_table.add_row(
            r["ts"],
            r["model"][:10],
            fmt_tokens(r["before"]),
            fmt_tokens(r["after"]),
            fmt_tokens(r["saved"]),
            f"[{pct_color}]{pct:.0f}%[/{pct_color}]",
            r["transforms"][:35],
        )

    if not log_recent:
        feed_table.add_row("—", "—", "—", "—", "—", "—", "no traffic yet")

    layout["right"].update(Panel(feed_table, title="[bold]Recent Requests[/bold]", border_style="yellow"))

    return layout


def main():
    parser = ArgumentParser(description="Live headroom token monitor")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    console = Console()
    last_data = None
    last_updated = 0.0

    try:
        with Live(console=console, refresh_per_second=2, screen=True) as live:
            while True:
                data = fetch_stats(args.port)
                if data is not None:
                    last_data = data
                    last_updated = time.time()
                live.update(build_display(last_data, args.port, last_updated))
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
