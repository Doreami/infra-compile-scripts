#!/usr/bin/env python3
"""Create GIST1M table with configurable Parquet row_group_size.

Creates table via gaussdb (proper type mapping), writes data with PyArrow
(controlling row_group_size), registers via pyiceberg add_files().

Usage:
  python3 setup_gist_rg.py --row-group-size 5000
"""
import argparse, json, os, subprocess, sys, struct
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hdf5", default=os.path.expanduser("~/测试文件/gist-960-euclidean.hdf5"))
    p.add_argument("--namespace", default="gist_ns")
    p.add_argument("--table", default="gist1m_rg5k")
    p.add_argument("--row-group-size", type=int, default=5000)
    p.add_argument("--num-clusters", type=int, default=256)
    args = p.parse_args()

    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    warehouse = os.path.expanduser(
        os.environ.get("ICEBERG_WAREHOUSE", "file://$HOME/warehouse").replace("file://", ""))
    warehouse_uri = f"file://{warehouse}"

    # 1. Read HDF5
    import h5py
    f = h5py.File(args.hdf5, "r")
    train = f["train"]
    n, dim = train.shape
    fixed_len = dim * 4
    print(f"Data: {n} x {dim} → fixed({fixed_len})")

    # 2. Create table via gaussdb
    print("\n=== Create table ===")
    schema_json = json.dumps({
        "type": "struct", "schema-id": 0,
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

    gsql_run(f"SELECT iceberg_catalog.create_namespace('{args.namespace}');", fatal=False)
    gsql_run(f"SELECT iceberg_catalog.drop_table('{args.namespace}', '{args.table}');", fatal=False)
    r = gsql_run(f"DROP FOREIGN TABLE IF EXISTS {args.namespace}.{args.table};", fatal=False)

    wh_path = os.path.join(warehouse, args.namespace, args.table)
    if os.path.exists(wh_path):
        import shutil
        shutil.rmtree(wh_path)

    r = gsql_run(
        f"SELECT iceberg_catalog.create_table("
        f"'{args.namespace}', '{args.table}', '{schema_json}'::jsonb,"
        f"'{warehouse_uri}/{args.namespace}/{args.table}', NULL);", timeout=30)

    resp = json.loads(r.stdout.strip())
    md_path = resp.get("metadata-location") or resp.get("metadata_location", "")
    if not md_path:
        # Fallback: query tables_internal
        r2 = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                            f"SELECT metadata_location FROM iceberg_catalog.tables_internal "
                            f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
                           capture_output=True, text=True, timeout=15)
        md_path = r2.stdout.strip()
    print(f"  Created: {args.table}, metadata: {md_path}")

    # 3. Patch metadata for pyiceberg compat
    md_local = md_path.replace("file://", "")
    with open(md_local, "r") as fh:
        meta = json.load(fh)
    if meta.get("format-version") == 3:
        meta["format-version"] = 2
    meta.setdefault("properties", {})["write.parquet.compression-codec"] = "uncompressed"
    with open(md_local, "w") as fh:
        json.dump(meta, fh)
    print("  Format-version: 3→2")

    # 4. Write data with controlled row_group_size, then add_files via pyiceberg
    print(f"\n=== Write data (row_group_size={args.row_group_size}) ===")
    data_dir = os.path.join(warehouse, args.namespace, args.table, "data")
    os.makedirs(data_dir, exist_ok=True)

    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.io.pyarrow import schema_to_pyarrow

    tmp_db = os.path.join(os.path.expanduser("~"), f".pyiceberg_{args.namespace}.db")
    for ext in ["", "-wal", "-shm"]:
        db_file = tmp_db + ext
        if os.path.exists(db_file):
            os.remove(db_file)

    catalog = SqlCatalog("temp", uri=f"sqlite:///{tmp_db}", warehouse=warehouse_uri)
    catalog.create_namespace_if_not_exists(args.namespace)
    tbl = catalog.register_table(f"{args.namespace}.{args.table}", md_path)
    arrow_schema = schema_to_pyarrow(tbl.schema())

    chunk_size = 50000
    file_paths = []
    total = 0
    file_idx = 0
    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        vec_bytes = [train[i].tobytes() for i in range(start, end)]
        batch = pa.table(
            [pa.array(range(start + 1, end + 1), type=pa.int64()),
             pa.array(vec_bytes, type=pa.binary(fixed_len))],
            schema=arrow_schema)

        dst_name = f"00000-{file_idx}-rg{args.row_group_size}.parquet"
        dst_path = os.path.join(data_dir, dst_name)
        pq.write_table(batch, dst_path, row_group_size=args.row_group_size)
        file_paths.append(dst_path)
        total = end
        file_idx += 1
        if total % 200000 == 0:
            print(f"  {total}/{n} ...")

    # Verify row groups
    total_rg = sum(pq.read_metadata(p).num_row_groups for p in file_paths)
    print(f"  Done: {file_idx} files, {total_rg} row groups")

    # 5. Register files via pyiceberg add_files()
    print("\n=== Register files ===")
    tbl.add_files(file_paths)

    # 6. Update metadata_location in gaussdb
    new_md = tbl.metadata_location
    snapshots = tbl.snapshots()
    new_snapshot_id = snapshots[-1].snapshot_id if snapshots else "NULL"
    gsql_run(
        f"UPDATE iceberg_catalog.tables_internal "
        f"SET metadata_location = '{new_md}', "
        f"    current_snapshot_id = {new_snapshot_id} "
        f"WHERE namespace = '{args.namespace}' AND table_name = '{args.table}';")

    # Cleanup
    for p in [tmp_db + ext for ext in ["", "-wal", "-shm"]]:
        if os.path.exists(p):
            try: os.remove(p)
            except: pass

    # 7. Verify
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.namespace}.{args.table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print(f"\n=== Done! {file_idx} files, {total_rg} row groups ===")
    nc = args.num_clusters
    print(f"Next: SELECT iceberg_catalog.create_index('{args.namespace}', '{args.table}',"
          f" 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf',"
          f" '{{\"vector_column\":\"vec\",\"num_clusters\":{nc},\"sample_rate\":100000}}'::jsonb);")

if __name__ == "__main__":
    main()
