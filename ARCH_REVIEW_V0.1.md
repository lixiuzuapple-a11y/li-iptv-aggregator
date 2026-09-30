# Architecture Review v0.1

日期：2026-09-30
Reviewer：大G

## 结论

方向正确，但 DATA_MODEL_V0.1 仍有少量“为了未来而提前建”的成分。按个人自用 + 最小可用原则，V1 应继续缩减。

## 接受的核心实体

必须保留：

1. source
2. source_channel
3. canonical_channel
4. channel_binding
5. stream
6. stream_source
7. probe
8. probe_result

这 8 个对象已经足够表达：

- 多来源
- 同频道不同命名
- 一频道多线路
- 一线路多来源
- 多探针
- 历史测活

## 建议从 V1 schema 移除

### stream_score

不作为表。

V1 直接从 probe_result 查询/聚合：
- 最近成功率
- 连续失败
- 最近成功
- 中位启动时间
- 分辨率

理由：
- 当前数据量小；
- 避免“原始事实 + 派生事实”双写同步问题；
- 真出现性能瓶颈再物化。

### publication

V1 不需要完整 publication 历史表。

先用文件级机制：
- live.m3u
- live.previous.m3u
- 临时 live.next.m3u

生成和校验成功后原子替换。

日志记录发布时间、频道数、checksum 即可。

若未来确实需要审计/历史回滚，再增加 publication 表。

## 需要修正的实体关系

### stream 与 canonical_channel

V1 允许 stream.canonical_channel_id。

但要明确：
- URL 不是主键；
- URL hash 只作为去重辅助唯一键；
- 若未来发现“同一个 URL 在不同上下文代表不同频道”，可改为 stream endpoint + channel relation 分离。

目前个人 IPTV 使用场景无需提前拆开。

### source_channel.raw_stream_url

保留。

理由：
- source_channel 是上游原始观察；
- stream 是归一化后的播放实体；
- 两者字段看似重复，但语义不同。

## V1 最小 schema 最终建议

```
source
source_channel
canonical_channel
channel_binding
stream
stream_source
probe
probe_result
```

8 张业务表足够。

## V1 不做 migration framework 过度设计

TASK-001 可以有 schema 初始化与最小版本号，但不必引入重型 migration 框架。

例如：
- schema_version 表
- SQL 初始化文件

后续出现第二版 schema 时再决定 Alembic 等工具。

## 技术栈建议

V1：
- Python 3.12+
- stdlib sqlite3
- requests/httpx（二选一）
- ffprobe 外部命令
- pytest
- argparse 或 Typer（二选一）

优先 stdlib，避免 ORM。

原因：
- 表少；
- SQL 简单；
- 个人项目；
- 调试透明；
- 小W更容易实现，大G更容易 Review。

## Gate 判断

架构已足够清晰，可以进入正式 repo 阶段。

但正式 TASK-001 应按本 Review 缩减：
- 不建 stream_score 表；
- 不建 publication 表；
- 不引入 ORM；
- 不做 migration framework；
- 不做网络采集、测活、部署。

TASK-001 只验证“数据骨架能正确表达问题”。

