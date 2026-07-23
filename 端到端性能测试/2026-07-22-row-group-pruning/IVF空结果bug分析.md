# IVF 查询返回空结果 — `file_path` 被 P0 优化设为空字符串导致候选全被过滤

## 现象

所有 IVF 索引查询返回 0 行，FullScan 正常。

```sql
SET enable_vectorsearch=on;
SET try_vector_engine_strategy=force;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '[...]'::vector LIMIT 10;
-- 返回 (0 rows)
```

EXPLAIN ANALYZE 显示 `Foreign Scan` 的 `actual rows=0`，`Input Rows: 0`。

## 根因

commit `b5376c0` ("feat: index performance optimize (#153)") 在 `crates/iceberg-index-plugins/src/ivf.rs` 的 `IvfRuntimeIndex::search()` 中：

```rust
// 修改前（正常）
file_path: partition.file_paths[partition.file_indices[row_idx] as usize].clone(),

// 修改后（bug）
// P0: defer String clone — use empty string + file_index
file_path: String::new(),
```

意图是通过 `file_index` + `file_table` 延迟 String clone。但下游 `index_scan_vector()`（`crates/iceberg-index-table/src/metadata.rs`）的候选过滤逻辑未同步更新：

```rust
let candidates: Vec<IndexCandidate> = candidates
    .into_iter()
    .filter(|c| segment.covered_data_files.contains(&c.address.file_path))
    //                                                ^^^^^^^^^^^^^^^^^^^^
    //                                     file_path 永远是 "" → 永不匹配 → 全部过滤
    .collect();
```

**搜索返回 50 候选 → 全部被 filter 过滤 → 0 结果 → IVF 返回空。**

## 复现

1. SIFT1M 或 GIST1M 数据集，建 IVF/IVFPQ 索引
2. `SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;`
3. 任意向量查询，返回 0 行
4. `SET enable_vectorsearch=off;` 的 FullScan 正常返回

## 修复

恢复 `file_path` 的正确赋值（见 diff）。

但 P0 优化的目标（避免 String clone）仍需正确的实现方式：
- 在 `index_scan_vector` 中用 `file_index` + 返回的 `file_paths()` 解析路径
- 或改用其他避免 clone 的方式

## 影响范围

所有使用 IVF/IVFPQ 索引的查询（SIFT、GIST 等全部数据集）。

## 测试条件

- openEuler 24.03, 96 核, 723GB
- SIFT1M (128 维), IVFPQ nc=256, uncompressed, fixed 类型
- 代码: iceberg-index @ b5376c0
