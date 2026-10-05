# TASK-008 Independent QA — Review 01

日期：2026-10-05
Reviewer：大G
受审 HEAD：`67f43fb79fae07d750b8466a8a3de0466fd65301`
实现提交：`2a2d0c5` → `840c243`
结论：**REJECT → 核心业务已通过，仅剩 per-source published accounting 一处定向返工 → REVIEW**

## 已确认通过的核心成果

- 新增 `failure_policy=all_or_nothing|isolate`，默认 `all_or_nothing`，旧 TASK-003 语义保留。
- `isolate` 真正按源隔离：单一动态源失败，不再拖掉其它成功源。
- 真机 smoke B 暴露的 `fixed=0` 判据错误已在 `840c243` 修正；大G认可修正语义：
  - isolate 看的是“本轮最终有没有任何可发布内容”；
  - 成功动态源本身就是可发布内容，不要求必须有 fixed。
- `DEGRADED_DYNAMIC_PARTIAL` 映射 exit 0 合理：确实完成了发布，但状态不伪装成 OK。
- `require_dynamic` 在 isolate 下以“至少 1 条本轮新鲜动态条目进入 playlist”为准，fixed 不能代替。
- 双源全失败 + fixed=0 不覆盖 last-known-good；组合层不读取旧 live.m3u 回填动态 URL。
- 跨源只做精确 `(URL,name,group)` 去重；同名异 URL 不合并，仅对真正跨源显示冲突追加稳定来源标签；无 fuzzy matching。
- JSNZKPG 与 KORICE 真实结构均已 smoke；KORICE 不被硬编码成 NFL 格式。
- 生产配置已使用两个动态源、`failure_policy=isolate`、15 分钟刷新；8080 仍只绑定 localhost。
- EV-Lab 零伤害证据成立。
- 小W全量：**515 passed / 0 failed / 0 skipped**。
- `git diff --check` 通过，冻结 schema/probe/health/doctor/deploy 等模块未改。

## 唯一阻断 QA-008A：per-source 摘要缺 `published`，跨源精确去重后统计会失真

TASK-008 §6 冻结的每源动态摘要至少要求：

```json
{
  "name": "...",
  "fetched": 0,
  "included": 0,
  "published": 0,
  "discarded": 0
}
```

当前 `_dynamic_summary()` 实际只有：

- `fetched`
- `included`
- `discarded`

没有 `published`。

这在没有跨源去重时看似不影响使用，但 TASK-008 新增了**跨源精确去重**，于是 `included` 已经不等于最终实际贡献到 playlist 的条目数。

确定性例子：

- KORICE：4 条过滤后 included；
- `dyn-korice-twin`：同样 4 条 included；
- 两源 `(URL,name,group)` 完全相同；
- 跨源精确去重后最终 `dynamic_count=4`；
- 第一源实际 published=4；
- 第二源实际 published=0。

当前摘要会呈现两个来源都 `included=4`，但没有任何 per-source 字段告诉运维第二源最终贡献其实是 0。
这与任务书“逐源 fetch/filter/publish 计数”及 §6 的 `published` 要求不一致。

## 必须修复

只修 per-source accounting，不改已通过的聚合/真机逻辑。

推荐做法：

1. 在跨源处理完成、`dynamic_included` 最终确定后，按 `source_name` 统计最终条目数。
2. 对每个 `dynamic_report` 写入：
   - `published` = 该来源最终仍存在于 `dynamic_included` 的条目数；
   - 失败源 = 0；
   - all_or_nothing fail-closed 时所有动态源 = 0。
3. `_dynamic_summary().sources[]` 必须输出 `published`。
4. 统计必须满足可审计关系：
   - `sum(source.published) == dynamic_summary.published_entries == composition.dynamic_count`。
5. 对跨源 exact duplicate 增加永久回归：
   - kept source `included=4, published=4`；
   - duplicate source `included=4, published=0`；
   - 全局 `published_entries=4`。
6. 对 display collision（只改名、不删除）增加回归：每源 `published == included`。
7. isolate 单源失败：失败源 `published=0`，成功源 published 与实际 playlist 一致。
8. all_or_nothing 任一源失败：所有动态源 `published=0`。

`discarded` 是否继续只表示 fail-closed/policy 舍弃，还是把 cross-source duplicate 也算入，可由小W选择；但必须在字段定义里明确。不要为了凑等式偷偷改掉既有 `included` 语义。

## 不需要重做

- 不需要重新设计 failure_policy。
- 不需要再改 smoke B/C/D 业务逻辑。
- 不需要再跑大规模真机故障注入；修复是纯统计层。
- 不改 selector / schema / probe / health / deploy / restore。
- 生产配置无需变化。

完成后：

- 跑 `tests/test_task008.py`；
- 跑 publish/runtime 相关旧回归；
- 跑全量测试；
- 更新 `REPORTS/TASK-008-REPORT.md`；
- TASK-008 回 REVIEW；
- commit + push main 后停止；
- 禁止启动 TASK-009。
