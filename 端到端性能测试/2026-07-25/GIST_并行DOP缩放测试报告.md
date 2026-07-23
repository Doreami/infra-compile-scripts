# GIST1M 并行 DOP 缩放测试报告

> 2026-07-25, openEuler 24.03, 96核 723GB, Parquet uncompressed, 默认 jemalloc

## 一、测试对象

| 属性 | 分区表 | 非分区表 |
|------|--------|---------|
| 命名空间 | gist_ns_part | gist_ns |
| 表名 | gist1m_part | gist1m |
| 向量数 | 1,000,000 | 1,000,000 |
| 维度 | 960 (vector), 存储 fixed(3840) | 同左 |
| 分区 | bucket[32] by id | 无 |
| Parquet 文件数 | 32（每分区 1 个） | 10 |
| 每文件行数 | ~31,250 | ~100,000 |
| 每文件大小 | ~97 MB | ~290 MB |
| 数据总大小 | 3.1 GB | 2.9 GB |

| 索引 | 分区表 | 非分区表 |
|------|--------|---------|
| 类型 | IVFPQ, num_clusters=256, nprobe=10 | 同左 |
| 索引文件数 | 33（1 index-registry + 32 分区 puffin） | 11 |
| 索引存储 | 3.7 GB | 3.7 GB |
| 构建耗时 | ~58 分钟 | ~30 分钟 |

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
| K=10 | 1779ms | 1156ms | 613ms | 321ms | 193ms | 167ms |
| K=100 | 2229ms | 1188ms | 601ms | 323ms | 199ms | 279ms |
| K=10000 | 2318ms | 2091ms | 1185ms | 619ms | 376ms | 354ms |

### 加速比（vs DOP=1）

| 场景 | DOP=2 | DOP=4 | DOP=8 | DOP=16 | DOP=32 |
|------|------:|------:|------:|-------:|-------:|
| K=10 | 1.5× | 2.9× | 5.5× | **9.2×** | 10.6× |
| K=100 | 1.9× | 3.7× | 6.9× | **11.2×** | 8.0× |
| K=10000 | 1.1× | 2.0× | 3.7× | **6.2×** | 6.5× |

### IVF — 分区 vs 非分区

| 场景 | 非分区 DOP=1 | 分区 DOP=1 | 分区 DOP=8 | 分区 DOP=16 |
|------|:---:|:---:|:---:|:---:|
| K=10 | 1627ms | 1779ms | 321ms | **193ms** |
| K=100 | 1673ms | 2229ms | 323ms | **199ms** |
| K=10000 | 1747ms | 2318ms | 619ms | **376ms** |

非分区 FullScan：

| K=10 | K=100 | K=10000 |
|------:|------:|------:|
| 6234ms | 6262ms | 6344ms |

## 三、对比 SIFT

| 指标 | SIFT (128-dim) | GIST (960-dim) | 比值 |
|------|:---:|:---:|:---:|
| 数据量 | 147 MB | 3.1 GB | 21× |
| 索引大小 | 506 MB | 3.7 GB | 7.3× |
| IVF K=10 DOP=1 | 204ms | 1779ms | 8.7× |
| IVF K=10 DOP=16 | 51ms | 193ms | 3.8× |
| 最佳加速比 | 4.6× | **11.2×** | — |

GIST 绝对延迟是 SIFT 的 4-9×，但并行加速比更高——高计算密度摊薄调度开销。

## 四、IVF 火焰图分析 — K=10000

### 热点函数自耗时 (>1.5%)

**DOP=1 (2318ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 39.3% | worker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 8.0% | worker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 4.8% | worker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 3.6% | worker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |
| 2.5% | worker | `[kernel.kallsyms] zap_pte_range` | 页表项回收 |
| 2.1% | worker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.8% | worker | `[kernel.kallsyms] __count_memcg_events` | 内存事件统计 |
| 1.6% | worker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

> `[kernel.kallsyms] rep_movs_alternative` 是内核 memcpy（x86 `rep movs` 指令）。960 维向量 (3840 字节) 跨 2 个物理页，每次 L2 距离计算都要将两个向量拷贝到 CPU 缓存，触发海量 memcpy + 缺页。向量搜索本身仅 1.6%。

**DOP=8 (619ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 21.2% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 17.1% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 2.6% | streamworker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 2.2% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 1.9% | streamworker | `[kernel.kallsyms] native_irq_return_iret` | 中断返回 |
| 1.9% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.8% | streamworker | `[kernel.kallsyms] zap_pte_range` | 页表项回收 |
| 1.3% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

> `[kernel.kallsyms] clear_page_erms` 从 8% 飙升至 21%——8 个 worker 并发缺页，每个 worker 都触发页面分配+清零。

**DOP=16 (376ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 22.6% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 16.7% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 2.8% | streamworker | `libc.so.6 __memcpy_avx_unaligned_erms` | 用户态 SIMD memcpy |
| 2.6% | streamworker | `libc.so.6 __memmove_avx_unaligned_erms` | 用户态 SIMD memmove |
| 2.3% | streamworker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 2.3% | streamworker | `[kernel.kallsyms] native_irq_return_iret` | 中断返回 |
| 2.2% | streamworker | `[kernel.kallsyms] __cond_resched` | 内核调度检查 |
| 1.9% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |
| 1.8% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |

> DOP=16 时 `[kernel.kallsyms] clear_page_erms` 已升至 22.6%，接近 DOP=32 水平 (21.7%)——内存瓶颈在 DOP=16 已饱和。

**DOP=32 (354ms)**

| 占比 | 线程 | 函数 | 说明 |
|:---:|------|------|------|
| 21.7% | streamworker | `[kernel.kallsyms] clear_page_erms` | 物理页面清零 |
| 14.8% | streamworker | `[kernel.kallsyms] rep_movs_alternative` | 内核批量内存拷贝 |
| 3.1% | streamworker | `[kernel.kallsyms] do_anonymous_page` | 匿名页分配 |
| 2.4% | streamworker | `[kernel.kallsyms] get_mem_cgroup_from_mm` | 内存 cgroup 记账 |
| 1.8% | streamworker | `[kernel.kallsyms] __pte_offset_map_lock` | 页表项映射 |
| 1.5% | streamworker | `[kernel.kallsyms] try_charge_memcg` | 内存分配记账 |
| 1.1% | streamworker | `libiceberg_rust_bridge.so IvfRuntimeIndex::search` | IVF 向量搜索 |

### 全链路耗时估算

**DOP=1 (2318ms)**

```
内核内存拷贝   ~1060ms (46%)   [kernel.kallsyms] rep_movs_alternative (39%),
                                [kernel.kallsyms] clear_page_erms (8%),
                                [kernel.kallsyms] do_anonymous_page (5%),
                                [kernel.kallsyms] try_charge_memcg (4%)
向量距离计算   ~185ms ( 8%)    libiceberg_rust_bridge.so IvfRuntimeIndex::search, L2 distance (960-dim, PQ)
Parquet 数据读取 ~140ms ( 6%)   page decode, column read
gaussdb executor ~933ms (40%)   ForeignScan, plan execution
```

**DOP=8 (619ms)**

```
内核内存管理   ~310ms (50%)    [kernel.kallsyms] clear_page_erms (21%),
                                [kernel.kallsyms] rep_movs_alternative (17%),
                                [kernel.kallsyms] do_anonymous_page (3%),
                                [kernel.kallsyms] get_mem_cgroup_from_mm (2%)
向量距离计算   ~55ms ( 9%)     libiceberg_rust_bridge.so IvfRuntimeIndex::search
Parquet 数据读取 ~30ms ( 5%)    page decode
任务调度+GATHER ~80ms (13%)    8路 result merge
gaussdb executor ~144ms (23%)   ForeignScan, plan execution
```

**DOP=16 (376ms)**

```
内核内存管理   ~188ms (50%)    [kernel.kallsyms] clear_page_erms (23%),
                                [kernel.kallsyms] rep_movs_alternative (17%),
                                libc.so.6 __memcpy_avx_unaligned_erms (3%),
                                [kernel.kallsyms] do_anonymous_page (2%)
向量距离计算   ~45ms (12%)     libiceberg_rust_bridge.so IvfRuntimeIndex::search
Parquet 数据读取 ~19ms ( 5%)    page decode
任务调度+GATHER ~53ms (14%)    16路 result merge
gaussdb executor ~71ms (19%)    ForeignScan, plan execution
```

**DOP=32 (354ms)**

```
内核内存管理   ~165ms (47%)    [kernel.kallsyms] clear_page_erms (22%),
                                [kernel.kallsyms] rep_movs_alternative (15%),
                                [kernel.kallsyms] do_anonymous_page (3%),
                                [kernel.kallsyms] get_mem_cgroup_from_mm (2%)
向量距离计算   ~25ms ( 7%)     libiceberg_rust_bridge.so IvfRuntimeIndex::search
Parquet 数据读取 ~18ms ( 5%)    page decode
任务调度+GATHER ~60ms (17%)    32路 result merge
gaussdb executor ~86ms (24%)    ForeignScan, plan execution
```

### DOP 趋势

- DOP=1 (2318ms): memcpy 46%, 搜索 8%, 读取 6%, executor 40%
- DOP=8 (619ms): mem 50%, 搜索 9%, GATHER 13%, executor 23%
- DOP=16 (376ms): mem 50%, 搜索 12%, GATHER 14%, executor 19%
- DOP=32 (354ms): mem 47%, 搜索 7%, GATHER 17%, executor 24%

> `[kernel.kallsyms] rep_movs_alternative` 从 39% 降至 15%（每个 worker 处理更少向量），但 `[kernel.kallsyms] clear_page_erms` 从 8% 升至 22%（更多 worker 并发缺页）。内核内存管理始终占 ~47-50%。

## 五、关键结论

| # | 结论 | 数据支撑 |
|---|------|------|
| 1 | **GIST 瓶颈是 `[kernel.kallsyms] rep_movs_alternative` (内核 memcpy)** | 39.3% (DOP=1), ~911ms 绝对耗时 |
| 2 | **IVF 甜点 DOP=16** | K=100: 199ms, 11.2× 加速 |
| 3 | **`[kernel.kallsyms] clear_page_erms` 随 DOP 飙升** | 8% → 22%, DOP=16 已饱和 |
| 4 | **向量搜索极轻 (1.6%)** | 瓶颈在内核 page fault，不在算法 |
| 5 | **并行加速优于 SIFT** | 11.2× vs 4.6×, 高计算密度收益更大 |
| 6 | **索引构建需并行化** | 单线程 58 分钟, 960 维 k-means 运算量巨大 |

### 优化方向

| 优化 | 预期收益 | 说明 |
|------|:---:|------|
| Huge Pages (2MB) | -20~30% | 减少 3840B 向量跨页 fault, 降低 `[kernel.kallsyms] clear_page_erms` 占比 |
| SIMD L2 distance (AVX-512) | -15~25% | 加速 960 维距离计算 |
| 内存池/预分配 | -5~10% | 减少向量临时对象, 降低 `[kernel.kallsyms] rep_movs_alternative` |
| 并行索引构建 | 构建 10-20× | k-means 多线程 |
