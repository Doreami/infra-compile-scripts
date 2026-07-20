#!/usr/bin/env python3
"""Parallel query performance benchmark — query_dop scaling test.

Supports both serial (non-partitioned) and parallel (partitioned) modes.

Usage:
  # Serial mode — non-partitioned table, only dop=1
  python3 bench_parallel.py --serial --dataset sift
  python3 bench_parallel.py --serial --all

  # Parallel mode — partitioned table, dop=1,2,4,8
  python3 bench_parallel.py --dataset sift
  python3 bench_parallel.py --dataset gist --namespace gist_ns_part --table gist1m_part
  python3 bench_parallel.py --all

  # Custom DOP values
  python3 bench_parallel.py --dop 1,2 --dataset sift
  python3 bench_parallel.py --serial --dop 1,2 --dataset sift

  # Skip certain test types
  python3 bench_parallel.py --skip-fullscan --dataset gist
  python3 bench_parallel.py --serial --skip-ivf --dataset sift
"""
import argparse, os, struct, subprocess, sys, time

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

# ── defaults (overridden by --serial / --dop) ──
DEFAULT_PARALLEL_DOP = [1, 2, 4, 8]
DEFAULT_SERIAL_DOP = [1]
K_VALUES = [10, 100, 10000]
DEFAULT_ROUNDS = 5
DEFAULT_WARMUP = 1


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

    Returns (is_expected, detail):
      - query_dop=1: should NOT have LOCAL GATHER
      - query_dop>1: should have LOCAL GATHER (partitioned tables only)
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


def run_bench(dataset, ns, tbl, dim, dop_values, skip_ivf, skip_fullscan,
              rounds, warmup_rounds, check_plan, is_partitioned, aggregate):
    qv = load_query(dataset)
    tbl_full = f"{ns}.{tbl}"

    mode_label = "partitioned" if is_partitioned else "non-partitioned"
    print(f"\n{'=' * 70}")
    print(f"  {dataset.upper()} ({dim}-dim) — {tbl_full}  [{mode_label}]")
    print(f"  query_dop: {dop_values}")
    print(f"{'=' * 70}")

    results = {}  # {(scenario, dop): [runtimes]}

    for dop in dop_values:
        print(f"\n--- query_dop = {dop} ---")

        # ── IVF ──
        if not skip_ivf:
            for k in K_VALUES:
                label = f"IVF_K{k}"
                dop_setup = f"SET query_dop = {dop}; SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;"

                # Warmup rounds
                sql_warm = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                for _ in range(warmup_rounds):
                    gsql(sql_warm, timeout=300)

                times = []
                for i in range(rounds):
                    sql = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                    out, err, rc = gsql(sql, timeout=300)
                    t = parse_runtime(out)
                    if t is not None:
                        times.append(t)

                    # Check parallel plan on first round
                    if i == 0 and check_plan:
                        ok, detail = check_parallel_plan(out, dop)
                        marker = "✓" if ok else "✗"
                        print(f"  [{marker}] {label:12s} plan={detail}")
                    elif i == 0 and not check_plan:
                        print(f"  [-] {label:12s} plan check skipped (serial/non-partitioned)")

                if len(times) >= rounds:
                    if aggregate == "median":
                        sorted_times = sorted(times)
                        value = sorted_times[len(sorted_times) // 2]
                    else:
                        value = sum(times) / len(times)
                    results[(f"IVF_K{k}", dop)] = value
                    raw_str = ",".join(f"{t:.0f}" for t in times)
                    print(f"       {label:12s} {aggregate}={value:>8.0f}ms  raw=[{raw_str}]")
                elif times:
                    results[(f"IVF_K{k}", dop)] = times[0]
                    print(f"       {label:12s} single={times[0]:>8.0f}ms")
                else:
                    print(f"       {label:12s} FAILED (no runtime parsed)")

        # ── FullScan ──
        if not skip_fullscan:
            for k in K_VALUES:
                label = f"FS_K{k}"
                dop_setup = f"SET query_dop = {dop}; SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;"

                # Warmup rounds
                sql_warm = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                for _ in range(warmup_rounds):
                    gsql(sql_warm, timeout=300)

                times = []
                for i in range(rounds):
                    sql = f"{dop_setup} EXPLAIN ANALYZE SELECT id FROM {tbl_full} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"
                    out, err, rc = gsql(sql, timeout=300)
                    t = parse_runtime(out)
                    if t is not None:
                        times.append(t)

                    if i == 0 and check_plan:
                        ok, detail = check_parallel_plan(out, dop)
                        marker = "✓" if ok else "✗"
                        print(f"  [{marker}] {label:12s} plan={detail}")
                    elif i == 0 and not check_plan:
                        print(f"  [-] {label:12s} plan check skipped (serial/non-partitioned)")

                if len(times) >= rounds:
                    if aggregate == "median":
                        sorted_times = sorted(times)
                        value = sorted_times[len(sorted_times) // 2]
                    else:
                        value = sum(times) / len(times)
                    results[(f"FS_K{k}", dop)] = value
                    raw_str = ",".join(f"{t:.0f}" for t in times)
                    print(f"       {label:12s} {aggregate}={value:>8.0f}ms  raw=[{raw_str}]")
                elif times:
                    results[(f"FS_K{k}", dop)] = times[0]
                    print(f"       {label:12s} single={times[0]:>8.0f}ms")
                else:
                    print(f"       {label:12s} FAILED (no runtime parsed)")

    # ── Print summary tables ──
    _print_summary(dataset.upper(), dim, dop_values, K_VALUES, results,
                   "IVF", skip_ivf)
    _print_summary(dataset.upper(), dim, dop_values, K_VALUES, results,
                   "FullScan", skip_fullscan)

    return results


def _print_summary(ds_label, dim, dop_values, k_values, results,
                   scan_type, skip):
    if skip:
        return

    print(f"\n  ┌─ {scan_type} ─" + "─" * 55)
    header = f"  │ {'K':<6}" + "".join(f" {'dop=' + str(d):>10}" for d in dop_values)
    if len(dop_values) > 1:
        header += f"  {'speedup':>8}"
    print(header)
    print(f"  │{'-' * (len(header) - 4)}")
    for k in k_values:
        times = [results.get((f"{scan_type}_K{k}", d)) for d in dop_values]
        baseline = times[0] if times[0] else None
        cells = "".join(f" {t:>10.0f}" if t else f" {'?':>10}" for t in times)
        if len(dop_values) > 1 and baseline and times[-1]:
            speedup = baseline / times[-1]
            print(f"  │ K={k:<4}" + cells + f"  {speedup:>7.2f}×")
        else:
            print(f"  │ K={k:<4}" + cells)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Parallel query performance benchmark — query_dop scaling test")
    parser.add_argument("--serial", action="store_true",
                        help="Serial mode: only dop=1, skip parallel plan checks, "
                             "default to non-partitioned table names")
    parser.add_argument("--dop", type=str, default=None,
                        help="Comma-separated DOP values, e.g. '1,2,4,8'. "
                             "Overrides --serial default.")
    parser.add_argument("--skip-ivf", action="store_true",
                        help="Skip IVF index scan tests")
    parser.add_argument("--skip-fullscan", action="store_true",
                        help="Skip FullScan tests")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"Measurement rounds per test (default: {DEFAULT_ROUNDS})")
    parser.add_argument("--warmup-rounds", type=int, default=DEFAULT_WARMUP,
                        help=f"Warmup rounds per K value (default: {DEFAULT_WARMUP})")
    parser.add_argument("--aggregate", choices=["avg", "median"], default="avg",
                        help="Aggregation method: avg or median (default: avg)")
    parser.add_argument("--all", action="store_true",
                        help="Run both SIFT and GIST")
    parser.add_argument("--dataset", choices=["sift", "gist"])
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--table", default=None)
    args = parser.parse_args()

    # ── Resolve DOP values ──
    if args.dop:
        dop_values = [int(x.strip()) for x in args.dop.split(",")]
    elif args.serial:
        dop_values = DEFAULT_SERIAL_DOP
    else:
        dop_values = DEFAULT_PARALLEL_DOP

    # Whether to check plan for LOCAL GATHER
    # Only meaningful for partitioned tables with dop>1
    check_plan = not args.serial

    # ── Resolve configs ──
    if args.all:
        if args.serial:
            # Non-partitioned defaults for both
            configs = [
                ("sift", "sift_ns", "sift1m", 128),
                ("gist", "gist_ns", "gist1m", 960),
            ]
        else:
            # Partitioned defaults for both
            configs = [
                ("sift", "sift_ns_part", "sift1m_part", 128),
                ("gist", "gist_ns_part", "gist1m_part", 960),
            ]
    elif args.dataset:
        if args.namespace and args.table:
            ns, tbl = args.namespace, args.table
        elif args.serial:
            ns = f"{args.dataset}_ns"
            tbl = f"{args.dataset}1m"
        else:
            ns = f"{args.dataset}_ns_part"
            tbl = f"{args.dataset}1m_part"
        dim = DATASETS[args.dataset]["dim"]
        configs = [(args.dataset, ns, tbl, dim)]
    else:
        parser.print_help()
        sys.exit(1)

    if args.skip_ivf and args.skip_fullscan:
        sys.exit("Error: --skip-ivf and --skip-fullscan both set — nothing to test.")

    is_partitioned = not args.serial

    for dataset, ns, tbl, dim in configs:
        run_bench(dataset, ns, tbl, dim, dop_values,
                  args.skip_ivf, args.skip_fullscan,
                  args.rounds, args.warmup_rounds,
                  check_plan, is_partitioned, args.aggregate)

    print("\n=== Done ===")
