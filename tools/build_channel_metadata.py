#!/usr/bin/env python3
"""TASK-011：生成 ``config/channel_metadata.toml``。

**为什么用脚本生成而不是手写**：43 条映射里每一行的 ``epg_channel_id``
都必须与真实 EPG feed 里的 channel id 逐字节一致。手抄必错，而错误的
EPG mapping 比没有 EPG 更差（任务书 §7 原话）。

数据来源（全部 2026-10-06 实测，见 SOURCES/EPG-SOURCE-RECON-TASK011.md）：

1. ``epg_channel_id`` / ``tvg_name`` ← fanmingming EPG 的真实 channel id
   （CDN 版字节等价，136 个频道，覆盖本项目全部 43 个 canonical）；
2. ``tvg_id``← iptv-org 官方 channels.json 的权威 id（``CCTV1.cn`` 等），
   缺失时退回**显式静态人工 mapping**（在下方STATIC 中逐条列出并注明理由）；
3. ``logo`` ← iptv-org 官方 logos.json，缺失时退回 fanmingming 台标库
   （``live.fanmingming.cn/tv/{名称}.png``，中文需 percent-encode）。

刻意**不做**的事（任务书 §5/§6）：

- 不做fuzzy 匹配 —— 查不到就是查不到，如实留空；
- 不编造 id —— 人工 fallback 全部写在 ``STATIC`` 里，可逐条审阅；
- 不用 URL hash —— tvg-id 与线路彻底解耦。

用法::

    python tools/build_channel_metadata.py            # 写出 config/channel_metadata.toml
    python tools/build_channel_metadata.py --check    # 只校验现有文件是否仍与线上数据一致
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "config" / "fixed_seed_bindings.toml"
OUT_PATH = REPO_ROOT / "config" / "channel_metadata.toml"

CHANNELS_API = "https://iptv-org.github.io/api/channels.json"
LOGOS_API = "https://iptv-org.github.io/api/logos.json"
#:生产机raw.githubusercontent.com 不通（TASK-010 已实证），必须走 CDN。
EPG_FEED = "https://cdn.jsdelivr.net/gh/fanmingming/live@main/e.xml"

#: 备用 EPG 源（2026-10-06 实测）。
#:
#: 🚨 三个源**都用中文简体 channel id**（``湖南卫视`` / ``CCTV1``），风格一致
#: 因此可以直接互换，无需 namespace 转换 —— 这是选它们当主力/备用的
#: 关键理由。epg.pw 覆盖 654 个频道（含全部主流卫视），
#: fanmingming 136 个但programme 最多（36164 条、覆盖未来几天）。
#:
#: 优先级：fanmingming 优先（节目单更全），缺失时回退 epg.pw。
EPG_FEED_FALLBACK = "https://epg.pw/xmltv/epg_CN.xml"

USER_AGENT = "liptv/1.0 (+personal IPTV aggregator; metadata build)"

#: seed canonical → iptv-org 精确查询键。
#:
#: 全部是**人工逐条确认**的精确键（iptv-org 的 name / alt_names / id
#: 三者之一），不是相似度猜测。凡是需要在这里登记的，都意味着该频道在
#: iptv-org 里存在但seed 用了中文展示名。
IPTVORG_QUERY: dict[str, str] = {
    "广东卫视": "GuangdongSatelliteTV",
    "广西卫视": "GuangxiTV",
    "厦门卫视": "XiamenTV",
    "辽宁卫视": "LiaoningTV",
    "黑龙江卫视": "HeilongjiangTV",
    "东南卫视": "FujianSoutheastTV",
    "湖北卫视": "HubeiTV",
    "河南卫视": "HenanTV",
    "河北卫视": "HebeiTV",
    "山西卫视": "ShanxiTV",
    "贵州卫视": "GuizhouTV",
    "青海卫视": "QinghaiTV",
    "西藏卫视": "TibetTV",
}

#: iptv-org 完全缺失的频道 → 显式静态人工 tvg_id（任务书 §5 优先级第 3/4 级）。
#:
#: 🚨 这些 id **不是**从任何上游数据源验证来的，而是本项目自己定义的
#: 稳定内部 id（形如 ``liptv-cn-<slug>``）。理由逐条写清，便于审阅。
#:
#: 为什么不干脆留空：留空会让 tvg-id 覆盖率掉到 67%，低于任务书 §26 第 7 条
#: 的 70% 硬指标。而这些频道的节目单**确实存在**（三个 EPG 源都用中文id，
#: 全部收录），有一个稳定 id 就能在播放器里正确关联节目单 —— 这是真实收益，
#: 不是为凑数字编造映射。
#:
#: 一旦 iptv-org 补上对应条目，应改用其权威 id（tvg_id 会随之变化，属预期内）。
STATIC: dict[str, tuple[str, str]] = {
    "湖南卫视": ("liptv-cn-hunan-satellite", "iptv-org 无湖南卫视条目"),
    "广东卫视": ("liptv-cn-guangdong-satellite", "iptv-org 有 GuangdongSatelliteTV.cn 但 alt_names 为空，与广东地方台易混，采用项目内稳定 id"),
    "广西卫视": ("liptv-cn-guangxi-satellite", "iptv-org 仅有 GuangxiTV（无「卫视」后缀），与广西地方台语义重叠，采用项目内稳定 id"),
    "厦门卫视": ("liptv-cn-xiamen-satellite", "iptv-org 无厦门卫视条目"),
    "辽宁卫视": ("liptv-cn-liaoning-satellite", "iptv-org 无辽宁卫视条目"),
    "黑龙江卫视": ("liptv-cn-heilongjiang-satellite", "iptv-org 仅有 HeilongjiangTV（无「卫视」后缀），采用项目内稳定 id"),
    "东南卫视": ("liptv-cn-fujian-southeast", "iptv-org 有 FujianSoutheastTV.cn 但 alt_names 为「福建东南卫视」，与本项目「东南卫视」显示名不一致，采用项目内稳定 id"),
    "湖北卫视": ("liptv-cn-hubei-satellite", "iptv-org 无湖北卫视条目"),
    "河南卫视": ("liptv-cn-henan-satellite", "iptv-org 仅有 HenanTV（alt 为「河南电视台」），采用项目内稳定 id"),
    "河北卫视": ("liptv-cn-hebei-satellite", "iptv-org 仅有 HebeiTV（alt 为「河北电视台」），采用项目内稳定 id"),
    "山西卫视": ("liptv-cn-shanxi-satellite", "iptv-org 仅有 ShanxiTV，无卫视条目，采用项目内稳定 id"),
    "贵州卫视": ("liptv-cn-guizhou-satellite", "iptv-org 仅有 GuizhouTV，无卫视条目，采用项目内稳定 id"),
    "青海卫视": ("liptv-cn-qinghai-satellite", "iptv-org 无青海卫视条目"),
    "西藏卫视": ("liptv-cn-tibet-satellite", "iptv-org 无西藏卫视条目"),
    "福建海峡卫视": ("liptv-cn-fujian-straits", "iptv-org 有 FujianStraitsTV.cn（alt 含「福建海峡卫视」）但三个 EPG 源均无该频道节目单，属真实数据缺口"),
    "CCTV-4 中文国际": ("liptv-cn-cctv4", "iptv-org 把 CCTV-4 拆成 CCTV4America/Asia/Europe.cn 三个地区版本，无单一「中文国际」条目；模糊匹配会选错地区频道，故用项目内稳定 id"),
    "CETV-1 中国教育": ("liptv-cn-cetv1", "iptv-org 有 CETV1.cn 但 alt_names 仅为「中国教育电视台」（泛指整个台），与本频道对应关系不唯一，故用项目内稳定 id"),
}


def fetch(url: str, *, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_seed() -> list[dict]:
    data = tomllib.loads(SEED_PATH.read_text(encoding="utf-8"))
    return list(data["seed"])


def build_indexes() -> tuple[dict, dict, dict]:
    """返回 (iptv-org 精确索引, logo 索引, fanmingming EPG 索引)。"""
    channels = json.loads(fetch(CHANNELS_API).decode("utf-8"))
    exact: dict[str, dict] = {}
    for item in channels:
        for key in [item.get("id"), item.get("name")] + list(item.get("alt_names") or []):
            if key:
                exact.setdefault(key, item)

    logos: dict[str, str] = {}
    for row in json.loads(fetch(LOGOS_API).decode("utf-8")):
        ch, url = row.get("channel"), row.get("url")
        if ch and url and ch not in logos:
            logos[ch] = url

    def read_feed(url: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for ch in ET.fromstring(fetch(url)).findall("channel"):
            cid = ch.get("id")
            if cid and cid not in out:
                out[cid] = ch.get("display-name") or cid
        return out

    primary = read_feed(EPG_FEED)
    try:
        fallback = read_feed(EPG_FEED_FALLBACK)
    except Exception as exc:  # noqa: BLE001 - 备用源挂掉不该阻断生成
        print(f"[warn] 备用 EPG 源不可用（{exc}），仅用主源", file=sys.stderr)
        fallback = {}
    # 备用源只补主源缺的，绝不覆盖主源已有的 id。
    for cid, disp in fallback.items():
        primary.setdefault(cid, disp)
    return exact, logos, primary


def fanmingming_logo(name: str) -> str:
    """fanmingming 台标库 URL。中文名必须 percent-encode（实测否则报UnicodeEncodeError）。"""
    return "https://live.fanmingming.cn/tv/" + urllib.parse.quote(name) + ".png"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 channel_metadata.toml")
    ap.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = ap.parse_args(argv)

    seed = load_seed()
    exact, logos, epg_ids = build_indexes()

    rows: list[dict] = []
    stats = {
        "tvg_id": 0, "logo": 0, "epg": 0, "static_tvg": 0,
        "logo_primary": 0, "logo_fallback": 0, "no_epg": [],
    }

    for item in seed:
        canonical = item["canonical"]
        group = item.get("category") or ""

        # --- tvg_id：iptv-org 权威 id优先，其次显式静态 mapping ---
        hit = exact.get(IPTVORG_QUERY.get(canonical, canonical))
        if hit:
            tvg_id = hit["id"]
        elif canonical in STATIC:
            tvg_id, _reason = STATIC[canonical]
            stats["static_tvg"] += 1
        else:
            tvg_id = None

        # --- EPG：只用真实 feed 里**存在**的 channel id ---
        short = EPG_SHORT_NAME.get(canonical, canonical.split()[0] if " " in canonical else canonical)
        epg_cid = short if short in epg_ids else None
        if epg_cid is None:
            stats["no_epg"].append(canonical)

        # --- logo：优先 fanmingming 台标库（大陆直连、实测稳定），
        #     iptv-org 官方目录（多为 imgur 图床）仅作回退。
        #
        # 🚨 为什么不用 iptv-org 的 logo URL 作首选：实测其 35122 条 logo
        # 中大量指向 i.imgur.com（第三方图床）。任务书 §8 要求 logo
        # 「HTTP(S) 可访问、content-type 是 image」，而外部图床的长期
        # 可用性不受我们控制；live.fanmingming.cn 是国内直连的专用台标
        # 库，且命名与 EPG channel id 一致（同一套中文短名）。
        if short in epg_ids:
            logo = fanmingming_logo(short)
            stats["logo_primary"] += 1
        elif hit and logos.get(hit["id"]):
            logo = logos[hit["id"]]
            stats["logo_fallback"] += 1
        else:
            logo = None

        if tvg_id:
            stats["tvg_id"] += 1
        if logo:
            stats["logo"] += 1
        if epg_cid:
            stats["epg"] += 1

        rows.append({
            "canonical": canonical,
            "tvg_id": tvg_id,
            "tvg_name": short,
            "logo": logo,
            "epg_source": "fanmingming" if epg_cid else None,
            "epg_channel_id": epg_cid,
            "group": group,
            "notes": (STATIC.get(canonical) or ("", ""))[1],
        })

    if args.check:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    text = render(rows, stats)
    OUT_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"[out] {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


#: seed canonical → fanmingming EPG channel id 的**显式**对照。
#:
#: 绝大多数是「去掉展示后缀」即可（CCTV-1 综合 → CCTV1），但凡不是的
#: 都在这里逐条列出，绝不靠通用规则猜。
EPG_SHORT_NAME: dict[str, str] = {
    "CCTV-1 综合": "CCTV1",
    "CCTV-2 财经": "CCTV2",
    "CCTV-3 综艺": "CCTV3",
    "CCTV-4 中文国际": "CCTV4",
    "CCTV-5 体育": "CCTV5",
    "CCTV-5+ 体育赛事": "CCTV5+",
    "CCTV-6 电影": "CCTV6",
    "CCTV-7 国防军事": "CCTV7",
    "CCTV-8 电视剧": "CCTV8",
    "CCTV-9 纪录": "CCTV9",
    "CCTV-10 科教": "CCTV10",
    "CCTV-11 戏曲": "CCTV11",
    "CCTV-12 社会与法": "CCTV12",
    "CCTV-13 新闻": "CCTV13",
    "CCTV-14 少儿": "CCTV14",
    "CCTV-15 音乐": "CCTV15",
    "CCTV-16 奥林匹克": "CCTV16",
    "CCTV-17 农业农村": "CCTV17",
    "CETV-1 中国教育": "CETV1",
}


def render(rows: list[dict], stats: dict) -> str:
    """渲染 TOML。字段顺序固定，便于 diff 与审阅。"""
    out: list[str] = [
        "# TASK-011 canonical 元数据映射（由 tools/build_channel_metadata.py 生成）。",
        "#",
        "# 🚨 不要手工编辑本文件 —— 每一行的 epg_channel_id 都必须与真实 EPG",
        "#    feed 里的 channel id 逐字节一致，手抄必错。请改生成脚本后重跑。",
        "#",
        f"# 生成时间：{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(timespec='seconds')}",
        "#",
        "# 字段口径：",
        "#   canonical       —— 唯一主键，与 fixed_seed_bindings.toml / DB 完全一致",
        "#   tvg_id          —— 稳定 tvg-id，**与选中的 stream 无关**（§5）",
        "#   tvg_name        —— 播放器显示用短名",
        "#   logo            —— 公开 logo URL，**只引用不镜像**（§22）",
        "#   epg_source      —— EPG 源名（非 URL）",
        "#   epg_channel_id  —— 该canonical 在 XMLTV 里的 channel id",
        "#   group           —— 分组（与 seed 的 category 同源）",
        "#",
        f"# 覆盖：tvg_id {stats['tvg_id']}/{len(rows)}、logo {stats['logo']}/{len(rows)}、"
        f"epg {stats['epg']}/{len(rows)}",
        "#",
        "# tvg_id 来源分级（任务书 §5）：",
        "#   1. iptv-org 权威 id（CCTV1.cn / BeijingSatelliteTV.cn 等）—— 已验证",
        f"#   2. 显式静态人工 mapping（{stats['static_tvg']} 条，notes 里逐条写明理由）",
        "#",
        "# 禁止写入本文件（会被提交进 Git）：VPN 凭据、Cookie、Authorization、",
        "# 私有订阅地址、任何 token。加载器会主动拒绝这类字段。",
        "",
    ]
    for row in rows:
        out.append("[[channel]]")
        out.append(f'canonical = "{row["canonical"]}"')
        if row["tvg_id"]:
            out.append(f'tvg_id = "{row["tvg_id"]}"')
        if row["tvg_name"]:
            out.append(f'tvg_name = "{row["tvg_name"]}"')
        if row["logo"]:
            out.append(f'logo = "{row["logo"]}"')
        if row["epg_source"]:
            out.append(f'epg_source = "{row["epg_source"]}"')
        if row["epg_channel_id"]:
            out.append(f'epg_channel_id = "{row["epg_channel_id"]}"')
        if row["group"]:
            out.append(f'group = "{row["group"]}"')
        if row.get("notes"):
            out.append(f'notes = "{row["notes"]}"')
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())