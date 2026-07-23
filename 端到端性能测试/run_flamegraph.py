#!/usr/bin/env python3
"""火焰图采集 — 通用脚本。

支持串行模式（非分区表）和并行模式（分区表 + dop=8）。
查询向量支持三种来源：指定文件（fvecs/hdf5）、表内取 id=1、gsql 直接获取。

用法:
  # 串行模式 — 非分区表
  python3 run_flamegraph.py --serial --dataset sift --namespace sift_ns --table sift1m
  python3 run_flamegraph.py --serial --dataset gist --namespace gist_ns --table gist1m

  # 指定查询向量文件
  python3 run_flamegraph.py --serial --dataset sift --namespace sift_ns --table sift1m \
      --query-file ~/测试文件/sift_query.fvecs --query-format fvecs

  # 不指定文件，自动取表中 id=1（默认行为，无需 --dataset）
  python3 run_flamegraph.py --serial --namespace sift_ns --table sift1m

  # 自定义场景
  python3 run_flamegraph.py --serial --namespace sift_ns --table sift1m \
      --scenarios ivf_k10,ivf_k100

  # 并行模式 — 分区表
  python3 run_flamegraph.py --namespace sift_ns_part --table sift1m_part \
      --scenarios fullscan_k10,ivf_k10,ivf_k100,ivf_k10_dop8
"""
import argparse, os, subprocess, sys, time

# ── 路径 ──
GSQL = "gsql"
STACK = os.path.expanduser("~/FlameGraph/stackcollapse-perf.pl")
FLAME = os.path.expanduser("~/FlameGraph/flamegraph.pl")

# ── 默认场景（串行安全，不含 dop=8） ──
DEFAULT_SERIAL_SCENARIOS = (
    "fullscan_k10,fullscan_k100,fullscan_k1000,"
    "ivf_k10,ivf_k100,ivf_k10000"
)
# 并行模式额外追加的场景
PARALLEL_EXTRA_SCENARIOS = "ivf_k10_dop8,ivf_k100_dop8,fullscan_k10_dop8"


def load_query_vector_from_file(path, fmt, dataset_name=None, index=0):
    """从文件读取查询向量。支持 fvecs、hdf5 和 fbin 格式。"""
    if fmt == "fvecs":
        import struct
        with open(path, "rb") as f:
            data = f.read()
        dim = struct.unpack_from("<i", data, 0)[0]
        qv = list(struct.unpack_from(f"<{dim}f", data, 4))
    elif fmt == "fbin":
        import struct
        with open(path, "rb") as f:
            nvecs = struct.unpack("<i", f.read(4))[0]
            dim = struct.unpack("<i", f.read(4))[0]
            off = 8 + index * dim * 4
            f.seek(off)
            qv = list(struct.unpack(f"<{dim}f", f.read(dim * 4)))
    elif fmt == "hdf5":
        import h5py
        with h5py.File(path, "r") as f:
            qv = f[dataset_name][index].tolist()
    else:
        raise ValueError(f"unknown format: {fmt}")
    return "[" + ",".join(str(v) for v in qv) + "]"


def load_query_vector_from_table(namespace, table):
    """从表中取 id=1 的向量作为查询向量。"""
    r = subprocess.run(
        [GSQL, "-d", "postgres", "-p", "37000", "-t", "-A",
         "-c", f"SELECT vec FROM {namespace}.{table} WHERE id=1;"],
        capture_output=True, text=True, timeout=30)
    qv = r.stdout.strip()
    if not qv:
        sys.exit(f"Failed to get query vector from {namespace}.{table} WHERE id=1")
    return qv


def gsql_run(sql, timeout=120):
    return subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", sql],
                          capture_output=True, text=True, timeout=timeout)


def collect(label, setup_sql, query_sql, rounds, out_dir, prefix):
    pid = subprocess.run("pgrep -f 'gaussdb.*37000'", capture_output=True, text=True, shell=True).stdout.strip()
    today = time.strftime("%Y-%m-%d")
    perf_dir = os.path.join(out_dir, today, "flamegraphs", "perf_data")
    svg_dir = os.path.join(out_dir, today, "flamegraphs")
    os.makedirs(perf_dir, exist_ok=True)

    print(f"[{label}] PID={pid}")

    # Warmup
    full_sql = f"{setup_sql} {query_sql}"
    gsql_run(full_sql, timeout=600)
    print("  warmup done")

    # Perf record
    pf = os.path.join(perf_dir, f"perf_{prefix}_{label}_{today}.data")
    p = subprocess.Popen(["perf", "record", "-F", "99", "-g", "-p", pid, "-o", pf, "--", "sleep", "999"])
    time.sleep(1.5)

    for i in range(rounds):
        print(f"  round {i+1}/{rounds}...")
        gsql_run(full_sql, timeout=600)

    time.sleep(0.5)
    p.terminate()
    p.wait(timeout=10)

    # SVG
    svg = os.path.join(svg_dir, f"flame_{prefix}_{label}_{today}.svg")
    cmd = f"perf script -i {pf} | {STACK} | {FLAME} --title '{prefix} {label} ({today})' --width 1200 --colors hot > {svg}"
    subprocess.run(cmd, shell=True)
    print(f"  SVG: {os.path.getsize(svg)} bytes")

    # Samples
    r = subprocess.run(["perf", "report", "-i", pf, "--stdio"], capture_output=True, text=True)
    samples = r.stdout.count("\n")
    print(f"  samples: ~{samples}")
    return pf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="火焰图采集 — 支持串行/并行模式，查询向量支持文件或表中取 id=1")
    parser.add_argument("--dataset", default=None,
                        help="数据集名称，用于输出文件前缀（如 sift/gist）。不指定时从 --table 推断")
    parser.add_argument("--namespace", required=True, help="命名空间（如 sift_ns）")
    parser.add_argument("--table", required=True, help="表名（如 sift1m）")
    parser.add_argument("--serial", action="store_true",
                        help="串行模式：默认场景不含 *_dop* 并行变体")
    parser.add_argument("--scenarios", default=None,
                        help="逗号分隔的场景列表。不指定则使用默认值")
    parser.add_argument("--query-file", default=None,
                        help="查询向量文件路径（.fvecs 或 .hdf5）。不指定则取表中 id=1")
    parser.add_argument("--query-format", default=None, choices=["fvecs", "hdf5", "fbin"],
                        help="查询向量文件格式（hdf5 需指定 dataset 名和 index）")
    parser.add_argument("--query-dataset", default="test",
                        help="hdf5 格式时的 dataset 名（默认 test）")
    parser.add_argument("--query-index", type=int, default=0,
                        help="hdf5 格式时的行索引（默认 0）")
    args = parser.parse_args()

    table = f"{args.namespace}.{args.table}"
    prefix = args.dataset or args.table
    out_dir = os.path.dirname(os.path.abspath(__file__))

    # ── 查询向量 ──
    if args.query_file:
        fmt = args.query_format
        if not fmt:
            if args.query_file.endswith(".fvecs"):
                fmt = "fvecs"
            elif args.query_file.endswith(".hdf5") or args.query_file.endswith(".h5"):
                fmt = "hdf5"
            elif args.query_file.endswith(".fbin"):
                fmt = "fbin"
            else:
                sys.exit("--query-format required (cannot auto-detect from extension)")
        qv = load_query_vector_from_file(
            os.path.expanduser(args.query_file), fmt,
            dataset_name=args.query_dataset, index=args.query_index)
        print(f"Query vector: {args.query_file} (fmt={fmt})")
    else:
        qv = load_query_vector_from_table(args.namespace, args.table)
        print(f"Query vector: {table} WHERE id=1")

    print(f"Table: {table}")

    # ── 场景定义 ──
    SCENARIOS = {
        "fullscan_k10":   ("fullscan_k10",   "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",   2),
        "fullscan_k100":  ("fullscan_k100",  "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",  2),
        "fullscan_k1000": ("fullscan_k1000", "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 1000;", 2),
        "ivf_k10":        ("ivf_k10",        "SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100":       ("ivf_k100",       "SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        "ivf_k10000":     ("ivf_k10000",     "SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "btree_point":    ("btree_point",    "",
                          f"SELECT * FROM {table} WHERE id = 500000;",                             60),
        "ivf_k10_dop8":  ("ivf_k10_dop8",  "SET query_dop = 8; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop8": ("ivf_k100_dop8", "SET query_dop = 8; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        "fullscan_k10_dop8": ("fullscan_k10_dop8", "SET query_dop = 8; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",   2),
        "ivf_k10_dop4":  ("ivf_k10_dop4",  "SET query_dop = 4; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop4": ("ivf_k100_dop4", "SET query_dop = 4; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        "ivf_k10_dop64":  ("ivf_k10_dop64",  "SET query_dop = 64; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop64": ("ivf_k100_dop64", "SET query_dop = 64; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        # DOP scaling flamegraphs (K=10000, 2 rounds each)
        "ivf_k10000_dop1":  ("ivf_k10000_dop1",  "SET query_dop = 1; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "ivf_k10000_dop2":  ("ivf_k10000_dop2",  "SET query_dop = 2; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "ivf_k10000_dop4":  ("ivf_k10000_dop4",  "SET query_dop = 4; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "ivf_k10000_dop8":  ("ivf_k10000_dop8",  "SET query_dop = 8; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "ivf_k10000_dop16": ("ivf_k10000_dop16", "SET query_dop = 16; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "ivf_k10000_dop32": ("ivf_k10000_dop32", "SET query_dop = 32; SET enable_vectorsearch = on;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        # FullScan DOP scaling (K=10000, 2 rounds)
        "fullscan_k10000_dop1":  ("fullscan_k10000_dop1",  "SET query_dop = 1; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "fullscan_k10000_dop8":  ("fullscan_k10000_dop8",  "SET query_dop = 8; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "fullscan_k10000_dop16": ("fullscan_k10000_dop16", "SET query_dop = 16; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
    }

    # ── 解析场景列表 ──
    if args.scenarios:
        scenario_names = [s.strip() for s in args.scenarios.split(",")]
    elif args.serial:
        scenario_names = [s.strip() for s in DEFAULT_SERIAL_SCENARIOS.split(",")]
    else:
        # 默认并行模式：串行场景 + dop=8 场景
        scenario_names = [s.strip() for s in DEFAULT_SERIAL_SCENARIOS.split(",")]
        scenario_names += [s.strip() for s in PARALLEL_EXTRA_SCENARIOS.split(",")]

    # 串行模式：过滤掉 dop=8 场景
    skipped = []
    if args.serial:
        filtered = []
        for name in scenario_names:
            if "_dop" in name:
                skipped.append(name)
            else:
                filtered.append(name)
        if skipped:
            print(f"[serial mode] Skipping parallel DOP scenarios: {', '.join(skipped)}")
        scenario_names = filtered

    print(f"Scenarios: {', '.join(scenario_names)}")
    mode = "serial (non-partitioned)" if args.serial else "parallel (partitioned)"
    print(f"Mode: {mode}")

    for name in scenario_names:
        if name not in SCENARIOS:
            print(f"  skip unknown scenario: {name}")
            continue
        label, setup, query, rounds = SCENARIOS[name]
        print(f"\n=== {prefix} {label} ({rounds} rounds) ===")
        collect(label, setup, query, rounds, out_dir, prefix)

    print("\nDone.")
