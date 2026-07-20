#!/usr/bin/env python3
"""Build GIST IVF-PQ index with timing."""
import subprocess, time, sys

SQL = """SELECT iceberg_catalog.create_index(
    'gist_ns_part', 'gist1m_part', 'idx_ivf_pq_vec',
    '["vec"]'::jsonb, 'ivf_pq', 'ivf',
    '{"vector_column":"vec","num_clusters":256,"sample_rate":100000}'::jsonb
);"""

print(f"[{time.strftime('%H:%M:%S')}] Starting GIST IVF-PQ index (nc=256)...")
print(f"SQL: {SQL[:80]}...")
sys.stdout.flush()

t0 = time.time()
try:
    r = subprocess.run(
        ["gsql", "-d", "postgres", "-p", "37000", "-c", SQL],
        capture_output=True, text=True, timeout=7200)
    elapsed = time.time() - t0
    print(r.stdout)
    if r.returncode != 0:
        print(f"STDERR: {r.stderr}")
    else:
        print(f"\n[{time.strftime('%H:%M:%S')}] INDEX BUILD DONE in {elapsed:.0f}s ({elapsed/60:.1f}min)")

    # Verify
    r2 = subprocess.run(
        ["gsql", "-d", "postgres", "-p", "37000", "-c",
         "SELECT index_name, index_type, index_status FROM iceberg_catalog.table_indexes;"],
        capture_output=True, text=True)
    print(r2.stdout)
except subprocess.TimeoutExpired:
    elapsed = time.time() - t0
    print(f"\n[{time.strftime('%H:%M:%S')}] TIMEOUT after {elapsed:.0f}s")
except Exception as e:
    print(f"\n[{time.strftime('%H:%M:%S')}] ERROR: {e}")
