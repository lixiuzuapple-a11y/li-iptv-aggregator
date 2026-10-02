# TASK-006 Independent QA — Review 01

日期：2026-10-02
Reviewer：大G
受审 HEAD：`a4c2ffcd7a83cf64ce9baece2bc5f36760e9a291`
实现提交：`e1a6dc3ce03d33726f68a4f60f639edcae54641b`
结论：**REJECT → 两处生产安全边界定向返工 → REVIEW**

## 已确认的成果

- 已实现 Linux 单机部署内核、systemd unit、生产配置模板、doctor、SQLite 在线备份、install/upgrade/rollback、healthcheck、Caddy 示例与离线部署 demo。
- 小W报告提交前全量：**385 passed / exit 0**；离线 demo：**73/73**。
- 实机部署如实标注 **NOT EXECUTED**，没有伪造云主机/systemd 实机成功。
- 本轮拒绝依据来自两个可直接从生产代码与离线权限意图复现的安全问题，不依赖 Runner 超时。

## 阻断 QA-006A：应用代码与生产配置属主实际被设置成 liptv:liptv，与冻结权限模型相反

TASK-006 冻结的生产模型要求：

- `/opt/li-iptv-aggregator` / releases / venv：**root:root**，服务用户只读；
- `/etc/li-iptv-aggregator`：**root:liptv**；
- `/etc/li-iptv-aggregator/config.toml`：**root:liptv 0640**；
- `/var/lib` / `/var/cache` / `/run`：`liptv:liptv`。

执行报告 §2 也明确写了上述归属。

但实际 `Layout.mode_table()` 先统一定义 `owner = f"{self.user}:{self.group}"`，随后把 app、release、releases、venv、etc、config、current、deploy-state 全部赋成这个 owner；默认即 `liptv:liptv`。`_apply_modes()` 会逐项调用 `rec.chown()`，而 `DeployOptions.chown` 默认 **True**，所以在真实 Linux root 安装时会真的执行这个 chown。

大G独立打印权限意图，实际结果：

```text
ROOT/opt/li-iptv-aggregator                    liptv:liptv 0755
ROOT/opt/li-iptv-aggregator/releases/r1        liptv:liptv 0755
ROOT/opt/li-iptv-aggregator/releases           liptv:liptv 0755
ROOT/opt/li-iptv-aggregator/venv               liptv:liptv 0755
ROOT/etc/li-iptv-aggregator                    liptv:liptv 0750
ROOT/etc/li-iptv-aggregator/config.toml        liptv:liptv 0640
ROOT/opt/li-iptv-aggregator/current            liptv:liptv 0644
ROOT/opt/li-iptv-aggregator/deploy-state.json  liptv:liptv 0640
ROOT/etc/systemd/system/li-iptv.service        root:root 0644
```

这意味着低权限服务账号拥有应用 release/venv 与生产配置。`ProtectSystem=strict` 只能限制 systemd 服务进程在该 unit sandbox 里的写权限，不能把文件系统所有权从 `liptv` 变回 root，也不能防止同一账号在 unit 外被利用后修改代码/配置。

### 必须修复

1. 把生产属主模型真正落到代码：
   - app_dir / releases_dir / release_dir / venv_dir / current / deploy-state：`root:root`；
   - etc_dir / config.toml：`root:liptv`（或等价最小权限，必须给出理由）；
   - lib/cache/run/backups：`liptv:liptv`；
   - systemd unit：`root:root`。
2. `_create_layout_dirs()` 与 `mode_table()` 必须一致，不能一个“创建时意图 liptv”、一个“报告 root”。
3. `--no-chown` 仍只用于离线演练；真实 root install 默认必须应用上述属主。
4. 增加永久回归：明确断言每个关键路径的 owner/mode；至少 app/release/venv/config/data/unit 六类。
5. 执行报告中的权限表必须由代码测试证据支撑，不能手写成与实现不一致。

## 阻断 QA-006B：restore-db 允许在服务运行时直接替换 SQLite，只有提示、没有强制停机保护

`Deployer.restore_db()` 当前实现直接：

```python
result = backup_mod.restore_sqlite(backup_path, self.layout.db_path, allow_overwrite=yes, ...)
```

然后才在 notes 里写：

```text
服务在恢复期间应处于停止状态。
```

它既不查询 systemd/process service 状态，也不 stop 服务，更不拒绝“服务仍 active”的恢复。

这在 SQLite 生产运维里是不安全的：运行中的 scheduler 可能已经打开数据库连接；`restore_sqlite()` 又通过临时文件替换目标 DB。命令表面成功后，磁盘路径可能指向恢复后的新文件，而旧进程继续持有旧 inode/旧连接并写入，造成运行状态与磁盘文件分叉。事后一句 note 不能构成保护。

另外 `backup.restore_sqlite()` 的 `safety_copy` 对现有 DB 使用 `target.read_bytes()` 直接复制；如果服务仍在写，这份所谓 pre-restore 安全副本也不是 SQLite 一致性快照。

### 必须修复

1. `restore-db` 必须 **fail-closed**：默认只允许在确认服务已停止时执行。
2. 推荐语义：
   - 查询当前部署使用的 service manager；
   - 如果 service active，先明确 stop 并确认 inactive；stop 失败则拒绝恢复；
   - 恢复完成后是否自动 restart 要显式冻结。更保守可默认不自动启动，并在输出中明确要求人工 `start`；若选择自动重启，必须健康检查。
3. 如果无法可靠判断 service 状态，默认拒绝，除非提供一个明确的 break-glass 参数（如 `--force-offline-restore`）；不能只靠 `--yes` 代表“服务一定停了”。
4. pre-restore safety copy 必须使用 SQLite 一致性方式：可复用 `backup_sqlite()` / sqlite backup API，或在已确认服务停止后才允许原始文件复制。推荐统一走 SQLite backup API。
5. 新增永久回归：
   - active service + restore-db ⇒ 必须 stop 后才恢复，或明确拒绝；
   - stop 失败 ⇒ DB 字节不变；
   - restore 成功 ⇒ 备份可验证、schema 正常；
   - 不允许服务 active 时静默替换数据库。

## 返工范围

- 只修 QA-006A / QA-006B。
- 不重构 selector、probe、publish、HTTP、schema。
- systemd 其它 hardening、doctor、upgrade/rollback 主链、Caddy 示例保持现有成果。
- 原有 385 项测试必须零回归，并新增权限矩阵与安全 restore 回归。
- 更新 `REPORTS/TASK-006-REPORT.md`，TASK-006 状态改回 REVIEW，commit + push main 后停止。
- 禁止启动 TASK-007。
