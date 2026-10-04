# TASK-007 — Real Linux Host Deployment & Production Smoke Acceptance

状态：REJECTED（Review 01：QA-007A/B 真机缺陷 + G-007C fixed-stream Gate；见 REVIEWS/TASK-007-REVIEW-01.md）
Owner：老李
Architect / Reviewer：大G
Executor：小W
基线：TASK-001 / 002 / 003 / 004 / 005 / 006 均已独立 ACCEPT；开工前拉取最新 main。
参考：DEPLOYMENT.md、TASKS/TASK-006.md、REVIEWS/TASK-006-REVIEW-02.md。
本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-008。

## 目标

把 TASK-006 已验收的“离线生产部署能力”真正落到**一台真实 Linux 主机**，完成首次生产级 smoke acceptance。

本轮不是开发新业务功能，而是回答一个问题：

> 这套服务在真实 Linux + systemd + ffprobe + SQLite + 实际网络环境里，能否按 TASK-006 的设计长期稳定运行？

最终目标链路：

```text
real Linux host
→ systemd
→ liptv run --serve
→ fixed fetch
→ stream-sync
→ real ffprobe
→ selector
→ publish
→ localhost /healthz + /live.m3u
→ controlled client access
```

---

## 0. 强制 Gate：真实主机变更前必须有 Owner 明确授权

这是本任务最重要的边界。

### 0.1 默认只允许只读侦察

在未获得老李对**具体主机**的明确授权前，小W只能：

- 查询主机 OS / CPU / RAM / 磁盘；
- 查询 Python / ffprobe / systemd 版本；
- 查询端口占用；
- 查询现有 systemd services；
- 查询现有用户/目录；
- 查询防火墙/安全组当前状态；
- 查询网络连通性；
- 查询仓库/代码是否已存在；
- 形成部署计划。

**禁止**：

- apt install / apt upgrade；
- useradd / groupadd；
- 创建 `/opt` `/etc` `/var/lib` 生产目录；
- 写 systemd unit；
- systemctl enable/start/stop/restart 任何现有服务；
- 修改 firewall / nftables / iptables / 安全组；
- 修改 DNS / TLS / 反向代理；
- clone/pull 到生产目录；
- 占用生产端口；
- 写入真实 IPTV source；
- 修改 EV-Lab 或其他现有项目任何文件。

### 0.2 候选主机的特殊保护

目前只读信息显示存在一台候选 Tencent Cloud Lighthouse：

- 名称：`ev-lab-shanghai`；
- Ubuntu 24.04 LTS；
- 正在运行 EV-Lab 生产采集服务。

这台主机**不是自动批准的部署目标**。

如果最终使用它，必须由老李明确说出“允许在 ev-lab-shanghai 上部署 IPTV”或等价授权后才可进入 Phase B。

没有这条授权时，本任务最多完成 Phase A（只读侦察）并停 Gate。

---

## 1. Phase A — 只读实机 Preflight

对候选主机做只读检查并生成证据。

至少记录：

- distro / kernel / architecture；
- CPU / RAM / swap；
- root filesystem 剩余空间；
- Python 版本；
- `ffprobe -version` 是否存在；
- systemd 版本；
- 8080 或拟用端口是否空闲；
- 当前监听端口；
- 当前 systemd active services；
- 是否已有 `liptv` 用户；
- `/opt/li-iptv-aggregator`、`/etc/li-iptv-aggregator`、`/var/lib/li-iptv-aggregator` 是否存在；
- 当前防火墙 / ufw 状态；
- 当前对外公网/IP/私网访问条件；
- 是否存在 Caddy/nginx/Tailscale/WireGuard；
- Git/Python 到 GitHub/包源是否可访问（只读）；
- 当前时间 / 时区 / NTP 状态。

必须输出**部署冲突清单**：

- 与现有 EV-Lab 的用户冲突；
- 目录冲突；
- 端口冲突；
- systemd unit 冲突；
- CPU / RAM 资源竞争；
- 磁盘空间；
- 网络/防火墙冲突。

Phase A 不得产生主机状态变化。

---

## 2. Phase B — 经 Owner 授权后的真实安装

只有明确授权具体主机后才执行。

### 2.1 隔离要求

如果与 EV-Lab 共机，必须同时满足：

- 独立用户：`liptv`；
- 独立组：`liptv`；
- 独立 app：`/opt/li-iptv-aggregator`；
- 独立 config：`/etc/li-iptv-aggregator`；
- 独立 data：`/var/lib/li-iptv-aggregator`；
- 独立 cache/run；
- 独立 systemd unit：`li-iptv.service`；
- 独立监听端口；
- 不改 EV-Lab unit；
- 不改 EV-Lab Python/venv；
- 不改 EV-Lab 数据目录；
- 不 kill/restart EV-Lab 进程。

任何一步发现需要修改 EV-Lab 才能继续，立即停止。

### 2.2 安装前快照

真实写入前记录：

- 当前 systemd active services；
- 当前监听端口；
- EV-Lab unit 状态；
- 当前磁盘占用；
- 目标目录不存在/现状；
- 目标 unit 不存在/现状。

用于部署后证明“未伤及现有服务”。

### 2.3 安装 ffprobe

如果 ffprobe 不存在：

- 仅在 Owner 已授权真实部署后允许安装；
- 使用系统包管理器，例如 `apt install ffmpeg`；
- 记录安装前后版本；
- 不做全系统升级；
- 不替换系统 Python；
- 不安装无关软件。

---

## 3. 真实部署

严格使用 TASK-006 已验收的部署入口。

推荐：

```bash
sudo tools/deploy_linux.sh plan
sudo tools/deploy_linux.sh install
```

要求：

- plan 先执行并归档；
- install 必须用正式 root `/` 前缀，不用测试 `--root`；
- 必须创建/确认 `liptv` 低权限用户；
- app/release/venv = root:root；
- etc/config = root:liptv；
- lib/cache/run = liptv:liptv；
- systemd unit = root:root；
- 不覆盖已有 config / DB；
- schema 保持 V1；
- 不使用 `--force` 绕过异常。

安装后立即独立核验真实 `stat` / `systemctl cat`，不能只相信部署脚本输出。

---

## 4. 生产配置

生产配置必须由真实部署者显式填写。

### 4.1 第一阶段：最小固定频道 smoke

不要一上来接全部真实源。

先只启用：

- 1 个明确允许访问的 fixed_m3u；
- 1–3 个已人工绑定的 canonical；
- probe.enabled = true；
- dynamic_event_m3u = false；
- server.host = 127.0.0.1。

证明固定链路真实可跑：

```text
fetch
→ sync
→ real ffprobe
→ selector
→ publish
→ live.m3u
```

### 4.2 动态赛事源

只有固定链路稳定后才允许显式开启。

动态赛事继续遵守既有规则：

- 不落 fixed inventory；
- 不进 probe_result；
- 每轮重新抓；
- fail-closed；
- 不缓存旧签名；
- 不把真实签名 URL 写进报告/Git。

---

## 5. systemd 实机验收

真实 systemd 必须逐项验证：

```bash
systemctl daemon-reload
systemctl enable li-iptv
systemctl start li-iptv
systemctl status li-iptv
systemctl show li-iptv ...
journalctl -u li-iptv
```

最低要求：

- `User=liptv`；
- `Group=liptv`；
- 非 root；
- `Restart=on-failure`；
- `RestartPreventExitStatus=3`；
- `ProtectSystem=strict` 实际不阻断写 data/cache/run；
- 服务能正常 SIGTERM 停止；
- 停止后 lock 正常释放；
- restart 后正常恢复；
- 开机 enable 状态正确。

必须实际执行一次：

```bash
sudo systemctl restart li-iptv
```

并验证重启前后 DB/live.m3u 不损坏。

---

## 6. 真实 ffprobe smoke

必须证明 TASK-005 在真实 Linux 上可工作。

要求：

- `python -m liptv probe-check` PASS；
- 至少 1 条固定流真实 probe；
- 成功时记录脱敏媒体摘要；
- 失败时记录 error_type；
- 不粘完整 URL/query；
- 不录制；
- 不长期播放；
- `probe_result` 确实新增；
- selector 能读取本轮真实历史。

如果所有已授权流都不可播：

- 不伪造成功；
- 记录真实 failure；
- 仍可验收 ffprobe 调用链，但 `/live.m3u` 业务健康不能因此宣称 OK。

---

## 7. `/healthz` 与 `/live.m3u` 实机验收

本机至少执行：

```bash
curl -fsS http://127.0.0.1:<port>/healthz
curl -fsS -D - http://127.0.0.1:<port>/live.m3u -o /tmp/live.m3u
```

验证：

- `/healthz` HTTP 200；
- JSON status = ok；
- playlist exists = true；
- last_success_publish_at 合理；
- `/live.m3u` HTTP 200；
- Content-Type 正确；
- Content-Length > 0；
- M3U 可重新解析；
- 不返回 DB/summary/previous；
- `/../` 等路径仍 404。

报告不得粘 playlist 完整正文或真实 stream token。

---

## 8. 重启与故障恢复

必须做真实故障演练，但只允许对 IPTV 自己。

### 8.1 service restart

- `systemctl restart li-iptv`；
- 重新 healthy；
- DB/live.m3u checksum 合理；
- 无第二实例；
- 无残留 lock。

### 8.2 kill/restart

可以对 `li-iptv.service` 做一次受控异常退出测试，验证 systemd `Restart=on-failure`。

禁止对 EV-Lab 或系统关键服务做故障注入。

### 8.3 EXIT_LOCKED

模拟第二个 liptv scheduler 尝试启动：

- 第二实例 exit 3；
- systemd 不形成 tight restart loop；
- 原实例不受影响。

---

## 9. 升级 / 回滚真实 smoke

不能只验证首次安装。

至少做一次不改变业务逻辑的 release smoke：

1. 当前 release A 健康；
2. 构造 release B（可只改版本标记/文档，不改业务行为）；
3. `deploy upgrade`；
4. 验证 B 成为 current；
5. `/healthz` / `/live.m3u` 仍正常；
6. DB/live.m3u 保留；
7. 显式 rollback 回 A；
8. 再次健康。

禁止为了测试制造会影响其它项目的系统级故障。

---

## 10. 网络暴露

本轮不要求裸公网。

推荐优先级保持 TASK-006：

1. Tailscale / WireGuard 私网；
2. Caddy + TLS + access control；
3. 只有 Owner 明确要求才开放公网。

验收至少需要一种**实际客户端可访问**方式，但必须经过 Owner 决策。

如果本轮没有 Owner 的网络暴露授权：

- localhost 健康可通过；
- 外部访问标记 NOT EXECUTED；
- 不得擅自改安全组、防火墙、DNS 或证书。

---

## 11. 现有服务零伤害证明

如果部署在已有 EV-Lab 的主机上，必须给出部署前后对比：

- `evlab-task0006.service` active 状态；
- EV-Lab PID/启动时间（允许变化仅当系统自身发生，与本任务无关时需说明）；
- EV-Lab 数据目录未改；
- EV-Lab unit checksum 未改；
- EV-Lab 端口未变；
- IPTV 使用独立用户/目录/port/unit。

任何证据显示 TASK-007 导致 EV-Lab 中断，TASK-007 自动 FAIL。

---

## 12. 最低验收

1. Phase A 只读 preflight 完整，0 主机变更。
2. 没有 Owner 明确授权具体主机时，Phase B 不得执行。
3. 获授权后，install plan 与 install 均成功。
4. 真实文件 ownership/mode 与 TASK-006 冻结矩阵一致。
5. 真实 systemd unit 启动、停止、restart、enable 均符合预期。
6. `doctor` 在真实配置下 PASS（允许 backup count warn）；probe enabled 时真实 ffprobe PASS。
7. 【2026-10-04 Owner 授权大G代决策】真实 fixed-stream smoke **顺延到后续任务**：当前没有明确合规/授权的 fixed_m3u，不允许拿 dynamic_event_m3u 冒充；TASK-007 不以此阻断验收。
8. TASK-007 本轮只要求真实 Linux 部署链路、空库存 fail-closed、systemd/restart/lock/backup/upgrade/rollback 与 EV-Lab 零伤害成立；fixed selector → live.m3u 的真实业务 smoke 后续单独验收。
9. 空库存时 `/healthz` HTTP 200 + `status=missing`、`/live.m3u` 503 视为**符合设计**；不得为追求 `ok/200` 人工制造假 fixed 数据。
10. service restart 后恢复 healthy，DB/live.m3u 不损坏。
11. 第二实例被锁拒绝，exit 3，不形成 systemd restart storm。
12. 真实 upgrade + rollback smoke 成功，DB/live.m3u 保留。
13. 生产日志无完整 stream token / Authorization / Cookie。
14. 如果与 EV-Lab 共机，部署前后 EV-Lab 零变化、零中断。
15. 外部网络访问仅在 Owner 明确授权后执行；否则标 NOT EXECUTED。
16. Git repo 中不出现生产 config、真实 source URL、token、SSH key、云密钥。
17. 执行报告包含命令、退出码、时间、systemd 状态、health 摘要、checksum，但不包含敏感 URL。
18. 完成后 TASK 状态只到 REVIEW，禁止启动 TASK-008。

---

## 13. Stop / Gate

以下任一情况立即停止，不自作主张：

- Owner 未授权具体真实主机；
- 需要修改 EV-Lab；
- 需要停/restart 非 IPTV 服务；
- 需要修改安全组/防火墙/DNS/TLS 但未授权；
- 发现端口/目录/unit 冲突；
- 安装 ffmpeg 会触发大规模系统升级/包替换；
- doctor 报 schema/guard/权限问题；
- install plan 会覆盖已有未知文件；
- 真实 source 合规/授权状态不明确；
- 需要把私密 URL/token 提交 Git；
- 需要视频代理/转码才能继续。

遇到 Gate：记录证据，停在 REVIEW / BLOCKED，不得绕过。

---

## 14. 执行报告

完成后填写 `REPORTS/TASK-007-REPORT.md`，至少分成：

- Phase A 只读主机侦察；
- Owner 授权证据/范围；
- Phase B 真实变更清单；
- systemd；
- ownership；
- doctor；
- ffprobe；
- fixed stream smoke；
- `/healthz` / `/live.m3u`；
- restart / EXIT_LOCKED；
- upgrade / rollback；
- EV-Lab 零伤害证明；
- 网络暴露状态；
- 敏感信息检查；
- Git/Gate。

完成后 commit + push main，TASK 状态改为 REVIEW；停止，等待大G独立实机 QA。
