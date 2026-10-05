# TASK-007 Independent QA — Review 04（最终验收）

日期：2026-10-05
Reviewer：大G
受审 HEAD：`8463a3c797d94aa817ce9e2230103bacdbf662db`
结论：**ACCEPT**

## 最终关闭项

### QA-007A
- health frozen dataclass 缺陷已关闭；
- `deploy status` 在 `missing/stale` 下返回结构化结果，不再 traceback。

### QA-007B
- upgrade / rollback 已采用 service-aware preflight；
- systemd RuntimeDirectory 在 stop 后被回收时可安全重建；
- health failure 可进入正常 rollback；
- 不再存在“active 时 lock fail / inactive 时 dirs fail”的互斥死结。

### QA-007B-1
- `_service_active_state()` 已冻结为 fail-closed 白名单三态：
  - active / True → True；
  - inactive / failed / False → False；
  - activating / deactivating / reloading / maintenance / unknown / 空 / None / 其它 → None。

### QA-007C
- `restore-db` 已复用同一个 `_service_active_state()`，不再维护第二套二值解释；
- 过渡态 / 未知态默认 BLOCKED；
- active → stop 后复查必须明确 False 才允许恢复；
- active → deactivating/unknown 继续 BLOCKED；
- inactive/failed 继续允许；
- `--force-offline-restore` 仅覆盖“判不出来”的 break-glass，不覆盖“明确 active”。大G认可该更保守边界。

## 大G独立确定性复验

直接调用 `_restore_service_gate()` 注入 service state，实际：

```text
inactive     blocked=False
failed       blocked=False
activating   blocked=True
deactivating blocked=True
reloading    blocked=True
maintenance  blocked=True
unknown      blocked=True
active -> inactive      blocked=False
active -> deactivating  blocked=True
active -> unknown       blocked=True
activating + force      blocked=False forced=True
active + force          blocked=True forced=False
```

Review 03 的旧反例已全部翻转。

## 测试与范围

- 小W本轮 restore-db 定向：**28 passed**。
- deploy + health + doctor：**156 passed**。
- 全量：**461 passed**。
- 负向验证回退旧二值逻辑：**14 failed / 14 passed**，证明回归测试能抓住旧缺陷。
- `git diff --check` 通过。
- 本轮未修改 health / doctor / runtime / backup / schema / deploy unit 模板。

## 真机验收边界

TASK-007 已真实落到 `ev-lab-shanghai`，完成过：

- Ubuntu/systemd 实机部署；
- 非 root `liptv` 运行；
- ownership/mode 实测；
- ffprobe 能力检查；
- localhost `/healthz` / `/live.m3u`；
- restart / SIGTERM；
- 第二实例 EXIT_LOCKED=3；
- SQLite backup；
- upgrade/rollback 真机 smoke；
- EV-Lab 零伤害核验。

fixed-stream 真实业务 smoke 已由 Owner 授权大G决定正式顺延；TASK-007 不以没有 fixed_m3u 阻断。
动态源 JSNZKPG 与 KORICE 继续按 `dynamic_event_m3u` 规则管理，不得冒充 fixed。

## 最终结论

**TASK-007 ACCEPTED。**

至此项目已具备：
- 本地/云端单机长期运行；
- systemd 托管；
- 固定频道真实 ffprobe 能力；
- 动态赛事源安全临时合入；
- fail-closed health / lock / restore / upgrade / rollback；
- 真实 Linux 部署与运维闭环。

TASK-008 尚未定义、未启动。
