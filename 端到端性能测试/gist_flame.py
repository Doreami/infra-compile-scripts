#!/usr/bin/env python3
"""GIST1M flame graph collection."""
import h5py, subprocess, os, time

GSQL = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql")
OUT = os.path.expanduser("~/infra-compile-scripts/端到端性能测试/2026-07-17/flamegraphs")
PERF_DIR = os.path.join(OUT, "perf_data")
os.makedirs(PERF_DIR, exist_ok=True)

STACK = os.path.expanduser("~/FlameGraph/stackcollapse-perf.pl")
FLAME = os.path.expanduser("~/FlameGraph/flamegraph.pl")

f = h5py.File(os.path.expanduser("~/测试文件/gist-960-euclidean.hdf5"), "r")
QV = "[" + ",".join(str(v) for v in f["test"][0].tolist()) + "]"
f.close()

def collect(label, setup_sql, rounds):
    pid = subprocess.run(["pgrep", "-f", "gaussdb.*37000"], capture_output=True, text=True).stdout.strip()
    print(f"[{label}] PID={pid}")

    query = f"SELECT id FROM gist_ns.gist1m ORDER BY vec <-> '{QV}'::vector LIMIT 10;"
    full_sql = f"{setup_sql} {query}"

    # Warmup
    print(f"  Warmup...")
    subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", full_sql],
                   capture_output=True, timeout=120)

    # Perf record
    pf = os.path.join(PERF_DIR, f"perf_gist_{label}.data")
    p = subprocess.Popen(["perf", "record", "-F", "99", "-g", "-p", pid, "-o", pf, "--", "sleep", "999"])
    time.sleep(1.5)

    for i in range(rounds):
        print(f"  Round {i+1}/{rounds}...")
        subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c", full_sql],
                       capture_output=True, timeout=120)

    time.sleep(0.5)
    p.terminate()
    p.wait()

    # Generate SVG
    svg = os.path.join(OUT, f"flame_gist_{label}.svg")
    cmd = f"perf script -i {pf} | {STACK} | {FLAME} --title 'GIST1M {label}' --width 1200 --colors hot > {svg}"
    subprocess.run(cmd, shell=True, capture_output=True)
    sz = os.path.getsize(svg)
    print(f"  SVG: {sz} bytes")

    # Top hotspots
    r = subprocess.run(["perf", "report", "-i", pf, "--stdio", "--percent-limit", "0.5", "--no-children"],
                       capture_output=True, text=True)
    for line in r.stdout.split("\n")[:15]:
        if line.strip() and not line.startswith("#"):
            print(f"  {line.strip()[:120]}")

# Main
print("=== GIST IVF K=10 ===")
collect("ivf_k10", "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;", 2)

print("\n=== GIST FullScan K=10 ===")
collect("fullscan_k10", "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;", 1)

print("\nDone!")
