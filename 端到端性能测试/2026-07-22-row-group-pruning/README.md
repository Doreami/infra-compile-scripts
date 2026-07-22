# Row Group 剪枝重测 — 2026-07-22

## 背景

7/21 的 Row Group 剪枝测试显示零收益。7/22 分析发现根因是 GIST 文件只有 1 个 row group（192MB/文件，50K 行），无法剪枝。

## 数据重建

用 PyArrow `write_table(..., row_group_size=5000)` 重建 `gist_ns.gist1m_rg5k` 表：
- 20 个文件 × 10 row group = **200 row group**（原来 25）
- `setup_gist_rg.py`：gaussdb create_table + PyArrow 写文件 + pyiceberg add_files 注册

## 稀疏回表（sparse-row-take）最终结论

真·sparse .so（hash `50d5bce1`）重测结果（同会话 A/B）：

```
K       fxhash    sparse    差异
─────────────────────────────────────
K=1     19,056    19,019    -0.2%
K=10    19,975    18,467    -7.6%
K=100   20,086    18,929    -5.8%
K=1000  21,350    18,699    -12.4%
K=10000 26,321    19,152    -27.2%
```

**稀疏 take 延迟不随 K 增长**，消除应用层物化开销。火焰图证实 `materialize_projection_row`、`datumCopy`、`InitVector` 全部消失。

> ⚠️ 踩坑：`.so.sparse` 曾被错误复制为 fxhash 副本，导致早期所有 A/B 测试都在对比同一个二进制。

## Row Group 剪枝 bug

`feat/row-group-pruning` 分支存在候选行丢失问题：

- Index 返回 50 个候选行（fetch_k=50），pruning 后只拿到 **16 行**
- 丢了 34 个候选行（-68%），说明剪枝逻辑有 bug
- 原因待查：可能是 row group 筛选时跳过了含候选行的 row group

## 性能预估（含 bug，仅供参考）

测试方法：`date +%s%3N` 测量 gsql 墙钟时间，排除 EXPLAIN ANALYZE 开销。

| 分支 | 表 | row groups | 墙钟 |
|------|------|:--:|:--:|
| fxhash | gist1m_none | 25（1/文件） | ~20.5s |
| fxhash | gist1m_rg5k | 200（10/文件） | ~23.1s |
| pruning | gist1m_rg5k | 200（10/文件） | ~18.9s |

关键发现：
- 多 row group 但不剪枝 → 更慢（23.1s vs 20.5s，+13%），metadata 开销真实存在
- Row group 剪枝 → -18%（23.1s → 18.9s）
- **但部分收益来自 bug**（少读 68% 候选行），修 bug 后真实收益预计低于 18%

## fetch_k 分析

gaussdb 中 fetchK 计算：

```cpp
// planvectorsearch.cpp
#define VECTORSEARCH_FETCH_MULTIPLIER 5
#define VECTORSEARCH_FETCH_MIN 50
#define VECTORSEARCH_FETCH_MAX 10000

fetchK = max(min(topK * 5, 10000), max(topK, 50))
```

| SQL K | fetchK | 候选文件命中（GIST 20 文件） |
|:--:|:--:|:--:|
| 1 | 50（MIN 钳住） | ~100% |
| 10 | 50 | ~100% |
| 100 | 500 | ~100% |

K=1 也只能取 50 个候选，无法通过减小 K 来降低回表文件数。

## GIST1M 规模分析

Parquet 默认 ~128-256MB/文件。GIST 1M 行 × 3840B = 3.6GB → 自然形成 ~20 个文件。50 个候选行在无分区、随机分布下几乎必然命中所有文件。

推演：

| 数据量 | 文件数 | 候选碰文件比例 | 回表 vs FullScan |
|:--:|:--:|:--:|:--:|
| 1M（GIST） | ~20 | ~100% | ~1:1 |
| 10M | ~200 | 20-25% | ~1:4 |
| 100M | ~2000 | ~2.5% | ~1:40 |

GIST1M 的规模太尴尬——文件太少、候选散不开。数据量越大，IVF 文件级裁剪优势越明显。

## 火焰图结论修正

原分析称"IVF 和 FullScan 火焰图形状几乎相同、瓶颈都在 gaussdb datumCopy"，经定量对比（SVG `width` 属性）修正：

| 函数 | IVF width | FullScan width |
|------|:--:|:--:|
| iceberg_arrow_materialize_projection_row | **40.2** | 30.4 |
| ExecSort | — | **150.1** |
| datumCopy | 23.0 | 13.7 |

- IVF：最大热点在 bridge 侧物化转换，gaussdb 只处理 10 行
- FullScan：最大热点在排序（1M 行），全套 heap tuple 操作
- 两者瓶颈完全不同

## 关联脚本

- `rebuild_gist_rg5k.py`：读 gist1m_none 重建为多 row group（已废弃，缺 vector_dim）
- `rewrite_rg.py`：原位重写 Parquet 文件（已废弃，会破坏 Iceberg 元数据一致性）
- `setup_gist_rg.py`：完整的 gaussdb 建表 + PyArrow 写文件 + add_files 流程（可复用）
- `ab_test_rg5k.sh`：A/B 测试脚本（已废弃，引用语法有误）

## IVF 回表 vs FullScan 的两条代码路径

### 结论

- **FullScan**：走 Iceberg 标准接口 `TableScan::to_arrow()`，传入谓词 + 投影列，自动获得 row group 过滤、page index、列裁剪等优化
- **IVF 回表**：谓词由索引消费，索引输出 `file_path + row_position`。Iceberg 没有"给定行号直接取数据"的能力，所以 `scan_single_file()` 只能自己拼一条原始路径：`plan_files()` 找到目标文件 → `ArrowReaderBuilder::read()` 把整个文件所有列解码 → 在应用层按 row_position 过滤

### 两条路径的关键差异

|  | Path 1: FullScan | Path 2: IVF 回表 |
|------|------|------|
| 入口 | `TableScan::to_arrow()` | `scan_single_file()` (reader.rs) |
| 传参 | predicate + projection | 全列 + 全文件 |
| row_selection | 支持（predicate→RowSelection） | **无** — builder 只设了 batch_size |
| 列投影 | 支持 | 写死全列 |
| 读取粒度 | 文件 + predicate + row group 过滤 | **整个文件全量解码 → 应用层过滤** |

本质矛盾：Iceberg 标准接口的设计前提是"我知道我要什么数据"（谓词 + 列），索引输出的是"我知道数据在哪"（文件 + 行号）——一个 Iceberg 没预料到的使用模式。

### 稀疏 take 机会

parquet-rs 底层有完整的行级稀疏读取能力（`RowSelection::from_consecutive_ranges()`），可以表达"只读第 100、1000、5000 行"，在 decoder 内 `skip_records()` 跳过无关值，不解码不物化。但 iceberg-rust 没把 `RowSelection` 暴露为"给定 row_positions"的接口——当前只能从 predicate/deletion vector 派生。

要让 IVF 回表走稀疏 take，需要在 iceberg-rust（datainfra fork）里加：

1. `FileScanTask` 新增 `row_offsets: Option<Vec<u64>>` 字段
2. `ArrowReader` 在构建 parquet reader 时把 `row_offsets` 转成 `RowSelection`
3. 或者提供独立的 `with_row_selection(RowSelection)` 入口让调用方直接传

## 待办

- [ ] 修复 `feat/row-group-pruning` 分支的 16 行 bug
- [ ] 修 bug 后重测，获得去除"bug 收益"后的真实剪枝效果
- [ ] iceberg-rust 接入 `RowSelection` 稀疏 take：`FileScanTask` 加 `row_offsets` → 转换 → parquet reader
- [ ] IVF 回表路径（`scan_single_file`）接入稀疏 take 或对齐 FullScan 管线
- [ ] 在更大数据集上验证文件级/row group 级裁剪的扩展性
