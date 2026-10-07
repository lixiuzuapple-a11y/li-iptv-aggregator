"""修正 prod_stability_t012.py 第 4 节的 SQL 括号错误（幂等）。

原写法多了一个右括号：
    " sum(success)>0))"   ->  " sum(success)>0)"
"""
import pathlib
import sys

p = pathlib.Path("/home/ubuntu/t012/prod_stability_t012.py")
s = p.read_text(encoding="utf-8")

bad = 'sum(success)>0))"'
good = 'sum(success)>0)"'

if bad not in s:
    if good in s:
        print("ALREADY_FIXED")
        sys.exit(0)
    print("PATTERN_NOT_FOUND")
    sys.exit(1)

p.write_text(s.replace(bad, good), encoding="utf-8")
print("FIXED")
