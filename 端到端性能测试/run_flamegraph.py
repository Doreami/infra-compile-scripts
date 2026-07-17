#!/usr/bin/env python3
"""火焰图采集 — 通用脚本，支持 SIFT1M / GIST1M 等数据集。

用法:
  python3 run_flamegraph.py --dataset sift --namespace sift_ns --table sift1m
  python3 run_flamegraph.py --dataset gist --namespace gist_ns --table gist1m
"""
import argparse, os, subprocess, sys, time

# ── 数据集配置 ──
DATASETS = {
    "sift": {"dim": 128, "query_file": "sift_query.fvecs", "fmt": "fvecs"},
    "gist": {"dim": 960, "query_file": "gist-960-euclidean.hdf5", "fmt": "hdf5",
             "dataset": "test", "index": 0},
}

# ── 路径 ──
GSQL = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql")
DATA_DIR = os.path.expanduser("~/测试文件")
STACK = os.path.expanduser("~/FlameGraph/stackcollapse-perf.pl")
FLAME = os.path.expanduser("~/FlameGraph/flamegraph.pl")


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


def collect(label, setup_sql, query_sql, rounds, out_dir):
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, choices=list(DATASETS))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--scenarios", default="ivf_k10,fullscan_k10",
                        help="comma-separated: ivf_k10,fullscan_k10,fullscan_k100,...")
    parser.add_argument("--rounds-ivf", type=int, default=2)
    parser.add_argument("--rounds-fullscan", type=int, default=1)
    args = parser.parse_args()

    dataset = args.dataset
    qv = load_query_vector(dataset)
    print(f"{dataset.upper()}: dim={DATASETS[dataset]['dim']}")

    table = f"{args.namespace}.{args.table}"
    query = f"SELECT id FROM {table} ORDER BY vec <-> '{qv}'::vector LIMIT 10;"

    out_dir = os.path.dirname(os.path.abspath(__file__))

    for scenario in args.scenarios.split(","):
        scenario = scenario.strip()
        if scenario.startswith("ivf"):
            print(f"\n=== {dataset.upper()} IVF K=10 ===")
            collect("ivf_k10",
                    "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;",
                    query, args.rounds_ivf, out_dir)
        elif scenario.startswith("fullscan"):
            print(f"\n=== {dataset.upper()} FullScan K=10 ===")
            collect("fullscan_k10",
                    "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;",
                    query, args.rounds_fullscan, out_dir)

    print("\nDone.")
