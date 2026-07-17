import subprocess, struct, os
import numpy as np
import pyarrow.parquet as pq

GSQL = "/home/xl/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql"
NQ = 10

with open(os.path.expanduser("~/端到端性能测试/sift_query.fvecs"), "rb") as f:
    data = f.read()
queries = []
offset = 0
for _ in range(NQ):
    dim = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    vec = struct.unpack_from(f"<{dim}f", data, offset)
    offset += dim * 4
    queries.append(list(vec))

gt = pq.read_table(os.path.expanduser("~/warehouse/sift_groundtruth.parquet"))
gt_arr = np.array([row.as_py() for row in gt["neighbors"]], dtype=np.int32)[:NQ] + 1

print(f"=== 全扫 vs IVF Recall@10 (前{NQ}条) ===")
for label, extra_sql in [("全扫", "SET enable_vectorsearch = off;"), ("IVF", "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;")]:
    hits = 0
    for i, vec in enumerate(queries):
        vec_str = "[" + ",".join(str(v) for v in vec) + "]"
        sql = f"{extra_sql} SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '{vec_str}'::vector LIMIT 10;"
        p = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-t", "-A", "-c", sql],
                           capture_output=True, text=True, timeout=30)
        ids = [int(x.strip()) for x in p.stdout.strip().split("\n") if x.strip().isdigit()]
        hit = len(set(ids[:10]) & set(gt_arr[i][:10].tolist()))
        hits += hit
    recall = hits / (NQ * 10)
    print(f"{label} Recall@10: {hits}/{NQ*10} = {recall:.1%}")

print("Done.")
