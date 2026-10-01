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

> 本机（WorkBuddy 沙箱）跑全量测试时，需要把 pytest 的 `--basetemp` 指到**操作系统临时目录**，
> 否则临时目录清理会撞上沙箱的批量删除保护、把 setup 阶段打断：
> `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -o addopts="" -p no:cacheprovider -q --basetemp="$TEMP/liptv-pytest"`。
> 也不要把它指到本仓库工作树内（会改变「路径不在任何 Git 工作树内」那类用例的前提）。

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

## 统一发布：`publish`（TASK-003）

把「已归一化、且有合格测活历史」的固定频道，与本轮即时抓取的动态赛事，
在**单次显式命令**里合并、校验并安全发布到一个**本地** M3U 文件。

```bash
# 只发固定频道（默认完全不联网：连一次 HTTP 都不会发）
python -m liptv publish

# 显式合入某个已登记的动态赛事源（可重复传 --dynamic-source）
python -m liptv publish --dynamic-source jsnzkpg-sports

# 动态失败就整次拒绝（默认策略是「降级为只发固定频道」）
python -m liptv publish --dynamic-source jsnzkpg-sports --require-dynamic

# 只组合与校验，不写任何文件（含不写摘要）
python -m liptv publish --dynamic --dry-run
```

| 状态 | 退出码 | 含义 |
|---|---|---|
| `OK` | 0 | 固定 + 动态都成功，已发布 |
| `DEGRADED_FIXED_ONLY` | 0 | 任一动态源失败 → fail-closed 只发固定频道（**本轮动态条目全部舍弃**，**绝不复用上一次的动态签名线路**） |
| `DRY_RUN` | 0 | 只校验，未写文件 |
| `DEGRADED_NO_PUBLISH` | 2 | 动态失败且固定也为空 → 不发布，**也不覆盖**已有文件 |
| `REJECTED_DYNAMIC_REQUIRED` | 1 | 指定了 `--require-dynamic` 但动态失败/缺失 → 整次拒绝 |
| `REJECTED_VALIDATION` | 1 | 组合/校验未通过（含全空结果、反向解析失败） |
| `REJECTED_IO` | 1 | 写盘失败 → 当前与上一版文件**字节不变** |

要点：

- **固定频道**复用 `select_playlist`：按历史探针记录选线、每个 canonical 最多一条、**不绕过最低成功阈值**（从未探测过的 stream 不会被发布）。
- **动态赛事**复用 TASK-002 的 `preview_dynamic_source`（HTTP 限额 + M3U 结构校验）；只取**本轮**实际成功获取的条目，**不落库**、也**不从旧 `live.m3u` 或历史快照回拼**动态线路；统一归入独立分组（默认 `体育赛事（实时）`），保留比赛名与 `[解说]` / `[原声]` 区别。
- 纳入规则**简单、配置化、可解释**（见 `[publish.dynamic]`）：默认走**排除法**——先剔掉宣传/推广与回放，其余分组（= 各联赛名）保留。原因是真实上游（JSNZKPG）的 `group-title` 是**联赛名**（`WNBA` / `欧俱杯` …），而「正在直播 / 赛事回放」只是 M3U 内部的**注释分区标记**；早期把「正在直播」当白名单会导致真实源 0/61 全被排除（TASK-003 QA-003A）。回放按**分组名或注释分区**两种写法识别（`# ===== 赛事回放 =====` 也能认），`include_replay=false` 时整类关闭；显式填写 `include_groups` 才会切换成严格白名单模式。过滤理由与计数都会写进摘要。
- 仅在同一次动态来源内做**字节级**去重（URL + 显示名 + 原始分组）；不跨来源去重，也不把 `[解说]`/`[原声]` 合并。
- **fail-closed 是「真」只发固定**：只要有一个动态来源失败，本轮**所有**动态条目（含其他成功来源的）一律舍弃，状态/计数/摘要/实际文件内容四者一致（QA-003B）。`--require-dynamic` 则整次拒绝、文件字节不变。
- 发布前完成全部校验；写盘走**同目录临时文件 → 原子替换 → `live.previous.m3u` 备份**，替换失败会把备份回滚，当前与上一版都不会半更新。全空结果默认**不覆盖**已有正常列表。
- 发布摘要（默认 `out/publish-summary.json`，已被 `.gitignore` 忽略）只含计数、过滤理由、来源抓取结果、checksum、退出码；**不含**完整签名 URL / playpath / 动态整表，URL 只保留 `scheme://host`。
- ⚠️ **本轮产物是单次静态文件**：在下一轮 `publish` 执行前**不会自动过期**，也**不是**「24 小时可用的稳定订阅」。这个限制留给后续调度/服务任务解决。

离线端到端演示（不访问任何公网地址）：

```bash
python tools/demo_publish_pipeline.py
```

`python -m liptv publish` 与旧命令兼容：`generate-m3u` / `select` 行为不变，**不会**被悄悄改成联网命令。

详细设计与命令说明见：

- [数据模型 V1](DATA_MODEL_V1.md)
- [运行时流程](V1_RUNTIME_FLOW.md)
- [TASK-001 执行报告](REPORTS/TASK-001-REPORT.md)
- [TASK-002 执行报告](REPORTS/TASK-002-REPORT.md)
- [TASK-003 执行报告](REPORTS/TASK-003-REPORT.md)

## 当前状态

- [TASK-001](TASKS/TASK-001.md)：V1 Skeleton / Data Foundation —— **ACCEPTED**（见 [第二轮独立验收](REVIEWS/TASK-001-REVIEW-02.md)）
- [TASK-002](TASKS/TASK-002.md)：远程 M3U 抓取及动态体育赛事源临时获取 —— **ACCEPTED**（见 [最终独立验收](REVIEWS/TASK-002-REVIEW-03.md)）
- [TASK-003](TASKS/TASK-003.md)：固定频道 + 动态赛事本地统一 M3U 组合与安全发布 —— **REVIEW**（[执行报告](REPORTS/TASK-003-REPORT.md)）

动态体育赛事源已登记：[JSNZKPG 体育赛事 M3U](SOURCES/JSNZKPG-SPORTS.md)。可用 `publish --dynamic-source jsnzkpg-sports` 显式并入统一 `out/live.m3u`（默认仍为禁用/不联网）。

TASK-001/002 已验收，TASK-003 已产出统一 `/live.m3u` 组合与安全发布，待大G独立验收。自动调度、多探针真实 ffprobe、EPG / Logo、云端部署仍未实现；且**当前 `live.m3u` 是单次静态文件，不会自动刷新**。
