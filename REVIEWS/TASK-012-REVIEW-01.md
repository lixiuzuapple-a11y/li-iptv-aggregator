# TASK-012 REVIEW-01 — CHANGES REQUESTED

日期：2026-10-08
Reviewer：大G
结论：**CHANGES REQUESTED / 暂不 ACCEPT**

## 1. 总体判断

TASK-012 主体质量高，方向正确，静态审查与小W生产证据基本成立：
- fixed stable 42/44 = 95.5%；
- 8/8 failover + recovery（隔离副本注入，非自然故障）；
- 55 轮生产 soak；
- dynamic preflight / stability / cadence / retention / no-change publish 均有真实问题驱动与回归；
- 本轮发现并修复 `/healthz` 永久 stale 真 bug，修法为结构性约束而非单点补丁。

但大G恢复 WebCodex 后做独立生产 spot-check，发现 **Daily Reliability Summary 没有真正进入生产无人值守闭环**。

因此本轮只做精准返工，不推倒 TASK-012。

---

## 2. 大G独立生产证据

2026-10-08 真机只读检查：

- `li-iptv.service`：active + enabled
- `NeedDaemonReload=no`
- `NRestarts=0`
- User/Group：`liptv:liptv`
- 监听：仅 `127.0.0.1:8080`
- `/healthz`：HTTP 200，`status=ok`，`is_stale=false`
- `/live.m3u`：HTTP 200
- `/epg.xml`：HTTP 200
- fixed with tvg-id：43
- dynamic current entries：181（随时点变化）
- XMLTV channels：42
- programmes：11059
- M3U↔XMLTV intersection：41
- 未命中仍为 `France24.fr` / `FujianStraitsTV.cn`

最重要的是：

`/var/lib/li-iptv-aggregator/reliability-summary.json` **不存在**。

生产 config 中也没有 reliability summary 的自动生成配置；代码搜索确认：

- `reliability-status` 默认只打印；
- 只有显式 `--write` 才调用 `write_summary()`；
- scheduler/runtime/deploy 中没有自动执行 `reliability-status --write`；
- deploy 模板也没有 reliability summary 的定时生成路径。

所以现在是：

> **可以手工生成 reliability summary，但生产不会自动持续生成。**

这与 TASK-012 的“Daily Reliability / 长期无人值守”目标不一致。

---

## 3. R1 — 必须修：Daily Reliability 生产闭环

目标：生产无需人工命令，就能持续拥有最新的 reliability summary。

### 可接受实现

优先选择最简单的一种，不要造新系统：

**方案 A（优先）**：在现有 scheduler 每个成功/降级轮次完成后，调用一次 reliability summary 生成并原子覆盖。

或：

**方案 B**：独立 systemd timer，每日/每若干小时执行一次只读 `reliability-status --write`。

优先 A，原因：
- 已有 DB / publish-summary / runtime-status 都在当轮最新；
- 不增加第二套 timer 运维面；
- summary 写失败本来就设计为 non-blocking。

但无论选哪种，必须满足：

1. 生产自动生成 `/var/lib/li-iptv-aggregator/reliability-summary.json`；
2. service user 可写、可读；
3. 原子写；
4. 写失败不影响 live.m3u publish；
5. 每轮/每日不会无限创建新文件，只覆盖一个有界文件；
6. summary 里无完整 stream URL / signed query / Cookie / Authorization / VPN；
7. system restart 后仍能继续自动更新；
8. `generated_at` 会随生产轮次推进；
9. 不要求新增 Dashboard / Prometheus / timer farm。

### 回归测试

至少增加：

- scheduler 成功轮次会触发 summary write；
- `DEGRADED_DYNAMIC_PARTIAL` 也会触发；
- rejected/no-publish 情况的语义明确；
- summary write failure 不改变 publish exit/status；
- 路径固定在运行数据目录；
- restart 后下一轮能继续覆盖；
- 输出脱敏；
- 文件不会累计成 history 目录。

---

## 4. R2 — 文档最终口径必须统一

`REPORTS/TASK-012-REPORT.md` 当前正文仍保留：

> healthz 修复“尚未部署到生产”

但后续执行摘要已写：

> release `997326580c86-20261008T010432Z` 已上线。

这两者不能并存。

返工后直接修主报告原文，以最终生产事实为准：

- healthz 修复已部署；
- 大G独立 spot-check `/healthz=200 status=ok`；
- reliability summary 在本次返工后真实存在且自动更新；
- soak 最终轮次统一使用 55 轮，不再在正文残留早期 21 轮作为“最终数字”；若保留 21 轮阶段证据，明确标成阶段快照。

---

## 5. 已通过、不要重做的部分

以下不要求重做：

1. stability 42/44 与 95.5% 目标；
2. 8/8 failover/recovery 隔离副本验证；
3. preflight authoritative/advisory 设计；
4. KORICE advisory 冻结；
5. 502/503/504 → UNKNOWN 防误杀；
6. no-change publish；
7. retention latest_complete_day 口径；
8. 120.76.248.139 不做粗暴 host blacklist；
9. selector 本轮不改动；
10. healthz stale 修复本身。

---

## 6. Reviewer 对现有结果的独立确认

大G已经独立确认：

- healthz stale 修复在当前生产**确实生效**；
- `/healthz` 当前 200 + ok；
- live/EPG 三端点正常；
- 本地与 GitHub HEAD 一致且工作区 clean；
- 当前生产 M3U / XMLTV 数字与 TASK-011/012 口径基本一致。

所以本次返工只针对“日报自动落生产”和文档收口。

---

## 7. 完成要求

小W：

1. 实现 R1；
2. 修 R2；
3. 增加回归测试；
4. 部署生产；
5. 至少跨两个 scheduler round 验证 `generated_at` 自动推进；
6. 真机确认文件属主/权限；
7. `git diff --check` PASS；
8. 受影响测试 + TASK-012 回归 PASS；
9. 更新 TASK-012 REPORT；
10. commit + push main；
11. TASK 继续保持 `REVIEW`；
12. 停止，禁止启动 TASK-013。

完成后等待大G REVIEW-02。