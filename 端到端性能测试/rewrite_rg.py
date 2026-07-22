#!/usr/bin/env python3
"""In-place rewrite of GIST data files with configurable row_group_size.

Reads existing Parquet files, rewrites them with specified row_group_size.
Does NOT change Iceberg metadata — just replaces the file contents.

Usage:
  python3 rewrite_rg.py --table gist_ns.gist1m_rg5k --row-group-size 5000
"""
import argparse, os, pyarrow.parquet as pq

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--table-ns", default="gist_ns")
    p.add_argument("--table-name", default="gist1m_rg5k")
    p.add_argument("--row-group-size", type=int, default=5000)
    p.add_argument("--warehouse", default=os.path.expanduser("~/warehouse"))
    args = p.parse_args()

    data_dir = os.path.join(args.warehouse, args.table_ns, args.table_name, "data")
    files = sorted([f for f in os.listdir(data_dir) if f.endswith(".parquet")])

    print(f"Rewriting {len(files)} files with row_group_size={args.row_group_size} ...")
    total_rg = 0
    for i, fname in enumerate(files):
        path = os.path.join(data_dir, fname)
        table = pq.read_table(path)
        tmp = path + ".tmp"
        pq.write_table(table, tmp, row_group_size=args.row_group_size)
        os.replace(tmp, path)
        rg = pq.read_metadata(path).num_row_groups
        total_rg += rg
        if (i + 1) % 5 == 0:
            print(f"  {i+1}/{len(files)} (last: {rg} rg)")

    print(f"Done: {len(files)} files, {total_rg} row groups total")

if __name__ == "__main__":
    main()
