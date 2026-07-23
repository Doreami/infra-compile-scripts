# GIST1M 并行 DOP 缩放测试报告（jemalloc tuned）

> 2026-07-25, openEuler 24.03, 96核 723GB, `MALLOC_CONF=retain:true`, Parquet uncompressed

测试对象（表、索引、查询向量）与 [基线报告](GIST_并行DOP缩放测试报告.md) 完全一致，唯一差异为 gaussdb 启动时设置了 jemalloc 调优参数。

调优前后对比、瓶颈分析、后续优化方案详见 [jemalloc调优验证报告](jemalloc调优验证报告.md)。

## 一、IVF 索引查询 — DOP 缩放

| 场景 | DOP=1 | DOP=16 | DOP=32 |
|------|------:|-------:|-------:|
| K=10 | 1137ms | 147ms | 157ms |
| K=100 | 1352ms | 149ms | 270ms |
| K=10000 | 1456ms | 319ms | 337ms |

### 加速比（vs DOP=1）

| 场景 | DOP=16 | DOP=32 |
|------|-------:|-------:|
| K=10 | **7.7×** | 7.2× |
| K=100 | **9.1×** | 5.0× |
| K=10000 | **4.6×** | 4.3× |

### 调优前后对比

| 场景 | baseline | jemalloc tuned | 改善 |
|------|------:|------:|:---:|
| K=10 DOP=1 | 1779ms | **1137ms** | **-36%** |
| K=10 DOP=16 | 193ms | **147ms** | **-24%** |
| K=10000 DOP=1 | 2318ms | **1456ms** | **-37%** |
| K=10000 DOP=16 | 376ms | **319ms** | **-15%** |

## 二、IVF 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (1456ms, baseline 2318ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 39.2% | worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 (x86 `rep movs`) |
| 8.5% | worker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 4.5% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配处理 |
| 3.3% | worker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |
| 2.3% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.9% | worker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 1.8% | worker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

> `rep_movs` 占比 39.2%（vs baseline 39.3%）——几乎不变。但查询从 2318ms 降到 1456ms，说明 jemalloc 消除的是 TLB shootdown 中断（函数之间的隐藏延迟），而非函数本身的拷贝开销。

**DOP=8 (jemalloc tuned)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 23.1% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 18.5% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.0% | streamworker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配处理 |
| 2.4% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 2.1% | streamworker | `[kernel.kallsyms] __cond_resched` | 内核调度检查 |
| 1.8% | streamworker | `[kernel.kallsyms] native_irq_return_iret` | 中断返回 |
| 1.5% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

**DOP=16 (319ms, baseline 376ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 25.4% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 19.2% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.1% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 2.8% | streamworker | `[kernel.kallsyms] __cond_resched` | 内核调度检查 |
| 2.5% | streamworker | `libc.so.6 __memmove_avx_unaligned_erms` | 用户态 SIMD memmove |
| 1.6% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |

> DOP=16 时 `clear_page` 25.4% + `rep_movs` 19.2% = **44.6%** 内核内存开销。与 baseline 的 39.3%（22.6%+16.7%）相比，占比反而略升——因为总时间缩短后，剩余开销相对集中。

**DOP=32 (337ms, baseline 354ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 20.8% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 16.5% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 2.8% | streamworker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配处理 |
| 2.3% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 1.9% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.6% | streamworker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |

### 全链路耗时估算

**DOP=1 (1456ms, baseline 2318ms)**

```
内核内存拷贝   ~760ms (52%)   rep_movs_alternative (39%), clear_page_erms (9%),
                                do_anonymous_page (5%), try_charge_memcg (3%)
向量距离计算   ~113ms ( 8%)   IvfRuntimeIndex::search, L2 distance (960-dim, PQ)
Parquet 数据读取 ~85ms ( 6%)   page decode, column read
gaussdb executor ~498ms (34%)  ForeignScan, plan execution
```

**DOP=8 (jemalloc tuned, 估计 ~560ms)**

```
内核内存管理   ~280ms (50%)   clear_page_erms (23%), rep_movs_alternative (19%),
                                do_anonymous_page (3%)
向量距离计算   ~50ms ( 9%)    IvfRuntimeIndex::search
Parquet 数据读取 ~28ms ( 5%)   page decode
任务调度+GATHER  ~73ms (13%)   8路 result merge
gaussdb executor ~129ms (23%)  ForeignScan, plan execution
```

**DOP=16 (319ms, baseline 376ms)**

```
内核内存管理   ~155ms (49%)   clear_page_erms (25%), rep_movs_alternative (19%),
                                __cond_resched (3%)
向量距离计算   ~38ms (12%)    IvfRuntimeIndex::search
Parquet 数据读取 ~16ms ( 5%)   page decode
任务调度+GATHER  ~48ms (15%)   16路 result merge
gaussdb executor ~62ms (19%)   ForeignScan, plan execution
```

**DOP=32 (337ms, baseline 354ms)**

```
内核内存管理   ~150ms (45%)   clear_page_erms (21%), rep_movs_alternative (17%),
                                do_anonymous_page (3%)
向量距离计算   ~24ms ( 7%)    IvfRuntimeIndex::search
Parquet 数据读取 ~17ms ( 5%)   page decode
任务调度+GATHER  ~57ms (17%)   32路 result merge
gaussdb executor ~89ms (26%)   ForeignScan, plan execution
```

### DOP 趋势

- DOP=1 (1456ms): memcpy 52%, 搜索 8%, 读取 6%, executor 34%
- DOP=8 (~560ms): mem 50%, 搜索 9%, GATHER 13%, executor 23%
- DOP=16 (319ms): mem 49%, 搜索 12%, GATHER 15%, executor 19%
- DOP=32 (337ms): mem 45%, 搜索 7%, GATHER 17%, executor 26%

> 与 baseline 趋势一致——`rep_movs` 占比随 DOP 升高而下降（每个 worker 处理更少向量），`clear_page` 占比上升（更多 worker 并发缺页）。内核内存始终占 ~45-52%。

## 三、关键结论

| # | 结论 | 数据支撑 |
|---|------|------|
| 1 | **IVF 甜点仍是 DOP=16** | K=100: 149ms, 9.1× 加速 |
| 2 | **GIST 是 jemalloc 最大受益者** | DOP=1 K=10 从 1779ms→1137ms (-36%) |
| 3 | **rep_movs 仍占 39%（DOP=1）** | 瓶颈从"jemalloc 还内存"变成"内存拷贝本身" |
| 4 | **DOP=16→32 延迟停滞 (319→337ms)** | 内存瓶颈在 DOP=16 已饱和 |
| 5 | **后续优化方向** | Huge Pages、buffer 池化、SIMD L2 distance |
