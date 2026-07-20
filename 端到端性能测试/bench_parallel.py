#!/usr/bin/env python3
"""Parallel query performance benchmark — query_dop scaling test.

Tests FullScan and IVF across query_dop = 1, 2, 4, 8 on partitioned tables.

Usage:
  python3 bench_parallel.py --dataset sift --namespace sift_ns_part --table sift1m_part
  python3 bench_parallel.py --dataset gist --namespace gist_ns_part --table gist1m_part
  python3 bench_parallel.py --all   # both SIFT + GIST
"""
import argparse, json, os, struct, subprocess, sys, time

GSQL = "gsql"
DATA_DIR = os.path.expanduser("~/测试文件")

# ── dataset config ──
DATASETS = {
    "sift": {
        "dim": 128,
        "query_file": "sift_query.fvecs",
        "fmt": "fvecs",
        "idx": 0,
    },
    "gist": {
        "dim": 960,
        "query_file": "gist-960-euclidean.hdf5",
        "fmt": "hdf5",
        "dataset_key": "test",
        "idx": 0,
    },
}

# ── test parameters ──
DOP_VALUES = [1, 2, 4, 8]
K_VALUES = [10, 100, 10000]
ROUNDS = 3


def load_query(dataset, idx=0):
    cfg = DATASETS[dataset]
    path = os.path.join(DATA_DIR, cfg["query_file"])
    if cfg["fmt"] == "fvecs":
        with open(path, "rb") as f:
            data = f.read()
        dim = struct.unpack_from("<i", data, 0)[0]
        vs = 4 + dim * 4
        off = idx * vs
        vec = struct.unpack_from(f"<{dim}f", data, off + 4)
    else:  # hdf5
        import h5py
        with h5py.File(path, "r") as f:
            vec = f[cfg["dataset_key"]][idx].tolist()
    return "[" + ",".join(str(v) for v in vec) + "]"


def gsql(sql, timeout=300):
    """Run SQL and return (stdout, stderr, returncode)."""
    r = subprocess.run(
        [GSQL, "-d", "postgres", "-p", "37000", "-c", sql],
        capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode


def parse_runtime(output):
    """Extract 'Total runtime: X.XXX ms' from EXPLAIN ANALYZE output."""
    for line in output.split("\n"):
        line = line.strip()
        if "Total runtime" in line:
            return float(line.split(":")[-1].strip().replace(" ms", ""))
    return None


def check_parallel_plan(explain_output, expected_dop):
    """Verify EXPLAIN shows expected parallel plan shape.

    Returns (is_parallel, detail):
      - query_dop=1: should NOT have LOCAL GATHER
      - query_dop>1: should have LOCAL GATHER with dop
    """
    has_gather = "LOCAL GATHER" in explain_output
    has_dop = "dop:" in explain_output

    if expected_dop == 1:
        if has_gather or has_dop:
            return False, "unexpected parallel plan for dop=1"
        return True, "serial (expected)"
    else:
        if has_gather:
            return True, f"parallel with LOCAL GATHER"
        elif has_dop:
            return True, f"parallel (dop in plan, no gather text)"
        else:
            return False, "no LOCAL GATHER or dop marker in plan"


def run_bench(dataset, ns, tbl, dim):
    qv = load_query(dataset)
    tbl_full = f"{ns}.{tbl}"

    print(f"\n{'=' * 70}")
    print(f"  {dataset.upper()} ({dim}-dim) — {tbl_full}")
    print(f"  query_dop: {DOP_VALUES}")
    print(f"{'=' * 70}")

    results = {}  # {(scenario, dop): [runtimes]}

    for dop in DOP_VALUES:
        print(f"\n--- query_dop = {dop} ---")

        # ── IVF ──
        for k in K_VALUES:
            label = f"IVF_K{k}"
            dop_setup = f"SET query_dop = {dop}; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;"

            # Cold-start warmup (exclude from results)
            sql_warm = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
            gsql(sql_warm, timeout=300)

            times = []
            plan_ok = True
            for i in range(ROUNDS):
                sql = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                out, err, rc = gsql(sql, timeout=300)
                t = parse_runtime(out)
                if t is not None:
                    times.append(t)

                # Check parallel plan on first round
                if i == 0:
                    ok, detail = check_parallel_plan(out, dop)
                    plan_ok = ok
                    marker = "✓" if ok else "✗"
                    print(f"  [{marker}] {label:12s} plan={detail}")

            if len(times) >= 3:
                median = sorted(times)[1]
                results[(f"IVF_K{k}", dop)] = median
                print(f"       {label:12s} median={median:>8.0f}ms  raw={[f'{t:.0f}' for t in times]}")
            elif times:
                results[(f"IVF_K{k}", dop)] = times[0]
                print(f"       {label:12s} single={times[0]:>8.0f}ms")

        # ── FullScan ──
        for k in K_VALUES:
            label = f"FS_K{k}"
            dop_setup = f"SET query_dop = {dop}; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;"

            # Warmup
            sql_warm = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
            gsql(sql_warm, timeout=300)

            times = []
            plan_ok = True
            for i in range(ROUNDS):
                sql = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                out, err, rc = gsql(sql, timeout=300)
                t = parse_runtime(out)
                if t is not None:
                    times.append(t)

                if i == 0:
                    ok, detail = check_parallel_plan(out, dop)
                    plan_ok = ok
                    marker = "✓" if ok else "✗"
                    print(f"  [{marker}] {label:12s} plan={detail}")

            if len(times) >= 3:
                median = sorted(times)[1]
                results[(f"FS_K{k}", dop)] = median
                print(f"       {label:12s} median={median:>8.0f}ms  raw={[f'{t:.0f}' for t in times]}")
            elif times:
                results[(f"FS_K{k}", dop)] = times[0]
                print(f"       {label:12s} single={times[0]:>8.0f}ms")

    # ── Print summary table ──
    print(f"\n{'─' * 70}")
    print(f"  Summary: {dataset.upper()} ({dim}-dim)")
    print(f"{'─' * 70}")

    # IVF table
    print(f"\n  ┌─ IVF ─" + "─" * 55)
    header = f"  │ {'K':<6}" + "".join(f" {'dop=' + str(d):>10}" for d in DOP_VALUES) + f"  {'speedup':>8}"
    print(header)
    print(f"  │{'-' * (len(header) - 4)}")
    for k in K_VALUES:
        times = [results.get((f"IVF_K{k}", d)) for d in DOP_VALUES]
        baseline = times[0] if times[0] else None
        cells = "".join(f" {t:>10.0f}" if t else f" {'?':>10}" for t in times)
        if baseline and times[-1]:
            speedup = baseline / times[-1]
            print(f"  │ K={k:<4}" + cells + f"  {speedup:>7.2f}×")
        else:
            print(f"  │ K={k:<4}" + cells + f"  {'-':>8}")

    # FullScan table
    print(f"\n  ┌─ FullScan ─" + "─" * 50)
    header = f"  │ {'K':<6}" + "".join(f" {'dop=' + str(d):>10}" for d in DOP_VALUES) + f"  {'speedup':>8}"
    print(header)
    print(f"  │{'-' * (len(header) - 4)}")
    for k in K_VALUES:
        times = [results.get((f"FS_K{k}", d)) for d in DOP_VALUES]
        baseline = times[0] if times[0] else None
        cells = "".join(f" {t:>10.0f}" if t else f" {'?':>10}" for t in times)
        if baseline and times[-1]:
            speedup = baseline / times[-1]
            print(f"  │ K={k:<4}" + cells + f"  {speedup:>7.2f}×")
        else:
            print(f"  │ K={k:<4}" + cells + f"  {'-':>8}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="Run both SIFT and GIST with default partitioned table names")
    parser.add_argument("--dataset", choices=["sift", "gist"])
    parser.add_argument("--namespace")
    parser.add_argument("--table")
    args = parser.parse_args()

    if args.all:
        configs = [
            ("sift", "sift_ns", "sift1m", 128),
            ("gist", "gist_ns_part", "gist1m_part", 960),
        ]
    elif args.dataset:
        ns = args.namespace or f"{args.dataset}_ns_part"
        tbl = args.table or f"{args.dataset}1m_part"
        dim = DATASETS[args.dataset]["dim"]
        configs = [(args.dataset, ns, tbl, dim)]
    else:
        parser.print_help()
        sys.exit(1)

    for dataset, ns, tbl, dim in configs:
        run_bench(dataset, ns, tbl, dim)

    print("\n=== Done ===")
