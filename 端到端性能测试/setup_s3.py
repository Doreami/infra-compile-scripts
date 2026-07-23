#!/usr/bin/env python3
"""Import vector datasets into openGauss via S3/MinIO storage.

Same logic as setup_fixed.py but writes data to S3 instead of local FS.
Requires boto3 and pyiceberg with PyArrowFileIO.

Usage:
  python3 setup_s3.py --input ~/测试文件/sift_base.fvecs
  python3 setup_s3.py --input ~/测试文件/gist-960-euclidean.hdf5
"""
import argparse, os, sys, subprocess, json, struct, boto3
import numpy as np
import pyarrow as pa
from pyiceberg.io.pyarrow import schema_to_pyarrow

# ── S3 config ──
S3_ENDPOINT = "http://172.168.22.25:19000"
S3_ACCESS_KEY = "xlperf"
S3_SECRET_KEY = "PerfTest123!"
S3_BUCKET = "xlperf-bucket"
S3_REGION = "us-east-1"


def s3_client():
    from botocore.config import Config
    return boto3.client("s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(signature_version="s3v4"),
        use_ssl=False, region_name=S3_REGION)


def read_hdf5(path):
    import h5py
    f = h5py.File(path, "r")
    return np.array(f["train"])


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


def s3_download(key):
    """Download a file from S3 and return bytes."""
    return s3_client().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def s3_upload(key, data):
    """Upload bytes to S3."""
    s3_client().put_object(Bucket=S3_BUCKET, Key=key, Body=data)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Data file (.hdf5 or .fvecs)")
    p.add_argument("--namespace", default=None)
    p.add_argument("--table", default=None)
    p.add_argument("--chunk-size", type=int, default=50000)
    p.add_argument("--num-clusters", type=int, default=256,
                   help="IVFPQ num_clusters for index creation hint (default: 256)")
    p.add_argument("--compression", default="uncompressed",
                   choices=["zstd", "lz4", "snappy", "gzip", "uncompressed"],
                   help="Parquet compression codec (default: uncompressed)")
    args = p.parse_args()

    # Auto-detect
    fn = os.path.basename(args.input).lower()
    if fn.endswith(".hdf5") or fn.endswith(".h5"):
        fmt = "hdf5"
        dataset = "gist" if "gist" in fn else "dataset"
        if args.namespace is None: args.namespace = f"{dataset}_ns"
        if args.table is None: args.table = f"{dataset}1m"
    elif fn.endswith(".fvecs"):
        fmt = "fvecs"
        dataset = "sift" if "sift" in fn else "dataset"
        if args.namespace is None: args.namespace = f"{dataset}_ns"
        if args.table is None: args.table = f"{dataset}1m"
    else:
        sys.exit(f"Unknown format: {fn}")

    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    # ── S3 warehouse path ──
    warehouse_prefix = f"s3://{S3_BUCKET}/warehouse"
    s3_prefix = f"warehouse/{args.namespace}/{args.table}"

    # ── 1. Read data ──
    print(f"=== Step 1: Read {fmt} file ===")
    read_fn = {"hdf5": read_hdf5, "fvecs": read_fvecs}[fmt]
    base = read_fn(args.input)
    n, dim = base.shape
    fixed_len = dim * 4
    print(f"  {n} × {dim} → fixed({fixed_len})")

    # ── 2. create_table via catalog ──
    print("\n=== Step 2: openGauss create_table (S3) ===")
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

    table_loc = f"{warehouse_prefix}/{args.namespace}/{args.table}"
    r = gsql_run(
        f"SELECT iceberg_catalog.create_table("
        f"'{args.namespace}', '{args.table}', '{schema_json}'::jsonb,"
        f"'{table_loc}');",
        timeout=30)
    if not r.stdout.strip():
        sys.exit(f"create_table returned empty. stderr: {r.stderr.strip()}")
    resp = json.loads(r.stdout.strip())
    md_path = resp.get("metadata-location", "")
    if not md_path:
        # fallback: query tables_internal
        r2 = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                             f"SELECT metadata_location FROM iceberg_catalog.tables_internal "
                             f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
                            capture_output=True, text=True, timeout=15)
        md_path = r2.stdout.strip()

    # ── 3. Verify column type ──
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

    # ── 4. Patch metadata: format-version 3→2 for pyiceberg compat ──
    print("\n=== Step 4: Patch metadata format-version (S3) ===")
    md_key = md_path.replace(f"{warehouse_prefix}/", "warehouse/")
    meta = json.loads(s3_download(md_key))
    if meta.get("format-version") == 3:
        meta["format-version"] = 2
    meta.setdefault("properties", {})["write.parquet.compression-codec"] = args.compression
    # Write to new metadata version
    new_md_key = md_key.replace("00000-", "00001-")
    s3_upload(new_md_key, json.dumps(meta))
    new_md_path = f"{warehouse_prefix}/{new_md_key[len('warehouse/'):]}"
    print(f"  Format: 3→2, compression={args.compression}")
    print(f"  New metadata: {new_md_path}")

    # ── 5. Append data via pyiceberg ──
    print(f"\n=== Step 5: Append {n} rows (S3) ===")
    from pyiceberg.catalog.sql import SqlCatalog
    tmp_db = os.path.join(os.path.expanduser("~"), f".pyiceberg_{args.namespace}.db")
    for ext in ["", "-wal", "-shm"]:
        db_file = tmp_db + ext
        if os.path.exists(db_file):
            os.remove(db_file)

    s3_props = {
        "s3.endpoint": S3_ENDPOINT,
        "s3.access-key-id": S3_ACCESS_KEY,
        "s3.secret-access-key": S3_SECRET_KEY,
        "s3.path-style-access": "true",
        "s3.region": S3_REGION,
        "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
    }
    catalog = SqlCatalog("temp", uri=f"sqlite:///{tmp_db}",
                         warehouse=warehouse_prefix, **s3_props)
    catalog.create_namespace_if_not_exists(args.namespace)
    tbl = catalog.register_table(f"{args.namespace}.{args.table}", new_md_path)
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

    # ── 6. Update metadata pointer in catalog ──
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
    for p in [tmp_db + ext for ext in ["", "-wal", "-shm"]]:
        if os.path.exists(p):
            try: os.remove(p)
            except: pass

    # ── 7. Verify ──
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.namespace}.{args.table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print(f"\n=== Done! (compression={args.compression}) ===")
    nc = args.num_clusters
    idx_sql = (
        f"SELECT iceberg_catalog.create_index('{args.namespace}', '{args.table}',"
        f" 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf',"
        f" '{{\"vector_column\":\"vec\",\"num_clusters\":{nc},\"sample_rate\":100000}}'::jsonb);")
    print(f"Next (IVFPQ, num_clusters={nc}): {idx_sql}")


if __name__ == "__main__":
    main()
