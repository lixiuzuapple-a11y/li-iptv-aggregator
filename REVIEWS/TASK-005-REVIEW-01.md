# TASK-005 Independent QA — Review 01

日期：2026-10-02
Reviewer：大G
受审 HEAD：`0128e5012a1e574c0495b891200bb658cddf6753`
实现提交：`7000834d0f4526fe37c54a89c0308f07f0860392`
结论：**REJECT → 两处定向返工 → REVIEW**

## 已确认的成果

- 已实现真实 fixed stream ffprobe 探测、ProbeObservation、错误分类、并发 worker、主线程统一 SQLite 写入、probe-check / probe-run、scheduler 集成、离线 fake ffprobe 与 demo。
- selector / schema / publish / HTTP 路由均未被本轮修改；总体方向与 TASK-005 一致。
- 小W报告提交前全量为 300 passed；大G本轮若干较大 pytest 组合在 Windows Runner 上出现长任务超时，因此本报告不把这些 Runner 超时当代码失败，也不声称取得单命令 300 项 exit 0。
- 本轮拒绝依据来自两个可独立、确定性复现的语义错误，不依赖测试超时。

## 阻断 QA-005A：混合环境故障会被写成单条 stream 健康失败，污染 selector 历史

TASK-005 明确把 `FFPROBE_NOT_FOUND` / `FFPROBE_START_FAILED` 定义为**环境级故障**：它们说明本机 ffprobe 环境有问题，不代表某条 stream 不可播，因此不能写进 probe_result 作为流健康失败。

当前 `run_round()` 只有在“本轮 attempted 全部都是 environment_error”时才整轮 `stage=failed + written=0`。只要同一轮里有一条正常成功、另一条 `FFPROBE_START_FAILED`，环境错误那条就会落入普通 attempted 写库循环，被写成 `success=0 / error_type=FFPROBE_START_FAILED`。

大G独立构造两条固定 stream：第一条 success，第二条 `FFPROBE_START_FAILED`，能力检查预先通过。实际结果：

```text
SUMMARY degraded False 2 1 1 {'FFPROBE_START_FAILED': 1}
ROWS [(2, 0, 'FFPROBE_START_FAILED'), (1, 1, None)]
```

也就是说，本机 ffprobe 在处理中途失效，被持久化成“stream 2 失败”。selector 后续会把这条环境事故当作真实频道健康历史。

### 必须修复

1. **任何** `ENVIRONMENT_ERROR_TYPES` observation 都不得写入 probe_result。
2. 更保守、推荐的冻结语义：本轮一旦出现任一环境级错误，整轮 probe 视为 `stage=failed`，本轮 **0 条 probe_result**，避免只更新部分 stream 造成选线偏差；仍可用旧历史继续 publish，但 runtime outcome / exit 必须明确 degraded/1。
3. 如果小W选择“保留本轮已完成的非环境 observations、只丢环境错误条目”，必须给出不会产生部分样本偏差的充分理由，并把 stage/runtime 状态冻结清楚；未经明确理由默认采用第 2 条 fail-closed。
4. 增加永久回归：一条 success + 一条 FFPROBE_START_FAILED；断言数据库 0 新行（按推荐语义）、probe.last_seen_at 不推进、publish 只能用旧历史、runtime 明确 probe stage failed。
5. 同样覆盖 FFPROBE_NOT_FOUND / START_FAILED 在 capability 通过后中途出现的混合场景。

## 阻断 QA-005B：heartbeat 存在 TOCTOU 竞态，可把别人的锁 token 覆盖回自己的 token

小W在执行报告 §10.2 已主动指出 TASK-004 遗留抖动，并给出原因：`heartbeat()` 先读取并确认 token，再调用 `_write()`；在“检查完成 → 写回之前”如果另一实例接管锁，旧 owner 仍会把自己的 LockInfo 覆盖回去。

这不是单纯测试抖动，而是单实例安全语义失效。TASK-005 已把 probe 加入 scheduler，长轮次/并发子进程使 scheduler 对可靠锁更依赖，因此不能继续登记成“已知抖动”而忽略。

大G做确定性竞态注入：在 heartbeat 完成 token 检查、即将 `_write` 时把锁文件替换成 `intruder-token`，随后让原 heartbeat 继续。

实际输出：

```text
HEARTBEAT_RETURN True
FINAL_TOKEN <原 owner token>
INTRUDER_SURVIVED False
LOCK_THINKS_LOST False
```

即：别人的锁被旧 owner 静默覆盖；旧 scheduler 认为自己仍持锁，不会触发 lock_lost。

### 必须修复

1. heartbeat 的“确认 ownership + 更新 heartbeat”必须具备 **CAS 等价语义**，不能是裸 read-check-write。
2. 修复后，如果 token 在更新窗口中被替换：旧 owner 必须返回 False / 标记 ownership_lost，不得覆盖新 token。
3. 不要求引入复杂分布式锁。可使用当前 lock 文件模型下的最小原子协议，例如：
   - 在同一原子替换前后做可验证 token/version 校验，并在竞态失败时放弃；或
   - 使用独占更新/辅助 claim 文件使 heartbeat 更新与 takeover 互斥；
   - 方案必须同时考虑 Windows/Linux。
4. 新增**确定性**永久回归，不要只靠概率重复测试：精确卡在“检查后、写前”插入 intruder token，修复后 intruder 必须保留，heartbeat 必须报告失锁。
5. 原 `test_cli_run_loop_stops_when_lock_is_stolen_and_keeps_foreign_lock` 应稳定重复通过；至少本机连续多次无抖动。

## 返工范围

- QA-005A 只改 probe 环境故障落库语义及对应 runtime 摘要。
- QA-005B 只修 SingleInstanceLock heartbeat/takeover 的 ownership 原子性，不重构 HTTP、publish、selector、schema。
- selector 算法继续冻结；schema 仍为 V1；动态赛事源仍不得进入 probe。
- 原有 300 项测试必须零回归，并新增上述两类确定性回归。
- 更新 `REPORTS/TASK-005-REPORT.md`，TASK-005 状态改回 REVIEW，commit + push main 后停止。
- 禁止启动 TASK-006。
