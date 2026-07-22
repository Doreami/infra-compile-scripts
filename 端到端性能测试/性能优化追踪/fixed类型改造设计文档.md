# vector(n) → Iceberg fixed(n*4) 类型改造设计文档

## 版本信息

| 组件 | Commit | 改动内容 |
|------|--------|---------|
| iceberg_fdw | `7ae7f00` | 类型映射 + Arrow 物化 |
| openGauss-Catalog | `3e13f7e` | 外表类型推导 |
| iceberg-index | `cf16b35` | IVF 插件向量列支持 |

---

## 一、背景

### 问题

`vector(n)` 列在 Iceberg 中表示为 `list<float>`（嵌套类型），Parquet 物理层产生三层额外编码：

```
list<float> Parquet 编码:
  ┌──────┬──────┬──────────────────┐
  │ Rep  │ Def  │ 实际 float 值     │
  │ 1B   │ 1B   │ n × 4B           │
  └──────┴──────┴──────────────────┘
       ↑ 每次读取需 RLE 解码 (14% CPU)
               ↑ 每次读取需 Rep/Def 解码 (12% CPU)
                 ↑ 需 ListArray 偏移量构造 (13% CPU)
---

共计 ~39% CPU 开销（来自 SIFT1M 火焰图实测）。

### 目标

将 `vector(n)` 的 Iceberg 类型从 `list<float>` 改为 `fixed(n*4)`，消除嵌套编码开销。

---

## 二、设计

### 核心思路

```
vector(960) → Iceberg fixed(3840) → Parquet FIXED_LEN_BYTE_ARRAY(3840)
                                      ↓
                                无 Rep/Def, 无 RLE, 无 ListArray
                                      ↓
                              Arrow FixedSizeBinary(3840)
                                      ↓
                              FDW: memcpy → openGauss vector
```

| 层 | 旧 | 新 |
|------|-----|------|
| openGauss 外表类型 | `vector(960)` | `vector(960)`（不变） |
| FDW 内部映射 | `"list<float>"` | `"fixed(3840)"` |
| Iceberg metadata | `{"type":"list","element":"float",...}` | `"fixed[3840]"` |
| Parquet 物理 | `List<Float32>` + Rep/Def/RLE | `FIXED_LEN_BYTE_ARRAY(3840)` |
| Arrow C Data | `List<Float32>` (offsets+children) | `FixedSizeBinary(3840)` (flat) |

### 用户视角不变

```sql
\d+ gist_ns.gist1m
  vec | vector(960) | ...   -- 用户看到的始终是 vector(960)

SELECT id FROM gist_ns.gist1m ORDER BY vec <-> '[...]' LIMIT 10;  -- 查询不变
```

---

## 三、改动详情

### 3.1 FDW: type_adapter.cpp

**位置**: `iceberg_fdw/src/type_adapter.cpp:71-73`

```cpp
// 旧
case VECTOROID:
    mapping->iceberg_type = pstrdup("list<float>");
    break;

// 新
case VECTOROID: {
    int dim = icebergVectorDimFromTypmod(pg_typmod, attname);
    mapping->iceberg_type = psprintf("fixed(%d)", dim * 4);
    break;
}
```

`create_table` 时 FDW 告诉 Catalog 向量列的 Iceberg 类型是 `fixed(n*4)`。

### 3.2 FDW: sdk_scan_adapter.cpp

**位置**: `iceberg_fdw/src/sdk_scan_adapter.cpp:269-313`

新增 `FixedSizeBinary` Arrow 数组的物化路径。判断依据：`array->n_children == 0`（FixedSizeBinary 无子数组）。

```
旧: array->children[0] → offsets → Float32 values → InitVector → memcpy
新: array->buffers[1]  → (row * 4*dim) offset → memcpy → InitVector
```

`FixedSizeBinary` 是单一 flat buffer，无需解析 offsets 和 children，代码更简单。

### 3.3 Catalog: fdw_util.cpp

**位置**: `openGauss-Catalog/src/fdw_util.cpp:327-356`

在现有的 `list<float>` + `vector_dim` 类型推导旁，新增 `fixed[L]` + `vector_dim` 分支：

```cpp
else if (type_val != NULL) {
    /* vector_dim on fixed(L) — L = dim × 4 bytes. */
    text *type_txt = ...;
    if (sscanf(type_str, "fixed[%ld]", &fixed_len) == 1 &&
        fixed_len > 0 && fixed_len % 4 == 0 &&
        fixed_len / 4 == vector_dim) {
        fields[nfields].sql_type = psprintf("vector(%ld)", vector_dim);
    }
}
```

`create_table` 路径：FDW 传 `vector_dim` → Catalog 存 `field_vector_dim` → 外表类型 `vector(N)`。

### 3.4 Index: ivf.rs (VectorColumn)

**位置**: `iceberg-index/crates/iceberg-index-plugins/src/ivf.rs:563`

新增 `FixedBinary` 变体，接受 `FixedSizeBinary` 作为向量列：

```rust
pub enum VectorColumn<'a> {
    Fixed(&'a FixedSizeListArray),           // 原有: FixedSizeList<Float32>
    FixedBinary(&'a FixedSizeBinaryArray),   // 新增: FixedSizeBinary
    List(&'a ListArray),                     // 原有: List<Float32>
    LargeList(&'a LargeListArray),           // 原有: LargeList<Float32>
    Utf8(&'a StringArray),                   // 原有: JSON字符串
}
```

`value()` 方法中将 bytes reinterpret 为 f32：

```rust
Self::FixedBinary(array) => {
    let bytes = array.value(row);
    let floats: Vec<f32> = bytes
        .chunks_exact(size_of::<f32>())
        .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
        .collect();
    Arc::new(Float32Array::from(floats))
}
```

`validate_definition` 也接受 `FixedSizeBinary(width % 4 == 0)`。

---

## 四、数据流

### 写入（create_table + append）

```
1. openGauss: CREATE FOREIGN TABLE via create_table
   → schema JSON 带 vector_dim:960
   → Catalog 存储 field_vector_dim=960
   → Iceberg metadata: "type": "fixed[3840]"
   → 外表: vec vector(960)

2. pyiceberg: append data
   → 读表 schema → FixedSizeBinary(3840) → Parquet FIXED_LEN_BYTE_ARRAY(3840)

3. Catalog: UPDATE tables_internal.metadata_location = '<新路径>'
   → 外表自动指向新数据
```

### 读取（查询）

```
1. gsql: SELECT ... ORDER BY vec <-> '[...]' LIMIT 10
2. openGauss: VectorSearch plan node
   → FDW iceberg_fdw
3. FDW: 调 bridge hybrid_scan
4. Bridge: 读 Parquet → Arrow FixedSizeBinary(3840)
5. IVF: VectorColumn::FixedBinary → f32 转换 → k-means 搜索
6. FDW: icebergArrowVectorDatum → memcpy → openGauss vector
7. openGauss: Top-K 排序 → 返回
```

---

## 五、性能收益

| 场景 | list<float> | fixed | 加速比 |
|------|:--:|:--:|:--:|
| SIFT IVF K=10 | 1044ms | 606ms | **1.7×** |
| GIST IVF K=10 | 7031ms | 5679ms | **1.2×** |
| GIST FullScan K=10 | 21143ms | 15428ms | **1.4×** |
| GIST IVF K=100 | 15639ms | 12504ms | **1.3×** |

火焰图对比（SIFT IVF K=10）：

| 热点 | 旧 | 新 |
|------|:--:|:--:|
| RLE 解码 | 14% | — |
| Rep/Def 解码 | 12% | — |
| ListArray 构造 | 13% | — |
| Zstd 解压 | 3% | 33% |
| **嵌套解码总计** | **39%** | **0%** |

---

## 六、兼容性

- **外表类型**: `vector(N)` 不变，用户无感知
- **查询语法**: `<->` 运算符不变
- **索引**: `create_index` 不变
- **存量表**: 不支持（需要重建），当前开发阶段无存量数据
- **Iceberg 兼容**: `fixed(L)` 是 Iceberg v2 标准类型
