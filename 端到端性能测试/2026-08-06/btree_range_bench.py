#!/usr/bin/env python3
"""
BTree all-operator performance test (main branch, single-column).
Tests FullScan vs Btree for =, >, >=, <, <= on key tables.
Verifies each Btree query EXPLAIN shows index scan.
"""
import subprocess, time, sys

GSQL = "/home/xl/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql"
PORT = 37000
HOST = "/tmp"
DB = "postgres"
WARMUP = 3
RUNS = 5

# Tables to test (skip non-existent ones)
TABLES = [
    # (label, namespace, table, id_col, eq_val, range_lo, range_hi)
    ("SIFT",    "sift_ns",  "sift1m",           "id", 500000,    100000,  900000),
    ("GIST",    "gist_ns",  "gist1m",           "id", 500000,    100000,  900000),
    ("DEEP",    "deep_ns",  "deep1b",           "id", 500000000, 100000000, 900000000),
    ("Synth",   "synth_ns", '"synth2048_10m"',  "id", 5000000,   1000000,  9000000),
]

OPERATORS = [
    ("=",  lambda t,lo,hi,mid: f"SELECT id FROM {t} WHERE id = {mid}"),
    (">",  lambda t,lo,hi,mid: f"SELECT id FROM {t} WHERE id > {lo}"),
    (">=", lambda t,lo,hi,mid: f"SELECT id FROM {t} WHERE id >= {lo}"),
    ("<",  lambda t,lo,hi,mid: f"SELECT id FROM {t} WHERE id < {hi}"),
    ("<=", lambda t,lo,hi,mid: f"SELECT id FROM {t} WHERE id <= {hi}"),
]

def run(sql, timeout=120):
    try:
        r = subprocess.run([GSQL, "-d", DB, "-p", str(PORT), "-h", HOST, "-t", "-A", "-c", sql],
                          capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"

def drop_index(ns, tbl):
    idx = f"btree_{tbl.strip(chr(34))}"
    run(f"SELECT iceberg_catalog.drop_index('{ns}','{tbl.strip(chr(34))}','{idx}')")

def create_index(ns, tbl):
    tbl_clean = tbl.strip('"')
    run(f"SELECT iceberg_catalog.create_index('{ns}','{tbl_clean}','btree_{tbl_clean}','[\"id\"]'::jsonb,'btree','btree','{{\"key_column\":\"id\"}}'::jsonb, p_is_async=>false, p_num_workers=>1)")

def check_explain(sql):
    """Return True if plan shows bridge scalar index scan or hybrid task_group"""
    out, _ = run(f"EXPLAIN (COSTS OFF) {sql}")
    if out:
        return "scalar index" in out.lower() or "hybrid" in out.lower()
    return False

def time_query(sql, runs=RUNS, warmup=WARMUP):
    for _ in range(warmup):
        out, err = run(sql)
        if err and "TIMEOUT" not in err:
            return None, err, 0
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        out, err = run(sql)
        t1 = time.perf_counter()
        if err:
            return None, err, 0
        times.append((t1 - t0) * 1000)
        row_count = len(out.split('\n')) if out else 0
    return sum(times) / len(times), None, row_count

def test_table(label, ns, tbl, col, mid, lo, hi):
    results = []
    print(f"\n{'='*60}")
    print(f"  {label}: {ns}.{tbl}")
    print(f"{'='*60}")

    # Phase 1: FullScan (ensure no index)
    drop_index(ns, tbl)
    for op_name, sql_fn in OPERATORS:
        sql = sql_fn(f"{ns}.{tbl}", lo, hi, mid)
        avg, err, rows = time_query(sql)
        status = f"{avg:8.1f}ms ({rows} rows)" if avg else f"ERROR {err[:40]}"
        print(f"  FS  {op_name:>3}: {status}")
        results.append(("FS", op_name, avg, rows))

    # Phase 2: Btree
    print("  Creating index...")
    create_index(ns, tbl)
    # warmup
    warm_sql = f"SELECT id FROM {ns}.{tbl} WHERE id = {mid}"
    run(warm_sql)
    run(warm_sql)

    for op_name, sql_fn in OPERATORS:
        sql = sql_fn(f"{ns}.{tbl}", lo, hi, mid)
        using_index = check_explain(sql)
        avg, err, rows = time_query(sql)
        idx_tag = "[IDX]" if using_index else "[FS?]"
        if avg:
            fs_ms = [r[2] for r in results if r[0]=="FS" and r[1]==op_name][0]
            ratio = f"FS/BT={fs_ms/avg:.2f}x" if fs_ms and avg else ""
            print(f"  BT  {op_name:>3}: {avg:8.1f}ms ({rows} rows) {idx_tag} {ratio}")
        else:
            print(f"  BT  {op_name:>3}: ERROR {err[:40]}")
        results.append(("BT", op_name, avg, rows))

    drop_index(ns, tbl)

    # Summary
    print(f"\n  {label} summary:")
    for mode in ["FS","BT"]:
        parts = [f"{r[1]}={r[2]:.0f}ms" for r in results if r[0]==mode and r[2]]
        if parts:
            print(f"    {mode}: {', '.join(parts)}")
    return results

def main():
    print("BTree All-Operator Test (main branch, single-column, Eq+Gt+Ge+Lt+Le)")
    print(f"Warmup={WARMUP}, Runs={RUNS}")

    all_results = {}
    for label, ns, tbl, col, mid, lo, hi in TABLES:
        r = test_table(label, ns, tbl, col, mid, lo, hi)
        all_results[label] = r

    # Final table
    print("\n\n" + "="*100)
    print("FINAL: FullScan vs Btree (ms) — ALL OPERATORS")
    print("="*100)
    header = f"{'Table':<10}"
    for op, _ in OPERATORS:
        header += f" {'FS-'+op:<12} {'BT-'+op:<12}"
    print(header)
    print("-"*100)
    for label, _, _, _, _, _, _ in TABLES:
        row = f"{label:<10}"
        for op_name, _ in OPERATORS:
            fs = next((f"{r[2]:.0f}ms" for r in all_results.get(label,[]) if r[0]=="FS" and r[1]==op_name and r[2]), "N/A")
            bt = next((f"{r[2]:.0f}ms" for r in all_results.get(label,[]) if r[0]=="BT" and r[1]==op_name and r[2]), "N/A")
            row += f" {fs:<12} {bt:<12}"
        print(row)

if __name__ == "__main__":
    main()
