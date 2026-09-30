# TASK-002 — Remote Source Fetch & Dynamic Sports Ingestion

状态：REJECTED（Review 02：仅剩 QA-002C；见 REVIEWS/TASK-002-REVIEW-02.md）
Owner：老李
Architect / Reviewer：大G
Executor：小W
基线：完成 TASK-001、包含个人使用来源原则的最新 `main`（开工先核 HEAD）
前置档案：`SOURCES/JSNZKPG-SPORTS.md`
禁止提前启动 TASK-003。

## 本轮目标

在 TASK-001 已验收的骨架上，完成**真正的远程 M3U 获取、校验、来源生命周期管理**；首次支持 JSNZKPG 等动态赛事源，但暂不承诺线上统一 /live.m3u 或 24 小时无人值守。可单独订阅上游原地址，不必等本轮。

## 两类源严格分离

**fixed_m3u：** 普通相对稳定频道的公开 URL；按现有 `source` / `source_channel` 归档。抓取成功才更新，解析失败不得改变旧库存量；同一批来源中已消失的条目仅对该来源置 `active=0`，不得硬删，不得影响其他来源的条目、绑定、stream 或 probe 历史。再出现时恢复 `active=1` 并尽可能保留身份。

**dynamic_event_m3u：** 动态赛事列表（首个登记案例 JSNZKPG），不导入 `canonical_channel` 或 `stream` 的长期固定频道流程。每次显式获取都重新请求原始 URL 并作 M3U 格式校验，返回本次临时事件列表及摘要；允许临时文件用于本地测试/客户端手动使用，但不能把内含短时签名 URL 的快照提交 Git、长期存入 SQLite 或当作稳定来源。不得发明“赛事持续时间”或通过猜测签名算法续期。

## 交付

1. 在配置/CLI 中注册两类源（不新增重型框架或数据库业务表）；保留 `source.kind` 等现有架构兼容性。示例配置必须有**禁用的、仅演示用途的 JSNZKPG 条目**或独立示例命令，防止 `fetch --all` 默认向其发请求。
2. 实现最小 HTTP(S) 拉取：标准库优先；配置化 connect/read 或总超时、最大响应大小、有限重定向、合适 User-Agent；HTTP 非 2xx、网络错误、解码失败、不合法/空列表须分类失败，不得污染当前源数据。**禁止**对视频流 URL 发起播放/试探请求，本轮仅获取被配置的 M3U 文本。
3. fixed 类型完成 fetch → parse → 原子更新**单一来源**的 source_channel：本次出现的更新/创建、消失的设 inactive、重新出现的恢复；重复抓取幂等；保留第一次发现/既有绑定/历史。对一个来源处理失败不阻塞其他来源；全批次输出分源结果及成功/失败数量。
4. dynamic 类型：只从入口 M3U 实时获取，返回 parse 后的临时结果（包括赛事显示名、group-title、线路类型原样文本、当次条目数量）；默认摘要输出**不得展示完整带签名 URL**，如指定 `--out` 保存原始/规范化 M3U，仅写入被 .gitignore 保护的临时/运行目录，带明确“短时快照”提示。不得默认自动混入固定频道输出。
5. CLI 至少能：注册远程来源；单源 fetch；全部 enabled 的 fixed 来源 fetch；针对动态赛事源显式单源 fetch/preview；查看来源 fetch 状态。命令名由小W提出，但要保持 `python -m liptv` 入口兼容。
6. 提供 1 个本地无真实网络的 HTTP mock/server 演示：成功、变更、消失、恢复、HTTP 错误、空/损坏 M3U、动态赛事带签名 URL 场景。默认自动化测试**不依赖 JSNZKPG 公网在线**。
7. 对 JSNZKPG 仅进行一次可选手工 smoke：GET `https://jsnzkpg.de5.net/all.m3u`、验证头/解析条目数；允许公网不可达时标记 `NETWORK_UNAVAILABLE`，不得因此把离线测试判失败；不抓底层赛事视频，不留存抓到的完整播放 URL 到报告/Git。
8. README 更新 CLI 使用范例，`REPORTS/TASK-002-REPORT.md` 逐条记录验收证据、测试命令、退出码、diff、提交 SHA，更新 `PROJECT_LOG.md` 本轮进度。

## 验收门槛

1. 全部 TASK-001 原 70 个测试必须通过，不允许回归。
2. 新增测试覆盖：fixed 首抓、重复幂等、更新/消失/inactive、恢复 active、不同源隔离、失败不污染、响应大小及超时限制；dynamic 临时解析、拒绝进入 fixed inventory、避免日志暴露带签名 URL。
3. 两类源状态明确；禁用源不被 `fetch --all` 请求；动态源**不会因为曾经出现过而永久留在 /live.m3u**。
4. 操作过程可复现；失败有分类、摘要和退出码；单一来源更新具备数据库事务边界。
5. `git diff --check` 干净，测试实际运行成功，提交并推送后 `main` 与远端 HEAD 一致。

## 停止条件

- 为本任务需要破坏 TASK-001 已冻结的数据身份/关联设计；
- 动态赛事源必须通过持久化短时签名 URL 才能实现；
- 拉取失败却可能覆盖已成功导入的来源库存或当前发布文件；
- 需要破解/逆向/绕过认证、复制第三方私密令牌或代理视频；
- 执行期间发现直接影响安全/数据完整性的不可控问题。

满足任一条立即记录问题并停在 REVIEW，不要临时绕过或扩充范围。

## 不包含

正式统一 `/live.m3u` 合并发布；自动调度；多节点真实 ffprobe；部署腾讯云；EPG/Logo 爬取；自动赛事识别；历史动态赛事库；视频转码/代理；GUI。不要把本轮扩大成完整产品。

## Gate

任务结束只允许状态 `REVIEW`，小W填写报告并 push 后停止；只有大G独立测试与源码审查可给出 ACCEPT。验收前不启动 TASK-003。
