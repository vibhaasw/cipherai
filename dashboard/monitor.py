"""Live terminal dashboard for CipherAI provider/key health."""

from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Any

import httpx
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

STATUS_COLOR = {
    "healthy": "green",
    "near_cap": "yellow",
    "cooling_down": "red",
}


def _build_table(rows: list[dict[str, Any]]) -> Table:
    """Build a rich table from /status response rows."""
    table = Table(title="CipherAI Local Telemetry", expand=True)
    table.add_column("Provider")
    table.add_column("Credential Ref")
    table.add_column("Status")
    table.add_column("Remaining Requests", justify="right")
    table.add_column("Remaining Tokens", justify="right")
    table.add_column("Reset In (s)", justify="right")

    now = time.time()
    for row in sorted(rows, key=lambda r: (r.get("provider", ""), r.get("credential_ref", ""))):
        provider = str(row.get("provider", "-"))
        credential_ref = str(row.get("credential_ref", "-"))
        status = str(row.get("status", "-"))
        remaining_requests = str(row.get("remaining_requests", "-"))
        remaining_tokens = str(row.get("remaining_tokens", "-"))
        reset_in = "-"

        if status == "cooling_down":
            try:
                reset_at = float(row.get("reset_requests_at", 0))
                delta = max(int(reset_at - now), 0)
                reset_in = str(delta)
            except (TypeError, ValueError):
                reset_in = "-"

        color = STATUS_COLOR.get(status, "white")
        table.add_row(
            provider,
            credential_ref,
            f"[{color}]{status}[/{color}]",
            remaining_requests,
            remaining_tokens,
            reset_in,
        )

    if not rows:
        table.add_row("-", "-", "[yellow]No quota data yet[/yellow]", "-", "-", "-")

    return table


async def _fetch_status(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Fetch quota status from local API."""
    response = await client.get("http://localhost:8000/status")
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return []


def _build_continuation_table(events: list[dict[str, Any]]) -> Table:
    """Build a continuation-events table from /continuations data."""
    table = Table(title="Recent Continuations", expand=True)
    table.add_column("Time")
    table.add_column("Domain")
    table.add_column("From → To")
    table.add_column("Compression")
    table.add_column("Tokens Saved %", justify="right")
    table.add_column("Status")

    for event in events:
        ts = int(event.get("timestamp", 0) or 0)
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts > 0 else "-"
        domain = str(event.get("domain", "-"))
        from_to = f"{event.get('original_provider', '-')}" + " → " + f"{event.get('fallback_provider', '-')}"
        compression = str(event.get("compression_used", "N/A"))
        if compression.lower() == "yes":
            compression_display = "✓"
        elif compression.lower() == "no":
            compression_display = "✗"
        else:
            compression_display = "N/A"

        if domain == "CODE_GEN":
            saved_display = "[dim]N/A[/dim]"
        else:
            pct = float(event.get("tokens_saved_pct", 0.0))
            if pct > 30:
                saved_display = f"[green]{pct:.1f}%[/green]"
            elif pct >= 0:
                saved_display = f"[yellow]{pct:.1f}%[/yellow]"
            else:
                saved_display = "[dim]N/A[/dim]"

        status = str(event.get("final_status", "-"))
        status_display = f"[green]{status}[/green]" if status == "success" else f"[red]{status}[/red]"
        table.add_row(when, domain, from_to, compression_display, saved_display, status_display)

    return table


def _build_summary_line(summary: dict[str, Any]) -> Text:
    """Build running continuation summary line from aggregate stats."""
    total_count = int(summary.get("total_count", 0))
    avg_saved = float(summary.get("avg_tokens_saved_pct", 0.0))
    compressed_count = int(summary.get("compressed_count", 0))
    eligible_count = int(summary.get("eligible_count", 0))
    fallback_count = int(summary.get("fallback_count", 0))
    return Text(
        (
            f"Continuations: {total_count} total | "
            f"Avg tokens saved (compressed domains only): {avg_saved:.1f}% | "
            f"Compression used: {compressed_count}/{eligible_count} eligible calls | "
            f"Ollama fallback triggered: {fallback_count} times"
        ),
        style="bold magenta",
    )


async def _fetch_continuations(client: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch continuation events and summary from local API."""
    response = await client.get("http://localhost:8000/continuations")
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        events = data.get("events")
        summary = data.get("summary")
        if isinstance(events, list) and isinstance(summary, dict):
            return {"events": events, "summary": summary}
    return {"events": [], "summary": {}}


async def run_monitor() -> None:
    """Run live updating dashboard that polls the API every 1.5 seconds."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        with Live(refresh_per_second=4, screen=False) as live:
            while True:
                last_updated = datetime.now().strftime("%H:%M:%S")
                try:
                    rows = await _fetch_status(client)
                    continuation_payload = await _fetch_continuations(client)
                    table = _build_table(rows)
                    events = continuation_payload["events"]
                    summary_line = _build_summary_line(continuation_payload["summary"])
                    if events:
                        continuation_table = _build_continuation_table(events)
                    else:
                        continuation_table = Text("No continuations recorded yet", style="bold yellow")
                    note = Text(
                        "Note: a provider/key only appears here after its first request — ranks that haven't been tried yet (due to higher-ranked candidates succeeding first) won't show until exercised.",
                        style="dim",
                    )
                    shared_note = Text("Multiple models using the same credential share one quota row.", style="dim")
                    footer = Text(f"Last Updated: {last_updated}", style="bold cyan")
                    live.update(Group(table, note, shared_note, continuation_table, summary_line, footer))
                except httpx.HTTPError:
                    waiting = Text("Waiting for CipherAI API at http://localhost:8000 ...", style="bold yellow")
                    note = Text(
                        "Note: a provider/key only appears here after its first request — ranks that haven't been tried yet (due to higher-ranked candidates succeeding first) won't show until exercised.",
                        style="dim",
                    )
                    shared_note = Text("Multiple models using the same credential share one quota row.", style="dim")
                    empty_continuations = Text("No continuations recorded yet", style="bold yellow")
                    summary_line = Text(
                        "Continuations: 0 total | Avg tokens saved (compressed domains only): 0.0% | Compression used: 0/0 eligible calls | Ollama fallback triggered: 0 times",
                        style="bold magenta",
                    )
                    footer = Text(f"Last Updated: {last_updated}", style="bold cyan")
                    live.update(Group(waiting, note, shared_note, empty_continuations, summary_line, footer))
                await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(run_monitor())
