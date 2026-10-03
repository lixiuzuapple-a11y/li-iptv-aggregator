# TASK-007 Execution Report

状态：**BLOCKED**（Phase A 已完成；等待 Owner 对「具体主机」的部署授权，Phase B 未执行）
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-006 **ACCEPTED**（[REVIEWS/TASK-006-REVIEW-02.md](../REVIEWS/TASK-006-REVIEW-02.md)）
基线 HEAD：`a7e81924cab679d53ee774542be309e228df3ba6`
（`task: define TASK-007 real Linux production smoke deployment`）
Phase A 执行窗口：**2026-10-03 12:16 – 12:21 CST**（UTC 04:16 – 04:21）

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| Phase A 只读侦察 | ✅ **完成** |
| Phase A 主机变更 | ✅ **0**（稳定指纹四连一致；EV-Lab PID / 启动时间 / unit sha 前后一致） |
| 候选主机数量 | **1**（`ev-lab-shanghai`；全地域扫描确认账号下只有这一台） |
| 部署冲突 | ✅ **无用户 / 目录 / 端口 / unit 冲突** |
| Phase A 发现的新约束 | ⚠️ **3 条**（GitHub 从主机不可达、TAT 通道禁用 `python3 -c`、ffprobe 缺失） |
| Owner 部署授权 | ❌ **未获得** ⇒ **Phase B 未执行** |
| TASK 状态 | **BLOCKED**（等 Owner 授权）；**未启动 TASK-008** |

> **一句话**：TASK-006 的离线能力已经就绪，候选主机体检干净、零冲突、可安全共存；
> 但老李**尚未授权「哪台真实主机可以部署 IPTV」**，因此按 Gate 0 停在 Phase A，
> 不进入 Phase B，也不对主机做任何写操作。

---

## 1. Phase A — 只读实机 Preflight（§1）

### 1.1 候选主机清单（全地域扫描）

用 Lighthouse 控制面只读 API `describe_instances` 扫描以下地域：

`ap-shanghai` / `ap-beijing` / `ap-guangzhou` / `ap-hongkong` / `ap-nanjing` /
`ap-tokyo` / `ap-singapore` / `na-siliconvalley` / `eu-frankfurt` / `ap-chengdu`

**结果：账号下只有 1 台实例**（ap-shanghai）；其余 9 个地域全部返回「暂无数据」。

| 项目 | 值 |
|---|---|
| 名称 | `ev-lab-shanghai` |
| InstanceId | `lhins-bukxxz3g` |
| 地域 / 可用区 | ap-shanghai / ap-shanghai-5 |
| 状态 | **RUNNING** |
| 规格 | 2 vCPU / 2 GB（`bundle_starter_mc_med2_02`） |
| 系统盘 | 50 GB `CLOUD_SSD`（`lhdisk-54b7uc86`） |
| 镜像 | `lhbp-1l4ptuvm` |
| 私网地址 | `10.0.0.13`（/22） |
| 公网地址 | 有公网 IPv4（本报告打码为 `124.220.x.x`，理由见 §13.2）；无 IPv6 |
| 出带宽 | 4 Mbps |
| 计费类型 | PREPAID（包月） |
| 到期时间 | **2026-10-24T16:04:12Z**（`NOTIFY_AND_MANUAL_RENEW` ⇒ **需手动续费**） |
| 最近操作 | `ResetInstance` / 2026-09-24T16:18:40Z / SUCCESS |

### 1.2 机器事实（Phase A 原始读数）

| 检查项（§1 要求） | 实测值 |
|---|---|
| distro | **Ubuntu 24.04.4 LTS**（noble，ID=ubuntu，ID_LIKE=debian） |
| kernel | `6.8.0-124-generic #124-Ubuntu SMP PREEMPT_DYNAMIC x86_64` |
| architecture | `x86_64`（CPU op-mode 32/64-bit） |
| CPU | **2 vCPU**，`AMD EPYC 7K62 48-Core Processor`（Virtualization 虚拟化，on-line 0,1） |
| RAM | **1967 MB** total（读时 used 497 / free 216 / buff-cache 1432 / **available 1470**） |
| swap | **1987 MB**（`/swap.img`，读时仅用 268 KB） |
| root fs 剩余 | `/dev/vda2` 50 G，used 6.0 G，**avail 42 G（13%）**；inode 4% |
| Python | **`Python 3.12.3`**（满足 `requires-python >= 3.12`） |
| `ffprobe -version` | ❌ **不存在**（`ffprobe: command not found`） |
| `ffmpeg` 包候选 | `7:6.1.1-3ubuntu5`，来自 `http://mirrors.tencentyun.com/ubuntu noble/universe`（Installed: **none**） |
| systemd 版本 | **255**（`255.4-1ubuntu8.15`） |
| 拟用端口 8080 | ✅ **空闲**（`ss` 匹配数 = 0） |
| 当前监听端口 | TCP：`22`（0.0.0.0 + [::]）、`53`（127.0.0.53%lo、127.0.0.54）；UDP：`68`（eth0 dhcp）、`323`（chrony lo）、`53`（resolved lo） |
| 当前 active services | 23 个，其中含 `evlab-task0006.service`、`tat_agent.service`、`ssh`、`chrony`、`systemd-*` 等（全清单见 §1.3） |
| `liptv` 用户 | ❌ 不存在（`id: 'liptv': no such user`；组亦不存在） |
| `/opt/li-iptv-aggregator` | ❌ 不存在（`/opt` 目录为空） |
| `/etc/li-iptv-aggregator` | ❌ 不存在 |
| `/var/lib/li-iptv-aggregator` | ❌ 不存在 |
| `/var/cache/li-iptv-aggregator` | ❌ 不存在 |
| `/run/li-iptv-aggregator` | ❌ 不存在 |
| 防火墙（主机侧） | `ufw` = **inactive**；`nft` 有 `table ip filter`，`chain YJ-FIREWALL-INPUT` **空**，INPUT policy `accept`；`iptables` policy 全 `ACCEPT`，`YJ-FIREWALL-INPUT` **空** |
| 防火墙（云侧安全组） | 仅两条：`22/TCP 0.0.0.0/0 ACCEPT`、`ICMP ALL 0.0.0.0/0 ACCEPT` ⇒ **8080 未放行** |
| 对外访问条件 | 有公网 IPv4 + 4 Mbps 出带宽；安全组仅放行 SSH/ICMP |
| Caddy / nginx | ❌ 均 **MISSING**，`systemctl is-active` = inactive |
| Tailscale / WireGuard | ❌ 均 **MISSING**（`wg`、`wg-quick` 无），inactive |
| Git → GitHub（只读） | ❌ **不可达**：`curl https://github.com` 两次超时（10 s / 15 s，`http_code=000`）；`git ls-remote` 无输出 |
| Python → 包源（只读） | ✅ `mirrors.tencentyun.com` = **200**；✅ `pypi.org` = **200** |
| 主机已有工具 | `git 2.43.0`、`curl`、`wget`、`tar`、`rsync` 均在 |
| 当前时间 / 时区 | `2026-10-03 12:18:52 CST`；`Asia/Shanghai`（CST, +0800） |
| NTP | ✅ `System clock synchronized: yes`，`NTP service: active`（chrony） |
| uptime / load | up **8 days**（boot 2026-09-25 00:20）；load `0.00, 0.00, 0.00` |
| 其它账号 | `/home` 下有 `ubuntu`、`lighthouse`（Lighthouse 内置账号） |

### 1.3 当前 active running services（23）

```
acpid  chrony  cron  dbus  evlab-task0006  fwupd  getty@tty1  ModemManager
multipathd  networkd-dispatcher  polkit  rsyslog  serial-getty@ttyS0  ssh
systemd-journald  systemd-logind  systemd-networkd  systemd-resolved
systemd-udevd  tat_agent  udisks2  unattended-upgrades  upower
```

`/etc/systemd/system` 下与 EV-Lab 相关的 unit：

| 文件 | 大小 | mtime | sha256（前 16） |
|---|---|---|---|
| `evlab-task0006.service` | 2287 | Sep 28 18:30 | `e9328102f9798472` |
| `evlab-task0005.service` | 940 | Sep 28 12:27 | （备用，未运行） |
| `evlab-task0006-health.service` | 600 | Sep 29 09:12 | `09537f4f9da51b01` |
| `evlab-task0006-health.timer` | 231 | Sep 29 09:12 | `71e6feb213590912` |

### 1.4 Phase A 期间「0 主机变更」证明

**证明方式（可复现）**：定义稳定指纹 `S2`，覆盖所有「一旦被写就会变」的状态量：

```
liptv 用户是否存在 · 6 个目标路径的 ls -ld · li-iptv.service 的 is-enabled/is-active ·
TCP LISTEN 集合 · running service 名称集合 · EV-Lab unit sha256 · EV-Lab MainPID/启动时间/状态/属主
```

结果（同一函数、连续取样）：

```
A1 = 947d00dbebd6737305c5f40dcd7160b6
A2 = 947d00dbebd6737305c5f40dcd7160b6
A3 = 947d00dbebd6737305c5f40dcd7160b6
F_FINAL = 947d00dbebd6737305c5f40dcd7160b6   ← Phase A 结束时复取
```

**四连完全一致**。另有逐项正向核对：`liptv` 用户不存在、6 个目标路径全部 `No such file or directory`、
`li-iptv.service` 不存在且 `is-enabled/is-active` 均为 unknown、8080 仍空闲。

> **如实披露一处口径修正**：Phase A 一开始我用的是「更宽」的指纹（额外把 **UDP** 监听套接字
> 也算进去），得到 `F1=bfaa5a76…`，收尾时得到 `F2=18d4e342…`，**两者不同**。
> 定位结论：差异**只来自 UDP**——`systemd-resolved` / `chrony` 会为每次查询临时开/关
> `0.0.0.0:*` 的 UNCONN 套接字，而这些查询**正是我们自己的只读 DNS 反查触发的**。
> 换成只含「TCP LISTEN + 服务名集合 + 目标路径 + unit 状态」的稳定口径后，四连一致。
> **没有把它藏起来**：这是口径选择问题，不是主机发生变更；TCP 监听集合全程只有
> `22 / 53(lo) / 53(lo)`，从未变化。

**EV-Lab 零变化（Phase A 前后逐项对照）**：

| 项 | Phase A 开始 | Phase A 结束 | 变化 |
|---|---|---|---|
| ActiveState / SubState | active / running | active / running | 无 |
| MainPID | `1264381` | `1264381` | 无 |
| ActiveEnterTimestamp | Mon 2026-09-28 18:31:45 CST | Mon 2026-09-28 18:31:45 CST | 无 |
| NRestarts | 0 | 0 | 无 |
| User / Group | ubuntu / ubuntu | ubuntu / ubuntu | 无 |
| unit sha256 | `e9328102f97984726812ee56fd80da81e5ea5e620e832417adde845a86b28eb4` | 同左 | 无 |
| 数据目录 | `/home/ubuntu/evlab-data` 290 M | 同左 | 无 |

**唯一被写过的路径**：`/usr/local/qcloud/tat_agent/tmp/invt-*.sh` —— 这是 TAT 通道
**自身**投递命令用的私有临时脚本（属 TAT 工作机制，非系统变更，且位于 TAT 私有目录内）。
清单里没有任何 app / config / data / unit / 用户 / 端口被创建或改动。

### 1.5 部署冲突清单（§1 要求）

| 维度 | 现状 | 判定 |
|---|---|---|
| **用户冲突** | 现有 `ubuntu`、`lighthouse`；`liptv` 不存在 | ✅ 无冲突（新建 `liptv` 即可） |
| **目录冲突** | `/opt` 空；`/etc`、`/var/lib`、`/var/cache`、`/run` 下均无 `li-*-iptv*` | ✅ 无冲突 |
| **端口冲突** | 仅 `22` / `53(lo)` / dhcp / chrony 在用；**8080 空闲** | ✅ 无冲突（且只绑 `127.0.0.1`） |
| **systemd unit 冲突** | 现有 `evlab-task0005/0006` + health.timer；`li-iptv.service` 不存在 | ✅ 无冲突（unit 名不重叠） |
| **CPU 竞争** | 2 vCPU；EV-Lab 4 天累计 CPU 仅 3 min 38 s；load `0.00` | ✅ 无实质竞争 |
| **内存竞争** | 1967 MB；EV-Lab RSS ≈ 307 MB（peak 310 MB），可用 1470 MB | ⚠️ 够用，但**2 G 共享** ⇒ `probe.max_concurrency` 压到 **2–4**（TASK-006 已定） |
| **磁盘空间** | 42 G 可用；EV-Lab 占 290 M | ✅ 充足 |
| **网络 / 防火墙** | 主机侧无防火墙；云安全组仅放行 22/TCP + ICMP，**8080 未放行** | ✅ 与「默认只绑 localhost」方案天然一致 |

---

## 2. Owner 授权 Gate（§0 / §2）—— **BLOCKED**

### 2.1 授权状态：**未获得**

任务书 §0.2 明确：

> 这台主机**不是自动批准的部署目标**。如果最终使用它，必须由老李明确说出
> 「**允许在 ev-lab-shanghai 上部署 IPTV**」或等价授权后才可进入 Phase B。

本轮到来的是**任务书下达**本身，而非**主机授权**。老李在本次指令中反而再次强调了 Gate：

> 「在老李没有明确授权『具体哪台真实主机可以部署 IPTV』之前，只允许只读检查……
> 如果候选是 `ev-lab-shanghai`，它当前承载 EV-Lab，**不能自动视为已授权部署目标**。」

因此：**`ev-lab-shanghai` 未被授权为部署目标**，Phase B **不执行**。

### 2.2 需要的授权语句（照抄任务书）

> 「允许在 `ev-lab-shanghai`（`lhins-bukxxz3g`）上部署 IPTV。」

（或等价表述，须**指明具体主机**，不能是「用云服务器部署吧」这类泛指。）

### 2.3 授权后 Phase B 的执行前置条件（已备好，等授权）

1. **装 `ffprobe`**：`sudo apt-get install -y ffmpeg`（候选 `7:6.1.1-3ubuntu5`，来自内网镜像；
   只新增包，**不做全系统升级、不换系统 Python**）。
2. **代码投递**：⚠️ 主机**连不上 GitHub**（§1.2），因此**不能**在主机上 `git clone`。
   改用 TASK-006 已验收的 `--method copy`：在本机打包 release 目录 → 经 SCP/rsync（或 TAT 文件通道）
   投递到 `/opt/li-iptv-aggregator/releases/<release-id>/`。
3. **命令编排**：⚠️ TAT 通道**拒绝 `python3 -c`**（§3.1），全部操作必须走
   `python3 -m liptv <cmd>` 或上传 `.sh` 后执行。
4. **生产配置**：`[[sources]]` 的真实 URL 由部署者显式填写，**不进 Git**。
5. **网络暴露**：默认只绑 `127.0.0.1:8080`；若要外部访问，需老李**另行**决定方式
   （Tailscale/WireGuard 私网 > 反代 + TLS + 访问控制 > 公网）并授权改安全组。

---

## 3. Phase B — 真实安装（§2 / §3）

**NOT EXECUTED（BLOCKED：无 Owner 主机授权）。**

未执行项：`apt install ffmpeg`、`useradd/groupadd liptv`、创建 `/opt` `/etc` `/var/lib` 生产目录、
写 `li-iptv.service`、`systemctl enable/start`、`tools/deploy_linux.sh plan|install`、写入生产配置。

### 3.1 Phase A 发现的 Phase B 约束（**新增情报，重要**）

| # | 约束 | 证据 | 对 Phase B 的影响 |
|---|---|---|---|
| 1 | **主机无法访问 GitHub** | `curl https://github.com` 超时 10 s 与 15 s（`code=000`）；`git ls-remote` 无输出；DNS 可解析到 `20.205.243.166` | 代码**不能**靠主机 `git clone`；必须用 `--method copy` + 上传 release；或改用内网可达的包/镜像通道 |
| 2 | **TAT 通道禁用 `python3 -c`** | 同一命令含 `python3 -c '...'` ⇒ `AccessDeny`；去掉该片段后同一命令正常 | 所有远程编排必须用 `python3 -m liptv ...`（TASK-006 的 CLI 正好是这个形态）或上传 `.sh`；**不能用内联 Python** 做一次性探针 |
| 3 | **`ffprobe` 缺失** | `command -v ffprobe` 空 | 必须在授权后 `apt install ffmpeg`；在此之前 `[probe] enabled` 只能保持 `false` |

> 约束 2 也解释了为什么 §1 里读不到 `sys.executable` 与 `sqlite3.sqlite_version`
> —— 这两项原计划用 `python3 -c` 读取，被通道策略拦下，**如实留白，未编造**。

---

## 4. 部署前快照（§2.2）

**NOT EXECUTED** —— 快照属写操作前的步骤，随 Phase B 一并阻塞。
不过 §1.2 / §1.3 / §1.4 已经**免费**拿到了等价的「部署前基线」：
systemd active services、监听端口、EV-Lab unit sha256 + PID + 启动时间、磁盘占用、
目标目录现状、目标 unit 现状。**它们可直接作为将来的「部署前快照」使用**，
且已带 2026-10-03 12:16–12:21 CST 的时间戳。

---

## 5. 权限矩阵（§3）

**NOT EXECUTED**（无部署 ⇒ 无真实 `stat`）。
目标矩阵取自 TASK-006 冻结设计（`Layout.mode_table()`，大G已在 REVIEW-02 独立打印核对）：

```
/opt/li-iptv-aggregator                    root:root   0755
/opt/li-iptv-aggregator/releases           root:root   0755
/opt/li-iptv-aggregator/releases/<id>      root:root   0755
/opt/li-iptv-aggregator/venv               root:root   0755
/opt/li-iptv-aggregator/current            root:root   0644
/opt/li-iptv-aggregator/deploy-state.json  root:root   0640
/etc/li-iptv-aggregator                    root:liptv  0750
/etc/li-iptv-aggregator/config.toml        root:liptv  0640
/var/lib/li-iptv-aggregator                liptv:liptv 0750
/var/lib/li-iptv-aggregator/liptv.sqlite3  liptv:liptv 0640
/var/lib/li-iptv-aggregator/backups        liptv:liptv 0700
/var/cache/li-iptv-aggregator              liptv:liptv 0750
/run/li-iptv-aggregator                    liptv:liptv 0750
/etc/systemd/system/li-iptv.service        root:root   0644
```

---

## 6. systemd 实机验收（§5）

**NOT EXECUTED。** 现有 systemd 状态**未被触碰**（`evlab-task0006` 及其 health.timer 全无变化）。

---

## 7. doctor（§5 / §6）

**NOT EXECUTED**（无生产配置、无生产目录，`doctor` 无对象可检）。

---

## 8. ffprobe / fixed stream smoke（§6）

**NOT EXECUTED。** 原因：`ffprobe` 未安装 + 无 Owner 授权 ⇒ 无授权 fixed 源、无 `probe_result` 新增。

---

## 9. `/healthz` / `/live.m3u` 实机验收（§7）

**NOT EXECUTED。** 8080 空闲，但**没有任何服务在跑**；不伪造健康结论。

---

## 10. restart / EXIT_LOCKED（§8）

**NOT EXECUTED。**

---

## 11. upgrade / rollback 真实 smoke（§9）

**NOT EXECUTED。**

> 说明：A→B→A 的真实路径已在 **TASK-006 离线端到端 demo** 中验证过
> （真起子进程、注入坏 release → 健康失败 → 自动回滚、数据逐字节不变，86/86 断言）。
> 但这**不能替代**实机 smoke，本轮不声称实机通过。

---

## 12. EV-Lab 零伤害证明（§11）

**Phase A 部分：✅ 已证明零变化**（§1.4 对照表：PID / 启动时间 / NRestarts / 属主 / unit sha256 / 数据目录全等）。
**Phase B 部分：N/A**（未部署，故无「部署后」可比）。
**EV-Lab 未出现任何中断**（`ActiveState=active`、`NRestarts=0`、`ActiveEnterTimestamp` 仍是 2026-09-28 18:31:45 CST）。

---

## 13. 网络暴露与敏感信息（§10 / §13）

### 13.1 网络暴露状态

| 方式 | 状态 |
|---|---|
| localhost（`127.0.0.1:8080`） | **NOT EXECUTED**（服务未部署） |
| Tailscale / WireGuard 私网 | **NOT EXECUTED**（主机上两者均未安装） |
| 反代 + TLS（Caddy / nginx） | **NOT EXECUTED**（两者均未安装） |
| 公网直连 | **NOT EXECUTED，且未授权** —— 安全组**未**放行 8080，本轮**未改**安全组 / 防火墙 / DNS / 证书 |

### 13.2 敏感信息检查

- 本报告与本次提交**不含**：生产 config、真实 IPTV source URL、stream token、SSH 私钥、云厂商密钥。
- 本次 Phase A **完全没有产生任何真实 IPTV 源数据**（未抓取、未探测任何频道）。
- **⚠️ 一处主动脱敏并需要大G确认**：本仓库经 GitHub API 查证为 **`private: false`（公开仓库）**。
  因此我**没有**把该主机的**公网 IPv4** 写进本报告（写成 `124.220.x.x`），
  以免在公开仓库里出现「可路由的公网地址 + 该地址 22/TCP 对 `0.0.0.0/0` 开放」的组合。
  私网 `10.0.0.13` 与 InstanceId `lhins-bukxxz3g` 保留（前者属 RFC1918，后者已在
  TASK-006-REPORT.md §11 与 TASK-007.md §0.2 中公开）。
  若大G认为验收需要精确 IP，请在私密渠道索取，或明确指示「可在报告中完整记录」。

---

## 14. 风险与未执行项（§14）

**NOT EXECUTED 汇总**：§3 Phase B 安装、§4 部署前快照、§5 权限 stat、§6 systemd、
§7 doctor、§8 ffprobe/fixed smoke、§9 `/healthz`+`/live.m3u`、§10 restart/EXIT_LOCKED、
§11 upgrade/rollback、§13 全部外部网络暴露。

**BLOCKED 原因（唯一）**：**无 Owner 对具体主机的部署授权**。

**待观察风险**：

1. **内存**：2 GB 与 EV-Lab 共享（EV-Lab RSS ≈ 307 MB）。TASK-006 已定
   `probe.max_concurrency` 压到 2–4；实机 smoke 时应实测峰值 RSS。
2. **GitHub 不可达**（§3.1-1）：升级/回滚流程依赖的「取得新 release」步骤需要一个
   实际可用的投递通道（本地打包 + SCP/rsync）。这一点**必须在 Phase B 开始前敲定**，
   否则 `upgrade` 会卡在拿不到代码。
3. **到期提醒**：该实例 **2026-10-24** 到期且需**手动续费**，与部署进度无关，
   但老李需要记得续。

---

## 15. Git & Gate

- 基线 HEAD：`a7e81924cab679d53ee774542be309e228df3ba6`
- 本轮改动：仅 `REPORTS/TASK-007-REPORT.md`（本文件）+ `TASKS/TASK-007.md`（状态行）
- 未改动任何代码：`liptv/**`、`tools/**`、`tests/**`、`deploy/**`、`schema/**` **零改动**
  （TASK-006 交付物冻结，本轮未碰）
- TASK 状态：**BLOCKED**（Phase A 完成 / Phase B 无授权）
- **停 Gate**：不启动 TASK-008，等老李给出主机授权；授权后再按 §2.3 进入 Phase B。
- SHA / `git diff --check` / 远端一致性：见 §15.1（本仓库惯例：先提交 → push →
  用一次记录提交回填 SHA，**不 amend**）。

### 15.1 提交与独立核验

- **本报告提交 SHA：`ff4a31bdc67b8ff90a5e7fcc22ac129d14c9dfc4`**
  （`docs: TASK-007 Phase A read-only preflight (BLOCKED awaiting owner authorization)`，
  署名 `lixiuzu <lixiuzuapple@gmail.com>`）
- 变更：2 个文件，**+346 / −32** —— 仅 `REPORTS/TASK-007-REPORT.md`（+345 / −31，模板 → 正式报告）
  与 `TASKS/TASK-007.md`（+1 / −1，状态行 `READY_FOR_EXECUTOR` → `BLOCKED`）
- `git diff --check`：exit **0**；字符卫生自检：**0** 处问题；暂存 blob：**CR = 0（全 LF）**
- **推送与独立核验**：
  - `git push origin main` → `a7e8192..ff4a31b  main -> main`（exit 0）
  - **云端连接器（GitHub API）独立核验**：远端 `main` 的 HEAD = `ff4a31bdc67b8ff90a5e7fcc22ac129d14c9dfc4`，
    文件清单 = `REPORTS/TASK-007-REPORT.md`(+345/−31) + `TASKS/TASK-007.md`(+1/−1)，
    合计 **+346 / −32** —— 与本地提交**逐字段一致**，且**不含任何代码 / 冻结模块**
- 冻结边界：`liptv/**`、`tools/**`、`tests/**`、`deploy/**`、`schema/**` 本轮**零改动**
  （TASK-006 交付物保持原状，未因本任务产生任何代码变更）
