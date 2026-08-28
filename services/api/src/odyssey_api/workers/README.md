# workers/ — deliberately empty

`docs/STRUCTURE.md` names `workers/drain_consumer.py` (`kafka -> spool drain`)
in this directory. It is **not built** — same explicit-deferral treatment
`judges.py` (item 7's LLM-as-judge) and the OTel-bridge/LLM-augmentation
items got before either had a concrete consumer: no Kafka topic, broker, or
producer exists anywhere in this repo today, and standing one up here would
be a heavy, speculative dependency with nothing real to drain from.

The actual ingest path today is `services/collector`'s stdlib HTTP receiver
(`odyssey.HttpSink` -> collector -> disk), which `services/api` reads from
read-only (`repositories/filesystem.py`). If a Kafka-backed ingest path is
ever built, it belongs here, draining into the same on-disk shape the
collector already writes so `services/api`'s read side needs no changes.
