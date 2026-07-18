#!/usr/bin/env python3
"""GIST1M: HDF5 → openGauss create_table → pyiceberg append

Usage (on server):
  source ~/iceberg-og/opengauss.env
  python3 setup_gist_fixed.py --hdf5 ~/测试文件/gist-960-euclidean.hdf5
"""
import argparse, os, sys, subprocess, json
import h5py
import pyarrow as pa

from pyiceberg.table import StaticTable
from pyiceberg.io import load_file_io
from pyiceberg.io.pyarrow import schema_to_pyarrow


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hdf5", required=True)
    p.add_argument("--namespace", default="gist_ns")
    p.add_argument("--table", default="gist1m")
    p.add_argument("--chunk-size", type=int, default=50000)
    args = p.parse_args()

    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    warehouse_env = os.environ.get("ICEBERG_WAREHOUSE", "file://$HOME/warehouse")
    if warehouse_env.startswith("file://"):
        warehouse_env = warehouse_env[7:]
    warehouse_env = os.path.expanduser(warehouse_env)

    # 1. Read HDF5
    print("=== Step 1: Read HDF5 ===")
    f = h5py.File(args.hdf5, "r")
    train = f["train"]
    n, dim = train.shape
    fixed_len = dim * 4
    print(f"  train: {n} × {dim} → fixed({fixed_len})")

    # 2. create_table via openGauss Catalog
    #    Schema JSON: vector_dim tells Catalog this fixed(L) is a vector
    print("\n=== Step 2: openGauss create_table ===")
    schema_json = json.dumps({
        "type": "struct",
        "schema-id": 0,
        "fields": [
            {"id": 1, "name": "id", "type": "long", "required": True},
            {"id": 2, "name": "vec", "type": f"fixed[{fixed_len}]",
             "required": False, "vector_dim": dim},
        ]
    })

    def gsql_run(sql, timeout=60, fatal=True):
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c", sql],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            err = r.stderr.strip()
            if err and "already exists" not in err:
                print(f"  FAIL: {err}", file=sys.stderr)
                if fatal:
                    sys.exit(1)
        return r

    # Ensure namespace + cleanup
    gsql_run(f"SELECT iceberg_catalog.create_namespace('{args.namespace}');", fatal=False)

    # Clean up: drop_table (normal), fallback to SQL DELETE (stale state)
    r = gsql_run(f"SELECT iceberg_catalog.drop_table('{args.namespace}', '{args.table}');",
                 fatal=False)
    if r.returncode != 0:
        subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c",
            f"DELETE FROM iceberg_catalog.tables_internal "
            f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
            capture_output=True, timeout=15)
    gsql_run(f"DROP FOREIGN TABLE IF EXISTS {args.namespace}.{args.table};", fatal=False)
    wh_path = os.path.join(warehouse_env, args.namespace, args.table)
    if os.path.exists(wh_path):
        import shutil
        shutil.rmtree(wh_path)

    table_loc = f"file://{warehouse_env}/{args.namespace}/{args.table}"
    r = gsql_run(
        f"SELECT iceberg_catalog.create_table("
        f"'{args.namespace}', '{args.table}', '{schema_json}'::jsonb,"
        f"'{table_loc}');",
        timeout=30)
    if not r.stdout.strip():
        sys.exit(f"create_table returned empty output. stderr: {r.stderr.strip()}")
    resp = json.loads(r.stdout.strip())
    md_path = resp.get("metadata_location", "")
    print(f"  Created: {args.namespace}.{args.table}")
    print(f"  metadata: {md_path}")

    # 3. Verify foreign table type
    print("\n=== Step 3: Verify column type ===")
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                        f"WHERE attrelid='{args.namespace}.{args.table}'::regclass "
                        f"AND attname='vec';"],
                       capture_output=True, text=True, timeout=30)
    col_type = r.stdout.strip()
    print(f"  vec: {col_type}")
    if col_type != f"vector({dim})":
        sys.exit(f"  FAIL: expected vector({dim}), got {col_type}")

    # 4. Append data via pyiceberg StaticTable
    print(f"\n=== Step 4: Append {n} rows ===")
    io = load_file_io({}, f"file://{warehouse_env}")
    tbl = StaticTable.from_metadata(
        os.path.join(warehouse_env, md_path), io)
    arrow_schema = schema_to_pyarrow(tbl.schema())

    total = 0
    for start in range(0, n, args.chunk_size):
        end = min(start + args.chunk_size, n)
        chunk_n = end - start
        vec_bytes = [train[i].tobytes() for i in range(start, end)]
        for i in range(chunk_n):
            assert len(vec_bytes[i]) == fixed_len

        batch = pa.table(
            [pa.array(range(start + 1, end + 1), type=pa.int64()),
             pa.array(vec_bytes, type=pa.binary(fixed_len))],
            schema=arrow_schema)
        tbl.append(batch)
        total = end
        if total % 200000 == 0:
            print(f"  {total}/{n} ...")
    print(f"  Done: {total} rows")

    # 5. Final verify
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.namespace}.{args.table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print("\n=== Done! ===")
    print(f"Next: SELECT iceberg_catalog.create_index(...)")


if __name__ == "__main__":
    main()
