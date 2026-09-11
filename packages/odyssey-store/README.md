# odyssey-store

Shared SQLite schema and connection helper for the one `ODYSSEY_DB_URI`
file `services/api` and `services/collector` both use — `services/api`'s
read index (`journeys`/`metrics_snapshots`/`exports`/`indexed_files`,
disposable/rebuildable) and `services/collector`'s `products` table
(real, unrecoverable tenant credentials, hash-only). Each table has
exactly one writer; see
`docs/superpowers/specs/2026-09-05-api-sqlite-index-design.md`.

This package owns only the DDL and the connection helper — no
business logic, no queries beyond schema application.
