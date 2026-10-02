# TASK-006 — Single-Host Linux Deployment & Production Operations

状态：READY_FOR_EXECUTOR
Owner：老李
Architect / Reviewer：大G
Executor：小W
基线：TASK-001 / 002 / 003 / 004 / 005 均已独立 ACCEPT；开工前拉取最新 main。
参考：README.md、V1_RUNTIME_FLOW.md、REVIEWS/TASK-005-REVIEW-02.md。
本轮完成后只允许进入 REVIEW；禁止自行启动 TASK-007。

## 目标

把目前已经验收的本地完整链路正式固化为**单台 Linux 主机上的长期运行服务**：

```
system boot
→ systemd
→ python -m liptv run --serve
→ fixed fetch
→ stream-sync
→ real ffprobe
→ selector
→ publish
→ /live.m3u + /healthz
```

本轮解决的是部署、进程托管、目录/权限、升级与回滚、持久化、日志和可验证运维。
不重新设计业务算法，不引入多节点，不做视频代理。

---

## 1. 目标运行环境

冻结首个生产目标：

- Linux x86_64；
- Python 3.11+；
- systemd；
- ffprobe/ffmpeg 由操作系统包管理器安装；
- SQLite；
- 单主机单实例；
- 本项目代码只跑一个长期 scheduler 实例；
- 播放器只访问本项目发布的 M3U，媒体流仍直连上游。

优先支持 Ubuntu 24.04 LTS / Debian 12 一类常见 systemd 主机。

本轮不绑定腾讯云/AWS/阿里云特定 API。只要是满足上述条件的 Linux VM 都可部署。
如果最后选择腾讯云，只写 provider-neutral 的主机前置条件和实际部署记录，不把腾讯云 SDK/凭据写进代码。

---

## 2. 目录与权限模型

生产环境不得直接在 Git 工作树里写数据库和 runtime 文件。

建议冻结：

```text
/opt/li-iptv-aggregator/          # 只读应用代码/venv
/etc/li-iptv-aggregator/          # 配置
/var/lib/li-iptv-aggregator/      # SQLite / 持久状态
/var/cache/li-iptv-aggregator/    # 可丢弃缓存（如需要）
/var/log/li-iptv-aggregator/      # 若不用 journald 才使用
/run/li-iptv-aggregator/          # 短生命周期 pid/lock（若适合）
```

要求：

- 新建低权限系统用户，例如 `liptv`；
- 服务不得以 root 身份长期运行；
- app 目录原则上不可写；
- SQLite、live.m3u、runtime-status、lock、publish summary 必须位于明确可写目录；
- 配置文件权限最小化；
- 任何 token/凭据不得提交 Git；
- 生产路径必须与现有 Git 输出护栏兼容，不得为了部署绕过 guard。

如果现有 guard 过度绑定“必须被 .gitignore 忽略”，导致 `/var/lib/...` 这类 Git 工作树外路径无法正确使用，应做**最小兼容修复**：Git 工作树外的明确 runtime 路径允许写；工作树内继续保持原三重护栏。

---

## 3. 安装/部署脚本

新增可审计、幂等的部署入口，例如：

```bash
sudo tools/deploy_linux.sh install
sudo tools/deploy_linux.sh upgrade
sudo tools/deploy_linux.sh status
sudo tools/deploy_linux.sh rollback
```

也可以采用 `Makefile + install script`，但必须满足：

- 幂等；
- 不静默覆盖现有数据库；
- 不静默覆盖已有生产配置；
- 安装前检查 Python、ffprobe、systemd；
- 创建 venv 并安装本项目；
- 不依赖开发机路径；
- 不把 Git credential/token 带进目标机；
- 安装/升级失败要明确退出码；
- 支持 dry-run 或 plan 模式，至少能看到将修改哪些路径。

本轮禁止 `curl | bash` 式不可审计安装。

---

## 4. systemd 服务

新增模板或安装产物，例如：

```text
deploy/systemd/li-iptv.service
```

服务至少要求：

- `User=liptv` / `Group=liptv`；
- `WorkingDirectory` 指向只读应用目录；
- `ExecStart=<venv>/python -m liptv run --serve --config /etc/.../config.toml`；
- `Restart=on-failure`；
- 合理的 `RestartSec`；
- 正常 SIGTERM 能触发 TASK-004 已验收的干净停止；
- `TimeoutStopSec` 大于一次正常 ffprobe 清理窗口；
- 开机自启；
- 不允许两个 service instance 同时指向同一 data/output；
- journald 能看到脱敏日志；
- 服务退出码 3（已有实例持锁）不得形成无限高速重启风暴。

可酌情使用 systemd hardening：

- `NoNewPrivileges=true`；
- `PrivateTmp=true`；
- `ProtectSystem=strict` 或适合本项目的最小写白名单；
- `ReadWritePaths=` 只开放数据目录；
- 不得启用会破坏 ffprobe 网络请求或 SQLite 写入的配置。

安全选项必须有自动/离线验证，不能“抄一堆 hardening 参数然后祈祷能跑”。

---

## 5. 生产配置与环境检查

新增生产配置模板：

```text
deploy/config.production.example.toml
```

至少显式设置：

- DB path；
- output/live.m3u；
- publish summary；
- runtime status；
- lock path；
- server host/port；
- scheduler interval；
- stale_after；
- probe enabled/name/location/ffprobe_path；
- max_concurrency / timeout；
- fixed/dynamic source enabled 状态。

要求：

- 所有路径用绝对路径；
- `probe.enabled` 在生产模板中仍默认 false，只有部署者明确启用后才跑真实流；
- dynamic_event_m3u 同样默认 false；
- 不得把真实私密 URL/token 写进 example 配置。

新增生产 preflight，例如：

```bash
python -m liptv doctor --config /etc/li-iptv-aggregator/config.toml
```

或等价命令，至少检查：

- 配置可解析；
- DB 目录可写；
- output/status/lock 目录可写；
- ffprobe 可执行（若 probe enabled）；
- 端口可绑定；
- 当前无冲突实例；
- schema 可读取且版本匹配；
- 不真正 fetch/publish；
- 不请求媒体流。

doctor 必须只诊断，不改变数据库业务状态。

---

## 6. 首次启动与数据库保护

首次部署：

- 没有 DB 时允许显式 init；
- 已有 DB 时绝不重建；
- schema 仍是 V1，不升级；
- 服务启动不得自动清空或迁移未知 schema；
- 部署脚本要在升级前备份 SQLite。

备份要求：

- 使用 SQLite 安全备份方式（`.backup` API / sqlite backup API / 在服务停止或一致性策略下复制）；
- 备份文件带时间戳或 release id；
- 保留最近 N 份（配置化或简单固定，如 5）；
- 不把备份提交 Git；
- rollback 可以恢复代码版本，但**数据库恢复必须是显式动作**，不能自动覆盖新数据。

---

## 7. 固定订阅 URL 与网络暴露边界

本轮需要让部署后的播放器有一个稳定订阅地址，但仍遵守“个人自用”。

默认模式：

```
server.host = 127.0.0.1
```

然后由外层受控网络方式暴露，推荐顺序：

1. 私有 VPN / Tailscale / WireGuard；或
2. 反向代理 + TLS + 访问控制；
3. 明确确认风险后才直接绑定公网。

本项目自身仍只提供只读 HTTP，不实现视频代理。

若本轮提供 reverse proxy 示例，必须满足：

- `/live.m3u` 和 `/healthz` 只反代本项目；
- 禁止目录浏览；
- 不暴露 DB、previous、summary、logs；
- TLS 终止在反代层；
- 访问日志不得记录 M3U 内部 stream URL；
- 不在仓库提交证书私钥。

允许提供 Caddy 或 nginx **一种**最小参考配置，不要同时维护多套生产栈。

本轮不要求自动申请域名/证书；如果没有域名，部署可先在私网 IP/隧道验证。

---

## 8. 健康检查与 systemd 行为

部署后必须有机器可判定的健康检查：

```bash
curl -fsS http://127.0.0.1:<port>/healthz
curl -fsS http://127.0.0.1:<port>/live.m3u
```

要求：

- systemd `active` 不等于业务健康；验收必须看 `/healthz`；
- `missing/stale` 不能被部署脚本伪装成 ok；
- `/live.m3u` 503 时 health checker 必须如实失败/降级；
- last-known-good 文件仍可按既有语义保留；
- probe 环境故障必须保留 TASK-005 的 degraded 状态，不因 systemd 存活被掩盖。

可选提供 `tools/healthcheck.py`，只访问 localhost。

---

## 9. 日志与隐私

生产日志优先 journald。

要求：

- 不能打印完整 stream query/token；
- 不能打印 Cookie / Authorization；
- systemd unit 不把敏感配置展开进命令行；
- 日志要能看到 round_id、fetch/probe/publish 摘要、退出原因；
- 可通过 `journalctl -u li-iptv` 定位最近失败；
- 不新增无限增长的自定义日志文件；
- 若使用 journald，不再重复写 `/var/log` 文本日志。

---

## 10. 升级与回滚

升级流程必须冻结并可验证：

```text
preflight
→ backup DB/config metadata
→ stop service
→ install new release
→ start service
→ wait for health
→ success
```

失败：

```text
health failed
→ stop
→ rollback application release
→ restart old release
→ verify /healthz
```

要求：

- release 用明确目录或 Git commit SHA，不原地覆盖到无法回退；
- rollback 默认只回退代码/venv，不回退 DB；
- schema V1 当前不涉及 migration，但流程必须为未来 schema migration 留出 Gate；
- 任何升级失败都不能删除现有 live.m3u / SQLite。

---

## 11. 部署验收必须可离线模拟

自动测试不能要求真的有腾讯云主机。

新增：

- systemd unit 静态测试；
- deploy script 的临时 root/prefix 模式，例如 `DESTDIR` 或 `--root <tmp>`；
- fake systemctl / fake ffprobe；
- 临时目录模拟 `/opt` `/etc` `/var/lib`；
- install → upgrade → rollback 的离线测试。

不能让测试修改开发机真实 `/etc/systemd/system`。

---

## 12. 最低验收场景

1. TASK-005 当前 **305 项**测试零回归。
2. install 到临时 root：目录、权限意图、venv/config/unit 产物正确；重复 install 幂等。
3. 已存在 DB/config 时重复 install 不覆盖。
4. production config 全绝对路径，example 无 token/真实私密 URL。
5. doctor：正常环境 PASS；缺 ffprobe（probe enabled）FAIL；probe disabled 时不要求 ffprobe；不访问 stream。
6. systemd unit 以非 root 用户运行，SIGTERM 能正常停；Restart 策略不会对 EXIT_LOCKED=3 形成 tight loop。
7. 服务真实启动（测试环境可用本地临时 prefix）后 `/healthz` 和 `/live.m3u` 行为与 TASK-004 完全一致。
8. SQLite backup 使用一致性安全方式，至少验证备份可重新打开、table count/schema 正常。
9. upgrade 成功路径：旧 release → 新 release → health ok；DB/live.m3u 保留。
10. upgrade 健康失败：application rollback 成功；DB/live.m3u 不丢失。
11. journald/unit/doctor/deploy 输出均不泄漏带 query 的 stream URL/token。
12. Git 工作树外 `/var/lib/...` runtime path 正常使用；工作树内未忽略/已跟踪路径仍被现有 guard 拒绝。
13. 不修改 selector 算法、不升级 schema、不新增视频代理。
14. `git diff --check`、执行报告、测试命令/退出码、离线部署 demo、Git SHA、本地/远端一致。

---

## 13. 离线部署演示

新增类似：

```bash
python tools/demo_deploy_linux.py
```

或 shell + Python helper，至少展示：

1. 临时 Linux root/prefix；
2. install；
3. production config；
4. fake systemd start；
5. 服务进程可启动并访问 `/healthz`；
6. SQLite backup；
7. upgrade；
8. 模拟 health failure；
9. rollback；
10. 数据库/live.m3u 仍存在且 checksum 不变；
11. 日志/配置输出无 token。

演示不得访问真实公网 IPTV 源。

---

## 14. 文档

新增：

- `DEPLOYMENT.md`：从一台干净 Linux VM 到可用服务的完整步骤；
- 安装、升级、回滚、看日志、doctor、healthcheck、备份恢复；
- 明确哪些步骤必须由老李提供主机/域名/网络权限；
- 明确本项目不会自动采购云主机、不会保存云厂商密钥。

README 只保留简明入口，细节放 DEPLOYMENT.md。

---

## 不包含

- 多地区 probe；
- 多主机协调/选主；
- Kubernetes；
- Docker（本轮不需要，为减少部署层复杂度）；
- Terraform/云厂商 IaC；
- 云厂商 API 自动买机器/改安全组；
- 自动域名注册；
- 自动证书私钥托管；
- 视频代理/转码/DVR；
- EPG/Logo；
- Dashboard；
- selector 算法变化；
- schema V2。

---

## Stop / Gate

若实际部署需要云厂商凭据、DNS 控制权、主机 root 权限或域名，而当前环境拿不到：

- 不伪造“已部署成功”；
- 完成可离线验证的部署产物、doctor、systemd、升级/回滚流程；
- 在报告明确写出哪一步需要老李一次性介入；
- 停在 REVIEW。

完成后填写 `REPORTS/TASK-006-REPORT.md`，TASK 状态改为 REVIEW，commit + push main 后停止。
只有大G独立 QA 可以 ACCEPT / REJECT；禁止提前启动 TASK-007。
