-- Li IPTV Aggregator — V1 schema (version 1)
-- 严格对应 DATA_MODEL_V1.md 的 8 张业务表。
-- 原则：
--   * 所有实体用稳定内部整型 ID 作主键（AUTOINCREMENT，不复用）；
--   * 频道名 / tvg-id / URL 一律不作主键；
--   * url_hash 仅作去重辅助唯一键（ARCH_REVIEW_V0.1 允许）；
--   * 不建 stream_score / publication 表。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 元数据
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------- 1. source
CREATE TABLE IF NOT EXISTS source (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    kind              TEXT    NOT NULL,
    url               TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_fetch_at     TEXT,
    last_fetch_status TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

-- ---------------------------------------------------- 2. source_channel
CREATE TABLE IF NOT EXISTS source_channel (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    external_id     TEXT,
    raw_name        TEXT    NOT NULL,
    raw_group       TEXT,
    raw_logo        TEXT,
    raw_epg_id      TEXT,
    raw_stream_url  TEXT    NOT NULL,
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_source_channel_source_url
    ON source_channel (source_id, raw_stream_url);
CREATE INDEX IF NOT EXISTS ix_source_channel_source
    ON source_channel (source_id);

-- ---------------------------------------------- 3. canonical_channel
CREATE TABLE IF NOT EXISTS canonical_channel (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,
    category         TEXT,
    preferred_tvg_id TEXT,
    preferred_logo   TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    priority         INTEGER NOT NULL DEFAULT 100,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_canonical_channel_category
    ON canonical_channel (category);

-- -------------------------------------------------- 4. channel_binding
CREATE TABLE IF NOT EXISTS channel_binding (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_channel_id   INTEGER NOT NULL REFERENCES source_channel(id) ON DELETE CASCADE,
    canonical_channel_id INTEGER NOT NULL REFERENCES canonical_channel(id) ON DELETE CASCADE,
    method              TEXT    NOT NULL,
    confidence          REAL    NOT NULL DEFAULT 1.0,
    created_at          TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_binding_pair
    ON channel_binding (source_channel_id, canonical_channel_id);
CREATE INDEX IF NOT EXISTS ix_channel_binding_canonical
    ON channel_binding (canonical_channel_id);

-- ---------------------------------------------------------------- 5. stream
CREATE TABLE IF NOT EXISTS stream (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_channel_id INTEGER NOT NULL REFERENCES canonical_channel(id) ON DELETE CASCADE,
    url                  TEXT    NOT NULL,
    url_hash             TEXT    NOT NULL UNIQUE,
    first_seen_at        TEXT    NOT NULL,
    last_seen_at         TEXT    NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    status               TEXT    NOT NULL DEFAULT 'observed'
);

CREATE INDEX IF NOT EXISTS ix_stream_canonical
    ON stream (canonical_channel_id);

-- --------------------------------------------------------- 6. stream_source
CREATE TABLE IF NOT EXISTS stream_source (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id         INTEGER NOT NULL REFERENCES stream(id) ON DELETE CASCADE,
    source_channel_id INTEGER NOT NULL REFERENCES source_channel(id) ON DELETE CASCADE,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_stream_source_pair
    ON stream_source (stream_id, source_channel_id);
CREATE INDEX IF NOT EXISTS ix_stream_source_stream
    ON stream_source (stream_id);

-- ----------------------------------------------------------------- 7. probe
CREATE TABLE IF NOT EXISTS probe (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    location     TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT
);

-- --------------------------------------------------------- 8. probe_result
CREATE TABLE IF NOT EXISTS probe_result (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id          INTEGER NOT NULL REFERENCES stream(id) ON DELETE CASCADE,
    probe_id           INTEGER NOT NULL REFERENCES probe(id) ON DELETE CASCADE,
    checked_at         TEXT    NOT NULL,
    success            INTEGER NOT NULL,
    error_type         TEXT,
    http_status        INTEGER,
    connect_ms         INTEGER,
    startup_ms         INTEGER,
    resolution_width   INTEGER,
    resolution_height  INTEGER,
    bitrate_kbps       INTEGER,
    protocol           TEXT,
    ipv_family         TEXT
);

-- 同一 stream × probe 在多个时刻保留多条记录（历史），故不设唯一约束。
CREATE INDEX IF NOT EXISTS ix_probe_result_stream_time
    ON probe_result (stream_id, checked_at);
CREATE INDEX IF NOT EXISTS ix_probe_result_probe_time
    ON probe_result (probe_id, checked_at);

-- V1 初始逻辑探针节点（DATA_MODEL_V1.md 指定）
INSERT OR IGNORE INTO probe (name, location, enabled, last_seen_at)
    VALUES ('shanghai-cloud', 'Shanghai', 1, NULL);
INSERT OR IGNORE INTO probe (name, location, enabled, last_seen_at)
    VALUES ('windows-local', 'Windows', 1, NULL);
