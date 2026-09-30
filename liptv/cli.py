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
from . import m3u as m3u_mod
from . import repo
from . import select as select_mod

DEFAULT_DB_ENV = "LIPTV_DB"


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
    source_id = repo.add_source(conn, args.name, args.kind, args.url, now=args.now)
    conn.commit()
    payload = {"source_id": source_id, "name": args.name, "kind": args.kind}
    conn.close()
    _emit(payload, as_json=args.json, printer=lambda p: print(f"source_id = {p['source_id']}"))
    return 0


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
    binding_id, created = repo.bind_source_channel(
        conn, args.source_channel_id, args.canonical_id,
        method=args.method, confidence=args.confidence, now=args.now,
    )
    conn.commit()
    payload = {
        "binding_id": binding_id,
        "created": created,
        "source_channel_id": args.source_channel_id,
        "canonical_channel_id": args.canonical_id,
        "method": args.method,
    }
    conn.close()
    _emit(payload, as_json=args.json,
          printer=lambda p: print(f"binding_id = {p['binding_id']} (created={p['created']})"))
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
    sp.add_argument("--kind", required=True)
    sp.add_argument("--url")
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
