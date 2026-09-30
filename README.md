# Li IPTV Aggregator

个人自用 IPTV 源聚合与自动维护项目。

## V1 目标

长期提供一个固定的 M3U 订阅 URL：

```
/live.m3u
```

后台自动完成：
- 拉取多个公开/授权 IPTV 来源
- 频道归一化与去重
- 保存同频道的多条 stream
- 多探针测活
- 基于历史稳定性选择最优线路
- 生成并原子发布 M3U
- 失败时保留 last-known-good

播放器直接访问实际 stream，本项目不做视频中转或转码。

## V1 原则

- 个人自用
- 最小可用
- SQLite
- CLI / 定时任务优先
- 不做 Dashboard
- 不做用户系统
- 不做转码/DVR/VOD
- 能复用成熟组件就不重造

## 快速上手

要求 Python 3.11+（仅用标准库）。

```bash
# 初始化数据库
python -m liptv init-db

# 导入本地 M3U
python -m liptv import-m3u examples/source_a.m3u --source "src-a"

# 建立归一化频道与绑定
python -m liptv canonical-add --name "CCTV-1 综合" --category 新闻
python -m liptv binding-add --source-channel-id 1 --canonical-id 1
python -m liptv stream-sync

# 选线并生成订阅
python -m liptv select --all
python -m liptv generate-m3u
```

运行测试：`python -m pytest -q`

## 远程来源（两类，严格分离）

| kind | 用途 | 是否进固定频道库存 |
|---|---|---|
| `fixed_m3u` | 相对稳定的公开频道列表 | 是（source_channel → canonical → stream） |
| `dynamic_event_m3u` | 动态赛事列表（如 JSNZKPG） | **否**，只做临时获取与预览 |

### 最短上手（全程不碰公网）

```bash
# 1) 起本地 mock 服务（另开一个窗口）
python tools/mock_source_server.py --port 8800

# 2) 注册来源（默认全部禁用，见 config/config.example.toml）
cp config/config.example.toml config/config.toml
python -m liptv source-register --from-config

# 3) 启用一个 fixed 来源（示例里 demo-fixed 指向 mock 的 /ok.m3u）
python -m liptv source-add --name demo-fixed --kind fixed_m3u \
    --url http://127.0.0.1:8800/ok.m3u --enable

# 4) 抓取并查看生命周期
python -m liptv fetch --all          # 只抓 enabled 的 fixed_m3u 来源
python -m liptv source-status        # 每个来源的状态与条目 active/inactive 统计

# 5) 动态赛事源：只预览，不落库
python -m liptv dynamic-fetch --source jsnzkpg-sports
```

要点：

- `fetch --all` **不会**请求禁用的来源，也**不会**请求 `dynamic_event_m3u` 来源（会明确列在 `skipped_*` 里）。
- 抓取失败（HTTP 非 2xx / 超时 / 解码失败 / 非法或空列表）**不会改动已有库存**，只更新该来源的 fetch 状态。
- 本次未出现的条目只置 `active=0`（不硬删，`first_seen_at` 与绑定保留），重新出现时自动恢复并复用原身份。
- 动态源摘要里的播放地址一律脱敏（去掉 query），不会把短时签名参数写进日志或报告。
- `dynamic-fetch --out <路径>` 是**三重强制**的：① 目标必须位于 `fetch.dynamic_tmp_dir`（默认 `out/tmp`）之内；
  ② 目标在 Git 层面必须安全 —— 位于某个 Git 工作树内时必须被 `.gitignore` 忽略，不在任何工作树内则允许；
  ③ 目标**不得已被该工作树跟踪**（存在于 Git 索引中）—— 因为 `.gitignore` 只对未跟踪文件生效，
  曾经 `git add -f` 过的文件必须另行拦截。
  任一不满足即拒绝写入且**不创建文件**、旧文件一个字节都不动；把 `dynamic_tmp_dir` 改成仓库内未被忽略的目录（如 `SOURCES/`）会被直接拒绝。
  判定用 `git ls-files --error-unmatch`（输出全部丢弃），取不到 git 时回退到直接解析 `.git/index`（v2/v3/v4）；
  两者都不可用则**保守拒绝**。可用环境变量 `LIPTV_GIT_EXECUTABLE` 指定 git 路径。
- 一次响应若「解析出了条目、但仍有 `#EXTINF` 没有配套播放地址」（被上游截断），会被判为 `INVALID_M3U`
  并整体拒绝 —— 不会因为一次截断的响应就把已有频道静默下线。结构完整的真正删台照常生效。

详细设计与命令说明见：

- [数据模型 V1](DATA_MODEL_V1.md)
- [运行时流程](V1_RUNTIME_FLOW.md)
- [TASK-001 执行报告](REPORTS/TASK-001-REPORT.md)
- [TASK-002 执行报告](REPORTS/TASK-002-REPORT.md)

## 当前状态

- [TASK-001](TASKS/TASK-001.md)：V1 Skeleton / Data Foundation —— **ACCEPTED**（见 [第二轮独立验收](REVIEWS/TASK-001-REVIEW-02.md)）
- [TASK-002](TASKS/TASK-002.md)：远程 M3U 抓取及动态体育赛事源临时获取 —— **REVIEW**（见 [执行报告](REPORTS/TASK-002-REPORT.md)）

动态体育赛事源已登记：[JSNZKPG 体育赛事 M3U](SOURCES/JSNZKPG-SPORTS.md)。目前可由播放器独立订阅，尚未并入统一 /live.m3u。

TASK-002 只做到「按需抓取 + 来源生命周期 + 动态源临时预览」；统一 `/live.m3u` 合并发布、自动调度、多探针真实 ffprobe、EPG / Logo、云端部署均未实现。
