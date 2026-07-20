#!/usr/bin/env python3
"""GIST1M: HDF5 → Iceberg table → openGauss register in one shot, no duplicates.

Usage (on server):
  source ~/iceberg-og/opengauss.env
  python3 setup_gist.py --hdf5 ~/测试文件/gist-960-euclidean.hdf5
"""
import argparse, os, sys, subprocess
import h5py
import pyarrow as pa
import pyarrow.parquet as pq

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, ListType, FloatType, NestedField


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hdf5", required=True)
    p.add_argument("--namespace", default="gist_ns")
    p.add_argument("--table", default="gist1m")
    p.add_argument("--chunk-size", type=int, default=50000, help="rows per write batch")
    args = p.parse_args()

    warehouse = os.path.expanduser(os.environ.get("ICEBERG_WAREHOUSE", "file://$HOME/warehouse"))
    if warehouse.startswith("file://"):
        warehouse = warehouse[7:]

    # 1. Read HDF5
    print("=== Step 1: Read HDF5 ===")
    f = h5py.File(args.hdf5, "r")
    train = f["train"]
    n, dim = train.shape
    print(f"  train: {n} × {dim}")

    # 2. Create Iceberg table via pyiceberg
    print("\n=== Step 2: Create Iceberg table ===")
    catalog_db = os.path.join(os.path.expanduser("~"), "gist_catalog.db")
    catalog = SqlCatalog("gist_reg", warehouse=f"file://{warehouse}",
                         uri=f"sqlite:///{catalog_db}")
    catalog.create_namespace_if_not_exists(args.namespace)

    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "vec", ListType(element_id=3, element_type=FloatType(),
                                       element_required=True), required=False),
    )
    tbl = catalog.create_table_if_not_exists(f"{args.namespace}.{args.table}", schema=schema)

    # 3. Append in chunks (batch write to avoid OOM)
    print(f"\n=== Step 3: Append {n} rows in chunks of {args.chunk_size} ===")
    total = 0
    for start in range(0, n, args.chunk_size):
        end = min(start + args.chunk_size, n)
        ids = list(range(start + 1, end + 1))  # 1-based
        vecs = [train[i].tolist() for i in range(start, end)]

        batch = pa.table({
            "id": pa.array(ids, type=pa.int64()),
            "vec": pa.array(vecs, type=pa.list_(pa.field("element", pa.float32(), nullable=False))),
        }, schema=pa.schema([
            pa.field("id", pa.int64(), nullable=False),
            pa.field("vec", pa.list_(pa.field("element", pa.float32(), nullable=False)), nullable=False),
        ]))
        tbl.append(batch)
        total = end
        if total % 200000 == 0:
            print(f"  {total}/{n} ...")

    print(f"  Done: {total} rows appended")

    # 4. Verify
    snaps = tbl.snapshots()
    print(f"  Snapshot: {snaps[-1].snapshot_id}")
    md = tbl.metadata_location
    print(f"  Metadata: {md}")

    # 5. Register with openGauss
    print("\n=== Step 4: Register with openGauss ===")
    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    def gsql_run(sql):
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c", sql],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 and "already exists" not in r.stderr and "unregister" not in sql:
            print(f"  WARN: {r.stderr.strip()}")
        return r

    # Unregister if exists
    gsql_run(f"SELECT iceberg_catalog.unregister_table('{args.namespace}', '{args.table}');")

    # Register
    r = gsql_run(
        f"SELECT iceberg_catalog.register_table("
        f"'{args.namespace}', '{args.table}', '{md}');"
    )
    print(r.stdout.strip())

    # Verify
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.namespace}.{args.table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print("\n=== Done! ===")
    print(f"Next: SELECT iceberg_catalog.create_index('{args.namespace}', '{args.table}',"
          f" 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf',"
          f" '{{\"vector_column\":\"vec\",\"num_clusters\":1024,\"sample_rate\":100000}}'::jsonb);")

if __name__ == "__main__":
    main()
