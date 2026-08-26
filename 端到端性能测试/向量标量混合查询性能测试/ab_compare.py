#!/usr/bin/env python3
import json, sys

base = json.load(open(sys.argv[1]))["results"]
opt = json.load(open(sys.argv[2]))["results"]
print("%-24s %12s %12s %9s %8s  %s" % ("tier", "基线(ms)", "优化(ms)", "差(ms)", "加速%", "结果一致"))
for k in opt:
    b = base.get(k)
    if not b:
        continue
    o = opt[k]
    diff = b["median_ms"] - o["median_ms"]
    ratio = (1 - o["median_ms"] / b["median_ms"]) * 100 if b["median_ms"] else 0
    same = b.get("result_ids_sample") == o.get("result_ids_sample")
    print("%-24s %12.1f %12.1f %9.1f %7.1f%%  %s" % (
        k, b["median_ms"], o["median_ms"], diff, ratio, same))
