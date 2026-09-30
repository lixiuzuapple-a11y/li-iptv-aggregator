"""liptv 命令行入口。

所有重要行为都可通过 CLI 完成（TASK-001 验收目标 10）。
运行方式：``python -m liptv <command> [options]``
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from . import config as config_mod
from . import db as db_mod
from . import fetch as fetch_mod
from . import ingest as ingest_mod
from . import m3u as m3u_mod
from . import publish as publish_mod
from . import repo
from . import select as select_mod

DEFAULT_DB_ENV = "LIPTV_DB"

# 运行期产物目录（写动态快照必须落在这里，见 ingest.write_dynamic_snapshot）
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------------------------------------------- 通用工具

def _emit(payload, *, as_json: bool, printer) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        printer(payload)


def _print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(empty)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def _resolve_config(args) -> dict:
    return config_mod.load_config(getattr(args, "config", None))


def _resolve_db_path(args) -> str:
    explicit = getattr(args, "db", None) or os.environ.get(DEFAULT_DB_ENV)
    if explicit:
        return explicit
    return str(_resolve_config(args)["database"]["path"])


def _open_db(args):
    conn = db_mod.connect(_resolve_db_path(args))
    if db_mod.read_schema_version(conn) is None:
        raise SystemExit("数据库尚未初始化，请先运行：python -m liptv init-db")
    return conn


def _parse_resolution(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    normalized = value.lower().replace("*", "x").replace("×", "x")
    if "x" not in normalized:
        raise SystemExit(f"--resolution 需要 WxH 形式，收到：{value}")
    width, _, height = normalized.partition("x")
    return int(width), int(height)


def _selection_kwargs(args) -> dict:
    cfg = _resolve_config(args)["selection"]
    return {
        "now": getattr(args, "now", None),
        "window_days": getattr(args, "window_days", None) or cfg["window_days"],
        "max_consecutive_failures": (
            getattr(args, "max_consecutive_failures", None) or cfg["max_consecutive_failures"]
        ),
        "min_successes": getattr(args, "min_successes", None) or cfg["min_successes"],
    }


def _fetch_settings(args) -> dict:
    return config_mod.fetch_settings(_resolve_config(args))


def _fetch_limits(args) -> fetch_mod.FetchLimits:
    return fetch_mod.FetchLimits.from_mapping(_fetch_settings(args))


def _dynamic_tmp_dir(args) -> str:
    return str(_fetch_settings(args).get("dynamic_tmp_dir", "out/tmp"))


# ------------------------------------------------------------- 各命令实现

def cmd_init_db(args) -> int:
    db_path = _resolve_db_path(args)
    conn = db_mod.connect(db_path)
    version = db_mod.init_db(conn)
    tables = db_mod.table_names(conn)
    probes = repo.list_probes(conn)
    payload = {
        "db_path": db_path,
        "schema_version": version,
        "tables": tables,
        "probes": [dict(p) for p in probes],
    }
    conn.close()

    def printer(p):
        print(f"db_path        : {p['db_path']}")
        print(f"schema_version : {p['schema_version']}")
        print(f"tables({len(p['tables'])})   : {', '.join(p['tables'])}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_source_add(args) -> int:
    conn = _open_db(args)
    enabled = None
    if getattr(args, "disable", False):
        enabled = 0
    elif getattr(args, "enable", False):
        enabled = 1
    source_id = repo.add_source(
        conn, args.name, args.kind, args.url, enabled=enabled, now=args.now
    )
    row = repo.get_source(conn, source_id)
    conn.commit()
    payload = {
        "source_id": source_id,
        "name": row["name"],
        "kind": row["kind"],
        "url": row["url"],
        "enabled": int(row["enabled"]),
    }
    conn.close()

    def printer(p):
        print(f"source_id = {p['source_id']}")
        print(f"kind      = {p['kind']}  enabled = {p['enabled']}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_source_register(args) -> int:
    """把来源注册进数据库：批量来自配置 [[sources]]，或单个来自命令行。"""
    if not args.from_config and not args.name:
        raise SystemExit("source-register 需要 --from-config 或 --name NAME 之一")

    cfg = _resolve_config(args)
    if args.from_config:
        entries = config_mod.source_entries(cfg)
        if args.only_enabled:
            entries = [item for item in entries if item["enabled"]]
        if not entries:
            print("配置里没有 [[sources]] 条目（检查 --config 指向的文件）")
    else:
        entries = [
            {
                "name": args.name,
                "kind": args.kind,
                "url": args.url,
                "enabled": not args.disable,
            }
        ]

    conn = _open_db(args)
    results = []
    for item in entries:
        existed = repo.get_source_by_name(conn, item["name"]) is not None
        source_id = repo.add_source(
            conn,
            item["name"],
            item["kind"],
            item["url"],
            enabled=1 if item["enabled"] else 0,
            now=args.now,
        )
        results.append(
            {
                "source_id": source_id,
                "name": item["name"],
                "kind": item["kind"],
                "url": item["url"],
                "enabled": 1 if item["enabled"] else 0,
                "created": not existed,
            }
        )
    conn.commit()
    conn.close()

    payload = {
        "registered": len(results),
        "enabled_count": sum(1 for r in results if r["enabled"]),
        "disabled_count": sum(1 for r in results if not r["enabled"]),
        "sources": results,
    }

    def printer(p):
        if not p["sources"]:
            print("(没有需要注册的来源)")
            return
        print(f"registered={p['registered']} enabled={p['enabled_count']} "
              f"disabled={p['disabled_count']}")
        _print_table(
            p["sources"], ["source_id", "name", "kind", "enabled", "created"]
        )

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_source_status(args) -> int:
    """查看每个来源的 kind / enabled / 最近抓取状态 / 条目 active 统计。"""
    conn = _open_db(args)
    stats = repo.source_channel_stats(conn)
    rows = []
    for src in repo.list_sources(conn):
        stat = stats.get(int(src["id"]), {"active": 0, "inactive": 0, "total": 0})
        rows.append(
            {
                "id": int(src["id"]),
                "name": src["name"],
                "kind": src["kind"],
                "enabled": int(src["enabled"]),
                "url": src["url"],
                "last_fetch_at": src["last_fetch_at"],
                "last_fetch_status": src["last_fetch_status"],
                "channels_active": stat["active"],
                "channels_inactive": stat["inactive"],
                "channels_total": stat["total"],
            }
        )
    conn.close()
    payload = {"source_count": len(rows), "sources": rows}

    def printer(p):
        _print_table(
            p["sources"],
            ["id", "name", "kind", "enabled", "last_fetch_at", "last_fetch_status",
             "channels_active", "channels_inactive"],
        )

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_fetch(args) -> int:
    """抓取 fixed_m3u 来源：--source 单个，或 --all 全部 enabled。"""
    if bool(args.source) == bool(args.all):
        raise SystemExit("fetch 需要 --source NAME|ID 或 --all 之一（二选一）")

    cfg = _resolve_config(args)
    limits = fetch_mod.FetchLimits.from_mapping(config_mod.fetch_settings(cfg))
    conn = _open_db(args)

    skipped_disabled: list[dict] = []
    skipped_dynamic: list[dict] = []
    if args.all:
        targets = repo.list_sources_by_kind(conn, ingest_mod.KIND_FIXED, enabled_only=True)
        skipped_disabled = [
            {"id": int(s["id"]), "name": s["name"]}
            for s in repo.list_sources_by_kind(conn, ingest_mod.KIND_FIXED)
            if not int(s["enabled"])
        ]
        skipped_dynamic = [
            {"id": int(s["id"]), "name": s["name"]}
            for s in repo.list_sources_by_kind(conn, ingest_mod.KIND_DYNAMIC)
        ]
    else:
        row = repo.resolve_source(conn, args.source)
        if row is None:
            conn.close()
            raise SystemExit(f"找不到来源：{args.source}")
        if row["kind"] == ingest_mod.KIND_DYNAMIC:
            conn.close()
            raise SystemExit(
                f"来源 {row['name']!r} 是动态赛事源（{ingest_mod.KIND_DYNAMIC}），"
                f"不进入固定频道流程。请改用：\n"
                f"  python -m liptv dynamic-fetch --source {row['name']}"
            )
        targets = [row]

    results = [
        ingest_mod.ingest_fixed_source(conn, src, limits=limits, now=args.now)
        for src in targets
    ]
    conn.close()

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count
    payload = {
        "requested": len(results),
        "ok_count": ok_count,
        "failed_count": fail_count,
        "total_created": sum(r["created"] for r in results),
        "total_updated": sum(r["updated"] for r in results),
        "total_deactivated": sum(r["deactivated"] for r in results),
        "total_reactivated": sum(r["reactivated"] for r in results),
        "skipped_disabled": skipped_disabled,
        "skipped_dynamic": skipped_dynamic,
        "results": results,
    }

    def printer(p):
        print(f"fetched={p['requested']} ok={p['ok_count']} failed={p['failed_count']}")
        print(f"created={p['total_created']} updated={p['total_updated']} "
              f"deactivated={p['total_deactivated']} reactivated={p['total_reactivated']}")
        if p["skipped_disabled"]:
            names = ", ".join(s["name"] for s in p["skipped_disabled"])
            print(f"skipped disabled fixed sources: {names}")
        if p["skipped_dynamic"]:
            names = ", ".join(s["name"] for s in p["skipped_dynamic"])
            print(f"skipped dynamic sources (use dynamic-fetch): {names}")
        rows = [
            {
                "source": r["source_name"],
                "kind": r["kind"],
                "status": r["status"],
                "entries": r["entries"],
                "created": r["created"],
                "updated": r["updated"],
                "deactivated": r["deactivated"],
                "reactivated": r["reactivated"],
                "ms": r["duration_ms"],
            }
            for r in p["results"]
        ]
        if rows:
            print()
            _print_table(
                rows,
                ["source", "kind", "status", "entries", "created", "updated",
                 "deactivated", "reactivated", "ms"],
            )
        for r in p["results"]:
            if not r["ok"]:
                print(f"  ! {r['source_name']}: [{r['status']}] {r['error']}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0 if fail_count == 0 else 1


def cmd_dynamic_fetch(args) -> int:
    """实时获取动态赛事源：默认只打摘要（URL 已脱敏），--out 才落短时快照。"""
    if bool(args.source) == bool(args.url):
        raise SystemExit("dynamic-fetch 需要 --source NAME|ID 或 --url URL 之一（二选一）")

    settings = _fetch_settings(args)
    limits = fetch_mod.FetchLimits.from_mapping(settings)
    tmp_dir = str(settings.get("dynamic_tmp_dir", "out/tmp"))

    conn = None
    if args.url:
        source = {
            "id": None,
            "name": args.name or "ad-hoc-dynamic",
            "kind": ingest_mod.KIND_DYNAMIC,
            "url": args.url,
        }
    else:
        conn = _open_db(args)
        row = repo.resolve_source(conn, args.source)
        if row is None:
            conn.close()
            raise SystemExit(f"找不到来源：{args.source}")
        if row["kind"] != ingest_mod.KIND_DYNAMIC:
            conn.close()
            raise SystemExit(
                f"来源 {row['name']!r} 的 kind 是 {row['kind']!r}，不是 "
                f"{ingest_mod.KIND_DYNAMIC}；固定来源请用 fetch。"
            )
        source = {"id": int(row["id"]), "name": row["name"], "kind": row["kind"],
                  "url": row["url"]}
        conn.close()

    result = ingest_mod.preview_dynamic_source(
        source, limits=limits, now=args.now, include_raw=bool(args.out)
    )
    raw_text = result.pop("_raw_text", None)

    snapshot = None
    if args.out and raw_text is not None:
        try:
            snapshot = ingest_mod.write_dynamic_snapshot(
                raw_text, args.out, allowed_dir=tmp_dir
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    result["snapshot"] = snapshot
    result["out_requested"] = args.out

    def printer(p):
        print(f"source        : {p['source_name']} ({p['kind']})")
        print(f"url           : {p['url_redacted']}")
        print(f"status        : {p['status']}")
        print(f"fetched_at    : {p['fetched_at']}")
        if not p["ok"]:
            print(f"error         : [{p['status']}] {p['error']}")
            return
        print(f"entry_count   : {p['entry_count']}  (bytes={p['bytes']}, "
              f"http={p['http_status']}, {p['duration_ms']}ms)")
        if p["groups"]:
            print("groups        : " + ", ".join(
                f"{k}={v}" for k, v in sorted(p["groups"].items())
            ))
        rows = [
            {
                "index": e["index"],
                "name": e["name"],
                "group": e["group_title"] or "",
                "tags": ",".join(e["tags"]),
                "ext": e["url_ext"],
                "url": e["url_redacted"],
            }
            for e in p["entries"]
        ]
        if rows:
            print()
            _print_table(rows, ["index", "name", "group", "tags", "ext", "url"])
        print()
        print(f"note          : {p['note']}")
        if p["snapshot"]:
            print(f"snapshot      : {p['snapshot']['path']} "
                  f"({p['snapshot']['bytes']} bytes, sha256={p['snapshot']['checksum'][:12]}…)")
            print("                ⚠ 短时快照，勿提交 Git、勿长期保存")
        elif p["out_requested"]:
            print("snapshot      : 未写出（本次没有可保存的内容）")

    _emit(result, as_json=args.json, printer=printer)
    return 0 if result["ok"] else 1


def cmd_source_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_sources(conn))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(p, ["id", "name", "kind", "url", "enabled", "last_fetch_status"]))
    return 0


def cmd_import_m3u(args) -> int:
    conn = _open_db(args)
    parsed = m3u_mod.parse_file(args.path)
    source_id = repo.add_source(
        conn, args.source, args.kind, args.url or None, now=args.now
    )

    created = updated = 0
    for entry in parsed.entries:
        _, is_new = repo.upsert_source_channel(conn, source_id, entry, now=args.now)
        created += int(is_new)
        updated += int(not is_new)

    repo.mark_source_fetched(conn, source_id, "ok", now=args.now)
    conn.commit()
    payload = {
        "source_id": source_id,
        "source_name": args.source,
        "entries": parsed.entry_count,
        "created": created,
        "updated": updated,
        "has_header": parsed.has_header,
        "skipped": parsed.skipped,
        "skipped_count": parsed.skipped_count,
    }
    conn.close()

    def printer(p):
        print(f"source_id       : {p['source_id']} ({p['source_name']})")
        print(f"parsed entries  : {p['entries']}")
        print(f"source_channel  : created={p['created']} updated={p['updated']}")
        print(f"#EXTM3U header  : {p['has_header']}")
        if p["skipped"]:
            print(f"skipped         : {p['skipped']}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_source_channel_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_source_channels(conn, args.source_id))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(p, ["id", "source_id", "external_id", "raw_name", "raw_group", "active"]))
    return 0


def cmd_canonical_add(args) -> int:
    conn = _open_db(args)
    cid = repo.add_canonical_channel(
        conn, args.name, category=args.category, tvg_id=args.tvg_id,
        logo=args.logo, priority=args.priority, now=args.now,
    )
    conn.commit()
    payload = {"canonical_channel_id": cid, "name": args.name, "category": args.category}
    conn.close()
    _emit(payload, as_json=args.json,
          printer=lambda p: print(f"canonical_channel_id = {p['canonical_channel_id']}"))
    return 0


def cmd_canonical_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_canonical_channels(conn))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(p, ["id", "name", "category", "preferred_tvg_id", "priority", "enabled"]))
    return 0


def cmd_binding_add(args) -> int:
    conn = _open_db(args)
    previous = repo.get_binding(conn, args.source_channel_id)
    previous_canonical = int(previous["canonical_channel_id"]) if previous is not None else None

    try:
        binding_id, created = repo.bind_source_channel(
            conn, args.source_channel_id, args.canonical_id,
            method=args.method, confidence=args.confidence,
            rebind=args.rebind, now=args.now,
        )
    except repo.BindingConflictError as exc:
        conn.close()
        raise SystemExit(f"binding 冲突：{exc}") from exc

    conn.commit()
    rebound_from = (
        previous_canonical
        if previous_canonical is not None and previous_canonical != args.canonical_id
        else None
    )
    payload = {
        "binding_id": binding_id,
        "created": created,
        "rebound_from": rebound_from,
        "source_channel_id": args.source_channel_id,
        "canonical_channel_id": args.canonical_id,
        "method": args.method,
    }
    conn.close()

    def printer(p):
        print(f"binding_id = {p['binding_id']} (created={p['created']})")
        if p["rebound_from"] is not None:
            print(f"rebound    : canonical#{p['rebound_from']} -> canonical#{p['canonical_channel_id']}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_binding_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_bindings(conn, args.canonical_id))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(
              p, ["id", "source_channel_id", "raw_name", "canonical_channel_id", "canonical_name", "method", "confidence"]))
    return 0


def cmd_stream_sync(args) -> int:
    conn = _open_db(args)
    stats = repo.sync_streams(conn, canonical_channel_id=args.canonical_id, now=args.now)
    conn.commit()
    conn.close()
    _emit(stats, as_json=args.json,
          printer=lambda p: print(" ".join(f"{k}={v}" for k, v in p.items())))
    return 0


def cmd_stream_list(args) -> int:
    conn = _open_db(args)
    rows = repo.list_streams(conn, args.canonical_id)
    payload = []
    for row in rows:
        item = dict(row)
        if args.sources:
            item["sources"] = [
                {"source_channel_id": s["source_channel_id"], "raw_name": s["raw_name"]}
                for s in repo.list_stream_sources(conn, int(row["id"]))
            ]
        payload.append(item)
    conn.close()

    def printer(p):
        _print_table(p, ["id", "canonical_channel_id", "status", "enabled", "url"])
        if args.sources:
            for item in p:
                names = ", ".join(s["raw_name"] for s in item.get("sources", []))
                print(f"  stream {item['id']} sources: {names or '(none)'}")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_probe_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_probes(conn))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(p, ["id", "name", "location", "enabled", "last_seen_at"]))
    return 0


def cmd_probe_result_add(args) -> int:
    conn = _open_db(args)
    probe_id = repo.ensure_probe(conn, args.probe)
    width, height = _parse_resolution(args.resolution)
    result_id = repo.add_probe_result(
        conn,
        stream_id=args.stream_id,
        probe_id=probe_id,
        success=not args.fail,
        checked_at=args.checked_at,
        error_type=args.error_type,
        http_status=args.http_status,
        connect_ms=args.connect_ms,
        startup_ms=args.startup_ms,
        resolution_width=width,
        resolution_height=height,
        bitrate_kbps=args.bitrate_kbps,
        protocol=args.protocol,
        ipv_family=args.ipv_family,
    )
    conn.commit()
    payload = {"probe_result_id": result_id, "stream_id": args.stream_id,
               "probe_id": probe_id, "success": not args.fail}
    conn.close()
    _emit(payload, as_json=args.json,
          printer=lambda p: print(f"probe_result_id = {p['probe_result_id']}"))
    return 0


def cmd_probe_result_list(args) -> int:
    conn = _open_db(args)
    rows = _rows_to_dicts(repo.list_probe_results(conn, args.stream_id))
    conn.close()
    _emit(rows, as_json=args.json,
          printer=lambda p: _print_table(
              p, ["id", "stream_id", "probe_id", "checked_at", "success", "http_status",
                  "startup_ms", "resolution_width", "resolution_height", "bitrate_kbps"]))
    return 0


def _score_to_dict(score: select_mod.StreamScore) -> dict:
    data = dict(score.__dict__)
    data["pixels"] = score.pixels
    return data


def cmd_select(args) -> int:
    conn = _open_db(args)
    kwargs = _selection_kwargs(args)

    if not args.canonical_id and not args.all:
        conn.close()
        raise SystemExit("select 需要 --canonical-id ID 或 --all 之一")

    if args.canonical_id:
        scores = select_mod.score_streams(conn, args.canonical_id, **kwargs)
        payload = {
            "canonical_channel_id": args.canonical_id,
            "best_stream_id": next((s.stream_id for s in scores if s.eligible), None),
            "candidates": [_score_to_dict(s) for s in scores],
        }
        conn.close()

        def printer(p):
            print(f"canonical_channel_id = {p['canonical_channel_id']}")
            print(f"best_stream_id       = {p['best_stream_id']}")
            _print_table(
                p["candidates"],
                ["stream_id", "eligible", "success_rate", "consecutive_failures",
                 "last_success_at", "median_startup_ms", "resolution_width",
                 "resolution_height", "bitrate_kbps", "reason"],
            )

        _emit(payload, as_json=args.json, printer=printer)
        return 0

    group_order = config_mod.category_order(_resolve_config(args))
    result = select_mod.select_playlist(conn, group_order=group_order, **kwargs)
    payload = {
        "generated_at": result["generated_at"],
        "selected": [
            {"canonical_channel_id": e["canonical_channel_id"], "name": e["name"],
             "category": e["category"], "stream_id": e["stream_id"], "url": e["url"],
             "success_rate": e["score"].success_rate}
            for e in result["entries"]
        ],
        "skipped": result["skipped"],
    }
    conn.close()

    def printer(p):
        _print_table(p["selected"], ["canonical_channel_id", "name", "category", "stream_id", "success_rate"])
        if p["skipped"]:
            print()
            _print_table(p["skipped"], ["canonical_channel_id", "name"])

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def cmd_generate_m3u(args) -> int:
    conn = _open_db(args)
    cfg = _resolve_config(args)
    kwargs = _selection_kwargs(args)
    group_order = config_mod.category_order(cfg)
    result = select_mod.select_playlist(conn, group_order=group_order, **kwargs)

    channels = [
        m3u_mod.M3UChannel(
            key=e["canonical_channel_id"],
            name=e["name"],
            url=e["url"],
            tvg_id=e["preferred_tvg_id"],
            tvg_name=e["name"],
            tvg_logo=e["preferred_logo"],
            group_title=e["category"],
        )
        for e in result["entries"]
    ]

    out_path = args.out or cfg["output"]["m3u_path"]
    stats = m3u_mod.write_m3u(
        channels, out_path, keep_previous=bool(cfg["output"]["keep_previous"])
    )
    payload = dict(stats)
    payload["selected"] = len(result["entries"])
    payload["skipped"] = result["skipped"]
    conn.close()

    def printer(p):
        print(f"m3u path      : {p['path']}")
        print(f"channel_count : {p['channel_count']}")
        print(f"bytes         : {p['bytes']}")
        print(f"checksum      : {p['checksum']}")
        print(f"previous      : {p['previous']}")
        if p["skipped"]:
            print(f"skipped       : {len(p['skipped'])} channel(s) 无可用线路")

    _emit(payload, as_json=args.json, printer=printer)
    return 0


def _resolve_dynamic_sources(args, conn) -> list[dict]:
    """确定本次发布要用的动态来源。

    优先级：显式 ``--dynamic-source``（可重复，允许指向已登记但未启用的源）
    → 配置 ``publish.dynamic_sources`` → 数据库里所有 **enabled** 的 dynamic 源。
    任何情况下都只认**已登记**（或数据库里）的源，不会凭空造 URL。
    """
    tokens: list[str] = []
    explicit = list(getattr(args, "dynamic_source", None) or [])
    if explicit:
        tokens = explicit
    else:
        tokens = [str(name) for name in (config_mod.publish_settings(_resolve_config(args))["dynamic_sources"] or [])]

    resolved: list[dict] = []
    if tokens:
        for token in tokens:
            row = repo.resolve_source(conn, token)
            if row is None:
                raise SystemExit(f"找不到动态来源：{token}")
            if row["kind"] != ingest_mod.KIND_DYNAMIC:
                raise SystemExit(
                    f"来源 {row['name']!r} 的 kind 是 {row['kind']!r}，不是 "
                    f"{ingest_mod.KIND_DYNAMIC}；publish --dynamic-source 只接受动态赛事源。"
                )
            resolved.append(
                {"id": int(row["id"]), "name": row["name"], "kind": row["kind"],
                 "url": row["url"]}
            )
    else:
        for row in repo.list_sources_by_kind(conn, ingest_mod.KIND_DYNAMIC, enabled_only=True):
            resolved.append(
                {"id": int(row["id"]), "name": row["name"], "kind": row["kind"],
                 "url": row["url"]}
            )
    return resolved


def cmd_publish(args) -> int:
    """组合固定频道 + 本轮动态赛事，校验后安全发布到本地 live.m3u（TASK-003）。"""
    cfg = _resolve_config(args)
    pub_cfg = config_mod.publish_settings(cfg)
    settings = config_mod.fetch_settings(cfg)
    limits = fetch_mod.FetchLimits.from_mapping(settings)

    include_dynamic = bool(args.dynamic or args.require_dynamic or args.dynamic_source)

    conn = _open_db(args)
    try:
        dynamic_sources = _resolve_dynamic_sources(args, conn) if include_dynamic else []
        result = publish_mod.publish(
            conn,
            output_path=args.out or cfg["output"]["m3u_path"],
            group_order=config_mod.category_order(cfg),
            selection_kwargs=_selection_kwargs(args),
            keep_previous=bool(cfg["output"]["keep_previous"]),
            include_dynamic=include_dynamic,
            dynamic_sources=dynamic_sources,
            dynamic_filters=pub_cfg.get("dynamic"),
            dynamic_group_title=str(pub_cfg.get("dynamic_group_title")
                                    or publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE),
            require_dynamic=bool(args.require_dynamic),
            limits=limits,
            stamp=args.now,
            dry_run=bool(args.dry_run),
            summary_path=None if args.no_summary else (args.summary_out or pub_cfg["summary_path"]),
        )
    finally:
        conn.close()

    def printer(p):
        print(f"status        : {p['status']}")
        print(f"fixed/dynamic : {p.get('fixed_count', 0)} / {p.get('dynamic_count', 0)}"
              f"  (total {p.get('channel_count', 0)})")
        if p.get("reason"):
            print(f"reason        : {p['reason']}")
        if p.get("risk"):
            print(f"risk          : {p['risk']}")
        if p.get("path"):
            print(f"live.m3u      : {p['path']}")
            print(f"bytes/checksum: {p['bytes']} / {str(p['checksum'])[:16]}…")
            print(f"previous      : {p['previous']}")
        elif p.get("dry_run"):
            print("dry-run       : 未写入任何文件")
            if p.get("expected_checksum"):
                print(f"would write   : {p['expected_bytes']} bytes / "
                      f"{p['expected_checksum'][:16]}…")
        print(f"note          : {p['note']}")
        for warning in p.get("warnings", []):
            print(f"  ! {warning}")
        if p.get("fixed_skipped"):
            print(f"skipped fixed : {len(p['fixed_skipped'])} 个频道无合格线路")
        for src in p.get("dynamic_sources", []):
            print(f"dynamic src   : {src['source_name']} [{src['status']}] "
                  f"fetched={src['fetched_entries']} included={src['included']}")
            for why, number in (src.get("excluded_by_reason") or {}).items():
                print(f"    过滤 {number} 条：{why}")
            for sample in src.get("excluded_samples", []):
                print(f"    - #{sample['index']} {sample['name']} "
                      f"[{sample.get('group') or '(无分组)'}] → {sample['why']}")
        if p.get("summary"):
            print(f"summary       : {p['summary']['path']}")
        if p.get("summary_error"):
            print(f"summary 未写出 : {p['summary_error']}")

    _emit(result, as_json=args.json, printer=printer)
    return int(result.get("exit_code", 0))


def cmd_status(args) -> int:
    conn = _open_db(args)
    payload = {
        "db_path": _resolve_db_path(args),
        "schema_version": db_mod.read_schema_version(conn),
        "counts": repo.counts(conn),
    }
    conn.close()

    def printer(p):
        print(f"db_path        : {p['db_path']}")
        print(f"schema_version : {p['schema_version']}")
        _print_table([{"table": k, "rows": v} for k, v in p["counts"].items()], ["table", "rows"])

    _emit(payload, as_json=args.json, printer=printer)
    return 0


# ------------------------------------------------------------- 参数构建

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径（默认 config/config.toml）")
    common.add_argument("--db", help="SQLite 路径（默认取配置，或用环境变量 LIPTV_DB）")
    common.add_argument("--json", action="store_true", help="以 JSON 输出")

    parser = argparse.ArgumentParser(prog="liptv", description="Li IPTV Aggregator V1 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str):
        sp = sub.add_parser(name, parents=[common], help=help_text)
        sp.set_defaults(func=func)
        return sp

    add("init-db", cmd_init_db, "初始化数据库 schema")

    sp = add("source-add", cmd_source_add, "新增/更新一个来源")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True,
                    help="fixed_m3u / dynamic_event_m3u / local-m3u")
    sp.add_argument("--url")
    sp.add_argument("--disable", action="store_true", help="登记为禁用（不被 fetch --all 请求）")
    sp.add_argument("--enable", action="store_true", help="显式启用该来源")
    sp.add_argument("--now")

    sp = add("source-register", cmd_source_register,
             "把来源注册进数据库（--from-config 批量 / --name 单个）")
    sp.add_argument("--from-config", action="store_true",
                    help="从配置文件的 [[sources]] 批量注册")
    sp.add_argument("--only-enabled", action="store_true",
                    help="配合 --from-config：只注册配置里 enabled=true 的条目")
    sp.add_argument("--name")
    sp.add_argument("--kind", default="fixed_m3u")
    sp.add_argument("--url")
    sp.add_argument("--disable", action="store_true")
    sp.add_argument("--now")

    add("source-status", cmd_source_status,
        "查看来源 fetch 状态与条目 active/inactive 统计")

    sp = add("fetch", cmd_fetch,
             "抓取 fixed_m3u 来源（--source 单个 / --all 全部 enabled）")
    sp.add_argument("--source", help="来源名称或 ID")
    sp.add_argument("--all", action="store_true",
                    help="抓取所有 enabled 的 fixed_m3u 来源（跳过禁用源与动态源）")
    sp.add_argument("--now")

    sp = add("dynamic-fetch", cmd_dynamic_fetch,
             "实时获取动态赛事源（默认只打脱敏摘要；--out 才落短时快照）")
    sp.add_argument("--source", help="已注册的动态来源名称或 ID")
    sp.add_argument("--url", help="临时 URL（不查数据库，仅预览）")
    sp.add_argument("--name", help="配合 --url 的显示名")
    sp.add_argument("--out", help="保存短时快照的路径（必须落在 fetch.dynamic_tmp_dir 内）")
    sp.add_argument("--now")

    add("source-list", cmd_source_list, "列出来源")

    sp = add("import-m3u", cmd_import_m3u, "导入本地 M3U 为来源原始条目")
    sp.add_argument("path")
    sp.add_argument("--source", required=True, help="来源名称（不存在则自动创建）")
    sp.add_argument("--kind", default="local-m3u")
    sp.add_argument("--url")
    sp.add_argument("--now")

    sp = add("source-channel-list", cmd_source_channel_list, "列出原始频道条目")
    sp.add_argument("--source-id", type=int)

    sp = add("canonical-add", cmd_canonical_add, "新增 canonical channel")
    sp.add_argument("--name", required=True)
    sp.add_argument("--category")
    sp.add_argument("--tvg-id")
    sp.add_argument("--logo")
    sp.add_argument("--priority", type=int, default=100)
    sp.add_argument("--now")

    add("canonical-list", cmd_canonical_list, "列出 canonical channel")

    sp = add("binding-add", cmd_binding_add, "建立 source_channel → canonical_channel 绑定")
    sp.add_argument("--source-channel-id", type=int, required=True)
    sp.add_argument("--canonical-id", type=int, required=True)
    sp.add_argument("--method", default="manual")
    sp.add_argument("--confidence", type=float, default=1.0)
    sp.add_argument("--rebind", action="store_true",
                    help="显式把该 source_channel 从原 canonical 迁移到新 canonical（默认拒绝静默改绑）")
    sp.add_argument("--now")

    sp = add("binding-list", cmd_binding_list, "列出绑定关系")
    sp.add_argument("--canonical-id", type=int)

    sp = add("stream-sync", cmd_stream_sync, "从绑定归集 stream / stream_source")
    sp.add_argument("--canonical-id", type=int)
    sp.add_argument("--now")

    sp = add("stream-list", cmd_stream_list, "列出 stream")
    sp.add_argument("--canonical-id", type=int)
    sp.add_argument("--sources", action="store_true", help="同时显示来源关系")

    add("probe-list", cmd_probe_list, "列出测活节点")

    sp = add("probe-result-add", cmd_probe_result_add, "写入一条（模拟）测活结果")
    sp.add_argument("--stream-id", type=int, required=True)
    sp.add_argument("--probe", required=True)
    sp.add_argument("--fail", action="store_true", help="标记为失败")
    sp.add_argument("--checked-at")
    sp.add_argument("--error-type")
    sp.add_argument("--http-status", type=int)
    sp.add_argument("--connect-ms", type=int)
    sp.add_argument("--startup-ms", type=int)
    sp.add_argument("--resolution", help="如 1920x1080")
    sp.add_argument("--bitrate", type=int, dest="bitrate_kbps")
    sp.add_argument("--protocol")
    sp.add_argument("--ipv-family", dest="ipv_family")

    sp = add("probe-result-list", cmd_probe_result_list, "列出测活结果")
    sp.add_argument("--stream-id", type=int)

    sp = add("select", cmd_select, "按 V1 规则选线")
    sp.add_argument("--canonical-id", type=int)
    sp.add_argument("--all", action="store_true", help="对所有频道选线")
    sp.add_argument("--now")
    sp.add_argument("--window-days", type=int)
    sp.add_argument("--max-consecutive-failures", type=int)
    sp.add_argument("--min-successes", type=int)

    sp = add("generate-m3u", cmd_generate_m3u, "生成 M3U（原子写）")
    sp.add_argument("--out")
    sp.add_argument("--now")
    sp.add_argument("--window-days", type=int)
    sp.add_argument("--max-consecutive-failures", type=int)
    sp.add_argument("--min-successes", type=int)

    sp = add("publish", cmd_publish,
             "组合固定频道 + 本轮动态赛事，校验后安全发布到本地 live.m3u（TASK-003）")
    sp.add_argument("--dynamic", action="store_true",
                    help="显式启用动态赛事合并（默认不联网、完全不碰动态源）")
    sp.add_argument("--dynamic-source", action="append", metavar="NAME|ID",
                    help="指定已登记的动态来源（可重复；默认用配置或数据库里 enabled 的动态源）")
    sp.add_argument("--require-dynamic", action="store_true",
                    help="动态失败/缺失时整次拒绝（默认降级为只发布固定频道）")
    sp.add_argument("--out", help="输出路径（默认取配置 output.m3u_path）")
    sp.add_argument("--dry-run", action="store_true", help="只组合与校验，不写任何文件")
    sp.add_argument("--no-summary", action="store_true", help="不写发布摘要 JSON")
    sp.add_argument("--summary-out", help="发布摘要 JSON 路径（默认取配置 publish.summary_path）")
    sp.add_argument("--now")
    sp.add_argument("--window-days", type=int)
    sp.add_argument("--max-consecutive-failures", type=int)
    sp.add_argument("--min-successes", type=int)

    add("status", cmd_status, "查看数据库概况")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
