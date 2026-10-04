# TASK-007 Independent QA — Review 02

日期：2026-10-04
Reviewer：大G
受审 HEAD：`0e195eba74ca98980257b079e16ec952b1dd9716`
返工实现提交：`b9627af`
结论：**REJECT → QA-007A/B 主体已通过，仅剩 QA-007B-1 一处 service-state 三态归一化边界 → 定向返工 → REVIEW**

## 已确认通过

### QA-007A 已关闭

- `HealthResult` 继续保持 `frozen=True`。
- `check_once()` 已不再中途修改 frozen instance，改用局部 `detail`，最终一次性构造/replace。
- 大G独立运行 `tests/test_health.py`：**8 passed**。
- Review 01 的旧反例 `status=missing` 不再抛 `FrozenInstanceError`。
- 小W真机 `deploy status` 已从 traceback 变成结构化 `ok=False / status=missing / playlist=503`。

### QA-007B 主路径已关闭

- upgrade 已采用 service-aware doctor preflight；
- active + live lock 在只读升级 preflight 中可放行；
- stop 后 RuntimeDirectory 被 systemd 删除时会重建 `/run/li-iptv-aggregator`；
- rollback 走相同重建语义；
- 健康失败可正常进入 rollback，不再被 QA-007A traceback 打断；
- 小W已在 `ev-lab-shanghai` 真机跑通上述路径并证明 EV-Lab 未受影响。

### G-007C 已解除

Owner 已授权大G代做常规技术决策。大G已在 `d978b1f` 正式修改 TASK-007 验收口径：

- 不拿 dynamic_event_m3u 冒充 fixed_m3u；
- 真实 fixed-stream smoke 顺延到后续任务；
- 空库存下 `healthz=missing / live.m3u=503` 在 TASK-007 本轮视为符合 fail-closed 设计。

同时已登记第二个动态源 `korice-ppv`，不属于本轮 fixed smoke。

## 唯一剩余阻断 QA-007B-1：systemd service state 三态归一化过宽

`upgrade()` 当前代码：

```python
raw_active = service_probe.query().get("active")
if isinstance(raw_active, bool):
    service_active = raw_active
elif isinstance(raw_active, str) and raw_active.strip():
    service_active = raw_active.strip() == "active"
else:
    service_active = None
```

注释宣称语义是：

- `active` → True；
- `inactive/failed` → False；
- 无法确定 → None，保持严格。

但实际实现把**所有非空且不等于 active 的字符串**都变成 False。

大G独立执行同一映射逻辑，实际：

```text
active       => True
inactive     => False
failed       => False
activating   => False
deactivating => False
reloading    => False
maintenance  => False
unknown      => False
<empty>      => None
```

这会把 systemd 过渡态/未知态错误解释成“明确已经停止”，从而允许 doctor 对 RuntimeDirectory 缺失采用宽松语义。与 Review 01 要求的 fail-closed 三态设计不一致。

## 必须修复

只修这一处，不扩大范围。

推荐冻结：

```python
state = raw_active.strip().lower() if isinstance(raw_active, str) else raw_active
if state is True or state == "active":
    service_active = True
elif state is False or state in {"inactive", "failed"}:
    service_active = False
else:
    service_active = None
```

要求：

1. 仅 `active` 明确 True。
2. 仅 `inactive` / `failed` 明确 False（如果要额外接受其它状态，必须逐项证明其等价于稳定 inactive）。
3. `activating / deactivating / reloading / maintenance / unknown / 空 / 查询失败` ⇒ None，保持 doctor 严格判定。
4. 增加永久参数化回归覆盖上述状态。
5. 至少补一条 upgrade 回归：service query 返回 `activating` 或 `unknown` 时，不得把 `service_active_at_preflight` 记成 False；缺失 run_dir 必须继续阻断。
6. 现有 health / doctor / deploy / 全量测试继续零回归。
7. 无需再重复大规模真机安装；修复后可在现有 `ev-lab-shanghai` 做一次只读/低风险 preflight smoke，确认 active 正常映射即可。

## Gate

QA-007A 已通过，不再改。
QA-007B 的 RuntimeDirectory 主方案已通过，不再重构。
G-007C 已解除，不再等待 fixed 源。

完成 QA-007B-1 后更新 `REPORTS/TASK-007-REPORT.md`，状态回 REVIEW，commit + push 后停止。
禁止启动 TASK-008。
