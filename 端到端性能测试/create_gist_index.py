#!/usr/bin/env python3
"""Create GIST IVF-PQ index."""
import subprocess, sys

sql = '''SELECT iceberg_catalog.create_index(
    'gist_ns_part', 'gist1m_part', 'idx_ivf_pq_vec',
    '["vec"]'::jsonb, 'ivf_pq', 'ivf',
    '{"vector_column":"vec","num_clusters":64,"sample_rate":100000}'::jsonb
);'''
print(f"Running: {sql}")
r = subprocess.run(['gsql', '-d', 'postgres', '-p', '37000', '-c', sql],
                   capture_output=True, text=True, timeout=600)
print(r.stdout)
if r.returncode != 0:
    print(f"STDERR: {r.stderr}", file=sys.stderr)
    sys.exit(r.returncode)
print("INDEX CREATED")
