"""TASK-009：按显式 seed binding 生成 fixed 库存导入计划（**默认只输出计划，不写库**）。

设计红线（与 `TASKS/TASK-009.md` §5 / §11 一一对应）：

* **绝不做 fuzzy auto-binding**：匹配只有 ``exact_normalized`` 一种，
  即「按 :func:`normalize_name` 归一后逐字节相等」。没有编辑距离、没有 NLP、
  没有「名字看起来差不多」。归一后撞名的 canonical 一律跳过并报 ambiguous。
* **绝不写入 stream URL / 签名 query**：本脚本 stdout 只输出
  ``(source, normalized_name, 条目数)`` 三元组与统计，**不打印任何 URL**。
* **排除带签名/身份参数的条目**：含 ``token/auth/key/secret/msisdn/migutoken/
  sign/hdnts/expire/mdspid`` 的 stream 一律不进 fixed 库存（§3/§6）。
* **排除万能流**：同一个 stream URL 被 ≥ :data:`CATCH_ALL_MIN_CANONICALS` 个
  canonical 共用 ⇒ 判为占位流，剔除（见 SOURCES/FIXED-SOURCE-RECON-20261005.md §4）。

用法::

    # 只看计划（离线，用已抓到的快照）
    python tools/build_fixed_seed.py --plan

    # 真正抓取 + 过滤 + 匹配，输出导入计划
    python tools/build_fixed_seed.py --build --out out/fixed-seed-plan.json

    # 真正写库（需要已 register 来源 + 已有 source_channel）
    python tools/build_fixed_seed.py --apply --plan out/fixed-seed-plan.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys
import tomllib
import urllib.parse
import urllib.request
from collections import defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from liptv import m3u as m3u_mod  # noqa: E402

#: 公开 fixed 源入口（只登记公开、无需认证的 URL；不涉及任何私密订阅）。
SOURCE_URLS: dict[str, str] = {
    "iptv-org-cn": "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "guovin-gd-ipv4": (
        "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u"
    ),
}

#: stream URL 里出现这些 query 键 ⇒ 短时签名或用户身份，**排除**。
BANNED_QUERY_KEYS = frozenset({
    "token", "auth", "key", "secret", "msisdn", "migutoken",
    "sign", "hdnts", "expire", "mdspid",
})

#: 同一 stream URL 被多少个 canonical 共用就判为「万能流 / 占位流」。
CATCH_ALL_MIN_CANONICALS = 5

#: 末尾分辨率括号。**只**剥这一类，其它字符一律逐字节保留。
_RES_SUFFIX = re.compile(r"\s*\(\d{3,4}\s*[pi]\)\s*$")

USER_AGENT = "liptv/1.0 (+TASK-009 fixed seed builder; personal use)"


def normalize_name(name: str) -> str:
    """归一：只剥离末尾分辨率括号，其余逐字节保留。

    这是**唯一**允许的归一规则。不做大小写折叠、不做全半角转换、不删标点 ——
    那些都会把「不同频道」合并掉。
    """
    return _RES_SUFFIX.sub("", (name or "").strip()).strip()


def has_banned_query(url: str) -> bool:
    """stream URL 是否带签名 / 身份类 query 参数。"""
    try:
        query = urllib.parse.urlsplit(url or "").query
    except ValueError:
        return True
    if not query:
        return False
    for key, _ in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if key.strip().lower() in BANNED_QUERY_KEYS:
            return True
    return False


def stream_identity(url: str) -> str:
    """去掉 query 后的 stream 身份（用于识别「同一路被多个频道共用」）。"""
    parts = urllib.parse.urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def load_seed_bindings(path: pathlib.Path) -> list[dict]:
    """读取显式 seed binding 文件。"""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    seeds = data.get("seed") or []
    if not isinstance(seeds, list) or not seeds:
        raise SystemExit(f"seed binding 文件里没有 [[seed]] 条目：{path}")
    for entry in seeds:
        if entry.get("match") != "exact_normalized":
            # 只允许一种匹配方式；出现别的直接拒绝，避免以后有人偷偷加 fuzzy。
            raise SystemExit(
                f"seed {entry.get('canonical')!r} 的 match={entry.get('match')!r} 非法；"
                "TASK-009 §5 只允许 exact_normalized"
            )
    return seeds


def fetch_source(url: str, *, timeout: float, max_bytes: int) -> str:
    """抓公开 M3U 文本。只发匿名 GET，不设置 Cookie / Authorization。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise SystemExit(f"{url} 响应超过 max_bytes={max_bytes}，拒绝处理")
    return body.decode("utf-8", "replace")


def build_plan(*, bindings_path: pathlib.Path, timeout: float, max_bytes: int) -> dict:
    """抓取 → 过滤 → 精确匹配 → 生成导入计划（**纯内存，不写库、不写文件**）。"""
    seeds = load_seed_bindings(bindings_path)

    # ---- 1. 抓取并解析（原始 URL 只留在内存，绝不进输出） ----
    entries_by_source: dict[str, list] = {}
    raw_counts: dict[str, int] = {}
    for name, url in SOURCE_URLS.items():
        text = fetch_source(url, timeout=timeout, max_bytes=max_bytes)
        parsed = m3u_mod.parse_text(text)
        raw_counts[name] = len(parsed.entries)
        entries_by_source[name] = list(parsed.entries)

    # ---- 2. 过滤带签名/身份参数的条目 ----
    kept_by_source: dict[str, list] = {}
    dropped_query: dict[str, int] = {}
    for name, entries in entries_by_source.items():
        kept = []
        dropped = 0
        for entry in entries:
            if has_banned_query(entry.url):
                dropped += 1
                continue
            kept.append(entry)
        kept_by_source[name] = kept
        dropped_query[name] = dropped

    # ---- 3. 归一 + 统计每 (source, normalized) 的条目数 ----
    buckets: dict[tuple[str, str], int] = defaultdict(int)
    catch_all: dict[str, set[str]] = defaultdict(set)
    for name, entries in kept_by_source.items():
        for entry in entries:
            key = normalize_name(entry.name)
            if not key:
                continue
            buckets[(name, key)] += 1
            catch_all[stream_identity(entry.url)].add(f"{name}::{key}")

    banned_streams = {
        identity for identity, owners in catch_all.items()
        if len(owners) >= CATCH_ALL_MIN_CANONICALS
    }

    # ---- 4. 精确匹配（唯一且无歧义才绑） ----
    plan: list[dict] = []
    ambiguous: list[dict] = []
    unmatched: list[dict] = []
    for seed in seeds:
        canonical = str(seed["canonical"])
        wanted_sources = list(seed.get("source_names") or [])
        # 展示名里可能带分组前缀（如 "CCTV-2 财经"），匹配用首个空格前的部分
        match_key = normalize_name(canonical.split(" ", 1)[0])
        matched: list[dict] = []
        problems: list[str] = []
        for source in wanted_sources:
            count = buckets.get((source, match_key), 0)
            if count == 0:
                problems.append(f"{source}: 无匹配条目")
                continue
            matched.append({"source": source, "normalized": match_key, "entries": count})
        if problems and matched:
            # 一侧有、一侧无 ⇒ 仍可绑（单 stream），但必须记录
            pass
        if not matched:
            unmatched.append({"canonical": canonical, "match_key": match_key,
                              "reason": "; ".join(problems) or "全部来源均无匹配"})
            continue
        # 歧义检查：同一 source 内该归一名对应多个**不同**原始名 ⇒ 不绑
        ambiguous_here = []
        for source in wanted_sources:
            variants = {
                e.name for e in kept_by_source.get(source, [])
                if normalize_name(e.name) == match_key
            }
            if len(variants) > 1:
                ambiguous_here.append(f"{source}: {len(variants)} 个原始名")
        if ambiguous_here:
            ambiguous.append({"canonical": canonical, "match_key": match_key,
                              "reason": "; ".join(ambiguous_here)})
            continue
        plan.append({
            "canonical": canonical,
            "category": str(seed.get("category") or "其他"),
            "match": "exact_normalized",
            "match_key": match_key,
            "sources": matched,
            "note": str(seed.get("note") or ""),
        })

    multi = [p for p in plan if len(p["sources"]) >= 2]
    return {
        "task": "TASK-009",
        "source_entry_urls": dict(SOURCE_URLS),
        "raw_channel_counts": raw_counts,
        "dropped_by_query": dropped_query,
        "kept_counts": {k: len(v) for k, v in kept_by_source.items()},
        "catch_all_streams_excluded": len(banned_streams),
        "plan": plan,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "summary": {
            "seed_canonicals": len(plan),
            "multi_source_canonicals": len(multi),
            "ambiguous_skipped": len(ambiguous),
            "unmatched": len(unmatched),
        },
    }


def render_text(plan: dict) -> str:
    """人读摘要。**不含任何 stream URL**（只有 source 入口 URL）。"""
    lines: list[str] = []
    lines.append("TASK-009 fixed seed 导入计划")
    lines.append("=" * 56)
    for source, url in plan["source_entry_urls"].items():
        raw = plan["raw_channel_counts"].get(source, 0)
        kept = plan["kept_counts"].get(source, 0)
        dropped = plan["dropped_by_query"].get(source, 0)
        lines.append(
            f"  {source:<16} 原始 {raw:>5}  剔除签名/身份 {dropped:>4}  保留 {kept:>5}"
        )
        lines.append(f"  {'':<16} {url}")
    lines.append("")
    lines.append(f"万能流（占位流）已排除：{plan['catch_all_streams_excluded']} 条 stream")
    lines.append("")
    lines.append(f"canonical 计划（{len(plan['plan'])} 个）:")
    for item in plan["plan"]:
        srcs = ", ".join(f"{s['source']}×{s['entries']}" for s in item["sources"])
        lines.append(f"  {item['canonical']:<18} [{item['category']}]  {srcs}")
    if plan["plan"]:
        lines.append("")
        lines.append(
            f"其中跨独立源多 stream 的 canonical：{plan['summary']['multi_source_canonicals']} 个"
        )
    if plan["ambiguous"]:
        lines.append("")
        lines.append("歧义跳过（保持 unbound，绝不猜）:")
        for item in plan["ambiguous"]:
            lines.append(f"  {item['canonical']:<18} {item['reason']}")
    if plan["unmatched"]:
        lines.append("")
        lines.append("未匹配:")
        for item in plan["unmatched"]:
            lines.append(f"  {item['canonical']:<18} {item['reason']}")
    return "\n".join(lines)


def apply_plan(plan: dict, *, db_path: pathlib.Path, config_path: pathlib.Path,
               now: str | None, dry_run: bool) -> dict:
    """把计划写进数据库：source-register → ingest → canonical+bind → prune → stream-sync。

    走**既有 CLI / repo 层逻辑**（:mod:`liptv.repo` / :mod:`liptv.ingest`），
    不另写一套写库代码 —— 这样 TASK-002 冻结的生命周期自动生效：
    抓取失败只写 fetch 状态、**库存零改动**；本轮消失的条目只 ``active=0``；
    再出现时恢复同一身份（``identity_hash``）。

    ⚠️ 第 2 步 ``ingest_fixed_source`` 会把**整份**上游 M3U 灌进库存
    （TASK-002 冻结语义，不可改）。因此第 4 步必须把「本轮 seed 用不到的条目」
    收敛成 ``active=0``，否则几百条用不到的库存会长期躺在库里，其中混着
    带短时签名的 stream。**只置 inactive、绝不硬删**（TASK-002 要求保留身份）。

    ``dry_run=True`` 时只统计将要做什么，**一个字节都不写**。
    """
    from liptv import config as config_mod
    from liptv import db as db_mod
    from liptv import ingest as ingest_mod
    from liptv import repo as repo_mod
    from liptv.util import utcnow_iso

    config_mod.load_config(config_path)          # 校验配置可用（不写任何东西）
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)                          # schema V1；已存在则无副作用
    actions: list[dict] = []
    stamp = now or utcnow_iso()

    try:
        # ---- 1. 注册来源（同名即复用，见 repo.add_source） ----
        source_ids: dict[str, int] = {}
        for name, url in plan["source_entry_urls"].items():
            if dry_run:
                row = repo_mod.get_source_by_name(conn, name)
                source_ids[name] = int(row["id"]) if row else -1
                actions.append({"action": "source-register", "source": name,
                                "exists": row is not None})
                continue
            source_ids[name] = repo_mod.add_source(
                conn, name=name, kind="fixed_m3u", url=url, enabled=1, now=stamp
            )
            conn.commit()
            actions.append({"action": "source-register", "source": name,
                            "source_id": source_ids[name]})

        # ---- 2. 抓取并应用快照（TASK-002 生命周期） ----
        for name in plan["source_entry_urls"]:
            if dry_run:
                actions.append({"action": "ingest", "source": name, "skipped": "dry-run"})
                continue
            source = repo_mod.get_source(conn, source_ids[name])
            result = ingest_mod.ingest_fixed_source(conn, source, now=stamp)
            conn.commit()
            actions.append({
                "action": "ingest", "source": name, "source_id": source_ids[name],
                "ok": result.get("ok"), "status": result.get("status"),
                "entries": result.get("entries"),
                "created": result.get("created"),
                "updated": result.get("updated"),
                "deactivated": result.get("deactivated"),
                "reactivated": result.get("reactivated"),
            })

        # ---- 3. canonical + binding（只绑「归一后完全相等」的条目） ----
        for item in plan["plan"]:
            if dry_run:
                actions.append({"action": "canonical+bind",
                                "canonical": item["canonical"], "skipped": "dry-run"})
                continue
            existing = conn.execute(
                "SELECT id FROM canonical_channel WHERE name = ?", (item["canonical"],)
            ).fetchone()
            if existing is not None:
                canonical_id = int(existing["id"])
            else:
                canonical_id = repo_mod.add_canonical_channel(
                    conn, item["canonical"], category=item["category"], now=stamp
                )
            conn.commit()

            bound = 0
            conflicts = 0
            matched_total = 0
            for src in item["sources"]:
                source_id = source_ids.get(src["source"]) or -1
                if source_id <= 0:
                    continue
                # 只取该来源下、归一后**完全等于** match_key 的 active 条目。
                # 这一句就是「不做 fuzzy」的全部实现 —— 没有相似度、没有兜底。
                rows = [
                    row for row in repo_mod.list_source_channels(conn, source_id)
                    if row["active"] and normalize_name(row["raw_name"]) == item["match_key"]
                ]
                matched_total += len(rows)
                for row in rows:
                    try:
                        repo_mod.bind_source_channel(
                            conn,
                            source_channel_id=int(row["id"]),
                            canonical_channel_id=canonical_id,
                            method="exact_normalized",
                            confidence=1.0,
                            now=stamp,
                        )
                        bound += 1
                    except repo_mod.BindingConflictError:
                        # 已绑定到**别的** canonical ⇒ 尊重既有绑定，绝不静默改绑。
                        # 这里只处理这一种预期冲突；其它异常必须炸出来（见下方断言）。
                        conflicts += 1
                    except sqlite3.IntegrityError:
                        # 幂等重复绑定
                        pass
            conn.commit()
            # 自证：计划里说这个 canonical 有 N 条匹配，就必须真的绑上 N 条。
            # 少了就是静默失败（历史上就踩过：参数名写错 + 裸 except 把 TypeError 吞掉，
            # 结果 bound_channels 全是 0 却没有任何报错）。宁可炸，也不写出一份假成功。
            if matched_total and bound + conflicts < matched_total:
                raise SystemExit(
                    f"canonical {item['canonical']!r} 计划匹配 {matched_total} 条，"
                    f"实际绑定 {bound} 条（冲突跳过 {conflicts}）—— 拒绝继续"
                )
            actions.append({"action": "canonical+bind", "canonical": item["canonical"],
                            "canonical_id": canonical_id, "matched_channels": matched_total,
                            "bound_channels": bound, "conflicts_skipped": conflicts})

        # ---- 4. 库存收敛：把「本轮 seed 用不到」的条目标 active=0 ----
        #
        # 🚨 TASK-009 真机发现（2026-10-05）：第 2 步的 ``ingest_fixed_source`` 按
        # TASK-002 冻结语义把**整份上游 M3U** 灌进库存（cn 145 + guovin 473 = 618 条），
        # 而 build_plan 的签名/身份过滤只作用于**绑定**这一步。结果：618 条库存里
        # 只有 53 条会被绑定，剩下 565 条既不会被用到，**其中还混着带短时签名的
        # stream**（实测 ``?auth=...``）—— 一旦被绑定就会随 live.m3u 流出去，
        # 既违反任务书 §11（禁止完整带签名 query 的 stream URL），也会让 selector
        # 反复测一批注定失败的线路。
        #
        # 收敛用 ``active=0`` 而不是硬删：TASK-002 要求「消失只置 inactive、再出现
        # 恢复同一身份」，硬删会破坏 identity_hash 的可追溯性。
        if not dry_run:
            keep_ids: set[int] = set()
            for item in plan["plan"]:
                for src in item["sources"]:
                    source_id = source_ids.get(src["source"]) or -1
                    if source_id <= 0:
                        continue
                    for row in repo_mod.list_source_channels(conn, source_id):
                        if row["active"] and normalize_name(row["raw_name"]) == item["match_key"]:
                            keep_ids.add(int(row["id"]))

            deactivated = 0
            for row in repo_mod.list_source_channels(conn):
                row_id = int(row["id"])
                if not int(row["active"]) or row_id in keep_ids:
                    continue
                # 命中签名/身份参数的**绝不**留在 active 库存里：短时签名会过期，
                # 留着只会让 selector 每轮测一批注定失败的线路。
                conn.execute(
                    "UPDATE source_channel SET active = 0 WHERE id = ?", (row_id,)
                )
                deactivated += 1
            conn.commit()
            actions.append({"action": "prune-unused", "deactivated": deactivated,
                            "kept": len(keep_ids)})

        # ---- 5. 归集 stream（不跑这步，绑定结果不会变成可被 selector 选的 stream）----
        if not dry_run:
            stats = repo_mod.sync_streams(conn, now=stamp)
            conn.commit()
            actions.append({"action": "stream-sync", **stats})
    finally:
        conn.close()

    return {"db": str(db_path), "dry_run": dry_run, "actions": actions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bindings", default=str(REPO_ROOT / "config" / "fixed_seed_bindings.toml"))
    parser.add_argument("--build", action="store_true", help="真的抓取公网源并生成计划")
    parser.add_argument("--apply", action="store_true", help="把计划写进数据库")
    parser.add_argument("--db", help="apply 时的 SQLite 路径")
    parser.add_argument("--config", help="apply 时的配置文件路径")
    parser.add_argument("--now", help="确定性时间戳（测试用）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--out", help="把计划写成 JSON（仍不含 stream URL）")
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--json", action="store_true", help="stdout 输出完整 JSON")
    args = parser.parse_args(argv)

    if not args.build and not args.json and not args.apply:
        parser.print_help()
        return 0

    plan = build_plan(
        bindings_path=pathlib.Path(args.bindings),
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )
    if args.out:
        target = pathlib.Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    if args.apply:
        if not args.db or not args.config:
            parser.error("--apply 需要同时给 --db 与 --config")
        applied = apply_plan(
            plan,
            db_path=pathlib.Path(args.db),
            config_path=pathlib.Path(args.config),
            now=args.now,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps({"plan_summary": plan["summary"], "applied": applied},
                             ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(render_text(plan))
            print()
            print(f"apply（dry_run={applied['dry_run']}）→ {applied['db']}")
            for act in applied["actions"]:
                print("  " + json.dumps(act, ensure_ascii=False, sort_keys=True))
        return 0

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
