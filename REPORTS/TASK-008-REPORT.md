# TASK-008 Execution Report

状态：PENDING_EXECUTOR
Owner：老李
Executor：小W
Reviewer：大G
基线：TASK-007 ACCEPTED（REVIEWS/TASK-007-REVIEW-04.md）

## 1. 基线与变更
记录基线 SHA、实现 SHA、变更文件和冻结模块。

## 2. Dynamic failure policy
说明 all_or_nothing / isolate 的最终语义、配置与状态码。

## 3. Multi-source composition
记录逐源 fetch/filter/publish 计数、跨源 exact duplicate 与 display collision 处理。

## 4. require_dynamic
证明 isolate 模式下以“至少一条本轮新鲜动态条目”为准。

## 5. 隐私与脱敏
验证 summary/runtime/journal 无 query/token/raw M3U。

## 6. Offline E2E
两源成功、单源失败、双源失败、0 条、重复、同名异 URL、malformed、timeout、旧动态不得回流。

## 7. JSNZKPG real-source smoke
只记录结构/计数/脱敏摘要，不粘播放 URL。

## 8. KORICE real-source smoke
只记录结构/计数/脱敏摘要，不粘播放 URL。

## 9. Production deployment
ev-lab-shanghai 上 IPTV release/config 变更、systemd restart、15min scheduler。

## 10. Production dynamic smoke
两源真实 fetch/publish、单源人工失败、health/live.m3u 结果。

## 11. Last-known-good
证明双源全失败时不覆盖当前文件，last_success_publish_at 不推进，组合层不读取旧动态 URL。

## 12. EV-Lab zero-harm
unit checksum、timer、数据目录、系统 Python、端口对照。

## 13. Tests
基线 461、新增数、总数、命令、exit code、flaky 如实记录。

## 14. Risks / NOT EXECUTED
公网访问、fixed 源、TLS、EPG、代理等明确标记。

## 15. Git & Gate
git diff --check、本地/远端 SHA；最终只置 REVIEW，禁止 TASK-009。
