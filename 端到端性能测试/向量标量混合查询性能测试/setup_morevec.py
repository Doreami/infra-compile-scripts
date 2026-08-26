#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MoReVec 数据集导入 gaussdb（场景 A：真实过滤负载 A/B）。

MoReVec（ANN-Benchmark-HQ 论文数据集）movies/reviews 表：
- 向量：`train_mvector` / `train_rvector`（float64，768 维）
- 属性：`train_*` 其余键（float64 → double、bytes → string）
- 过滤定义与查询负载在 MoReVec_small/{filters,queries}/ 下（本脚本只导数据 + 建索引）

用法：
  python3 setup_morevec.py --input datasets/movies_dataset_0.hdf5 \
      --namespace more_ns --table movies
  python3 setup_morevec.py --input datasets/reviews_dataset_0.hdf5 \
      --namespace more_ns --table reviews --partition-buckets 32 --max-rows 200000
"""
import argparse
import json
import os
import struct
import subprocess
import sys
import time

import numpy as np


def read_morevec(path, max_rows=None):
    """读取 MoRe HDF5：返回 (向量 float32 (N,dim), 属性 dict[name->np.array], id_key)。"""
    import h5py
    f = h5py.File(path, "r")
    vec_key = "train_mvector" if "train_mvector" in f else "train_rvector"
    vec = np.asarray(f[vec_key], dtype=np.float32)
    n, dim = vec.shape
    if max_rows is not None and max_rows < n:
        vec = vec[:max_rows]
        n = max_rows
    attrs = {}
    for k in f.keys():
        if k.startswith("train_") and k != vec_key:
            a = f[k][:n]
            if a.dtype.kind in ("f", "i"):
                attrs[k] = np.asarray(a, dtype=np.float64)
            else:
                attrs[k] = np.asarray(a)  # bytes/str
    id_key = None
    for cand in ("train_mid", "train_rid"):
        if cand in f:
            id_key = cand
            break
    return vec, attrs, id_key


def attr_iceberg_type(a):
    """numpy 数组 → Iceberg schema JSON 类型。"""
    if a.dtype.kind in ("f", "i", "u"):
        return "double"
    return "string"


def main():
    p = argparse.ArgumentParser(description="MoReVec → gaussdb")
    p.add_argument("--input", required=True, help="MoRe HDF5 (movies/reviews)")
    p.add_argument("--namespace", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--partition-buckets", type=int, default=0)
    p.add_argument("--num-clusters", type=int, default=256)
    p.add_argument("--max-rows", type=int, default=None, help="截取行数（reviews 大表先截段）")
    p.add_argument("--gsql", default=None,
                   help="gsql 绝对路径（默认从 GAUSSHOME 找）")
    p.add_argument("--port", default="37000")
    args = p.parse_args()

    if args.gsql:
        gsql = args.gsql
    else:
        gauss_home = os.environ.get("GAUSSHOME")
        gsql = os.path.join(gauss_home, "bin", "gsql") if gauss_home else "gsql"
    if not os.path.exists(gsql):
        sys.exit(f"gsql not found: {gsql}")

    # ── 1. 读数据 ──
    print(f"=== Step 1: 读 MoRe HDF5 {args.input} ===")
    vec, attrs, id_key = read_morevec(args.input, args.max_rows)
    n, dim = vec.shape
    print(f"  {n} × {dim}（float32）; 属性: {list(attrs.keys())}; id 键: {id_key}")
    if n < 100:
        sys.exit("数据行数过少")

    def gsql_run(sql, timeout=120, fatal=True):
        r = subprocess.run([gsql, "-d", "postgres", "-p", args.port, "-t", "-A", "-c", sql],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            err = r.stderr.strip()
            if err and "already exists" not in err:
                print(f"  FAIL: {err}", file=sys.stderr)
                if fatal:
                    sys.exit(1)
        return r

    # ── 2. create_table ──
    print("\n=== Step 2: create_table ===")
    fields = [{"id": 1, "name": "id", "type": "long", "required": True}]
    fields.append({"id": 2, "name": "vec", "type": {
        "type": "list", "element": "float", "element-id": 100,
        "element-required": True}, "required": False, "vector_dim": dim})
    next_id = 3
    for name in attrs:
        # 去掉前导 train_
        col = name[len("train_"):] if name.startswith("train_") else name
        fields.append({"id": next_id, "name": col, "type": attr_iceberg_type(attrs[name]),
                       "required": False})
        next_id += 1
    schema_json = json.dumps({"type": "struct", "schema-id": 0, "fields": fields})

    gsql_run(f"SELECT iceberg_catalog.create_namespace('{args.namespace}');", fatal=False)
    r = gsql_run(f"SELECT iceberg_catalog.drop_table('{args.namespace}', '{args.table}');", fatal=False)
    if r.returncode != 0:
        subprocess.run([gsql, "-d", "postgres", "-p", args.port, "-c",
            f"DELETE FROM iceberg_catalog.tables_internal "
            f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';"],
            capture_output=True, timeout=15)
    gsql_run(f"DROP FOREIGN TABLE IF EXISTS {args.namespace}.{args.table};", fatal=False)
    warehouse = os.path.join(os.path.expanduser("~"), "iceberg-og", "warehouse") \
        if not os.path.isdir("/data/xl/warehouse") else "/data/xl/warehouse"
    table_loc = f"file://{warehouse}/{args.namespace}/{args.table}"

    part_spec_json = "NULL"
    if args.partition_buckets > 0:
        part_spec = {"spec-id": 0, "fields": [{
            "source-id": 1, "field-id": 1000, "name": "id_bucket",
            "transform": f"bucket[{args.partition_buckets}]"}]}
        part_spec_json = "'" + json.dumps(part_spec) + "'::jsonb"

    r = gsql_run(
        f"SELECT iceberg_catalog.create_table('{args.namespace}', '{args.table}', "
        f"'{schema_json}'::jsonb, '{table_loc}', {part_spec_json});", timeout=60)
    if not r.stdout.strip():
        sys.exit(f"create_table returned empty. stderr: {r.stderr.strip()}")

    # Patch metadata: 降级 format-version 3 → 2（pyiceberg 0.11 无法写 v3 manifest）。
    md = gsql_run(
        f"SELECT metadata_location FROM iceberg_catalog.tables_internal "
        f"WHERE namespace='{args.namespace}' AND table_name='{args.table}';")
    md_path = md.stdout.strip()
    md_local = md_path.replace("file://", "")
    if md_local.startswith("///"):
        md_local = md_local[2:]
    with open(md_local, "r") as fh:
        meta = json.load(fh)
    if meta.get("format-version") == 3:
        meta["format-version"] = 2
    with open(md_local, "w") as fh:
        json.dump(meta, fh)
    print(f"  metadata format-version 3→2: {md_local}")

    # ── 3. 写数据（pyiceberg）──
    print(f"\n=== Step 3: 写 {n} 行（partition_buckets={args.partition_buckets or 'none'}）===")
    import pyarrow as pa
    from pyiceberg.catalog.sql import SqlCatalog
    tmp_db = os.path.join(os.path.expanduser("~"), f".pyiceberg_{args.namespace}.db")
    for ext in ["", "-wal", "-shm"]:
        db_file = tmp_db + ext
        if os.path.exists(db_file):
            os.remove(db_file)
    catalog = SqlCatalog("temp", uri=f"sqlite:///{tmp_db}", warehouse=f"file://{warehouse}")
    catalog.create_namespace_if_not_exists(args.namespace)
    tbl = catalog.register_table(f"{args.namespace}.{args.table}", md_path)
    from pyiceberg.io.pyarrow import schema_to_pyarrow
    arrow_schema = schema_to_pyarrow(tbl.schema())

    # 小 chunk：每文件一个 row-group，过大 row-group 会让回表读（按行号取行）解压整组而极慢。
    # 5000 行/文件 ≈ 15MB row-group，随机取行只需解压 ~15MB。
    # 回表读性能关键：row-group 大小。chunk=5000 → 每文件 5000 行 / 1 个 ~16MB row-group，
    # 按行号读少量行只解压小 row-group（若大 chunk=50000 → 每文件 1 个 ~157MB 巨组，读几行
    # 也要解压全部，回表极慢）。已实测：5000/文件 vs 50000/文件，回表物化差 ~2×。
    chunk_size = 5000
    t0 = time.time()
    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        ids_arr = pa.array(range(start + 1, end + 1), type=pa.int64())
        flat = vec[start:end].ravel()
        offsets = np.arange(0, (end - start + 1) * dim, dim, dtype=np.int64)
        vec_arr = pa.ListArray.from_arrays(
            pa.array(offsets, type=pa.int32()), pa.array(flat, type=pa.float32()))
        cols = [ids_arr, vec_arr]
        for name in attrs:
            a = attrs[name][start:end]
            if a.dtype.kind in ("f", "i", "u"):
                cols.append(pa.array(a, type=pa.float64()))
            else:
                # bytes → utf8 字符串（去 b'' 前缀）
                cols.append(pa.array([x.decode() if isinstance(x, bytes) else str(x) for x in a],
                                     type=pa.string()))
        batch = pa.table(cols, schema=arrow_schema)
        tbl.append(batch)
        if end % (chunk_size * 5) == 0 or end == n:
            print(f"  {end:,}/{n:,} ({100*end/n:.0f}%) {time.time()-t0:.0f}s")
    print(f"  写入完成")

    # ── 4. 更新 metadata pointer + 建索引 ──
    new_md = tbl.metadata_location
    snaps = tbl.snapshots()
    snap_id = snaps[-1].snapshot_id if snaps else "NULL"
    gsql_run(
        f"UPDATE iceberg_catalog.tables_internal SET metadata_location = '{new_md}', "
        f"    current_snapshot_id = {snap_id} "
        f"WHERE namespace = '{args.namespace}' AND table_name = '{args.table}';")

    r = gsql_run(f"SELECT count(*) FROM {args.namespace}.{args.table};")
    print(f"\n  openGauss count: {r.stdout.strip()}")

    print(f"\n=== 建索引（IVFPQ num_clusters={args.num_clusters}）===")
    gsql_run(
        f"SELECT iceberg_catalog.create_index('{args.namespace}', '{args.table}',"
        f" 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf',"
        f" '{{\"vector_column\":\"vec\",\"num_clusters\":{args.num_clusters},\"sample_rate\":100000}}'::jsonb);",
        timeout=600)
    print("  索引创建完成（若异步需查 table_indexes 状态）")

    print(f"\n=== Done: {args.namespace}.{args.table}（{n} 行，{dim}d）===")
    print(f"过滤列可用: {[n[6:] for n in attrs]}")


if __name__ == "__main__":
    main()
