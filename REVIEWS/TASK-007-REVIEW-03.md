# TASK-007 Independent QA — Review 03

日期：2026-10-04
Reviewer：大G
受审 HEAD：`e4ba7256f46b6230b4249bbb397ce55892fa905d`
结论：**REJECT → QA-007B-1 已通过，但发现 restore-db 同源 fail-closed 缺陷；只剩这一条定向返工 → REVIEW**

## 已确认通过

### QA-007B-1 已关闭

- `_service_active_state()` 已改成 fail-closed 白名单三态：
  - `active` / True → True；
  - `inactive` / `failed` / False → False；
  - `activating / deactivating / reloading / maintenance / unknown / 空 / None / 其它` → None。
- 小W报告给出 22 个参数化输入 + 6 个过渡态 upgrade 回归 + 1 个 failed 对照。
- 小W全量：**441 passed / 0 failed**。
- 负向验证把旧 bug 放回去后有 **12 failed**，说明新回归确实能抓住旧缺陷。
- QA-007A / QA-007B 主方案 / G-007C 均继续保持通过或解除状态。

## 唯一剩余阻断 QA-007C：restore-db 仍把 systemd 过渡态/未知态当成“已停”，违反 QA-006B fail-closed

小W在 §37.1 主动披露：`restore-db` 的 `_restore_service_gate()` 仍使用旧式二值判断：

```python
if state.get("active") == "active":
    ... stop + recheck ...
return {"blocked": False, ...}
```

也就是说，只要不是精确字符串 `active`，就直接落到“允许恢复”。

这与 QA-006B 已冻结语义冲突：

> 判断不了服务状态，默认拒绝恢复；`--yes` 不代表服务已停。

### 大G确定性复现

直接给 `_restore_service_gate()` 注入 managed=True 的不同 systemd 状态，实际输出：

```text
inactive     blocked=False
failed       blocked=False
activating   blocked=False   ← 错
deactivating blocked=False   ← 错
reloading    blocked=False   ← 错
maintenance  blocked=False   ← 错
unknown      blocked=False   ← 错
```

因此不是静态代码审美问题，而是真正的生产数据保护漏洞：服务处于启动/停止过渡态时，restore-db 可能直接替换 SQLite。

## 必须修复

只修 restore-db 门禁，不扩大范围。

### 推荐方案

复用已经通过 Review 03 的 `_service_active_state()`，不要再维护第二套状态解释。

冻结语义：

- True：服务明确 active → 先 stop，再复查。
- False：仅明确 inactive/failed → 允许恢复。
- None：过渡态/未知态/查询异常 → **默认 BLOCKED**，除非显式 `--force-offline-restore` break-glass。

stop 后复查也必须复用 `_service_active_state()`：

- 只有复查明确 False 才算停机成功；
- True 或 None 都拒绝恢复。

### 永久回归

至少新增：

1. 参数化 `activating / deactivating / reloading / maintenance / unknown`：restore-db 必须 BLOCKED，DB hash 不变。
2. active → stop 后状态变 `deactivating`：必须 BLOCKED，不能恢复。
3. active → stop 后状态变 `unknown`：必须 BLOCKED。
4. inactive / failed：继续允许恢复。
5. `--force-offline-restore`：仍可显式 break-glass，并在 payload/notes 中标明 forced。
6. 现有 QA-006B restore 测试全部零回归。

## Gate

- QA-007A：通过，不再改。
- QA-007B RuntimeDirectory 主方案：通过，不再改。
- QA-007B-1 三态归一化：通过，不再改。
- G-007C fixed smoke：已顺延，不阻断。
- 本轮不要求再做大规模真机安装。

修完 QA-007C 后：

- 跑 restore-db 定向回归 + deploy 全量 + 全量测试；
- 可在 `ev-lab-shanghai` 只做只读 service-state smoke，不要真的 restore 生产 DB；
- 更新 `REPORTS/TASK-007-REPORT.md`；
- TASK 回 REVIEW；
- commit + push 后停止；
- 禁止启动 TASK-008。
