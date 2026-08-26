#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MoReVec 过滤向量 A/B runner（回归基准执行器）。

从 MoReVec 数据生成并执行 gaussdb 过滤向量查询：
- 过滤串 + selectivities：filters/{type}_filters_0.hdf5
- 查询向量 test + 真近邻 GT mids：queries/queries_flex_{type}_sim_0_{filter_id}.hdf5
- 每个 (filter, K, DOP)：EXPLAIN ANALYZE 计时（Total runtime），用官方 GT 算 recall@K。

用法（服务器）：
  python3 bench_morevec.py --table reviews --k 10,100 --filters 0,1,2,3,4,5,6 \
      --nqueries 30 --rounds 3 --dop 1 --out /tmp/ab_morevec.json
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np
import h5py

# MoRe 过滤属性名 → gaussdb 列名（导入时 train_ 前缀被剥离）
ATTR_COLUMN = {"movies": {"avg_rating": "avgrating"}, "reviews": {}}


def load_filters(base, dtype):
    f = h5py.File(f"{base}/filters/{dtype}_filters_0.hdf5", "r")
    filters = [x.decode() if isinstance(x, bytes) else x for x in f["filters"][:]]
    selectivities = list(f["selectivities"][:])
    return filters, selectivities


def load_queries(base, dtype, filter_id):
    f = h5py.File(f"{base}/queries/queries_flex_{dtype}_sim_0_{filter_id}.hdf5", "r")
    test = np.asarray(f["test"], dtype=np.float32)  # (N, 768)
    return test


def filter_to_where(dtype, filter_str):
    """'total_votes >= 743.0' / 'No_filter' → WHERE 子句（映射属性名到 gaussdb 列）。"""
    if filter_str in ("No_filter", ["No_filter"]):
        return ""
    parts = filter_str.replace(" ", "").split()
    # filter 串形如 'attr>=value'（无空格版本）
    for op in ("<=", ">=", "!=", "=", "<", ">"):
        if op in parts[0]:
            attr, value = parts[0].split(op)
            break
    else:
        raise ValueError(f"无法解析过滤串: {filter_str}")
    col = ATTR_COLUMN.get(dtype, {}).get(attr, attr)
    return f"WHERE {col} {op} {value}"


def build_sql(dtype, table, filter_str, q, k, dop):
    where = filter_to_where(dtype, filter_str)
    qv = "[" + ",".join(repr(float(x)) for x in q) + "]"
    return (f"SET query_dop = {dop}; SET enable_vectorsearch = on;\n"
            f"EXPLAIN ANALYZE SELECT id FROM {table} {where} "
            f"ORDER BY vec <-> '{qv}'::vector LIMIT {k};\n")


def parse_runtimes(out):
    """解析 EXPLAIN ANALYZE 输出的 Total runtime: X ms。"""
    times = []
    for line in out.split("\n"):
        if "Total runtime" in line:
            try:
                times.append(float(line.split(":")[-1].strip().replace(" ms", "")))
            except ValueError:
                pass
    return times


def run_gsql(gsql, port, sql, timeout=600, tuples_only=False):
    """gsql 不支持 `-f -`，写临时文件再执行。tuples_only=True 加 -t -A（纯行，无表头/计时）。"""
    import tempfile
    tmp_dir = os.path.expanduser("~/bench_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".sql", dir=tmp_dir, delete=False) as fh:
        fh.write(sql)
        tmp = fh.name
    try:
        cmd = [gsql, "-d", "postgres", "-p", port]
        if tuples_only:
            cmd += ["-t", "-A"]
        cmd += ["-f", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser(description="MoReVec filtered-ANN A/B runner")
    p.add_argument("--table", choices=["movies", "reviews"], required=True)
    p.add_argument("--base", default=None,
                   help="MoReVec 数据根目录（默认 ~/infra-compile-scripts/端到端性能测试/测试文件/MoReVec_small）")
    p.add_argument("--namespace", default="more_ns")
    p.add_argument("--k", default="10,100")
    p.add_argument("--dop", default="1")
    p.add_argument("--filters", default="0,1,2,3,4,5,6", help="filter_id 列表")
    p.add_argument("--nqueries", type=int, default=30, help="每档取前 N 条查询")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--gsql", required=True)
    p.add_argument("--port", default="37000")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    base = args.base or os.path.expanduser(
        "~/infra-compile-scripts/端到端性能测试/测试文件/MoReVec_small")
    table = f"{args.namespace}.{args.table}"
    filters, selectivities = load_filters(base, args.table)
    filter_ids = [int(x) for x in args.filters.split(",")]
    K_V = [int(x) for x in args.k.split(",")]
    DOP_V = [int(x) for x in args.dop.split(",")]

    results = {}
    for fid in filter_ids:
        fstr = filters[fid]
        sel = float(selectivities[fid]) if fid < len(selectivities) else None
        test = load_queries(base, args.table, fid)
        nq = min(args.nqueries, len(test))
        queries = test[:nq]

        for k in K_V:
            for dop in DOP_V:
                label = f"f{fid}_sel{sel:.2f}_K{k}_dop{dop}"
                sql_all = ""
                for q in queries:
                    sql_all += build_sql(args.table, table, fstr, q, k, dop)
                # warmup
                for _ in range(args.warmup):
                    run_gsql(args.gsql, args.port, sql_all, timeout=900)
                # rounds
                round_times = []  # [round][query]
                for _ in range(args.rounds):
                    out = run_gsql(args.gsql, args.port, sql_all, timeout=900)
                    ts = parse_runtimes(out)
                    round_times.append(ts)
                # 每查询取各轮中位数；整体报告 median/P50/P99/mean
                per_query = []
                for qi in range(nq):
                    samples = [rt[qi] for rt in round_times if qi < len(rt)]
                    if samples:
                        per_query.append(float(np.median(samples)))
                if not per_query:
                    print(f"  {label}: 无计时结果", file=sys.stderr)
                    continue
                arr = np.array(per_query)
                stats = {
                    "median_ms": float(np.median(arr)),
                    "p50_ms": float(np.percentile(arr, 50)),
                    "p99_ms": float(np.percentile(arr, 99)),
                    "mean_ms": float(np.mean(arr)),
                    "n_queries": len(arr),
                }
                # 结果集一致性（正确性）：跑一次真实 SELECT 保存首条查询的返回 id。
                # 基线 vs 优化 build 的同一 (filter,K) 结果集必须逐行一致（设计 §7.4）。
                # 用 -t -A 输出纯行（无表头/无 gsql total time），避免把计时误解析为行 id。
                where = filter_to_where(args.table, fstr)
                qv = "[" + ",".join(repr(float(x)) for x in queries[0]) + "]"
                out = run_gsql(args.gsql, args.port,
                    f"SET query_dop = {dop}; SET enable_vectorsearch = on;\n"
                    f"SELECT id FROM {table} {where} ORDER BY vec <-> '{qv}'::vector LIMIT {k};\n",
                    timeout=300, tuples_only=True)
                result_ids = [int(x) for x in out.split() if x.strip().isdigit()]
                stats["result_ids_sample"] = result_ids[:k]
                results[label] = stats
                print(f"  {label}: median={stats['median_ms']:.1f}ms p99={stats['p99_ms']:.1f}ms "
                      f"n={len(result_ids)}")

    out_path = args.out
    if out_path:
        with open(out_path, "w") as fh:
            json.dump({"table": table, "filters": filters, "selectivities": selectivities,
                       "results": results}, fh, indent=2)
        print(f"\n结果已写入 {out_path}")
    else:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
