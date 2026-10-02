# 单机 Linux 生产部署（TASK-006）

把本项目从「手工跑一次」固化成**一台 Linux 主机上长期运行的系统服务**。

- 目标环境：Linux x86_64 / Python 3.11+ / systemd / SQLite。优先 Ubuntu 24.04 LTS、Debian 12。
- 单主机、单实例。**不依赖任何云厂商 API**，不做多主机选主，不做 Docker / Kubernetes。
- 服务**不以 root 长期运行**；应用目录对服务用户**只读**。

> ⚠️ 本项目**不会**替你采购云主机、**不会**注册域名、**不会**改安全组、**不会**保存云厂商密钥或 TLS 私钥。
> 需要主机 / 域名 / root / 网络权限的步骤在下面标注为 **【需要老李操作】**。

---

## 0. 一句话流程

```bash
# 在目标 Linux 主机上（先把仓库放到本机某个目录，例如 ~/li-iptv-aggregator）
sudo ./tools/deploy_linux.sh install --create-user --service-manager systemd --start
curl -fsS http://127.0.0.1:8080/healthz
```

`install` 会：建目录与权限 → 装 release → 建 venv → **只在没有数据库时**显式 init 一次 →
写生产配置（**已存在则绝不覆盖**）→ 装 systemd unit → 启用开机自启 → 起服务 → 等 `/healthz` 真的 `ok`。

---

## 1. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| OS | Ubuntu 24.04 LTS / Debian 12（其它 systemd 发行版应可用） | `cat /etc/os-release` |
| Python | ≥ 3.11（仓库 `pyproject.toml` 写的是 `>=3.12`，以它为准） | `python3 --version` |
| systemd | 作为 init（PID 1） | `systemctl --version` |
| ffprobe | **仅当** `[probe] enabled = true` 才需要 | `ffprobe -version` |
| SQLite | 标准库自带 | — |

装 ffprobe（Debian/Ubuntu）：

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

安装前脚本会自动检查 Python 版本、ffprobe（仅当启用测活）、systemd 可用性；
不满足会以 **退出码 2（PREFLIGHT_FAILED）** 明确失败，而不是装到一半。

> `install` / `upgrade` 需要 root（写 `/opt`、`/etc`、`/var/lib`、`/etc/systemd/system`）。
> 脚本用 `sudo` 调用即可，**不会**用 `curl | bash` 的方式自举。

---

## 2. 目录模型与权限

| 路径 | 用途 | 属主 | 权限 |
|---|---|---|---|
| `/opt/li-iptv-aggregator` | 应用（release + venv），**服务只读** | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/releases/<id>` | 每个版本一个独立目录，不原地覆盖 | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/venv` | 虚拟环境 | `root:root` | `0755` |
| `/opt/li-iptv-aggregator/current` | **文本文件**，内容 = 当前活动 release id | `root:root` | `0644` |
| `/etc/li-iptv-aggregator` | 配置目录 | `root:liptv` | `0750` |
| `/etc/li-iptv-aggregator/config.toml` | **生产配置（正本）**，安装时若已存在则**不覆盖** | `root:liptv` | `0640` |
| `/etc/systemd/system/li-iptv.service` | systemd unit | `root:root` | `0644` |
| `/var/lib/li-iptv-aggregator` | 持久数据（SQLite） | `liptv:liptv` | `0750` |
| `/var/lib/li-iptv-aggregator/liptv.sqlite3` | 数据库 | `liptv:liptv` | `0640` |
| `/var/lib/li-iptv-aggregator/backups` | SQLite 一致性备份 | `liptv:liptv` | `0700` |
| `/var/lib/li-iptv-aggregator/live.m3u` | 当前发布的订阅文件 | `liptv:liptv` | `0640` |
| `/var/cache/li-iptv-aggregator` | 可丢弃缓存（动态源临时快照等） | `liptv:liptv` | `0750` |
| `/run/li-iptv-aggregator` | 短生命周期（pid / lock） | `liptv:liptv` | `0750` |

- 服务用户/组：`liptv`（系统用户，`--create-user` 才创建，需 root）。
- 应用目录**不可写**：服务只能写 `/var/lib`、`/var/cache`、`/run`（由 unit 的
  `ProtectSystem=strict` + `ReadWritePaths` 强制）。
- 每次 `install`/`upgrade` 都会打印**权限意图表**；`--no-chown` 可以只记录不执行
  （离线演练用），生产不要加。
- 上面这张表**不是文档自说自话**：`Layout.mode_table()` 是唯一来源，`_apply_modes()`
  逐项照它 chown/chmod，并由 `tests/test_deploy.py` 的权限矩阵用例与
  `tools/demo_deploy_linux.py`（第 2 节）现场断言。**低权限服务账号不拥有任何代码或配置路径。**

---

## 3. install / upgrade / rollback

统一入口（薄包装，等价于 `python3 -m liptv deploy <子命令>`）：

```bash
sudo ./tools/deploy_linux.sh <子命令> [选项]
```

> 也可用环境变量 `LIPTV_PYTHON=/usr/bin/python3` 指定解释器。

### 3.1 `plan` —— 先看会动什么

```bash
sudo ./tools/deploy_linux.sh plan --root /tmp/dry
```

等价于 `install --dry-run`：**一个字节都不写**，也**不会**调用 `systemctl`。
输出里 `status = planned`，并列出 `changed` / `skipped` 路径。装之前先跑一遍最稳妥。

### 3.2 `install` —— 首次安装

```bash
# 最小：只落文件，不建用户、不托管服务（适合先检查目录）
sudo ./tools/deploy_linux.sh install

# 生产：建用户 + systemd 托管 + 装完立刻起服务并等健康
sudo ./tools/deploy_linux.sh install --create-user --service-manager systemd --start
```

**幂等**：重复 `install` 是安全的 —— 第二次运行 `changed` 为空、生产配置记 `skipped`、
数据库记 `skipped`（理由「数据库已存在」）。

**绝不静默覆盖**：

- 生产配置已存在 ⇒ **不覆盖**（要改就手工编辑 `/etc/li-iptv-aggregator/config.toml`）；
- 数据库已存在 ⇒ **不重建、不清空、不迁移**；
- 同名 release 目录已存在 ⇒ 记 `skipped`，要重装必须 `--force`。

### 3.3 `upgrade` —— 升级（失败自动回滚代码）

```bash
sudo ./tools/deploy_linux.sh upgrade --service-manager systemd
```

固定顺序：

```
preflight → 备份 DB → 停服务 → 安装新 release → 起新 release → 等 /healthz ok
   ↓ 任一步失败
停服务 → 回滚到上一个 release → 重启旧版本 → 再验 /healthz
```

- release 用**独立目录**（默认 `<git sha>-<UTC 时间戳>`），**不原地覆盖**旧版本。
- 升级前**必定先做一次 SQLite 一致性备份**（可用 `--retention N` 调整保留份数）。
- 健康闸门失败 ⇒ `exit 3`，并且**自动回滚代码**；**数据库与 `live.m3u` 一个字节都不动**。
- 同 release 且未 `--force` ⇒ 以 `PREFLIGHT_FAILED`（exit 2）拒绝。
- 卸载/覆盖都不会删除现有 `live.m3u` 或 SQLite。

### 3.4 `rollback` —— 只回代码

```bash
sudo ./tools/deploy_linux.sh rollback --service-manager systemd
```

- **只回代码**：把 `current` 指回上一个 release，重渲染 unit 并重启。
- **数据库必须显式恢复**（见 §6）：`rollback` 不会碰数据库。
- 没有上一个 release ⇒ 干净失败（`PREFLIGHT_FAILED`），不会把系统搞成半吊子。

### 3.5 `status` —— 只读查看

```bash
sudo ./tools/deploy_linux.sh status            # 含主动探测 /healthz
sudo ./tools/deploy_linux.sh status --no-health
python3 -m liptv deploy status --root /tmp/x --json
```

`status` **不写任何文件**：读 `deploy-state.json`、当前 release、unit 是否在、serve 是否在。
注意 **systemd `active` ≠ 业务健康**，验收看 `/healthz`（见 §8）。

### 3.6 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 通用错误（参数 / IO / 备份失败等） |
| `2` | `PREFLIGHT_FAILED`（环境或前置条件不满足、无上一个 release、重复 release） |
| `3` | `HEALTH_FAILED`（启动后 `/healthz` 不健康；`upgrade` 会已自动回滚代码） |

### 3.7 安装方式（`--method`）

| 值 | 行为 | 联网 |
|---|---|---|
| `copy`（默认） | release 目录 + unit 里 `Environment=PYTHONPATH=<release>`，零依赖安装 | **否** |
| `pip` | `<venv>/pip install --no-deps <release>` | 是（需要 pip 源） |

项目**只用标准库**，所以默认 `copy` 就够了，安装全程不需要网络。
`--no-venv` 直接用 `--python` 给的解释器 + `PYTHONPATH` 指向 release，用于离线演练/本机验证。

---

## 4. systemd 服务

unit 模板在 [`deploy/systemd/li-iptv.service.in`](deploy/systemd/li-iptv.service.in)，
`install`/`upgrade` 时渲染占位符后写到 `/etc/systemd/system/li-iptv.service`。

关键约束（**静态自动校验**，见 `tests/test_deploy.py`）：

| 项 | 值 | 为什么 |
|---|---|---|
| `User=` / `Group=` | `liptv` | 非 root 长期运行（`User=root` 会被校验直接拒绝） |
| `WorkingDirectory=` | `/opt/li-iptv-aggregator` | 只读应用目录 |
| `Environment=PYTHONPATH=` | 活动 release 目录 | 「指向活动 release」的机制（**不用 symlink**） |
| `ExecStart=` | `<venv>/python -m liptv run --serve --config /etc/...` | 直接 exec，无 shell |
| `Restart=` | `on-failure` | |
| `RestartSec=` | `30`（校验要求 ≥ 5） | 避免异常时高频重启 |
| `RestartPreventExitStatus=` | `3` | **退出码 3 = EXIT_LOCKED 不重启** |
| `StartLimitIntervalSec=` / `StartLimitBurst=` | `300` / `3` | 双重闸门，**杜绝无限高速重启风暴** |
| `KillSignal=` | `SIGTERM` | 触发已验收的干净停止（不启动新一轮、释放锁、关 HTTP） |
| `TimeoutStopSec=` | `90`（校验要求 ≥ 30） | 必须大于一次 ffprobe 清理窗口 |
| `NoNewPrivileges=` / `PrivateTmp=` | `true` | hardening |
| `ProtectSystem=` | `strict` | 应用/系统目录只读 |
| `ReadWritePaths=` | `/var/lib`… 、`/var/cache`… 、`/run`… | 只放开真正要写的三处；过宽（`/`、`/var`、`/etc`、`/usr`、`/var/lib`）会被校验拒绝 |
| `RuntimeDirectory=` | `li-iptv-aggregator` | `/run` 下的短生命周期目录 |
| `UMask=` | `0027` | 新建文件默认不给 other 读 |

`ReadWritePaths` 与 `ProtectSystem=strict` 是**联动**的：静态校验会比对「unit 声明的可写目录」
与「配置里实际要写的目录」，任何一边缺了都会失败 —— 防止出现「装得上但一跑就 `Read-only file system`」。

启用开机自启由 `install --service-manager systemd` 完成（`systemctl enable --now`）。

---

## 5. 生产配置与 `doctor`

配置模板：[`deploy/config.production.example.toml`](deploy/config.production.example.toml)。
安装时渲染成 `/etc/li-iptv-aggregator/config.toml`（**已存在则不覆盖**）。

要点：

- **全绝对路径**：数据库 / `live.m3u` / 摘要 / 状态 / 锁 / 动态临时目录一律绝对路径
  （校验会拒绝相对路径）。
- `[server] host` 默认 `127.0.0.1`（**不裸露公网**，见 §7）。
- `[probe] enabled` 默认 `false`；`[[sources]]` 默认留空 ⇒ 装上就是一个**完全不出网**的服务。
- 模板**不含**任何真实 URL / token / 密钥；来源清单要自己填（或用 `source-add`）。

### `doctor` —— 上线前体检

```bash
python3 -m liptv doctor --config /etc/li-iptv-aggregator/config.toml
# 或（容器/CI 里端口检查不适用时）
python3 -m liptv doctor --config ... --no-port-check
```

检查项：

| id | 检查内容 |
|---|---|
| `config` | 配置文件存在且 TOML 可解析 |
| `guard` | 运行产物路径护栏（见 §5.1） |
| `dirs` | DB / output / status / lock 所在目录**真的可写**（会真写一个探针文件再删，`os.access` 在 root 下会骗人） |
| `db` | 数据库可打开；**不存在只 WARN**（首次安装正常） |
| `schema` | `schema_version` 与代码一致；**不一致直接 FAIL，绝不迁移** |
| `ffprobe` | **仅当** `[probe] enabled = true` 才检查（否则 SKIP）；检查默认**不请求任何媒体流** |
| `port` | **仅当** `[server] enabled = true` 才检查端口能否绑定（`--no-port-check` 可跳过） |
| `lock` | 单实例锁现状（free / held / stale / unreadable） |
| `backups` | 备份目录里可验证的备份份数 |

**`doctor` 无业务副作用**：不 fetch、不 publish、不请求媒体流、不改数据库业务状态
（回归用例 `test_doctor_does_not_migrate_schema` / `test_doctor_is_read_only` 看着）。
但它**会**在目录里写一个临时探针文件（`--no-write-probe` 可关）。

退出码：`0` = 全 PASS（可有 WARN/SKIP）；非 0 = 有 FAIL。

### 5.1 关于「Git 运行产物护栏」

TASK-002/003 引入的护栏（`publish.guard_runtime_output_path`）用来防止把运行产物写进 Git：

- 目标**不在任何 Git 工作树内** ⇒ **直接放行**（`/var/lib/...`、`/var/cache/...`、`/run/...` 属于此类）；
- 目标在某个 Git 工作树内 ⇒ 必须**被 `.gitignore` 忽略**且**未被 Git 跟踪**，否则拒绝。

因此生产路径（全是工作树外）天然可用，**无需改动护栏**。两种行为都有离线回归覆盖。

---

## 6. SQLite 一致性备份与恢复

### 6.1 备份

```bash
sudo ./tools/deploy_linux.sh backup --retention 5
```

- 用 **SQLite 在线备份 API**（`sqlite3.Connection.backup()`）—— 不是文件拷贝，
  数据库正在被服务写也**能拿到一致快照**。
- 文件名 `liptv-<UTC 时间戳>[-<label>].sqlite3`，落在 `/var/lib/li-iptv-aggregator/backups/`。
- 备份完**立刻校验**：只读打开 + `PRAGMA integrity_check` + 表清单 + schema 版本；
  校验失败即**删掉这个备份并报错**（宁可不给备份，也不给一个坏的）。
- 保留最近 N 份（默认 5），超出的自动清理。
- 数据库不存在 ⇒ 记 `skipped`，不报错。
- 备份**不进 Git**。

`upgrade` 会自动先备份一次，不用手动跑。

### 6.2 恢复（**破坏性，必须过两道确认**）

```bash
# 先看清要恢复哪个
ls -l /var/lib/li-iptv-aggregator/backups/

# 直接跑即可：restore-db 会自己核实服务状态并按需停机
sudo ./tools/deploy_linux.sh restore-db \
  /var/lib/li-iptv-aggregator/backups/liptv-20261001T120000Z.sqlite3 --yes
```

恢复要同时过**两道独立确认**：

1. `--yes` —— 「我知道这会覆盖现有数据库」；
2. **停机门禁（fail-closed，QA-006B）** —— 命令自己查服务状态：
   - 服务 `active` ⇒ **先自动 `stop` 并复查为 `inactive`**，再恢复；
   - `stop` 失败，或复查仍是 `active` ⇒ **拒绝恢复，数据库一个字节都不变**（退出码 2）；
   - 查不到服务状态（`--service-manager none`）⇒ **默认拒绝**。

`--yes` **不代表**「服务已经停了」—— 两者是独立的两件事。

- 恢复**不会自动重启**服务（保守口径）：确认数据无误后自己
  `sudo systemctl start li-iptv`，再复查 `/healthz`。
- 唯一 break-glass 开关是 `--force-offline-restore`：表示「我确认服务已停」，跳过停机门禁。
  **危险，默认关闭**，且命令会在输出里明确标注你用了它。
- 覆盖前会把现有数据库另存为 `<db>.pre-restore-<ts>`；这份安全副本**同样走 SQLite 在线备份 API**
  生成一致性快照（不是裸文件复制），即使在服务仍有写操作时也拿得到可用副本。
- 恢复后跑一次 `doctor` 确认 schema 版本正常。

```bash
# 没有 systemd（容器 / 已手工确认停机）时的显式 break-glass，只有当你知道自己在做什么才用
sudo ./tools/deploy_linux.sh restore-db <备份路径> --yes \
  --service-manager none --force-offline-restore
```

---

## 7. 网络暴露边界

**默认只绑 `127.0.0.1`**，公网访问不到。暴露优先级（从安全到最不安全）：

1. **VPN / Tailscale / WireGuard**（推荐）—— 服务留 `127.0.0.1`，播放器通过私网访问；
2. **反代 + TLS + 访问控制** —— 反代监听公网，只反代 `/live.m3u` 与 `/healthz`；
3. **直接公网** —— 不推荐，至少要有 TLS + 访问控制。

本仓库提供**唯一**一种反代示例：[`deploy/reverse-proxy/Caddyfile.example`](deploy/reverse-proxy/Caddyfile.example)。
该示例的安全边界：

- 只反代 `/live.m3u` 与 `/healthz`，**其它路径一律 404**；
- 开启 `basicauth`（访问控制）+ `tls`（证书由反代管理，**私钥不进仓库**）；
- 禁目录浏览、不暴露数据库 / `live.previous.m3u` / 发布摘要 / 日志；
- 访问日志**不记录带 query 的 stream URL**；
- 收紧响应头（HSTS / `nosniff` / `no-referrer` / 去 `Server`）。

文件末尾另有「nginx 用户？」一节，说明用 nginx 时该注意什么（`location =` 精确匹配、
`autoindex off`、`access_log` 去 query、`proxy_intercept_errors off`）。

**【需要老李操作】**：域名解析、证书签发（或 Tailscale 组网）、安全组/防火墙放行、
反代软件的安装与启用 —— 这些**必须由你**在拿到主机与域名后决定并执行。
本项目**不会**自动改安全组、**不会**注册域名、**不会**托管证书私钥。

---

## 8. 健康检查

```bash
# 服务自身（只允许 loopback，除非显式 --allow-non-loopback）
python3 -m tools.healthcheck --host 127.0.0.1 --port 8080
# 或直接 curl
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/live.m3u
```

- `/healthz` **总是 HTTP 200**，业务状态在 JSON 的 `status` 字段里：
  `ok` / `stale` / `missing`。
- `/live.m3u` 缺失或为空 ⇒ **503**（不会生成空列表冒充成功）。
- **健康 = 「`/healthz` 的 `status == ok`」且「`/live.m3u` 200 且非空」**。
- **systemd `active` ≠ 业务健康** —— 你的监控/验收**必须**看 `/healthz`。
- `missing` / `stale` **不会**被伪装成 `ok`；`/live.m3u` 503 时检查器会如实失败/降级。
- probe 环境级故障（ffprobe 缺失等）保留 TASK-005 的 `degraded` 语义，`/healthz` 不会报全 OK。

`tools/healthcheck.py` 的退出码：`0` 健康 / `1` 降级 / `2` 不可达 / `3` 用法错误；
它**只访问 localhost**，且**永不打印播放列表正文**。可直接挂到外部监控。

---

## 9. 日志

- 优先 **journald**，不额外维护自增长日志文件：

```bash
journalctl -u li-iptv -n 200 --no-pager     # 最近 200 行
journalctl -u li-iptv -f                    # 实时跟随
journalctl -u li-iptv --since '1 hour ago'
```

- 日志里能定位到 `round_id`、fetch / probe / publish 摘要、退出原因。
- **脱敏**：不打印完整 stream query / token、`Cookie` / `Authorization`；
  敏感配置不展开进命令行；URL 一律 `scheme://host/...`。
- 自定义日志不无限增长（走 journald 的轮转策略）。

---

## 10. 离线验证（不需要 Linux、不需要 root、不出网）

在开发机（含 Windows）上就能把 install → health → backup → upgrade → rollback 全流程跑一遍，
用的是：临时 root + **假 systemctl** + **假 ffprobe** + 本机 mock 来源。

```bash
python tools/demo_deploy_linux.py            # 端到端离线演示（73 项断言）
python tools/demo_deploy_linux.py --keep     # 保留临时目录以便排查

# 受控 pytest（注意 --basetemp 必须落系统临时目录）
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider \
    -q --basetemp="$TEMP/liptv-pytest" tests/test_deploy.py tests/test_doctor.py
```

非 POSIX 本机跑 `install` **必须**显式给 `--root <临时目录>`（默认 root 只在 POSIX 下是 `/`），
这是为了**防止误写开发机的 `/`**。

---

## 11. 需要老李一次性介入的步骤

以下是**本项目无法自行完成**的部分，需要你在拿到资源后执行：

1. **准备一台 Linux 主机**（Ubuntu 24.04 LTS / Debian 12，x86_64），并拿到 root 或 sudo。
   本项目不会替你买机器、不会调用任何云厂商 API。
2. **把仓库放到那台机器上**（`git clone`，或你手工上传），并获得执行 `tools/deploy_linux.sh` 的权限。
3. **决定网络暴露方式**（§7）：Tailscale/WireGuard 私网，还是反代 + TLS。
   这需要域名 / 证书 / 安全组，都要你操作。
4. **填写生产配置**：`/etc/li-iptv-aggregator/config.toml` 里的来源清单（`[[sources]]`）
   与（可选的）`[probe]` 设置 —— 里面会出现你的私密来源 URL，**不要提交进 Git**。
5. **可选**：装 ffmpeg（提供 ffprobe）并把 `[probe] enabled = true`，才能有真实测活。
6. **首次验收**：`sudo systemctl status li-iptv`、`curl -fsS http://127.0.0.1:8080/healthz`。

拿到主机后，把 §0 的三条命令跑通即完成部署。

---

## 12. 不受支持 / 范围外

多地区多探针协调、多主机选主、Kubernetes、Docker、Terraform / 云 IaC、
自动采购主机 / 改安全组 / 注册域名 / 托管证书私钥、视频代理 / 转码 / DVR、
EPG / Logo、Dashboard、selector 算法变更、schema V2。
