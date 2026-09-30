# Li IPTV Aggregator

个人自用 IPTV 源聚合与自动维护项目。

## V1 目标

长期提供一个固定的 M3U 订阅 URL：

```
/live.m3u
```

后台自动完成：
- 拉取多个公开/授权 IPTV 来源
- 频道归一化与去重
- 保存同频道的多条 stream
- 多探针测活
- 基于历史稳定性选择最优线路
- 生成并原子发布 M3U
- 失败时保留 last-known-good

播放器直接访问实际 stream，本项目不做视频中转或转码。

## V1 原则

- 个人自用
- 最小可用
- SQLite
- CLI / 定时任务优先
- 不做 Dashboard
- 不做用户系统
- 不做转码/DVR/VOD
- 能复用成熟组件就不重造

## 快速上手

要求 Python 3.11+（仅用标准库）。

```bash
# 初始化数据库
python -m liptv init-db

# 导入本地 M3U
python -m liptv import-m3u examples/source_a.m3u --source "src-a"

# 建立归一化频道与绑定
python -m liptv canonical-add --name "CCTV-1 综合" --category 新闻
python -m liptv binding-add --source-channel-id 1 --canonical-id 1
python -m liptv stream-sync

# 选线并生成订阅
python -m liptv select --all
python -m liptv generate-m3u
```

运行测试：`python -m pytest -q`

详细设计与命令说明见：

- [数据模型 V1](DATA_MODEL_V1.md)
- [运行时流程](V1_RUNTIME_FLOW.md)
- [TASK-001 执行报告](REPORTS/TASK-001-REPORT.md)

## 当前状态

工程骨架与数据基础已按 TASK-001 实现完成：

- [TASK-001](TASKS/TASK-001.md)：V1 Skeleton / Data Foundation —— **ACCEPTED**（见 [第二轮独立验收](REVIEWS/TASK-001-REVIEW-02.md)）
- [TASK-002](TASKS/TASK-002.md)：远程 M3U 抓取及动态体育赛事源临时获取 —— **READY_FOR_EXECUTOR**；[报告模板](REPORTS/TASK-002-REPORT.md)

动态体育赛事源已登记：[JSNZKPG 体育赛事 M3U](SOURCES/JSNZKPG-SPORTS.md)。目前可由播放器独立订阅，尚未并入统一 /live.m3u。

TASK-002 仅启动远程抓取与来源生命周期；完整采集生态、EPG、多探针网络通信、定时任务、部署尚未实现。
