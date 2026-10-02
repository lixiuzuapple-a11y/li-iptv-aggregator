# TASK-005 Independent QA — Review 02（最终验收）

日期：2026-10-02
Reviewer：大G
受审 HEAD：`041b9a3b86691d6355ccfcfe47ba60f42370c75a`
返工实现提交：`3a8ee8f82f3bec16a16cf115c78acbc346a2eccd`
结论：**ACCEPT**

## 最终核验

- QA-005A 已关闭：任一 `FFPROBE_NOT_FOUND / FFPROBE_START_FAILED` 环境级错误出现时，本轮 probe 走 fail-closed，整轮 0 条 probe_result，旧历史仍可用于 publish，runtime 明确标记 degraded / exit 1。
- 大G独立运行 `mixed_environment_failure` 两条永久回归：**2 passed**。
- QA-005B 已关闭：heartbeat 不再是裸 read-check-write，而是跨进程 gate + CAS 等价提交；旧 owner 不会覆盖新 owner token。
- 大G用 Review 01 同口径的确定性竞态注入复验：

```text
HEARTBEAT_RETURN False
FINAL_TOKEN intruder-token
INTRUDER_SURVIVED True
LOCK_THINKS_LOST True
LAST_ERROR 提交前复核失败：锁已被其它实例接管（未覆盖对方 token）
```

即旧反例已经完全翻转。

## 冻结边界

- `liptv/select.py`：未改；大G独立跑 **11/11 passed**。
- `schema/schema_v1.sql`：未改。
- `liptv/publish.py`、`liptv/server.py`、`liptv/m3u.py`：未改。
- `git diff --check 8006a9a..HEAD` 干净。

## 测试证据

- 小W提交前全量结果：**305 passed / exit 0**。
- 大G独立复验：
  - QA-005A 两条混合环境错误用例：2 passed；
  - selector 原测试：11 passed；
  - heartbeat CAS 确定性反例：通过，intruder token 保留、旧 owner 明确失锁。
- 部分较重 pytest 组合在当前 Windows Runner 中出现长时间运行/不回显，因此本报告不声称大G取得单命令 305 项 exit 0；最终 ACCEPT 依据为：小W完整全量 + 大G对返工核心语义的确定性独立复验 + 冻结模块差异核验。

## Gate

**TASK-005 ACCEPTED**。

项目现在具备：
- 本机真实 ffprobe 固定流测活；
- 环境级错误 fail-closed，不污染 selector 历史；
- probe_result 历史驱动既有 selector；
- scheduler 中 fetch → sync → probe → publish 的同轮闭环；
- 单实例锁 heartbeat 的 CAS 等价 ownership 保护。

仍不包含多地区探针、云端 probe agent、腾讯云部署、长时 QoE、EPG/Logo、转码/代理或 schema V2。

TASK-006 尚未定义、未启动。
