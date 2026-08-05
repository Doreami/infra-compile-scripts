# Btree 索引性能测试报告

> 测试日期：2026-08-04 ~ 2026-08-05

| 项目      | 配置                                   |
| ------- | ------------------------------------ |
| OS      | openEuler 24.03                      |
| CPU     | 96 核                                 |
| 内存      | 723 GB                               |
| 存储      | NVMe 本地磁盘 (30TB, /data/xl/warehouse) |
| gaussdb | 单实例, 端口 37000                        |

---

## 一、测试环境与表信息

### 1.1 被测表

| 数据集       | 命名 | 维度 | 行数 | 分区 | 文件数 | ID 分布 |
| --------- | ---- | ---- | ----- | --- | --- | ------- |
| SIFT | `sift1m` | 128 | 100万 | bucket[32] | 32 | 顺序 |
| GIST | `gist1m` | 960 | 100万 | bucket[32] | 32 | 顺序 |
| DEEP-1B | `deep1b` | 96 | 10亿 | bucket[32] | 1024 | 顺序 |
| Synth | `synth2048_10m` | 2048 | 1000万 | 无 | 160 | 顺序 |
| Synth-细 | `synth2048_10m_fine` | 2048 | 1000万 | 无 | 2000 | 顺序 |
| Synth-RR-200 | `synth2048_1M_rr` | 2048 | 100万 | 无 | 200 | **轮询打散** |
| Synth-RR-2000 | `synth2048_2M_rr` | 2048 | 200万 | 无 | 2000 | **轮询打散** |

> **Round-Robin**：ID 按轮询分配（行 i → 文件 `i % N_FILES`），每个文件的 id 范围覆盖 ~全表，Parquet min/max statistics 无法跳过任何文件——FullScan 最不利场景。

### 1.2 查询方式

```sql
-- FullScan（无 btree 索引）
SELECT id FROM <tbl> WHERE id = <N>;

-- Btree（索引存在时自动走，无法通过 GUC 关闭）
SELECT id FROM <tbl> WHERE id = <N>;
```

---

## 二、延迟对比

> 预热后 3 轮取均值，均为 `SELECT id`。

| 表 | 文件 | ID 分布 | FullScan | Btree | 差值 | 赢家 |
|----|------|---------|----------|-------|------|------|
| SIFT | 32 | 顺序 | 39ms | 46ms | +7ms | FS 1.2x |
| GIST | 32 | 顺序 | 79ms | 92ms | +13ms | FS 1.2x |
| DEEP | 1024 | 顺序 | 261ms | 976ms | +715ms | **FS 3.7x** |
| Synth | 160 | 顺序 | 105ms | 105ms | 0 | 持平 |
| Synth-细 | 2000 | 顺序 | 1,750ms | 5,888ms | +4,138ms | **FS 3.4x** |
| Synth-RR-200 | 200 | 打散 | 323ms | 438ms | +115ms | **FS 1.4x** |
| Synth-RR-2000 | 2000 | 打散 | 1,794ms | 5,928ms | +4,134ms | **FS 3.3x** |

### 关键发现

1. **btree 全场景全败**。在所有数据组织下，btree 从未赢过 FullScan。

2. **差值不是固定的**。从 200 文件到 2000 文件，btree 多出的开销从 +115ms 涨到 +4,134ms（增长 36x），说明 btree 路径中也有随文件数线性增长的组件——不是简单的"固定 metadata 开销"。

3. **分区裁剪直接消灭 btree 需求**。`id_bucket[32]` 分区将点查限制在 1/32 文件内，32 文件 39ms 已足够快。

---

## 三、火焰图分析

> 采集方式：循环执行查询（6 次 FullScan / 2 次 Btree）在 15s 窗口内，覆盖率 ~72%（FS）/ ~79%（BT）。
> 数据来源：Synth-RR-2000（2000 文件，Round-Robin），perf record -g。

### 3.1 FullScan 热点

| 占比 | 热点 | 说明 |
|------|------|------|
| 9.2% | `regex_lite::PikeVM` | 正则引擎 |
| 11.8% | `malloc/free/jemalloc` | 内存分配 |
| 6.0% | `asm_exc_page_fault` | 缺页 |

### 3.2 Btree 热点

| 占比 | 热点 | 说明 | vs FS |
|------|------|------|-------|
| 10.7% | `regex_lite::PikeVM` | 正则引擎 | +1.5% |
| 14.7% | `malloc/free/jemalloc` | 内存分配 | +2.9% |
| 3.8% | `asm_exc_page_fault` | 缺页 | -2.2% |
| 1.6% | `core::str::from_utf8` | 字符串转换 | **新增** |
| 1.2% | `SipHash` | 哈希计算 | **新增** |
| 2.0% | `serde_json` | JSON 反序列化 | **新增** |
| 2.1% | `String/BTreeMap::clone` | Clone 开销 | **新增** |

### 3.3 对比分析

- **CPU 采样总量相近**（FS 47K vs BT 52K），说明 CPU 开销差异不大。
- **墙钟差距 3.3x** 主要来自 **I/O 等待**——btree 多读了 btree 索引文件 + 二次 iceberg metadata。
- **regex_lite 两者都有**（FS 9.2% vs BT 10.7%）——这是 iceberg manifest 路径解析，FullScan 也需要。
- **serde_json + from_utf8 + SipHash + clone 是 btree 独有**（合计 ~6.9%），来自 `plan_index_files → plan_snapshot` 的二次 metadata 加载。

### 3.4 火焰图文件

所有火焰图均已保存至 `flamegraphs/` 目录。下图文件均互为 FullScan↔Btree 对照。

| 文件 | 表 | 文件数 | ID 分布 | 场景 | 查询 | 延迟 | 采集方式 |
|------|-----|--------|---------|------|------|------|----------|
| `flame_synth_160_fs.svg` | Synth | 160 | 顺序 | FullScan | `WHERE id=5000000` | 105ms | 循环 ~100 次 |
| `flame_synth_160_bt.svg` | Synth | 160 | 顺序 | Btree | `WHERE id=5000000` | 105ms | 循环 ~100 次 |
| `flame_synth_fine_fs.svg` | Synth-细 | 2000 | 顺序 | FullScan | `WHERE id=5000000` | 1,750ms | 循环 6 次 |
| `flame_synth_fine_bt.svg` | Synth-细 | 2000 | 顺序 | Btree | `WHERE id=5000000` | 5,888ms | 循环 2 次 |
| `flame_fs_2M_rr.svg` | Synth-RR-2000 | 2000 | 打散 | FullScan | `WHERE id=1000000` | 1,794ms | 循环 6 次 |
| `flame_bt_2M_rr.svg` | Synth-RR-2000 | 2000 | 打散 | Btree | `WHERE id=1000000` | 5,928ms | 循环 2 次 |
| `flame_deep_fs.svg` | DEEP-1B | 1024 | 顺序 | FullScan | `WHERE id=500000000` | 261ms | 循环 40 次 |
| `flame_deep_bt.svg` | DEEP-1B | 1024 | 顺序 | Btree | `WHERE id=500000000` | 976ms | 循环 12 次 |
| `flame_btree_synth_2000files.svg` | Synth-细 | 2000 | 顺序 | Btree | `WHERE id=5000000` | 5,888ms | 单次, perf 8s（仅供参考） |

> **采集方式**：`perf record -g -p <gaussdb_pid>` 采样 15 秒，其间循环执行查询。火焰图展示的是采样窗口内所有查询迭代的 CPU 热点聚合（非平均，无需平均——perf 是统计采样，迭代次数越多覆盖率越高，热点分布越准确）。
>
> **延迟数据**：表中"延迟"列为同条件独立测量（预热后 3 轮 `date +%s%3N` 墙钟取均值），与火焰图采集无关。
>
> **对照说明**：同名 `_fs` / `_bt` 后缀文件互为 FullScan↔Btree A/B 对照。
>
> SIFT / GIST 未采集火焰图：延迟在 ~40-90ms 级别，查询时间太短难以通过 perf 循环覆盖。
>
> `flame_btree_synth_2000files.svg` 为早期单次采集，覆盖率仅 ~5%，热点分布与 `flame_synth_fine_bt.svg` 一致但噪声更大。

---

## 四、调用链分析

> 通过 `perf script` + FlameGraph 折叠堆栈，确认关键调用路径。

### Btree 额外调用链

```
build_hybrid_arrow_stream
  └─ hybrid_real_index_streams
       └─ plan_index_files_by_metadata
            └─ plan_index_files
                 ├─ load_registry        → serde_json (JSON 解析)
                 └─ plan_snapshot        → iceberg SchemaVisitor
                                           └─ avro schema parse
                                                └─ serde_json::MapHelper
```

### FullScan 调用链

```
build_task_group_arrow_stream
  └─ replan_and_match_tasks
       └─ table_scan (iceberg SDK)       → predicate pushdown
```

**根因**：Btree 比 FullScan 多走了一次 `plan_snapshot`。这个调用为每个文件条目解析 Avro manifest，随文件数线性增长。200 文件时额外 +115ms，2000 文件时 +4,134ms。即使 btree 索引查找本身只需要 O(log n)，上层 metadata 处理的 O(files) 开销把它完全淹没。

---

## 五、优化方向

### 5.1 近期可行

| 方向 | 内容 | 预期收益 |
|------|------|:--:|
| **消除二次 plan_snapshot** | `replan_and_match_tasks` 已读了一次 metadata，`plan_index_files` 不要再读——共用结果 | **高** |
| **btree 独立执行路径** | 不走 hybrid task_group，直接用 `search_scalar` 返回的行地址读 Parquet，绕过 per-file task dispatch | **高** |
| **covering index** | `SELECT id` 直接从 btree page 返回 key，不读 Parquet | **中**（仅同列查询） |
| **FxHash 替换 SipHash** | `plan_index_files` 中的 HashMap 改用 FxHash | 低（~1%） |
| **registry 缓存** | `load_registry` 结果在查询内缓存，不每次 JSON 解析 | 低~中 |

### 5.2 需要架构调整

| 方向 | 内容 |
|------|------|
| **LanceDB 式二级索引** | btree 输出直接是文件级 row address，不经过 iceberg metadata 层 |
| **索引覆盖文件** | 索引数据不存单独文件，嵌入 Parquet footer——消除索引文件 I/O |

---

## 六、SELECT * 回表开销

| 表 | BT `SELECT id` | BT `SELECT *` | 向量大小 | 增量 |
|----|---------------|--------------|---------|------|
| SIFT | 46ms | 46ms | 512B | 0% |
| GIST | 92ms | 92ms | 3.8KB | 0% |
| DEEP | 976ms | 976ms | 384B | 0% |
| Synth | 105ms | 690ms | 8KB | +560% |

> 高维向量回表开销显著，但与 btree 无关——FullScan `SELECT *` 同样要读向量列。

---

## 七、结论

1. **btree 当前实现全场景无收益**。根因是 hybrid task_group 路径中的二次 `plan_snapshot` 调用，其 Manifest 解析开销随文件数线性增长，完全覆盖了 btree 索引查找的 O(log n) 优势。

2. **FullScan 已足够高效**。分区裁剪（id_bucket）+ predicate pushdown 组合在 32~2000 文件范围内均达到实用水平。

3. **btree 设计的正确方向**：`id → (file, row)` 映射、BTreeLookup + page 加载都是正确的。需要的是去掉上层的 per-file metadata 重读，给 btree 一条独立于 hybrid task_group 的执行路径。

4. **火焰图确认**：CPU 热点的差异（+serde_json / +SipHash / +clone）来自二次 metadata 加载，而墙钟时间的差距（3.3x）主要来自额外的 I/O 等待。两项都指向同一个优化点：**消除重复的 plan_snapshot，让 btree 直连 parquet 回表**。
