"""Reshape the harness output into the JSON the results page reads.

The two results files sit in the repository root and carry the full run
config. GitHub Pages serves only docs/, so the page needs its own copy, and
taking it through a script keeps the page from drifting: it always shows the
last run rather than numbers someone typed in.

    python3 scripts/make_page_data.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "data"

# Everything the page plots. Dropping the rest keeps the payload small and
# makes it obvious that nothing here is derived.
KEEP = (
    "scenario", "mode", "concurrency", "think_ms", "lease_ttl_ms", "contend_ms",
    "trials", "agents", "double_bookings", "lost_intents", "defects",
    "double_per_100_agents", "lost_per_100_trials",
    "overhead_p50_ms", "overhead_p95_ms", "overhead_p99_ms",
    "mean_attempts", "outcomes",
)


def trim(rows: list[dict]) -> list[dict]:
    return [{k: r[k] for k in KEEP if k in r} for r in rows]


def main() -> None:
    sweep = json.loads((ROOT / "results.sweep.json").read_text())
    anomalies = json.loads((ROOT / "results.anomalies.json").read_text())

    payload = {
        "ran": sweep.get("started"),
        "config": sweep.get("config", {}),
        # The booking race, swept over how many agents and how long each one
        # looks at the screen before clicking.
        "sweep": trim(sweep["sweep"]),
        # The same harness pointed at two other ways concurrent edits collide.
        "anomalies": trim(anomalies["anomalies"]),
        # How long a lease may be held, which is the one knob a lease has.
        "lease_ttl": trim(sweep.get("lease_ttl", [])),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "runs.json"
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1024:.1f} kB")


if __name__ == "__main__":
    main()
