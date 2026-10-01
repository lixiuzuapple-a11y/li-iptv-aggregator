# TASK-004 Independent QA — Review 02

日期：2026-10-01
Reviewer：大G
受审 HEAD：`322c79a0c14491cc438afcb900e0e810108f3ce7`
返工实现提交：`3caf18d`（后续仅报告记录提交）
结论：**REJECT → 仅剩 QA-004C 一处 Windows 状态文件并发问题 → 定向返工 → REVIEW**

## 已确认通过

- QA-004A（心跳未接线）已关闭：心跳现在实际进入 scheduler 生命周期，包含轮次边界、长 interval 休眠期和长轮次后台线程。
- 大G独立重跑旧反例：3 轮运行后 `heartbeat_at` 已推进，`LOCK_LOST=False`，心跳计数实际增加；旧版“心跳完全不变”不再复现。
- 跨主机观察场景的永久回归已存在并通过：活跃锁保持新鲜时返回 `held_by_remote_host`，不会按 stale 接管。
- QA-004B（release 误删未知锁）已关闭：大G独立把锁文件改成损坏 JSON 后调用 release，结果 `False` 且文件继续存在。
- 心跳 token 被替换、损坏/缺失锁文件、保守 release、后台线程与轮次心跳并发等新增测试均已通过。

## 独立测试

现有测试共 **266 项**。大G按互不遗漏的分组运行，均 exit 0：

- `tests/test_server.py`：23 passed；
- `tests/test_runtime.py -k 'heartbeat or lock or release'`：32 passed（含真实信号用例，因此与下一组之外的 runtime 测试合计覆盖 54 项）；
- `tests/test_runtime.py -k 'not (heartbeat or lock or release or real_process...)'`：22 passed；
- 真实信号子进程再单独复验：1 passed（重复验证，不计入 266 总数）；
- publish / m3u / 历史 QA：79 passed；
- 其余旧 core / fetch / dynamic / ingest：110 passed。

`tests/test_runtime.py` 整文件一次合并命令在 Windows Runner 超时；拆分后全部业务断言成功，因此不将 Runner 超时视作代码失败，也不声称大G取得单命令 266 项的 exit 0。

## 阻断 QA-004C：`/healthz` 并发读取会高频阻止 runtime-status.json 原子替换

小W在执行报告 §12.11 主动披露了这个遗留问题。大G没有仅按“理论风险”接受，而是在当前 Windows 环境用真实 `SubscriptionServer` + `StatusStore.write()` 做了并发压力反例：

测试条件：

- 本机真实 HTTP `/healthz`；
- 2 个并发客户端累计 2000 次 GET；
- 同时执行 500 次 `StatusStore.write()`；
- playlist 为本地假数据，不访问任何公网地址。

实际结果：

```text
STATUS_RACE_WRITES 209 READS 2000 ERRORS 291
ERR_SAMPLE: PermissionError [WinError 5] runtime-status.json.tmp... -> runtime-status.json
```

即 **500 次状态写只有 209 次成功，291 次失败（58.2%）**。失败点正是 `_atomic_write_json()` 的裸 `os.replace(tmp, runtime-status.json)`；与此同时 `/healthz` 通过 `StatusStore.read() -> Path.read_text()` 用默认 Windows 文件共享方式打开同一目标。

这不是可以后置的轻微观测误差：TASK-004 明确要求运行状态文件原子更新、`/healthz` 暴露 last successful publish / last run / freshness，并要求失败轮次不得伪造成功时间。若 scheduler + HTTP 同时运行时大量状态更新被丢弃，健康端点可能长期展示旧的 `last_success_publish_at` / last_run，直接破坏本任务的运行状态与新鲜度功能。

### 唯一返工要求

1. 关闭同一物理问题，不扩大架构：Windows 下 status 写入与 `/healthz` 读取必须能并发。
2. 最小可接受方案：
   - 写侧 `_atomic_write_json()` 对目标被读句柄瞬时占用的 WinError 5/32 使用已有的窄口径、有界 replace retry；**或**
   - 读侧改为允许 `FILE_SHARE_DELETE` 的共享读；
   - 推荐两侧都按现有 playlist/lock 已验证模式统一，但不要为了抽象而重构已 ACCEPT 模块。
3. 其它 OSError（权限、磁盘满、非法路径等）仍需正常失败，不得无脑吞错或无限重试。
4. 新增真实 Windows/跨平台可跳过的并发永久回归：HTTP `/healthz` 高频读取 + 至少数百次 status 原子写；要求 **0 次因读写竞争导致的状态写失败**，最终 JSON 可解析且最后写入值可验证。
5. 再加一条功能级回归：scheduler 成功发布后，即使 `/healthz` 同时被连续请求，`last_success_publish_at` 最终必须推进，不能因为状态写失败保留旧值。
6. 现有 266 项测试必须零回归；更新 `REPORTS/TASK-004-REPORT.md`，状态回 REVIEW，commit + push 后停 Gate；禁止启动 TASK-005。

## Gate

本轮不要求修改 HTTP 路由、scheduler 业务顺序、锁语义、publish、数据库 schema 或 TASK-001/002/003 已验收代码。QA-004A/B 已通过，不得借本轮重做。
