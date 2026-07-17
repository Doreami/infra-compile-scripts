#!/usr/bin/env python3
"""
Convert GIST1M HDF5 (ANN-Benchmarks format) to Parquet for Iceberg.

Input:  gist-960-euclidean.hdf5
        - train:      1M × 960 float32   base vectors
        - test:       1000 × 960 float32  query vectors
        - neighbors:  1000 × 100 int32    ground truth (indices are 0-based)
        - distances:  1000 × 100 float32  ground truth L2 distances

Output: Same format as convert_sift.py — Parquet files with id + vec (list<float>),
        queries + ground truth as standalone Parquet.

Usage:
    python3 convert_gist_hdf5.py \
        --hdf5 ~/测试文件/gist-960-euclidean.hdf5 \
        --warehouse file://$HOME/warehouse \
        --namespace gist_ns \
        --table gist1m
"""
import argparse
import os
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def main():
    parser = argparse.ArgumentParser(description="Convert GIST1M HDF5 to Parquet")
    parser.add_argument("--hdf5", required=True, help="Path to gist-960-euclidean.hdf5")
    parser.add_argument("--warehouse", required=True, help="Iceberg warehouse path (e.g. file://$HOME/warehouse)")
    parser.add_argument("--namespace", default="gist_ns")
    parser.add_argument("--table", default="gist1m")
    parser.add_argument("--rows-per-file", type=int, default=100000)
    args = parser.parse_args()

    # Load HDF5 (deferred — only open datasets on access)
    try:
        import h5py
    except ImportError:
        print("ERROR: h5py is required. Install with: pip install h5py")
        return 1

    print(f"Opening {args.hdf5} ...")
    f = h5py.File(args.hdf5, "r")

    train = f["train"]
    test = f["test"]
    neighbors = f["neighbors"]

    n, dim = train.shape
    nq = test.shape[0]
    gt_k = neighbors.shape[1]
    print(f"  Base:  {n} vectors × {dim} dimensions")
    print(f"  Query: {nq} × {dim}")
    print(f"  GT:    {nq} × top-{gt_k} (indices are 0-based)")

    # Strip file:// prefix for os.path
    warehouse = args.warehouse
    if warehouse.startswith("file://"):
        warehouse = warehouse[7:]
    warehouse = os.path.expanduser(warehouse)

    # ── 1. Base vectors → Parquet data files ──
    parquet_dir = os.path.join(warehouse, args.namespace, args.table, "data")
    os.makedirs(parquet_dir, exist_ok=True)

    schema = pa.schema([
        pa.field("id", pa.int64(), nullable=False),
        pa.field("vec", pa.list_(pa.field("element", pa.float32(), nullable=False)), nullable=False),
    ])

    print(f"\nWriting base vectors to {parquet_dir} ...")
    total_rows = args.rows_per_file
    file_count = 0
    for start in range(0, n, total_rows):
        end = min(start + total_rows, n)
        batch_size = end - start

        ids = list(range(start + 1, end + 1))  # 1-based IDs
        vecs = [train[i].tolist() for i in range(start, end)]

        table = pa.table({"id": ids, "vec": vecs}, schema=schema)
        out_path = os.path.join(parquet_dir, f"part-{start:07d}.parquet")
        pq.write_table(table, out_path)
        file_count += 1
        if file_count % 5 == 0:
            print(f"  ... {end}/{n} rows ({file_count} files)")

    print(f"  Done: {file_count} files, {n} rows")

    # ── 2. Query vectors → standalone Parquet ──
    query_out = os.path.join(warehouse, "gist_queries.parquet")
    print(f"\nWriting query vectors to {query_out} ...")
    query_schema = pa.schema([
        pa.field("qid", pa.int32(), nullable=False),
        pa.field("vec", pa.list_(pa.field("element", pa.float32(), nullable=False)), nullable=False),
    ])
    query_vecs = [test[i].tolist() for i in range(nq)]
    t = pa.table({"qid": list(range(nq)), "vec": query_vecs}, schema=query_schema)
    pq.write_table(t, query_out)
    print(f"  {nq} queries")

    # ── 3. Ground truth → standalone Parquet ──
    gt_out = os.path.join(warehouse, "gist_groundtruth.parquet")
    print(f"\nWriting ground truth to {gt_out} ...")
    gt_schema = pa.schema([
        pa.field("qid", pa.int32(), nullable=False),
        pa.field("neighbors", pa.list_(pa.int32()), nullable=False),
    ])
    # HDF5 neighbors are 0-based; convert to 1-based to match table IDs
    gt_1based = [neighbors[i].astype(np.int32).tolist() for i in range(nq)]
    # Note: ground truth is 0-based; recall eval must add 1 (same as SIFT workflow)
    t = pa.table({"qid": list(range(nq)), "neighbors": gt_1based}, schema=gt_schema)
    pq.write_table(t, gt_out)
    print(f"  {nq} × top-{gt_k} (0-based indices, +1 to align with table IDs)")

    f.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
