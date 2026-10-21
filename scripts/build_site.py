#!/usr/bin/env python3
"""Build the static showcase site and its OpenAPI specification."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


def main() -> None:
    SITE.mkdir(exist_ok=True)
    (SITE / "openapi.json").write_text(json.dumps(app.openapi(), indent=2) + "\n")
    for artifact in ("results.json", "events.json"):
        source = ROOT / "loadtest" / artifact
        if source.exists():
            shutil.copyfile(source, SITE / artifact)
        else:
            (SITE / artifact).write_text("{}\n" if artifact == "results.json" else "[]\n")
    print(f"Static site built in {SITE}")


if __name__ == "__main__":
    main()
