#!/usr/bin/env python3
"""Verify flash-sale booking invariants and save a benchmark result."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from redis.asyncio import Redis, from_url

ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = ROOT / "loadtest" / "results.json"
BENCHMARKS_PATH = ROOT / "docs" / "benchmarks.md"


def hardware_description() -> str:
    details = (
        f"{platform.system()} {platform.release()}, {platform.machine()}, "
        f"{os.cpu_count()} logical CPUs"
    )
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            memory_gib = int(result.stdout.strip()) / 1024**3
            details += f", {memory_gib:.1f} GiB RAM"
    extra = os.getenv("LOADTEST_HARDWARE_DETAIL")
    if extra:
        details += f"; {extra}"
    return details


def k6_metrics(summary_path: Path) -> dict[str, float | int | None]:
    summary = json.loads(summary_path.read_text())
    metrics = summary.get("metrics", {})
    duration_metric = metrics.get("http_req_duration", {})
    request_metric = metrics.get("http_reqs", {})
    failure_metric = metrics.get("http_req_failed", {})
    duration = duration_metric.get("values", duration_metric)
    requests = request_metric.get("values", request_metric)
    failures = failure_metric.get("values", failure_metric)
    return {
        "p50_latency_ms": duration.get("p(50)", duration.get("med")),
        "p95_latency_ms": duration.get("p(95)"),
        "p99_latency_ms": duration.get("p(99)"),
        "throughput_requests_per_second": requests.get("rate"),
        "http_request_count": requests.get("count"),
        "http_request_failure_rate_percent": (
            100 * failures.get("value") if failures.get("value") is not None else None
        ),
        "confirmed_flows": metrics.get("seatlock_confirmed_bookings", {}).get("count"),
        "seat_contention": metrics.get("seatlock_seat_unavailable", {}).get("count"),
        "flow_failures": metrics.get("seatlock_flow_failures", {}).get("count"),
    }


async def verify(args: argparse.Namespace) -> dict[str, Any]:
    database_url = args.database_url or os.getenv(
        "DATABASE_URL", "postgresql://seatlock:seatlock@localhost:5432/seatlock"
    )
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(database_url)
    redis: Redis = from_url(
        args.redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    try:
        double_bookings = await connection.fetchval(
            """
            SELECT count(*) FROM (
                SELECT seat_id FROM bookings
                WHERE show_id = $1 AND status IN ('confirmed', 'paid')
                GROUP BY seat_id HAVING count(*) > 1
            ) AS duplicate_seats
            """,
            args.show_id,
        )
        confirmed_bookings = await connection.fetchval(
            "SELECT count(*) FROM bookings WHERE show_id = $1 AND status IN ('confirmed', 'paid')",
            args.show_id,
        )
        number_sold = await connection.fetchval(
            """
            SELECT count(DISTINCT seat_id) FROM bookings
            WHERE show_id = $1 AND status IN ('confirmed', 'paid')
            """,
            args.show_id,
        )
        booking_rows = await connection.fetch(
            """
            SELECT b.id, b.seat_id, b.user_id, b.payment_ref,
                   EXISTS (
                       SELECT 1 FROM payments p
                       WHERE p.booking_ref = b.id::text
                         AND p.status IN ('pending', 'paid', 'success')
                   ) AS has_payment
            FROM bookings b
            WHERE b.show_id = $1 AND b.status IN ('confirmed', 'paid')
            """,
            args.show_id,
        )
        missing_payment_or_hold: list[int] = []
        for booking in booking_rows:
            held_by = await redis.get(f"hold:{args.show_id}:{booking['seat_id']}")
            if not booking["has_payment"] and held_by != booking["user_id"]:
                missing_payment_or_hold.append(booking["id"])
    finally:
        await connection.close()
        await redis.aclose()

    latency = (
        k6_metrics(Path(args.k6_summary))
        if args.k6_summary
        else {
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "p99_latency_ms": None,
            "throughput_requests_per_second": None,
            "http_request_count": None,
        }
    )
    violations = {
        "double_bookings": int(double_bookings),
        "confirmed_bookings_over_capacity": int(confirmed_bookings) > args.seat_capacity,
        "missing_payment_or_valid_hold_booking_ids": missing_payment_or_hold,
    }
    result: dict[str, Any] = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "show_id": args.show_id,
        "seat_capacity": args.seat_capacity,
        "confirmed_bookings": int(confirmed_bookings),
        "number_sold": int(number_sold),
        "double_bookings": int(double_bookings),
        "invariants": violations,
        "latency": latency,
        "hardware": hardware_description(),
        "k6_command": args.command,
    }
    RESULTS_PATH.write_text(json.dumps(result, indent=2) + "\n")
    append_benchmark(result)
    if (
        result["double_bookings"] != 0
        or violations["confirmed_bookings_over_capacity"]
        or missing_payment_or_hold
    ):
        raise SystemExit(f"Load-test invariants failed; see {RESULTS_PATH}")
    print(json.dumps(result, indent=2))
    return result


def append_benchmark(result: dict[str, Any]) -> None:
    BENCHMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not BENCHMARKS_PATH.exists():
        BENCHMARKS_PATH.write_text("# Seatlock load-test benchmarks\n")
    latency = result["latency"]
    with BENCHMARKS_PATH.open("a") as report:
        report.write(
            f"\n## {result['run_at_utc']} — show {result['show_id']}\n\n"
            f"- Hardware: {result['hardware']}\n"
            f"- k6 command: `{result['k6_command']}`\n"
            f"- Results: p50 {latency['p50_latency_ms']} ms, "
            f"p95 {latency['p95_latency_ms']} ms, p99 {latency['p99_latency_ms']} ms, "
            f"throughput {latency['throughput_requests_per_second']} req/s; "
            f"HTTP failures {latency.get('http_request_failure_rate_percent')}%.\n"
            f"- Sold: {result['number_sold']}/{result['seat_capacity']}; "
            f"double bookings: {result['double_bookings']}.\n"
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-id", type=int, required=True)
    parser.add_argument("--seat-capacity", type=int, default=500)
    parser.add_argument("--database-url")
    parser.add_argument("--redis-url")
    parser.add_argument("--k6-summary")
    parser.add_argument("--command", default="not recorded")
    args = parser.parse_args()
    await verify(args)


if __name__ == "__main__":
    asyncio.run(main())
