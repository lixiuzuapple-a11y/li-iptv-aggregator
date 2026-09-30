# TASK-001 — V1 Skeleton / Data Foundation

状态：REJECTED（Review 01；定向返工，见 REVIEWS/TASK-001-REVIEW-01.md）

Owner：老李  
Architect / Reviewer：大G  
Executor：小W

执行规则：
- 小W从 GitHub `main` 拉取最新代码后开始；
- 实现过程中允许新增最小开发依赖（如 pytest），但不得引入 ORM / PostgreSQL / Web UI；
- 完成后必须运行测试并写 `REPORTS/TASK-001-REPORT.md`；
- 完成后状态只能改为 `REVIEW`，不得自行改成 ACCEPTED；
- 提交并 push 到 `main` 后停止，等待大G独立 Review。

## 目标

建立 Li IPTV Aggregator 的最小工程骨架和 SQLite 数据基础，不实现完整采集生态，不做 UI。

## 范围

应包含：
- Python 项目骨架
- SQLite schema / migration
- 基础配置文件
- source / source_channel / canonical_channel / channel_binding / stream / stream_source / probe / probe_result 数据模型
- 一个最小 M3U parser
- 一个最小 M3U generator
- 基础 CLI
- 自动化测试
- README
- 直接使用 SQLite（优先 stdlib sqlite3），不引入 ORM
- 简单 schema 初始化与版本号，不引入重型 migration framework

## 不包含

- Dashboard
- Docker（除非实现成本极低且不影响主任务）
- EPG 抓取
- Logo 抓取
- Guovin 集成
- 多探针网络通信
- 腾讯云部署
- 自动定时任务
- 复杂 fuzzy matching
- 生产发布
- stream_score 表
- publication 表
- ORM
- PostgreSQL

## 验收目标

1. 能导入一份本地 M3U。
2. 能把条目写入 SQLite。
3. 能手工建立两个 source_channel → 一个 canonical_channel 的绑定。
4. 能为一个 canonical_channel 保存多条 stream。
5. 能插入模拟 probe_result。
6. 能根据简单规则选出一条最佳 stream。
7. 能生成标准 live.m3u。
8. 测试覆盖核心数据关系和生成逻辑。
9. 不使用频道名或 URL 作为数据库主键。
10. 所有重要行为可通过 CLI 完成。
11. schema 必须与 DATA_MODEL_V1.md 一致。
12. 代码实现不得把 display name、URL 或 tvg-id 当数据库主键。

## 停止条件

如果实现过程中发现当前数据模型无法支持：
- 一频道多 source_channel；
- 一频道多 stream；
- 一 stream 多来源；
- stream × probe 历史；

则停止并回到架构 Review，不允许通过临时字段绕过去。

