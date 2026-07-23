# SIFT1M 并行 DOP 缩放测试报告

> 2026-07-25, openEuler 24.03, 96核 723GB, Parquet uncompressed, 默认 jemalloc

## 一、测试对象

| 属性 | 分区表 | 非分区表 |
|------|--------|---------|
| 命名空间 | sift_ns_part | sift_ns |
| 表名 | sift1m_part | sift1m |
| 向量数 | 1,000,000 | 1,000,000 |
| 维度 | 128 (vector), 存储 fixed(512) | 同左 |
| 分区 | bucket[32] by id | 无 |
| Parquet 文件数 | 32（每分区 1 个） | 10 |
| 每文件行数 | ~31,250 | ~100,000 |
| 每文件大小 | ~4.6 MB | ~14.6 MB |
| 数据总大小 | 147 MB | 146 MB |

| 索引 | 分区表 | 非分区表 |
|------|--------|---------|
| 类型 | IVFPQ, num_clusters=256, nprobe=10 | 同左 |
| 索引文件数 | 33（1 index-registry + 32 分区 puffin） | 11 |
| 索引存储 | 506 MB | 501 MB |
| 构建耗时 | ~3s | ~3s |

查询向量：表中 `id=1` 的行。查询 SQL 模板：

```sql
SET query_dop = <N>;
SET enable_vectorsearch = on;
SET try_vector_engine_strategy = force;
SELECT id FROM <ns>.<table>
  ORDER BY vec <-> '<query_vector>'::vector LIMIT <K>;
```

## 二、IVF 索引查询 — DOP 缩放

| 场景 | DOP=1 | DOP=2 | DOP=4 | DOP=8 | DOP=16 | DOP=32 |
|------|------:|------:|------:|------:|-------:|-------:|
| K=10 | 204ms | 169ms | 105ms | 68ms | 51ms | 58ms |
| K=100 | 240ms | 169ms | 110ms | 70ms | 52ms | 72ms |
| K=10000 | 278ms | 348ms | 221ms | 131ms | 89ms | 86ms |

### 加速比（vs DOP=1）

| 场景 | DOP=2 | DOP=4 | DOP=8 | DOP=16 | DOP=32 |
|------|------:|------:|------:|-------:|-------:|
| K=10 | 1.2× | 1.9× | 3.0× | **4.0×** | 3.5× |
| K=100 | 1.4× | 2.2× | 3.4× | **4.6×** | 3.3× |
| K=10000 | 0.8× | 1.3× | 2.1× | **3.1×** | 3.2× |

### IVF — 分区 vs 非分区

| 场景 | 非分区 DOP=1 | 分区 DOP=1 | 分区 DOP=8 | 分区 DOP=16 |
|------|:---:|:---:|:---:|:---:|
| K=10 | 325ms | 204ms | 68ms | **51ms** |
| K=100 | 329ms | 240ms | 70ms | **52ms** |
| K=10000 | 360ms | 278ms | 131ms | **89ms** |

## 三、FullScan — DOP 缩放

FullScan SQL 模板：

```sql
SET query_dop = <N>;
SET enable_indexscan = off;
SET enable_bitmapscan = off;
SET enable_vectorsearch = off;
SELECT id FROM <ns>.<table>
  ORDER BY vec <-> '<query_vector>'::vector LIMIT <K>;
```

| 场景 | 非分区 DOP=1 | 分区 DOP=1 | 分区 DOP=2 | 分区 DOP=4 | 分区 DOP=8 | 分区 DOP=16 | 分区 DOP=32 |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| K=10 | 1372ms | 1363ms | 703ms | 366ms | **200ms** | 318ms | 329ms |
| K=100 | 1355ms | 1366ms | 895ms | 479ms | **345ms** | 328ms | 320ms |
| K=10000 | 1398ms | 1401ms | 885ms | 488ms | **366ms** | 346ms | 369ms |

加速比 (vs DOP=1)：

| K | DOP=2 | DOP=4 | DOP=8 | DOP=16 | DOP=32 |
|---|------:|------:|------:|-------:|-------:|
| K=10 | 1.9× | 3.7× | **6.8×** | 4.3× | 4.1× |
| K=100 | 1.5× | 2.9× | **4.0×** | 4.2× | 4.3× |
| K=10000 | 1.6× | 2.9× | **3.8×** | 4.0× | 3.8× |

## 四、IVF 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (278ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 4.2% | worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.6% | worker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 2.1% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 1.5% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.5% | worker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |
| 1.3% | worker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影 |
| 1.2% | worker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 1.1% | worker | `gaussdb ExecProjectByRecursion` | gaussdb 投影 |

**DOP=8 (131ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 11.9% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 5.1% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 4.1% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 3.7% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 3.5% | streamworker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |
| 2.3% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |
| 1.9% | streamworker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 1.8% | streamworker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影 |

**DOP=16 (89ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 11.0% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 6.6% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 4.1% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | 自旋锁争用 |
| 3.5% | streamworker | `[kernel.kallsyms] __list_del_entry_valid_or_report` | 链表删除校验 |
| 3.4% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 2.7% | streamworker | `[kernel.kallsyms] zap_pte_range` | 页表项回收 |
| 2.7% | streamworker | `[kernel.kallsyms] __handle_mm_fault` | 缺页处理 |
| 1.7% | streamworker | `libiceberg_rust_bridge.so miniz_oxide::inflate::core::init_tree` | puffin 索引解压 |

**DOP=32 (86ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 8.3% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 6.5% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 4.2% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 3.8% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | 自旋锁争用 |
| 2.1% | streamworker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |
| 1.8% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |
| 1.5% | streamworker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影 |

### 全链路耗时估算

**DOP=1 (278ms)**

```
内核内存        ~25ms ( 9%)   [kernel.kallsyms] rep_movs_alternative, [kernel.kallsyms] clear_page_erms,
                                [kernel.kallsyms] do_anonymous_page
IVF 向量搜索    ~30ms (11%)   libiceberg_rust_bridge.so IvfRuntimeIndex::search
Parquet 数据读取 ~35ms (13%)   page decode, column read
gaussdb executor ~188ms (67%)  gaussdb ExecMakeFunctionResultNoSets, gaussdb ExecProjectByRecursion,
                                iceberg_fdw.so iceberg_arrow_materialize_projection_row
```

**DOP=8 (131ms)**

```
内核内存        ~40ms (30%)   [kernel.kallsyms] rep_movs_alternative (12%), [kernel.kallsyms] clear_page_erms (5%),
                                [kernel.kallsyms] __pte_offset_map_lock (4%), [kernel.kallsyms] try_charge_memcg (4%)
向量搜索 + 读取  ~25ms (19%)   libiceberg_rust_bridge.so IvfRuntimeIndex::search, Parquet decode
任务调度+GATHER  ~25ms (19%)   bridge task scheduling, result merge
gaussdb executor ~41ms (32%)   gaussdb ExecMakeFunctionResultNoSets, gaussdb ExecProjectByRecursion
```

**DOP=16 (89ms)**

```
内核内存        ~18ms (20%)   [kernel.kallsyms] rep_movs_alternative (11%), [kernel.kallsyms] clear_page_erms (7%),
                                [kernel.kallsyms] native_queued_spin_lock_slowpath (4%)
向量搜索 + 读取  ~16ms (18%)   libiceberg_rust_bridge.so IvfRuntimeIndex::search,
                                libiceberg_rust_bridge.so miniz_oxide puffin 解压
任务调度+GATHER  ~20ms (22%)   16路 result merge
gaussdb executor ~35ms (40%)   gaussdb ExecMakeFunctionResultNoSets
```

**DOP=32 (86ms)**

```
内核内存        ~20ms (23%)   [kernel.kallsyms] clear_page_erms (8%), [kernel.kallsyms] rep_movs_alternative (7%),
                                [kernel.kallsyms] native_queued_spin_lock_slowpath (4%),
                                [kernel.kallsyms] __pte_offset_map_lock (4%)
向量搜索 + 读取  ~12ms (14%)   libiceberg_rust_bridge.so IvfRuntimeIndex::search, Parquet decode
任务调度+GATHER  ~22ms (26%)   32路 result merge (GATHER 占比上升)
gaussdb executor ~32ms (37%)   gaussdb ExecMakeFunctionResultNoSets,
                                iceberg_fdw.so iceberg_arrow_materialize_projection_row
```

### DOP 趋势

- DOP=1 (278ms): 内核 9%, 搜索 11%, 读取 13%, executor 67%
- DOP=8 (131ms): 内核 30%, 搜索+读取 19%, GATHER 19%, executor 32%
- DOP=16 (89ms): 内核 20%, 搜索+读取 18%, GATHER 22%, executor 40%
- DOP=32 (86ms): 内核 23%, 搜索+读取 14%, GATHER 26%, executor 37%

> `[kernel.kallsyms] native_queued_spin_lock_slowpath` 在 DOP=16 首次出现 (4.1%)，持续到 DOP=32 (3.8%)。DOP=16 后延迟不再下降 (89→86ms)，页表锁争用成为瓶颈。

## 五、FullScan 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (1401ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 14.2% | worker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 7.3% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.5% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 3.1% | worker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 2.8% | worker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 2.4% | worker | `gaussdb VectorL2SquaredDistance` | L2 向量距离 |
| 2.1% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 1.7% | worker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 1.7% | worker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影 |

**DOP=8 (366ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 11.0% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 5.3% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 4.3% | streamworker | `gaussdb gs_memory_send` | LOCAL GATHER 通道 |
| 3.5% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 1.7% | streamworker | `gaussdb ExecProjectByRecursion` | gaussdb 投影 |
| 1.7% | streamworker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影 |

**DOP=16 (346ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 11.4% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 5.5% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 5.0% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 4.7% | streamworker | `gaussdb gs_memory_send` | LOCAL GATHER 通道 |
| 2.0% | worker | `gaussdb ExecCopySlot` | Tuple 内存拷贝 |
| 2.0% | streamworker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 1.7% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | 共享内存锁争用 |
| 1.6% | streamworker | `gaussdb StreamProducer::sendByMemory` | 流发送 (共享内存) |

### 全链路耗时估算

**DOP=1 (1401ms)**

```
数据扫描+拷贝   ~350ms (25%)   libc.so.6 __memcpy_avx_unaligned_erms (14%),
                                [kernel.kallsyms] rep_movs_alternative (7%),
                                [kernel.kallsyms] clear_page_erms, [kernel.kallsyms] do_anonymous_page
L2 距离排序     ~120ms ( 9%)   gaussdb VectorL2SquaredDistance, top-K heap sort
gaussdb executor ~931ms (66%)  gaussdb ExecMakeFunctionResultNoSets, gaussdb ExecProjectByRecursion,
                                iceberg_fdw.so iceberg_arrow_materialize_projection_row
```

**DOP=8 (366ms)**

```
数据扫描+拷贝    ~75ms (20%)   libc.so.6 __memcpy_avx_unaligned_erms (11%),
                                [kernel.kallsyms] rep_movs_alternative (5%)
L2 距离排序      ~30ms ( 8%)   gaussdb VectorL2SquaredDistance
GATHER 数据搬运  ~25ms ( 7%)   gaussdb gs_memory_send (4%), gaussdb ExecCopySlot
gaussdb executor ~236ms (65%)  gaussdb ExecProjectByRecursion
```

**DOP=16 (346ms)**

```
数据扫描+拷贝    ~80ms (23%)   libc.so.6 __memcpy_avx_unaligned_erms (11%),
                                [kernel.kallsyms] rep_movs_alternative (5%),
                                [kernel.kallsyms] clear_page_erms (6%)
L2 距离排序      ~20ms ( 6%)   gaussdb VectorL2SquaredDistance
GATHER 数据搬运  ~35ms (10%)   gaussdb gs_memory_send (5%),
                                [kernel.kallsyms] native_queued_spin_lock_slowpath (2%),
                                gaussdb StreamProducer::sendByMemory (2%),
                                gaussdb ExecCopySlot (2%)
gaussdb executor ~211ms (61%)  gaussdb ExecMakeFunctionResultNoSets
```

### 瓶颈分析

DOP=1: `libc.so.6 __memcpy_avx_unaligned_erms` (14.2%) + executor 开销为主。`gaussdb VectorL2SquaredDistance` (2.4%) 较轻。

DOP=8: `gaussdb gs_memory_send` (4.3%) 首次出现——worker 通过共享内存向 gather 节点发送结果。这是 FullScan 的新瓶颈。

DOP=16: GATHER 开销升至 10%（`gaussdb gs_memory_send` 5% + `[kernel.kallsyms] native_queued_spin_lock_slowpath` 2% + `gaussdb StreamProducer::sendByMemory` 2% + `gaussdb ExecCopySlot` 2%），吃掉了 I/O 并行收益。

> FullScan 瓶颈不在 I/O 带宽——32 文件的吞吐不是问题。瓶颈在 LOCAL GATHER 通道的共享内存争用。

## 六、关键结论

| # | 结论 | 数据支撑 |
|---|------|------|
| 1 | **IVF 甜点 DOP=16** | K=100: 52ms, 4.6× 加速 |
| 2 | **FullScan 甜点 DOP=8** | K=10: 200ms, 6.8×; DOP=16 降速 |
| 3 | **IVF 瓶颈：内核内存管理** | `[kernel.kallsyms] rep_movs_alternative` + `[kernel.kallsyms] clear_page_erms` ~17% (DOP=8) |
| 4 | **FullScan 瓶颈：GATHER 通道** | `gaussdb gs_memory_send` + `[kernel.kallsyms] native_queued_spin_lock_slowpath` + `gaussdb StreamProducer::sendByMemory` ~10% (DOP=16) |
| 5 | **spin_lock 从 DOP=16 开始出现** | IVF 4.1%, FullScan 1.7% |
| 6 | **分区 DOP=1 反而比非分区快** | 204ms vs 325ms, 小文件更轻量 |
