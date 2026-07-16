#!/usr/bin/env python3
"""
Convert SIFT1M .fvecs/.ivecs files into Parquet format for Iceberg.

Usage:
    python3 convert_sift.py --input ./ --warehouse ~/warehouse --namespace sift_ns --table sift1m
"""
import argparse
import os
import struct
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def read_fvecs(path):
    """Read .fvecs file (vector format: 4-byte dim + dim*4-byte floats)."""
    with open(path, "rb") as f:
        data = f.read()
    vecs = []
    offset = 0
    while offset < len(data):
        dim = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        vec = struct.unpack_from(f"<{dim}f", data, offset)
        offset += dim * 4
        vecs.append(vec)
    return np.array(vecs, dtype=np.float32)


def read_ivecs(path):
    """Read .ivecs file (same format as fvecs but int32)."""
    with open(path, "rb") as f:
        data = f.read()
    all_ids = []
    offset = 0
    while offset < len(data):
        dim = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        ids = struct.unpack_from(f"<{dim}i", data, offset)
        offset += dim * 4
        all_ids.append(ids)
    return np.array(all_ids, dtype=np.int32)


def main():
    parser = argparse.ArgumentParser(description="Convert SIFT1M to Parquet")
    parser.add_argument("--input", required=True, help="Directory containing sift_base.fvecs etc.")
    parser.add_argument("--warehouse", required=True, help="Iceberg warehouse path")
    parser.add_argument("--namespace", default="sift_ns")
    parser.add_argument("--table", default="sift1m")
    parser.add_argument("--rows-per-file", type=int, default=100000)
    args = parser.parse_args()

    input_dir = args.input

    # 1. Read base vectors
    base_path = os.path.join(input_dir, "sift_base.fvecs")
    print(f"Reading base vectors from {base_path} ...")
    base = read_fvecs(base_path)
    n, dim = base.shape
    print(f"  {n} vectors × {dim} dimensions")

    # 2. Write parquet files to warehouse
    parquet_dir = os.path.join(
        os.path.expanduser(args.warehouse),
        f"{args.namespace}",
        args.table,
        "data",
    )
    os.makedirs(parquet_dir, exist_ok=True)

    # Build Arrow schema: id (int64) + vec (list<float>)
    schema = pa.schema([
        pa.field("id", pa.int64(), nullable=False),
        pa.field("vec", pa.list_(pa.field("element", pa.float32(), nullable=False)), nullable=False),
    ])

    print(f"Writing {n} rows to {parquet_dir} ...")
    total_rows = args.rows_per_file
    for start in range(0, n, total_rows):
        end = min(start + total_rows, n)
        batch_size = end - start

        ids = list(range(start + 1, end + 1))  # 1-based IDs
        vecs = [base[i].tolist() for i in range(start, end)]

        table = pa.table({"id": ids, "vec": vecs}, schema=schema)
        out_path = os.path.join(parquet_dir, f"part-{start:07d}.parquet")
        pq.write_table(table, out_path)
        print(f"  {out_path} ({batch_size} rows)")

    # 3. Write query vectors (as a separate file for easy loading)
    query_path = os.path.join(input_dir, "sift_query.fvecs")
    if os.path.exists(query_path):
        print(f"\nReading query vectors from {query_path} ...")
        queries = read_fvecs(query_path)
        nq, _ = queries.shape
        print(f"  {nq} queries")

        query_out = os.path.join(os.path.expanduser(args.warehouse),
                                 "sift_queries.parquet")
        query_schema = pa.schema([
            pa.field("qid", pa.int32(), nullable=False),
            pa.field("vec", pa.list_(pa.field("element", pa.float32(), nullable=False)), nullable=False),
        ])
        query_ids = list(range(nq))
        query_vecs = [queries[i].tolist() for i in range(nq)]
        t = pa.table({"qid": query_ids, "vec": query_vecs}, schema=query_schema)
        pq.write_table(t, query_out)
        print(f"  Queries written to {query_out}")

    # 4. Read ground truth
    gt_path = os.path.join(input_dir, "sift_groundtruth.ivecs")
    if os.path.exists(gt_path):
        print(f"\nReading ground truth from {gt_path} ...")
        gt = read_ivecs(gt_path)
        ngt, k = gt.shape
        print(f"  {ngt} queries × top-{k}")

        gt_out = os.path.join(os.path.expanduser(args.warehouse),
                              "sift_groundtruth.parquet")
        gt_schema = pa.schema([
            pa.field("qid", pa.int32(), nullable=False),
            pa.field("neighbors", pa.list_(pa.int32()), nullable=False),
        ])
        gt_ids = list(range(ngt))
        gt_vecs = [gt[i].tolist() for i in range(ngt)]
        t = pa.table({"qid": gt_ids, "neighbors": gt_vecs}, schema=gt_schema)
        pq.write_table(t, gt_out)
        print(f"  Ground truth written to {gt_out}")

    print("\nDone.")


if __name__ == "__main__":
    main()
