#!/usr/bin/env python3
"""Full performance benchmark for fixed-type SIFT + GIST."""
import os, subprocess, sys, struct, time

GSQL = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
if not os.path.exists(GSQL):
    GSQL = "gsql"
DATA_DIR = os.path.expanduser("~/测试文件")

def gsql(sql, timeout=600):
    r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", sql],
                       capture_output=True, text=True, timeout=timeout)
    out = r.stdout + r.stderr
    for line in out.split("\n"):
        line = line.strip()
        if "Total runtime" in line:
            return line.split(":")[-1].strip().replace(" ms", "")
    return "?"

def load_query(dataset, idx=0):
    if dataset == "sift":
        with open(os.path.join(DATA_DIR, "sift_query.fvecs"), "rb") as f:
            data = f.read()
        dim = struct.unpack_from("<i", data, 0)[0]
        vs = 4 + dim * 4
        off = idx * vs
        vec = struct.unpack_from(f"<{dim}f", data, off + 4)
    elif dataset == "deep":
        # fbin format: [nvecs: i32][dim: i32][float32...]
        deep_q = os.path.join(DATA_DIR, "query.public.10K.fbin")
        if not os.path.exists(deep_q):
            deep_q = os.path.expanduser("~/big-ann-benchmarks/data/deep1b/query.public.10K.fbin")
        with open(deep_q, "rb") as f:
            nvecs = struct.unpack("<i", f.read(4))[0]
            dim = struct.unpack("<i", f.read(4))[0]
            off_bytes = 8 + idx * dim * 4
            f.seek(off_bytes)
            vec = struct.unpack(f"<{dim}f", f.read(dim * 4))
    else:  # gist
        import h5py
        with h5py.File(os.path.join(DATA_DIR, "gist-960-euclidean.hdf5"), "r") as f:
            vec = f["test"][idx].tolist()
    return "[" + ",".join(str(v) for v in vec) + "]"

def run_bench(dataset, ns, tbl, dim, ks):
    qv = load_query(dataset)
    print(f"\n{'='*60}")
    print(f"  {dataset.upper()} ({dim}-dim)")
    print(f"{'='*60}")

    # Per-K warmup: IVF first, then FullScan
    for k in ks:
        # IVF
        sql_warm = f"""SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;
EXPLAIN ANALYZE SELECT id FROM {ns}.{tbl} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"""
        gsql(sql_warm, timeout=120)
        times = []
        for _ in range(3):
            sql = f"""SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;
EXPLAIN ANALYZE SELECT id FROM {ns}.{tbl} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"""
            t = gsql(sql)
            times.append(float(t) if t != "?" else 0.0)
        print(f"  IVF       K={k:>5}: {sorted(times)[1]:>8.0f}ms")

        # FullScan
        times = []
        for _ in range(3):
            sql = f"""SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;
EXPLAIN ANALYZE SELECT id FROM {ns}.{tbl} ORDER BY vec <-> '{qv}'::vector LIMIT {k};"""
            t = gsql(sql)
            times.append(float(t) if t != "?" else 0.0)
        print(f"  FullScan  K={k:>5}: {sorted(times)[1]:>8.0f}ms")

# SIFT
run_bench("sift", "sift_ns", "sift1m", 128, [10, 100, 10000])

# GIST
run_bench("gist", "gist_ns", "gist1m", 960, [10, 100, 10000])

# DEEP-100M — FullScan 可能 >5min, 由 gsql(timeout=600) 保障
run_bench("deep", "deep_ns", "deep100m", 96, [10, 100, 10000])

print("\n=== Done ===")
