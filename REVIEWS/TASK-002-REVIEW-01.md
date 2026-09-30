# TASK-002 Independent QA — Review 01

日期：2026-09-30
Reviewer：大G
受审 HEAD：`ddf07a0ef9aaeb5024163333d02a5a5623109de4`（实现提交 `e90fdb2`，随后仅补交报告 SHA）
结论：**REJECT → 定向返工 → REVIEW**

## 本轮已核实

- 远程 GitHub 文件、TASK-002 REPORT 和实际代码已同步读取；本地与远程同为 `ddf07a0`，原工作区干净。
- 大G独立审阅 `liptv/fetch.py`、`liptv/ingest.py`、`liptv/repo.py`、`liptv/cli.py` 及测试。
- Windows WebCodex 上 Python 3.13.15、pytest 9.1.1；禁用非项目 pytest 插件、分为三个全覆盖且不重叠的测试组，三组均 **exit 0**：
  - TASK-001 原六个文件：70 passed in 9.82s；
  - TASK-002 tests/test_fetch.py + tests/test_dynamic.py：36 passed in 7.74s；
  - TASK-002 tests/test_remote_ingest.py：22 passed in 8.90s。
  - **合计 128/128 个用例独立通过**。之前一次整套命令在 Windows Runner 上超时、旧离线演示目录清理受限；分组测试成功，环境噪声不作为代码缺陷，亦不声称该次全量单命令成功。
- HTTP 获取有限制，动态赛事默认只生成脱敏摘要、与固定频道库存隔离等常规路径已实现并经现有测试覆盖。

## 阻断 QA-002A：部分截断的有效前缀被当成完整快照，静默下线原频道

`liptv/ingest.py::validate_m3u_text()` 仅检查 `parsed.entry_count > 0` 就立即接受，忽略解析器报告的 `extinf_without_url`。随后 `apply_source_snapshot()` 把本次未出现的旧条目标记为 inactive。

大G独立编写并运行的反例，使用真实 SQLite + monkeypatch 仅替换 HTTP 返回文本，未改仓库文件：

1. 首次正常列表有 Alpha、Beta、Gamma 三条，拉取 `ok`，均为 active=1；
2. 第二次入口 HTTP 200，但正文只含 Alpha 的完整条目，然后以 `#EXTINF:-1,Beta` 结束（最后一条声明没有配套 URL），属明显不完整 M3U；
3. 当前代码输出：
```
BEFORE ok
PARTIAL ok accepted True deactivated 2
CHANNELS [('Alpha', 1), ('Beta', 0), ('Gamma', 0)]
```

这不是合理的正常删台：第二份文本在 `#EXTINF` 后意外终止，解析器自身已能标记 `extinf_without_url`。在快照驱动的删除/置 inactive 机制里，将其当作完整成功快照会静默误下线旧频道。该风险直接影响任务目标“失败不得污染库存”。

**必须修复：** fixed 来源完整性校验至少拒绝“仍有未配 URL 的 `#EXTINF`”等明显截断场景；返回可枚举错误、记录失败状态并保证原库存逐字段不变。不能把“列表条目减少”一概拒绝：正常、结构完整的真正删台仍须正常置 inactive。新增长期回归用例覆盖“损坏的部分成功前缀”和“真正完整缩减”两条分支。

## 阻断 QA-002B：动态快照的忽略保护并未真正强制执行

`liptv/ingest.py::write_dynamic_snapshot()` 只检查输出文件位于任意配置的 `allowed_dir` 下；但 `fetch.dynamic_tmp_dir` 可以由配置覆盖成项目中**未被 .gitignore 忽略**的目录（例如 `SOURCES/`）。即使当前默认 `out/tmp` 安全，配置错误时仍允许把带签名播放地址的完整文件保存进 Git 跟踪范围。这与 TASK-002 明确的“仅写入 .gitignore 保护的临时/运行目录”不符。

**必须修复：** 不可把“位于用户自定 allowed_dir 内”直接等同于“受忽略保护”。在写文件前验证实际路径确实被仓库 Git 忽略规则覆盖，或把保存行为约束到代码固定且已验证忽略的专用目录（并对覆盖配置实施校验）。若非 Git 仓库运行环境，采用明确、可测试的安全路径策略，不可默默假定忽略有效。新增至少一个负向用例：将动态目录配置为仓库内未被忽略的路径时必须拒绝，且不创建快照。

## 返工范围与 Gate

1. 仅定向修复 QA-002A/B；无关模块不重构。
2. 保留 128 个现有测试全部通过，补充边界回归与必要文档/配置说明。
3. 记录大G两项反例的重跑输出、总测试数和退出码至 `REPORTS/TASK-002-REPORT.md` 的 Review 01 返工节。
4. 本轮新增反例与测试必须使用离线 mock，不依赖公网赛事源。
5. 状态由小W返工后改为 `REVIEW`，commit + push 后停 Gate；**不得自行 ACCEPT，不启动 TASK-003**。

建议修复后继续使用分组测试且各组采用不同的全新 basetemp，避免 Windows 沙箱批量清理误报。若声称“单命令 128+ 全过”，则需该命令本身 exit 0 的证据。
