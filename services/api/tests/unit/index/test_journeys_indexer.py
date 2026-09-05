from __future__ import annotations

from odyssey.jsonl import write_events
from odyssey.primitives import JourneyEvent, JourneyHeader, Message, Terminal
from odyssey_api.index.journeys_indexer import index_journeys

from odyssey_store.db import connect

JID = "j_idx"


def _write_journey(journeys_dir, jid, date, project=None, complete=True):
    date_dir = journeys_dir / date
    date_dir.mkdir(parents=True, exist_ok=True)
    header = JourneyHeader(
        journey_id=jid,
        data_source="livekit",
        journey_metadata={"project": project} if project else None,
    )
    events = [
        JourneyEvent(
            journey_id=jid,
            seq=0,
            kind="message",
            event_id="e0",
            message=Message(role="user", content="hi"),
        )
    ]
    if complete:
        events.append(
            JourneyEvent(
                journey_id=jid,
                seq=1,
                kind="terminal",
                event_id="e1",
                terminal=Terminal(termination_reason="ENV_DONE"),
            )
        )
    write_events(date_dir / f"{jid}.jsonl", events, header=header)


def test_index_journeys_inserts_row(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, JID, "2026-08-28", project="odyssey")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_journeys(conn, journeys_dir)

    assert count == 1
    row = conn.execute("SELECT * FROM journeys WHERE journey_id = ?", (JID,)).fetchone()
    assert row["date"] == "2026-08-28"
    assert row["complete"] == 1
    assert row["project"] == "odyssey"
    assert row["product_slug"] is None


def test_index_journeys_skips_unchanged_file_on_second_pass(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir, JID, "2026-08-28")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    first = index_journeys(conn, journeys_dir)
    second = index_journeys(conn, journeys_dir)

    assert first == 1
    assert second == 0  # nothing changed, nothing reprocessed


def test_index_journeys_tags_product_slug_in_scoped_layout(tmp_path):
    journeys_dir = tmp_path / "journeys"
    _write_journey(journeys_dir / "unpod", JID, "2026-08-28")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    index_journeys(conn, journeys_dir)

    row = conn.execute(
        "SELECT product_slug FROM journeys WHERE journey_id = ?", (JID,)
    ).fetchone()
    assert row["product_slug"] == "unpod"


def test_index_journeys_skips_malformed_shard(tmp_path, caplog):
    journeys_dir = tmp_path / "journeys"
    date_dir = journeys_dir / "2026-08-28"
    date_dir.mkdir(parents=True)
    (date_dir / "broken.jsonl").write_text("not valid jsonl\n")
    conn = connect(f"sqlite:///{tmp_path}/db.sqlite3")

    count = index_journeys(conn, journeys_dir)

    assert count == 0
    assert conn.execute("SELECT COUNT(*) FROM journeys").fetchone()[0] == 0
