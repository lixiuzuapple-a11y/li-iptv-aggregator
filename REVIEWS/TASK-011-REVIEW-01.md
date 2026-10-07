# TASK-011 REVIEW-01 — CHANGES REQUESTED

日期：2026-10-07
Reviewer：大G
结论：**CHANGES REQUESTED / 暂不 ACCEPT**

## 1. 总体判断

TASK-011 的代码与数据工作大部分成立：metadata / tvg-id / logo / XMLTV / LKG / France 24 / 分片级 probe 等均有实质价值；大G独立重跑 `tests/test_task011.py`，62 passed。

但生产存在一个阻断缺陷：**`GET /epg.xml` 当前真实返回 HTTP 404**。因此“EPG 已上线供订阅服务读取”的核心闭环尚未完成，TASK-011 不能 ACCEPT。

不要重做整轮。只做本 Review 指定的精准返工，完成后回到 REVIEW。

---

## 2. 大G独立生产证据

2026-10-07 只读 SSH spot-check：

- `li-iptv.service`：active + enabled
- service User/Group：`liptv:liptv`
- `/live.m3u`：HTTP 200
- 当前 fixed with tvg-id：43
- 当前 dynamic without tvg-id：182
- **`/epg.xml`：HTTP 404**
- `/etc/li-iptv-aggregator/epg.xml`：实际存在，但为 `root:root 0600`
- `/etc/li-iptv-aggregator/`：`root:liptv 0750`
- `liptv` 用户无法读取该 EPG 文件
- 生产 `config.toml` 当前没有 `[epg]` 段，故代码默认 `epg.enabled=false`
- systemd hardening 的可写运行目录只有 `/var/lib/li-iptv-aggregator`、`/var/cache/li-iptv-aggregator`、`/run/li-iptv-aggregator`
- systemd 当前另有 `NeedDaemonReload` 类警告，需要在返工后核清并消除

结论：EPG 是运行期生成/LKG 数据，**不应放在 `/etc`**。当前同时存在“生产未启用 EPG”和“文件位置/属主不符合服务运行模型”两个问题。

---

## 3. 必须返工 — R1 生产 EPG 闭环

### R1.1 路径模型
将生产 EPG 运行期产物迁到持久数据目录，例如：

- `/var/lib/li-iptv-aggregator/epg.xml`
- `/var/lib/li-iptv-aggregator/epg-status.json`

不得继续把可变运行期 EPG 文件放 `/etc`。

理由：
- `/etc` 是配置目录；
- service `ProtectSystem=strict`；
- systemd 已明确只开放 `/var/lib` / `/var/cache` / `/run` 写入；
- EPG refresh/LKG 属运行期数据，与 `live.m3u` 同类。

### R1.2 生产配置
生产 `/etc/li-iptv-aggregator/config.toml` 必须显式配置 `[epg]`：

- `enabled = true`
- `output_path` 指向 `/var/lib/li-iptv-aggregator/epg.xml`
- `status_path` 指向 `/var/lib/li-iptv-aggregator/epg-status.json`
- 主力 source 使用已验证的 fanmingming jsDelivr feed
- `restrict_to_metadata = true`

`[server].epg_path = "/epg.xml"` 可显式写出。

### R1.3 权限
EPG 文件必须由服务账号可读，refresh 运行模型下应可由服务账号安全替换。

建议与 live.m3u 一致采用 `liptv:liptv` + 0640（或等价最小权限），不得靠 world-readable 0777/0666 绕过。

### R1.4 部署模板
修 `deploy/config.production.example.toml` / deploy 相关测试，使未来 install/upgrade 不会再次把 EPG 漏出生产配置。

至少新增回归测试证明：
1. production config 支持/生成 `[epg]`；
2. EPG 运行文件路径位于可写数据目录，不在 `/etc`；
3. service user 能读取生成的 EPG；
4. `/epg.xml` 在 enabled + valid file 时 200；
5. disabled / missing / empty 语义仍符合 404/503 设计。

### R1.5 生产验收
返工后必须真机验证：

- `sudo -u liptv test -r <epg.xml>` = success
- `GET http://127.0.0.1:8080/epg.xml` = 200
- Content-Type XML
- XML well-formed
- channel/programme 数量合理
- M3U tvg-id ↔ XMLTV channel id 交集按当前真实发布集合核对
- `/live.m3u` 仍 200
- `/healthz` 仍 status=ok
- localhost 8080 不变
- `systemctl show li-iptv.service -p NeedDaemonReload` 最终必须为 `no`

---

## 4. 必须返工 — R2 文档勘误

接受小W `REPORTS/TASK-011-ERRATA-01-PLAYER-COMPAT.md` 的 E1～E3 发现。

不要只留一份旁路 ERRATA。直接修原报告，避免下一轮继续读到错误事实：

1. `REPORTS/TASK-011-PLAYER-COMPAT.md` 修正服务单元名、EPG 实际路径/获取方式、固定频道数字。
2. `REPORTS/TASK-011-REPORT.md` 统一 41 / 42 / 43 / 44 的统计口径：明确区分 seed inventory、metadata inventory、当前 selected/published fixed、XMLTV channels、tvg-id intersection。
3. `REPORTS/TASK-011-EPG-COVERAGE.md` 在 France 24 纳入后的最终口径重新生成/校准，不允许前后用不同快照却写成同一口径。
4. 返工后的报告必须以**修复后的生产最终态**为准，不复制旧数字。

---

## 5. 三项 Reviewer 裁决

### Q1 — Apple TV / APTV 如何拿到 M3U + EPG

**裁决：本 TASK 选 A，不解除公网冻结。**

- 不开放 8080 公网。
- 不改安全组/DNS/TLS。
- 不为了“实机验证”破坏 TASK-011 §27 与验收第 47 条。
- Apple TV 实机显示继续标记 **未验证**。

APTV 官网当前公开资料可以独立确认：APTV 支持 M3U 文件或 URL、具备节目单能力，并公开推荐 XMLTV EPG URL；但公开页不足以完全独立证明“本地配置与 EPG 输入字段互斥”的具体 UI 细节。因此 ERRATA 的 APTV UI 结论保留为高可信待 Owner 实机确认，不作为本次代码验收阻断。

后续如需要真正日常使用，应单独设计“安全的播放器交付通道”，优先家庭内网/VPN/Tailscale/已有 NAS 等，不在 TASK-011 临时开公网。

### Q2 — 数字/路径是否现在修

**裁决：现在修。**

理由：E2 路径错误会直接导致运维取错文件；41/42/43/44 口径混用会污染下一轮判断。不要只依赖 ERRATA。

### Q3 — `120.76.248.139` 是否现在硬排除

**裁决：暂不加 host 级永久排除。**

理由：
- 当前 selector/probe 已能把实际失败 stream 降权/排除；
- 故障形态从 timeout 变为 fake-200/签名下游不可达，证据机制发生变化；
- host 级黑名单误伤成本高。

继续记录真实 probe history。若后续形成稳定、重复、跨轮的同型无效证据，再做精确排除；优先按 stream 行为判坏，不按一个 host 一刀切。

### Q4 — epg.pw 42 条 id 映射是否现在做

**裁决：TASK-011 不做。**

当前主力 EPG 覆盖已约 95%+，且 LKG 已存在；为第二源维护一套 42 条数字 id 人工映射的即时收益不足。保留为后续灾备优化。如果主力源出现持续可靠性问题，再做 exact/manual mapping，不做 fuzzy。

---

## 6. 国际频道验收裁决

任务书目标“至少 2 个国际频道”本轮只得到 France 24。

**Reviewer 接受该例外，不要求继续凑第二个。**

理由：
- 已扩到 15 个候选；
- NHK / DW / Al Jazeera / CGTN 等已做更深层的 segment/media-first-byte 验证；
- 真实世界证据不支持第二个稳定候选；
- 任务书同时明确“不降低门槛、不为分类凑数”。

因此国际频道数量不足不再作为返工项。France 24 的 6 条真实 probe success 是有效交付。

---

## 7. 测试结论

大G独立运行：

`tests/test_task011.py` → **62 passed in 10.45s**。

说明 TASK-011 单元/离线专项实现基本健康；当前阻断是**生产配置/部署闭环没有被测试覆盖**。返工必须补上这一层测试，不能只再次跑 62 项然后宣称修复。

---

## 8. 结束要求

小W只处理本 Review：

1. 修 R1 生产 EPG 闭环；
2. 修 R2 报告原文；
3. 增加生产配置/部署回归测试；
4. 真机 smoke；
5. `git diff --check` PASS；
6. 受影响测试 + TASK-011 + deploy/server/runtime 回归；
7. 更新 TASK-011 REPORT，说明 REVIEW-01 返工结果；
8. commit + push；
9. TASK 仍置 `REVIEW`；
10. 停止，禁止启动 TASK-012。

完成后等待大G REVIEW-02。