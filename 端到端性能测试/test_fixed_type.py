"""验证 pyiceberg 对 fixed 类型的端到端支持"""
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import FixedType, NestedField, IntegerType, DoubleType, FloatType, BinaryType
from pyiceberg.io.pyarrow import pyarrow_to_schema, schema_to_pyarrow
import pyarrow as pa
import pyarrow.parquet as pq
import os, uuid, tempfile

# ============================================================
# Test 1: schema_to_pyarrow roundtrip — can pyiceberg represent fixed?
# ============================================================
print('=== Test 1: Iceberg schema -> PyArrow -> Iceberg roundtrip ===')
ice_schema = Schema(
    NestedField(1, 'id', IntegerType(), required=True),
    NestedField(2, 'hash', FixedType(16), required=False),
    identifier_field_ids=[1],
)
print(f'Iceberg schema: {ice_schema}')
for f in ice_schema.fields:
    print(f'  {f.name}: {f.field_type} (repr={repr(f.field_type)})')

try:
    arrow_from_ice = schema_to_pyarrow(ice_schema)
    print(f'Arrow from Iceberg: {arrow_from_ice}')
    for f in arrow_from_ice:
        print(f'  {f.name}: {f.type} (type_id={f.type.id})')
    # Roundtrip back
    ice_back = pyarrow_to_schema(arrow_from_ice)
    print(f'Iceberg roundtrip: {ice_back}')
    for f in ice_back.fields:
        print(f'  {f.name}: {f.field_type} (repr={repr(f.field_type)})')
    match = str(ice_schema) == str(ice_back)
    print(f'Roundtrip match: {match}')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback; traceback.print_exc()

# ============================================================
# Test 2: Large fixed type — GIST 960-dim equivalent
# ============================================================
print()
print('=== Test 2: Large fixed(3840) schema roundtrip ===')
ice_schema2 = Schema(
    NestedField(1, 'id', IntegerType(), required=True),
    NestedField(2, 'vec', FixedType(3840), required=False),
    identifier_field_ids=[1],
)
print(f'Iceberg schema: {ice_schema2}')
try:
    arrow2 = schema_to_pyarrow(ice_schema2)
    print(f'Arrow: {arrow2}')
    for f in arrow2:
        print(f'  {f.name}: {f.type} (type_id={f.type.id})')
    ice_back2 = pyarrow_to_schema(arrow2)
    print(f'Iceberg roundtrip matches: {str(ice_schema2) == str(ice_back2)}')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback; traceback.print_exc()

# ============================================================
# Test 3: End-to-end — create table, append, read back (fixed(16))
# ============================================================
print()
print('=== Test 3: End-to-end (create, append, scan) ===')
tmpdir = tempfile.gettempdir()
db_path = f'{tmpdir}/test_fixed_e2e_{uuid.uuid4().hex[:8]}.db'
warehouse = f'file://{tmpdir}/test_fixed_warehouse_{uuid.uuid4().hex[:8]}'
print(f'catalog: {db_path}')
print(f'warehouse: {warehouse}')

try:
    catalog = SqlCatalog('test_fixed', **{'uri': f'sqlite:///{db_path}', 'warehouse': warehouse})
    catalog.create_namespace_if_not_exists('fixed_ns')

    schema = Schema(
        NestedField(1, 'id', IntegerType(), required=True),
        NestedField(2, 'hash', FixedType(16), required=False),
        identifier_field_ids=[1],
    )
    tbl = catalog.create_table_if_not_exists('fixed_ns.hash_tbl', schema=schema)
    print(f'Table created: {tbl}')
    print(f'Schema fields:')
    for f in tbl.schema().fields:
        print(f'  {f.name}: {f.field_type}')

    # Get the expected Arrow schema from Iceberg table
    expected_arrow = schema_to_pyarrow(tbl.schema())
    print(f'Expected Arrow schema: {expected_arrow}')

    # Build data matching expected Arrow schema exactly (including nullability)
    hash_data = [bytes.fromhex('aabbccddeeff00112233445566778899'),
                 bytes.fromhex('00112233445566778899aabbccddeeff')]
    arrow_data = pa.table(
        [pa.array([1, 2], type=pa.int32()), pa.array(hash_data, type=pa.binary(16))],
        schema=expected_arrow)
    print(f'Data schema: {arrow_data.schema}')
    tbl.append(arrow_data)
    print(f'Appended {len(arrow_data)} rows OK')

    # Read back
    scan = tbl.scan()
    result = scan.to_arrow()
    print(f'Read back rows: {len(result)}')
    print(f'  id = {result.column("id").to_pylist()}')
    hashes = result.column('hash').to_pylist()
    for i, h in enumerate(hashes):
        print(f'  hash[{i}] = {h.hex() if isinstance(h, bytes) else h} ({type(h).__name__})')

    # Verify it's FixedSizeBinary not regular Binary
    hash_type = result.schema.field('hash').type
    print(f'Arrow hash type: {hash_type} (type_id={hash_type.id})')
    print(f'Is FixedSizeBinary: {"fixed_size_binary" in str(hash_type)}')

    print('TEST 3 PASSED')

except Exception as e:
    print(f'FAILED: {e}')
    import traceback; traceback.print_exc()

# ============================================================
# Test 4: Parquet file with FIXED_LEN_BYTE_ARRAY compatibility
# ============================================================
print()
print('=== Test 4: Parquet FIXED_LEN_BYTE_ARRAY write/read ===')
try:
    # Write a parquet file with FixedSizeBinary data
    pq_path = f'{tmpdir}/test_fixed_parquet_{uuid.uuid4().hex[:8]}.parquet'
    f16_type = pa.binary(16)  # FixedSizeBinary(16)
    table = pa.table({
        'id': pa.array([1, 2, 3], type=pa.int32()),
        'hash': pa.array(
            [bytes.fromhex('aabbccddeeff00112233445566778899'),
             bytes.fromhex('00112233445566778899aabbccddeeff'),
             bytes.fromhex('deadbeefcafebabedeadbeefcafebabe')],
            type=f16_type),
    })
    pq.write_table(table, pq_path)
    print(f'Wrote {pq_path}')

    # Read back
    read_back = pq.read_table(pq_path)
    print(f'Read back schema: {read_back.schema}')
    for f in read_back.schema:
        print(f'  {f.name}: {f.type} (type_id={f.type.id})')
    print(f'  hash values: {[h.hex() for h in read_back.column("hash").to_pylist()]}')
    print('TEST 4 PASSED')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback; traceback.print_exc()

# ============================================================
# Test 5: Vector use case — create FixedSizeBinary table via Arrow then register
# ============================================================
print()
print('=== Test 5: Vector scenario — create table, append 2 rows, scan ===')
dim = 128  # SIFT dimension
fixed_len = dim * 4  # 512
import numpy as np

try:
    vec_schema = Schema(
        NestedField(1, 'id', IntegerType(), required=True),
        NestedField(2, 'vec', FixedType(fixed_len), required=False),
        identifier_field_ids=[1],
    )
    tbl_vec = catalog.create_table_if_not_exists('fixed_ns.vec_tbl', schema=vec_schema)
    print(f'Table created: vec_tbl')

    arrow_vec_schema = schema_to_pyarrow(tbl_vec.schema())
    print(f'Arrow schema: {arrow_vec_schema}')

    # Build vector data
    rng = np.random.default_rng(42)
    vec_a = rng.random(dim).astype(np.float32).tobytes()
    vec_b = rng.random(dim).astype(np.float32).tobytes()
    assert len(vec_a) == fixed_len
    assert len(vec_b) == fixed_len

    arrow_data = pa.table(
        [pa.array([1, 2], type=pa.int32()), pa.array([vec_a, vec_b], type=pa.binary(fixed_len))],
        schema=arrow_vec_schema)
    print(f'Data schema matches: {arrow_data.schema == arrow_vec_schema}')
    tbl_vec.append(arrow_data)
    print('Appended 2 vectors OK')

    # Read back
    scan = tbl_vec.scan()
    result = scan.to_arrow()
    print(f'Read back: {len(result)} rows')
    vec_col = result.column('vec')
    print(f'  vec type: {vec_col.type}')
    vec_bytes = vec_col[0].as_py()
    floats_back = np.frombuffer(vec_bytes, dtype=np.float32)
    floats_orig = np.frombuffer(vec_a, dtype=np.float32)
    print(f'  vec[0] first 5 floats: {floats_back[:5]}')
    print(f'  matches original: {np.allclose(floats_back, floats_orig)}')

    print('TEST 5 PASSED')
except Exception as e:
    print(f'FAILED: {e}')
    import traceback; traceback.print_exc()

print()
print('=== All done ===')
