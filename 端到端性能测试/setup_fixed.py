#!/usr/bin/env python3
"""Import vector datasets into openGauss via create_table + pyiceberg append.

Supports HDF5, fvecs, and fbin formats. Auto-detects from file extension.
Large fbin files stream via memmap (no full-RAM load).
Partitioned tables group rows by id_bucket before append (1 file/bucket/round).

Usage:
  # Small datasets (SIFT/GIST)
  python3 setup_fixed.py --input ./测试文件/gist-960-euclidean.hdf5
  python3 setup_fixed.py --input ./测试文件/sift_base.fvecs

  # Large fbin datasets
  python3 setup_fixed.py --input ~/big-ann-benchmarks/data/deep1b/base.1B.fbin \
      --namespace deep_ns --table deep1b --chunk-size 1000000

  # Partitioned table (parallel query testing)
  python3 setup_fixed.py --input ~/big-ann-benchmarks/data/deep1b/base.1B.fbin \
      --namespace deep_ns --table deep1b --chunk-size 1000000 \
      --partition-buckets 32
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


def read_fbin(path, max_rows=None):
    """Read .fbin file: [nvecs: int32][dim: int32][float32 × nvecs × dim]

    Returns (array, n, dim).  For large files returns a memmap (no copy);
    callers must slice chunks without loading the entire file.
    Cropped files (e.g. deep-100M from deep-1B) have header nvecs > actual data.
    Use max_rows to override nvecs when the file is a partial download.
    """
    with open(path, "rb") as f:
        header_nvecs = struct.unpack("<i", f.read(4))[0]
        dim = struct.unpack("<i", f.read(4))[0]
    n = max_rows if max_rows else header_nvecs
    arr = np.memmap(path, dtype=np.float32, mode='r', offset=8)
    expected = n * dim
    actual = len(arr)
    if actual < expected:
        n = actual // dim
    # Return memmap view (no copy) — caller is responsible for chunking
    return arr[:expected].reshape(n, dim), n, dim


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, help="Data file (.hdf5, .fvecs, .fbin)")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows to N (for cropped .fbin where header > actual data)")
    p.add_argument("--namespace", default=None)
    p.add_argument("--table", default=None)
    p.add_argument("--chunk-size", type=int, default=50000)
    p.add_argument("--partition-buckets", type=int, default=0,
                   help="Bucket partition count (0 = no partitioning). "
                        "Creates partitioned table for parallel query testing.")
    p.add_argument("--num-clusters", type=int, default=256,
                   help="IVFPQ num_clusters for index creation hint (default: 256)")
    p.add_argument("--vec-type", default="list", choices=["list", "fixed"],
                   help="Iceberg column type: list<float> or fixed[N] (default: list). "
                        "Use 'fixed' for high-dim data (>~1000d) where gaussdb maps "
                        "list<float> to text instead of vector(N).")
    p.add_argument("--compression", default="uncompressed",
                   choices=["zstd", "lz4", "snappy", "gzip", "uncompressed"],
                   help="Parquet compression codec (default: uncompressed). "
                        "No decompression CPU overhead; recommended for vector workloads.")
    args = p.parse_args()

    # Auto-detect format。分区表默认加 _part 后缀避免覆盖非分区表，手动指定 --namespace/--table 可覆盖。
    part_buckets = args.partition_buckets
    vec_type = args.vec_type
    ns_suffix = "_part" if part_buckets > 0 and args.namespace is None else ""
    tbl_suffix = "_part" if part_buckets > 0 and args.table is None else ""

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
    elif ".fbin" in fn:
        fmt = "fbin"
        dataset = "deep" if "deep" in fn else "dataset"
        if args.namespace is None: args.namespace = f"{dataset}_ns{ns_suffix}"
        if args.table is None: args.table = f"{dataset}1m{tbl_suffix}"
    else:
        sys.exit(f"Unknown format: {fn}. Expected .hdf5, .fvecs, or .fbin")

    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    warehouse_env = os.environ.get("ICEBERG_WAREHOUSE", "file:///data/xl/warehouse")
    if warehouse_env.startswith("file://"):
        warehouse_env = warehouse_env[7:]
    warehouse = os.path.expanduser(warehouse_env)

    # 1. Read data
    print(f"=== Step 1: Read {fmt} file ===")
    if fmt == "fbin":
        base, n, dim = read_fbin(args.input, args.max_rows)
    else:
        read_fn = {"hdf5": read_hdf5, "fvecs": read_fvecs}[fmt]
        base = read_fn(args.input)
        n, dim = base.shape
    fixed_len = dim * 4
    print(f"  {n} × {dim} → fixed({fixed_len})")

    # 2. create_table
    print("\n=== Step 2: openGauss create_table ===")
    if vec_type == "fixed":
        vec_schema = {"id": 2, "name": "vec", "type": f"fixed[{fixed_len}]",
                       "required": False, "vector_dim": dim}
    else:
        vec_schema = {"id": 2, "name": "vec", "type": {
            "type": "list", "element": "float",
            "element-id": 100, "element-required": True
        }, "required": False, "vector_dim": dim}
    schema_json = json.dumps({
        "type": "struct",
        "schema-id": 0,
        "fields": [
            {"id": 1, "name": "id", "type": "long", "required": True},
            vec_schema,
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
                        f"SELECT format_type(a.atttypid, a.atttypmod) "
                        f"FROM pg_attribute a JOIN pg_class c ON a.attrelid=c.oid "
                        f"JOIN pg_namespace n ON c.relnamespace=n.oid "
                        f"WHERE n.nspname='{args.namespace}' AND c.relname='{args.table}' "
                        f"AND a.attname='vec';"],
                       capture_output=True, text=True, timeout=30)
    col_type = r.stdout.strip()
    print(f"  vec: {col_type}")
    need_alter = (col_type != f"vector({dim})")
    if need_alter:
        print(f"  ALTER needed: {col_type or 'empty'} → vector({dim})")
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c",
                        f"ALTER FOREIGN TABLE {args.namespace}.{args.table} "
                        f"ALTER COLUMN vec TYPE vector({dim});"],
                       capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"  ALTER failed: {r.stderr.strip()}")
        else:
            print(f"  ALTER OK")

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
    # Patch metadata: add write properties for compression, then downgrade format-version
    with open(md_local, "r") as fh:
        meta = json.load(fh)
    if meta.get("format-version") == 3:
        meta["format-version"] = 2
    # Inject Parquet compression codec into table properties.
    # pyiceberg's FileIO reads this from table metadata at write time.
    # We patch metadata directly instead of calling update_properties()
    # for compatibility with older pyiceberg versions.
    meta.setdefault("properties", {})["write.parquet.compression-codec"] = args.compression
    with open(md_local, "w") as fh:
        json.dump(meta, fh)
    print(f"  Format: 3→2, compression={args.compression}")

    # 6. Append data — use pyiceberg SQL catalog.  For fbin files we stream
    #    via memmap to avoid loading the full dataset into memory.
    print(f"\n=== Step 5: Append {n} rows (chunk_size={args.chunk_size}, "
          f"partitions={part_buckets or 'none'}) ===")
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
    # When partitioned, process in larger rounds so each partition gets
    # ~chunk_size rows (≈ 1 file per partition per round).
    outer_step = args.chunk_size * max(part_buckets, 1)
    total_rounds = (n + outer_step - 1) // outer_step
    print(f"  DEBUG: n={n}, chunk_size={args.chunk_size}, parts={part_buckets}, "
          f"outer_step={outer_step}, rounds={total_rounds}")
    t0 = time.time() if "time" in dir() else None

    for round_start in range(0, n, outer_step):
        round_end = min(round_start + outer_step, n)
        round_n = round_end - round_start

        chunk = base[round_start:round_end]
        ids_arr = pa.array(range(round_start + 1, round_end + 1), type=pa.int64())
        if vec_type == "fixed":
            data_buf = pa.py_buffer(chunk.tobytes())
            vec_arr = pa.FixedSizeBinaryArray.from_buffers(
                pa.binary(fixed_len), round_n, [None, data_buf])
        else:
            flat = chunk.ravel()
            offsets = np.arange(0, (round_n + 1) * dim, dim, dtype=np.int64)
            total_len = offsets[-1]
            if total_len < 2_147_483_648:  # fits int32
                vec_arr = pa.ListArray.from_arrays(
                    pa.array(offsets, type=pa.int32()), pa.array(flat, type=pa.float32()))
            else:
                vec_arr = pa.LargeListArray.from_arrays(
                    pa.array(offsets), pa.array(flat, type=pa.float32()))
        batch = pa.table([ids_arr, vec_arr], schema=arrow_schema)
        tbl.append(batch)

        total = round_end
        if total % (args.chunk_size * 10) == 0 or total == n:
            print(f"  {total:,}/{n:,} ({100*total/n:.0f}%)")
    print(f"  Done: {total:,} rows")

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

    print(f"\n=== Done! (compression={args.compression}) ===")
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
