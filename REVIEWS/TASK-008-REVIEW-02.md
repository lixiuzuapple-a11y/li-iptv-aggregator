# TASK-008 Independent QA — Review 02

日期：2026-10-05
Reviewer：大G
受审 HEAD：`4e97935`
前置 Review：`REVIEWS/TASK-008-REVIEW-01.md`
结论：**ACCEPTED**

## 1. Review 01 唯一阻断 QA-008A

Review 01 的唯一阻断为：per-source 动态摘要缺少 `published`，导致跨源精确去重后无法回答“哪个来源实际贡献了几条”。

本轮返工已补齐：

- 每个动态 report 增加 `published`；
- 每个动态 summary source 增加 `published`；
- 增加 `cross_source_duplicate`；
- 增加顶层 `published_counted`；
- 最终记账在跨源去重 / fail-closed 之后执行；
- 条目通过私有 `_source_slot` 绑定到来源序号，避免同名 source 被指定两次时重复归属。

## 2. 独立代码审查

独立检查确认：

1. `_source_slot` 在动态条目进入 `dynamic_included` 时写入；
2. `attribute_published_per_source()` 在 isolate/all_or_nothing 处理及跨源整理之后、构造最终 Composition 之前执行；
3. 因此 `published` 基于最终实际进入 playlist 的 dynamic entries；
4. `_dynamic_summary().sources[]` 输出 fetched / included / published / discarded / cross_source_duplicate；
5. runtime 继续透传脱敏后的 `dynamic_summary`；
6. CLI 文本输出补充 `published=`，避免把 included 误解成最终发布数。

## 3. 关键语义确认

### 3.1 exact cross-source duplicate

- kept source：`included=4, published=4`
- duplicate source：`included=4, published=0`
- 全局：`published_entries=4`

符合 Review 01 要求。

### 3.2 display collision

只追加来源标签、不删除条目，因此 `published == included`。

### 3.3 isolate 单源失败

失败源 `published=0`；成功源 `published` 与实际 playlist 条目数一致。

### 3.4 all_or_nothing fail-closed

任一动态源失败时，动态内容整体不发布，因此所有动态源 `published=0`；旧 TASK-003 语义保持。

## 4. 独立测试

大G本地独立执行：

- TASK-008 定向 accounting / dedup / collision / AON：**19 passed**
- 旧 publish/runtime 回归：**94 passed**
- 完整 `tests/test_task008.py`：**61 passed**
- `git diff --check 82c8a06..HEAD`：**通过**

小W报告全量：**522 passed / 0 failed / 0 skipped**。

大G未重复运行整套 522 项，但已独立覆盖本次返工、TASK-008 全文件与 publish/runtime 关键回归，未发现新阻断。

## 5. 业务结论

TASK-008 的核心目标现已成立：

- JSNZKPG + KORICE 两个 dynamic_event_m3u 可同时进入生产聚合；
- production 使用 `failure_policy=isolate`；
- 单一动态源失败时，另一成功源的本轮赛事继续发布；
- 失败源不会复用旧动态 URL；
- 双源全失败且 fixed=0 时保留 last-known-good；
- 同名异 URL 不 fuzzy merge；
- exact duplicate 可跨源精确去重；
- 每源 published accounting 现已可审计；
- 生产 15 分钟刷新、localhost-only、EV-Lab 隔离均保持。

## 6. Final

**TASK-008 ACCEPTED。**

后续不得把 TASK-008 的动态源当作 fixed，也不得因为已验收而放宽：

- 不代理视频；
- 不持久化旧签名 URL；
- 不绕过 DRM / 登录 / Cookie / Authorization；
- 不做 fuzzy event merge；
- 不开放公网 8080。

TASK-009 是否启动，等待 Owner 新指令。
