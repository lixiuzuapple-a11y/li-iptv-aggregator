"""TASK-011 —— 只同步 canonical metadata 到 DB（不碰库存、不抓源）。

## 为什么需要这个独立入口

`build_fixed_seed.py --apply` 的第一步是 `build_plan()`，它必须**抓取
fixed 源的 M3U** 才能算出绑定计划。生产实测这一步很脆弱：

* 2026-10-06 生产机连续3 次在 `fetch_source()`抛
  ``TimeoutError: The read operation timed out``（源 CDN 不稳）；
* 而本任务真正要做的metadata 同步**完全不需要抓源** ——
  metadata 是 curated 的独立事实源（`config/channel_metadata.toml`），
  与「这轮抓到哪些流」无关。

把两者绑死的结果就是：**源网络抖一下，连频道显示名/logo 都同步不了。**

## 安全性

* **只** UPDATE ``canonical_channel.preferred_tvg_id`` / ``preferred_logo``；
* **不** INSERT/DELETE 任何行，**不**碰 stream / source_channel / binding；
* 已有值**不覆盖**（避免冲掉人工填的 tvg-id），只有不同才写
  —— 与 ``build_fixed_seed.py`` 的既有语义完全一致（不是另发明一套）；
* 不存在的 canonical **跳过并如实报告**，不静默新建
  （新建会改变 channel_binding，属于库存变更，不在授权范围内）；
* 默认 ``--dry-run``，必须显式 ``--apply`` 才写库。

## 参数纪律

文件路径一律作为显式命令行参数传入，**不写死任何生产路径**。
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liptv import channel_metadata as metadata_mod  # noqa: E402


def sync_metadata(
    *,
    db_path: pathlib.Path,
    metadata_path: pathlib.Path,
    apply: bool = False,
) -> dict:
    book = metadata_mod.load_channel_metadata(metadata_path)

    #🚨 dry-run 必须用**只读连接**：否则「只想看看会改什么」本身就会
    # 在生产 DB 上拿到写锁（BEGIN IMMEDIATE），而 dry-run 的全部意义
    # 就是零副作用 —— 包括不留锁。
    if apply:
        conn = sqlite3.connect(str(db_path))
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    updated: list[dict] = []
    unchanged = 0
    missing: list[str] = []
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        for meta in book.entries:
            row = conn.execute(
                "SELECT id, preferred_tvg_id, preferred_logo FROM canonical_channel "
                "WHERE name = ?",
                (meta.canonical,),
            ).fetchone()
            if row is None:
                missing.append(meta.canonical)
                continue
            want_tvg = meta.tvg_id or None
            want_logo = meta.logo or None
            cur_tvg = row["preferred_tvg_id"] or None
            cur_logo = row["preferred_logo"] or None
            tvg_differs = bool(want_tvg) and want_tvg != cur_tvg
            logo_differs = bool(want_logo) and want_logo != cur_logo
            if not tvg_differs and not logo_differs:
                unchanged += 1
                continue
            if apply:
                if tvg_differs:
                    conn.execute(
                        "UPDATE canonical_channel SET preferred_tvg_id = ? WHERE id = ?",
                        (want_tvg, int(row["id"])),
                    )
                if logo_differs:
                    conn.execute(
                        "UPDATE canonical_channel SET preferred_logo = ? WHERE id = ?",
                        (want_logo, int(row["id"])),
                    )
            updated.append({
                "canonical": meta.canonical,
                "id": int(row["id"]),
                "tvg_id": {"from": cur_tvg, "to": want_tvg} if tvg_differs else None,
                "logo": {"from": cur_logo, "to": want_logo} if logo_differs else None,
            })
        if apply:
            conn.commit()
    finally:
        conn.close()

    return {
        "apply": apply,
        "db": str(db_path),
        "metadata": str(metadata_path),
        "metadata_entries": len(book.entries),
        "would_update": len(updated),
        "unchanged": unchanged,
        "missing_canonical": missing,
        "updates": updated,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="只把 channel_metadata.toml 同步进 canonical_channel（不碰库存）")
    ap.add_argument("--db", required=True, help="生产 DB 路径（显式传入，不写死）")
    ap.add_argument("--metadata", required=True, help="channel_metadata.toml 路径")
    ap.add_argument("--apply", action="store_true", help="真正写库；缺省只报告")
    args = ap.parse_args(argv)

    result = sync_metadata(
        db_path=pathlib.Path(args.db),
        metadata_path=pathlib.Path(args.metadata),
        apply=bool(args.apply),
    )
    verb = "APPLIED" if result["apply"] else "DRY-RUN"
    print(f"[{verb}] metadata={result['metadata_entries']} "
          f"changed={result['would_update']} unchanged={result['unchanged']}")
    missing = result["missing_canonical"]
    if missing:
        # 只报数量 + 前若干个：43 条全列出来会把真正要看的输出淹掉
        shown = ", ".join(missing[:6])
        more = f" 等 {len(missing)} 个" if len(missing) > 6 else ""
        print(f"  ⚠ DB 里没有这些 canonical（本脚本不新建，跳过）：{shown}{more}")
    for u in result["updates"][:5]:
        print(f"  {u['canonical']}: tvg_id {u['tvg_id']} logo={'yes' if u['logo'] else 'no'}")
    if len(result["updates"]) > 5:
        print(f"  ... 其余 {len(result['updates']) - 5} 条省略")
    return 0


if __name__ == "__main__":
    sys.exit(main())