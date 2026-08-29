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
    table.add_column("Key ID")
    table.add_column("Status")
    table.add_column("Remaining Requests", justify="right")
    table.add_column("Remaining Tokens", justify="right")
    table.add_column("Reset In (s)", justify="right")

    now = time.time()
    for row in sorted(rows, key=lambda r: (r.get("provider", ""), r.get("key_id", ""))):
        provider = str(row.get("provider", "-"))
        key_id = str(row.get("key_id", "-"))
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
            key_id,
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


async def run_monitor() -> None:
    """Run live updating dashboard that polls the API every 1.5 seconds."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        with Live(refresh_per_second=4, screen=False) as live:
            while True:
                last_updated = datetime.now().strftime("%H:%M:%S")
                try:
                    rows = await _fetch_status(client)
                    table = _build_table(rows)
                    note = Text(
                        "Note: a provider/key only appears here after its first request — ranks that haven't been tried yet (due to higher-ranked candidates succeeding first) won't show until exercised.",
                        style="dim",
                    )
                    footer = Text(f"Last Updated: {last_updated}", style="bold cyan")
                    live.update(Group(table, note, footer))
                except httpx.HTTPError:
                    waiting = Text("Waiting for CipherAI API at http://localhost:8000 ...", style="bold yellow")
                    note = Text(
                        "Note: a provider/key only appears here after its first request — ranks that haven't been tried yet (due to higher-ranked candidates succeeding first) won't show until exercised.",
                        style="dim",
                    )
                    footer = Text(f"Last Updated: {last_updated}", style="bold cyan")
                    live.update(Group(waiting, note, footer))
                await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(run_monitor())
