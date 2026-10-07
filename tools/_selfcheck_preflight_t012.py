"""TASK-012 自检：preflight + reliability（真 HTTP，真 sqlite）。"""
import sys, pathlib, sqlite3, threading, datetime as dt
sys.path.insert(0, '.')
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from liptv import db as D, repo, preflight as P, reliability as R, stability as S

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)
STATE = {'seg_mode': 'ok'}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path
        if p == '/good.m3u8':
            body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nseg1.ts\n"
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == '/seg1.ts':
            if STATE['seg_mode'] == 'ok':
                body = b'\x47' + b'\x00' * 500
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp2t')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif STATE['seg_mode'] == '404':
                self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()
            elif STATE['seg_mode'] == 'html':
                body = b'<!DOCTYPE html><html>403</html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif p == '/html.m3u8':
            body = b'<!DOCTYPE html><html><body>Forbidden</body></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == '/empty.m3u8':
            body = b"#EXTM3U\n#EXT-X-VERSION:3\n"
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == '/gone':
            self.send_response(410); self.send_header('Content-Length', '0'); self.end_headers()
        elif p == '/geoblock':
            self.send_response(403); self.send_header('Content-Length', '0'); self.end_headers()
        else:
            self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()


srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f'http://127.0.0.1:{port}'

print('=== preflight 判定 ===')
cases = [
    ('good',      f'{B}/good.m3u8',  P.PRECHECK_PASS),
    ('html fake', f'{B}/html.m3u8',  P.PRECHECK_FAIL),
    ('seg 404',   f'{B}/seg1.ts',    None),
    ('empty hls', f'{B}/empty.m3u8', P.PRECHECK_FAIL),
    ('410',       f'{B}/gone',       P.PRECHECK_FAIL),
    ('403 geo',   f'{B}/geoblock',   P.PRECHECK_FAIL),
    ('rtmp',      'rtmp://x/y',      P.PRECHECK_UNKNOWN),
    ('bad scheme','gopher://x/y',   P.PRECHECK_FAIL),
]
for name, url, expect in cases:
    r = P.precheck_entry(url, index=0)
    flag = '' if expect is None else ('OK ' if r.result == expect else 'MISMATCH ')
    print(f'{flag}{name:10} {r.result:18} cat={r.error_category:22} seg={r.segment_ok} {r.detail or ""}')

print('--- 分片三态 ---')
for mode in ('ok', '404', 'html'):
    STATE['seg_mode'] = mode
    r = P.precheck_entry(f'{B}/good.m3u8', index=0)
    print(f'  seg={mode:5} {r.result:18} cat={r.error_category:24} {r.detail}')

print('--- unknown（不可达）---')
r = P.precheck_entry('http://127.0.0.1:9/x.m3u8', index=0)
print(' ', r.result, r.error_category, '|', r.detail)

print('=== policy：advisory 不删条目 ===')
STATE['seg_mode'] = '404'
res = P.precheck_entries([f'{B}/good.m3u8'])
print('  authoritative=True ->', P.apply_policy(res, True)['excluded_indexes'])
print('  authoritative=False ->', P.apply_policy(res, False)['excluded_indexes'],
      'advisory_fail=', P.apply_policy(res, False)['advisory_fail'])
STATE['seg_mode'] = 'ok'

print('=== 脱敏：不含 url/query ===')
import json
blob = json.dumps([x.to_dict() for x in res])
assert '127.0.0.1:%d/seg' % port not in blob, blob
print('  ok, keys=', sorted(res[0].to_dict()))

print('=== reliability summary ===')
conn = D.connect(':memory:'); D.init_db(conn)
for n in range(3):
    cid = repo.add_canonical_channel(conn, f'CH{n}', category='新闻')
    cur = conn.execute(
        "INSERT INTO stream (canonical_channel_id,url,url_hash,first_seen_at,last_seen_at,enabled,status)"
        " VALUES (?,?,?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',1,'active')",
        (cid, f'http://s{n}.example/a.m3u8', f'h{n}'))
    sid = int(cur.lastrowid)
    for i in (1, 2, 3):
        conn.execute("INSERT INTO probe_result (stream_id,probe_id,checked_at,success,error_type,startup_ms)"
                     " VALUES (?,1,?,1,NULL,?)", (sid, (NOW - dt.timedelta(hours=i)).isoformat(), 100 + i))
conn.commit()

summary = R.build_summary(
    conn, now=NOW, window_days=7,
    publish_summary={'fixed_count': 3, 'dynamic_sources': [
        {'name': 'jsnzkpg-sports', 'ok': True, 'status': 'ok', 'error_category': None},
        {'name': 'korice-ppv', 'ok': True, 'status': 'ok', 'error_category': None}],
     'dynamic_summary': {'published_counted': 5, 'selected_sources': 2,
                         'failure_policy': 'isolate', 'sources': [
                             {'name': 'jsnzkpg-sports', 'fetched': 10, 'published': 5},
                             {'name': 'korice-ppv', 'fetched': 2, 'published': 0,
                              'precheck_fail': 2, 'authoritative': False}]},
     'dynamic_fail_closed': False},
    runtime_status={'last_success_publish_at': '2026-10-07T11:50:00+00:00',
                    'current_round': {'round_id': 'r-7', 'outcome': 'ok',
                                      'publish_status': 'OK'}},
    epg_status={'last_success_epoch': 1000, 'feeds': [{'ok': True}]},
    epg_live={'channel_count': 42, 'programme_count': 11059},
)
print(R.render_human(summary))
print()
out = pathlib.Path('C:/Users/Administrator/AppData/Local/Temp/t012_rel.json')
import os
res_w = R.write_summary(summary, out)
print('write:', res_w['written'], res_w['bytes'], 'bytes ->', res_w['path'])
bad = R.write_summary(summary, pathlib.Path('Z:/nope/deep/x.json'))
print('写失败 fail-soft:', bad['written'], '|', (bad['error'] or '')[:50])
srv.shutdown()
