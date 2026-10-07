# TASK-011 执行报告 —— EPG / Logo / Metadata 与播放器体验升级

日期：2026-10-06 → 2026-10-07
执行：小W
状态：**REVIEW**（未启动 TASK-012）

---

## 0. 一句话结论

TASK-011 的技术目标全部达成，**所有硬指标超标**；
生产 `live.m3u` 从「只有频道名」升级为「tvg-id + tvg-logo + 分组 + 节目单」。

但有 **3 项没有按任务书预期达成**，全部是**真实世界证据不支持**，
已如实记录而没有降低门槛凑数：

| 任务书要求 | 实际 | 原因 |
|:--|:--|:--|
| §12 至少新增 **2 个**国际频道 | **1 个**（France 24） | NHK World 三个官方域分片 0/6、DW 404、Al Jazeera/CGTN 不可达 |
| §26 第 32 条「至少 2 国际频道」 | **未达成** | 同上 |
| §13 坏流治理 | **已定位根因，未做排除** | 见 §4，需长期数据支撑 |

---

## 1. 本轮最重要的发现（生产实测，不是规范推导）

### 1.1 节目单会 100% 失效，且「存在性检查」看不出来

初版 metadata 用 iptv-org 的英文 id 当 `tvg-id`（`CCTV1.cn`），
而 fanmingming feed 里的 channel id 是中文（`CCTV1`、`湖南卫视`）。
生产隔离环境实测：

| 阶段 | M3U tvg-id | XMLTV channel id | 交集 |
|:--|:--|:--|--:|
| 初版 | `CCTV1.cn`（41 个） | `CCTV1`（42 个） | **0** |
| 修正后 | `CCTV1`（41 个） | 同 | **41** |

**如果初版上线，整个节目单功能完全失效**，而「M3U 里有 tvg-id」这个检查会通过。

外部证据（Apple TV IPTV EPG 配置指南）独立印证了这一点：
> 播放器拿 `tvg-id` 去 XMLTV 里找 channel，**字符串必须完全一致（含大小写）**，
> 对不上就什么都拿不到 —— 这是节目单空白最常见的根因。

修正规则：**有 EPG 时 `tvg_id == epg_channel_id`**（任务书 §19 的直接要求）。
两处 fail-closed：生成脚本 + 加载器交叉校验（不能只靠生成脚本）。

### 1.2 连带修正：tvg-id 字符集

初版 `[A-Za-z0-9._-]` 白名单**拒绝**了 `CCTV5+` 与 `湖南卫视` ——
而这两个正是真实 feed 的 channel id。若不改，这 42 个频道
根本无法满足「tvg-id == epg_channel_id」，CCTV-5+ 恰是任务书 §2A
点名必须独立成条的频道。改为**排除法**（禁引号/逗号/尖括号/反斜杠/斜杠/空白/控制字符）。

### 1.3 连带修正：tvg_name 被截断

旧代码用一个 `short` 变量同时干三件事（查 EPG id、拼台标 URL、写 tvg_name），
而这三个用途的答案并不总相同：`"France 24".split()[0] == "France"`
⇒ 播放器显示名被截成「France」。已拆成三个独立变量。

---

## 2. §3 EPG 侦察（≥8 个源）

产出 `SOURCES/EPG-SOURCE-RECON-TASK011.md`，检查 **9 个**候选源。

**本机与生产机结果不同**（这是本轮一个关键认识）：

| 源 | 本机 | 生产机 | 结论 |
|:--|:--|:--|:--|
| fanmingming（jsDelivr CDN） | 200 / 136 ch / 34705 prog | **200 / 可用** | ✅ 主力 |
| epg.pw | 200 / 654 ch / 29162 prog | 200 可取但 **id 体系不匹配** | ⚠️ 灾备（见下） |
| epg.112114.xyz | 200 / 540 ch | Network unreachable | reject |
| fanmingming raw | 200 | TLS timeout | reject |
| iptv-org guide legacy | 404 | 404 | reject |
| tvguide.cn | 504 | 504 | reject |
| AlexWanTR/iptv-checker | 404 | timeout/404 | reject |
| live.fanmingming.com | 200 | **DNS 污染** | reject |
| iptv-org `channels.json` | 200 / 31489 条 | 可用 | ✅ tvg-id 权威源 |

生产机网络事实：`raw.githubusercontent.com` 不通、Fastly IP 段 443 不可达、
只能走 CDN；`live.fanmingming.cn` 解析到 Meta 网段（`2a03:2880::`，DNS 污染）。

### 2.1 epg.pw 为什么列为「灾备但不可直接用」

生产实测：epg.pw 源本身有 **654 个 channel**，但与我们 metadata 登记的
42 个 `epg_channel_id` **交集为 0** —— 它用纯数字 id（`539631…`），
而 fanmingming 用中文 id。

这是 **id 体系不匹配，不是源故障**。原报错只说「未通过质量门禁」，
运维会误判成源挂了去换源 —— 其实换任何源都一样。
现已改为明确报出「交集为 0 + 源 id 示例 + 这是 id 体系不匹配」，
并加了回归测试锁住这个信息。

**换 epg.pw 需要人工建立 42 条 id 映射表**，不在本轮范围。

---

## 3. §7/§8 覆盖率：全部硬指标超标

| 指标 | 要求 | 实测 |
|:--|:--|--:|
| fixed tvg-id | ≥ 70% | **100%**（44/44） |
| fixed EPG | ≥ 60% | **95.5%**（42/44） |
| 央视组 EPG | ≥ 80% | **100%**（13/13） |
| 主流卫视 EPG | ≥ 60% | **95.8%**（23/24） |
| fixed logo | ≥ 80% | **100%**（44/44） |
| 央视组 logo | ≥ 90% | **100%**（13/13） |
| 主流卫视 logo | ≥ 80% | **100%**（24/24） |

明细见 `REPORTS/TASK-011-EPG-COVERAGE.md`。

**唯一缺口如实标注**：福建海峡卫视 —— 三个 EPG 源都没有该频道，
标 `no_epg`，**不编造映射**（§7：「错误 EPG 比没有 EPG 更差」）。

---

## 4. §13 坏流治理：定位了根因，但**不做排除**

生产两轮采样（51 个目标）+ 分片级深探：

| 目标 | 数量 | 实测结果 | 根因 |
|:--|--:|:--|:--|
| `120.76.248.139` | 15 | **fake_200**：200 但 `content-type: text/html` | 返回真 M3U，但每段指向带 `accesstoken`/`playtoken` 签名的 `httplive.slave.bfgd.com.cn:14311`，**这些 URL 全部不可达** |
| segment-404 组 | 27 | playlist 正常、**分片不可达** | 同上机制：需要外部签名服务在线 |
| CCTV-5+ | 4 | 3 empty + 1 个 **404** | 其中 1 条**分片实测可播** |

**为什么不做排除**：任务书 §13 明确「一次失败不永久封禁」「如果长期稳定证明某 host 是坏线路」。
目前只有 2 轮采样，且 `120.76.248.139` 的行为已从 TASK-010 记录的「超时」
变成「返回假 200」—— **性质变了，需要重新评估**，不是同一条证据的延续。
故本轮只产出证据，不动排除规则。

⚠️ **未写入 Git / 报告的内容**：上述 URL 里的 `accesstoken` / `playtoken`
是短时签名，**不进 Git、不进报告**（§22 冻结）。

---

## 5. §12 国际频道：真实结果只有 1 个

生产机分片级实测 **15 个候选**（判据：分片 + MPEG-TS `0x47` 同步字节）：

| 结果 | 数量 | 明细 |
|:--|--:|:--|
| ✅ playable | **6** | France 24 官方英/法/西/阿 + `-b` 备用域 + antik 第三方 CDN |
| ❌ not_playable | 3 | NHK World `media-osa` / `media-tyo` / `masterpl` —— master 200 但**分片 0/6**（地域限制） |
| ❌ http_error | 1 | DW `dwamdstream107.akamaized.net` 404 |
| ❌ unreachable | 4 | Al Jazeera、CGTN Doc、amagi.tv、klowdtv.com |

**只纳入 France 24**，6 条 stream（满足 TASK-010 §5.2「单线路不收」）。

「至少 2 个」**未达成**，按 §12「不降低门槛 / 可用其它真实候选替代」如实记录。

### 5.2 France 24 上线实测（生产 ffprobe 独立验证）

6 条 stream 全部 probe 成功，且**不是同一档位**（说明确实是不同源）：

| stream | 分辨率 | 入口 |
|:--|:--|:--|
| 169 | 1920x1080 | france24.com 英文 |
| 170 | 1920x1080 | france24.com `-b` 备用域 |
| 171 | 1920x1080 | france24.com 法语 |
| 172 | 1920x1080 | france24.com 西语 |
| 173 | 1920x1080 | france24.com 阿语 |
| 174 | 1280x720 | antik.sk 第三方 CDN |

⚠️ **上线过程中发现的一个真实门槛**：新建 canonical + 6 条 stream 之后，
publish 时 France 24 **仍出现在 skipped 列表**里 —— selector 要求
**probe 成功**才允许入选。所以「建了 stream」≠「会出现在 M3U」，
必须 `probe-run` 让 ffprobe 验过分片。这是本轮新增运维知识。

上线后最终态：**43 fixed全部带 `tvg-id` + `tvg-logo`**，
France 24 的 `group-title="国际"`、logo 为实测可用的 JioTV CDN。

### 5.1 🚨 本轮最重要的方法论教训

> **「playlist 返回 200」不等于「可播」。**

本轮最初把 `master.nhkworld.jp` 判为 ok（200 + 8 条目），
下探到分片才发现 8 个 variant 全部 404/403。
TASK-011 §13 的 segment-404（27 条 playlist 正常、分片全挂）是同一个陷阱。

任何「可播」结论都必须到**分片 + 媒体首包字节**这一层。
本轮还因此修掉自己两个探测 bug：
① 只认含 `://` 的行 ⇒ 漏判相对路径分片（France 24 有 8 个分片被判「零条目」）；
② 写死「master→variant→segment 三层」⇒ France 24 的 master 直接就是媒体 playlist，
把 TS 二进制当 URL 解析产出一堆 `InvalidURL`。

---

## 6. §9/§10/§16/§17/§18 EPG 与端点

* `epg-refresh` / `epg-status` 两个 CLI；
* **LKG**：5 种坏输入（HTTP 失败 / malformed / 空 / 纯空白 / stale 90 天 / 无 programme）
  全部 `ok=False` 且文件**字节完全不变**；正常源成功写入；
* **原子写**：`flush` + `fsync` + `os.replace`，无半文件；
* **质量门禁**：well-formed + ≥1 channel + ≥1 programme + ≥半数 channel 未来 48h 有节目；
* `/epg.xml`：200 / 404（未启用或无文件）/ 503（空文件），只读、仍绑 localhost；
* **EPG refresh 失败不影响 `live.m3u` 发布**（实测 publish 仍 OK）。

生产实测：`42 channels / 11059 programmes / 2.4 MB`，
未来 48h 有节目 **42/42**。

---

## 7. §21 自动测试

`tests/test_task011.py` **62 passed**（覆盖 §21 的 40 项清单）。
**全量回归 681 passed**（基线 552）。

### 7.1 负向验证 6/6

`tools/_negcheck_t011.py` —— 每个新断言都在「缺陷真的存在时」验证过会失败：

| 组 | 注入的缺陷 | 结果 |
|:--|:--|:--|
| 1 | metadata tvg-id 冲突不再 fail-closed | ✅ 抓到 |
| 2 | EPG stale 门禁失效 | ✅ 抓到 |
| 3 | LKG 保护失效（源全挂也写盘） | ✅ 抓到 |
| 4 | XMLTV `display-name` 改回 attribute 读取 | ✅ 抓到 |
| 5 | logo 假图片不再识别 | ✅ 抓到 |
| 6 | tvg-id 与 epg_channel_id 不一致不再被拒 | ✅ 抓到 |

**负向验证抓出我自己的测试设计缺陷**（值得记录）：
初版 `test_10_epg_xml_parse` 的样本里 `display-name` 恰好等于 channel id，
于是「用 attribute 读」和「用子元素读」结果完全一样 ——
**缺陷存在时测试仍然通过，等于没测**。
已补 `test_10b`（id 与显示名明确不同）与 `test_10c`（icon 在 `src` attribute）。

---

## 8. 生产部署与 smoke

| 检查项 | 结果 |
|:--|:--|
| `deploy upgrade` | ✅ release `9625106`，health **ok=True** |
| `/live.m3u` | 200 |
| `/healthz` | 200，status=ok |
| fixed tvg-id | **43/43**（升级前 0） |
| fixed tvg-logo | **43/43**（升级前 0） |
| 动态赛事无 tvg-id | 188 条，符合预期（§14 不继承 fixed EPG） |
| JSNZKPG / KORICE | 均保留且本轮均 ok（JSNZKPG 70→20、KORICE 171→171） |
| France 24 | ✅ 已上线（6 条 stream，probe 全部 success） |
| 8080 绑定 | 仍只 `127.0.0.1` |
| schema | 仍 V1，**零 schema 改动** |
| M3U ↔ XMLTV 交集 | **41/43**（未匹配 = France 24 + 福建海峡卫视，均已知无 EPG） |

### 8.1 两次部署失败的根因（如实记录）

**第一次**：`health_after_rollback  status=stale`。
根因：`stale_after_seconds = 2700`（45 分钟），而上次成功 publish 是 66 分钟前。
**手工 publish 不更新 `runtime-status.json` 的 `last_success_publish_at`** ——
health 读的是运行中服务（`127.0.0.1:8080/healthz`）报的那个字段。

**修复**：`daemon-reload` + `restart` 让 scheduler 补跑一轮后 health 恢复 ok，再部署成功。

**第二次**：同样 stale。原因是 publish 后没有再次确认 health 就直接部署。
**教训**：部署前必须确认 `/healthz` 的 `status == ok`，
而不是「刚publish 过就算健康」。

### 8.2 REVIEW-01 返工：`/epg.xml` 生产真实返回 404

Reviewer 在生产实测发现 `/epg.xml` 返回 **404**，而我当时只核了「文件存在」，
**没核「服务能否读到它」** —— 这是漏检，不是笔误。三个叠加根因：

| # | 根因 | 修复 |
|:--:|:--|:--|
| 1 | 生产 `config.toml` **完全没有 `[epg]` 段** ⇒ `epg.enabled=false` ⇒ CLI **压根不注册**该路由 | 加 `[epg]`（`enabled=true` + 显式绝对路径 + 主力源）|
| 2 | `epg.xml` 放在 `/etc/li-iptv-aggregator/` 且 `root:root 0600` ⇒ 服务账号 `liptv` **读不到** | 迁到 `/var/lib/li-iptv-aggregator/`，`liptv:liptv 0640` |
| 3 | `deploy install` 写配置用 `overwrite=False`、`upgrade` 也不重写 ⇒ **模板修了生产也不会自动生效** | 生产 config 手工补段（已备份 `.bak-r1`） |

配套改了 `deploy/config.production.example.toml`，让**未来** install/upgrade
不再漏 EPG；`Layout` 增加 `epg_output_path` / `epg_status_path`；
`mode_table` 显式登记 `live.m3u` 与 `epg.xml` 同为 `0640 data_owner`（R1.3）。

**返工后生产实测**（2026-10-07）：

| 检查项 | 结果 |
|:--|:--|
| `/epg.xml` | **200**，`application/xml`，2,522,086 B |
| XML well-formed | ✅ 头`<?xml ...?><tv>` / 尾 `</programme></tv>` |
| EPG 内容 | **42 channels / 11059 programmes**（与首轮一致） |
| `sudo -u liptv test -r` | ✅ 通过 |
| `/live.m3u` | 200，**43 tvg-id / 43 tvg-logo** |
| M3U ↔ XMLTV 交集 | **41 命中 / 2 未命中**（`France24.fr`、`FujianStraitsTV.cn`） |
| 8080 绑定 | 仍只 `127.0.0.1`（§47 未变） |
| schema | 仍 V1 |
| systemd | `active` / `enabled` |

> ⚠️ **踩坑记录**：`systemctl restart` 后配置**不生效**，日志横幅仍只提
> `live.m3u`、无 EPG 行。原因是 unit 文件有变动、systemd 提示
> `changed on disk. Run 'daemon-reload'` —— 此时的 restart 是**带 warning 的空转**。
> **正解：`daemon-reload` → `restart` → 再验证**，两步缺一不可。
> 这个坑与 §8.1 的 stale 教训同源：**「我以为重启了」≠「服务真的重载了配置」**。

**回归测试**：新增 5 项（`tests/test_deploy.py::test_r1_4_*`）——
①production config 生成可用 `[epg]` ②EPG 产物落在可写数据目录而非 `/etc`
③`mode_table` 里 EPG 与 `live.m3u` 同为 0640 服务账号属主
④enabled/disabled/missing/empty 四态的 HTTP 行为 ⑤install 后路径一致。
**负向验证**（换回旧实现必须 failed）：路径退回 `/etc` ⇒ 失败；
`enabled` 改回 `false` ⇒ 失败。全量 **686 passed**（基线 681 + 5）。

---

## 9. 新增运维工具（都只读或受限写入）

| 工具 | 用途 | 副作用 |
|:--|:--|:--|
| `tools/recon_epg_t011.py` | §3 EPG/Logo 源侦察 | 无 |
| `tools/recon_t011_prod.py` | §12/§13 生产复验（URL 全从 DB 取） | 无（DB `mode=ro`） |
| `tools/diag_t011_prod.py` | 分片级深挖 | 无 |
| `tools/deep_probe_t011.py` | master→variant→segment 三级判定 | 无 |
| `tools/intl_pool_t011.py` | 国际候选池（含**故意不可达的自检**） | 无 |
| `tools/verify_epg_link.py` | §19 M3U↔XMLTV 关联核验 | 无 |
| `tools/sync_metadata.py` | **metadata-only 同步** | 只 UPDATE canonical 两列 |

### 9.1 `sync_metadata.py` 为什么必须单独存在

`build_fixed_seed.py --apply` 第一步 `build_plan()` 必须抓 fixed 源 M3U，
而源 CDN 很脆弱 —— 生产机连续 3 次 `TimeoutError`。
结果是**源网络抖一下，连频道显示名/logo 都同步不了**，
而 metadata 是 curated 的独立事实源，与「这轮抓到哪些流」无关。

新工具：只 UPDATE `preferred_tvg_id` / `preferred_logo`，
不 INSERT/DELETE，不碰 stream / binding；已有值不覆盖；
DB 里没有的 canonical 跳过并如实报告；默认 dry-run 且走 `mode=ro`
（dry-run 的意义就是零副作用，**包括不在生产库上拿写锁**）。

验证：dry-run `changed=43` 库未变 → apply `changed=43` → **二次 apply `changed=0`（幂等）**。

---

## 10. §11 Apple TV / Player reality check

详见 `REPORTS/TASK-011-PLAYER-COMPAT.md`。要点：

* **不输出 `x-tvg-url`** —— 服务只绑 `127.0.0.1`，Apple TV 访问不到
  上海服务器的 localhost；且外部证据表明部分播放器完全忽略该标签。
  依赖可选实现等于把关键功能押在运气上。
* logo 可达性**不能用生产机判据** —— logo URL 是写进 M3U 交给
  **用户播放器设备**请求的字符串。生产机 `live.fanmingming.cn`
  被 DNS 污染，但这不构成淘汰理由（与 KORICE 同一个道理）。
* **需要 Owner 做一步人工验证**（报告 §6 给了清单与预期结果）。

---

## 11. §22 安全 / 隐私 / 合规

* 无 DRM 破解、无登录绕过、无 Cookie/Authorization/token；
* 未代理视频、未录制、未转码；
* **未镜像任何 logo**（只引用原始公开 URL）；
* 短时签名 URL（`accesstoken` / `playtoken`）**未写入 Git、未写入报告**；
* metadata 加载器主动拒绝凭据类字段名（cookie/auth/token/password/secret/…）；
* 未改 EV-Lab、未改安全组、未改防火墙、未改 DNS、未公网暴露 8080；
* 未启动 TASK-012。

---

## 12. §26 验收 50 条自评

| # | 条目 | 结论 |
|:--:|:--|:--|
| 1 | TASK-010 零回归 | ✅ 681 passed |
| 2 | EPG recon ≥ 8 sources | ✅ 9 个 |
| 3 | metadata 可审计 | ✅ 生成脚本 + notes 逐条理由 |
| 4 | stable tvg-id | ✅ 不随 stream 变化 |
| 5 | duplicate tvg-id fail closed | ✅ |
| 6 | no fuzzy mapping | ✅ |
| 7 | fixed tvg-id ≥ 70% | ✅ 100% |
| 8 | EPG ≥ 60% | ✅ 95.5% |
| 9 | 央视 EPG ≥ 80% | ✅ 100% |
| 10 | 卫视 EPG ≥ 60% | ✅ 95.8% |
| 11 | logo ≥ 80% | ✅ 100% |
| 12 | 央视 logo ≥ 90% | ✅ 100% |
| 13 | 卫视 logo ≥ 80% | ✅ 100% |
| 14 | logo failure non-blocking | ✅ |
| 15 | EPG XML valid | ✅ |
| 16 | XML channel unique | ✅ |
| 17 | programme refs valid | ✅ 0 orphan |
| 18 | timezone valid | ✅ |
| 19 | EPG refresh | ✅ |
| 20 | EPG LKG | ✅ 5 种坏输入验证 |
| 21 | malformed XML reject | ✅ |
| 22 | empty XML reject | ✅ |
| 23 | atomic write | ✅ |
| 24 | refresh failure no overwrite | ✅ |
| 25 | M3U tvg-id render | ✅ |
| 26 | M3U tvg-logo render | ✅ |
| 27 | M3U group stable | ✅ |
| 28 | dynamic no fixed EPG inherit | ✅ 190 条无 tvg-id |
| 29 | CCTV-5 / CCTV-5+ distinct | ✅ `CCTV5` / `CCTV5+` |
| 30 | France24 production probe | ✅ 分片级 playable |
| 31 | NHK World production probe | ✅ 已probe（not_playable，如实记录） |
| 32 | **至少新增 2 国际频道** | ❌ **只 1 个** —— §12「不降低门槛」，见 §5 |
| 33 | 不为国际降低门槛 | ✅ 未降低 |
| 34 | segment-404 复验 | ✅ 根因已定位（见 §4） |
| 35 | bad host 复验 | ✅ 性质已重新认定（超时→假 200） |
| 36 | CCTV-5+ 复验 | ✅ 4 条中 1 条分片可播 |
| 37 | /live.m3u 200 | ✅ |
| 38 | /healthz 200 | ✅ status=ok |
| 39 | epg.xml 可生成 | ✅ 2.4 MB |
| 40 | M3U/XMLTV mapping sample PASS | ✅ 交集 41/42 |
| 41 | JSNZKPG 保留 | ✅（本轮 TIMEOUT，isolate 生效） |
| 42 | KORICE 保留 | ✅ 194 条 |
| 43 | isolate 保留 | ✅ |
| 44 | dynamic default 保留 | ✅ |
| 45 | playback context 保留 | ✅ |
| 46 | no sensitive credentials | ✅ |
| 47 | localhost binding unchanged | ✅ |
| 48 | EV-Lab zero change | ✅ |
| 49 | full tests pass | ✅ 681 passed |
| 50 | commit + push，状态只到 REVIEW | ✅ |

**48/50达成**，2 条（第 32、35）未按预期但均已如实记录原因。

---

## 13. 需要 Reviewer 关注的三点

1. **第 32 条「至少 2 个国际频道」未达成** —— 是否接受「只有 France 24 可播」这个真实结果，
   还是要求继续扩大候选池？（我已扩到 15 个，NHK/DW/AJ/CGTN 均实测不可用。）
2. **§13 坏流是否现在就该加排除规则** —— `120.76.248.139` 已连续两轮异常，
   但性质从「超时」变成「假 200」，我倾向再观察一轮再定。
3. **epg.pw 是否值得做 id 映射** —— 它是唯一可用的第二源，
   但需要人工建立 42 条映射表才能用。

---

## 14. 交付物清单

| 文件 | 说明 |
|:--|:--|
| `REPORTS/TASK-011-REPORT.md` | 本文件 |
| `REPORTS/TASK-011-EPG-COVERAGE.md` | §7/§8 覆盖率明细 |
| `REPORTS/TASK-011-PLAYER-COMPAT.md` | §11 reality check |
| `SOURCES/EPG-SOURCE-RECON-TASK011.md` | §3 侦察报告（9 个源） |
| `config/channel_metadata.toml` | 44 条 canonical metadata（生成，勿手改） |
| `config/fixed_sources/intl_france24.m3u` | France 24 六条实测可播入口 |
| `config/fixed_seed_bindings.toml` | 44 条 seed（含 France 24） |
| `liptv/channel_metadata.py` | metadata 加载与 fail-closed 校验 |
| `liptv/epg.py` / `epg_refresh.py` | XMLTV 解析/合并/原子写/LKG |
| `tools/recon_epg_t011.py` 等 7 个 | 侦察与核验工具（见 §9） |
| `tests/test_task011.py` | 62 passed |
| `tools/_negcheck_t011.py` | 负向验证 6/6 |

---

**TASK-011 状态 → REVIEW。停止。不启动 TASK-012。**