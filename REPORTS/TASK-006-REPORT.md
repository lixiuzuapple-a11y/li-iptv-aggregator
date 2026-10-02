# TASK-006 Execution Report

状态：REVIEW
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-005 ACCEPTED（[REVIEWS/TASK-005-REVIEW-02.md](../REVIEWS/TASK-005-REVIEW-02.md)）
基线 HEAD：`466d412f7ffef50b841b704cba91ea7b74d4967a`
（`task: define TASK-006 single-host Linux production deployment`）

---

## 1. 基线与变更

### 1.1 变更规模

`git diff --stat`（相对基线 HEAD）：**17 个文件，+5661 / −2**。

| 文件 | 行数 | 性质 |
|---|---|---|
| `liptv/deploy.py` | 1679 | **新增**：部署内核（布局 / 权限 / release / unit / 服务托管 / 健康闸门 / 升级 / 回滚 / 备份） |
| `liptv/doctor.py` | 339 | **新增**：生产 preflight 体检（9 项检查，只诊断） |
| `liptv/backup.py` | 251 | **新增**：SQLite 在线一致性备份 / 校验 / 显式恢复 |
| `liptv/health.py` | 237 | **新增**：只读健康探针（`/healthz` + `/live.m3u`） |
| `liptv/runtime.py` | +89 | **修改（纯新增）**：只读 `inspect_lock()` |
| `liptv/cli.py` | +223 | **修改（纯新增）**：`doctor` 子命令 + `deploy` 二级子命令 |
| `deploy/systemd/li-iptv.service.in` | 68 | **新增**：systemd unit 模板 |
| `deploy/config.production.example.toml` | 97 | **新增**：生产配置模板 |
| `deploy/reverse-proxy/Caddyfile.example` | 111 | **新增**：§7 唯一一种反代示例 |
| `tools/fake_systemctl.py` | 83 | **新增**：离线假 systemctl（测试接缝） |
| `tools/deploy_linux.sh` | 38 | **新增**：`sudo tools/deploy_linux.sh …` 薄包装 |
| `tools/healthcheck.py` | 101 | **新增**：外部健康检查器（只访问 localhost） |
| `tools/demo_deploy_linux.py` | 718 | **新增**：§13 离线端到端演示 |
| `tests/test_deploy.py` | 771 | **新增**：部署内核离线回归（55 项） |
| `tests/test_doctor.py` | 414 | **新增**：doctor 分支离线回归（25 项） |
| `DEPLOYMENT.md` | 405 | **新增**：运维手册（含需人工介入的步骤） |
| `README.md` | 39 ± | **修改**：新增 TASK-006 简明入口 + 状态行 |

### 1.2 明确未修改（冻结模块）

以下模块的 **blob 与基线 HEAD 逐字节相同**（`git hash-object` vs `git rev-parse HEAD:<path>`）：

| 文件 | blob（前 12） | 结论 |
|---|---|---|
| `liptv/select.py` | `ff1650249e3c` | SAME —— **selector 算法零改动** |
| `liptv/publish.py` | `639522d759f6` | SAME |
| `liptv/server.py` | `28526e378dd8` | SAME —— `/healthz`、`/live.m3u` 路由与 TASK-004 冻结口径零改动 |
| `liptv/m3u.py` | `652d4676778f` | SAME |
| `liptv/ingest.py` | `fec6cd3666ee` | SAME |
| `liptv/fetch.py` | `1ed75e6d7a1a` | SAME |
| `liptv/db.py` | `76a168b78e5f` | SAME —— **`SCHEMA_VERSION = 1` 未动** |
| `liptv/util.py` | `b6b5d6d177ff` | SAME |
| `liptv/probe.py` | `5f325c363abb` | SAME —— TASK-005 测活语义零改动 |
| `liptv/config.py` | `da3a5239543f` | SAME |
| `schema/schema_v1.sql` | `64c8d0ea4f0d` | SAME —— **零 schema 改动** |
| `pyproject.toml` | `fd781593222f` | SAME |
| `.gitignore` | `1d97bbef16d0` | SAME |

> 说明：`liptv/runtime.py` **有改动**，但仅**新增**一个只读函数 `inspect_lock()`（不改任何既有函数、
> 不改锁语义）。TASK-005 冻结的锁原语（`_gate` + `_commit_heartbeat` 的 CAS、stale 判定表、
> `release` 口径、心跳周期）**一个字节都没动** —— 见 §4.4 与 §10.4。

**未做**：视频代理 / 转码 / DVR、多主机选主、Docker / Kubernetes / Terraform、
云厂商 API 调用、selector 算法变更、schema V2、EPG / Logo、Dashboard。

---

## 2. Linux 目录与权限（§2）

目录模型（`liptv/deploy.py::Layout` + `mode_table()`）：

| 路径 | 用途 | 属主 | 权限 |
|---|---|---|---|
| `/opt/li-iptv-aggregator` | 应用（release + venv），**服务只读** | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/releases/<id>` | 每版本独立目录，**不原地覆盖** | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/venv` | 虚拟环境 | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/current` | 文本文件，内容 = 活动 release id | `root:root` | `0644` |
| `/etc/li-iptv-aggregator` | 配置目录 | `root:liptv` | `0750` |
| `/etc/li-iptv-aggregator/config.toml` | 生产配置（**已存在则永不覆盖**） | `root:liptv` | `0640` |
| `/etc/systemd/system/li-iptv.service` | unit | `root:root` | `0644` |
| `/var/lib/li-iptv-aggregator` | 持久数据 | `liptv:liptv` | `0750` |
| `/var/lib/li-iptv-aggregator/liptv.sqlite3` | 数据库 | `liptv:liptv` | `0640` |
| `/var/lib/li-iptv-aggregator/backups` | 备份 | `liptv:liptv` | `0700` |
| `/var/lib/li-iptv-aggregator/live.m3u` | 当前成品 | `liptv:liptv` | `0640` |
| `/var/cache/li-iptv-aggregator` | 可丢弃缓存 / 动态快照 | `liptv:liptv` | `0750` |
| `/run/li-iptv-aggregator` | pid / lock（短生命周期） | `liptv:liptv` | `0750` |

- 服务用户/组 `liptv`（`--create-user` 才创建，需 root）；**绝不以 root 长期运行**。
- 应用目录**对服务不可写** —— 由 unit 的 `ProtectSystem=strict` + `ReadWritePaths` 强制。
- `--root`（DESTDIR 式）用于离线验证；非 POSIX 本机**必须显式给** `--root`，
  否则 `default_root()` 抛 `DeployError`（**防止误写开发机的 `/`**）。
- `--no-chown` 只记录归属意图不执行（离线演练用）。

### 2.1 §2 的「guard 兼容」要求

§2 要求：若既有 guard 过度绑定「必须被 .gitignore 忽略」导致 `/var/lib` 类工作树外路径不可用，应做最小兼容修复。

**实测结论：既有 guard 已满足要求，无需任何改动。**
`publish.guard_runtime_output_path()` 对「不在任何 Git 工作树内」的路径**直接放行**，
只有落在工作树内时才要求「被忽略 + 未被跟踪」。生产路径（`/opt` `/etc` `/var/lib` `/run`）全在工作树外 ⇒ 天然可用。

两个方向都有离线回归（`tests/test_deploy.py::test_guard_*`）：
- 工作树外路径（`…/var/lib/liptv/liptv.sqlite3`）⇒ **放行**；
- 工作树内已跟踪路径（`README.md` / `liptv/deploy.py` / `TASKS/TASK-006.md`）⇒ **拒绝**。

---

## 3. install / upgrade / rollback（§3 / §10）

### 3.1 入口

```bash
sudo tools/deploy_linux.sh install|upgrade|rollback|status|backup|plan|restore-db [选项]
```

`tools/deploy_linux.sh` 只是 38 行薄包装（`exec "$PYTHON" -m liptv deploy "$@"`）。
**禁止 `curl | bash`**：仓库不提供任何自举下载器，也不从网络拉取脚本。

### 3.2 幂等与「不静默覆盖」

| 对象 | 已存在时的行为 |
|---|---|
| 生产配置 `/etc/…/config.toml` | **不覆盖**（`_Recorder.write_text(overwrite=False)`，记 `skipped`） |
| 数据库 | **不重建、不清空、不迁移**（记 `skipped`，理由「数据库已存在」） |
| 同名 release 目录 | 记 `skipped`，**不原地覆盖**；要重装必须显式 `--force` |
| 目录 | 已存在且权限一致 ⇒ 记 `skipped` |

离线断言：第二次 `install` 的 `changed == []`、`config` 记 `skipped`、`init-db` 记「数据库已存在」。

### 3.3 dry-run / plan

`plan` ≡ `install --dry-run`：**一个字节都不写**，且**不调用任何外部命令**。
实现上靠三处短路：`_Recorder` 构造时固化 `dry_run`（故 `options.dry_run = True`
必须在构造 `Deployer` **之前**）、`_service_action()` 一律返回 `status="planned"`、
`_wait_health()` 直接短路。
`_finish()` 在 dry-run 下 `payload["service"] = None`（**不查询**服务 —— 否则会真跑 `systemctl is-active`）。

### 3.4 release 指针机制（不用 symlink）

- `/opt/li-iptv-aggregator/current` 是**普通文本文件**，内容 = release id；
- unit 里 `Environment=PYTHONPATH=<releases/<id>>` 是「活动 release」的唯一载体；
- 切换 release = 原子改 `current` + 重渲染 unit + `daemon-reload`；
- release id 默认 `<git sha>-<UTC 时间戳>`，`sanitize_release_id()` 白名单化（拒绝 `..`、`/` 等）。

### 3.5 安装方式

- `--method copy`（默认）：release 目录 + unit 的 `PYTHONPATH`，**全程不联网**（项目只用标准库）；
- `--method pip`：`<venv>/pip install --no-deps <release>`；
- `--no-venv`：直接用 `--python` 的解释器 + `PYTHONPATH`，用于离线演练/本机验证。

### 3.6 失败语义与退出码

| 码 | 常量 | 含义 |
|---|---|---|
| 0 | `EXIT_OK` | 成功 |
| 1 | `EXIT_ERROR` | 通用错误（IO / 备份 / 参数） |
| 2 | `EXIT_PREFLIGHT` | `PREFLIGHT_FAILED`：环境不满足、无上一个 release、重复 release |
| 3 | `EXIT_HEALTH` | `HEALTH_FAILED`：启动后 `/healthz` 不健康（`upgrade` 会已自动回滚代码） |

**升级失败绝不删数据**：`upgrade` 失败路径只回代码，SQLite 与 `live.m3u` 逐字节不变（§9 第 11 节逐字节校验）。
`rollback` **只回代码**；恢复数据库必须显式 `restore-db --yes`。

---

## 4. systemd（§4）

### 4.1 unit 关键约束

模板：[`deploy/systemd/li-iptv.service.in`](../deploy/systemd/li-iptv.service.in)（68 行）。
`[Unit]` 之前是「给人看」的模板说明，**渲染时整段丢弃**。

| 项 | 值 | 依据 |
|---|---|---|
| `User=` / `Group=` | `liptv` | 非 root（`User=root` 被静态校验直接拒绝） |
| `WorkingDirectory=` | `/opt/li-iptv-aggregator` | 只读应用目录 |
| `Environment=PYTHONPATH=` | 活动 release 目录 | release 指针机制 |
| `ExecStart=` | `<venv>/python -m liptv run --serve --config <config>` | 无 shell 元字符（校验拒 `;&|` 等） |
| `Restart=` | `on-failure` | |
| `RestartSec=` | `30` | 校验要求 ≥ 5 |
| `RestartPreventExitStatus=` | `3` | **= `runtime.EXIT_LOCKED`，被锁住不重启** |
| `StartLimitIntervalSec=` / `StartLimitBurst=` | `300` / `3` | **双闸门 ⇒ 杜绝无限高速重启风暴** |
| `KillSignal=` | `SIGTERM` | 触发已验收的干净停止 |
| `TimeoutStopSec=` | `90` | 校验要求 ≥ 30，大于一次 ffprobe 清理窗口 |
| `NoNewPrivileges=` / `PrivateTmp=` | `true` | hardening |
| `ProtectSystem=` | `strict` | 应用/系统目录只读 |
| `ReadWritePaths=` | `LIB_DIR CACHE_DIR RUN_DIR` | 只放开三处；过宽（`/`、`/var`、`/etc`、`/usr`、`/var/lib`）被拒 |
| `RuntimeDirectory=` / `RuntimeDirectoryMode=` | `li-iptv-aggregator` / `0750` | `/run` 下短生命周期目录 |
| `UMask=` | `0027` | 新建文件默认不给 other 读 |
| `[Install] WantedBy=` | `multi-user.target` | 开机自启 |

### 4.2 静态自动校验

`deploy.validate_unit(text, required_writable_dirs=[lib, cache, run])` 是**自动**（离线）校验，
覆盖 §4 的全部硬约束，并额外检查：

- **`@@` 残留只扫非注释行**（模板注释里可以自然出现字面量说明）；
- `RestartPreventExitStatus` 必须含 `3`；
- `TimeoutStopSec ≥ 30`、`RestartSec ≥ 5`；
- `ReadWritePaths` **必须覆盖** `required_writable_dirs`（与 `ProtectSystem=strict` 联动）
  且**不得过宽** —— 防止「装得上但一跑就 `Read-only file system`」；
- 比对前两边都归一成 `pathlib.PurePosixPath`（消除 Windows 反斜杠差异）。

### 4.3 缺陷回归（本轮发现并修复）

`render_unit()` 原用子串 `template.index("[Unit]")` 定位小节头，而模板注释里恰好写着
`"[Unit] 之前的全部注释都会被丢掉"` ⇒ 注释被切一半留下、并把 `@@` 带进**生产 unit**
（静态校验只扫非注释行，所以没拦住；那半句注释既非空也不以 `#` 开头）。
⇒ 改为**按「独立节标题行」匹配**，并加永久回归 `test_render_unit_drops_template_comment`。

### 4.4 SIGTERM 与 EXIT_LOCKED

- SIGTERM 触发 TASK-004 已验收的干净停止（不启动新一轮 → 释放锁 → 关 HTTP）。
  离线验证：`test_upgrade_with_real_process_passes_health_gate`（真起子进程 + 真打 `/healthz`），
  以及 demo 第 6/8/9/10 节（真实 `Popen` 服务进程）。
- `EXIT_LOCKED=3` 由 `RestartPreventExitStatus=3` + `StartLimitBurst` 双闸门兜住，
  **不会形成高速重启风暴**（静态校验强制这两个指令存在且取值正确）。

---

## 5. production config / doctor（§5）

### 5.1 生产配置

模板 [`deploy/config.production.example.toml`](../deploy/config.production.example.toml)：

- **全绝对路径**：`[database] path` / `[output] m3u_path` / `[publish] summary_path` /
  `[runtime] lock_path,status_path` / `[fetch] dynamic_tmp_dir`；
- `[server] host = "127.0.0.1"`、`port = 8080`、`playlist_path = "/live.m3u"`、`health_path = "/healthz"`；
- `[probe] enabled = false`（默认关闭；`ffprobe_path = "/usr/bin/ffprobe"`）；
- `[[sources]]` 默认留空（注释里只有示例，**不含任何真实 URL / token / 密钥**）；
- 路径一律 `as_posix()` 写入（TOML 基本字符串里 `\` 是转义符）。

`validate_config_text()` 按**小节**判定（只有 `[probe]` / `[[sources]]` / `[publish]` / `[runtime]`
的 `enabled = true` 与 `include_dynamic` / `require_dynamic = true` 一票否决，
`[server] enabled = true` **不**算违规）。8 条参数化规则全部有离线回归。

**缺陷回归**：`validate_config_text` 原用 `os.path.isabs`，在 Windows 上把
`/var/lib/…` 判成**非**绝对（缺盘符）⇒ 误报「不是绝对路径」。
⇒ 新增 `_is_absolute_path(value)`（`value.startswith("/") or os.path.isabs(value)`）替换之。

### 5.2 `doctor` 检查项

| id | 判据 | 失败级别 |
|---|---|---|
| `config` | 文件存在 + TOML 可解析 | fail |
| `guard` | 运行期路径不落在「工作树内未忽略/已跟踪」位置 | fail |
| `dirs` | DB/输出/状态/锁/摘要/动态快照目录存在且可写 | fail |
| `db` | 数据库可打开（**不存在 ⇒ warn**，首次安装正常） | warn |
| `schema` | `schema_version == db.SCHEMA_VERSION`（**不一致 FAIL，绝不迁移**） | fail |
| `ffprobe` | **仅当** `probe.enabled = true` | fail |
| `port` | **仅当** `server.enabled = true`（`--no-port-check` 可跳过） | fail |
| `lock` | 只用 `runtime.inspect_lock()` 只读判断 | fail |
| `backups` | 备份目录里可验证的份数 | warn |

**无业务副作用**（有回归看着）：
`test_doctor_does_not_migrate_schema`（V99 库保持 99、字节不变）、
`test_doctor_is_read_only`（数据库与 `live.m3u` 字节 + mtime 不变、无残留探针）、
`test_ffprobe_skipped_when_probe_disabled` + `test_doctor_never_calls_ffprobe_when_disabled`
（`probe.enabled=false` 时 `probe_mod.check_ffprobe` **零调用**）。

可写性用**真写探针文件再删**判断（`os.access` 在 root 下会骗人），`--no-write-probe` 可关。

**缺陷回归（本轮发现并修复）**：
1. `_check(id, status, message, **detail)` 与 `**capability.to_dict()` 里的 `message` 键撞名
   ⇒ `TypeError: _check() got multiple values for argument 'message'`
   （默认 `probe.enabled=false` 所以之前没暴露）⇒ 改为 `info["probe_message"] = info.pop("message")`。
2. **`dirs` 检查会往「刚被 `guard` 判为不合法」的目录里写探针文件**。
   这既不自洽（护栏说不许往那儿写运行时产物，体检工具自己却写），
   也在误配到仓库内目录时产生「doctor 反而在仓库里建/删临时文件」的副作用。
   ⇒ 已被 guard 拒绝的位置，`dirs` 退化为 `os.access` 并明确标注
   `未写探针：该路径已被 Git 输出护栏拒绝，doctor 不往那里写文件`；加了回归断言。

---

## 6. SQLite backup（§6）

- 用 **SQLite 在线备份 API**（`sqlite3.Connection.backup()`）—— 不是文件拷贝，
  服务在写也能拿到**一致**快照。
- 文件名 `liptv-<UTC 时间戳>[-<label>].sqlite3`，落 `<LIB_DIR>/backups/`（模式 `0700`）。
- 备份后**立即校验**：只读 URI 打开 + `PRAGMA integrity_check` + `table_names` +
  `read_schema_version`；**校验失败即删除该备份并抛 `BackupError`**（宁可不给备份，不给坏备份）。
- 保留最近 N 份（默认 5，`--retention` 可调）；不入 Git。
- 源库不存在 ⇒ `{"skipped": True}`，不报错。
- **恢复是唯一写库入口**：`restore_sqlite(..., allow_overwrite=True)`，必须显式 `--yes`；
  覆盖前先存 `<db>.pre-restore-<ts>` 安全副本。
- `upgrade` 会在停服务前**自动备份一次**（有回归 `test_upgrade_takes_a_backup_before_touching_anything`）。

---

## 7. 网络暴露（§7）

- 默认 `[server] host = "127.0.0.1"` ⇒ **公网访问不到**；`health.py` 只允许 loopback
  （`health_base_url()` 把 `0.0.0.0` / `::` 归一成 `127.0.0.1`）。
- 暴露优先级（写在 `DEPLOYMENT.md` §7 与反代示例顶部）：
  **VPN / Tailscale / WireGuard > 反代 + TLS + 访问控制 > 直接公网**。
- 仓库只提供**一种**反代示例：[`deploy/reverse-proxy/Caddyfile.example`](../deploy/reverse-proxy/Caddyfile.example)。
  安全边界：只反代 `/live.m3u` 与 `/healthz`，**其余路径 404**；`basicauth` + `tls`；
  禁目录浏览；不暴露 DB / `live.previous.m3u` / 摘要 / 日志；访问日志**不记 query**；
  收紧响应头（HSTS / `nosniff` / `no-referrer` / 去 `Server`）。
  末尾另有一段「nginx 用户？」说明 `location =` 精确匹配、`autoindex off`、
  `access_log` 去 query、`proxy_intercept_errors off`。
- **不提交证书私钥**、不托管证书、不注册域名、不改安全组。

---

## 8. 健康与日志（§8 / §9）

### 8.1 健康口径（与 TASK-004 冻结口径一致）

- `/healthz` **总是 HTTP 200**，业务状态在 JSON 的 `status`：`ok` / `stale` / `missing`；
- `/live.m3u` 缺失或为空 ⇒ **503**（不生成空列表冒充成功）；
- **健康 = `/healthz` 的 `status == ok` 且 `/live.m3u` 200 且非空**（`health.py` 的 `check_once`）；
- **systemd `active` ≠ 业务健康** —— `status` 子命令主动探一次 `/healthz`，验收看它；
- `missing` / `stale` 不会被伪装成 `ok`；probe 环境级故障保留 TASK-005 的 `degraded`。

`tools/healthcheck.py`（只访问 localhost，**永不打印播放列表正文**）：
退出码 `0` 健康 / `1` 降级 / `2` 不可达 / `3` 用法错误。
`HealthResult.to_dict()` 是**白名单**字段，**不含任何 URL / token / 播放列表正文**。

### 8.2 日志

- 优先 journald（`journalctl -u li-iptv`），不新增自增长日志文件；
- 能定位 `round_id`、fetch / probe / publish 摘要、退出原因；
- 脱敏：`deploy` / `doctor` / unit 渲染输出与 demo 产物都做过「带 query 的 URL、token、password」扫描
  （demo 第 12 节 + `tests/test_deploy.py::test_unit_and_config_templates_have_no_secrets`）。

---

## 9. 离线部署 E2E（§13）

`python tools/demo_deploy_linux.py` —— 临时前缀 + **假 systemctl** + 假 ffprobe + 本机 mock 来源，
**不访问任何公网地址**。

13 节、**73 项断言全部通过**，退出码 **0**：

| 节 | 内容 | 关键断言 |
|---|---|---|
| 0 | 临时 DESTDIR 前缀 | 非 POSIX 必须显式 `--root` |
| 1 | `deploy plan` | `status=PLAN`、exit 0、**前缀目录不存在**、无 systemctl 动作 |
| 2 | `install`（fake systemctl） | 目录模型 + unit 文本 + 权限意图 + fake systemctl 收到 `daemon-reload`/`enable`/`start` |
| 3 | 幂等 | 第二次 `changed=[]`、config/DB 记 `skipped` |
| 4 | production config + `doctor` | 全绝对路径、无 sources、无 token、doctor PASS |
| 5 | 真实喂数据 | `source-register → fetch → canonical-add + binding-add → stream-sync → probe-result-add → run --once` |
| 6 | **真实服务进程** | 真起 `Popen` 服务，`/healthz` 真的 `ok`、`/live.m3u` 200 且非空 |
| 7 | SQLite 备份 | 在线备份 + 只读重开校验 |
| 8 | `upgrade` v1→v2 | 指针切到 v2、unit `PYTHONPATH` 切到 `releases/v2`、**升级后服务真的健康** |
| 9 | 显式 `rollback` v2→v1 | 状态 `ROLLED_BACK`、指针回 v1、**回滚后服务真的健康** |
| 10 | **注入坏 release** | 健康闸门判失败（**exit 3**）→ **自动回滚** → 回到 v1 且恢复健康 |
| 11 | 数据未被碰 | **数据库与 `/live.m3u` 的 sha256 逐字节不变** |
| 12 | 泄漏扫描 | 渲染产物 / 部署输出 / 服务日志**无带 query 的 URL / 凭据** |
| 13 | 关键观察 | releases 列表、release 指针、deploy-state 事件链、备份清单 |

**本轮有两个生产级缺陷只有这条「真实子进程 + 注入坏 release」路径才能发现**：

1. **`upgrade()` 起的是旧 release** —— `self.service = self._service_for(old_release)` 后
   一直沿用到 `start`，对 `process` 管理器等于把**旧版本**当新版本起起来 ⇒
   健康闸门永远验不到新 release，坏 release 会静默「升级成功」。
   ⇒ 在 `start` 前插入 `self.service = self._service_for(release_id)`；
   加永久回归 `test_upgrade_starts_the_new_release`（monkeypatch `build_service_manager`）。
2. **unit 渲染残留模板注释并带进 `@@`**（见 §4.3）。

另外两个（`doctor` 测活分支崩溃、`validate_config_text` 误判 POSIX 绝对路径）由离线测试暴露，见 §5.2。

---

## 10. 测试（§11 / §12）

### 10.1 分组命令与结果

解释器（受管 Python 3.13.12）、`--basetemp` **必须落系统临时目录**：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <py> -m pytest -o addopts="" -p no:cacheprovider -q --basetemp="$TEMP/liptv-t6-all2" tests/
```

| 组 | 命令 | 结果 | 退出码 |
|---|---|---|---|
| **全量** | `… tests/` | **385 passed in 888.62s (0:14:48)** | **0** |
| 本轮新增（两组） | `… tests/test_deploy.py tests/test_doctor.py` | **80 passed** | **0** |
| §13 离线 demo | `python tools/demo_deploy_linux.py` | **73/73 断言，演示结论：全部通过** | **0** |

- **基线 305 项零回归**；新增 **80** 项（`tests/test_deploy.py` 55 + `tests/test_doctor.py` 25）⇒ **385**。
- 全量 889s（上轮 TASK-005 全量为 715s）——变慢主要来自 `test_deploy.py` 里
  「真起子进程 + 真等 `/healthz`」的几条用例，以及沙箱对工作树内文件删除的额外拦截开销。
- 中途曾出现一次 `1 failed, 384 passed`，失败项是
  `tests/test_doctor.py::test_guard_conflict_is_a_failure`（沙箱安全删除护栏所致，见 §10.4 第 2 条）；
  修复后复跑 **385 passed / exit 0**。

### 10.2 §12 最低验收场景对照（14 条）

| # | 场景 | 覆盖 |
|---|---|---|
| 1 | 305 项零回归 | ✅ 全量 385 passed（305 基线 + 80 新增），exit 0 |
| 2 | install 到临时 root 且幂等 | ✅ `test_install_creates_frozen_layout` / `test_install_is_idempotent_and_never_overwrites` / demo 第 2–3 节 |
| 3 | 已存在 DB / config 不覆盖 | ✅ `test_install_never_overwrites_existing_production_config` / `test_install_does_not_recreate_existing_database` / demo 第 3 节 |
| 4 | production config 全绝对且 example 无 token | ✅ `test_render_config_is_all_absolute_and_carries_no_sources` / `test_unit_and_config_templates_have_no_secrets` |
| 5 | doctor 各分支 | ✅ `tests/test_doctor.py` 25 项（缺配置 / 坏 TOML / 目录 / 端口 / ffprobe / 锁 / schema / 备份 / 只读性） |
| 6 | unit 非 root + SIGTERM + `EXIT_LOCKED` 不 tight loop | ✅ `validate_unit` 静态校验 + `RestartPreventExitStatus=3` + `StartLimitBurst` 断言（`test_rendered_unit_passes_static_validation` / `test_validate_unit_rejects_unsafe_mutations`） |
| 7 | 真实启动后 `/healthz`、`/live.m3u` 与 TASK-004 一致 | ✅ `test_upgrade_with_real_process_passes_health_gate` / demo 第 6 节（`liptv/server.py` blob 未变） |
| 8 | SQLite 备份可重开、表数 / schema 正常 | ✅ `test_backup_and_verify_roundtrip` / `test_verify_backup_rejects_garbage`（`verify_backup`：integrity_check + 9 张表 + schema 版本）/ demo 第 7 节 |
| 9 | upgrade 成功路径保 DB / live.m3u | ✅ demo 第 8、11 节（sha256 逐字节不变） |
| 10 | upgrade 健康失败后 rollback 成功且 DB / live.m3u 不丢 | ✅ `test_upgrade_health_failure_rolls_back_and_keeps_data` / demo 第 10、11 节 |
| 11 | journald / unit / doctor / deploy 输出不泄漏带 query 的 URL / token | ✅ demo 第 12 节 + `test_unit_and_config_templates_have_no_secrets` + `test_state_file_records_events_and_never_holds_secrets` |
| 12 | **工作树外 `/var/lib/…` 可用；工作树内未忽略 / 已跟踪路径仍被拒** | ✅ `test_guard_allows_paths_outside_the_git_worktree` / `test_guard_still_rejects_tracked_paths_inside_the_worktree` / `test_guard_conflict_is_a_failure` |
| 13 | 不改 selector / schema / 不加视频代理 | ✅ §1.2 blob 对照表全 SAME |
| 14 | `git diff --check` + 报告 + 测试命令 / 退出码 + 离线 demo + Git SHA + 本地 / 远端一致 | ✅ §10.3 与 §13 |

### 10.3 代码卫生

- `git diff --check`（含未跟踪文件的 intent-to-add 形态）：**exit 0**，无空白错误。
- 字符卫生自检（新增/修改的 17 个文件）：**NUL / CRLF / C0 / 零宽与双向控制字符 = 0 处**。
  （本机 `core.autocrlf=true`，提交前逐个确认 blob 仍是 LF。）

### 10.4 环境噪声（如实记录）

1. **本机没有 ffprobe**（`PATH` 里没有）——预期行为，离线测试一律走
   `ffprobe_path = [python, tools/fake_ffprobe.py]`；`probe-check` 报 `FFPROBE_NOT_FOUND` 是**预期**。
2. **沙箱「安全删除」护栏**：本机 `sitecustomize.py` 会给 Python 的 `pathlib.Path.unlink`
   加一层「批量删除需确认」的闸门（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），
   命中时抛 `SystemExit(1)`。它曾让 `test_guard_conflict_is_a_failure` 在**全量**跑时失败
   （该用例把输出目录设成仓库根，`dirs` 探针文件落在仓库内 → 删除被拦 → 并留下一个残留探针文件）。
   **修复落在产品侧**（见 §5.2 第 2 条：doctor 不再往 guard 已拒绝的位置写探针），
   并在用例里加了「仓库根不得残留探针文件」的回归断言。修复后全量 385 passed。
   > 这不是产品缺陷，但值得知道：**在真实 Linux 主机上没有这层护栏**。
3. Windows 下 `ProcessServiceManager.stop()` 用 `TerminateProcess`（无法投递 SIGTERM），
   因此 demo 里停止后锁状态退化为 `stale` —— 属**可安全接管**；真实 SIGTERM 路径由 POSIX 语义覆盖
   （`SIGTERM` 处理逻辑本身在 `liptv/runtime.py`，TASK-004 已验收且本轮未改）。

---

## 11. 实机部署

**NOT EXECUTED。**

本轮**没有**任何 Linux 主机、域名、root 权限或网络放行凭据，因此**没有**做任何真实部署。
按 §Stop/Gate 的要求：**不伪造「已部署成功」**。

已完成的是**可在任何机器上离线复现**的部分：目录模型、权限意图、unit 渲染与静态校验、
install / upgrade / rollback / backup / restore 的完整逻辑、`doctor`、健康闸门、
以及用**真实子进程**跑通「服务起来 → `/healthz` ok → `/live.m3u` 非空 → 升级 → 回滚」。

**需要老李一次性介入的步骤**（同样写在 [`DEPLOYMENT.md`](../DEPLOYMENT.md) §11）：

1. 准备一台 Linux 主机（Ubuntu 24.04 LTS / Debian 12，x86_64），拿到 root 或 sudo；
2. 把仓库放到该主机（`git clone` 或手工上传）；
3. 决定网络暴露方式（Tailscale/WireGuard 私网，或反代 + TLS）——
   域名 / 证书 / 安全组都要你操作；
4. 填写生产配置里的 `[[sources]]`（含私密 URL，**不要提交进 Git**）；
5. 可选：装 `ffmpeg` 提供 ffprobe，并把 `[probe] enabled = true`；
6. 首次验收：`sudo systemctl status li-iptv` + `curl -fsS http://127.0.0.1:8080/healthz`。

拿到主机后，跑 `DEPLOYMENT.md` §0 的三条命令即可完成部署。

---

## 12. 风险与范围外

**范围外（本轮不做，也不应被当作已完成）**：
多地区 / 多探针协调、多主机选主、Kubernetes、Docker、Terraform / 云 IaC、
自动采购主机 / 改安全组 / 注册域名 / 托管证书私钥、视频代理 / 转码 / DVR、
EPG / Logo、Dashboard、selector 算法变更、schema V2。

**已知风险 / 待观察**：

1. **未在真实 systemd 上跑过**（§11）—— unit 只是**静态校验 + 离线渲染**通过，
   `systemctl` 交互在离线验证里由假 systemctl 与 `ProcessServiceManager` 代偿。
   首次真实安装时，`ProtectSystem=strict` + `ReadWritePaths` 的组合建议按 `doctor` 结论复核。
2. `--method pip` 路径未做端到端演练（默认 `copy` 足够，项目零依赖）；
3. `rollback` 的坏 release 目录**不会自动删除**（真实运维里由人工清理，demo 第 13 节已注明）；
4. 单实例语义仍是「同一套 data/output 只能有一个 scheduler」（TASK-004 冻结），
   本轮**没有**引入多主机选主 —— 靠 systemd 单机单实例 + 数据目录独占来保证。

---

## 13. Git & Gate

- 基线 HEAD：`466d412f7ffef50b841b704cba91ea7b74d4967a`
- 变更：17 个文件，+5661 / −2（见 §1.1）
- `git diff --check`：exit 0
- 实现提交 SHA：见紧随其后的「记录提交」（本仓库惯例：先提交 → push → 用一次记录提交回填 SHA，**不 amend**）
- 远端一致性：见记录提交里的云端连接器核验结果
- TASK 状态：**REVIEW**
- **停 Gate**：不启动 TASK-007，等大G独立验收。
