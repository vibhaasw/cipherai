#!/usr/bin/env python3
"""Extract continuation compression stats from Redis and print a comparison summary."""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import redis


MIN_SAMPLE_SIZE = 5
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EVENTS_KEY = "continuation_events"


@dataclass
class DomainStats:
    count: int = 0
    est_tokens_sent: list[float] = field(default_factory=list)
    est_tokens_if_uncompressed: list[float] = field(default_factory=list)
    tokens_saved_pct: list[float] = field(default_factory=list)
    compressed_events: int = 0
    fallback_events: int = 0

    def avg_sent(self) -> float | None:
        return sum(self.est_tokens_sent) / len(self.est_tokens_sent) if self.est_tokens_sent else None

    def avg_uncompressed(self) -> float | None:
        return (
            sum(self.est_tokens_if_uncompressed) / len(self.est_tokens_if_uncompressed)
            if self.est_tokens_if_uncompressed
            else None
        )

    def avg_saved_pct(self) -> float | None:
        return sum(self.tokens_saved_pct) / len(self.tokens_saved_pct) if self.tokens_saved_pct else None


def load_events(client: redis.Redis) -> list[dict[str, Any]]:
    raw = client.lrange(EVENTS_KEY, 0, -1)
    events: list[dict[str, Any]] = []
    for payload in raw:
        try:
            events.append(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            continue
    return events


def compute_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_domain: dict[str, DomainStats] = defaultdict(DomainStats)
    domain_counts: dict[str, int] = defaultdict(int)
    total_fallback = 0
    compressed_saved_pcts: list[float] = []

    all_sent: list[float] = []
    all_uncompressed: list[float] = []

    for event in events:
        domain = str(event.get("domain", "UNKNOWN"))
        domain_counts[domain] += 1
        stats = by_domain[domain]
        stats.count += 1

        sent = float(event.get("est_tokens_sent", 0))
        uncompressed = float(event.get("est_tokens_if_uncompressed", 0))
        saved_pct = float(event.get("tokens_saved_pct", 0))
        compression_used = str(event.get("compression_used", "")).lower()
        fallback_count = int(event.get("fallback_count", 0))
        eligible = bool(event.get("eligible_for_compression"))

        stats.est_tokens_sent.append(sent)
        stats.est_tokens_if_uncompressed.append(uncompressed)
        all_sent.append(sent)
        all_uncompressed.append(uncompressed)

        total_fallback += fallback_count
        if fallback_count > 0:
            stats.fallback_events += 1

        if domain == "CODE_GEN":
            continue

        if compression_used == "yes":
            stats.compressed_events += 1
            stats.tokens_saved_pct.append(saved_pct)
            compressed_saved_pcts.append(saved_pct)
        elif eligible and compression_used == "no":
            stats.tokens_saved_pct.append(saved_pct)

    overall_avg_sent = sum(all_sent) / len(all_sent) if all_sent else None
    overall_avg_uncompressed = (
        sum(all_uncompressed) / len(all_uncompressed) if all_uncompressed else None
    )
    overall_avg_saved = (
        sum(compressed_saved_pcts) / len(compressed_saved_pcts) if compressed_saved_pcts else None
    )

    domain_rows: list[dict[str, Any]] = []
    for domain in sorted(by_domain.keys()):
        stats = by_domain[domain]
        if domain == "CODE_GEN":
            domain_rows.append(
                {
                    "domain": domain,
                    "compression_used": "N/A (verbatim by design)",
                    "avg_est_tokens_sent": stats.avg_sent(),
                    "avg_est_tokens_if_uncompressed": stats.avg_uncompressed(),
                    "avg_tokens_saved_pct": None,
                    "avg_tokens_saved_pct_label": "N/A (verbatim by design)",
                    "sample_size": stats.count,
                    "compressed_events": 0,
                    "fallback_events": stats.fallback_events,
                }
            )
        else:
            avg_saved = stats.avg_saved_pct()
            domain_rows.append(
                {
                    "domain": domain,
                    "compression_used": (
                        f"yes ({stats.compressed_events}/{stats.count})"
                        if stats.compressed_events
                        else f"no (0/{stats.count})"
                    ),
                    "avg_est_tokens_sent": stats.avg_sent(),
                    "avg_est_tokens_if_uncompressed": stats.avg_uncompressed(),
                    "avg_tokens_saved_pct": avg_saved,
                    "avg_tokens_saved_pct_label": (
                        f"{avg_saved:.2f}%" if avg_saved is not None else "N/A (no compressed events)"
                    ),
                    "sample_size": stats.count,
                    "compressed_events": stats.compressed_events,
                    "fallback_events": stats.fallback_events,
                }
            )

    return {
        "total_events": len(events),
        "domain_counts": dict(sorted(domain_counts.items())),
        "overall_avg_est_tokens_sent": overall_avg_sent,
        "overall_avg_est_tokens_if_uncompressed": overall_avg_uncompressed,
        "overall_avg_tokens_saved_pct_compressed_only": overall_avg_saved,
        "ollama_fallback_triggers": total_fallback,
        "compressed_continuation_count": len(compressed_saved_pcts),
        "domain_rows": domain_rows,
    }


def fmt_num(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}{suffix}"


def print_report(events: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    total = stats["total_events"]
    print("=" * 72)
    print("CIPHER AI — Continuation Context Compression Report")
    print("Source: Redis list `continuation_events` (all stored records)")
    print("=" * 72)
    print()

    if total < MIN_SAMPLE_SIZE:
        print(
            f"WARNING: Limited sample size ({total} events) — recommend running "
            "additional continuation test cases before presenting this data to judges."
        )
        print()

    print(f"Total continuation events: {total}")
    if stats["domain_counts"]:
        print("Breakdown by domain:")
        for domain, count in stats["domain_counts"].items():
            print(f"  - {domain}: {count}")
    else:
        print("Breakdown by domain: (none recorded)")
    print()

    print("Per-domain aggregates:")
    print("-" * 72)
    if not stats["domain_rows"]:
        print("  No domain data — continuation_events is empty.")
    for row in stats["domain_rows"]:
        print(f"  Domain: {row['domain']} (N={row['sample_size']})")
        print(f"    Avg est_tokens_sent:           {fmt_num(row['avg_est_tokens_sent'])}")
        print(
            f"    Avg est_tokens_if_uncompressed: {fmt_num(row['avg_est_tokens_if_uncompressed'])}"
        )
        print(f"    Avg tokens_saved_pct:          {row['avg_tokens_saved_pct_label']}")
        print(f"    Compression used:              {row['compression_used']}")
        if row["fallback_events"]:
            print(f"    Events with Ollama fallback:   {row['fallback_events']}")
        print()

    print("Overall (all events):")
    print(f"  Avg est_tokens_sent:              {fmt_num(stats['overall_avg_est_tokens_sent'])}")
    print(
        "  Avg est_tokens_if_uncompressed:     "
        f"{fmt_num(stats['overall_avg_est_tokens_if_uncompressed'])}"
    )
    print(
        "  Avg tokens_saved_pct (compressed):  "
        f"{fmt_num(stats['overall_avg_tokens_saved_pct_compressed_only'], '%')}"
        if stats["overall_avg_tokens_saved_pct_compressed_only"] is not None
        else "  Avg tokens_saved_pct (compressed):  N/A (no compressed continuations recorded)"
    )
    print(
        f"  Ollama fallback triggers (sum of fallback_count): {stats['ollama_fallback_triggers']}"
    )
    print(
        f"  Compressed continuation events: {stats['compressed_continuation_count']} / {total}"
    )
    print()
    print("=" * 72)


def main() -> int:
    try:
        client = redis.from_url(REDIS_URL, decode_responses=True)
        client.ping()
    except redis.RedisError as exc:
        print(f"ERROR: Could not connect to Redis at {REDIS_URL}: {exc}", file=sys.stderr)
        return 1

    events = load_events(client)
    stats = compute_stats(events)
    print_report(events, stats)

    out_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "docs",
        "comparison_report_stats.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2)
    print(f"Stats JSON written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
