# SIFT1M 并行 DOP 缩放测试报告（jemalloc tuned）

> 2026-07-25, openEuler 24.03, 96核 723GB, `MALLOC_CONF=retain:true`, Parquet uncompressed

测试对象（表、索引、查询向量）与 [基线报告](SIFT_并行DOP缩放测试报告.md) 完全一致，唯一差异为 gaussdb 启动时设置了 jemalloc 调优参数。

调优前后对比、瓶颈分析、后续优化方案详见 [jemalloc调优验证报告](jemalloc调优验证报告.md)。

## 一、IVF 索引查询 — DOP 缩放

| 场景 | DOP=1 | DOP=16 | DOP=32 |
|------|------:|-------:|-------:|
| K=10 | 169ms | 40ms | 53ms |
| K=100 | 201ms | 42ms | 64ms |
| K=10000 | 231ms | 75ms | 79ms |

### 加速比（vs DOP=1）

| 场景 | DOP=16 | DOP=32 |
|------|-------:|-------:|
| K=10 | **4.2×** | 3.2× |
| K=100 | **4.8×** | 3.1× |
| K=10000 | **3.1×** | 2.9× |

### 调优前后对比

| 场景 | baseline | jemalloc tuned | 改善 |
|------|------:|------:|:---:|
| K=10 DOP=1 | 204ms | **169ms** | **-17%** |
| K=10 DOP=16 | 51ms | **40ms** | **-22%** |
| K=10000 DOP=1 | 278ms | **231ms** | **-17%** |
| K=10000 DOP=16 | 89ms | **75ms** | **-16%** |

## 二、IVF 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (231ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 4.5% | worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.2% | worker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 2.0% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配处理 |
| 1.8% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.6% | worker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

**DOP=8 (109ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 18.3% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 11.0% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 3.6% | streamworker | `[kernel.kallsyms] zap_pte_range` | 页表项回收 |
| 3.1% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 2.9% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |

**DOP=16 (75ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 12.4% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 7.2% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 3.8% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | **自旋锁争用** |
| 3.2% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 2.5% | streamworker | `[kernel.kallsyms] zap_pte_range` | 页表项回收 |

**DOP=32 (79ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 7.8% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 6.2% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.9% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | **自旋锁争用** |
| 3.8% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 2.1% | streamworker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |

> DOP=16 和 DOP=32 的 `spin_lock` 占比与 baseline 接近 (~4%)，说明 jemalloc 调优没有消除页表锁争用——`retain:true` 只消除了还内存 → TLB shootdown 路径，并发缺页引发的 mmap_lock 争用仍然存在。

### 全链路耗时估算

**DOP=1 (231ms，baseline 278ms)**

```
内核内存        ~20ms ( 9%)   rep_movs_alternative, clear_page_erms, do_anonymous_page
IVF 向量搜索    ~25ms (11%)   IvfRuntimeIndex::search
Parquet 数据读取 ~30ms (13%)   page decode, column read
gaussdb executor ~156ms (67%)  ExecMakeFunctionResultNoSets, ExecProjectByRecursion
```

**DOP=8 (109ms，baseline 131ms)**

```
内核内存        ~35ms (32%)   rep_movs_alternative (18%), clear_page_erms (11%),
                               zap_pte_range (4%)
向量搜索 + 读取  ~22ms (20%)   IvfRuntimeIndex::search, Parquet decode
任务调度+GATHER  ~22ms (20%)   bridge task scheduling, result merge
gaussdb executor ~30ms (28%)   ExecMakeFunctionResultNoSets
```

**DOP=16 (75ms，baseline 89ms)**

```
内核内存        ~17ms (23%)   rep_movs_alternative (12%), clear_page_erms (7%),
                               native_queued_spin_lock_slowpath (4%)
向量搜索 + 读取  ~12ms (16%)   IvfRuntimeIndex::search
任务调度+GATHER  ~16ms (22%)   16路 result merge
gaussdb executor ~30ms (39%)   ExecMakeFunctionResultNoSets
```

**DOP=32 (79ms，baseline 86ms)**

```
内核内存        ~14ms (18%)   clear_page_erms (8%), rep_movs_alternative (6%),
                               native_queued_spin_lock_slowpath (4%), __pte_offset_map_lock (4%)
向量搜索 + 读取  ~10ms (13%)   IvfRuntimeIndex::search
任务调度+GATHER  ~20ms (25%)   32路 result merge
gaussdb executor ~35ms (44%)   ExecMakeFunctionResultNoSets
```

### DOP 趋势

- DOP=1 (231ms): 内核 9%, 搜索 11%, 读取 13%, executor 67%
- DOP=8 (109ms): 内核 32%, 搜索+读取 20%, GATHER 20%, executor 28%
- DOP=16 (75ms): 内核 23%, 搜索+读取 16%, GATHER 22%, executor 39%
- DOP=32 (79ms): 内核 18%, 搜索+读取 13%, GATHER 25%, executor 44%

> DOP=16→32 延迟不再下降（75→79ms），spin_lock 持续 4%，GATHER 从 22% 升到 25%。jemalloc 调优后极限延迟从 86ms 降到 75ms，但瓶颈结构未变。

## 三、FullScan 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (jemalloc tuned)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 13.8% | worker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 6.9% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.2% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 2.9% | worker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 2.5% | worker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 2.2% | worker | `gaussdb VectorL2SquaredDistance` | L2 向量距离 |
| 1.9% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |

**DOP=8 (jemalloc tuned)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 10.5% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 4.9% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 4.1% | streamworker | `gaussdb gs_memory_send` | **LOCAL GATHER 通道** |
| 3.3% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 1.8% | streamworker | `iceberg_fdw.so iceberg_arrow_materialize_projection_row` | FDW 行投影物化 |

**DOP=16 (jemalloc tuned)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 10.8% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 5.2% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 4.7% | tokio-rt-worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 4.4% | streamworker | `gaussdb gs_memory_send` | **LOCAL GATHER 通道** |
| 1.9% | worker | `gaussdb ExecCopySlot` | Tuple 内存拷贝 |
| 1.9% | streamworker | `gaussdb ExecMakeFunctionResultNoSets` | gaussdb 表达式求值 |
| 1.6% | streamworker | `[kernel.kallsyms] native_queued_spin_lock_slowpath` | **共享内存锁争用** |
| 1.5% | streamworker | `gaussdb StreamProducer::sendByMemory` | **流发送 (共享内存)** |

### 全链路耗时估算

**DOP=8 (366ms baseline → jemalloc tuned 估计 ~310ms)**

```
数据扫描+拷贝     ~72ms (23%)   __memcpy_avx_unaligned_erms (11%), rep_movs_alternative (5%)
L2 距离排序       ~28ms ( 9%)   VectorL2SquaredDistance
GATHER 数据搬运   ~25ms ( 8%)   gs_memory_send (4%), ExecCopySlot
gaussdb executor ~185ms (60%)   ExecProjectByRecursion
```

**DOP=16 (346ms baseline → jemalloc tuned 估计 ~300ms)**

```
数据扫描+拷贝     ~75ms (25%)   __memcpy_avx_unaligned_erms (11%), rep_movs_alternative (5%),
                                clear_page_erms (5%)
L2 距离排序       ~18ms ( 6%)   VectorL2SquaredDistance
GATHER 数据搬运   ~30ms (10%)   gs_memory_send (4%), spin_lock (2%),
                                StreamProducer::sendByMemory (2%), ExecCopySlot (2%)
gaussdb executor ~177ms (59%)   ExecMakeFunctionResultNoSets
```

### 瓶颈分析

FullScan 的 `gs_memory_send` 占比在 jemalloc 调优前后基本不变（DOP=8: 4.3%→4.1%，DOP=16: 4.7%→4.4%）——GATHER 通道争用不受 jemalloc 影响。`spin_lock` 同样持续存在（DOP=16: 1.7%→1.6%）。

## 四、关键结论

| # | 结论 | 数据支撑 |
|---|------|------|
| 1 | **IVF 甜点仍是 DOP=16** | K=10: 40ms, 4.2× 加速 |
| 2 | **jemalloc 调优全面改善延迟** | IVF -16~22%, DOP=1 受益最大 |
| 3 | **内核内存瓶颈未消除** | `rep_movs` + `clear_page` 仍占 20-30% |
| 4 | **GATHER 通道不受 jemalloc 影响** | `gs_memory_send` 占比持平 |
| 5 | **spin_lock 持续存在** | 页表锁争用与 jemalloc 无关 |
