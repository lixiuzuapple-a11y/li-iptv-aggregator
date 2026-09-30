# Li IPTV Aggregator — Data Model v0.1

日期：2026-09-30

目标：满足“个人自用 + 固定 M3U URL + 后台自动刷新”的最小可用需求。

原则：
- 只设计 V1 真正需要的对象；
- 所有实体使用稳定内部 ID；
- 名称、URL、外部 id 都不是主键；
- 保留来源关系和测活历史；
- 不引入平台化、多用户、权限、转码等无关模型。

## 1. source

表示一个上游来源。

字段建议：
- id
- name
- kind（m3u / txt / api / manual）
- url
- enabled
- fetch_interval_minutes
- last_fetch_at
- last_fetch_status
- created_at
- updated_at

说明：
- source 只描述“从哪里来”；
- 不直接代表频道或线路；
- 同一条 stream 可以来自多个 source。

## 2. source_channel

表示某个来源里出现的“原始频道条目”。

字段建议：
- id
- source_id
- external_id（若有 tvg-id 等）
- raw_name
- raw_group
- raw_logo
- raw_epg_id
- raw_stream_url
- first_seen_at
- last_seen_at
- active

说明：
- 保存上游原貌；
- 不直接用于最终输出；
- 同一个 canonical channel 可以关联多个 source_channel。

## 3. canonical_channel

表示系统内部认定的真实频道。

字段建议：
- id
- name
- category
- preferred_tvg_id
- preferred_logo
- enabled
- priority
- created_at
- updated_at

说明：
- 这是最终 M3U 的频道层；
- display name 可改，但 id 不变；
- source_channel 通过 binding 归并到这里。

## 4. channel_binding

表示 source_channel → canonical_channel 的归一化关系。

字段建议：
- id
- source_channel_id
- canonical_channel_id
- method（exact_id / alias / fuzzy / manual）
- confidence
- created_at

说明：
- 自动归一化必须保留依据；
- 模糊匹配低置信度时允许暂不绑定；
- 手工修正不会覆盖原始 source_channel。

## 5. stream

表示一个实际可播放线路。

字段建议：
- id
- canonical_channel_id
- url
- url_hash
- first_seen_at
- last_seen_at
- enabled
- status
- notes

说明：
- 同 URL 不重复建多个 stream；
- 但不同 source 对同 stream 的“来源关系”要保留；
- URL 变化应视情况生成新 stream，而不是覆盖历史。

## 6. stream_source

表示 stream 来自哪些 source/source_channel。

字段建议：
- id
- stream_id
- source_channel_id
- first_seen_at
- last_seen_at

说明：
- provenance 独立保存；
- 一个 stream 可被多个来源同时发现。

## 7. probe

表示一个测活节点。

字段建议：
- id
- name
- location
- network_type
- enabled
- capabilities
- last_seen_at

V1 初始：
- shanghai-cloud
- windows-local

未来：
- singapore
- hongkong

## 8. probe_result

表示一次具体测活结果。

字段建议：
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
- sample_duration_ms

说明：
- 这是原始事实；
- 不直接决定最终 M3U；
- retention 需要后续汇总，避免无限增长。

## 9. stream_score

表示某条 stream 在某个时点的聚合评价。

字段建议：
- stream_id
- calculated_at
- success_rate_24h
- success_rate_7d
- consecutive_failures
- last_success_at
- median_startup_ms_7d
- resolution_score
- stability_score
- final_score

说明：
- V1 可以是物化表，也可以运行时计算；
- 先简单，不做机器学习。

## 10. publication

表示一次正式 M3U 发布。

字段建议：
- id
- generated_at
- channel_count
- file_path
- checksum
- status
- reason
- is_current

说明：
- 支持 last-known-good；
- 只有通过校验的新版本才成为 current；
- 失败发布不覆盖旧文件。

# 关系总览

```
source
  ↓
source_channel
  ↓ channel_binding
canonical_channel
  ↓
stream
  ↓
stream_source

stream × probe
  ↓
probe_result
  ↓
stream_score
  ↓
publication
  ↓
live.m3u
```

# V1 明确不建的表

- users
- roles
- permissions
- subscriptions
- tenants
- billing
- dvr
- vod
- transcoding_jobs
- playback_sessions
- dashboards

