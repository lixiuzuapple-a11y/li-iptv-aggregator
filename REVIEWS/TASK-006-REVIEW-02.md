# TASK-006 Independent QA — Review 02（最终验收）

日期：2026-10-03
Reviewer：大G
受审 HEAD：`79eb6a1dda1707e000d9564956cba817762dec91`
返工实现提交：`2c9ab6b726177d1c2ab8fd0f53ff443200e3c8a8`
结论：**ACCEPT**

## 最终核验

- QA-006A 已关闭：生产属主矩阵已真正落地。
- 大G独立打印 `Layout.mode_table('r1')`，结果：

```text
/opt/li-iptv-aggregator                    root:root 0755
/opt/li-iptv-aggregator/releases           root:root 0755
/opt/li-iptv-aggregator/releases/r1        root:root 0755
/opt/li-iptv-aggregator/venv               root:root 0755
/opt/li-iptv-aggregator/current            root:root 0644
/opt/li-iptv-aggregator/deploy-state.json  root:root 0640
/etc/li-iptv-aggregator                    root:liptv 0750
/etc/li-iptv-aggregator/config.toml        root:liptv 0640
/var/lib/li-iptv-aggregator                liptv:liptv 0750
/var/lib/li-iptv-aggregator/liptv.sqlite3  liptv:liptv 0640
/var/lib/li-iptv-aggregator/backups        liptv:liptv 0700
/var/cache/li-iptv-aggregator              liptv:liptv 0750
/run/li-iptv-aggregator                    liptv:liptv 0750
/etc/systemd/system/li-iptv.service        root:root 0644
```

- QA-006A 三条权限永久回归由大G独立运行：**3 passed**。

- QA-006B 已关闭：restore-db 现在有独立停机门禁；`--yes` 只代表确认覆盖，不再代表“服务已停”。
- 大G独立运行关键恢复反例：
  - active service + stop 失败：**1 passed**，恢复被拒；
  - active service + stop 成功：**1 passed**，复查 inactive 后才恢复；
  - service state unknown + break-glass：**2 passed**，默认拒绝，显式 force 才允许。
- `restore_sqlite()` 的 pre-restore safety copy 已改用 SQLite backup API，并在替换目标库前验证；失败时拒绝继续。
- 恢复成功后默认不自动重启服务，要求运维人员确认数据后显式启动并复查 `/healthz`。

## 冻结边界

返工相对 Review 01 未修改以下冻结模块：

- selector；
- probe；
- publish；
- HTTP server；
- m3u；
- ingest/fetch；
- schema V1。

大G执行 `git diff f067bff..HEAD -- <冻结模块>` 输出为空；`git diff --check` 通过。

## 测试证据

- 小W返工后提交前全量：**394 passed / exit 0**。
- 小W离线部署 demo：**86/86** 断言通过。
- 大G本轮独立复验：
  - QA-006A 权限矩阵：3 passed；
  - QA-006B stop-fail：1 passed；
  - QA-006B stop-success：1 passed；
  - QA-006B unknown/break-glass：2 passed；
  - 权限矩阵直接打印与代码实现一致；
  - 冻结模块 diff = 0。

本轮部分大测试组合在 Windows Runner 中仍存在长任务不回显现象，因此大G不声称自己取得单命令 394 项 exit 0；最终 ACCEPT 依据为：小W完整全量 + 大G对两条阻断项的独立确定性复验 + 冻结边界核验。

## 实机部署边界

**TASK-006 ACCEPT 不等于真实 Linux 主机已经上线。**

本任务验收的是：

- Linux 生产目录/权限模型；
- systemd unit 与 hardening 规则；
- install / upgrade / rollback；
- doctor；
- SQLite backup / restore 安全门禁；
- 健康检查；
- 离线部署 E2E。

真实 Linux/systemd 主机部署仍标记 **NOT EXECUTED**。首次真实上线时仍必须做一次实机验收：systemd、目录权限、ffprobe、`/healthz`、`/live.m3u`、重启恢复与实际网络暴露方式。

## Gate

**TASK-006 ACCEPTED**。

TASK-007 尚未定义、未启动。
