# SIFT1M 火焰图分析 — IVF vs FullScan

> 2026-07-23, openEuler 24.03 / 96核 / 723GB
> SIFT1M (128维), IVFPQ nc=256, uncompressed, fixed 类型
> 表: `sift_ns.sift1m`, 索引: `idx_ivf_pq_vec`

## 一、测试条件

| 属性 | 值 |
|------|------|
| 数据集 | SIFT1M, 128维, 1,000,000 行 |
| 存储类型 | fixed(512), uncompressed |
| 索引 | IVFPQ, nc=256, sample_rate=100000 |
| 查询向量 | id=1 的自查向量 |
| LIMIT | 10 |
| 预热 | 1 次 |
| 采样 | perf record -F 99 -g, IVF 20 轮 |

## 二、Top 函数对比

| 函数 | IVF K=10 | FullScan K=10 | 说明 |
|------|:--:|:--:|------|
| `rep_movs_alternative` (kernel) | **32.92%** | 7.83% | 内核内存拷贝（磁盘→page cache） |
| `clear_page_erms` (kernel) | 14.63% | 6.86% | 内核页清零 |
| page fault handler (libc) | 1.10% | **17.14%** | 缺页处理（全扫冷数据量大） |
| `IvfRuntimeIndex::search` | 2.27% | — | IVF 索引搜索 |
| `iceberg_arrow_materialize_projection_row` | — | 2.48% | FDW 行物化 |
| `ExecMakeFunctionResultNoSets` | — | 2.73% | gaussdb 表达式计算 |
| `l2_distance` / `VectorL2SquaredDistance` | — | 1.49% | L2 距离计算 |
| regex_lite (interval filter) | 1.38% | — | Parquet page index 过滤 |
| `String::clone` | 0.80% | — | 文件路径 clone |
| parquet decompress (miniz_oxide) | 0.77% | — | Parquet 解压 |

## 三、按模块分布

### IVF K=10 (560 samples)

```
内核内存操作       █████████████████████████████████████████████████  48.3%
bridge (I/O+index) ██████████████████  8.5%
gaussdb            ██████  4.8%
[unknown]          ████████  9.3%
其他               28.9%
```

- 内核 `rep_movs_alternative` 占 32.9%，call chain 显示走 `filemap_read → ArrowFileReader → read_file_rows_addressed`，是 Parquet 数据读取开销
- IVF 索引搜索 (`IvfRuntimeIndex::search`) 仅 2.27%，索引本身开销很小
- `regex_lite` 1.38% 是 Parquet page index 的 interval filter

### FullScan K=10 (467 samples)

```
内核内存操作       ██████████████████████████  26.9%
缺页 (page fault)  █████████████████████  17.1%
gaussdb            █████████████  12.8%
FDW (iceberg_fdw)  ███  3.2%
[unknown]          █████  5.1%
其他               34.9%
```

- FullScan 扫描 1M 行，缺页处理 17.1% — 数据不在 page cache
- gaussdb 占比 12.8%（距离计算 + heap tuple 构建）
- FDW `materialize_projection_row` 2.48%

## 四、关键发现

### 4.1 IVF 瓶颈在内核态内存拷贝

`rep_movs_alternative` 32.9% 的调用链：
```
read() → filemap_read → copy_page_to_iter → copyout → rep_movs_alternative
```
经 `ArrowFileReader::get_byte_ranges` 触发。这是 Parquet 文件读取的物理 I/O 开销，不是 CPU 计算瓶颈。

### 4.2 索引搜索开销可忽略

`IvfRuntimeIndex::search` 仅 2.27%（~13 samples），IVF 的聚类遍历 + L2 距离计算远小于数据 I/O。与之前 GIST 分析的结论一致——瓶颈不在索引搜索。

### 4.3 FullScan 缺页比例高

FullScan 扫描全部 1M 行（500MB uncompressed），首次访问数据不在 page cache，缺页 17.1%。IVF 只访问索引覆盖的少量文件，缺页仅 1.1%。

### 4.4 与之前分析的对比

| 指标 | 本次 (SIFT) | 之前 (GIST, 7/21) |
|------|:--:|:--:|
| IVF 内核 I/O | 48.3% | 类似 |
| IVF 索引搜索 | 2.27% | <1% |
| FullScan 缺页 | 17.1% | — |
| FullScan gaussdb | 12.8% | ~15% |

SIFT vs GIST 的差异符合预期：维度越低（128 vs 960），I/O 占比越小，gaussdb 开销占比越大。

## 五、火焰图

- [IVF K=10](flame_sift_ivf_k10.svg) (431K, 560 samples)
- [FullScan K=10](flame_sift_fullscan_k10.svg) (130K, 467 samples)
