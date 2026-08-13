#!/usr/bin/env python3
"""Multi-column btree performance test: FullScan vs Index Scan."""
import subprocess, time

GSQL = "/home/xl/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql"
TBL = 'multicol_ns.t_multi'
WARMUP = 3
RUNS = 5

def run(sql, timeout=120):
    t0 = time.perf_counter()
    r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-h", "/tmp", "-t", "-A", "-c", sql],
                      capture_output=True, text=True, timeout=timeout)
    ms = (time.perf_counter() - t0) * 1000
    rows = len(r.stdout.strip().split('\n')) if r.stdout.strip() else 0
    return ms, rows, r.stderr.strip()

def run_sql(sql, timeout=30):
    r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-h", "/tmp", "-c", sql],
                      capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip()

def bench(label, sql):
    for _ in range(WARMUP):
        run(sql)
    times = []
    for _ in range(RUNS):
        ms, rows, err = run(sql)
        if err and "ERROR" in err:
            return None, err[:60]
        times.append(ms)
    avg = sum(times) / len(times)
    return avg, rows

# Test queries — multi-column btree on (id, category, score)
QUERIES = [
    ("1col: id =",    f"SELECT id FROM {TBL} WHERE id = 50000"),
    ("1col: id >",    f"SELECT id FROM {TBL} WHERE id > 90000"),
    ("2col: id = AND cat =", f"SELECT id FROM {TBL} WHERE id = 50000 AND category = 'alpha'"),
    ("2col: id > AND cat =", f"SELECT id FROM {TBL} WHERE id > 80000 AND category = 'beta'"),
    ("3col: id > range AND cat =", f"SELECT id FROM {TBL} WHERE id > 45000 AND id < 55000 AND category = 'alpha'"),
    ("skip-lead: cat =", f"SELECT id FROM {TBL} WHERE category = 'alpha'"),
]

print(f"Multi-Column BTree Performance: {TBL}")
print(f"Warmup={WARMUP}, Runs={RUNS}\n")

# Phase 1: FullScan
run_sql(f"SELECT iceberg_catalog.drop_index('multicol_ns','t_multi','btree_multi')", 10)
print("=== FullScan ===")
for label, sql in QUERIES:
    avg, result = bench(label, sql)
    if avg:
        print(f"  FS {label:20s}: {avg:8.1f}ms ({result} rows)")
    else:
        print(f"  FS {label:20s}: ERROR {result}")

# Phase 2: Create multi-column index
print("\nCreating multi-column index on (id, category, score)...")
out, err = run_sql(
    f"SELECT iceberg_catalog.create_index('multicol_ns','t_multi','btree_multi','[\"id\",\"category\",\"score\"]'::jsonb,'btree','btree','{{\"key_columns\":[\"id\",\"category\",\"score\"]}}'::jsonb, p_is_async=>false, p_num_workers=>1)",
    60
)
print(f"  Index: {out[:100] if out else err[:100]}")

# Verify index
out, _ = run_sql(f"EXPLAIN SELECT id FROM {TBL} WHERE id = 50000 AND category = 'alpha'", 10)
has_idx = "scalar index" in out.lower()
print(f"  EXPLAIN shows index: {has_idx}")

print("\n=== Btree Index Scan ===")
for label, sql in QUERIES:
    avg, result = bench(label, sql)
    if avg:
        print(f"  BT {label:20s}: {avg:8.1f}ms ({result} rows)")
    else:
        print(f"  BT {label:20s}: ERROR {result}")

# Cleanup
run_sql(f"SELECT iceberg_catalog.drop_index('multicol_ns','t_multi','btree_multi')", 10)
print("\nDone.")
