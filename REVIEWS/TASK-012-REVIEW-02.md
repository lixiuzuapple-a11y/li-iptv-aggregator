# TASK-012 REVIEW-02 — CHANGES REQUESTED

日期：2026-10-09
Reviewer：大G
结论：**CHANGES REQUESTED / 仅剩生产部署闭环**

## 1. 结论

REVIEW-01 的代码返工方向正确，`3e45269` 已把 reliability summary 挂到现有 scheduler after-round，并补齐路径推导、non-blocking 写盘、restart/脱敏/有界文件等测试。

但大G独立真机复核确认：**这版代码尚未部署到生产。**

因此 TASK-012 仍不能 ACCEPT；本轮禁止重写逻辑，只补部署闭环。

## 2. 独立证据

当前仓库：
- HEAD `3e45269`
- `main...origin/main` clean
- `git diff --check f295bfc..3e45269` PASS
- `tests/test_task012d.py`：33 collected，32 passed + 1 skipped

当前生产：
- service active + enabled
- NeedDaemonReload=no
- NRestarts=0
- `/healthz` 200 / ok
- `/live.m3u` 200
- `/epg.xml` 200
- 8080 only `127.0.0.1`

但：
- `/var/lib/li-iptv-aggregator/reliability-summary.json` **不存在**
- service unit 当前 `PYTHONPATH=/opt/li-iptv-aggregator/releases/997326580c86-20261008T010432Z`
- journal 当前已跑到 round #79，仍是旧 release
- 旧 release 内没有 `after_round / refresh_after_round / --no-reliability-summary` 新实现

所以 REVIEW-01 的生产验收条件尚未真正发生。

## 3. 唯一返工项

把 GitHub/main 当前 `3e45269` 对应 release 正式部署到生产。

要求：
1. 使用既有 deploy upgrade 流程；
2. daemon-reload / restart 确保 systemd 真正切到新 release；
3. 不改安全组、防火墙、DNS/TLS；
4. 不动 EV-Lab；
5. 不手工伪造 reliability-summary.json；
6. summary 必须由 scheduler 自己生成。

## 4. 部署后必须验证

至少跨两个 scheduler round：

### Round A
- 文件自动出现：`/var/lib/li-iptv-aggregator/reliability-summary.json`
- owner/group/mode 合理，service user 可读写
- JSON parse PASS
- `generated_at = A`

### Round B
- 不执行人工 `reliability-status --write`
- scheduler 自然跑下一轮
- 同一文件仍存在
- `generated_at = B` 且 `B > A`
- 文件是覆盖更新，不产生 history 目录/无限新文件

同时确认：
- `/healthz` 200 + ok
- `/live.m3u` 200
- `/epg.xml` 200
- NRestarts 无异常增长
- 8080 仍 localhost
- `after_round_failures = 0`（如有计数）
- summary redaction 五项仍为 false

## 5. 文档

`REPORTS/TASK-012-REPORT.md` 的口径统一本次已基本完成，保留。

部署验证完成后，只需在报告附录追加最终生产 release、两个 round 的 `generated_at` 和权限证据；不要再次改动主体数字，除非真实生产数据发生变化。

## 6. 禁止事项

- 不要重新设计 reliability 模块
- 不要新建 timer
- 不要重跑 8/8 failover
- 不要重做 55 轮 soak
- 不要修改 selector
- 不要启动 TASK-013

完成后 TASK 仍保持 REVIEW，等待大G REVIEW-03。