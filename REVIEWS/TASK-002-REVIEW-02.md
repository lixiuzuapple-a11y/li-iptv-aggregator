# TASK-002 Independent QA — Review 02

日期：2026-09-30
Reviewer：大G
受审 HEAD：`4c51c042251f957673505e54f571dc88d607fc82`；返工实现提交：`6aecd258c7008016b74a64223fa4d815ba8748a9`
结论：**REJECT（仅剩 QA-002C 一处极小安全边界）→ 定向补丁 → REVIEW**。

## 已独立确认通过

- QA-002A：`validate_m3u_text()` 检查未配 URL 的 `#EXTINF` 并返回 `INVALID_M3U`；损坏的部分成功快照不再用于置 inactive。正常、结构完整的缩减依然保留正常删台语义。
- QA-002B 原问题：目标在 Git 工作树内、但不被 .gitignore 覆盖时写盘会拒绝；默认忽略目录仍能写入。
- 小W `tools/qa002_repro.py` 由大G独立运行 **6/6 PASS、exit 0**。
- 全部测试由大G使用 Windows Python 3.13.15 / pytest 9.1.1 关闭自动插件，按互不重复的四组及独立临时目录重跑，**141/141 全过、各组 exit 0**：原 TASK-001 70；fetch+dynamic 36；remote ingest 22；新增 Review 01 用例 13。全套单命令曾在 Runner 上超时，故只声称四组完整覆盖，不声称本人已取得单命令 exit 0。

## 最后一处阻断：QA-002C，已跟踪文件不能靠 .gitignore 保护

`liptv/ingest.py::write_dynamic_snapshot()` 将“文件路径匹配 .gitignore”直接解释为“Git 不会跟踪该文件”。然而 Git 对**已跟踪**的文件不再应用 .gitignore；例如先使用 `git add -f out/tmp/signed.m3u`，之后再写该文件，新的动态签名内容会成为待提交的已跟踪变更。

大G使用**真实临时 Git 仓库**独立复现（未改动本项目仓库）：
```
git init
echo out/ > .gitignore
mkdir out/tmp
echo OLD > out/tmp/signed.m3u
git add -f out/tmp/signed.m3u
# 然后调用 write_dynamic_snapshot(..., out/tmp/signed.m3u, allowed_dir=out/tmp)
BEFORE_TRACKED True
WRITE_ALLOWED True REMAINS_TRACKED True
```

这直接违反 TASK-002 “动态快照不得误入 Git 版本库”的实际保护目标。该漏洞不影响其他业务功能，但应在结束本任务前关闭。

### 唯一返工要求

1. 对工作树内目标，写盘前除了验证路径匹配忽略规则，还必须**拒绝已经被该 Git 工作树跟踪的路径**（例如用 `git ls-files --error-unmatch -- <path>` 或其他与 Git index 一致的机制）。不得覆盖已跟踪文件。实现应正确处理带空格的 Windows/OneDrive 路径和 nested worktree；不能依赖输出包含私密内容。
2. 至少新增一项**真实临时 Git 仓库**回归：已忽略目录中的文件先通过 `git add -f` 进入 index，随后快照写入必须拒绝且旧文件原字节不变；保留“未跟踪且确实忽略的文件可写”正向测试。
3. 不改动网络抓取、数据模型或原任务范围；不得为这个补丁引入复杂 Git 框架。README/报告记录这个边界及实际测试证据。
4. 既有 141 项测试继续全过；完整测试结果、独立反例、diff、Git SHA 加入 `REPORTS/TASK-002-REPORT.md` Review 02 补丁节。
5. 完成后状态回 `REVIEW`，commit + push 后停 Gate；**禁止提前启动 TASK-003**。

备注：本报告仅要求阻止明确可复现的误提交路径，不要求扩展为全面 Git 安全产品；相关文件不在工作树内时保持已有行为。
