"""TASK-012 自检：stability 派生层（真库、真函数，不用 mock）。"""
import sys, sqlite3, datetime as dt
sys.path.insert(0, '.')
from liptv import db as D, repo, stability as S

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.timezone.utc)


def mk():
    conn = D.connect(':memory:')
    D.init_db(conn)
    return conn


def add_stream(conn, cid, url):
    cur = conn.execute(
        "INSERT INTO stream (canonical_channel_id, url, url_hash, first_seen_at,"
        " last_seen_at, enabled, status) VALUES (?, ?, ?, '2026-10-01T00:00:00+00:00',"
        " '2026-10-01T00:00:00+00:00', 1, 'active')",
        (cid, url, 'h' + str(abs(hash(url)))))
    return int(cur.lastrowid)


def probe(conn, sid, ok, hours_ago, err=None):
    stamp = (NOW - dt.timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        "INSERT INTO probe_result (stream_id, probe_id, checked_at, success, error_type)"
        " VALUES (?, 1, ?, ?, ?)", (sid, stamp, 1 if ok else 0, err))
    conn.commit()


print("=== 1. 无 probe -> UNKNOWN ===")
c = mk(); cid = repo.add_canonical_channel(c, 'X', category='新闻')
h = S.derive_channel_health(c, cid, now=NOW)
print(h.state, '|', h.reason)

print("=== 2. 样本不足 2 -> UNKNOWN（不算 STABLE）===")
c = mk(); cid = repo.add_canonical_channel(c, 'X', category='新闻')
sid = add_stream(c, cid, 'http://a/1.m3u8')
probe(c, sid, True, 1); probe(c, sid, True, 2)
h = S.derive_channel_health(c, cid, now=NOW)
print(h.state, '| samples', h.probe_count)

print("=== 3. 5/6 成功 + 最近成功 -> STABLE ===")
c = mk(); cid = repo.add_canonical_channel(c, 'X', category='新闻')
sid = add_stream(c, cid, 'http://a/1.m3u8')
probe(c, sid, True, 1); probe(c, sid, True, 2); probe(c, sid, True, 3)
probe(c, sid, True, 4); probe(c, sid, False, 5, 'TIMEOUT'); probe(c, sid, True, 6)
h = S.derive_channel_health(c, cid, now=NOW)
print(h.state, '| rate', round(h.success_rate, 3), '| sel', h.selected_stream_id == sid)

print("=== 4. 连续失败 3 -> FAILED（自然 skip）===")
c = mk(); cid = repo.add_canonical_channel(c, 'X', category='新闻')
sid = add_stream(c, cid, 'http://a/1.m3u8')
probe(c, sid, True, 10)
for i in (1, 2, 3):
    probe(c, sid, False, i, 'TIMEOUT')
h = S.derive_channel_health(c, cid, now=NOW)
print(h.state, '| sel', h.selected_stream_id, '|', h.reason)

print("=== 5. stale success（半年前成功+近 3 次失败中 2 失败）===")
c = mk(); cid = repo.add_canonical_channel(c, 'X', category='新闻')
sid = add_stream(c, cid, 'http://a/1.m3u8')
probe(c, sid, True, 24 * 200)   # 200 天前
probe(c, sid, False, 2, 'TIMEOUT'); probe(c, sid, False, 1, 'TIMEOUT')
h = S.derive_channel_health(c, cid, now=NOW)
print(h.state, '| age_h', round(h.stream_total and h.probe_count, 0), '|', h.reason)

print("=== 6. failover：两条线，A 连续失败后选 B，metadata 不变 ===")
c = mk(); cid = repo.add_canonical_channel(c, 'CCTV-1 综合', category='新闻', tvg_id='CCTV1', logo='http://l/1.png')
sa = add_stream(c, cid, 'http://a/1.m3u8'); sb = add_stream(c, cid, 'http://b/1.m3u8')
for i in (6, 5, 4, 3):
    probe(c, sa, True, i); probe(c, sb, True, i)
before = S.derive_channel_health(c, cid, now=NOW)
print('before sel', before.selected_stream_id, '(sa=%d sb=%d)' % (sa, sb), before.state)
for i in (1, 2, 3):
    probe(c, sa, False, i, 'TIMEOUT'); probe(c, sb, True, i)
after = S.derive_channel_health(c, cid, now=NOW)
print('after  sel', after.selected_stream_id, after.state, '| A eligible?', sa in after.candidate_stream_ids)

print("=== 7. recovery：A 恢复后重新进候选 ===")
probe(c, sa, True, 0.2)
rec = S.derive_channel_health(c, cid, now=NOW)
print('A back in candidates?', sa in rec.candidate_stream_ids)

print("=== 8. aggregate 真实稳定率 ===")
c = mk()
ids = []
for n in range(4):
    cid = repo.add_canonical_channel(c, 'CH%d' % n, category='新闻')
    ids.append(cid)
    sid = add_stream(c, cid, 'http://a/%d.m3u8' % n)
    for i in (1, 2, 3):
        probe(c, sid, True, i)
agg = S.aggregate(S.derive_all(c, now=NOW))
print(agg['stable'], '/', agg['canonical_total'], '=', agg['stable_rate'])
