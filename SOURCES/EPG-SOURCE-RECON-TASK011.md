# TASK-011 §3 — EPG / Logo 公开源侦察报告

- 侦察脚本：`tools/recon_epg_t011.py`（只读、可复跑，入库备审）
- 侦察时间：2026-10-06T14:05Z（本机） / 2026-10-06T14:30Z（生产机 `ev-lab-shanghai`）
- 双环境对照的原因：本机走 FlClash TUN 代理，会切断部分国际源 TLS（TASK-010 已实证），
  单环境结论不足以支撑生产决策。
- 安全边界：全程无 Cookie / 无 Authorization / 不请求视频内容 / 不镜像 logo。

---

## 1. 结论先行

| 用途 | 选定 | 理由 |
|:--|:--|:--|
| **主力 EPG** | fanmingming（jsDelivr CDN 入口） | 唯一覆盖本项目全部 43 个 canonical；programme 最多；中文简体 channel id 与频道名天然对齐 |
| **备用 EPG** | epg.pw | 覆盖广（654 ch），生产机实测可达；但 channel id 是纯数字，需人工映射，不能直接用 |
| **tvg-id 权威源** | iptv-org `channels.json` | 31489 条频道的官方 id（`CCTV1.cn` 等），是 §5 优先级第 1 级依据 |
| **logo 首选** | `live.fanmingming.cn/tv/{名}.png` | 与 EPG channel id 同一套中文短名，一致性最好；**但生产机 DNS 污染，见 §4** |
| **logo 备选** | iptv-org `logos.json` | 35122 条官方 logo 目录（735 条中国频道），指向第三方 CDN |

**最重要的一条实测结论**：logo URL 是写进 M3U 交给**播放器设备**访问的字符串，
因此**生产机能否访问 logo 源不影响 logo 能否显示**——判据必须来自用户播放环境（§11）。
本轮生产机`live.fanmingming.cn` 不可达，属环境差异，不构成淘汰理由。

---

## 2. EPG 候选源逐条记录

| # | source | feed URL | 本机 | 生产机 | XMLTV valid | channel | programme | channel id 风格 | tz | logo | 大陆覆盖 | auth | license | 判定 |
|:--|:--|:--|:--|:--|:--|--:|--:|:--|:--|:--|:--|:--|:--|:--|
| 1 | iptv-org CN guide（legacy） | `iptv-org.github.io/epg/guides/cn/epg.xml` | 404 | 404 | ✗ | 0 | 0 | — | — | — | — | 无 | MIT | **reject**：路径已变更 |
| 2 | epg.pw CN | `epg.pw/xmltv/epg_CN.xml` | 200 / 7.24 MB | 200 / 7.24 MB | ✓ | 654 | 29162 | 纯数字 `539631` | offset | 有 | 全 | 无 | 公开 | **backup**：id 需人工映射 |
| 3 | epg.112114.xyz | `epg.112114.xyz/pp.xml` | 200 / 3.04 MB | **不可达** | ✓ | 540 | 19283 | 中文 `CCTV10` | offset | 无 | 高 | 无 | 不明 | **reject**：生产机 Network unreachable |
| 4 | fanmingming（raw） | `raw.githubusercontent.com/fanmingming/live/main/e.xml` | 200 | **TLS 超时** | ✓ | 136 | 36164 | 中文 `CCTV1` | offset | 无 | 全部 43 | 无 | MIT | **reject**：生产机 raw 阻断 |
| 5 | fanmingming（jsDelivr） | `cdn.jsdelivr.net/gh/fanmingming/live@main/e.xml` | 200 / 6.85 MB | 200 / 7.66 MB | ✓ | 134~136 | 31092~34705 | 中文 `CCTV1` | offset | 无 | 全部 43 | 无 | MIT | **CHOSEN（主力）** |
| 6 | tvguide.cc | `www.tvguide.cn/xmltv/epg.xml` | 504 | 504 | ✗ | 0 | 0 | — | — | — | — | 无 | 不明 | **reject**：持续 504 |
| 7 | AlexWanTR/iptv-checker | `raw.githubusercontent.com/.../epg/cn.xml` | 404 | 超时 | ✗ | 0 | 0 | — | — | — | — | 无 | — | **reject**：不存在 |
| 8 | IPTV-FAN / live.fanmingming.com | `live.fanmingming.com/e.xml` | 200（与 #4 同源） | **不可达** | ✓ | 136 | 36164 | 中文 | offset | 无 | 全部 43 | 无 | MIT | **reject**：生产机 DNS 污染 |
| 9 | iptv-org `channels.json` | `iptv-org.github.io/api/channels.json` | 200 / 31489 条 | — | 非XMLTV | — | — | `CCTV1.cn` | — | 关联 logos.json | 全 | 无 | CC0 | **CHOSEN（id 权威源）** |

共检查 **9 个候选**（要求 ≥8），采纳 2 个 EPG feed + 1 个 id 权威源。

### 2.1 tz 风格说明

三个可用源都使用 `<programme start="20260930175800 +0800">` 形态（offset 带时区），
解析时**必须保留 `+0800`**（§15「timezone 保留」）。不带时区的输入按本机本地时间解释。

### 2.2 channel id 风格是选源的决定因素

|源 | id 示例 | 能否直接用于本项目 |
|:--|:--|:--|
| fanmingming | `CCTV1` / `湖南卫视` | ✅ 中文名与 canonical 一一对应 |
| 112114 | `CCTV10` / `湖南卫视` | ✅ 同上 |
| epg.pw | `539631` | ❌ 纯数字无语义，需 654 条人工映射表 |

这直接决定了 fanmingming 为主力、epg.pw 仅作灾备（§15 允许
「canonical → 单一 authoritative EPG source」，不做复杂 ranking）。

---

## 3. iptv-org 映射侦察（非 fuzzy 的依据）

`channels.json` 提供 31489 条频道，字段含 `id` / `name` / `alt_names` / `country` / `categories`。

**抽样 14/14 精确命中**（查询键取 `name` 或 `alt_names` 的**完整精确值**，不做相似度匹配）：

| seed canonical | iptv-org id | 匹配键 |
|:--|:--|:--|
| CCTV-1 综合 | `CCTV1.cn` | alt_names = `CCTV-1 综合` |
| CCTV-5 体育 | `CCTV5.cn` | name = `CCTV-5` |
| CCTV-5+ 体育赛事 | `CCTV5Plus.cn` | name = `CCTV-5+` |
| CCTV-13 新闻 | `CCTV13.cn` | name = `CCTV-13` |
| 北京卫视 | `BeijingSatelliteTV.cn` | alt_names = `北京卫视` |
| 东方卫视 | `DragonTV.cn` | alt_names = `东方卫视` |
| 云南卫视 | `YunnanSatelliteTV.cn` | alt_names = `云南卫视` |
| 陕西卫视 | `ShaanxiSatelitalTV.cn` | alt_names = `陕西卫视` |

⚠️ **上游数据质量问题（如实记录，不擅自修正）**：`ShaanxiSatelitalTV.cn`
拼写有误（`Satelital` → 应为 `Satellite`）。这是 iptv-org 的笔误，
但它是**上游的权威 id**，修正它会导致与 iptv-org 的对应关系失效。
因此保留原样，仅在此标注。

### 3.1 CCTV-5 与 CCTV-5+ 严格独立（§5 明令）

```
CCTV5.cn       → CCTV-5
CCTV5Plus.cn   → CCTV-5+
```
两者 id 完全不同，**不存在共用**。metadata 配置中亦为两条独立记录。

### 3.2 iptv-org 的真实数据缺口（不编造）

以下 16 个 canonical 在 iptv-org 中**查不到权威条目**，已逐一核查 CN 全部含「卫视」的条目：

湖南卫视 / 广东卫视 / 广西卫视 / 厦门卫视 / 辽宁卫视 / 黑龙江卫视 / 东南卫视 /
湖北卫视 / 河南卫视 / 河北卫视 / 山西卫视 / 贵州卫视 / 青海卫视 / 西藏卫视 /
福建海峡卫视 / CCTV-4 中文国际 / CETV-1 中国教育

逐条原因（已写进 `config/channel_metadata.toml` 的 `notes` 字段）：

- `CCTV-4 中文国际`：iptv-org 拆成 `CCTV4America/Asia/Europe.cn` 三个地区版本，
  **没有单一「中文国际」条目** —— 模糊匹配会选错地区频道，故弃用自动匹配。
- `CETV-1 中国教育`：有 `CETV1.cn`，但 alt_names 仅 `中国教育电视台`（泛指整个台），
  与本频道的对应关系**不唯一**。
- 其余卫视：iptv-org 缺失或只有无后缀的 `XxxTV`（alt为「XX电视台」，非「XX卫视」），
  语义与卫视频道不严格对应。

处理方式：走§5 优先级第3/4 级「明确静态人工 mapping」，采用
`liptv-cn-<slug>` 形式的**项目内稳定 id**，并在配置里逐条写明理由。
**这不是为凑 coverage 编造映射** —— 这些频道的节目单在三个 EPG 源里都真实存在，
有了稳定 id 播放器就能正确关联。

---

## 4. Logo 侦察

### 4.1 两类 logo 源

| 源 | URL 形态 | 本机实测 | 生产机实测 |
|:--|:--|:--|:--|
| fanmingming 台标库 | `live.fanmingming.cn/tv/{名称}.png` | CCTV1 / CCTV5+ / 湖南卫视 均 200 `image/png`；不存在频道 404 | **DNS 解析到 `2a03:2880::`（Meta 网段，污染），全部失败** |
| iptv-org logos.json | 35122 条，其中 `.cn` 735 条 | `CCTV1.cn` → `i.imgur.com/TpA3cUl.png` | logos.json 本体可达 |

### 4.2 三个必须记录的事实

1. **中文名必须 percent-encode**。`live.fanmingming.cn/tv/湖南卫视.png` 直接请求会抛
   `UnicodeEncodeError`；必须 `urllib.parse.quote()` 编码后再请求。
2. **iptv-org 的 logo 路径已废弃**。2025-07 起 logo 字段从 `channels.json` 移到
   `logos.json`；旧路径 `iptv-org.github.io/logos/tv/CCTV1.png` 与
   `/api/logos/CCTV1.cn.png` **全部 404**。
   → 这正是任务书 §11「不允许只按规范想象」的实证：按旧规范写的代码会直接踩空。
3. **生产机 logo 不可达 ≠ logo 不可用**。生产机 `live.fanmingming.cn` 被 DNS 污染到
   Meta 网段，但 logo URL 是写进 M3U 交给**用户播放器设备**访问的字符串，
   生产机可达性**不是**判据。真实判据见 `REPORTS/TASK-011-PLAYER-COMPAT.md`。

### 4.3 logo 源选择

metadata 生成脚本采用「fanmingming 台标库优先（42 条）、iptv-org 目录回退（1 条）」：

- 优先 fanmingming 的理由：与 EPG channel id 用**同一套中文短名**，
  一致性最好、可人工核对；且它是面向国内播放器的专用台标库。
- 回退 iptv-org 的理由：覆盖更全（含 fanmingming 没有的频道），
  但 URL 指向第三方图床（`i.imgur.com`），长期可用性不受我们控制。

**logo 缺失不阻断频道发布**（§8）：`logo` 为空时 M3U 里直接省略 `tvg-logo` 属性。

---

## 5. 未采用 / 待后续

| 源 | 状态 | 后续动作 |
|:--|:--|:--|
| epg.112114.xyz | 本机可用、生产机不可达 | 若生产机网络恢复可作第二灾备 |
| epg.pw | 可用但 id 为纯数字 | 值得做 654 条人工映射表；本轮不做（超出范围） |
| France 24 / NHK World EPG | 未在本轮侦察 | 见 TASK-011 §12，若纳入 fixed 需单独确认 EPG |
| DW 旧入口 | Reviewer 实测 404 | 淘汰 |

---

## 6. 安全与合规声明

- 未使用任何 Cookie / Authorization / token；未访问需要登录的 EPG。
- 未抓取任何付费 EPG。
- 未请求条目里的**视频内容**，只访问 playlist / XMLTV / logo 图片本身。
- **未镜像 logo**：配置里只引用原始公开 URL（§22「公开可访问 ≠ 可重新托管」）。
- metadata 配置里不含任何凭据；加载器 `channel_metadata.py` 会主动拒绝
  含 `cookie` / `auth` / `token` / `password` / `secret` 的字段并直接报错。

---

## 7. 复现方式

本机（部分国际源会被 TUN 挡掉，用于快速迭代）：

```bash
python tools/recon_epg_t011.py --out /tmp/epg_recon.json
```

生产机（真实环境结论以此为准）：

```bash
cd /opt/liptv-src/repo
PYTHONPATH=. python tools/recon_epg_t011.py --out /tmp/epg_recon.json --timeout 40
```

生成 metadata（会再次访问上游，用于刷新 logo / EPG id）：

```bash
python tools/build_channel_metadata.py
python tools/build_channel_metadata.py --check    # 只看覆盖统计，不写文件
```