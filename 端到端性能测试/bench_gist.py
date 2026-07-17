#!/usr/bin/env python3
"""Quick GIST1M EXPLAIN ANALYZE benchmarks."""
import subprocess, os, sys, time, h5py, json

GSQL = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql")
HDF5 = os.path.expanduser("~/测试文件/gist-960-euclidean.hdf5")
f = h5py.File(HDF5, "r")
qv = f["test"][0].tolist()
f.close()
qv_str = "[" + ",".join(str(v) for v in qv) + "]"

def run(sql, label=""):
    t0 = time.time()
    r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    t1 = time.time()
    ms = (t1 - t0) * 1000
    # Extract actual time from EXPLAIN output
    for line in r.stdout.split("\n"):
        if "actual time" in line:
            print(f"  {label}: {line.strip()}")
    return ms

print("=== GIST1M Quick Benchmarks ===")
print(f"Query vector: 960-dim")

# Warmup (index load)
print("\n--- Warmup ---")
run(f"SET enable_vectorsearch = on; SET try_vector_engine_strategy = force; SELECT id FROM gist_ns.gist1m ORDER BY vec <-> '{qv_str}'::vector LIMIT 10;", "IVF warmup")

# IVF K=10
print("\n--- IVF K=10 (3 rounds) ---")
for i in range(3):
    ms = run(f"SET enable_vectorsearch = on; SET try_vector_engine_strategy = force; EXPLAIN (ANALYZE) SELECT id FROM gist_ns.gist1m ORDER BY vec <-> '{qv_str}'::vector LIMIT 10;", f"round {i+1}")
    print(f"  Wall time: {ms:.0f}ms")

# Full scan K=10
print("\n--- FullScan K=10 (2 rounds) ---")
for i in range(2):
    ms = run(f"SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off; EXPLAIN (ANALYZE) SELECT id FROM gist_ns.gist1m ORDER BY vec <-> '{qv_str}'::vector LIMIT 10;", f"round {i+1}")
    print(f"  Wall time: {ms:.0f}ms")

print("\nDone.")
