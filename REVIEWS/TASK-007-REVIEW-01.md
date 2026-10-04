# TASK-007 Independent QA — Review 01

日期：2026-10-04
Reviewer：大G
受审 HEAD：`c9400b8e416eea434ef042744f815f1d5d67e912`
结论：**REJECT → 两处真机缺陷定向返工 + 一个 fixed-stream Gate 待 Owner 决策 → REVIEW**

## 已确认的成果

- Owner 已明确授权在 `ev-lab-shanghai` 上部署 IPTV，Phase B 因此具备授权前提。
- 小W在真实 Ubuntu/systemd 主机完成了 ffprobe 安装、部署 plan/install、真实 ownership stat、systemd start/restart、EXIT_LOCKED、SQLite backup、localhost HTTP 路由与 EV-Lab 隔离检查。
- 真实 service 已运行在 `127.0.0.1:8080`；空库存下 `/healthz` 返回 200 + `status=missing`，`/live.m3u` 返回 503，符合“不能用空列表冒充成功”的冻结语义。
- 第二实例真实退出码 3，原实例 PID/NRestarts 不变；DB restart 前后 sha256 一致。
- EV-Lab unit checksum、数据目录与健康 timer 未被本任务修改；报告给出的零伤害证据自洽。
- 外部网络暴露未获授权，未修改安全组/防火墙/DNS/TLS，处理正确。

## 阻断 QA-007A：`health.check_once()` 在非 ok 业务状态下修改 frozen dataclass，生产状态查询/upgrade 健康闸门会崩

`HealthResult` 明确是 `@dataclasses.dataclass(frozen=True)`，但 `check_once()` 里仍然执行：

```python
result.detail = ...
```

只要 `/healthz` HTTP 200 且业务 `status != ok`、同时初始 detail 为空，就会抛 `FrozenInstanceError`。

大G独立最小反例：模拟 `/healthz => 200, {"status":"missing"}`，实际输出：

```text
EXC FrozenInstanceError cannot assign to field 'detail'
```

这与小W真机报告的 traceback 完全一致，因此不是环境噪声。

### 影响

- `python -m liptv deploy status` 在 `missing/stale` 场景会崩；
- `Deployer._wait_health()` / `upgrade()` 遇到业务未就绪时可能直接 traceback，而不是返回结构化不健康结果；
- 健康闸门失去“失败可回滚”的语义。

### 必须修复

1. `check_once()` 不得就地修改 frozen instance；使用局部 `detail` 变量，最后一次 `dataclasses.replace(...)` 返回。
2. 增加永久回归：
   - status=missing；
   - status=stale；
   - health JSON 不可解析；
   - `/live.m3u` 503；
   均返回 `HealthResult(ok=False)`，不得抛异常。
3. `deploy status` 在真实/模拟 missing 状态下必须返回结构化结果与非崩溃退出。
4. upgrade 健康失败必须走正常 `HEALTH_FAILED / rollback` 流程，不能因 health helper traceback 中断。

## 阻断 QA-007B：真实 systemd `RuntimeDirectory` 与 upgrade/rollback preflight 互相矛盾

systemd unit 使用：

```text
RuntimeDirectory=li-iptv-aggregator
```

因此服务停止后 `/run/li-iptv-aggregator` 会由 systemd 自动删除。

当前 `upgrade()` 在正式升级前调用 `doctor.collect(...)`；doctor 的 `dirs` 又要求 runtime/lock 目录存在且可写。结果在真机形成不可同时满足的两态：

- 服务运行中：doctor lock 检查失败（活实例持锁）；
- 服务停止后：`/run/li-iptv-aggregator` 被 systemd 删除，doctor dirs 检查失败。

小W已在真机两次复现。源码与 unit 结构也确认该矛盾真实存在。

### 必须修复

采用最小、可解释的方案，优先：

1. doctor 对“由 systemd RuntimeDirectory 管理的 `/run/li-iptv-aggregator`”采用条件语义：
   - 服务 active 时目录应存在；
   - 服务 inactive 时目录缺失是**正常**，不得判 dirs fail；
   - 其它 data/cache/config 目录仍严格检查。
2. upgrade/rollback 流程需要有一致的 service-aware preflight：
   - 允许在服务 active 状态做只读 preflight，但 lock=held_by_live_process 不应被当成升级阻断；
   - 真正切换 release 前再 stop；
   - stop 后 RuntimeDirectory 缺失不得阻断。
3. 不建议把 lock 永久移出 `/run`，除非证明前两种最小修复不可行。
4. 新增真实语义回归：
   - service active + live lock → upgrade preflight 可继续到 stop；
   - stop 后 run_dir 消失 → upgrade 仍可安装/启动新 release；
   - upgrade 健康失败 → 自动 rollback A；
   - rollback 在相同 RuntimeDirectory 语义下可工作。

## Gate G-007C：真实 fixed stream smoke 未完成

TASK-007 最低验收明确要求：

- 至少 1 条**已授权 fixed_m3u**；
- 至少 1 条真实 ffprobe；
- probe_result 新增；
- selector 使用真实 probe 历史；
- 至少 1 条 canonical 进入实际 `/live.m3u`；
- `/healthz` 最终达到 `status=ok`，`/live.m3u` HTTP 200 且非空。

当前生产配置 sources 为空；唯一公开登记源 JSNZKPG 是 dynamic_event_m3u，按项目规则不能拿来冒充 fixed 源。小W选择停 Gate 是正确的。

### Owner 决策

二选一：

1. 老李提供/指定一个明确允许用于本项目的 fixed_m3u 源，小W在**服务器本地配置**中加入（不入 Git、不写报告完整 URL），补跑真实 fixed smoke；
2. 老李明确决定 TASK-007 只验“空库存 fail-closed + 基础部署”，则需要**修改 TASK-007 验收口径**，把 fixed smoke 顺延到后续任务。未修改任务书前，大G不能把当前状态判 ACCEPT。

默认不擅自降低验收标准。

## 返工范围

- 修 QA-007A：health frozen dataclass。
- 修 QA-007B：upgrade/rollback 与 RuntimeDirectory 的 service-aware preflight。
- 不修改 selector、probe 核心算法、schema、HTTP 路由语义。
- 修复后在 `ev-lab-shanghai` 上真实重跑：
  - deploy status；
  - upgrade A→B；
  - rollback B→A；
  - health failure rollback；
  - restart / EXIT_LOCKED 再确认。
- EV-Lab 零伤害证据继续保留。
- fixed stream Gate 按 Owner 决策执行；未获 fixed 源时不得伪造。
- 完成后更新 `REPORTS/TASK-007-REPORT.md`，状态改回 REVIEW，commit + push main 后停 Gate。
- 禁止启动 TASK-008。
