#!/usr/bin/env python3
"""Build SIFT IVF-PQ index with timing."""
import subprocess, time

SQL = """SELECT iceberg_catalog.create_index(
    'sift_ns_part', 'sift1m_part', 'idx_ivf_pq_vec',
    '["vec"]'::jsonb, 'ivf_pq', 'ivf',
    '{"vector_column":"vec","num_clusters":256,"sample_rate":100000}'::jsonb
);"""

print(f"[{time.strftime('%H:%M:%S')}] Starting SIFT IVF-PQ index (nc=256)...")
t0 = time.time()
r = subprocess.run(["gsql", "-d", "postgres", "-p", "37000", "-c", SQL],
                   capture_output=True, text=True, timeout=7200)
elapsed = time.time() - t0
print(r.stdout)
if r.returncode != 0:
    print(f"STDERR: {r.stderr}")
else:
    print(f"[{time.strftime('%H:%M:%S')}] DONE in {elapsed:.0f}s ({elapsed/60:.1f}min)")
