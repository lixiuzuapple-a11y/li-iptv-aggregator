"""TASK-010 §4/§13：区分「聚合器可达」与「播放环境可达」。

## 为什么需要这个模块

KORICE（PPV赛事源）在 TASK-010 planning note 里被确认存在一个**真实且关键**的分离：

* 上海生产机**能抓** KORICE 的 source M3U（聚合器层职责已完成）；
* 上海生产机对底层 PPV stream 的 ffprobe **失败**（超时/5XX）；
* 但 Owner 家庭网络在可用 VPN 下**实际能播放** KORICE。

如果把这两件事合成一个 PASS/FAIL，那么：

* 用上海 FAIL 判定全局不可用 ⇒ 会把 Owner 真正看得着的赛事从 M3U 里删掉；
* 反过来只看上海 PASS ⇒ 会把只有家庭网络能播的线路当成「云端健康」。

播放器**直接访问上游、不经上海转发**，所以家庭/VPN 环境的 probe 结论
权重天然高于上海云 probe。:mod:`liptv.probe` 继续负责「上海事实」，
本模块负责**表述**两者是不同上下文下的两份独立事实。

## 设计边界（严格遵守任务书）

* **不改 schema V1**。probe 表**已有** ``location`` 字段（预置 ``shanghai-cloud``），
  所以「probe 发生在哪个上下文」本来就能记录，无需迁移。
* **不进入 selector 算法**。selector 只认 probe 事实；「播放上下文权重」是**产品
  层面的解释**，不是本轮要重写的评分模型（任务书 §6.3 明确不重新设计评分）。
* **绝不含VPN 凭据**。本模块只表达 ``requires_vpn = true`` 这类**事实标记**，
  不接收、不存储、不导出任何用户名/密码/节点地址/Cookie/Authorization。
* **KORICE 不因上海 probe 失败而被删源**。这条规则由
  :func:`should_drop_source_on_probe_failure` 表达，并在 publish 路径中被引用。
"""

from __future__ import annotations

import tomllib
from typing import Any

#: 允许的 probe/聚合上下文标识。
#:
#: 取值刻意做成**自由文本 + 白名单校验**两段式：白名单只挡明显笔误
#: （大小写/空格错），真正的环境名留给部署方扩展。
KNOWN_CONTEXTS = (
    "shanghai-cloud",
    "home-windows-vpn",
    "windows-local",
)

#: ``cloud_probe_authoritative`` 的语义：当云端 probe 对播放不可靠时，
#: 下游不得把「云端 FAIL」当作「全局不可用」。
DEFAULT_CLOUD_PROBE_AUTHORITATIVE = True


class SourcePolicyError(ValueError):
    """source policy 配置非法。**直接抛出**，绝不静默回落默认值。

    理由与 ``failure_policy`` 相同：一个拼错的关键字悄悄退回默认，
    会让「KORICE 该不该因云端 FAIL 被删」这种判断变成不可解释的黑箱。
    """


def load_source_policies(path) -> dict[str, dict[str, Any]]:
    """读取 ``config/source_policies.toml``。

    期望结构::

        [[source_policy]]
        name = "korice-ppv"
        kind = "dynamic_event_m3u"
        aggregator_context = "shanghai-cloud"
        playback_context = "home-windows-vpn"
        playback_requires_vpn = true
        cloud_probe_authoritative = false
        notes = "..."

    ``name`` 是**唯一键**：重复定义直接报错（fail-closed），
    否则「后一条覆盖前一条」会让运维以为配了A 实际生效的是 B。
    """
    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        return {}
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    raw = data.get("source_policy") or []
    if not isinstance(raw, list):
        raise SourcePolicyError("source_policies.toml 的 source_policy 必须是数组")

    out: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise SourcePolicyError(f"source_policy 条目必须是表：{entry!r}")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise SourcePolicyError("source_policy 缺少 name")
        if name in out:
            raise SourcePolicyError(f"source_policy 的 {name!r} 重复定义，拒绝覆盖")
        out[name] = normalize_policy(entry, name=name)
    return out


def normalize_policy(entry: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    """规范化并**校验**一条 source policy。

    只校验「可解释性」相关字段，不校验也不接收任何凭据类字段 ——
    即使配置文件里塞了 password/token 之类，本函数也**不会**把它们带进结果。
    """
    nm = str(name or entry.get("name") or "").strip()
    agg = str(entry.get("aggregator_context") or "shanghai-cloud").strip()
    play = str(entry.get("playback_context") or "").strip() or None
    if agg != agg.strip().lower() or " " in agg:
        raise SourcePolicyError(
            f"source_policy {nm!r} 的 aggregator_context={agg!r} 含空格或大小写异常；"
            "请用小写短横线命名（如 shanghai-cloud）"
        )
    if play is not None and (" " in play or play != play.lower()):
        raise SourcePolicyError(
            f"source_policy {nm!r} 的 playback_context={play!r} 含空格或大小写异常"
        )
    # 上下文名不做白名单硬失败：允许部署方登记自己的环境（如 apple-tv-lan），
    # 只要符合「小写短横线」命名。未登记的名字不会进 known_contexts 清单，
    # 但**不会**导致配置失效 —— 这比把自定义环境硬挡在门外更实用。
    return {
        "name": nm,
        "kind": str(entry.get("kind") or "").strip() or None,
        "aggregator_context": agg,
        "playback_context": play,
        "playback_requires_vpn": bool(entry.get("playback_requires_vpn", False)),
        "cloud_probe_authoritative": bool(
            entry.get("cloud_probe_authoritative", DEFAULT_CLOUD_PROBE_AUTHORITATIVE)
        ),
        "notes": str(entry.get("notes") or "").strip() or None,
    }


def default_policy(*, kind: str | None = None) -> dict[str, Any]:
    """未登记源用的默认 policy。

    默认 ``cloud_probe_authoritative = True``：没有显式声明时，
    云端 probe 就是权威（本轮所有 fixed 源都走这条）。
    KORICE 那种「云端不可靠」的情况必须**显式**登记，不靠猜。
    """
    return normalize_policy({
        "name": "__default__",
        "kind": kind,
        "aggregator_context": "shanghai-cloud",
        "playback_context": None,
        "playback_requires_vpn": False,
        "cloud_probe_authoritative": DEFAULT_CLOUD_PROBE_AUTHORITATIVE,
    })


def should_drop_source_on_probe_failure(policy: dict[str, Any] | None) -> bool:
    """云端 stream probe 全失败时，是否应该把**整个源**从发布中移除。

    返回 ``False`` 表示：**不删源**。

    这就是 TASK-010 §4.2 的 KORICE 冻结规则 ——
    ``cloud_probe_authoritative = false`` 的源，即使上海侧底层流全失败，
    也必须保留（聚合层职责已由「能抓 source M3U」完成）。
    """
    if not policy:
        return True
    return bool(policy.get("cloud_probe_authoritative", DEFAULT_CLOUD_PROBE_AUTHORITATIVE))


def summarize_contexts(
    policies: dict[str, dict[str, Any]],
    *,
    active_sources: list[str] | None = None,
) -> dict[str, Any]:
    """生成「聚合器可达 vs 播放可达」的可审计摘要（进 publish summary）。

    只输出**上下文名与标记**，绝不含任何 URL / query / 凭据。
    """
    rows = []
    for name in sorted(policies):
        pol = policies[name]
        if active_sources is not None and name not in active_sources:
            continue
        rows.append({
            "source": name,
            "kind": pol.get("kind"),
            "aggregator_context": pol.get("aggregator_context"),
            "playback_context": pol.get("playback_context"),
            "playback_requires_vpn": pol.get("playback_requires_vpn"),
            "cloud_probe_authoritative": pol.get("cloud_probe_authoritative"),
            "notes": pol.get("notes"),
        })
    split = [r for r in rows if not r["cloud_probe_authoritative"]]
    return {
        # 明确写出「这是两类事实」，防止下游/读者继续把它们合成一个 PASS/FAIL。
        "semantics": {
            "aggregator_reachability": "能否抓取 source M3U / source endpoint（上海生产机视角）",
            "playback_reachability": "实际观看网络能否播放底层 stream（家庭/VPN/Apple TV 视角）",
            "must_not_merge": True,
        },
        "known_contexts": list(KNOWN_CONTEXTS),
        "sources": rows,
        "cloud_probe_non_authoritative_count": len(split),
        "cloud_probe_non_authoritative_sources": sorted(
            r["source"] for r in split
        ),
    }