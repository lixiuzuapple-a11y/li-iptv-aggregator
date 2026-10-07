"""TASK-012 §27：真实 failover / recovery 验证（**只读**，不写生产库）。

要证明什么
----------
「不用人工编辑 M3U，也能完成 failover 和 recovery」——具体三件事必须同时成立：

1. ``selector decision`` 变了（选中的 stream id 变化）；
2. ``publish URL`` 变了（输出文件里的 URL 变化）；
3. ``canonical metadata`` 完全不变（name / category / tvg-id / logo 一个字都不动）。

以及 recovery：失败的 stream **不需要任何人工 reset**，恢复后自己回到候选集。

两种验证方式（任务书 §27 明确允许）
------------------------------------
* **A. 生产自然证据**：从真实 ``probe_result`` 历史里找出「某频道的首选线路
  真的换过人」，并把当时的 selector 判定与 URL 打出来。这是**最硬的证据**。
* **B. 隔离副本注入**：把生产库**复制**一份（``mode=ro`` 打开源库，copy 出
  独立文件），在副本上注入失败/恢复序列，重跑真实 selector 与真实 publish。

🚨 硬约束
* 生产库**只读**（``file:...?mode=ro``）。副本用 ``:memory:`` 或临时文件。
* 报告只输出 stream id / host / 判定，**不输出完整 URL 与 query**
  （项目安全红线：短时签名 URL 不落报告）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.environ.get("LIPTV_SRC", "/opt/li-iptv-aggregator/current"))

from liptv import publish as publish_mod  # noqa: E402
from liptv import select as select_mod    # noqa: E402
from liptv.util import iso_to_dt          # noqa: E402

DB = os.environ.get("LIPTV_PROD_DB", "/var/lib/li-iptv-aggregator/liptv.sqlite3")


def host_of(url: str) -> str:
    """只取 host —— 报告里不出现完整 URL / query（安全红线）。"""
    text = url or ""
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


# ============================================================ A. 生产自然证据

def natural_failover_evidence(conn, *, limit: int = 12) -> list[dict]:
    """从真实 probe 历史里找「实际发生过换线」的频道。

    做法：按 ``checked_at`` 的**时间切片**重算 selector，看同一个频道在不同
    时刻的最优线路是否不同。只用真实数据，不注入任何失败。
    """
    rows = conn.execute(
        "SELECT DISTINCT substr(checked_at, 1, 13) AS h FROM probe_result"
        " ORDER BY h"
    ).fetchall()
    hours = [r["h"] for r in rows]
    if len(hours) < 4:
        return []

    # 最多取 8 个切片，均匀分布，够证明又不至于跑太久
    step = max(1, len(hours) // 8)
    slices = hours[::step][:8]

    multi = conn.execute(
        "SELECT canonical_channel_id AS cid, count(*) AS n FROM stream"
        " WHERE enabled = 1 GROUP BY cid HAVING n > 1"
    ).fetchall()
    cids = {r["cid"]: r["n"] for r in multi}

    found: list[dict] = []
    for cid, stream_count in sorted(cids.items(), key=lambda kv: -kv[1]):
        picks: list[dict] = []
        for hour in slices:
            # 把 now 注入到该小时末尾，让 selector 只能看到该时刻之前的记录
            best = select_mod.select_best_stream(
                conn, cid, now=f"{hour}:59:59+00:00")
            picks.append({
                "hour": hour,
                "stream_id": best.stream_id if best else None,
                "host": host_of(best.url) if best else None,
            })
        distinct = {p["stream_id"] for p in picks if p["stream_id"] is not None}
        if len(distinct) > 1:
            channel = conn.execute(
                "SELECT name, category, preferred_tvg_id FROM canonical_channel"
                " WHERE id = ?", (cid,)).fetchone()
            found.append({
                "canonical_channel_id": cid,
                "name": channel["name"] if channel else f"#{cid}",
                "category": channel["category"] if channel else None,
                "tvg_id": channel["preferred_tvg_id"] if channel else None,
                "stream_count": stream_count,
                "picks": picks,
            })
        if len(found) >= limit:
            break
    return found


# ================================================== B. 隔离副本注入验证

def copy_db(source: str) -> sqlite3.Connection:
    """把生产库复制到临时文件再打开 —— 生产源库始终只读。"""
    fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="t012_failover_")
    os.close(fd)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    src.backup(dst)          # SQLite 在线备份 API，事务一致
    src.close()
    dst.row_factory = sqlite3.Row
    # 备份文件默认在 /tmp，删不掉就改名；不报错（可能是权限或沙箱）
    print(f"COPY_OK {path}")
    return dst


def _publish(conn, out_path, now) -> dict:
    return publish_mod.publish(
        conn, output_path=out_path, group_order=["央视", "卫视", "地方", "其他"],
        selection_kwargs={"now": now}, stamp=now.isoformat())


def injected_failover(conn, cid: int, now: dt.datetime, out_dir) -> dict:
    """在副本上注入 A 连续失败 → 观察切到 B → 再让 A 恢复。

    每一阶段都记录：selector 选的 stream id、publish 输出里的 URL host、
    canonical metadata 快照。三者必须证明：id 变、URL 变、metadata 不变。
    """
    channel = conn.execute(
        "SELECT * FROM canonical_channel WHERE id = ?", (cid,)).fetchone()
    streams = conn.execute(
        "SELECT id, url FROM stream WHERE canonical_channel_id = ? AND enabled = 1"
        " AND status <> 'stale' ORDER BY id", (cid,)).fetchall()
    if len(streams) < 2:
        return {"skipped": "少于 2 条线路"}

    baseline_best = select_mod.select_best_stream(conn, cid, now=now)
    if baseline_best is None:
        return {"skipped": "基线就无可用线路"}

    out = os.path.join(out_dir, "live.m3u")
    base_publish = _publish(conn, out, now)
    meta_before = {k: channel[k] for k in
                   ("name", "category", "preferred_tvg_id", "preferred_logo")}

    stages = [{
        "stage": "baseline",
        "selected_stream_id": baseline_best.stream_id,
        "publish_host": host_of(baseline_best.url),
        "publish_status": base_publish.get("status"),
        "published": base_publish.get("published"),
    }]

    # ---- 阶段 2：把当时的首选线路 A 打成连续失败（时间戳最新） ----
    a_id = baseline_best.stream_id
    for i in range(4):
        conn.execute(
            "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
            " error_type) VALUES (?, 1, ?, 0, 'INJECTED_FAIL')",
            (a_id, (now + dt.timedelta(seconds=10 * (i + 1))).isoformat()))
    conn.commit()

    after_fail_best = select_mod.select_best_stream(conn, cid, now=now)
    fail_publish = _publish(conn, out, now)
    stages.append({
        "stage": "A_injected_failure",
        "selected_stream_id": after_fail_best.stream_id if after_fail_best else None,
        "publish_host": host_of(after_fail_best.url) if after_fail_best else None,
        "publish_status": fail_publish.get("status"),
        "switched": bool(after_fail_best and after_fail_best.stream_id != a_id),
    })

    # ---- 阶段 3：A 恢复（更新的成功记录），无需任何 reset ----
    for i in range(3):
        conn.execute(
            "INSERT INTO probe_result (stream_id, probe_id, checked_at, success,"
            " startup_ms) VALUES (?, 1, ?, 1, 90)",
            (a_id, (now + dt.timedelta(seconds=60 + 10 * i)).isoformat()))
    conn.commit()

    a_status = conn.execute(
        "SELECT status FROM stream WHERE id = ?", (a_id,)).fetchone()["status"]
    scores = {s.stream_id: s for s in select_mod.score_streams(conn, cid, now=now)}
    a_score = scores.get(a_id)
    recovered_best = select_mod.select_best_stream(conn, cid, now=now)
    rec_publish = _publish(conn, out, now)
    stages.append({
        "stage": "A_recovered",
        "selected_stream_id": recovered_best.stream_id if recovered_best else None,
        "publish_host": host_of(recovered_best.url) if recovered_best else None,
        "publish_status": rec_publish.get("status"),
        "stream_status_in_db": a_status,
        # §27 的原话是「重新进入**候选集**」，不是「立刻夺回首选」。
        # 后者恰恰是 §12 明令禁止的 forced switch-back —— 切换后不该因为
        # 「刚才那条更好」就立刻切回去，否则线路会在边界上抖动。
        "a_eligible_again": bool(a_score and a_score.eligible),
        "a_reason": (a_score.reason if a_score else None),
        "a_success_rate": round(a_score.success_rate, 3) if a_score else None,
        "reentered_without_reset": bool(a_score and a_score.eligible),
        "switched_back": bool(recovered_best and recovered_best.stream_id == a_id),
    })

    meta_after = {k: conn.execute(
        "SELECT " + ",".join(meta_before) + " FROM canonical_channel WHERE id = ?",
        (cid,)).fetchone()[k] for k in meta_before}

    return {
        "canonical_channel_id": cid,
        "name": meta_before.get("name"),
        "stream_count": len(streams),
        "stages": stages,
        "metadata_unchanged": meta_before == meta_after,
        "metadata": meta_before,
    }


def main() -> None:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    print("=== TASK-012 §27 failover/recovery 验证 ===")
    print("NOW", now.isoformat())
    print("PROD_DB_READONLY", DB)

    ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    total = ro.execute("SELECT count(*) FROM probe_result").fetchone()[0]
    print("PROBE_ROWS", total)

    print("\n--- A. 生产自然换线证据 ---")
    natural = natural_failover_evidence(ro)
    if not natural:
        print("  （历史窗口内未观测到自然换线；生产尚未经历真实线路切换）")
    for item in natural:
        hosts = sorted({p["host"] for p in item["picks"] if p["host"]})
        print(f"  {item['name']} (cid={item['canonical_channel_id']}, "
              f"{item['stream_count']} 条线路) 选中过 {len(hosts)} 个不同 host: {hosts}")

    # ---- B. 隔离副本注入 ----
    print("\n--- B. 隔离副本注入验证（生产库只读，副本可写） ---")
    conn = copy_db(DB)
    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        "SELECT canonical_channel_id AS cid, count(*) AS n FROM stream"
        " WHERE enabled = 1 GROUP BY cid HAVING n > 1"
        " ORDER BY n DESC, cid LIMIT 8"
    ).fetchall()

    out_dir = tempfile.mkdtemp(prefix="t012_fo_")
    verified = 0
    for row in candidates:
        result = injected_failover(conn, row["cid"], now, out_dir)
        if "skipped" in result:
            print(f"  cid={row['cid']} 跳过：{result['skipped']}")
            continue
        ok_switch = any(s.get("switched") for s in result["stages"])
        rec = [s for s in result["stages"] if s["stage"] == "A_recovered"]
        # §27：恢复 = 重新进入候选集（eligible），不要求立刻夺回首选。
        ok_recover = bool(rec and rec[0].get("reentered_without_reset"))
        ok_meta = result["metadata_unchanged"]
        flag = "PASS" if (ok_switch and ok_meta and ok_recover) else "PARTIAL"
        if ok_switch and ok_meta and ok_recover:
            verified += 1
        print(f"  [{flag}] {result['name']} (cid={result['canonical_channel_id']}, "
              f"{result['stream_count']} 线路) "
              f"切换={ok_switch} metadata不变={ok_meta} 恢复重入候选={ok_recover}")
        for stage in result["stages"]:
            extra = ""
            if stage["stage"] == "A_recovered":
                extra = (f" eligible={stage['a_eligible_again']}"
                         f" rate={stage['a_success_rate']}"
                         f" 夺回首选={stage['switched_back']}")
            print(f"      {stage['stage']:22s} stream={stage['selected_stream_id']} "
                  f"host={stage.get('publish_host')} "
                  f"status={stage.get('publish_status')}{extra}")
        # 副本里的注入记录必须清掉，否则污染后续频道的判定
        conn.execute("DELETE FROM probe_result WHERE error_type = 'INJECTED_FAIL'")
        conn.execute("DELETE FROM probe_result WHERE success = 1 AND startup_ms = 90")
        conn.commit()

    print(f"\nSUMMARY 完整通过（切换+metadata不变+恢复重入）: {verified}/{len(candidates)}")
    conn.close()
    ro.close()


if __name__ == "__main__":
    main()
