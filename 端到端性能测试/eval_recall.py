#!/usr/bin/env python3
"""Recall@K 评测 — 独立脚本，按需使用，不耦合性能测试流程。

支持全扫（100% recall）和 IVF 近似搜索的 recall 对比，可一次扫描多个 nprobe 值。

用法:
  python3 eval_recall.py --dataset sift --namespace sift_ns --table sift1m --nprobes 1,4,8,16,32
  python3 eval_recall.py --dataset gist --namespace gist_ns --table gist1m --nq 50 --k 100
"""
import argparse, os, struct, subprocess
import numpy as np

GSQL = os.path.expanduser("~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql")
DATA_DIR = os.path.expanduser("~/测试文件")
BIGANN_DIR = os.path.expanduser("~/big-ann-benchmarks/data")
if not os.path.exists(GSQL):
    GSQL = "gsql"

DATASETS = {
    "sift": {
        "dim": 128,
        "gt_file": "sift_groundtruth.ivecs", "gt_fmt": "ivecs",
        "query_file": "sift_query.fvecs", "query_fmt": "fvecs",
    },
    "gist": {
        "dim": 960,
        "gt_file": "gist-960-euclidean.hdf5", "gt_fmt": "hdf5", "gt_key": "neighbors",
        "query_file": "gist-960-euclidean.hdf5", "query_fmt": "hdf5", "query_key": "test",
    },
    "deep": {
        "dim": 96,
        "gt_file": "deep-100M", "gt_fmt": "ibin",
        "query_file": "query.public.10K.fbin", "query_fmt": "fbin",
    },
}


# ── File readers ──

def _read_fvecs(path, n):
    with open(path, "rb") as f:
        data = f.read()
    vecs, off = [], 0
    for _ in range(n):
        dim = struct.unpack_from("<i", data, off)[0]
        off += 4
        vecs.append(list(struct.unpack_from(f"<{dim}f", data, off)))
        off += dim * 4
    return vecs


def _read_ivecs(path, n, k):
    with open(path, "rb") as f:
        data = f.read()
    rows, off = [], 0
    for _ in range(n):
        dim = struct.unpack_from("<i", data, off)[0]
        off += 4
        rows.append(list(struct.unpack_from(f"<{dim}i", data, off)))
        off += dim * 4
    # SIFT ground truth is 0-based; table IDs are 1-based → +1
    return np.array([r[:k] for r in rows], dtype=np.int32) + 1


def _read_ibin(path, n, k):
    """Read .ibin ground truth: [nvecs: int32][dim: int32][int32 × nvecs × dim].
    0-based indices, convert to 1-based for table IDs."""
    with open(path, "rb") as f:
        nvecs = struct.unpack("<i", f.read(4))[0]
        dim = struct.unpack("<i", f.read(4))[0]
        data = f.read()
    rows = []
    off = 0
    for _ in range(min(n, nvecs)):
        row = list(struct.unpack_from(f"<{dim}i", data, off))
        off += dim * 4
        rows.append(row)
    return np.array([r[:k] for r in rows], dtype=np.int32) + 1


def _read_fbin(path, n):
    """Read .fbin query file: [nvecs: int32][dim: int32][float32 × nvecs × dim]."""
    with open(path, "rb") as f:
        nvecs = struct.unpack("<i", f.read(4))[0]
        dim = struct.unpack("<i", f.read(4))[0]
        data = f.read()
    vecs = []
    off = 0
    row_bytes = dim * 4
    for _ in range(min(n, nvecs)):
        vecs.append(list(struct.unpack_from(f"<{dim}f", data, off)))
        off += row_bytes
    return vecs


# ── Main ──

def main():
    p = argparse.ArgumentParser(description="Recall@K 评测")
    p.add_argument("--dataset", required=True, choices=list(DATASETS))
    p.add_argument("--namespace", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--nq", type=int, default=100)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--nprobes", default="0,1,4,8,16,32",
                   help="逗号分隔，0=全扫(100% recall)")
    args = p.parse_args()

    cfg = DATASETS[args.dataset]
    ns, tbl = args.namespace, args.table
    table = f"{ns}.{tbl}"

    # Load ground truth
    gt_path = (os.path.join(DATA_DIR, cfg["gt_file"]) if cfg["gt_file"] else None)
    if gt_path and not os.path.exists(gt_path):
        # fallback: big-ann-benchmarks data dir
        ds_name = args.dataset + "1b"  # e.g. deep1b
        alt = os.path.join(BIGANN_DIR, ds_name, cfg["gt_file"])
        if os.path.exists(alt):
            gt_path = alt
    if cfg["gt_fmt"] == "ivecs":
        gt = _read_ivecs(gt_path, args.nq, args.k)
    elif cfg["gt_fmt"] == "ibin":
        gt = _read_ibin(gt_path, args.nq, args.k)
    elif cfg["gt_fmt"] == "hdf5":
        import h5py
        with h5py.File(gt_path, "r") as f:
            gt = np.array(f[cfg["gt_key"]][:args.nq, :args.k], dtype=np.int32) + 1
    else:
        sys.exit(f"Unknown gt_fmt: {cfg['gt_fmt']}")

    # Load queries
    q_path = os.path.join(DATA_DIR, cfg["query_file"]) if cfg["query_file"] else None
    if q_path and not os.path.exists(q_path):
        ds_name = args.dataset + "1b"
        alt = os.path.join(BIGANN_DIR, ds_name, cfg["query_file"])
        if os.path.exists(alt):
            q_path = alt
    if cfg["query_fmt"] == "fvecs":
        queries = _read_fvecs(q_path, args.nq)
    elif cfg["query_fmt"] == "fbin":
        queries = _read_fbin(q_path, args.nq)
    elif cfg["query_fmt"] == "hdf5":
        import h5py
        with h5py.File(q_path, "r") as f:
            queries = f[cfg["query_key"]][:args.nq].tolist()
    else:
        sys.exit(f"Unknown query_fmt: {cfg['query_fmt']}")

    print(f"=== Recall@{args.k} — {args.dataset.upper()} (前 {args.nq} 条 query) ===\n")

    def _gsql(sql, timeout=600):
        r = subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-t", "-A", "-c", sql],
                           capture_output=True, text=True, timeout=timeout)
        return [int(x.strip()) for x in r.stdout.strip().split("\n") if x.strip().lstrip("-").isdigit()]

    def _set_nprobe(n):
        subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c",
                        f"ALTER FOREIGN TABLE {table} OPTIONS (DROP nprobe);"],
                       capture_output=True, timeout=10)
        if n > 0:
            subprocess.run([GSQL, "-d", "postgres", "-p", "37000", "-c",
                            f"ALTER FOREIGN TABLE {table} OPTIONS (ADD nprobe '{n}');"],
                           capture_output=True, timeout=10)

    for nprobe_str in args.nprobes.split(","):
        nprobe = int(nprobe_str.strip())

        _set_nprobe(nprobe)

        if nprobe == 0:
            setup = "SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;"
            label = "全扫"
        else:
            setup = "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;"
            label = f"IVF nprobe={nprobe}"

        hits = 0
        for i, vec in enumerate(queries):
            v = "[" + ",".join(str(x) for x in vec) + "]"
            ids = _gsql(f"{setup} SELECT id FROM {table} ORDER BY vec <-> '{v}'::vector LIMIT {args.k};")
            hits += len(set(ids[:args.k]) & set(gt[i][:args.k].tolist()))

        recall = hits / (args.nq * args.k)
        print(f"  {label:20s}  Recall@{args.k} = {hits}/{args.nq * args.k} = {recall:.1%}")

    _set_nprobe(0)  # restore default
    print("\nDone.")


if __name__ == "__main__":
    main()
