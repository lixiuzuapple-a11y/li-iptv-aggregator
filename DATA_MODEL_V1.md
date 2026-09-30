# Li IPTV Aggregator — V1 Data Model

日期：2026-09-30

这是经过 Architecture Review 后冻结给 TASK-001 使用的最小模型。

## 核心原则

- 个人自用、最小可用；
- 所有实体使用稳定内部 ID；
- 频道名和 URL 都不能作为数据库主键；
- 原始来源事实与归一化结果分离；
- Channel 与 Stream 分离；
- 多探针结果按 stream × probe × time 保存；
- 派生评分 V1 运行时计算，不单独落表；
- M3U 发布历史 V1 用文件 + 日志解决，不单独建表。

## V1 业务表

### 1. source

上游数据源。

最小字段：
- id
- name
- kind
- url
- enabled
- last_fetch_at
- last_fetch_status
- created_at
- updated_at

### 2. source_channel

某个上游来源中观察到的原始频道条目。

最小字段：
- id
- source_id
- external_id
- raw_name
- raw_group
- raw_logo
- raw_epg_id
- raw_stream_url
- first_seen_at
- last_seen_at
- active

### 3. canonical_channel

系统内部认定的真实频道。

最小字段：
- id
- name
- category
- preferred_tvg_id
- preferred_logo
- enabled
- priority
- created_at
- updated_at

### 4. channel_binding

source_channel → canonical_channel 的映射。

最小字段：
- id
- source_channel_id
- canonical_channel_id
- method
- confidence
- created_at

### 5. stream

可播放线路。

最小字段：
- id
- canonical_channel_id
- url
- url_hash
- first_seen_at
- last_seen_at
- enabled
- status

### 6. stream_source

stream 的来源关系。

最小字段：
- id
- stream_id
- source_channel_id
- first_seen_at
- last_seen_at

### 7. probe

测活节点。

最小字段：
- id
- name
- location
- enabled
- last_seen_at

V1 初始逻辑节点：
- shanghai-cloud
- windows-local

### 8. probe_result

一次测活事实。

最小字段：
- id
- stream_id
- probe_id
- checked_at
- success
- error_type
- http_status
- connect_ms
- startup_ms
- resolution_width
- resolution_height
- bitrate_kbps
- protocol
- ipv_family

## 不作为 V1 表

- stream_score：运行时从 probe_result 聚合
- publication：live.m3u / live.previous.m3u + 日志
- users / roles / permissions
- VOD / DVR / transcode
- playback sessions

## 核心关系

```
source
  ↓
source_channel
  ↓ channel_binding
canonical_channel
  ↓
stream
  ↑
stream_source

stream × probe × time
  ↓
probe_result
```

## TASK-001 必须证明

1. 一频道可绑定多个 source_channel；
2. 一频道可拥有多个 stream；
3. 同一 stream 可保留多个来源关系；
4. probe_result 能保存同一 stream 在不同 probe、不同时刻的结果；
5. 最终可从历史结果中选择一个最优 stream 并生成 M3U。

