"""liptv 命令行入口。

所有重要行为都可通过 CLI 完成（TASK-001 验收目标 10）。
运行方式：``python -m liptv <command> [options]``
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys

from . import __version__
from . import backup as backup_mod
from . import config as config_mod
from . import db as db_mod
from . import deploy as deploy_mod
from . import doctor as doctor_mod
from . import fetch as fetch_mod
from . import health as health_mod
from . import ingest as ingest_mod
from . import m3u as m3u_mod
from . import probe as probe_mod
from . import publish as publish_mod
from . import repo
from . import runtime as runtime_mod
from . import select as select_mod
from . import server as server_mod
from .util import utcnow_iso

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
            # TASK-008：多动态源失败策略走 [publish.dynamic].failure_policy；
            # 非法值由 publish 层 fail-fast（不会静默回落默认值）。
            failure_policy=(pub_cfg.get("dynamic") or {}).get("failure_policy"),
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
        # TASK-008：多源失败策略与成功/失败来源数（脱敏，不含任何完整 URL）
        dyn = p.get("dynamic_summary")
        if dyn:
            print(f"failure_policy: {dyn['failure_policy']}  "
                  f"selected={dyn['selected_sources']} "
                  f"ok={dyn['successful_sources']} failed={dyn['failed_sources']}")
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


# ------------------------------------------------------------- 运行期（TASK-004）

def _runtime_settings(args) -> runtime_mod.RuntimeSettings:
    """把 [runtime] 配置与命令行覆盖项合成 RuntimeSettings。"""
    raw = dict(config_mod.runtime_settings(_resolve_config(args)))
    if getattr(args, "interval", None):
        raw["interval_seconds"] = int(args.interval)
    if getattr(args, "lock_path", None):
        raw["lock_path"] = args.lock_path
    if getattr(args, "status_path", None):
        raw["status_path"] = args.status_path
    if getattr(args, "stale_after", None):
        raw["stale_after_seconds"] = int(args.stale_after)
    if getattr(args, "dynamic", False) or getattr(args, "dynamic_source", None):
        raw["include_dynamic"] = True
    if getattr(args, "dynamic_source", None):
        raw["dynamic_sources"] = list(args.dynamic_source)
    if getattr(args, "require_dynamic", False):
        raw["include_dynamic"] = True
        raw["require_dynamic"] = True
    return runtime_mod.RuntimeSettings.from_mapping(raw)


def _open_db_for_runtime(args):
    """运行期打开数据库：**不**用 SystemExit，改成异常交给轮次状态记录。"""
    conn = db_mod.connect(_resolve_db_path(args))
    if db_mod.read_schema_version(conn) is None:
        conn.close()
        raise RuntimeError("数据库尚未初始化，请先运行：python -m liptv init-db")
    return conn


def _install_stop_handlers(scheduler: runtime_mod.Scheduler) -> list:
    """把 SIGINT / SIGTERM / SIGBREAK 变成「请求停止」，返回原处理器以便还原。"""
    installed = []

    def handler(signum, _frame):
        scheduler.request_stop(f"signal_{signum}")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            continue  # 非主线程或平台不支持 —— 交给 KeyboardInterrupt 兜底
        installed.append((sig, previous))
    return installed


def _restore_stop_handlers(installed) -> None:
    for sig, previous in installed:
        try:
            signal.signal(sig, previous)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover
            pass


def _build_round_fn(args, *, settings, output_path, group_order, pub_cfg, limits, use_now,
                    probe_settings=None, probe_should_stop=None, probe_cancel=None):
    def round_fn(*, round_id: str) -> dict:
        conn = _open_db_for_runtime(args)
        try:
            return runtime_mod.execute_round(
                conn,
                output_path=output_path,
                group_order=group_order,
                selection_kwargs=_selection_kwargs(args),
                keep_previous=bool(_resolve_config(args)["output"]["keep_previous"]),
                include_dynamic=settings.include_dynamic,
                dynamic_tokens=settings.dynamic_sources,
                dynamic_filters=pub_cfg.get("dynamic"),
                dynamic_group_title=str(
                    pub_cfg.get("dynamic_group_title")
                    or publish_mod.DEFAULT_DYNAMIC_GROUP_TITLE
                ),
                require_dynamic=settings.require_dynamic,
                limits=limits,
                summary_path=pub_cfg["summary_path"],
                # TASK-008：scheduler 每轮沿用同一份 failure_policy（15 分钟刷新不改变策略）。
                dynamic_failure_policy=(pub_cfg.get("dynamic") or {}).get("failure_policy"),
                # --now 只对 --once 有意义：loop 的每轮必须用真实时间，否则时间戳全部相同。
                stamp=getattr(args, "now", None) if use_now else None,
                # TASK-005：probe.enabled=false 时 execute_round 内部 0 次 ffprobe。
                probe_settings=probe_settings,
                probe_should_stop=probe_should_stop,
                probe_cancel=probe_cancel,
            )
        finally:
            conn.close()

    return round_fn


def _start_http_service(args, *, settings, sv, output_path, started_at):
    host = args.host if getattr(args, "host", None) is not None else sv.host
    port = args.port if getattr(args, "port", None) is not None else sv.port
    warning = runtime_mod.validate_server_binding(host)
    service = server_mod.SubscriptionServer(
        host=host,
        port=port,
        playlist_file=output_path,
        status_file=settings.status_path,
        playlist_route=sv.playlist_path,
        health_route=sv.health_path,
        stale_after_seconds=settings.stale_after_seconds,
        version=__version__,
        started_at=started_at,
        quiet=True,
    )
    return service, warning


def cmd_run(args) -> int:
    """本地调度：``run --once`` 跑一轮，``run`` 长期循环，``run --serve`` 同时起只读 HTTP。"""
    cfg = _resolve_config(args)
    settings = _runtime_settings(args)
    sv = runtime_mod.ServerSettings.from_mapping(config_mod.server_settings(cfg))
    pub_cfg = config_mod.publish_settings(cfg)
    limits = fetch_mod.FetchLimits.from_mapping(config_mod.fetch_settings(cfg))
    output_path = args.out or cfg["output"]["m3u_path"]
    group_order = config_mod.category_order(cfg)
    once = bool(args.once)

    logger = (lambda _m: None) if args.json else (lambda m: print(m, flush=True))

    lock = runtime_mod.SingleInstanceLock(
        settings.lock_path,
        stale_after_seconds=settings.stale_after_seconds,
        interval_seconds=settings.interval_seconds,
        version=__version__,
    )
    try:
        lock.acquire()
    except runtime_mod.LockError as exc:
        payload = {
            "mode": "once" if once else "loop",
            "status": "LOCKED",
            "reason": exc.reason,
            "message": str(exc),
            "holder": exc.holder,
            "lock_path": str(lock.path),
            "exit_code": runtime_mod.EXIT_LOCKED,
        }
        _emit(payload, as_json=args.json, printer=lambda p: print(
            f"本实例未启动：{p['message']}\n  reason    : {p['reason']}\n"
            f"  lock_path : {p['lock_path']}\n"
            f"  （同一套 data/output 目录只允许一个 scheduler；未执行任何 fetch/publish）"
        ))
        return runtime_mod.EXIT_LOCKED

    if lock.taken_over_from:
        logger(f"[runtime] 接管了一把过期锁：{lock.taken_over_from['reason']} "
               f"（原持有者 {lock.taken_over_from['previous'].get('hostname')}/"
               f"pid={lock.taken_over_from['previous'].get('pid')}）")

    started_at = utcnow_iso()
    status_store = runtime_mod.StatusStore(
        settings.status_path, max_rounds=settings.status_history, version=__version__
    )
    probe_settings = _probe_settings(args)
    # 停止闸门：round_fn 需要在测活过程中知道 scheduler 是否已被要求停止
    # （「不再启动新探测 + 终止在跑的 ffprobe」）。Scheduler 在 round_fn 之后才构造，
    # 因此用一个可变 holder 回填。
    stop_holder: dict = {}
    round_fn = _build_round_fn(
        args, settings=settings, output_path=output_path, group_order=group_order,
        pub_cfg=pub_cfg, limits=limits, use_now=once,
        probe_settings=probe_settings,
        probe_should_stop=lambda: bool(
            stop_holder.get("scheduler") is not None and stop_holder["scheduler"].stopped
        ),
    )
    scheduler = runtime_mod.Scheduler(
        settings=settings, round_fn=round_fn, status_store=status_store, logger=logger,
        lock=lock,
    )
    stop_holder["scheduler"] = scheduler
    logger(f"[runtime] 锁心跳周期：{scheduler.heartbeat_interval:g}s"
           f"（stale 阈值 {settings.stale_after_seconds}s）")
    if probe_settings.enabled:
        logger(f"[runtime] 真实测活已启用：节点 {probe_settings.name}"
               f"（{probe_settings.location}）ffprobe={probe_settings.ffprobe_path} "
               f"超时={probe_settings.timeout_seconds:g}s 分析={probe_settings.analyze_seconds:g}s "
               f"并发={probe_settings.max_concurrency}")
    else:
        logger("[runtime] 真实测活未启用（[probe] enabled = false）：本轮不会调用任何 ffprobe")

    # 是否随 run 起 HTTP：[server] enabled 是默认值，显式 --serve / --no-serve 覆盖它
    want_serve = args.serve if args.serve is not None else bool(sv.enabled)
    service = None
    service_warning = None
    if want_serve:
        service, service_warning = _start_http_service(
            args, settings=settings, sv=sv, output_path=output_path, started_at=started_at
        )
        service.start()
        logger(f"[runtime] 只读订阅服务：{service.url}{sv.playlist_path}"
               f"  （健康检查 {service.url}{sv.health_path}）")
        if service_warning:
            logger(f"[runtime] ⚠ {service_warning}")

    installed = _install_stop_handlers(scheduler)
    interrupted = False
    # 后台心跳：覆盖「单轮本身很久」的窗口；轮次开始/结束与长休眠由 Scheduler 自己刷新。
    scheduler.start_heartbeat()
    try:
        if once:
            entry = scheduler.run_once(next_run_at=None)
            loop_result = {"rounds": 1, "failed_rounds": 0 if entry.get("outcome") == "ok" else 1,
                           "stopped": scheduler.stopped, "stop_reason": scheduler.stop_reason,
                           "elapsed_seconds": 0}
            exit_code = int(entry.get("exit_code", runtime_mod.EXIT_ROUND_FAILED))
        else:
            loop_result = scheduler.run(max_rounds=getattr(args, "max_rounds", None))
            entry = None
            exit_code = runtime_mod.EXIT_OK
            if scheduler.lock_lost:
                exit_code = runtime_mod.EXIT_ROUND_FAILED
    except KeyboardInterrupt:
        scheduler.request_stop("keyboard_interrupt")
        interrupted = True
        loop_result = {"rounds": scheduler.rounds_run, "failed_rounds": scheduler.failed_rounds,
                       "stopped": True, "stop_reason": "keyboard_interrupt", "elapsed_seconds": None}
        entry = None
        exit_code = runtime_mod.EXIT_OK
    finally:
        scheduler.stop_heartbeat()
        _restore_stop_handlers(installed)
        if service is not None:
            service.stop()
            logger("[runtime] 只读订阅服务已关闭")
        if scheduler.lock_lost:
            logger("[runtime] ⚠ 本次运行中已失去单实例锁：后续轮次一律未执行 fetch/publish")
        released = lock.release()
        if released:
            logger(f"[runtime] 锁已释放：{lock.path}")
        elif scheduler.lock_lost:
            logger(f"[runtime] 保守处理：未删除锁文件（已无法证明它仍属于本实例）{lock.path}")
        else:
            logger("[runtime] 未删除锁文件（文件已不存在或 token 已变更）")

    payload = {
        "mode": "once" if once else "loop",
        "status": "OK" if exit_code == runtime_mod.EXIT_OK else "FAILED",
        "started_at": started_at,
        "output_path": str(output_path),
        "lock_path": str(lock.path),
        "lock_released": released,
        "lock_lost": bool(scheduler.lock_lost),
        "heartbeat_interval_seconds": scheduler.heartbeat_interval,
        "heartbeat_count": scheduler.heartbeat_count,
        "heartbeat_beats": scheduler.heartbeat_beats,
        "status_path": str(settings.status_path),
        "probe_enabled": bool(probe_settings.enabled),
        "serve": bool(service is not None),
        "service_url": f"{service.url}{sv.playlist_path}" if service is not None else None,
        "interval_seconds": settings.interval_seconds,
        "stale_after_seconds": settings.stale_after_seconds,
        "include_dynamic": settings.include_dynamic,
        "interrupted": interrupted,
        "loop": loop_result,
        "round": entry,
        "exit_code": exit_code,
    }

    def printer(p):
        print(f"mode          : {p['mode']}")
        if p.get("round"):
            r = p["round"]
            print(f"round         : {r.get('round_id')}  outcome={r.get('outcome')}  "
                  f"exit={r.get('exit_code')}")
            fetch = r.get("fetch") or {}
            print(f"fixed fetch   : ok={fetch.get('ok')} failed={fetch.get('failed')} "
                  f"requested={fetch.get('requested')}")
            pub = r.get("publish") or {}
            print(f"publish       : {pub.get('status')} fixed={pub.get('fixed_count')} "
                  f"dynamic={pub.get('dynamic_count')} published={pub.get('published')}")
            probe = r.get("probe") or {}
            if probe:
                print(f"probe         : stage={probe.get('stage')} "
                      f"requested={probe.get('requested')} succeeded={probe.get('succeeded')} "
                      f"failed={probe.get('failed')} written={probe.get('written')}")
                if probe.get("stage") == probe_mod.STAGE_FAILED:
                    print(f"⚠ 测活环境级故障（{probe.get('error_type')}）：本轮 0 条 probe_result，"
                          f"发布沿用已有历史；请先运行 python -m liptv probe-check")
            if pub.get("reason"):
                print(f"reason        : {pub['reason']}")
        print(f"loop          : rounds={p['loop'].get('rounds')} "
              f"failed={p['loop'].get('failed_rounds')} stop={p['loop'].get('stop_reason')}")
        print(f"status file   : {p['status_path']}")
        if p.get("service_url"):
            print(f"subscription  : {p['service_url']}")
        print(f"lock released : {p['lock_released']}")
        print(f"lock heartbeat: every {p['heartbeat_interval_seconds']}s  "
              f"refreshes={p['heartbeat_count']}  thread_beats={p['heartbeat_beats']}  "
              f"lost={p['lock_lost']}")

    _emit(payload, as_json=args.json, printer=printer)
    return int(exit_code)


def cmd_serve(args) -> int:
    """只起只读 HTTP 订阅服务（不抓取、不发布、不占单实例锁）。"""
    cfg = _resolve_config(args)
    settings = runtime_mod.RuntimeSettings.from_mapping(config_mod.runtime_settings(cfg))
    sv = runtime_mod.ServerSettings.from_mapping(config_mod.server_settings(cfg))
    output_path = args.out or cfg["output"]["m3u_path"]
    started_at = utcnow_iso()

    service, warning = _start_http_service(
        args, settings=settings, sv=sv, output_path=output_path, started_at=started_at
    )
    if not args.json:
        print(f"liptv serve: {service.url}{sv.playlist_path}  "
              f"(HEAD 同址；健康检查 {service.url}{sv.health_path})")
        print(f"playlist    : {output_path}")
        print(f"status file : {settings.status_path}")
        if warning:
            print(f"⚠ {warning}")
        print("只读：不代理任何视频流。Ctrl+C 停止。")
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()

    payload = {
        "status": "STOPPED",
        "serve": True,
        "playlist_path": sv.playlist_path,
        "health_path": sv.health_path,
        "playlist_file": str(output_path),
        "exit_code": 0,
    }
    if args.json:
        _emit(payload, as_json=True, printer=lambda _p: None)
    return 0


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


# ------------------------------------------------------------- 真实测活（TASK-005）

def _probe_settings(args) -> probe_mod.ProbeSettings:
    """把 [probe] 配置与命令行覆盖项合成 ProbeSettings。"""
    raw = dict(config_mod.probe_settings(_resolve_config(args)))
    if getattr(args, "ffprobe_path", None):
        raw["ffprobe_path"] = args.ffprobe_path
    return probe_mod.ProbeSettings.from_mapping(raw)


def cmd_probe_check(args) -> int:
    """只检查 ffprobe 可执行文件与版本；**不请求任何 stream**、不碰数据库。"""
    settings = _probe_settings(args)
    capability = probe_mod.check_ffprobe(settings)
    exit_code = probe_mod.EXIT_OK if capability.ok else probe_mod.EXIT_ENVIRONMENT
    payload = {
        "status": "OK" if capability.ok else "ENVIRONMENT_ERROR",
        "ok": capability.ok,
        "ffprobe_path": capability.path,
        "version": capability.version_line,
        "error_type": capability.error_type,
        "message": capability.message,
        "elapsed_ms": capability.elapsed_ms,
        "probe_enabled": bool(settings.enabled),
        "exit_code": exit_code,
    }

    def printer(p):
        print(f"ffprobe path  : {p['ffprobe_path']}")
        if p["ok"]:
            print(f"ffprobe version: {p['version']}")
            print(f"probe enabled : {p['probe_enabled']}")
            print("capability    : OK（只检查可执行文件，未请求任何 stream）")
        else:
            print(f"capability    : FAILED  error_type={p['error_type']}")
            print(f"message       : {p['message']}")
            print("说明：ffprobe 缺失/不可用属于**环境级**故障；")
            print("      此时 probe-run 会写 0 条 probe_result，绝不把整批流写成失败。")
            print("      本工具不负责下载安装 ffmpeg，请自行安装并把 ffprobe 放进 PATH。")

    _emit(payload, as_json=args.json, printer=printer)
    return exit_code


def cmd_probe_run(args) -> int:
    """对固定库存 stream 做一轮真实测活（TASK-005）。"""
    settings = _probe_settings(args)
    conn = _open_db(args)
    try:
        summary = probe_mod.run_round(
            conn,
            settings=settings,
            now=getattr(args, "now", None),
            stream_id=getattr(args, "stream_id", None),
            limit=getattr(args, "limit", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    finally:
        conn.close()

    stage = summary.get("stage")
    exit_code = (
        probe_mod.EXIT_ENVIRONMENT if stage == probe_mod.STAGE_FAILED else probe_mod.EXIT_OK
    )
    payload = {
        "status": "OK" if exit_code == probe_mod.EXIT_OK else "ENVIRONMENT_ERROR",
        "stage": stage,
        "dry_run": bool(summary.get("dry_run")),
        "probe_name": summary.get("probe_name"),
        "probe_location": summary.get("probe_location"),
        "ffprobe": summary.get("ffprobe"),
        "requested": summary.get("requested"),
        "succeeded": summary.get("succeeded"),
        "failed": summary.get("failed"),
        "written": summary.get("written"),
        "skipped": summary.get("skipped"),
        "environment_error": bool(summary.get("environment_error")),
        "environment_failed_streams": summary.get("environment_failed_streams") or [],
        "discarded_observations": summary.get("discarded_observations"),
        "error_type": summary.get("error_type"),
        "error_counts": summary.get("error_counts"),
        "reason": summary.get("reason"),
        "results": summary.get("results") or [],
        "exit_code": exit_code,
    }

    def printer(p):
        print(f"stage         : {p['stage']}   dry_run={p['dry_run']}")
        if p.get("ffprobe"):
            ff = p["ffprobe"]
            print(f"ffprobe       : ok={ff.get('ok')} path={ff.get('path')}")
        print(f"requested     : {p['requested']}   succeeded={p['succeeded']} "
              f"failed={p['failed']} written={p['written']} skipped={p['skipped']}")
        if p["environment_error"]:
            print(f"environment   : 环境级故障（{p['error_type']}）—— 本轮 0 条 probe_result；"
                  f"环境故障不是任何一条流的失败，也不会进线路健康历史")
        if p["error_counts"]:
            print(f"error_counts  : {p['error_counts']}")
        if p["reason"]:
            print(f"reason        : {p['reason']}")
        rows = p["results"]
        if rows:
            _print_table(
                [
                    {
                        "stream_id": r["stream_id"],
                        "canonical": r["canonical_name"] or "",
                        "ok": "yes" if r["success"] else "no",
                        "error_type": r["error_type"] or "",
                        "startup_ms": r["startup_ms"] if r["startup_ms"] is not None else "",
                        "resolution": r["resolution"] or "",
                        "bitrate": r["bitrate_kbps"] if r["bitrate_kbps"] is not None else "",
                        "protocol": r["protocol"] or "",
                        "url": r["url"] or "",
                    }
                    for r in rows
                ],
                ["stream_id", "canonical", "ok", "error_type", "startup_ms",
                 "resolution", "bitrate", "protocol", "url"],
            )
        print("说明：URL 一律脱敏成 scheme://host/...，不含 path/query/token。")

    _emit(payload, as_json=args.json, printer=printer)
    return exit_code


# ------------------------------------------------------------- 参数构建

def cmd_doctor(args) -> int:
    """生产 preflight 体检（TASK-006 §5）：只诊断，不抓取、不发布、不请求媒体流。"""
    config_path = args.config or config_mod.DEFAULT_CONFIG_PATH
    result = doctor_mod.collect(
        config_path,
        db_override=args.db,
        check_port=not getattr(args, "no_port_check", False),
        write_probe=not getattr(args, "no_write_probe", False),
    )
    marks = {doctor_mod.CHECK_OK: "PASS", doctor_mod.CHECK_WARN: "WARN",
             doctor_mod.CHECK_FAIL: "FAIL", doctor_mod.CHECK_SKIP: "SKIP"}

    def printer(p):
        print(f"liptv doctor : {'PASS' if p['ok'] else 'FAIL'}   config = {p['config_path']}")
        for check in p["checks"]:
            print(f"  [{marks.get(check['status'], check['status']):>4}] "
                  f"{check['id']:<8} {check['message']}")
        summary = p["summary"]
        print(f"  summary      : ok={summary.get('ok')} warn={summary.get('warn')} "
              f"fail={summary.get('fail')} skip={summary.get('skip')}")

    _emit(result, as_json=args.json, printer=printer)
    return runtime_mod.EXIT_OK if result["ok"] else runtime_mod.EXIT_ROUND_FAILED


# ------------------------------------------------------- TASK-006 部署入口

def _deploy_options(args) -> deploy_mod.DeployOptions:
    """把 argparse 命名空间转成部署选项（路径前缀 + 全部可注入开关）。"""
    layout = deploy_mod.build_layout(getattr(args, "root", None),
                                     user=getattr(args, "user", None),
                                     group=getattr(args, "group", None))
    source = pathlib.Path(getattr(args, "source", None) or REPO_ROOT).expanduser()
    return deploy_mod.DeployOptions(
        layout=layout,
        source_dir=pathlib.Path(os.path.abspath(str(source))),
        release_id=getattr(args, "release_id", None),
        python=getattr(args, "python", None),
        method=getattr(args, "method", None) or "copy",
        no_venv=bool(getattr(args, "no_venv", False)),
        force=bool(getattr(args, "force", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        start=bool(getattr(args, "start", False)),
        init_db=not bool(getattr(args, "no_init_db", False)),
        service_manager=getattr(args, "service_manager", None) or "none",
        systemctl=getattr(args, "systemctl", None),
        useradd=getattr(args, "useradd", None),
        create_user=bool(getattr(args, "create_user", False)),
        chown=not bool(getattr(args, "no_chown", False)),
        health_url=getattr(args, "health_url", None),
        skip_health=bool(getattr(args, "skip_health", False)),
        health_timeout=float(getattr(args, "health_timeout", None) or 30.0),
        health_interval=float(getattr(args, "health_interval", None) or 1.0),
        retention=int(getattr(args, "retention", None) or backup_mod.DEFAULT_RETENTION),
        templates_dir=(pathlib.Path(args.templates).expanduser()
                       if getattr(args, "templates", None) else None),
        json=bool(getattr(args, "json", False)),
    )


def _print_deploy(payload: dict) -> None:
    print(f"deploy {payload['action']:<11}: {payload['status']}")
    print(f"  prefix      : {payload['prefix']}")
    if payload.get("release_id"):
        line = f"  release     : {payload['release_id']}"
        if payload.get("previous_release"):
            line += f"  (previous {payload['previous_release']})"
        print(line)
    for step in payload["steps"]:
        detail = {k: v for k, v in step.items() if k not in {"step", "status"}}
        extra = ""
        if detail:
            text = ", ".join(f"{k}={v}" for k, v in detail.items() if v not in (None, "", {}))
            extra = f"  {text[:160]}" if text else ""
        print(f"  [{step['status']:>7}] {step['step']}{extra}")
    if payload.get("changed"):
        print(f"  changed     : {len(payload['changed'])} 个路径")
    if payload.get("skipped"):
        print(f"  skipped     : {len(payload['skipped'])} 个路径（内容相同/已存在/未启用）")
    for note in payload.get("notes") or []:
        print(f"  note        : {note}")
    service = payload.get("service") or {}
    if service.get("managed"):
        print(f"  service     : active={service.get('active')} enabled={service.get('enabled')}"
              + (f" pid={service.get('pid')}" if service.get("pid") else ""))
    health = payload.get("health") or {}
    if health:
        print(f"  health      : ok={health.get('ok')} "
              f"status={health.get('health_status')} "
              f"playlist_http={health.get('playlist_http_status')} "
              f"playlist_bytes={health.get('playlist_bytes')}")
        if health.get("detail"):
            print(f"                {health['detail']}")
    if payload.get("backup"):
        info = payload["backup"]
        if info.get("skipped"):
            print(f"  backup      : skipped（{info.get('reason')}）")
        else:
            print(f"  backup      : {info.get('path')}  bytes={info.get('bytes')}  "
                  f"kept={info.get('kept')}")


def cmd_deploy(args) -> int:
    """单机 Linux 部署与运维（TASK-006）：install / upgrade / rollback / status / …"""
    action = args.deploy_action
    try:
        options = _deploy_options(args)
        if action == "plan":
            # 必须在构造 Deployer **之前**置位：记录器在构造时就固定了 dry_run。
            options.dry_run = True
        deployer = deploy_mod.Deployer(options)

        if action in {"install", "plan"}:
            payload = deployer.install()
        elif action == "upgrade":
            payload = deployer.upgrade()
        elif action == "rollback":
            payload = deployer.rollback()
        elif action == "status":
            payload = deployer.status(probe_health=not bool(getattr(args, "no_health", False)))
        elif action == "backup":
            payload = deployer.backup()
        elif action == "restore-db":
            payload = deployer.restore_db(
                args.backup,
                yes=bool(getattr(args, "yes", False)),
                force_offline=bool(getattr(args, "force_offline_restore", False)),
            )
        else:  # pragma: no cover - argparse 已限定取值
            print(f"未知的 deploy 子命令：{action}")
            return runtime_mod.EXIT_ROUND_FAILED
    except deploy_mod.DeployError as exc:
        print(f"deploy {action} 失败：{exc}")
        return runtime_mod.EXIT_ROUND_FAILED
    except backup_mod.BackupError as exc:
        print(f"deploy {action} 失败：{exc}")
        return runtime_mod.EXIT_ROUND_FAILED

    _emit(payload, as_json=args.json, printer=_print_deploy)
    return int(payload.get("exit_code", runtime_mod.EXIT_OK))


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

    sp = add("probe-check", cmd_probe_check,
             "检查 ffprobe 可执行文件与版本（只检查，不请求任何 stream；TASK-005）")
    sp.add_argument("--ffprobe-path", dest="ffprobe_path", help="覆盖 [probe] ffprobe_path")

    sp = add("probe-run", cmd_probe_run,
             "对固定库存 stream 做一轮真实测活（需 [probe] enabled = true；TASK-005）")
    sp.add_argument("--stream-id", type=int, help="只测这一条 stream（仍须是合法库存）")
    sp.add_argument("--limit", type=int, help="本轮最多测几条（覆盖 [probe] per_round_limit）")
    sp.add_argument("--dry-run", action="store_true", help="只探测，不写数据库")
    sp.add_argument("--ffprobe-path", dest="ffprobe_path", help="覆盖 [probe] ffprobe_path")
    sp.add_argument("--now", help="本轮 checked_at（默认当前 UTC 时间）")

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

    sp = add("run",
             cmd_run,
             "本地调度：抓取固定源 → stream-sync → 统一发布（--once 一轮 / 默认长期循环 / --serve 同起 HTTP）")
    sp.add_argument("--once", action="store_true", help="只跑一轮后退出（测试 / 计划任务用）")
    sp.add_argument("--serve", dest="serve", action="store_true", default=None,
                    help="同时启动只读 HTTP 订阅服务（覆盖 [server] enabled）")
    sp.add_argument("--no-serve", dest="serve", action="store_false",
                    help="即使 [server] enabled = true 也不启动 HTTP 服务")
    sp.add_argument("--interval", type=int, help="覆盖 [runtime] interval_seconds")
    sp.add_argument("--stale-after", dest="stale_after", type=int,
                    help="覆盖 [runtime] stale_after_seconds")
    sp.add_argument("--lock-path", dest="lock_path", help="覆盖 [runtime] lock_path")
    sp.add_argument("--status-path", dest="status_path", help="覆盖 [runtime] status_path")
    sp.add_argument("--host", help="HTTP 绑定地址（覆盖 [server] host）")
    sp.add_argument("--port", type=int, help="HTTP 绑定端口（覆盖 [server] port）")
    sp.add_argument("--out", help="发布输出路径（默认取配置 output.m3u_path）")
    sp.add_argument("--dynamic", action="store_true",
                    help="每轮显式启用动态赛事合并（默认完全不碰公网动态源）")
    sp.add_argument("--dynamic-source", action="append", metavar="NAME|ID",
                    help="每轮使用的已登记动态来源（可重复）")
    sp.add_argument("--require-dynamic", action="store_true",
                    help="动态失败时整轮拒绝（默认沿用 TASK-003 降级为只发固定频道）")
    sp.add_argument("--max-rounds", type=int, dest="max_rounds",
                    help="（测试/演示用）循环最多跑几轮后退出")
    sp.add_argument("--now")

    sp = add("serve", cmd_serve, "只起只读 HTTP 订阅服务：GET/HEAD /live.m3u、GET /healthz")
    sp.add_argument("--host", help="绑定地址（默认取 [server] host，缺省 127.0.0.1）")
    sp.add_argument("--port", type=int, help="绑定端口（默认取 [server] port）")
    sp.add_argument("--out", help="要服务的 M3U 路径（默认取配置 output.m3u_path）")

    add("status", cmd_status, "查看数据库概况")

    # ------------------------------------------------- TASK-006：doctor / deploy
    sp = add("doctor", cmd_doctor,
             "生产 preflight 体检：只诊断，不抓取、不发布、不请求媒体流（TASK-006）")
    sp.add_argument("--no-port-check", dest="no_port_check", action="store_true",
                    help="跳过端口可绑定检查（升级前老服务可能仍占用端口）")
    sp.add_argument("--no-write-probe", dest="no_write_probe", action="store_true",
                    help="不写临时探针文件，只用 os.access 判断目录可写")

    deploy_parser = sub.add_parser(
        "deploy", parents=[common],
        help="单机 Linux 部署与运维：install / plan / upgrade / rollback / status / backup / restore-db（TASK-006）",
    )
    dsub = deploy_parser.add_subparsers(dest="deploy_action", required=True)

    def add_deploy(name: str, help_text: str, *, with_start: bool = False):
        dp = dsub.add_parser(name, parents=[common], help=help_text)
        dp.add_argument("--root",
                        help="安装前缀（DESTDIR 式）。生产省略 = /；非 POSIX 本机离线验证必须显式给")
        dp.add_argument("--source", help="源码树路径（默认 = 本仓库）")
        dp.add_argument("--release-id", dest="release_id",
                        help="release 目录名（默认 <git sha>-<UTC 时间戳>）")
        dp.add_argument("--python", help="创建 venv 用的解释器（默认当前解释器）")
        dp.add_argument("--method", choices=("copy", "pip"), default="copy",
                        help="项目安装方式：copy（默认，release + PYTHONPATH，不联网）/ pip")
        dp.add_argument("--no-venv", dest="no_venv", action="store_true",
                        help="不创建 venv，直接用 --python 指定的解释器（离线演练用）")
        dp.add_argument("--force", action="store_true", help="允许同名 release 重装")
        dp.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="只记录将修改的路径，一个字节都不写")
        dp.add_argument("--no-init-db", dest="no_init_db", action="store_true",
                        help="即使没有数据库也不 init（默认会显式 init 一次）")
        dp.add_argument("--service-manager", dest="service_manager",
                        choices=("none", "systemd", "process"), default="none",
                        help="服务托管：systemd（生产）/ process（离线验证）/ none（只落文件）")
        dp.add_argument("--systemctl", help="systemctl 可执行文件（.py 视为脚本，用当前解释器执行）")
        dp.add_argument("--useradd", help="useradd 可执行文件（.py 视为脚本）")
        dp.add_argument("--create-user", dest="create_user", action="store_true",
                        help="创建低权限系统用户（需要 root）")
        dp.add_argument("--no-chown", dest="no_chown", action="store_true",
                        help="不改属主（只记录归属意图）")
        dp.add_argument("--user", help=f"运行用户（默认 {deploy_mod.DEFAULT_USER}）")
        dp.add_argument("--group", help=f"运行组（默认 {deploy_mod.DEFAULT_GROUP}）")
        dp.add_argument("--health-url", dest="health_url",
                        help="健康检查基地址（默认由生产配置的 [server] host/port 推出）")
        dp.add_argument("--skip-health", dest="skip_health", action="store_true",
                        help="跳过启动后的健康闸门（不推荐；systemd active 不等于业务健康）")
        dp.add_argument("--health-timeout", dest="health_timeout", type=float,
                        help="健康检查最长等待秒数（默认 30）")
        dp.add_argument("--health-interval", dest="health_interval", type=float,
                        help="健康检查轮询间隔秒数（默认 1）")
        dp.add_argument("--retention", type=int,
                        help=f"SQLite 备份保留份数（默认 {backup_mod.DEFAULT_RETENTION}）")
        dp.add_argument("--templates", help="部署模板目录（默认 <源码树>/deploy）")
        if with_start:
            dp.add_argument("--start", action="store_true",
                            help="install 完成后立刻启动服务并等 /healthz 真的 ok")
        dp.set_defaults(func=cmd_deploy)
        return dp

    add_deploy("install", "安装到目标前缀：目录/权限、release、venv、生产配置、unit、DB init", with_start=True)
    add_deploy("plan", "等价于 install --dry-run：只列出将修改/创建哪些路径")
    add_deploy("upgrade", "预检 → 备份 DB → 停服务 → 装新 release → 起服务 → 等健康（失败自动回滚代码）")
    add_deploy("rollback", "回滚到上一个 release（只回代码，数据库必须显式 restore-db）")
    add_deploy("backup", "对 SQLite 做一次一致性备份（sqlite3 online backup API）")

    sp = dsub.add_parser("status", parents=[common], help="只读查看安装状态与健康")
    sp.add_argument("--root", help="安装前缀（DESTDIR 式）")
    sp.add_argument("--no-health", dest="no_health", action="store_true",
                    help="不主动探测 /healthz")
    sp.add_argument("--systemctl", help="systemctl 路径")
    sp.add_argument("--python", help="解释器（仅用于 process 模式的进程查询）")
    sp.set_defaults(func=cmd_deploy)

    sp = dsub.add_parser("restore-db", parents=[common],
                         help="显式恢复数据库（破坏性动作：服务必须已停，且必须 --yes）")
    sp.add_argument("backup", help="要恢复的备份文件路径")
    sp.add_argument("--yes", action="store_true",
                    help="确认覆盖现有数据库（必须显式给出；**不**代表服务已停）")
    sp.add_argument("--root", help="安装前缀（DESTDIR 式）")
    sp.add_argument("--service-manager", dest="service_manager",
                    choices=["none", "systemd", "process"], default="systemd",
                    help="用于核实/停止服务的托管方式（默认 systemd；离线验证用 process/替身）")
    sp.add_argument("--systemctl", help="systemctl 路径（离线验证可给替身脚本）")
    sp.add_argument("--force-offline-restore", dest="force_offline_restore",
                    action="store_true",
                    help="break-glass：跳过停机门禁，由调用方声明服务已停（危险，默认关闭）")
    sp.set_defaults(func=cmd_deploy)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
