#!/usr/bin/env python3
"""
CSV → openGauss Iceberg 通用数据管理工具
=========================================
支持 create / insert / drop / update / delete 五种操作。

用法:
  # 建表
  python3 import_csv.py --op create --csv data.csv -n myns -t mytbl \
      -c "id:long,name:string,vec:vector(128)" -w /data/xl/warehouse --gsql "gsql -d postgres"

  # 追加
  python3 import_csv.py --op insert --csv more.csv -n myns -t mytbl

  # 更新（按 key 列匹配，CSV 中有则覆盖，无则插入）
  python3 import_csv.py --op update --csv changes.csv -n myns -t mytbl --key id

  # 删除（CSV 中 key 列的值就是要删的行）
  python3 import_csv.py --op delete --csv del_keys.csv -n myns -t mytbl --key id

  # 删表
  python3 import_csv.py --op drop -n myns -t mytbl --gsql "gsql -d postgres"

列类型:
  long|bigint|int64 → bigint     double|float64 → double precision
  int|integer|int32  → integer    float|float32  → real
  string|text        → text       bool|boolean   → boolean
  vector(N)          → vector(N)  (CSV 中写 "[1.0,2.0,...]")
"""

import argparse, csv, os, re, shutil, subprocess, sys, tempfile, uuid
from typing import Optional, List

import pyarrow as pa, pyarrow.parquet as pq
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import (
    LongType, IntegerType, DoubleType, FloatType,
    StringType, BooleanType, ListType, NestedField,
)
from pyiceberg.partitioning import PartitionSpec

# ── 类型映射 ──────────────────────────────────────────────────
TYPE_MAP = {
    "long":    ("bigint",           LongType(),   pa.int64()),
    "bigint":  ("bigint",           LongType(),   pa.int64()),
    "int64":   ("bigint",           LongType(),   pa.int64()),
    "int":     ("integer",          IntegerType(), pa.int32()),
    "integer": ("integer",          IntegerType(), pa.int32()),
    "int32":   ("integer",          IntegerType(), pa.int32()),
    "double":  ("double precision", DoubleType(),  pa.float64()),
    "float64": ("double precision", DoubleType(),  pa.float64()),
    "float":   ("real",             FloatType(),   pa.float32()),
    "float32": ("real",             FloatType(),   pa.float32()),
    "string":  ("text",             StringType(),  pa.string()),
    "text":    ("text",             StringType(),  pa.string()),
    "bool":    ("boolean",          BooleanType(), pa.bool_()),
    "boolean": ("boolean",          BooleanType(), pa.bool_()),
}
VECTOR_RE = re.compile(r"^vector\((\d+)\)$", re.IGNORECASE)

# ── 工具函数 ──────────────────────────────────────────────────

def warn(msg):  print(f"[WARN] {msg}", file=sys.stderr)
def info(msg):  print(f"[info] {msg}")


def parse_columns(col_spec: str) -> List[dict]:
    """解析 'name:type,...' 字符串为列定义列表。"""
    cols = []
    for part in col_spec.split(","):
        part = part.strip()
        if ":" not in part:
            raise ValueError(f"列定义格式应为 name:type，收到: {part}")
        name, ctype = part.split(":", 1)
        name, ctype = name.strip(), ctype.strip()

        vm = VECTOR_RE.match(ctype)
        if vm:
            dim = int(vm.group(1))
            cols.append({
                "name": name, "og_type": f"vector({dim})",
                "ice_type": ListType(element_id=0, element_type=FloatType(), element_required=True),
                "pa_type": pa.list_(pa.field("element", pa.float32(), nullable=False)),
                "is_vector": True, "vector_dim": dim,
            })
            continue

        t = TYPE_MAP.get(ctype.lower())
        if t is None:
            raise ValueError(f"不支持的类型: {ctype}（支持: {', '.join(TYPE_MAP.keys())}, vector(N)）")
        og, ice, pa_t = t
        cols.append({"name": name, "og_type": og, "ice_type": ice, "pa_type": pa_t, "is_vector": False})
    return cols


def parse_vector(s: str, dim: int) -> list:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):   s = s[1:-1]
    elif s.startswith("{") and s.endswith("}"): s = s[1:-1]
    vals = [float(x.strip()) for x in s.split(",") if x.strip()]
    if len(vals) != dim:
        raise ValueError(f"向量维度不匹配: 期望 {dim}，实际 {len(vals)}")
    return vals


def csv_to_arrow(csv_path: str, col_defs: List[dict]) -> pa.Table:
    """读 CSV 并转换为 PyArrow Table。"""
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            parsed = {}
            for col in col_defs:
                name = col["name"]
                raw = row.get(name, "").strip()
                if raw == "" or raw.upper() == "NULL":
                    parsed[name] = None
                elif col["is_vector"]:
                    parsed[name] = parse_vector(raw, col["vector_dim"])
                elif isinstance(col["ice_type"], (LongType, IntegerType)):
                    parsed[name] = int(raw)
                elif isinstance(col["ice_type"], (DoubleType, FloatType)):
                    parsed[name] = float(raw)
                elif isinstance(col["ice_type"], BooleanType):
                    parsed[name] = raw.lower() in ("true", "1", "yes", "t")
                else:
                    parsed[name] = raw
            rows.append(parsed)

    arrays = {}
    for col in col_defs:
        name = col["name"]
        vals = [r.get(name) for r in rows]
        if col["is_vector"]:
            arrays[name] = pa.array(vals, type=pa.list_(pa.field("element", pa.float32(), nullable=False)))
        elif isinstance(col["ice_type"], BooleanType):
            arrays[name] = pa.array(vals, type=pa.bool_())
        else:
            arrays[name] = pa.array(vals)
    return pa.table(arrays)


def build_iceberg_schema(col_defs: List[dict]) -> Schema:
    """从列定义构建 Iceberg Schema。"""
    fields, eid = [], len(col_defs) + 1
    for i, col in enumerate(col_defs):
        if col["is_vector"]:
            eid += 1
            t = ListType(element_id=eid, element_type=FloatType(), element_required=True)
        else:
            t = col["ice_type"]
        fields.append(NestedField(i + 1, col["name"], t, required=False))
    return Schema(*fields)


def run_gsql(gsql_cmd: str, sql: str):
    """通过 gsql 执行 SQL。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(sql)
        tmp = f.name
    try:
        r = subprocess.run(f"{gsql_cmd} -f {tmp}", shell=True, capture_output=True, text=True, timeout=30)
        if r.stdout:
            print(r.stdout)
        if r.stderr:
            print(f"[gsql ERROR]\n{r.stderr}")
        if r.returncode != 0:
            raise RuntimeError(f"gsql 执行失败 (exit={r.returncode})")
    finally:
        os.unlink(tmp)


# ── 表结构读取 ──────────────────────────────────────────────────

def load_col_defs_from_og(gsql_cmd: str, ns: str, tbl: str) -> List[dict]:
    """从 openGauss catalog 读取已有表的列定义。"""
    sql = (
        f"SELECT field_name, field_type FROM iceberg_catalog.table_schemas "
        f"WHERE table_uuid = (SELECT table_uuid FROM iceberg_catalog.tables_internal "
        f"WHERE namespace='{ns}' AND table_name='{tbl}') "
        f"ORDER BY field_position"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(sql); tmp = f.name
    try:
        r = subprocess.run(f"{gsql_cmd} -t -A -f {tmp}", shell=True, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"读取表结构失败: {r.stderr}")
        cols = []
        for line in r.stdout.strip().split("\n"):
            if "|" not in line: continue
            name, ctype = line.split("|", 1)
            cols.append({"name": name.strip(), "og_type": ctype.strip()})
        if not cols:
            raise RuntimeError(f"表 {ns}.{tbl} 在 catalog 中没有列信息")
        # 补全其他字段
        result = []
        for c in cols:
            result.append(_fill_col_def(c["name"], c["og_type"]))
        return result
    finally:
        os.unlink(tmp)


def _fill_col_def(name: str, og_type: str) -> dict:
    vm = VECTOR_RE.match(og_type)
    if vm:
        dim = int(vm.group(1))
        return {"name": name, "og_type": og_type,
                "ice_type": ListType(element_id=0, element_type=FloatType(), element_required=True),
                "pa_type": pa.list_(pa.field("element", pa.float32(), nullable=False)),
                "is_vector": True, "vector_dim": dim}
    for key, (og, ice, pa_t) in TYPE_MAP.items():
        if og == og_type:
            return {"name": name, "og_type": og, "ice_type": ice, "pa_type": pa_t, "is_vector": False}
    # fallback: treat as string
    return {"name": name, "og_type": "text", "ice_type": StringType(), "pa_type": pa.string(), "is_vector": False}


def read_existing_parquet(tbl_path: str, col_defs: List[dict]) -> pa.Table:
    """读取 Iceberg 表中所有 Parquet 数据，统一转换为目标 schema。"""
    data_dir = f"{tbl_path}/data"
    files = sorted([f for f in os.listdir(data_dir) if f.endswith(".parquet")])
    if not files:
        # 返回空表
        arrays = {c["name"]: pa.array([], type=c["pa_type"]) for c in col_defs}
        return pa.table(arrays)

    target_schema = pa.schema([(c["name"], c["pa_type"]) for c in col_defs])
    tables = []
    for f in files:
        t = pq.read_table(os.path.join(data_dir, f))
        # 对齐列：重命名或填充缺失列
        aligned = {}
        for col in col_defs:
            n = col["name"]
            if n in t.column_names:
                aligned[n] = t.column(n).cast(col["pa_type"])
            else:
                aligned[n] = pa.array([None] * len(t), type=col["pa_type"])
        tables.append(pa.table(aligned))
    return pa.concat_tables(tables)


# ── 各操作 SQL 生成 ────────────────────────────────────────────

def sql_create(ns: str, tbl: str, col_defs: List[dict], t_uuid: str,
               meta_loc: str, warehouse: str, snap_id: int) -> str:
    """创建 foreign table + catalog 注册。"""
    w = os.path.expanduser(warehouse)
    og_cols = ", ".join("{} {}".format(c["name"], c["og_type"]) for c in col_defs)
    field_vals = ",\n       ".join(
        "('{}'::uuid, 0, {}, {}, '{}', {}, '{}')".format(
            t_uuid, i, i+1, c["name"],
            "true" if not isinstance(c["ice_type"], StringType) else "false",
            c["og_type"])
        for i, c in enumerate(col_defs)
    )
    return f"""CREATE SCHEMA IF NOT EXISTS {ns};
DROP FOREIGN TABLE IF EXISTS {ns}.{tbl};
-- 清理旧 catalog 记录（OID 可能已变）
DELETE FROM iceberg_catalog.table_schemas WHERE table_uuid IN (SELECT table_uuid FROM iceberg_catalog.tables_internal WHERE namespace='{ns}' AND table_name='{tbl}');
DELETE FROM iceberg_catalog.table_indexes WHERE namespace='{ns}' AND table_name='{tbl}';
DELETE FROM iceberg_catalog.tables_internal WHERE namespace='{ns}' AND table_name='{tbl}';
CREATE FOREIGN TABLE {ns}.{tbl} ({og_cols})
SERVER iceberg_catalog_server OPTIONS (namespace '{ns}', table_name '{tbl}');

INSERT INTO iceberg_catalog.tables_internal(
  relid,namespace,table_name,table_uuid,metadata_location,
  previous_metadata_location,table_location,last_column_id,
  current_schema_id,current_snapshot_id,default_spec_id)
VALUES ('{ns}.{tbl}'::regclass,'{ns}','{tbl}','{t_uuid}'::uuid,
  '{meta_loc}',NULL,'file://{w}/{ns}/{tbl}',{len(col_defs)},0,{snap_id},0);

INSERT INTO iceberg_catalog.table_schemas(
  table_uuid,schema_id,field_position,field_id,field_name,field_required,field_type)
VALUES {field_vals};"""


def sql_insert(ns: str, tbl: str, snap_id: int, meta_loc: str) -> str:
    """追加数据后更新 snapshot。"""
    return (
        f"UPDATE iceberg_catalog.tables_internal "
        f"SET current_snapshot_id = {snap_id}, metadata_location = '{meta_loc}'\n"
        f"WHERE namespace = '{ns}' AND table_name = '{tbl}';"
    )


def sql_create_or_update(ns: str, tbl: str, col_defs: List[dict], t_uuid: str,
                         meta_loc: str, warehouse: str, snap_id: int) -> str:
    """如果表不存在则 create，存在则 update snapshot（用于 update/delete 后的全量替换）。"""
    return f"""DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM iceberg_catalog.tables_internal WHERE namespace='{ns}' AND table_name='{tbl}') THEN
    {sql_insert(ns, tbl, snap_id, meta_loc)}
  ELSE
    {sql_create(ns, tbl, col_defs, t_uuid, meta_loc, warehouse, snap_id)}
  END IF;
END $$;"""


def sql_drop(ns: str, tbl: str) -> str:
    """删除 foreign table + catalog 记录。"""
    return f"""DROP FOREIGN TABLE IF EXISTS {ns}.{tbl};
DELETE FROM iceberg_catalog.table_schemas WHERE table_uuid = (
  SELECT table_uuid FROM iceberg_catalog.tables_internal WHERE namespace='{ns}' AND table_name='{tbl}'
);
DELETE FROM iceberg_catalog.table_indexes WHERE namespace='{ns}' AND table_name='{tbl}';
DELETE FROM iceberg_catalog.tables_internal WHERE namespace='{ns}' AND table_name='{tbl}';
DELETE FROM iceberg_catalog.snapshots WHERE table_uuid NOT IN (SELECT table_uuid FROM iceberg_catalog.tables_internal);"""


# ── 主流程 ─────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="openGauss Iceberg 通用数据管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--op", default="insert",
                   choices=["create", "insert", "drop", "update", "delete"],
                   help="操作: create(重建) insert(追加) drop(删表) update(更新) delete(删除) (默认 insert)")
    p.add_argument("--csv", help="CSV 文件路径 (create/insert/update/delete 需要)")
    p.add_argument("-n", "--namespace", required=True)
    p.add_argument("-t", "--table", required=True)
    p.add_argument("-c", "--columns", help="列定义: name:type,... (仅 create 需要；其他操作从 catalog 自动读取)")
    p.add_argument("--key", help="update/delete 的匹配列名")
    p.add_argument("-w", "--warehouse", default="/data/xl/warehouse")
    p.add_argument("--gsql", help="gsql 命令，如 'gsql -d postgres -p 37000'")
    p.add_argument("--no-execute", action="store_true", help="只打印 SQL")
    args = p.parse_args()

    w = os.path.expanduser(args.warehouse)
    ns, tbl = args.namespace, args.table
    ident = f"{ns}.{tbl}"
    tbl_dir = f"{w}/{ns}/{tbl}"
    db_uri = f"sqlite:///{w}/import_catalog.db"

    # ── Iceberg catalog ──
    cat = load_catalog("import_cat", **{"type": "sql", "uri": db_uri, "warehouse": f"file://{w}"})
    try: cat.create_namespace(ns)
    except: pass

    ice_exists = False
    try:
        ice_tbl = cat.load_table(ident)
        ice_exists = True
    except:
        # load_table 可能因物理文件缺失而失败，但 sqlite 里可能还有记录
        try:
            import sqlite3
            sc = sqlite3.connect(f"{w}/import_catalog.db")
            row = sc.execute("SELECT 1 FROM iceberg_tables WHERE catalog_name='import_cat' AND table_namespace=? AND table_name=?", (ns, tbl)).fetchone()
            sc.close()
            ice_exists = row is not None
        except:
            pass

    # ── 列定义 ──
    if args.columns:
        col_defs = parse_columns(args.columns)
    elif args.op != "drop":
        # 非 create 操作：从 openGauss catalog 读取已有列定义
        if not args.gsql:
            p.error("非 create/drop 操作需要 --gsql 来读取已有表结构")
        try:
            col_defs = load_col_defs_from_og(args.gsql, ns, tbl)
        except RuntimeError as e:
            p.error(str(e))
    else:
        col_defs = []

    if col_defs:
        info("列: " + ", ".join("{}:{}".format(c["name"], c["og_type"]) for c in col_defs))

    # ── 读 CSV ──
    if args.op in ("create", "insert", "update", "delete") and args.csv:
        info(f"读取 {args.csv} ...")
        new_data = csv_to_arrow(args.csv, col_defs)
        info(f"  {len(new_data)} 行")
    else:
        new_data = None

    # ═══════════════════════════════════════════════════════
    # 各操作
    # ═══════════════════════════════════════════════════════

    if args.op == "create":
        # 删除旧表（物理 + catalog）
        if os.path.isdir(tbl_dir):
            shutil.rmtree(tbl_dir)
            info(f"清理物理文件: {tbl_dir}")
        if ice_exists:
            try:
                cat.drop_table(ident)
            except Exception:
                # 物理文件已删导致 drop 失败时，直接从 sqlite 清理
                import sqlite3
                sc = sqlite3.connect(f"{w}/import_catalog.db")
                sc.execute("DELETE FROM iceberg_tables WHERE catalog_name='import_cat' AND table_namespace=? AND table_name=?", (ns, tbl))
                sc.commit(); sc.close()
                info(f"从 catalog 清理残留记录: {ident}")
        os.makedirs(f"{tbl_dir}/data", exist_ok=True)

        # 创建新表 & 导入
        ice_schema = build_iceberg_schema(col_defs)
        ice_tbl = cat.create_table(ident, schema=ice_schema, partition_spec=PartitionSpec(spec_id=0))
        parquet_file = f"{tbl_dir}/data/data_{uuid.uuid4().hex[:8]}.parquet"
        pq.write_table(new_data, parquet_file, compression="zstd")
        info(f"  Parquet: {parquet_file}")
        ice_tbl.append(pq.read_table(parquet_file))

        snap = ice_tbl.current_snapshot().snapshot_id
        meta = ice_tbl.metadata_location
        sql = sql_create(ns, tbl, col_defs, str(uuid.uuid4()), meta, args.warehouse, snap)
        info(f"创建完成，snapshot={snap}")

    elif args.op == "insert":
        if not ice_exists:
            # 表不存在则自动 create
            info(f"表不存在，自动创建 ...")
            os.makedirs(f"{tbl_dir}/data", exist_ok=True)
            ice_schema = build_iceberg_schema(col_defs)
            ice_tbl = cat.create_table(ident, schema=ice_schema, partition_spec=PartitionSpec(spec_id=0))
            parquet_file = f"{tbl_dir}/data/data_{uuid.uuid4().hex[:8]}.parquet"
            pq.write_table(new_data, parquet_file, compression="zstd")
            ice_tbl.append(pq.read_table(parquet_file))
            snap = ice_tbl.current_snapshot().snapshot_id
            meta = ice_tbl.metadata_location
            sql = sql_create(ns, tbl, col_defs, str(uuid.uuid4()), meta, args.warehouse, snap)
        else:
            os.makedirs(f"{tbl_dir}/data", exist_ok=True)
            parquet_file = f"{tbl_dir}/data/data_{uuid.uuid4().hex[:8]}.parquet"
            pq.write_table(new_data, parquet_file, compression="zstd")
            info(f"  Parquet: {parquet_file}")
            ice_tbl.append(pq.read_table(parquet_file))
            snap = ice_tbl.current_snapshot().snapshot_id
            meta = ice_tbl.metadata_location
            sql = sql_insert(ns, tbl, snap, meta)
        info(f"追加完成，snapshot={snap}")

    elif args.op == "drop":
        if ice_exists:
            info(f"删除 Iceberg 表 {ident}")
            cat.drop_table(ident)
        # 也删掉 warehouse 目录
        if os.path.isdir(tbl_dir):
            shutil.rmtree(tbl_dir)
            info(f"已删除 {tbl_dir}")
        sql = sql_drop(ns, tbl)

    elif args.op == "update":
        if not args.key:
            p.error("update 操作需要 --key 指定匹配列")
        key = args.key
        if not ice_exists:
            p.error(f"表 {ident} 不存在，无法 update")
        info(f"读取现有数据 ...")
        existing = ice_tbl.scan().to_arrow()
        info(f"  现有 {len(existing)} 行")

        # 确保 key 列类型一致
        key_type = None
        for c in col_defs:
            if c["name"] == key:
                key_type = c["pa_type"]
                break
        if key_type is None:
            p.error(f"key 列 '{key}' 不在表定义中")

        key_arr = new_data.column(key)
        old_key_arr = existing.column(key)

        # count CSV rows per key
        from collections import Counter
        new_counts = Counter(key_arr.to_pylist())

        # 逐行匹配：每个 CSV row 最多替换一条旧行，多余旧行保留
        seen = {}
        keep_mask = []
        for k in old_key_arr.to_pylist():
            n = new_counts.get(k, 0)   # CSV 中该 key 出现了几次
            s = seen.get(k, 0)          # 已替换了几条旧行
            if s < n:
                seen[k] = s + 1
                keep_mask.append(False)  # 被 CSV 替换
            else:
                keep_mask.append(True)   # 保留

        kept = existing.filter(pa.array(keep_mask))
        result = pa.concat_tables([kept, new_data])
        replaced = sum(1 for v in keep_mask if not v)
        info(f"  更新后 {len(result)} 行（替换 {replaced} + 保留 {len(kept)} + 新增 {len(new_data) - replaced}）")

        # 重建 Iceberg 表（全量替换）
        cat.drop_table(ident)
        ice_schema = build_iceberg_schema(col_defs)
        ice_tbl = cat.create_table(ident, schema=ice_schema, partition_spec=PartitionSpec(spec_id=0))
        # 清空旧 parquet，写新的
        old_files = [f for f in os.listdir(f"{tbl_dir}/data") if f.endswith(".parquet")]
        for f in old_files:
            os.remove(os.path.join(f"{tbl_dir}/data", f))
        parquet_file = f"{tbl_dir}/data/data_{uuid.uuid4().hex[:8]}.parquet"
        pq.write_table(result, parquet_file, compression="zstd")
        info(f"  Parquet: {parquet_file}")
        ice_tbl.append(pq.read_table(parquet_file))
        snap = ice_tbl.current_snapshot().snapshot_id
        meta = ice_tbl.metadata_location
        sql = sql_create_or_update(ns, tbl, col_defs, str(uuid.uuid4()), meta, args.warehouse, snap)
        info(f"更新完成，snapshot={snap}")

    elif args.op == "delete":
        if not args.key:
            p.error("delete 操作需要 --key 指定匹配列")
        key = args.key
        if not ice_exists:
            p.error(f"表 {ident} 不存在，无法 delete")
        info(f"读取现有数据 ...")
        existing = ice_tbl.scan().to_arrow()
        info(f"  现有 {len(existing)} 行")

        # CSV 中 key 列的值就是要删除的
        del_keys_set = set(new_data.column(key).to_pylist())
        keep_mask = [k not in del_keys_set for k in existing.column(key).to_pylist()]
        result = existing.filter(pa.array(keep_mask))
        deleted = len(existing) - len(result)
        info(f"  删除 {deleted} 行，剩余 {len(result)} 行")

        # 重建 Iceberg 表
        cat.drop_table(ident)
        ice_schema = build_iceberg_schema(col_defs)
        ice_tbl = cat.create_table(ident, schema=ice_schema, partition_spec=PartitionSpec(spec_id=0))
        old_files = [f for f in os.listdir(f"{tbl_dir}/data") if f.endswith(".parquet")]
        for f in old_files:
            os.remove(os.path.join(f"{tbl_dir}/data", f))
        parquet_file = f"{tbl_dir}/data/data_{uuid.uuid4().hex[:8]}.parquet"
        pq.write_table(result, parquet_file, compression="zstd")
        info(f"  Parquet: {parquet_file}")
        ice_tbl.append(pq.read_table(parquet_file))
        snap = ice_tbl.current_snapshot().snapshot_id
        meta = ice_tbl.metadata_location
        sql = sql_create_or_update(ns, tbl, col_defs, str(uuid.uuid4()), meta, args.warehouse, snap)
        info(f"删除完成，snapshot={snap}")

    # ═══════════════════════════════════════════════════════
    # 输出 & 执行 SQL
    # ═══════════════════════════════════════════════════════
    label = {"create": "建表", "insert": "追加", "drop": "删表", "update": "更新", "delete": "删除"}[args.op]
    print(f"\n{'='*60}")
    print(f"[{args.op.upper()}] {label} — 即将在 openGauss 执行:")
    print(f"{'='*60}")
    print(sql)
    print(f"{'='*60}")

    if args.no_execute:
        info("跳过执行 (--no-execute)")
    elif args.gsql:
        info("执行 SQL ...")
        run_gsql(args.gsql, sql)
    else:
        info("使用 --gsql 自动执行，或手动复制上方 SQL")


if __name__ == "__main__":
    main()
