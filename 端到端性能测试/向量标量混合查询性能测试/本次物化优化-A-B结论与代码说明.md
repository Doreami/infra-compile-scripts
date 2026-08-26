# 物化路径优化（#26-29）· 本次修改说明与 A/B 结论

> 本文记录**本次代码修改**（物化路径优化）的内容、A/B 实测结论与现状——与通用性能测试指南分离，避免污染无状态指南。

---

## 1. 本次改了什么（物化路径优化）

针对向量+标量混合查询（`WHERE <标量> ORDER BY vec <-> q LIMIT K`）的**物化路径**优化，三层改动：

| 层 | 改动 | 文件 |
|---|---|---|
| 引擎-列子集读 | `IcebergTableReader.with_projection`：物化读集从「全表列」收窄到「输出列∪向量列」；向量列防御性并集保留（内核精确重排必需） | `iceberg-index-iceberg/src/reader.rs` |
| 引擎-标量先行 | `materialize_candidates_scalar_first`：两阶段物化——阶段①只读标量过滤列→求值→存活行；阶段②存活行读其余投影列；被过滤行不读任何列（含 768d 向量列） | `reader.rs` + `scalar_filter.rs` |
| 引擎-求值器 | 通用标量表达式求值（Int64/Utf8/Float64/Bool，Eq/Lt/Le/Gt/Ge/In/Between，NULL→false，多表达式 AND） | `scalar_filter.rs`（新） |
| 桥接 | 向量 index_profile 解析 `scalar_filter` 透传到引擎 | `iceberg-rust-bridge/scan.rs`、`arrow_stream.rs` |
| FDW | 可下推谓词（=、<、<=、>、>=）序列化为 `scalar_filter` 进向量 profile；EXPLAIN 标注 `Materialization: scalar-first / projection-only`；fdw_private 加槽位 | `iceberg_fdw/*` |

分支：`wip-scalar-first`（已提交，3 仓）；基线 = `ab-baseline`（原始 HEAD）。

## 2. A/B 实测结论（2026-08-26）

**方法**：基线分支（原始 HEAD）vs 优化分支（wip-scalar-first），同 SQL 同矩阵，`bench_morevec.py`（10 查询 × 2 轮中位数，DOP=1）。

**reviews（247K×768d）**：

| tier | 基线(ms) | 优化(ms) | 加速 |
|---|---|---|---|
| No K10 | 1672.5 | 1656.5 | 1.0% |
| No K100 | 1882.3 | 1873.7 | 0.5% |
| 1% K10 | 1667.6 | 1660.0 | 0.5% |
| 1% K100 | 1891.3 | 1869.5 | 1.2% |
| 50% K10 | 1675.0 | 1656.6 | 1.1% |
| 50% K100 | 1897.1 | 1874.9 | 1.2% |

movies：K1000 No 快 2.3%、50% 快 1.6%。**结果集两 build 逐行一致**（正确性 OK）。

**结论**：物化优化延迟收益 ~0.5-1.2%，微乎其微。设计文档"物化是最大耗时"前提在实测下不成立。

## 3. 瓶颈根因（bridge 层计时 + parquet 分析）

`index_scan_by_metadata` 计时（写 /tmp/bridge_timing.log）：
- `load_view` 0ms / `load_registry` 0ms / `load_segment+search` **42ms** / **materialize ~1.74s**

parquet 分析：reviews 每个数据文件 **50,000 行 = 1 个 157MB row-group**。回表按行号读行（哪怕 9 个存活行）要解压整个 row-group → ~1.7s。

**根因**：不是 CPU、不是索引加载、不是搜索，是**回表物化的 parquet row-group 解压**（大 row-group + 随机行读取）。

## 4. 优化价值与后续

- scalar-first 省的是"读哪些行/列"，但省不掉 row-group 解压（读 1 行 = 解压整组）→ 大 row-group 下优化无收益。
- **修复**：导入时小 chunk（5000 行/文件，~15MB row-group）→ 回表读快 5-10×，scalar-first 收益才可能显现。
- 待验证：小 chunk 重导后重跑 A/B，看优化真实收益（见 §5 状态）。

## 5. 当前状态（2026-08-26 晚）

- reviews 正在用小 chunk（5000）重导 + 重建索引。
- 重导后重跑 A/B：若优化收益显现 → 有合入底气；若无收益 → 优化价值存疑，需重新定位。
- 临时计时插桩（timing_log → /tmp/bridge_timing.log）在 reader 路径，验证后移除。
- `ICEBERG_SCALAR_FIRST_LOG` 埋点（fetch_k/survivors）已加但 eprintln 未路由到 gaussdb 日志——不可见，待修（或直接改用文件输出）。

## 6. 附：本次改动附带修复
- fdw Makefile 无 header 依赖 → 改 struct 必须 `--force` 全量重编（否则 stale .o 静默用旧布局，垃圾指针）。详见 common-pitfalls 记忆。
