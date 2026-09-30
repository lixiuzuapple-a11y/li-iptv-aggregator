# TASK-001 Independent QA — Review 01

日期：2026-09-30
Reviewer：大G
审查提交：`0f2795263baa78359c3434be73fcf23a02aaa6cd`
结论：**REJECT → 定向返工 → REVIEW**

## 已通过的事实

- GitHub `main` 已包含 TASK-001 实现及 REPORT。
- 使用 WebCodex Windows Runner 上独立安装的 Python 3.13.15 和 pytest 9.1.1 测试，设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 后完整测试结果为 **60 passed in 7.66s**。
- 核对了 `schema/schema_v1.sql`、`liptv/repo.py`、`liptv/select.py`、`liptv/m3u.py`、`tests/test_repo.py` 等源码。
- 常规单频道多来源、单频道多线路、历史探针、M3U 生成逻辑有实现且现有测试通过。
- 初次直接运行 pytest 超时属本次 QA 环境噪声；已采用关闭外部插件自动加载的受控命令复跑，不当作实现缺陷。

## 阻断项 QA-001：相同 URL 跨频道造成身份错配与来源污染

### 问题一：stream URL 被全局去重，但 stream 单独归属一个 canonical channel

目前：
- `stream.url_hash` 全局 UNIQUE；
- `stream.canonical_channel_id` 单值；
- `sync_streams()` 仅按 URL hash 查找既有 stream，不核查其 canonical_channel_id；
- `stream_source` 仍将另一频道来源关联到先出现的 stream。

**独立复现：**
- source A 有频道甲，source B 有频道乙；
- 两条原始记录 URL 都为 `http://shared.example/live.m3u8`；
- 两个 source_channel 分别绑定不同 canonical channel；
- 执行 `sync_streams()`。

实测结果：
```
SYNC {'streams_created': 1, 'streams_updated': 1, 'links_created': 2, 'links_updated': 0}
CHANNEL 1 STREAMS [stream #1]
CHANNEL 2 STREAMS []
PROVENANCE stream #1 <- 频道甲（属于 canonical #1）
PROVENANCE stream #1 <- 频道乙（实际属于 canonical #2，但被记录成 canonical #1 的来源）
```

这是静默数据污染：频道乙没有可选线路，频道甲被错误记录了另一个频道的来源。

### 问题二：同一 source 内的不同频道共享 URL 被过早合并

`source_channel` 的 UNIQUE(`source_id`, `raw_stream_url`) 和 `upsert_source_channel()` 查找条件相同。即使某个来源存在两个名称不同、频道归属不同、却引用相同 URL 的独立原始条目，也只保存最后更新的一条 source_channel。它们未进入后续规范化就已丢失身份。

### 问题三：一个 source_channel 可同时绑定多个 canonical channel

`channel_binding` 目前仅 UNIQUE(`source_channel_id`, `canonical_channel_id`)，允许同一个原始条目归属两个不同 canonical channel。与当前 V1 单值 stream.canonical_channel_id 模型结合，同样会触发上面的交叉归属问题。

## 定向返工要求（只修这组身份问题）

1. 先明确 V1 一条 source_channel 只属于一个 canonical channel 的约束；重复绑定另一 canonical 时必须拒绝并给出明确错误，或提供显式、可审计的重新绑定操作。禁止静默多归属。
2. 归一化前必须保留同一 source 中 URL 相同但原始频道条目不同的独立身份。可在 V1 使用可解释的复合身份策略，不得单纯以 URL 为 source_channel 唯一身份；同时保持重复导入幂等。
3. 对相同 URL 出现在不同 canonical channel 的情况，必须避免跨频道 stream_source 错链。可采用 canonical 作用域内 URL 去重（例如 UNIQUE(canonical_channel_id,url_hash)），或者显式冲突隔离并阻止错误关联；选择前者时须保证一个 canonical 下多来源相同 URL 仍只有一个 stream。
4. 新增至少三类永久回归测试：
   - 两个不同来源、两个不同 canonical、相同 URL；
   - 同一来源、两条不同原始频道、相同 URL，重复导入仍幂等；
   - 同一 source_channel 被请求绑定两个 canonical 时按约定拒绝/显式迁移，不得静默跨绑。
5. 完整复跑原测试和新增测试；给出独立反例重跑结果、变更 diff、commit SHA。
6. 不改动无关模块，不做真实采集/测活/部署，不开始 TASK-002。

## 返工交付

- TASK-001 继续由小W执行，完成后状态改为 `REVIEW`。
- 在原 `REPORTS/TASK-001-REPORT.md` 追加 `Review 01 返工结果` 一节，逐项回应 QA-001，记录测试结果和提交 SHA。
- push 后停止，由大G再次独立验收。

## Gate

本次是数据身份完整性阻断，不是测试数量不足。不能仅增加测试、仅记录风险、或把问题延期到 TASK-002 后判定 TASK-001 ACCEPT。
