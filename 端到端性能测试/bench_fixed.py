"""Quick EXPLAIN ANALYZE for fixed-type SIFT and GIST."""
import subprocess, struct, os, numpy as np

gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
if not os.path.exists(gsql):
    gsql = "gsql"

def run(sql):
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c", sql],
                       capture_output=True, text=True, timeout=120)
    return r.stdout, r.stderr

# SIFT vector (first row)
base = open(os.path.expanduser("~/测试文件/sift_base.fvecs"), "rb").read()
dim = struct.unpack("<i", base[:4])[0]
vec = np.frombuffer(base[4:4+dim*4], dtype=np.float32)
vec_str = "[" + ",".join(str(x) for x in vec) + "]"

print(f"SIFT IVF K=10 (warmup)...")
sql = f"""SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;
EXPLAIN ANALYZE SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '{vec_str}'::vector LIMIT 10;"""
run(sql)  # warmup

print(f"\nSIFT IVF K=10:")
out, err = run(sql)
for line in out.split("\n"):
    if "Total runtime" in line or "Vector Search" in line:
        print(f"  {line.strip()}")
print(f"  (warm)")

print(f"\nSIFT FullScan K=10 (warmup)...")
sql = f"""SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;
EXPLAIN ANALYZE SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '{vec_str}'::vector LIMIT 10;"""
run(sql)

print(f"\nSIFT FullScan K=10:")
out, err = run(sql)
for line in out.split("\n"):
    if "Total runtime" in line:
        print(f"  {line.strip()}")
print(f"  (warm)")

print("\n=== Done ===")
