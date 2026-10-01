# TASK-003 Independent QA — Review 02（最终验收）

日期：2026-10-01
Reviewer：大G
受审 HEAD：`d1e2c4237e4ebcf4743480f4cc1a5572ee5f624d`
返工实现提交：`9b445526623a2e5f3e79edc573ca25609c2dcc61`
结论：**ACCEPT**

## 独立核验

- QA-003A：默认动态过滤已改为“排除宣传/推广/回放，其他联赛分组默认纳入”；同时解析 `# ===== 正在直播 =====` / `# ===== 赛事回放 =====` 注释分区，避免将回放误纳。
- 大G于 2026-10-01 仅拉取 JSNZKPG 入口 M3U 文本复验，未请求任何底层播放 URL：本轮样本 **58 条**，其中默认纳入 **9 条**（WNBA、玻利杯、球会友谊、美乙、哥伦甲），排除 **48 条赛事回放 + 1 条 TG 推广**。说明真实目标源不再出现“全部 0 纳入”的阻断。
- QA-003B：大G独立构造双动态源场景（good=1 条合成赛事，bad=TIMEOUT），真实 `publish()` 写入临时目录，结果：
  ```
  STATUS DEGRADED_FIXED_ONLY FIXED 1 DYNAMIC 0 DISCARDED 1 DYNAMIC_FILE False EXIT 0
  ```
  状态、计数和实际文件一致；失败时本轮其他动态成功条目也被舍弃，真正只发布固定频道。
- 源码审阅确认 `M3UEntry.section`、分区解析、动态排除规则和 fail-closed 收窄均限定在本轮职责范围，没有修改 schema 或 TASK-001/002 已冻结的身份/网络安全边界。

## 独立测试

Windows Python 3.13.15，禁用 pytest 自动插件，所有组使用不同全新 `basetemp`。五组互不重复，全部 exit 0：

1. `tests/test_publish.py`：**36 passed in 12.21s**
2. `tests/test_m3u.py`：**20 passed in 0.17s**
3. 旧 core（schema/repo/select/cli/identity）：**52 passed in 10.76s**
4. fetch/dynamic/remote_ingest：**58 passed in 15.08s**
5. Review 01/02 历史 QA：**23 passed in 5.97s**

合计 **189/189 passed**。

此前较大的合并命令在 Windows Runner 上存在长时间运行/超时噪声，因此本报告只声明上述五组完整独立覆盖，不把未返回的合并命令作为成功证据。

## Gate

**TASK-003 ACCEPTED**。

本轮已完成：
- 固定频道 + 动态赛事单次本地统一 M3U；
- 真实 JSNZKPG 联赛分组兼容；
- 宣传/回放过滤；
- 多动态源任一失败时真正 fail-closed 到 fixed-only；
- 动态签名不从旧列表回拼；
- 原子发布、previous 回滚及摘要脱敏。

本次 ACCEPT **不表示**已经实现自动调度、云端托管、24 小时稳定订阅或真实多节点播放质量测活；这些必须另立 TASK。

TASK-004 尚未定义、未启动。
