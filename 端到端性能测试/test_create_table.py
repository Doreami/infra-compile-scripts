"""验证: Catalog.create_table + pyiceberg StaticTable append"""
import os, json, subprocess, tempfile, numpy as np, pyarrow as pa
from pyiceberg.table import StaticTable
from pyiceberg.io.pyarrow import schema_to_pyarrow

gsql = os.path.expanduser(os.environ.get("GAUSSHOME", "")) + "/bin/gsql"
if not os.path.exists(gsql):
    gsql = "gsql"

def gsql_run(sql, timeout=30):
    r = subprocess.run([gsql, "-d", "postgres", "-p", "37000", "-c", sql],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"  FAIL: {r.stderr.strip()}")
    return r

dim = 128
fixed_len = dim * 4  # 512

# 1. create_table via openGauss Catalog
print("=== Step 1: create_table ===")
schema_json = json.dumps({
    "type": "struct",
    "schema-id": 0,
    "fields": [
        {"id": 1, "name": "id", "type": "long", "required": True},
        {"id": 2, "name": "vec", "type": f"fixed[{fixed_len}]", "required": False, "vector_dim": dim},
    ]
})
r = gsql_run(f"SELECT iceberg_catalog.create_namespace('test_ns');")
r = gsql_run(
    f"SELECT iceberg_catalog.create_table("
    f"'test_ns', 'vec_test', '{schema_json}'::jsonb,"
    f"'file://$HOME/warehouse/test_ns/vec_test');",
    timeout=30)
print(r.stdout.strip())

# Parse metadata_location from response
# create_table returns JSON: {"metadata_location": "...", ...}
resp = json.loads(r.stdout.strip())
md_path = resp.get("metadata_location", "")
print(f"  metadata_location: {md_path}")

# 2. Verify foreign table column type
print("\n=== Step 2: Verify column type ===")
r = gsql_run(f"SELECT attname, atttypid::regtype FROM pg_attribute "
             f"WHERE attrelid = 'test_ns.vec_test'::regclass AND attnum > 0;")
print(r.stdout.strip())

# 3. Append data via pyiceberg StaticTable
print("\n=== Step 3: Append data via pyiceberg StaticTable ===")
warehouse = os.path.expanduser(os.environ.get("ICEBERG_WAREHOUSE", "file://$HOME/warehouse"))
if warehouse.startswith("file://"):
    warehouse = warehouse[7:]
warehouse = f"file://{warehouse}"
from pyiceberg.io import load_file_io
io = load_file_io({}, warehouse)
tbl = StaticTable.from_metadata(os.path.join(warehouse.replace("file://", ""), md_path), io)

# Get Arrow schema from table
arrow_schema = schema_to_pyarrow(tbl.schema())
print(f"Arrow schema: {arrow_schema}")

# Write test data
vec_a = np.random.default_rng(42).random(dim).astype(np.float32).tobytes()
vec_b = np.random.default_rng(43).random(dim).astype(np.float32).tobytes()
batch = pa.table(
    [pa.array([1, 2], type=pa.int64()),
     pa.array([vec_a, vec_b], type=pa.binary(fixed_len))],
    schema=arrow_schema)
tbl.append(batch)
print(f"Appended 2 rows OK")

# 4. Verify row count + query
print("\n=== Step 4: Verify query ===")
r = gsql_run(f"SELECT count(*) FROM test_ns.vec_test;")
print(r.stdout.strip())

r = gsql_run(f"SELECT id FROM test_ns.vec_test ORDER BY id;")
print(r.stdout.strip())

# 5. Cleanup
print("\n=== Step 5: Cleanup ===")
gsql_run(f"DROP FOREIGN TABLE test_ns.vec_test;")
gsql_run(f"SELECT iceberg_catalog.drop_table('test_ns', 'vec_test');")

print("\n=== Done ===")
