#!/usr/bin/env python3
"""Rebuild GIST1M with row_group_size=5000 for row group pruning test.

Reads gist1m_none, re-writes with small row groups via PyArrow,
registers via pyiceberg add_files() to avoid re-writing.

Usage (on server):
  source ~/iceberg-og/opengauss.env
  python3 rebuild_gist_rg5k.py
"""
import argparse, os, subprocess
import pyarrow as pa
import pyarrow.parquet as pq
import shutil

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, FixedType, NestedField

WAREHOUSE_BASE = os.path.expanduser("/data/xl/warehouse")

def main():
    p = argparse.ArgumentParser(description="Rebuild GIST1M with configurable row group size")
    p.add_argument("--source-ns", default="gist_ns")
    p.add_argument("--source-table", default="gist1m_none")
    p.add_argument("--target-ns", default="gist_ns")
    p.add_argument("--target-table", default="gist1m_rg5k")
    p.add_argument("--row-group-size", type=int, default=5000,
                   help="Rows per Parquet row group (default 5000)")
    args = p.parse_args()

    SOURCE_DIR = os.path.join(WAREHOUSE_BASE, args.source_ns, args.source_table, "data")
    TARGET_DIR = os.path.join(WAREHOUSE_BASE, args.target_ns, args.target_table, "data")
    source_files = sorted([
        f for f in os.listdir(SOURCE_DIR) if f.endswith(".parquet")
    ])
    print(f"Source: {len(source_files)} files from {args.source_table}")

    # 1. Create target table
    warehouse_uri = f"file://{WAREHOUSE_BASE}"
    catalog_db = os.path.join(os.path.expanduser("~"), "gist_catalog.db")
    catalog = SqlCatalog("gist_reg", warehouse=warehouse_uri, uri=f"sqlite:///{catalog_db}")
    catalog.create_namespace_if_not_exists(args.target_ns)

    schema = Schema(
        NestedField(1, "id",  LongType(),  required=True),
        NestedField(2, "vec", FixedType(3840), required=False),
    )

    try:
        catalog.drop_table(f"{args.target_ns}.{args.target_table}")
        print(f"  Dropped existing {args.target_ns}.{args.target_table}")
    except Exception:
        pass

    tbl = catalog.create_table(f"{args.target_ns}.{args.target_table}", schema=schema)

    # Clean target dir
    if os.path.exists(TARGET_DIR):
        shutil.rmtree(TARGET_DIR)
    os.makedirs(TARGET_DIR, exist_ok=True)

    # 2. Re-encode files with row_group_size=5000
    file_paths = []
    total_rows = 0
    print(f"\nRe-encoding with row_group_size={args.row_group_size} ...")
    for i, fname in enumerate(source_files):
        src_path = os.path.join(SOURCE_DIR, fname)
        table = pq.read_table(src_path)

        dst_name = f"00000-{i}-rg{args.row_group_size}.parquet"
        dst_path = os.path.join(TARGET_DIR, dst_name)

        pq.write_table(table, dst_path, row_group_size=args.row_group_size)

        file_paths.append(dst_path)
        total_rows += table.num_rows
        if (i + 1) % 5 == 0:
            print(f"  {i+1}/{len(source_files)}")

    # 3. Register with Iceberg via add_files (bypasses pyiceberg write)
    print(f"\nRegistering {len(file_paths)} files with add_files() ...")
    tbl.add_files(file_paths)

    # 4. Verify row groups
    final_files = sorted([
        f for f in os.listdir(TARGET_DIR) if f.endswith(".parquet")
    ])
    total_rg, total_rows2 = 0, 0
    for f in final_files:
        meta = pq.read_metadata(os.path.join(TARGET_DIR, f))
        total_rg += meta.num_row_groups
        total_rows2 += meta.num_rows
    print(f"  {len(final_files)} files, {total_rows2} rows, {total_rg} row groups")
    print(f"  Avg: {total_rg/len(final_files):.1f} rg/file (was 1.2)")

    # 5. Verify
    snaps = tbl.snapshots()
    md = tbl.metadata_location
    print(f"  Snapshot: {snaps[-1].snapshot_id}")
    print(f"  Metadata: {md}")

    # 6. Register with openGauss
    print("\n=== Register with openGauss ===")
    gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
    if not os.path.exists(gsql):
        gsql = "gsql"

    def gsql_run(sql):
        r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c", sql],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and "already exists" not in r.stderr and "unregister" not in sql:
            print(f"  WARN: {r.stderr.strip()}")
        return r

    gsql_run(f"SELECT iceberg_catalog.unregister_table('{args.target_ns}', '{args.target_table}');")
    r = gsql_run(
        f"SELECT iceberg_catalog.register_table('{args.target_ns}', '{args.target_table}', '{md}');"
    )
    print(r.stdout.strip())

    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-t", "-A", "-c",
                        f"SELECT count(*) FROM {args.target_ns}.{args.target_table};"],
                       capture_output=True, text=True, timeout=30)
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print(f"\n=== Done! ===")
    print(f"Row groups: {total_rg} (was 25)")
    print(f"Next: build index, then A/B test row group pruning")

if __name__ == "__main__":
    main()
