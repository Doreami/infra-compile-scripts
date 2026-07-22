# 批量回表 — Row Group 文件内剪枝 设计方案

**日期**: 2026-07-21
**范围**: `iceberg-index/crates/iceberg-index-iceberg/src/reader.rs`
**分支**: `feat/siphash-to-fxhash` (iceberg-index)

---

## 一、问题建模

### 1.1 当前回表流程

```
IVF 索引搜索 → 50 个候选行 (RowAddress)
    │
    ▼
materialize_candidates()
    │
    ▼
read_file_rows_addressed(addresses)
    │
    ├─ 按 file_path 分组 (50 个候选 → ~20 个文件)
    │
    └─ for each file:
         │
         ▼
         read_single_file_rows_addressed(file_path, [2-3 positions])
              │
              ▼
              scan_single_file(file_path)          ◄── 问题所在
                   │
                   ├─ plan_files() → 遍历所有 manifest 条目
                   ├─ 过滤出该文件
                   ├─ ArrowReaderBuilder → 读取该文件全部 row group
                   └─ 逐 batch 解码所有行 (含 vec 3840B/行)
                        │
                        ▼
                   在 ~50K 行中过滤出 2-3 个目标行
```

### 1.2 浪费量的量化（GIST1M 为例）

```
每个 Parquet 文件:
  ├─ 50K 行, ~50 个 row group (每个 ~1000 行)
  ├─ 当前: 读取全部 50 个 row group → 解码 50K × 3848B ≈ 192MB
  ├─ 需要: 2-3 个候选行 → 只需 1-2 个 row group
  └─ 浪费: ~96% 的 I/O 和解码

20 个文件:
  ├─ 当前: 20 × 192MB ≈ 3.84GB 解码 (≈ 一次全表扫描)
  └─ 优化后: 20 × 4-8MB ≈ 80-160MB 解码
```

### 1.3 火焰图证据

IVF 无压缩火焰图中 `GenericColumnReader::read_records` 占 **33.28%**，是 `scan_single_file` 内 Parquet vec 列解码的直接体现。减少 95% 解码量 → 此热点同比例缩减。

---

## 二、技术方案

### 2.1 核心思路

绕过 Iceberg Arrow reader 的全文件扫描，改为 **parquet crate 直读 + row group 过滤**：

```
scan_single_file(file_path)          ← 当前: Iceberg scan → 全文件 Arrow 流
    ↓
read_file_row_groups(file_path, positions)  ← 新: parquet crate 直读, 按 row group 过滤
```

### 2.2 Row Group 定位算法

```
输入: file_path, target_positions: [u64]
输出: 只包含目标行的 RecordBatch 流

步骤:
  1. 打开文件 → 读取 Parquet footer (metadata)
  2. 遍历 row_groups:
       cumulative = 0
       for (idx, rg) in row_groups.enumerate():
           if target_positions 与 [cumulative, cumulative + rg.num_rows()) 有交集:
               selected_indices.push(idx)
           cumulative += rg.num_rows()
  3. 用 selected_indices 创建 ParquetRecordBatchReaderBuilder
     → .with_row_groups(selected_indices)
     → .build()
  4. 流式读取, 逐 batch 过滤具体行位置 (逻辑与现有一致)
```

### 2.3 API 选型

使用 **parquet 异步 API** + Iceberg 已有的 `ArrowFileReader` 适配层：

| 层 | API | 用途 |
|---|-----|------|
| **Iceberg** | `InputFile::reader()` | 获取 `Box<dyn FileRead>` (range read, 适配本地FS/S3/内存) |
| **Iceberg** | `ArrowFileReader::new(meta, reader)` | 将 `FileRead` 适配为 parquet 的 `AsyncFileReader` |
| **Parquet** | `AsyncFileReader::get_metadata()` | 读取 Parquet footer → 获取 row group 列表和 `num_rows` |
| **Parquet** | `ParquetRecordBatchStreamBuilder::new(reader)` | 构建异步流式读取器 |
| **Parquet** | `.with_row_groups(Vec<usize>)` | **只读指定 row group** |
| **Parquet** | `.with_projection(SchemaRef)` | 列裁剪 (预留, 后续配合 FDW 改造) |
| **Parquet** | `.build()` → `ParquetRecordBatchStream` | 异步流, `TryStreamExt` 兼容 |

### 2.4 S3 兼容性 —— 无需 Seek

整个调用链基于 `AsyncFileReader` trait，它只需要两个能力：

```rust
// parquet::arrow::async_reader::AsyncFileReader
fn get_bytes(&mut self, range: Range<u64>) -> BoxFuture<'_, Result<Bytes>>;    // 读指定字节范围
fn get_metadata(&mut self) -> BoxFuture<'_, Result<Arc<ParquetMetaData>>>;     // 读 footer
fn get_byte_ranges(&mut self, ranges: Vec<Range<u64>>) -> BoxFuture<'_, Result<Vec<Bytes>>>;  // 批量读
```

`ArrowFileReader` 内部委托给 `FileRead::read(range)`，底层就是 HTTP Range 请求。**不依赖 Seek，S3 完全支持**。

```
用户代码
  → ArrowFileReader (AsyncFileReader impl)
    → FileRead::read(range)
      → S3: GET with Range header
      → 本地: pread64
      → 内存: slice
```

---

## 三、改动清单

### 3.1 Cargo.toml 变更

**文件**: `crates/iceberg-index-iceberg/Cargo.toml`

```diff
 [dependencies]
+parquet.workspace = true            # parquet::arrow::async_reader::AsyncFileReader
+iceberg.workspace = true            # 新增: ArrowFileReader (已有间接依赖, 显式声明)

 [dev-dependencies]
-parquet.workspace = true            # 移除
```

> 注: `iceberg` 在 `iceberg-index-iceberg` 已有间接依赖 (通过 `iceberg-index-core` workspace)，但需要显式声明以直接使用 `ArrowFileReader`。

### 3.2 reader.rs 变更

```
IcebergTableReader
  │
  ├─ [不变] list_data_files()
  ├─ [不变] read_file_rows()          — 委托到新实现
  ├─ [不变] read_file_rows_addressed()
  │     └─ 内部调用改为 read_single_file_row_groups()  /* 替换 read_single_file_rows_addressed */
  ├─ [不变] materialize_candidates()
  │     └─ 同上, 间接受益
  │
  ├─ [不变] scan_single_file()        — 保留, FullScan 等其他路径仍用
  ├─ [新增] read_single_file_row_groups()
  │     └─ parquet 直读 + row group 过滤 + 逐 batch 行过滤
  │
  ├─ [不变] read_single_file_rows_addressed()  — 保留作为 fallback
  └─ [不变] position_counts()
```

**新增方法签名**:

```rust
/// 使用 parquet row group 过滤读取单文件的指定行。
/// 只解码包含目标行的 row group, 跳过无关 row group。
async fn read_single_file_row_groups(
    &self,
    file_path: &str,
    row_positions: &[u64],
) -> Result<(Vec<RecordBatch>, Vec<RowAddress>)>;
```

### 3.3 调用关系变更

```
改前:
  read_file_rows_addressed()
    └─ for file: read_single_file_rows_addressed(file, positions)
         └─ scan_single_file(file)  ← Iceberg Arrow reader, 全文件

改后:
  read_file_rows_addressed()
    └─ for file: read_single_file_row_groups(file, positions)  ← 新增
         ├─ 成功: 返回过滤后的 batches
         └─ 失败: fallback → read_single_file_rows_addressed(file, positions)  ← 保留
              └─ scan_single_file(file)  ← 原路径兜底
```

### 3.4 改动规模估算

| 文件 | 增 | 删 | 改 | 说明 |
|------|:--:|:--:|:--:|------|
| `reader.rs` | ~80 行 | 0 | ~10 行 | 新增 row group 方法 + 调用点替换 |
| `Cargo.toml` | 1 行 | 1 行 | 0 | parquet 移到 dependencies |
| **合计** | ~80 行 | ~1 行 | ~10 行 | 改动集中, 不影响 FFI/协议 |

---

## 四、风险 & 缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|:--:|:--:|------|
| Iceberg file I/O 不实现 Seek | 低 | 中 | fallback 到原路径, 不阻塞 |
| Parquet footer 读取失败 (文件损坏) | 低 | 低 | 错误传播, 已有 Error 类型 |
| row group num_rows 与 position 语义不一致 (delete file 场景) | 低 | 中 | 已有 delete file 检查提前拒绝 |
| 兼容性: 依赖 parquet crate 已有版本 | 无 | — | workspace 已有 v58.3.0 |
| 性能: footer 读取增加固定开销 | — | 低 | footer ~几KB, 远小于省掉的 192MB×20 |

---

## 五、验证方案

### 5.1 正确性验证

```
GIST1M 对比:
  SELECT id FROM gist_ns.gist1m ORDER BY vec <-> query_vec LIMIT K;
  主分支 vs 优化分支, K=10/100/10000, 各 5 轮
  → id 序列和分数必须完全一致
```

### 5.2 性能验证

| 测试 | 数据 | 场景 | K | 预期 |
|------|------|------|:--:|------|
| GIST IVF | gist1m_1M_3840d_none | IVF 索引扫 | 10 | 19.8s → 5-8s |
| GIST IVF | gist1m_1M_3840d_none | IVF 索引扫 | 100 | ~20s → ~6-9s |
| SIFT IVF | sift1m_1M_128d_none | IVF 索引扫 | 10 | 回归测试 (预期持平/略优) |
| 火焰图 | 同上 | IVF K=10 | — | GenericColumnReader 热点显著缩小 |

### 5.3 Fallback 验证

人为构造不支持 seek 的路径 → 确认自动降级到原路径, 查询正常返回。

---

## 六、后续扩展

Row group 过滤实现后, 配合 Parquet 的 `with_projection()` 可进一步做列裁剪。当 FDW 侧完成 `projected_columns` 透传改造后, 只需在此方法多加一行 `.with_projection(schema)` 即可完成端到端列裁剪。

---

## 七、实现步骤

```
Step 1: Cargo.toml — parquet + iceberg 显式依赖
Step 2: read_single_file_row_groups() 实现
         ├─ InputFile → ArrowFileReader → get_metadata() → 定位 row groups
         ├─ 新建 ArrowFileReader → ParquetRecordBatchStreamBuilder::with_row_groups()
         └─ 流式读取 → 逐 batch 过滤行位置 (复用 position_counts 逻辑)
Step 3: read_file_rows_addressed 调用点切换 + fallback 到原路径
Step 4: 编译验证 (bash build.sh bridge --release)
Step 5: 服务器部署 + 正确性验证 (GIST + SIFT, K=10/100/10000)
Step 6: 性能对比 + 火焰图
Step 7: 提交
```

### 伪代码

```rust
use iceberg::arrow::ArrowFileReader;
use parquet::arrow::async_reader::{AsyncFileReader, ParquetRecordBatchStreamBuilder};

async fn read_single_file_row_groups(
    &self,
    file_path: &str,
    row_positions: &[u64],
) -> Result<(Vec<RecordBatch>, Vec<RowAddress>)> {
    // 1. 打开文件, 读取 Parquet footer 获取 row group 布局
    let input = self.table.file_io().new_input(file_path)?;
    let meta = input.metadata().await?;  // FileMetadata { size }
    let reader1: Box<dyn FileRead> = input.reader().await?;
    let mut ar1 = ArrowFileReader::new(meta, reader1);
    let parquet_meta = ar1.get_metadata(None).await?;  // 读 footer

    // 2. 定位: 哪些 row group 包含目标行
    let mut selected = Vec::new();
    let mut cumulative: u64 = 0;
    for (idx, rg) in parquet_meta.row_groups().iter().enumerate() {
        let rg_end = cumulative + rg.num_rows() as u64;
        if row_positions.iter().any(|&p| p >= cumulative && p < rg_end) {
            selected.push(idx);
        }
        cumulative = rg_end;
    }
    if selected.is_empty() { return Ok((vec![], vec![])); }

    // 3. 新建 reader, 只读选中的 row groups
    let reader2 = input.reader().await?;
    let meta2 = input.metadata().await?;
    let ar2 = ArrowFileReader::new(meta2, reader2);
    let stream = ParquetRecordBatchStreamBuilder::new(ar2)?
        .with_row_groups(selected)
        .with_batch_size(self.batch_size.unwrap_or(1024))
        .build()?;

    // 4. 流式读取 + 逐 batch 过滤行位置 (逻辑与现有一致)
    let positions_count = position_counts(row_positions);
    let mut out_batches = Vec::new();
    let mut out_addrs = Vec::new();
    let mut counter: u64 = 0;

    while let Some(batch) = stream.try_next().await? {
        // ... 同 read_single_file_rows_addressed 的过滤逻辑
    }
    Ok((out_batches, out_addrs))
}
```
