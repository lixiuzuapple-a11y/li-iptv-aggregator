import sqlite3, collections, sys, os
db = sys.argv[1]
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
q = lambda s: c.execute(s).fetchone()[0]
print("canonical_channel       ", q("select count(*) from canonical_channel"))
print("stream                  ", q("select count(*) from stream"))
print("probe_result rows       ", q("select count(*) from probe_result"))
print("--- error distribution ---")
for r in c.execute("select error_type, count(*) n from probe_result group by error_type order by n desc"):
    print(f"  {r[0] or '(none)':<24} {r[1]}")
print("--- success ---")
print("  pass rows            ", q("select count(*) from probe_result where success=1"))
print("  canonical with >=1 pass",
      q("select count(distinct s.canonical_channel_id) from stream s "
        "join probe_result p on p.stream_id=s.id where p.success=1"))
