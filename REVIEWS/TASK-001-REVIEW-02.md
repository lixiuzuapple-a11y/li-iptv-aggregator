# TASK-001 Independent QA — Review 02

Date: 2026-09-30
Reviewer: 大G
Reviewed commit: 1e581eab0af3c14077e664e8f156caaf4a84a193
Decision: **ACCEPT**

## Independent evidence

- WebCodex Windows Python 3.13.15, pytest 9.1.1; disabled external auto-plugins and set fixed basetemp: **70 passed, 14.50 s, exit 0**.
- Reviewed schema/schema_v1.sql, liptv/repo.py, liptv/select.py and tests/test_identity.py. Source entry identity now (source_id, hash(name + group + URL)), one binding per source_channel, stream deduplication scoped to canonical channel.
- Independent temporary reproduction (not relying on executor tests): two different sources, distinct canonical channels, identical URL -> **two independent streams**, each stream_source links exclusively to its own source_channel; **PASS, exit 0**.
- Initial independent reproduction printed PASS but temporary SQLite cleanup failed due to open Windows file handle; reran closing connection, exit 0.
- Rebind invalid-provenance cleanup and stale exclusion covered by code and new regression tests.

## Gate

**ACCEPT** for TASK-001 only. This is a tested skeleton, not proof of live source fetching, production publication, authorization of third-party content, or live network probes. Before using an earlier V1 schema database, rebuild or plan an explicit migration; do not assume init-db upgrades it.
