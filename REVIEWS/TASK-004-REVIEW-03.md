# TASK-004 Independent QA — Review 03（最终验收）

日期：2026-10-01
Reviewer：大G
受审 HEAD：`6e8d8452243d60cfc6657cae11169457e30a0e50`
返工实现提交：`f49c2d4f3c53623230685dcdadd563fd15b6b8db`
结论：**ACCEPT**

## 最终核验

- QA-004A：scheduler 心跳已真实接入轮次边界、长休眠和长轮次后台线程；失锁会停止后续 fetch/publish。
- QA-004B：release/heartbeat 已改为 fail-closed，无法确认 token 归属时不删除、不覆盖未知锁。
- QA-004C：Windows 下 `/healthz` 并发读取 `runtime-status.json` 与状态原子替换冲突已关闭。

大G使用与 Review 02 相同形状的独立压力反例复验：

```text
WRITES 500 READS 2000 ERRORS 0 DEGRADED 0 FINAL_MARKER 499
ERR_SAMPLE []
TMP_LEFT 0
```

即 500/500 状态写成功、2000/2000 HTTP 读取成功、0 WinError 5/32、0 次 freshness.source 降级、最终值正确、无临时文件残留。

功能级回归 `publish_advances_last_success_while_healthz_is_hammered` 也由大G独立运行通过，证明高频 `/healthz` 请求期间，成功发布仍能推进 `last_success_publish_at`。

## 测试与基线

- Review 02 时，大G已独立完整覆盖当时的 **266/266** 项测试，分组全部 exit 0。
- 本轮补丁相对 Review 02 只修改 `liptv/runtime.py` 及对应测试/文档/演示；`server.py`、`m3u.py`、`cli.py`、`publish.py`、schema 等冻结业务模块均未改。
- 小W报告的本轮全量结果为 **269 passed / exit 0**；大G独立复验了 3 个新增 QA-004C 关键场景中的功能级用例和 500×2000 实际并发压力反例，均通过。
- `git diff --check` 干净；本地与远程受审 HEAD 一致，工作区干净。

当前 Windows Runner 对部分长 pytest 组合命令仍存在超时噪声，因此大G不声称自己在本轮取得“单命令 269 项 exit 0”。最终 ACCEPT 依据为：上一轮 266 项完整独立基线 + 本轮严格变更范围 + 新增 QA-004C 独立反例与功能级回归通过。

## Gate

**TASK-004 ACCEPTED**。

本轮最终具备：
- 本地周期 scheduler；
- 单实例锁 + 持续心跳 + 失锁即停；
- fail-closed 的保守锁释放；
- 只读 HTTP `/live.m3u` / `/healthz`；
- Windows 下发布文件与状态文件的并发读写保护；
- last-known-good 与健康新鲜度状态。

仍不代表已经完成腾讯云/公网部署、TLS/域名、真实 ffprobe、多地区探针、EPG/Logo 或系统服务安装；这些需另立 TASK。

TASK-005 尚未定义、未启动。
