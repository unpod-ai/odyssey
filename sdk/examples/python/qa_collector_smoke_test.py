"""odyssey-collector QA smoke test — posts one dummy journey to a real,
already-running collector (your QA box), so you can watch it land on disk
and, with ODYSSEY_COLLECTOR_DEBUG/--debug set on that box, watch the
request show up in `journalctl -u odyssey-collector -f`.

Not part of the pytest suite on purpose — this talks to whatever endpoint
you point it at (a live deployment), not a server this process starts
itself. `sdk/examples/python/basic_usage.py` is the equivalent for the
read side (`services/api`); this is the write side (`services/collector`).

Usage (endpoint/api_key resolve the same way `odyssey.HttpSink` always
does — explicit argument wins, then the env var):

    ODYSSEY_ENDPOINT=https://qa.example.com \\
    ODYSSEY_API_KEY=<your product's api_key> \\
    uv run python sdk/examples/python/qa_collector_smoke_test.py

    # or pass them positionally instead of exporting:
    uv run python sdk/examples/python/qa_collector_smoke_test.py \\
        https://qa.example.com <api_key>

A fresh, timestamped journey_id is generated each run, so re-running this
never collides with (or gets deduped against) a previous run's journey.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey.sinks import HttpSink, HttpSinkError


def main() -> int:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else None
    api_key = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        sink = HttpSink(endpoint, api_key=api_key)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "pass an endpoint (arg or ODYSSEY_ENDPOINT) — see this file's "
            "module docstring for usage",
            file=sys.stderr,
        )
        return 1

    print(f"endpoint: {sink.endpoint}")
    print(f"api_key:  {'set' if sink.api_key else '(none — open mode)'}")

    try:
        health = urllib.request.urlopen(f"{sink.endpoint}/health", timeout=5)
        print("GET /health ->", health.status, health.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"GET /health failed: {exc}", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    journey_id = f"qa-smoke-{stamp}"
    header = JourneyHeader(
        journey_id=journey_id,
        data_source="livekit",
        trace_id=f"trace-{stamp}",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    events = [
        JourneyEvent(
            journey_id=journey_id,
            seq=0,
            kind="message",
            event_id="e0",
            message=Message(role="user", content="qa smoke test — hello"),
        ),
        JourneyEvent(
            journey_id=journey_id,
            seq=1,
            kind="message",
            event_id="e1",
            message=Message(role="assistant", content="qa smoke test — hi back"),
        ),
        JourneyEvent(
            journey_id=journey_id,
            seq=2,
            kind="terminal",
            event_id="e2",
            terminal=Terminal(termination_reason="ENV_DONE"),
        ),
    ]

    print(f"POST /journeys/{journey_id}/events ({len(events)} events)...")
    try:
        sink.send(journey_id, events, header=header)
    except HttpSinkError as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1
    finally:
        sink.close()

    print("OK — journey accepted. Check the collector's data dir for")
    print(f"  <date>/{journey_id}.jsonl")
    print("and, if ODYSSEY_COLLECTOR_DEBUG=1 is set on that box, a matching")
    print(f'  "POST /journeys/{journey_id}/events HTTP/1.1" 200')
    print("line in `journalctl -u odyssey-collector -f`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
