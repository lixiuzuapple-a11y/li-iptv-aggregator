# TASK-004 Independent QA — Review 01

日期：2026-10-01
Reviewer：大G
受审 HEAD：d058805e970c4f79da7cd7b5794a8ca89946af8d
实现提交：5ab2e9d7b5ba6823ca180a711988a2a254d3f66a
结论：**REJECT → 两处锁生命周期定向返工 → REVIEW**

## 已确认的成果

- Scheduler、单实例锁、只读 HTTP `/live.m3u` / `/healthz`、状态文件、新鲜度、Windows 共享读取与发布侧 replace 重试均已实际实现。
- 大G独立跑完现有 **248/248** 项测试，分组全部 exit 0：
  - tests/test_server.py：23 passed；
  - tests/test_runtime.py（除真实信号子进程）：35 passed；
  - 真实信号子进程：1 passed；
  - publish/m3u/历史 QA：79 passed；
  - 其余旧 core/network：110 passed。
- 因此现有测试覆盖的 HTTP、安全路由、原子读取、异常 loop、旧功能零回归均成立。

## 阻断 QA-004A：长时间运行时锁心跳从未刷新，跨主机共享目录可被误接管

`SingleInstanceLock.heartbeat()` 已实现，文档也明确称“每轮开始/结束刷新一次”，但实际 cmd_run / Scheduler 没有任何业务调用。因此锁在长跑中 heartbeat_at 永远停留在 acquire 时刻。

大G独立反例：

1. 取得锁，heartbeat_at = 2026-10-01T00:00:00+00:00；
2. scheduler 连跑 3 轮；
3. 锁文件 heartbeat_at 仍完全不变；
4. 模拟同一共享输出目录由另一台主机观察，在 stale_after_seconds=10、当前时间 20 秒后重新 acquire；
5. 第二实例成功以 stale_heartbeat 接管。

实际输出：
```
HEARTBEAT_BEFORE 2026-10-01T00:00:00+00:00 AFTER 2026-10-01T00:00:00+00:00 UNCHANGED True
REMOTE_CONTENDER_ACQUIRED True reason stale_heartbeat
```

这会在共享目录/跨主机场景形成两个同时写入者，直接违反 TASK-004 的“同一套 data/output 目录禁止两个 scheduler”。

### 必须修复

1. 把 heartbeat 真正接入 scheduler 生命周期：至少每轮开始/结束和长 interval 等待期间周期刷新，刷新频率必须显著小于 stale 阈值；不能只在业务轮次完成后刷新，否则长轮次或长 sleep 仍会误 stale。
2. 若 heartbeat 发现 token 已变或刷新失败，当前 scheduler 必须停止进入下一轮，不可继续写。
3. 增加 fake clock/sleep 永久回归：长跑超过 stale 阈值时 heartbeat_at 持续推进；模拟另一主机读取同一锁时仍判 HELD_REMOTE，不得接管。
4. 增加“heartbeat token 被替换”场景：当前 scheduler 检测失去锁后停止，并且不再执行后续 fetch/publish。

## 阻断 QA-004B：release 遇到不可解析锁文件会删除它，违背“只删自己的锁”

release() 当前逻辑：

- 若能读出元数据且 token 不同 → 不删；
- 若元数据无法解析，current is None → 直接 unlink。

大G独立反例：

1. 当前实例 acquire 成功；
2. 在 release 前把锁文件替换成另一持有者/外部进程写入的损坏 JSON；
3. 调用 release()。

实际结果：
```
RELEASE_RESULT True FILE_EXISTS_AFTER False
```

也就是说，无法证明“这个锁仍属于自己”时仍然会删除，和模块自己声明的“只删 token 还是自己的那把锁”矛盾。

### 必须修复

- release 必须 fail-closed：只有能成功解析并确认 token == 自己的 token 时才允许 unlink。
- 文件缺失可返回 False；不可解析 / token 不同均不得删除。
- 增加永久回归：损坏 JSON、空文件、合法但 token 不同三种情况都不得删除；自己的合法 token 正常释放。
- heartbeat 同样保持“无法确认 ownership 就停止拥有状态，不覆盖未知锁”的保守语义。

## 返工范围

只修 QA-004A / QA-004B，不重构 HTTP 服务、发布逻辑、数据模型或 TASK-001/002/003 已验收模块。

现有 248 项必须零回归，并新增锁心跳与保守 release 回归。更新 REPORTS/TASK-004-REPORT.md，状态回 REVIEW，commit + push 后停 Gate；禁止启动 TASK-005。
