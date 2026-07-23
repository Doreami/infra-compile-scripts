# jemalloc 调优验证报告

> 2026-07-25，基于 [iceberg-rust-bridge#96](https://github.com/DataInfraLab/iceberg-rust-bridge/issues/96)

## 一、问题发现

SIFT/GIST 并行 DOP 测试中，火焰图显示内核内存管理函数占比异常高：

| 函数 | SIFT DOP=8 | GIST DOP=1 | 说明 |
|------|:---:|:---:|------|
| `rep_movs_alternative` | 11.9% | **39.3%** | 内核批量内存拷贝 (x86 `rep movs`) |
| `clear_page_erms` | 5.1% | 8.0% | 物理页面清零（缺页时触发） |
| `native_queued_spin_lock_slowpath` | — | DOP=16 时 4.1% | 自旋锁（mmap_lock 争用） |

Issue #96 指出根因：jemalloc 将空闲内存归还 OS（madvise/munmap），触发 TLB shootdown IPI 横扫所有核 + 重复缺页。IPC 低至 0.21，每秒 175 万次缺页。

## 二、优化手段

### 已实施：jemalloc 参数调优

在 gaussdb 启动前设置环境变量：

```bash
export MALLOC_CONF="retain:true"
```

`retain:true` 阻止 jemalloc 将空闲内存归还 OS，从源头消除 TLB shootdown + 重复缺页。零代码修改、零风险、可立即部署。

### 效果对比

**GIST（960 维，内存压力最大）**

| 场景 | 调优前 | 调优后 | 改善 |
|------|------:|------:|:---:|
| K=10 DOP=1 | 1779ms | **1137ms** | **-36%** |
| K=100 DOP=1 | 2229ms | **1352ms** | **-39%** |
| K=10000 DOP=1 | 2318ms | **1456ms** | **-37%** |
| K=10 DOP=16 | 193ms | **147ms** | **-24%** |
| K=100 DOP=16 | 199ms | **149ms** | **-25%** |
| K=10000 DOP=16 | 376ms | **319ms** | **-15%** |

**SIFT（128 维）**

| 场景 | 调优前 | 调优后 | 改善 |
|------|------:|------:|:---:|
| K=10 DOP=1 | 204ms | **169ms** | **-17%** |
| K=100 DOP=1 | 240ms | **201ms** | **-16%** |
| K=10000 DOP=1 | 278ms | **231ms** | **-17%** |
| K=10 DOP=16 | 51ms | **40ms** | **-22%** |
| K=100 DOP=16 | 52ms | **42ms** | **-19%** |
| K=10000 DOP=16 | 89ms | **75ms** | **-16%** |

> GIST 受益远超 SIFT（36% vs 17%）：960 维向量 (3840B) 内存压力是 SIFT (512B) 的 7.5×，TLB shootdown 影响更大。

### 火焰图对比

调优前后 `rep_movs`、`clear_page` 等热点函数**占比几乎不变**，但总时间缩短。原因：jemalloc 调优消除的是函数之间的 TLB shootdown 中断（CPU 被 TLB 刷新打断的隐藏开销），而不是函数本身的拷贝工作。

| 函数 | GIST DOP=1 调优前 (2318ms) | GIST DOP=1 调优后 (1456ms) |
|------|:---:|:---:|
| `rep_movs_alternative` | 39.3% | 39.2% |
| `clear_page_erms` | 8.0% | 8.5% |
| `do_anonymous_page` | 4.8% | 4.5% |

### perf stat 缺页统计（干净重启，等温后单次查询）

| 指标 | Baseline | Jemalloc Tuned | 变化 |
|------|------:|------:|:---:|
| SIFT K=10000 DOP=1 | 16,226 faults | **6,919** | **-57%** |
| GIST K=10000 DOP=1 | 32,545 faults | **12,312** | **-62%** |

> `retain:true` 将单次查询的缺页减半。这些剩余缺页来自 `alloc → 首次 touch → page fault` 路径，对应 `[kernel.kallsyms] clear_page_erms` + `[kernel.kallsyms] do_anonymous_page`。jemalloc 管不到首次分配，根治方向是 Huge Pages 或 buffer 池化。

### 与 Issue #96 的差异

Issue #96 报告 c64 并发 QPS 提升 5×，我们测的单查询延迟改善 17-37%：

| 维度 | Issue #96 | 本次 |
|------|------|------|
| 瓶颈 | `mmap_lock` 序列化 (`down_read_trylock` 50%) | `rep_movs` 本身 (39%) |
| 受 jemalloc 影响 | 归还内存 → TLB shootdown → mmap_lock 争用 | 归还内存 → TLB 中断 → 函数间隐藏延迟 |
| 改善程度 | QPS 5×（消除锁瓶颈） | 延迟 -17~37%（消除中断） |

我们的场景下 `mmap_lock` 本身不是瓶颈（DOP=1 单 worker 无争用，DOP=16 只有 4% spin_lock），所以改善幅度小于 Issue #96。

## 三、剩余瓶颈

jemalloc 调优后，`rep_movs_alternative` 仍是 GIST DOP=1 的 **39%** 热点。根因是向量物化路径的 **alloc → memcpy → free** 循环：

1. Arrow/Parquet 解码产生临时 vector batch
2. L2 距离计算需要将 3840B 向量拷贝到连续内存
3. jemalloc 分配 → 使用 → 释放（即使不归还 OS，本地 arena 内仍有开销）

## 四、后续优化方案

| 优先级 | 方案 | 预期收益 | 说明 |
|:---:|------|:---:|------|
| P0 | **`MALLOC_CONF=retain:true`** | GIST -36%，SIFT -17% | ✅ 已实施，建议固化到启动脚本 |
| P1 | **Huge Pages (2MB)** | -15~25% | 减少 3840B 向量跨页 fault（当前每个向量跨 2 个 4K 页，2MB 大页消除跨页） |
| P1 | **物化路径 buffer 池化** | -20~30% | Arrow builder、列缓冲、stream arena 复用，降 alloc/free 频率 1-2 个数量级 |
| P2 | **SIMD L2 distance (AVX-512)** | -15~25% | 加速 960 维距离计算，8 个 float/指令 vs 当前 4 个 |
| P2 | **jemalloc `background_thread:true`** | -5~10% | 后台线程处理 purge，避免阻塞业务线程 |
| P3 | **零拷贝物化** | -30~50% | stream 直接引用解码缓冲，消除逐 batch memcpy（长期架构改造） |

### 优先级说明

- **P0** 零代码、零风险、立即生效
- **P1** 是当前瓶颈 (`rep_movs` + `clear_page`) 的直接对症方案
- **P2** 是计算加速，SIFT 收益小（128 维）、GIST 收益大（960 维）
- **P3** 需要物化路径重构，短期不可行

### 火焰图文件

```
flamegraphs/
├── baseline/          (14 SVG) — 默认 jemalloc 配置
└── jemalloc_tuned/    (14 SVG) — retain:true 调优后
```
