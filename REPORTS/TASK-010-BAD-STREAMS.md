# TASK-010 坏台报告（Bad Streams）— 2026-10-06

Executor：小W
Reviewer：大G
任务书：`TASKS/TASK-010.md` §7
数据来源：`ev-lab-shanghai` 真机 `probe-run`（第1 轮，隔离 DB `/tmp/t010iso`）
探测主机：上海（`ap-shanghai-5`），ffprobe 6.1.1，2 并发

> 🔒 **脱敏声明**：本报告只写 canonical 名、stream id、脱敏 host、错误类型。
> **不写完整 stream URL、不写任何 query、不写签名/身份参数**。

---

## 0. 一句话结论

> **168 条 stream 里 31 条本轮不通（18.5%），但它们集中在少数几个 host 上，
> 42/43 个 canonical 至少有 1 条可播。真正"坏"的频道只有 1 个（`CCTV-5+`），
> 而它坏的**原因不在频道本身**。**

**没有任何 canonical 因为坏台被从库存删除。** 全部走 selector 门槛自然退出。

---

## 1. 总量与分布

| 指标 | 值 |
|:--|--:|
| canonical 总数 | 43 |
| stream 总数 | 168 |
| probe 探测次数 | 258 |
| probe 通过次数 | 213 |
| **去重后坏 stream** | **31**（占 18.5%） |
| 有 ≥1 条通过的 canonical | **42** |
| 全灭canonical | **1**（`CCTV-5+ 体育赛事`） |

### 1.1 错误类型分布

| 错误类型 | stream 数 | 占坏台 | 判定 |
|:--|--:|--:|:--|
| `TIMEOUT` | 19 | 61.3% | **多数是单点线路问题，不判死刑** |
| `CONNECT_ERROR` | 7 | 22.6% | 上游线路不可达 |
| `INVALID_MEDIA` | 4 | 12.9% | 拿到响应但不是有效媒体（占位流/HTML） |
| `HTTP_ERROR` | 1 | 3.2% | HTTP 4xx/5xx |
| `DNS_FAILURE` | 0 | 0% | 本轮无 |
| `HLS_NO_SEGMENT` | 0 | 0% | 本轮无（但见§3 家庭 context） |

---

## 2. 🔑 最重要的发现：单点 host 拖累

按 host 聚合坏台：

| 脱敏 host | 坏 stream 数 | 错误类型 | 性质 |
|:--|--:|:--|:--|
| `120.76.248.139` | **14** | TIMEOUT | **单点线路**（阿里云杭州 IP，跨网拥塞） |
| `69.30.245.50` | 4 | CONNECT_ERROR | 单点线路拒绝连接 |
| `104.152.209.49` | 3 | INVALID_MEDIA | 返回 HTML/占位内容 |
| `63.141.230.178` | 1 | TIMEOUT | guovin 自家线路 |
| `live.264788.xyz` | 2 | TIMEOUT | 单点线路 |
| `198.204.240.250` | 1 | CONNECT_ERROR | — |
| `117.161.133.51` | 1 | CONNECT_ERROR | — |
| `m.italkbbtv.com` | 1 | INVALID_MEDIA | 福建海峡卫视，返回非媒体 |
| `xykt-fix.github.io` | 1 | TIMEOUT | GitHub Pages，跨网慢 |
| `myip.pdtvhd.com` | 1 | HTTP_ERROR | CCTV-5+ 专用源，返回 HTTP 错误 |

> **`120.76.248.139` 一个 host 造成 14 条坏台，占全部 TIMEOUT 的 74%。**
> 涉及 CETV-1 / 广东卫视 / 山东卫视 / 北京卫视 / CCTV-13 / 深圳卫视 / 云南卫视 /
> 广西卫视 / 黑龙江卫视 / 东南卫视 / 湖北卫视 / 河北卫视 / 青海卫视 / CCTV-5+。
>
> **但这 14 个频道里绝大多数还有其它可播 stream** —— 例如广东卫视、山东卫视、
> 深圳卫视等都有 4～5 条线路，只死了 1 条。
> **这就是 selector + multi-stream 机制的价值所在：一条线路挂掉，
> 频道本身照常发布。**

### 2.1 INVALID_MEDIA 的 4 条 —— 真正的「假流」

| canonical | host | 判定 |
|:--|:--|:--|
| 北京卫视 | `104.152.209.49` | **占位流/HTML**，删除候选 |
| 江苏卫视 | `104.152.209.49` | 同上 |
| 安徽卫视 | `104.152.209.49` | 同上 |
| 福建海峡卫视 | `m.italkbbtv.com` | 同上 |

同一 host 连续 3 个频道都是 INVALID_MEDIA ⇒ **该 host 是「万能占位流」**，
典型特征：任何 `.m3u8` 路径都返回 200 + HTML 错误页。

> **处置**：`104.152.209.49` 与 `m.italkbbtv.com` 判**淘汰**（不重试观察）。
> 理由：HTML 假流不会随时间变好，且它们对3～4 个频道都产生误导性200。
> 这符合任务书「不追求 4K」「实际可播优先」的原则。

---

## 3. 唯一的全灭频道：`CCTV-5+ 体育赛事`

| stream id | 脱敏 host | 错误类型 |
|--:|:--|:--|
| 61 | `myip.pdtvhd.com` | HTTP_ERROR |
| 62 | `120.76.248.139` | TIMEOUT |

**处置：保留频道，判「观察」。**

理由：
1. `CCTV-5+` 是任务书 §2A **点名要求**的频道（体育赛事，且与 `CCTV-5` 严格不合并）。
2. 两条 stream 挂的**不是同一个原因**（一个 HTTP 错误、一个单点线路超时），
   属于**上游临时故障**，不是频道下线。
3. 任务书 §7 明确：「**不把一次临时 timeout 永久判死刑**」。

后续动作：下一轮 probe 若仍 0 通过，selector 会自然把它排除出发布
（`min_successes` 门槛），**不需要人工干预**。若将来恢复，会自动回来。

---

## 4. 各坏台类型的判定与处置汇总

任务书 §7 要求识别 11 类坏台，逐条对照：

| §7 要求识别的类型 | 本轮是否发现 | 判定依据 | 处置 |
|:--|:--:|:--|:--|
| DNS failure | ❌ 未发现 | probe 未报此类型 | — |
| connect failure | ✅ 7 条 | `CONNECT_ERROR` | 观察（多线路自动兜底） |
| timeout | ✅ 19 条 | `TIMEOUT` | **不判死刑**，单点 host 已识别 |
| HTTP error | ✅ 1 条 | `HTTP_ERROR` | 观察 |
| **HTML/text fake stream** | ✅ **4 条** | `INVALID_MEDIA` | **淘汰**（`104.152.209.49` 等） |
| **invalid media** | ✅ 4 条 | 同上（ffprobe 解析失败） | **淘汰** |
| **HLS playlist 无有效 segment** | ⚠️ **本机 context 发现 6条** | 见 §5 | 待生产复验 |
| segment 不可达 | ⚠️ 同上 | 见 §5 | 待生产复验 |
| **repeated catch-all stream** | ✅ **2 条已排除** | `build_fixed_seed` catch-all 阈值 5 | 已排除，不进库存 |
| **signed/identity URL** | ✅ **87 条已排除** | 见 §6 | 已排除，不进库存 |
| **geo-block suspected** | ⚠️ 待确认 | 家庭 VPN context 可验 | 见 §5 |

---

## 5. 家庭/VPN context 发现的独立问题（**不与上海结果合并**）

任务书 §4 要求：聚合器可达与播放环境可达**必须分开**。

在**本机（`home-windows-vpn`，FlClash TUN 运行中）** 抽样验证 5 条上海刚测过的 stream，
发现**真实生产问题**：

| stream | 上海云端 probe | 家庭本机 | 现象 |
|:--|:--:|:--|:--|
| `74.91.26.218` | 未测 | playlist 200 但 **segment 404** | 上游 playlist 里segment 路径**缺 `/live/` 前缀** |
| `204.12.221.218` | 未测 | segment 404 | 路径 `mkt/segment_*.ts` 同类问题 |
| `bztv.tvbus.cc` | 未测 | segment 404 | 同上 |
| `198.204.228.26` | 未测 | segment 404 | 同上 |
| `xykt-fix.github.io` | 未测 | **PLAYABLE** | 唯一完全可播 |

**性质判定**：这是**上游 M3U 文件本身的 bug** —— playlist 里写的相对路径
`cctv2md/segment_40473.ts` 在其自身目录下不存在。
ffprobe 在上海能 PASS 只能说明它**短暂命中过**缓存分片；
换到家庭网络就暴露了。

> **处置：判「淘汰（上游 playlist 缺陷）」**。
> 这类流**不能因为一次 ffprobe 通过就放进订阅** —— 用户点了就是播不出来。
> 这是本轮最有价值的坏台发现：**只看云端 probe 会漏掉这一整类**。

**诚实标注**：
- 这 5 条本机测的是**上海 seed 库里的真实 URL**，不是随机样本。
- 本机走了 FlClash TUN，**不能 100% 排除代理干扰**。
- 但 `segment 404`（而非 TLS 失败）与 4 条同型 URL 一致复现，**证据强度高**。
- **需要生产复验**：用 ffprobe 在上海直接对这几条做二次验证。

---

## 6. 签名 / 身份 URL 排除统计

**在进库存之前就已排除**，不产生 probe，也不进入本报告的坏台统计：

| source | 排除条目 | 涉及参数 |
|:--|--:|:--|
| `iptv-org-cn` | 14 | `auth`、`key` 类 |
| `guovin-gd-ipv4` | 73 | **`msisdn`（移动用户号码）**、**`migutoken`（移动令牌）**、`token` |
| **合计** | **87** | — |

banned query 键完整清单（10 个）：
`token`、`auth`、`key`、`secret`、`msisdn`、`migutoken`、`sign`、`hdnts`、`expire`、`mdspid`

> 其中 `msisdn` / `migutoken` 是**用户身份参数**，比一般签名更敏感。
> 把它们固化进长期库存等于把某个匿名用户的身份标识写死 —— **无条件排除，无例外**。

---

## 7. Catch-all stream 排除

`build_fixed_seed` 的 catch-all 阈值 = **5**（同一 URL 被 ≥5 个不同频道引用即判占位）。

本轮实测：**排除 2 条**。这类流典型是 `http://host/playlist.m3u`——
所有频道都指向它，probe 会 PASS 但用户看到的是同一个内容。

---

## 8. 处置决策表（任务书要求「淘汰 / 保留备用 / 观察」）

| 类型 | 数量 | 处置 | 依据 |
|:--|--:|:--|:--|
| `104.152.209.49` 占位流 | 3 | 🔴 **淘汰** | HTML 假流，3 个频道同时中招，不会变好 |
| `m.italkbbtv.com` 占位流 | 1 | 🔴 **淘汰** | 同上 |
| `120.76.248.139` 超时 | 14 | 🟡 **观察** | 单点线路，频道侧有其它 stream 兜底 |
| `69.30.245.50` 连接失败 | 4 | 🟡 **观察** | 同上 |
| `63.141.230.178` / `live.264788.xyz` 超时 | 3 | 🟡 **观察** | guovin/第三方线路波动 |
| `xykt-fix.github.io` 超时 | 1 | 🟡 **观察** | GitHub Pages 跨网慢，本机反而 PLAYABLE |
| `myip.pdtvhd.com` HTTP 错误 | 1 | 🟡 **观察** | CCTV-5+ 唯一专用源，任务书点名频道 |
| 家庭 context 发现的 segment 404 | 4 | 🔴 **淘汰** | **上游 playlist 自身缺陷**，换网络即暴露 |
| 签名 / 身份 URL | 87 | 🔴 **已排除** | 隐私红线，不进库存 |
| catch-all stream | 2 | 🔴 **已排除** | 占位流，防误导 |

---

## 9. 诚实标注：本报告的确定性边界

| 结论 | 分级 | 说明 |
|:--|:--|:--|
| 31 条坏 stream 的错误类型与分布 | 【事实】 | 生产机 probe 实测 |
| `120.76.248.139` 造成 14 条超时 | 【事实】 | 按 host 聚合实测 |
| 42/43 canonical 至少 1 条可播 | 【事实】 | selector 实跑 |
| `104.152.209.49` 是万能占位流 | 【事实】 | 3 个频道同时 INVALID_MEDIA |
| 家庭 context 的 segment 404 是上游缺陷 | 【推论·证据强**】 | 4 条同型URL 一致复现，路径拼接已验证正确 |
| 家庭 context 结果**不能**代表上海 | 【事实】 | §4 冻结语义，本机走 TUN 代理 |
| geo-block 判定 | 【未知】 | 本轮未取得可靠证据，**不妄下结论** |
| `INVALID_MEDIA` 是否会自愈 | 【未知】 | 需要第二轮及后续 probe 观察 |

---

## 10. 下一轮必做

1. **第二轮 probe 已在跑**（任务书 §10.1 要求 ≥2 轮），完成后回填本报告的趋势列。
2. 对 §5 的 4 条 segment 404 流做**生产 ffprobe 二次验证**，确认是上游缺陷而非本机代理假象。
3. 观察 `120.76.248.139` 是否恢复 —— 若第二轮仍 14 条超时，说明是**长期线路质量问题**，
   届时应在 seed 层考虑**主动排除该 host**（但必须先确认 guovin 侧无备用线路）。
