#!/usr/bin/env python3
"""火焰图采集 — 通用脚本，支持 SIFT1M / GIST1M 等数据集。

支持串行模式（非分区表）和并行模式（分区表 + dop=8）。

用法:
  # 串行模式 — 非分区表，不含 dop=8 场景
  python3 run_flamegraph.py --serial --dataset sift --namespace sift_ns --table sift1m
  python3 run_flamegraph.py --serial --dataset gist --namespace gist_ns --table gist1m

  # 并行模式 — 分区表，包含 dop=8 场景
  python3 run_flamegraph.py --dataset sift --namespace sift_ns_part --table sift1m_part \
      --scenarios fullscan_k10,fullscan_k100,ivf_k10,ivf_k100,ivf_k10_dop8,ivf_k100_dop8,fullscan_k10_dop8
  python3 run_flamegraph.py --dataset gist --namespace gist_ns_part --table gist1m_part \
      --scenarios fullscan_k10,ivf_k10,ivf_k100,ivf_k10_dop8,ivf_k100_dop8

  # 自定义场景
  python3 run_flamegraph.py --dataset sift --namespace sift_ns --table sift1m \
      --scenarios ivf_k10,ivf_k100,btree_point
"""
import argparse, os, subprocess, sys, time

# ── 数据集配置 ──
DATASETS = {
    "sift": {"dim": 128, "query_file": "sift_query.fvecs", "fmt": "fvecs"},
    "gist": {"dim": 960, "query_file": "gist-960-euclidean.hdf5", "fmt": "hdf5",
             "dataset": "test", "index": 0},
}

# ── 路径 ──
GSQL = "gsql"
DATA_DIR = os.path.expanduser("~/测试文件")
STACK = os.path.expanduser("~/FlameGraph/stackcollapse-perf.pl")
FLAME = os.path.expanduser("~/FlameGraph/flamegraph.pl")

# ── 默认场景（串行安全，不含 dop=8） ──
DEFAULT_SERIAL_SCENARIOS = (
    "fullscan_k10,fullscan_k100,fullscan_k1000,"
    "ivf_k10,ivf_k100,ivf_k10000,btree_point"
)
# 并行模式额外追加的场景
PARALLEL_EXTRA_SCENARIOS = "ivf_k10_dop8,ivf_k100_dop8,fullscan_k10_dop8"


def load_query_vector(dataset):
    cfg = DATASETS[dataset]
    path = os.path.join(DATA_DIR, cfg["query_file"])
    if cfg["fmt"] == "fvecs":
        import struct
        with open(path, "rb") as f:
            data = f.read()
        dim = struct.unpack_from("<i", data, 0)[0]
        qv = list(struct.unpack_from(f"<{dim}f", data, 4))
    elif cfg["fmt"] == "hdf5":
        import h5py
        with h5py.File(path, "r") as f:
            qv = f[cfg["dataset"]][cfg["index"]].tolist()
    else:
        raise ValueError(f"unknown format: {cfg['fmt']}")
    return "[" + ",".join(str(v) for v in qv) + "]"


def gsql_run(sql, timeout=120):
    return subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", sql],
                          capture_output=True, text=True, timeout=timeout)


def collect(label, setup_sql, query_sql, rounds, out_dir, dataset):
    pid = subprocess.run(["pgrep", "-f", "gaussdb.*37000"], capture_output=True, text=True).stdout.strip()
    today = time.strftime("%Y-%m-%d")
    perf_dir = os.path.join(out_dir, today, "flamegraphs", "perf_data")
    svg_dir = os.path.join(out_dir, today, "flamegraphs")
    os.makedirs(perf_dir, exist_ok=True)

    print(f"[{label}] PID={pid}")

    # Warmup
    full_sql = f"{setup_sql} {query_sql}"
    gsql_run(full_sql, timeout=180)
    print("  warmup done")

    # Perf record
    pf = os.path.join(perf_dir, f"perf_{dataset}_{label}_{today}.data")
    p = subprocess.Popen(["perf", "record", "-F", "99", "-g", "-p", pid, "-o", pf, "--", "sleep", "999"])
    time.sleep(1.5)

    for i in range(rounds):
        print(f"  round {i+1}/{rounds}...")
        gsql_run(full_sql, timeout=180)

    time.sleep(0.5)
    p.terminate()
    p.wait(timeout=10)

    # SVG
    svg = os.path.join(svg_dir, f"flame_{dataset}_{label}_{today}.svg")
    cmd = f"perf script -i {pf} | {STACK} | {FLAME} --title '{dataset.upper()} {label} ({today})' --width 1200 --colors hot > {svg}"
    subprocess.run(cmd, shell=True)
    print(f"  SVG: {os.path.getsize(svg)} bytes")

    # Samples
    r = subprocess.run(["perf", "report", "-i", pf, "--stdio"], capture_output=True, text=True)
    samples = r.stdout.count("\n")
    print(f"  samples: ~{samples}")
    return pf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="火焰图采集 — 支持串行/并行模式")
    parser.add_argument("--dataset", required=True, choices=list(DATASETS))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--serial", action="store_true",
                        help="串行模式：默认场景不含 *_dop* 并行变体，且自动过滤用户指定场景中的 *_dop*")
    parser.add_argument("--scenarios", default=None,
                        help="逗号分隔的场景列表。不指定则根据 --serial 使用默认值")
    args = parser.parse_args()

    dataset = args.dataset
    qv = load_query_vector(dataset)
    print(f"{dataset.upper()}: dim={DATASETS[dataset]['dim']}")

    table = f"{args.namespace}.{args.table}"
    out_dir = os.path.dirname(os.path.abspath(__file__))

    # ── 场景定义: label, SQL setup, query SQL, rounds ──
    SCENARIOS = {
        "fullscan_k10":   ("fullscan_k10",   "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",   2),
        "fullscan_k100":  ("fullscan_k100",  "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",  2),
        "fullscan_k1000": ("fullscan_k1000", "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 1000;", 2),
        "ivf_k10":        ("ivf_k10",        "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100":       ("ivf_k100",       "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        "ivf_k10000":     ("ivf_k10000",     "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10000;", 2),
        "btree_point":    ("btree_point",    "",
                          f"SELECT * FROM {table} WHERE id = 500000;",                             60),
        "ivf_k10_dop8":  ("ivf_k10_dop8",  "SET query_dop = 8; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop8": ("ivf_k100_dop8", "SET query_dop = 8; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        "fullscan_k10_dop8": ("fullscan_k10_dop8", "SET query_dop = 8; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",   2),
        # DOP=4 场景（64 分区最优 DOP）
        "ivf_k10_dop4":  ("ivf_k10_dop4",  "SET query_dop = 4; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop4": ("ivf_k100_dop4", "SET query_dop = 4; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
        # DOP=64 场景（过度并行退化）
        "ivf_k10_dop64":  ("ivf_k10_dop64",  "SET query_dop = 64; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;",    5),
        "ivf_k100_dop64": ("ivf_k100_dop64", "SET query_dop = 64; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                          f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 100;",   3),
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
        print(f"\n=== {dataset.upper()} {label} ({rounds} rounds) ===")
        collect(label, setup, query, rounds, out_dir, dataset)

    print("\nDone.")
