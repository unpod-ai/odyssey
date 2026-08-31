"""odyssey-sdk (Python) — a runnable walkthrough of every resource.

Prerequisites: a real `services/api` instance reachable at the URL below
(see `sdk/examples/README.md` for how to stand one up with sample data).

Run from the repo root:

    uv run python sdk/examples/python/basic_usage.py [base_url]
"""

from __future__ import annotations

import sys

from odyssey_sdk import OdysseyAPINotFoundError, OdysseySDK


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    client = OdysseySDK(base_url)

    print("health:", client.health())

    journeys = client.journeys.list()
    print(f"{len(journeys)} journey(s)")
    for j in journeys[:3]:
        print(" -", j.journey_id)

    if journeys:
        detail = client.journeys.get(journeys[0].journey_id)
        print("first journey detail:", detail.journey_id, "complete =", detail.complete)

    try:
        client.journeys.get("does-not-exist")
    except OdysseyAPINotFoundError:
        print("journeys.get('does-not-exist') -> 404, as expected")

    print("datasets:", [d.name for d in client.datasets.list()])
    print("models:", [m.name for m in client.models.list()])
    print("eval runs:", [r.benchmark_name for r in client.runs.list()])
    print("exports:", [e.name for e in client.exports.list()])


if __name__ == "__main__":
    main()
