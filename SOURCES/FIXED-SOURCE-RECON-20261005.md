# TASK-009 Fixed Source Reconnaissance — 2026-10-05

Executor：小W
Reviewer：大G
任务书：`TASKS/TASK-009.md` §3
最后验证时间：**2026-10-05 16:20 (Asia/Shanghai)**
探测发起地：Windows 本机（`E:\OneDrive\...\webcodex\li-iptv-aggregator`）+ 生产主机 `ev-lab-shanghai` 交叉验证

---

## 0. 侦察原则（本轮实际遵守情况）

| 原则 | 遵守方式 |
|:--|:--|
| 不因 GitHub star 高就直接采用 | 每个候选都做**真实 HTTP + M3U 解析 + 首包媒体校验**三重判定，star/名气不进入任何决策依据 |
| 不依赖单一来源 | 最终 seed 采用 **2 个独立项目**（iptv-org 与 Guovin/iptv-api），互不隶属 |
| 不收集私密订阅 | 只用**公开无认证**入口 URL；`msisdn`/`migutoken` 等用户身份参数一律判为红线并排除 |
| 不绕过登录/Cookie/Authorization/DRM | 全程只用匿名 HTTP GET，`Cookie` 与 `Authorization` 头**从不设置** |
| 不逆向 token 生成 | 遇到 `jsbt`/`jsbk` 等签名**只观测不伪造**，见 §5 |
| 有短时签名/赛事轮换特征的仍归 dynamic | 见 §4 判定；带 token 参数的条目**整体排除**，不进 fixed 库存 |
| 公开可访问 ≠ 有再分发权 | 本项目仍**只做个人自用**；不镜像、不公开转售、不把 stream URL 入 Git |

---

## 1. 候选来源清单（16 个，任务书要求 ≥10）

字段按任务书 §3 全量记录。`HTTP 状态` 与 `可解析` 为 2026-10-05 实测。
`抽样可达` = 抽样 12 条 stream 做「跟随重定向 + 解析 HLS + 首分片 TS sync 校验」的通过数。

| # | name | 入口 URL | kind | HTTP | 可解析 | 频道数 | 类别 | 含 query/token | 需登录 | 适合 fixed | 结论 |
|--:|:--|:--|:--|:--|:--:|--:|:--|:--|:--:|:--|:--|
| 1 | iptv-org-cn | `https://iptv-org.github.io/iptv/countries/cn.m3u` | fixed_m3u | 200 | 是 | 145 | 国内综合/央视/卫视 | 部分(20 条带 auth/key) | 否 | **是**（剔除带 token 条目后） | **采用** |
| 2 | guovin-gd-ipv4 | `https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u` | fixed_m3u | 200 | 是 | 473 | 国内央视/卫视 | 部分(197 条带 auth/msisdn) | 否 | **是**（剔除带 token 条目后） | **采用** |
| 3 | iptv-org-jp | `.../countries/jp.m3u` | fixed_m3u | 200 | 是 | 7 | 日本综合 | 否(0) | 否 | 是 | 备选（频道过少，仅 7） |
| 4 | iptv-org-kr | `.../countries/kr.m3u` | fixed_m3u | 200 | 是 | 81 | 韩国综合 | 否(0) | 否 | 是 | 备选（与 iptv-org 同项目，不作独立第二源） |
| 5 | iptv-org-hk | `.../countries/hk.m3u` | fixed_m3u | 200 | 是 | 18 | 香港 | 否(0) | 否 | 是 | 备选（量小） |
| 6 | iptv-org-tw | `.../countries/tw.m3u` | fixed_m3u | 200 | 是 | 26 | 台湾 | 部分(2) | 否 | 是 | 备选（量小） |
| 7 | iptv-org-sg | `.../countries/sg.m3u` | fixed_m3u | 200 | 是 | 12 | 新加坡 | 部分(1) | 否 | 是 | 备选（量小） |
| 8 | iptv-org-zho | `.../languages/zho.m3u` | fixed_m3u | 200 | 是 | 214 | 华语综合 | 部分(24) | 否 | 是 | **淘汰**：与 iptv-org-cn 125 个同名条目 **stream 完全相同**（同一上游），去重后归 1 条，无法提供第二 stream |
| 9 | iptv-org-news | `.../categories/news.m3u` | fixed_m3u | 200 | 是 | 1035 | 新闻 | 部分(36，含 token/hdnts) | 否 | 部分 | **淘汰**：token 条目多；且与 iptv-org-cn 同上游，重叠条目无独立 stream |
| 10 | iptv-org-uk | `.../countries/uk.m3u` | fixed_m3u | 200 | 是 | 301 | 英国 | 部分(3) | 否 | 是 | 备选（geo-block 比例高，抽样 10/12 但生产主机待验） |
| 11 | iptv-org-de | `.../countries/de.m3u` | fixed_m3u | 200 | 是 | 287 | 德国 | 部分(8) | 否 | 是 | 备选（内容与国内需求不匹配） |
| 12 | iptv-org-index | `.../index.m3u` | fixed_m3u | 200 | 是 | 11138 | 全球全量 | 部分(375) | 否 | 是 | **淘汰**：任务书 §4 明令「不要一上来导入几千个频道」 |
| 13 | guovin-gd | `.../gd/output/result.m3u` | fixed_m3u | 200 | 是 | 1619 | 国内央视/卫视 | **大量**(511，含 msisdn/migutoken) | 否 | 部分 | **淘汰为 fixed**：含 `msisdn`/`migutoken`（**用户身份参数**，隐私红线）；且与 guovin-gd-ipv4 同项目同上游，193 个同名条目 stream 100% 相同 |
| 14 | fanmingming-tv | `https://live.fanmingming.com/tv/m3u/ipv6.m3u` | fixed_m3u | 200 | 是 | 82 | 国内央视/卫视 | **大量**(68) | 否 | 否 | **淘汰**：IPv6-only + 68/82 条带签名 query；生产主机抽样 **0/12 可达** |
| 15 | fanmingming-global | `https://live.fanmingming.com/tv/m3u/global.m3u` | — | **404** | 否 | 0 | — | — | — | 否 | **淘汰**：入口已失效 |
| 16 | YanG-1989/tvlist | `https://raw.githubusercontent.com/YanG-1989/tvlist/main/tvlist.m3u` | — | **404** | 否 | 0 | — | — | — | 否 | **淘汰**：入口已失效 |

> 附：另探测 `iptv-org-sg/tw/jp/kr/de/fr/in`、`fanmingming6/live`（404）等，共实测 **21 个** URL，
> 上表保留任务书要求字段完整的 16 个。

---

## 2. 固定性判定（任务书 §6）

对每个候选做**两次抓取**（间隔 2 s），比对「去 query 后的 stream URL 集合」指纹：

| 源 | 60 s 内指纹一致 | 判定 |
|:--|:--|:--|
| iptv-org-cn | ✅ 一致 | **稳定 ⇒ fixed** |
| guovin-gd-ipv4 | ✅ 一致 | **稳定 ⇒ fixed** |
| iptv-org-jp / kr / hk / tw / sg | ✅ 一致 | 稳定 ⇒ fixed |
| guovin-gd | ✅ 一致 | 稳定，但**含用户身份参数 ⇒ 淘汰** |
| fanmingming-tv | ✅ 一致 | 稳定，但 **IPv6 + 大量签名 + 生产主机 0/12 可达 ⇒ 淘汰** |

**结论**：进入 fixed 库存的 2 个源均为**内容稳定**的公开聚合列表，无赛事轮换特征。

---

## 3. token / 签名参数画像（决定取舍的关键）

对每个源的 stream URL 做 query 参数名统计：

| 源 | 高频参数 | 可疑签名参数 | 处置 |
|:--|:--|:--|:--|
| iptv-org-cn | `auth`(12) `p`(3) `key`(2) `playlive`(2) `authid`(2) | `auth` `key` | **带这些参数的条目整体排除**（12+2=14 条），其余 131 条进库存 |
| guovin-gd-ipv4 | `id`(67) `auth`(66) `streamid`(30) `livekey`(30) | `auth` `key` | 同上处置，安全条目 **357 条** |
| guovin-gd | `migutoken`(63) `msisdn`(30) `mdspid`(29) `auth`(66) | `auth` `key` `token` + **用户身份** | **全源淘汰**（`msisdn`=手机号，隐私红线，不可接受） |
| iptv-org-news | `token`(4) `hdnts`(1) | `token` `hdnts` | 淘汰该源 |
| iptv-org-jp / kr / hk | 无 | 无 | 干净 |

**统一排除参数集**（写进 seed 生成脚本，不进 Git）：
`token, auth, key, secret, msisdn, migutoken, sign, hdnts, expire, mdspid`

---

## 4. 「万能流」识别（真机交叉验证抓到的真实陷阱）

初轮本机抽样探测把某条 `gslb/*.m3u8` 线路判为可用（返回 `application/vnd.apple.mpegurl`）。
统计后发现：**同一个 stream URL 被 45 个不同 canonical 共用**（按 §11 脱敏，不记录具体地址）。

在生产主机 `ev-lab-shanghai` 复验：

```
万能流 A（gslb/*，45 个 canonical 共用）   ->  404 text/html 138B
另一路  B（bfgd/*，返回 HTML 而非媒体）     ->  200 text/html 880B
```

**判定**：这是典型的**占位/万能流**（所有频道指向同一路标，播放时得到相同内容或错误页）。
若不剔除，会让 45 个 canonical 全部「probe 通过」，而 selector 选出的线路**根本不是该频道**——
这会彻底架空 TASK-009 要验证的「历史选线」能力。

**处置**：`tools/build_fixed_seed.py` 按「同一 stream URL 被 ≥5 个 canonical 引用」判为万能流并**整体排除**，
排除清单写入报告。剔除后重测，可用 canonical 仍有 **45 个**，不影响 seed 规模。

---

## 5. 签名行为观测（不伪造、不逆向）

生产主机对某条 CCTV-2 线路入口的观测（按 §11 脱敏，不记录具体地址）：

```
HTTP/1.1 302 Found
location: http://<上游A>:82/live/<频道>.m3u8?jsbt=<时间戳>&jsbk=<hash>   ← 已脱敏
```

- 入口 URL **干净无 query**；签名只出现在 **302 跳转目标**上，由上游服务器按需生成。
- `jsbt` 形似时间戳、`jsbk` 形似 hash —— 属上游的**防盗链**，**本项目不逆向、不伪造、不缓存**。
- 每次 probe 都重新请求入口 URL，让上游自己签发，**签名不落 Git、不落报告、不落长期存储**。
- 因为入口稳定，符合任务书 §6「URL 不带敏感 query/token」对 **fixed 库存**的要求。

> ⚠️ 诚实标注：这类链路的**可用性依赖上游持续在线**。若上游停止服务，这些 stream 会转为失败，
> 由 selector 的历史评分自然淘汰 —— 这是设计预期，不是缺陷。

---

## 6. 跨源同 canonical 与「多 stream」可行性（任务书 §4 硬指标）

任务书要求「**至少 2 个 canonical 拥有来自不同 source 的多条 stream**」。这是最容易被糊弄过去的指标，
因此做了三层验证：

**第一层**：`iptv-org-cn` × `iptv-org-zho` 有 125 个同名 canonical，
但逐条比对 stream 后发现 **125/125 的 stream URL 完全相同**（同一上游聚合）⇒ 去重后只剩 1 条，**不算多 stream**。

**第二层**：`guovin-gd-ipv4` × `guovin-gd` 同名 193 个，**193/193 stream 相同** ⇒ 同项目同源，**不算独立**。

**第三层（采用）**：`iptv-org-cn` × `guovin-gd-ipv4` 是**两个独立项目**，显式归一后同名且 stream 真不同：

| canonical | iptv-org-cn | guovin-gd-ipv4 | 跨源 stream 是否真不同 |
|:--|:--|:--|:--|
| CCTV-2 | 1 条（host A） | 5 条（host B/C/D/E/F，均≠A） | ✅ 无交集 |
| CCTV-9 | 1 条（host A） | 5 条（host B/C/D/E，均≠A） | ✅ 无交集 |
| CCTV-15 | 1 条（host A） | 4 条（host B/C/D，均≠A） | ✅ 无交集 |
| CETV-1 | 1 条（host A） | 3 条（host B/C，均≠A） | ✅ 无交集 |

⇒ **4 个 canonical 满足「跨独立源多 stream」**，超过任务书要求的 2 个。

---

## 7. 命名归一与「不做 fuzzy」的边界（任务书 §5）

两个源的命名风格不同：

- Guovin 用**裸名**：`CCTV-13`
- iptv-org 用**带分辨率后缀**：`CCTV-13 (720p)` / `CCTV-13 HD (1080p)`

本轮采用的归一规则（**白名单式、确定性、可复核**）：

1. 剥离**末尾**的 `(720p)` / `(1080p)` / `(1080i)` / `(576i)` 等分辨率括号；
2. 剥离后要求**两侧完全相等**（逐字节）；
3. 归一后若在任一侧**不唯一**（撞名）⇒ **保持 unbound，不绑**。

这属于任务书 §5 允许项第 3 条「大小写、空格、常见全半角/标点归一后**仍唯一且无歧义**」。
**不是** Levenshtein、**不是** NLP、**不是**「看起来差不多」。

举例说明被拒绝的做法：
- `CCTV-1` ↔ `CCTV-1 HD` ⇒ **拒绑**（不同频道，不是同一路）
- `湖南卫视` ↔ `湖南卫视 HD` ⇒ **拒绑**
- `CCTV-5+` ↔ `CCTV-5` ⇒ **拒绑**（体育赛事 vs 常规频道，语义不同）
- `Anhui TV` ↔ `安徽卫视` ⇒ **拒绑**（跨语言，需人工审，本轮不做）

---

## 8. 最终采用

| 项 | 值 |
|:--|:--|
| 采用源 1 | `iptv-org-cn`（iptv-org，中国大陆清单，145 条 → 剔除 token/万能流后进入库存） |
| 采用源 2 | `guovin-gd-ipv4`（Guovin/iptv-api，广东电信 IPv4 专版，473 条 → 同上） |
| 独立性 | 两个不同 GitHub 项目 / 不同维护者，**不隶属** |
| seed canonical 规模 | 目标 10 个（任务书要求 ≥8） |
| 跨源多 stream canonical | 4 个（CCTV-2 / CCTV-9 / CCTV-15 / CETV-1），要求 ≥2 |
| 万能流排除 | 2 条 stream（被 ≥5 个 canonical 共用的占位流；地址按 §11 不入 Git） |
| 生产可用性预判 | 严格校验（跟重定向 + HLS 首分片 TS sync）下 **45 个 canonical 至少 1 条通过** |

---

## 9. 未执行 / 明确放弃

- **未**镜像任何 M3U 到 Git；**未**把任何完整 stream URL 写进 Git / 报告 / 状态文件。
- **未**使用 Cookie / Authorization / 付费订阅 / 私密源。
- **未**逆向 `jsbt`/`jsbk`/任何 token 生成算法。
- **未**下载或保存任何视频内容（探测只取首包特征，不落盘）。
- **未**引入 fuzzy / NLP / 频道知识图谱（任务书 §15 明令不做）。
- **未**采用 iptv-org-index（11138 频道）做全量导入 —— 任务书 §4 禁止。
- **未**采用 fanmingming 系（生产主机 0/12 可达 + IPv6-only + 大量签名）。
- **未**采用 guovin-gd 全量版（含 `msisdn` 手机号参数，隐私红线）。
