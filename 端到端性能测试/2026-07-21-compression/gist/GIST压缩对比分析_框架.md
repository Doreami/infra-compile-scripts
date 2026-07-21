# GIST1M 压缩对比 & 火焰图优化分析 — 2026-07-21

**测试环境**: openEuler 24.03, 96核, 723GB
**数据**: GIST1M (960-dim), fixed(3840), 非分区, IVFPQ nc=256

---

## 一、测试方案

| 维度 | 取值 |
|------|------|
| 查询方式 | IVF 索引扫, FullScan 全表扫 |
| query_dop | 1（串行，非分区表） |
| K 值 | 10, 100, 10000 |
| 压缩 | zstd vs uncompressed |
| 轮次 | 5 轮取平均（1 轮预热） |

**两张对比表**：

| 表 | 压缩 | 预期大小 | 索引 |
|---|------|------|:--:|
| `gist_ns.gist1m_zstd` | zstd | ~1.4GB | idx_ivf_pq_vec |
| `gist_ns.gist1m_none` | uncompressed | ~3.6GB | idx_ivf_pq_vec |

---

## 二、存储对比

| 压缩 | 大小 | vs Zstd | vs SIFT |
|------|------|:--:|:--:|
| zstd | __TODO__ | 基准 | __× vs SIFT 147MB__ |
| uncompressed | 3.6GB | +__TODO__% | __× vs SIFT 500MB__ |

> GIST 960 维 × 1M 行 = 3.84GB 原始 float 数据。与 SIFT (500MB) 相比，数据量 7.5×。

---

## 三、延迟对比

### IVF 索引扫

```
  K       zstd       none      加速比
  ────────────────────────────────────
  K=10     __TODO__   __TODO__   __TODO__
  K=100    __TODO__   __TODO__   __TODO__
  K=10000   __TODO__   __TODO__   __TODO__
```

### FullScan 全表扫

```
  K       zstd       none      加速比
  ────────────────────────────────────
  K=10     __TODO__   __TODO__   __TODO__
```

### 与 SIFT 对比

| 指标 | SIFT (128d, 500MB) | GIST (960d, 3.84GB) | 倍数 |
|------|:--:|:--:|:--:|
| IVF K=10 zstd | 4529ms | __TODO__ | __×__ |
| IVF K=10 none | 3934ms | __TODO__ | __×__ |
| none vs zstd 收益 | -13.1% | __TODO__ | — |
| 数据量 | 147MB(zstd) | __TODO__ | __×__ |

---

## 四、火焰图热点对比

### IVF K=10 热点函数（无压缩）

| # | 占比 | 组件 | 函数 | 分类 |
|:--:|:--:|------|------|------|
| 1 | __TODO__ | __TODO__ | __TODO__ | __TODO__ |
| 2 | __TODO__ | __TODO__ | __TODO__ | __TODO__ |
| 3 | __TODO__ | __TODO__ | __TODO__ | __TODO__ |
| 4 | __TODO__ | __TODO__ | __TODO__ | __TODO__ |
| 5 | __TODO__ | __TODO__ | __TODO__ | __TODO__ |

### Zstd vs 无压缩热点对比

| 热点函数 | Zstd 占比 | 无压缩占比 | 变化 |
|------|:--:|:--:|:--:|
| `ZSTD_decompressSequences` | __TODO__ | 0% | 消除 |
| __TODO__ | __TODO__ | __TODO__ | __TODO__ |
| kernel [unknown] | __TODO__ | __TODO__ | __TODO__ |

---

## 五、高维 vs 低维对比

对比 960 维和 128 维在消除压缩后的 CPU 分布差异：

| 模块 | SIFT (128d) | GIST (960d) | 差异分析 |
|------|:--:|:--:|------|
| Parquet 解码 | ~6% | __TODO__ | 高维数据列更长，解码占比应更高 |
| gaussdb infra | ~16% | __TODO__ | 与维度无关，占比应接近 |
| bridge tokio | ~5% | __TODO__ | 与维度无关 |
| kernel (缺页/拷贝) | ~12% | __TODO__ | **960 维内存压力 7.5×**，预计显著升高 |
| 向量距离计算 | <1% | __TODO__ | 维度 7.5× → 距离计算耗时也应增长 |

### 关键假设验证

1. **高维场景压缩是否更重要？**
   - SIFT: zstd→none +13% 延时改善
   - GIST 数据量 7.5× → 压缩开销绝对量更大 → 消除收益 **预计更大**
   
2. **高维场景内核占比是否更突出？**
   - SIFT: kernel ~12%
   - GIST: 数据量 7.5× → 缺页频率更高 → kernel 占比 **预计 >20%**
   - 这会让 CPU 优化（列裁剪、消 tokio）的收益被内核稀释

3. **FullScan 下压缩消除收益是否更明显？**
   - SIFT FullScan 100 万行全量解码 → 消除 Zstd 非常明显
   - GIST FullScan 100 万行 × 3840B/行 = 3.84GB → Zstd 解压 3.84GB 的 CPU 开销巨大
   - **预计 GIST FullScan 压缩消除收益远超 IVF**

---

## 六、与 SIFT 对比：后续优化优先级变化

基于高维数据的可能特征，优化路径可能需要调整：

| 优化项 | SIFT 预估收益 | GIST 预估变化 | 依据 |
|--------|:--:|:--:|------|
| 列裁剪 (P0) | -43~52% | **更大？** | 每行 3840B→8B，节省比例 99.8% vs SIFT 98.5% |
| SipHash→FxHash | -3% | 不变 | HashMap 操作与维度无关 |
| 消 tokio | -5% | **更小（被内核稀释）** | 高维内核占比高，tokio 相对占比下降 |
| Parquet 压缩消除 | -10~18% | **更大？** | 3.84GB vs 500MB，解压绝对开销大 |

---

## 七、火焰图文件

| 文件 | 说明 |
|------|------|
| `flame_gist_zstd_ivf_k10.svg` | GIST Zstd, K=10, DOP=1 |
| `flame_gist_zstd_ivf_k100.svg` | GIST Zstd, K=100 |
| `flame_gist_zstd_fullscan_k10.svg` | GIST Zstd, FullScan K=10 |
| `flame_gist_none_ivf_k10.svg` | GIST 无压缩, K=10 |
| `flame_gist_none_ivf_k100.svg` | GIST 无压缩, K=100 |
| `flame_gist_none_fullscan_k10.svg` | GIST 无压缩, FullScan K=10 |

---

## 八、原始数据

| 压缩 | bench log | flame log |
|------|------|------|
| zstd | `~/gist_bench_zstd.log` | `~/gist_flame_zstd.log` |
| none | `~/gist_bench_none.log` | `~/gist_flame_none.log` |

---

## 九、结论与建议

__TODO__ (填入测试数据后完成)
