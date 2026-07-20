#!/usr/bin/env python3
"""Import vector datasets into openGauss via create_table + pyiceberg append.

Supports HDF5 and fvecs formats. Auto-detects from file extension.

Usage:
  python3 setup_fixed.py --input ~/测试文件/gist-960-euclidean.hdf5
  python3 setup_fixed.py --input ~/测试文件/sift_base.fvecs
"""
import argparse, os, sys, subprocess, json, struct
import numpy as np
import pyarrow as pa

from pyiceberg.table import StaticTable
from pyiceberg.io import load_file_io
from pyiceberg.io.pyarrow import schema_to_pyarrow


def read_hdf5(path):
    import h5py
    f = h5py.File(path, "r")
    train = f["train"]
    return np.array(train)


def read_fvecs(path):
    data = open(path, "rb").read()
    dim = struct.unpack("<i", data[:4])[0]
    vec_size = 4 + dim * 4
    n = len(data) // vec_size
    vecs = np.zeros((n, dim), dtype=np.float32)
    for i in range(n):
        off = i * vec_size
        vecs[i] = np.frombuffer(data[off + 4:off + vec_size], dtype=np.float32)
    return vecs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Data file (.hdf5 or .fvecs)")
    p.add_argument("--namespace", default=None)
    p.add_argument("--table", default=None)
    p.add_argument("--chunk-size", type=int, default=50000)
    p.add_argument("--partition-buckets", type=int, default=0,
                   help="Bucket partition count (0 = no partitioning). "
                        "Creates partitioned table for parallel query testing.")
    p.add_argument("--num-clusters", type=int, default=256,
                   help="IVFPQ num_clusters for index creation hint (default: 256)")
    args = p.parse_args()

    # Auto-detect format and partition suffix
    part_buckets = args.partition_buckets
    ns_suffix = "_part" if part_buckets > 0 else ""
    tbl_suffix = "_part" if part_buckets > 0 else ""

    fn = os.path.basename(args.input).lower()
    if fn.endswith(".hdf5") or fn.endswith(".h5"):
        fmt = "hdf5"
        dataset = "gist" if "gist" in fn else "dataset"
        if args.namespace is None: args.namespace = f"{dataset}_ns{ns_suffix}"
        if args.table is None: args.table = f"{dataset}1m{tbl_suffix}"
    elif fn.endswith(".fvecs"):
        fmt = "fvecs"
        dataset = "sift" if "sift" in fn else "dataset"
        if args.namespace is None: args.namespace = f"{dataset}_ns{ns_suffix}"
        if args.table is None: args.table = f"{dataset}1m{tbl_suffix}"
    else:
        sys.exit(f"Unknown format: {fn}. Expected .hdf5 or .fvecs")

    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    warehouse_env = os.environ.get("ICEBERG_WAREHOUSE", "file://$HOME/warehouse")
    if warehouse_env.startswith("file://"):
        warehouse_env = warehouse_env[7:]
    warehouse = os.path.expanduser(warehouse_env)

    # 1. Read data
    print(f"=== Step 1: Read {fmt} file ===")
    read_fn = {"hdf5": read_hdf5, "fvecs": read_fvecs}[fmt]
    base = read_fn(args.input)
    n, dim = base.shape
    fixed_len = dim * 4
    print(f"  {n} × {dim} → fixed({fixed_len})")

    # 2. create_table
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

    gsql_run(f"SELECT iceberg_catalog.create_namespace('{args.namespace}');", fatal=False)

    # Clean up
    r = gsql_run(f"SELECT iceberg_catalog.drop_table('{args.namespace}', '{args.table}');",
                 fatal=False)
    if r.returncode != 0:
        subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c",
            f"DELETE FROM iceberg_catalog.tables_internal "
            f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
            capture_output=True, timeout=15)
    gsql_run(f"DROP FOREIGN TABLE IF EXISTS {args.namespace}.{args.table};", fatal=False)
    wh_path = os.path.join(warehouse, args.namespace, args.table)
    if os.path.exists(wh_path):
        import shutil
        shutil.rmtree(wh_path)

    table_loc = f"file://{warehouse}/{args.namespace}/{args.table}"

    # Build partition spec if requested
    part_spec_json = "NULL"
    if part_buckets > 0:
        part_spec = {
            "spec-id": 0,
            "fields": [{
                "source-id": 1,
                "field-id": 1000,
                "name": "id_bucket",
                "transform": f"bucket[{part_buckets}]"
            }]
        }
        part_spec_json = "'" + json.dumps(part_spec) + "'::jsonb"

    r = gsql_run(
        f"SELECT iceberg_catalog.create_table("
        f"'{args.namespace}', '{args.table}', '{schema_json}'::jsonb,"
        f"'{table_loc}', {part_spec_json});",
        timeout=30)
    if not r.stdout.strip():
        sys.exit(f"create_table returned empty. stderr: {r.stderr.strip()}")
    resp = json.loads(r.stdout.strip())
    print(f"  Created: {args.namespace}.{args.table}")

    # 3. Verify type
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

    # 4. Get metadata path for StaticTable
    md_path = resp.get("metadata_location", "")
    if not md_path:
        # Fallback: query tables_internal
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                            f"SELECT metadata_location FROM iceberg_catalog.tables_internal "
                            f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
                           capture_output=True, text=True, timeout=15)
        md_path = r.stdout.strip()
    print(f"  metadata: {md_path}")

    # 5. Patch metadata: downgrade format-version 3 → 2 for pyiceberg compat
    print("\n=== Step 4: Patch metadata format-version ===")
    md_local = md_path.replace("file://", "")
    if md_local.startswith("///"):
        md_local = md_local[2:]
    with open(md_local, "r") as fh:
        meta = json.load(fh)
    if meta.get("format-version") == 3:
        meta["format-version"] = 2
        with open(md_local, "w") as fh:
            json.dump(meta, fh)
        print(f"  Downgraded format-version: 3 → 2")

    # 6. Append data — use pyiceberg SQL catalog (StaticTable is read-only)
    print(f"\n=== Step 5: Append {n} rows ===")
    from pyiceberg.catalog.sql import SqlCatalog
    tmp_db = os.path.join(os.path.expanduser("~"), f".pyiceberg_{args.namespace}.db")
    for ext in ["", "-wal", "-shm"]:
        db_file = tmp_db + ext
        if os.path.exists(db_file):
            os.remove(db_file)
    catalog = SqlCatalog("temp", uri=f"sqlite:///{tmp_db}", warehouse=f"file://{warehouse}")
    catalog.create_namespace_if_not_exists(args.namespace)
    tbl = catalog.register_table(f"{args.namespace}.{args.table}", md_path)
    arrow_schema = schema_to_pyarrow(tbl.schema())

    total = 0
    for start in range(0, n, args.chunk_size):
        end = min(start + args.chunk_size, n)
        vec_bytes = [base[i].tobytes() for i in range(start, end)]
        batch = pa.table(
            [pa.array(range(start + 1, end + 1), type=pa.int64()),
             pa.array(vec_bytes, type=pa.binary(fixed_len))],
            schema=arrow_schema)
        tbl.append(batch)
        total = end
        if total % 200000 == 0:
            print(f"  {total}/{n} ...")
    print(f"  Done: {total} rows")

    # 6. Update metadata_location directly — avoid re-register (Rust SDK strips
    #    vector_dim from metadata).  The foreign table keeps its vector(N) type.
    print("\n=== Step 6: Update metadata pointer ===")
    new_md = tbl.metadata_location
    snapshots = tbl.snapshots()
    new_snapshot_id = snapshots[-1].snapshot_id if snapshots else "NULL"
    gsql_run(
        f"UPDATE iceberg_catalog.tables_internal "
        f"SET metadata_location = '{new_md}', "
        f"    current_snapshot_id = {new_snapshot_id} "
        f"WHERE namespace = '{args.namespace}' AND table_name = '{args.table}';")
    # Clean up pyiceberg temp catalog
    import shutil
    for p in [tmp_db + ext for ext in ["", "-wal", "-shm"]]:
        if os.path.exists(p):
            try: os.remove(p)
            except: pass

    # 7. Verify
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.namespace}.{args.table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print("\n=== Done! ===")
    nc = args.num_clusters
    idx_sql = (
        f"SELECT iceberg_catalog.create_index('{args.namespace}', '{args.table}',"
        f" 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf',"
        f" '{{\"vector_column\":\"vec\",\"num_clusters\":{nc},\"sample_rate\":100000}}'::jsonb);")
    print(f"Next (IVFPQ, num_clusters={nc}): {idx_sql}")
    if part_buckets > 0:
        print(f"\nPartitioned table ({part_buckets} buckets). "
              f"Ready for parallel query testing:")
        print(f"  python3 bench_parallel.py --dataset {dataset} "
              f"--namespace {args.namespace} --table {args.table}")
    else:
        print(f"\nNon-partitioned table. Ready for serial testing:")
        print(f"  python3 bench_parallel.py --serial --dataset {dataset} "
              f"--namespace {args.namespace} --table {args.table}")


if __name__ == "__main__":
    main()
