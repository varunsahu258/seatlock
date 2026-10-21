#!/usr/bin/env python3
"""Extract sampled seat outcomes emitted by k6 into a replayable JSON timeline."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import TypedDict

import asyncpg


class SeatEvent(TypedDict):
    t: int
    seat: int
    outcome: str


async def extract(log_path: Path, output_path: Path, show_id: int, database_url: str) -> int:
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(database_url)
    try:
        rows = await connection.fetch("SELECT id, label FROM seats WHERE show_id = $1", show_id)
        seat_labels = {int(row["id"]): int(row["label"]) for row in rows}
    finally:
        await connection.close()
    events: list[SeatEvent] = []
    for line in log_path.read_text(errors="replace").splitlines():
        marker = "SEATLOCK_EVENT "
        if marker not in line:
            continue
        try:
            event_text = line.split(marker, 1)[1].split('" source=console', 1)[0]
            event = json.loads(event_text.replace('\\"', '"'))
            if event["outcome"] not in {"winner", "rejected"}:
                continue
            events.append(
                {
                    "t": int(event["t"]),
                    "seat": seat_labels[int(event["seat"])],
                    "outcome": event["outcome"],
                }
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    events.sort(key=lambda event: event["t"])
    output_path.write_text(json.dumps(events, separators=(",", ":")) + "\n")
    return len(events)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, default=Path("loadtest/events.json"))
    parser.add_argument("--show-id", type=int, required=True)
    parser.add_argument(
        "--database-url",
        default="postgresql://seatlock:seatlock@localhost:5432/seatlock",
    )
    args = parser.parse_args()
    count = asyncio.run(extract(args.log, args.output, args.show_id, args.database_url))
    print(f"Wrote {count} sampled seat events to {args.output}")


if __name__ == "__main__":
    main()
