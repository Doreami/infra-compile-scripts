# 向量+标量混合查询方案A（Pre-filter）设计文档

> 最后更新: 2026-08-19
> 状态: 待实现

## 一、方案概述

### 1.1 背景

现有系统已实现：
- **标量多列索引**：BTree复合索引，支持多列条件的前缀定位和页内多列mask过滤（见[多列BTree索引设计方案.md](../getting-started/multi-column-btree.md)）
- **向量索引**：IVF-Flat/IVF-PQ向量化索引，支持Top-K相似度搜索

**问题**：缺少向量+标量混合查询的优化能力。实际查询常见组合：
```sql
-- 场景1：分类向量搜索
WHERE category = 'electronics' AND vector IS CLOSEST TO q

-- 场景2：时间范围向量搜索
WHERE created_at > '2024-01-01' AND vector IS CLOSEST TO q

-- 场景3：多条件向量搜索
WHERE user_id = 123 AND status = 'active' AND vector IS CLOSEST TO q
```

当前行为：
- 标量条件只能引擎外recheck，无法利用索引
- 向量搜索必须全表搜索，未利用标量裁剪

### 1.2 方案A核心思想：Pre-filter

```
标量索引预过滤 → 减少向量搜索候选集 → 向量搜索Top-K → 回表验证标量条件
```

**优势**：
- 标量索引精确裁剪候选集（1000倍压缩常见）
- 向量搜索量大幅减少，加速显著
- 复用现有标量索引基础设施
- 回表量极小（仅Top-K候选）

**代价**：
- 需要创建两个索引（标量BTree + 向量IVF）
- 有前缀语义限制（与多列BTree一致）
- 实现复杂度中等

---

## 二、执行流程

### 2.1 端到端流程图

```
┌─────────────────────────────────────────────────────────────┐
│  FDW层                                                         │
│    └─ 收集查询谓词                                            │
│        - scalar_col = 1                                     │
│        - vector ~ q                                         │
│    └─ 路由决策                                                │
│        - 检测到两个索引：idx_scalar (BTree), idx_vector (IVF)│
│        - 决定走HybridSearchRequest                           │
│    └─ 调用引擎                                                │
│        └─ IndexSearchCoordinator::search_hybrid_prefilter    │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  标量搜索（BTree索引）                                         │
│    ├─ Coverage规划（复用现有）                                │
│    ├─ 加载B-Tree segment                                     │
│    └─ 前缀定位 + 多列mask过滤                                  │
│        └─ 返回 Vec<RowAddress>（保守超集，如1000行）          │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  向量搜索（IVF索引，传入候选集过滤）                           │
│    ├─ 加载IVF segment                                        │
│    ├─ nprobe搜索（搜索最近邻中心）                            │
│    ├─ 在候选集中收集向量候选                                  │
│    └─ 过滤dead-row + Top-K排序                                │
│        └─ 返回 Vec<IndexCandidate>（候选集内的Top-K，如10行） │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  回表验证（并行读取标量列值）                                  │
│    ├─ 读取每个候选的 scalar_col 值                           │
│    ├─ 验证是否 = 1                                           │
│    ├─ 过滤掉不满足条件的候选                                  │
│    └─ 返回验证通过的候选                                      │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  全局排序 + 返回                                               │
│    ├─ 按距离排序                                             │
│    └─ 返回最终Top-K结果                                      │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  FDW层                                                         │
│    └─ 本地recheck（确保精确性）                               │
│    └─ 返回最终结果给用户                                      │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 详细步骤

#### 步骤1：标量搜索

```rust
let scalar_result = search_scalar(SearchScalarIndexRequest {
    table_uuid: request.table_uuid,
    snapshot_id: request.snapshot_id,
    index_name: request.scalar_index_name,
    query: request.scalar_query,  // expressions: [ScalarExpression { field_id: 1, op: Eq, value: 1 }]
    pruned_files: request.pruned_files,
}).await?;

// scalar_result.addresses = Vec<RowAddress>
// 例如：[RowAddress{file_path: "data/file1.parquet", row_position: 100},
//       RowAddress{file_path: "data/file2.parquet", row_position: 250}, ...]
```

#### 步骤2：向量搜索（带候选集过滤）

```rust
let vector_candidates = search_vector_prefiltered(
    &request.vector_query.query,    // query_vector
    request.vector_query.limit,      // k = 10
    &scalar_result.addresses,        // allowed_row_ids
    request.vector_query.score_order,
    request.vector_query.parameters,
).await?;

// vector_candidates = Vec<IndexCandidate>
// 每个候选包含：
// - row_address: RowAddress
// - distance: f64（平方L2距离）
// - is_overfetch: bool
```

#### 步骤3：回表验证

```rust
let verified_candidates = verify_scalar_conditions_parallel(
    vector_candidates,
    &request.scalar_query.expressions,
).await?;

// verified_candidates 过滤掉不满足 scalar_col = 1 的候选
```

#### 步骤4：全局排序

```rust
let mut sorted = verified_candidates;
sorted.sort_by_key(|c| c.distance);

// 返回 Top-K
return sorted.into_iter().take(k).collect();
```

---

## 三、接口设计

### 3.1 扩展 VectorSearchRequest

**文件**：`iceberg-index-core/src/runtime.rs`

```rust
/// 向量搜索请求
#[derive(Debug, Clone)]
pub struct VectorSearchRequest {
    pub query: Vec<f32>,
    pub limit: usize,
    pub score_order: ScoreOrder,
    pub parameters: serde_json::Value,
    
    /// ── 新增：标量候选集过滤 ───────────────────────────────
    /// 允许搜索的行地址集合（来自标量索引预过滤）。
    /// 如果为 None，搜索所有候选（当前行为）。
    /// 如果为 Some，只搜索这些地址，其他候选被跳过。
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,
}
```

**使用示例**：
```rust
// 无候选集过滤（当前行为）
let request = VectorSearchRequest {
    query: vec![1.0, 2.0, 3.0],
    limit: 10,
    score_order: ScoreOrder::SmallerIsBetter,
    parameters: serde_json::json!({}),
    allowed_row_ids: None,
};

// 有候选集过滤（方案A）
let candidates = search_vector_prefiltered(
    &query_vector,
    10,
    &allowed_row_ids,
    ScoreOrder::SmallerIsBetter,
    serde_json::json!({}),
).await?;
```

### 3.2 扩展 RuntimeIndex trait

**文件**：`iceberg-index-core/src/runtime.rs`

```rust
#[async_trait]
pub trait RuntimeIndex: Send + Sync {
    fn kind(&self) -> IndexKind;
    fn implementation(&self) -> &str;
    async fn prewarm(&self) -> Result<()>;
    fn statistics(&self) -> Result<RuntimeIndexStatistics>;
    fn file_paths(&self) -> Option<&[String]> {
        None
    }
    
    /// ── 新增：带标量候选集的向量搜索 ───────────────────────
    ///
    /// 在标量索引预过滤的基础上进行向量搜索，减少搜索量。
    /// 实现应在搜索前过滤掉不在 `allowed_row_ids` 中的候选。
    ///
    /// # 参数
    /// - `query`: 查询向量
    /// - `limit`: 返回结果数量限制
    /// - `allowed_row_ids`: 允许搜索的行地址集合（标量索引结果）
    /// - `score_order`: 排序方式（从小到大/从大到小）
    /// - `parameters`: 索引参数（如nprobe, num_clusters等）
    ///
    /// # 返回
    /// - 返回在候选集中的Top-K向量候选（按距离排序）
    /// - 不包含不在候选集中的行
    ///
    /// # 默认实现
    /// 默认实现直接调用 `search_vector`，不使用候选集过滤。
    /// 子类可覆盖此方法实现预过滤优化。
    async fn search_vector_prefiltered(
        &self,
        query: &[f32],
        limit: usize,
        allowed_row_ids: &BTreeSet<RowAddress>,
        score_order: ScoreOrder,
        parameters: serde_json::Value,
    ) -> Result<Vec<IndexCandidate>> {
        // 默认实现：直接调用 search_vector，不使用候选集
        let mut all_candidates = self.search_vector(&VectorSearchRequest {
            query: query.to_vec(),
            limit: limit * 2, // 多取一些，允许后续过滤
            score_order,
            parameters,
            allowed_row_ids: None,
        }).await?;
        
        // 手动过滤不在 allowed_row_ids 中的候选
        all_candidates.retain(|c| allowed_row_ids.contains(&c.row_address));
        Ok(all_candidates)
    }
}
```

### 3.3 搜索协调器新增接口

**文件**：`iceberg-index-runtime/src/search.rs`

```rust
/// ── 新增：向量+标量混合搜索请求 ───────────────────────────
pub struct HybridSearchRequest {
    pub table_uuid: String,
    pub snapshot_id: SnapshotId,
    pub scalar_index_name: String,
    pub vector_index_name: String,
    pub scalar_query: ScalarSearchRequest,
    pub vector_query: VectorSearchRequest,
    pub pruned_files: Option<BTreeSet<String>>,
}

/// ── 新增：向量+标量混合搜索结果 ───────────────────────────
pub struct HybridSearchResult {
    pub snapshot_id: SnapshotId,
    /// 最终返回的Top-K向量候选（距离 + 标量验证通过）
    pub candidates: Vec<IndexCandidate>,
}

impl IndexSearchCoordinator {
    /// ── 新增：执行向量+标量混合搜索（方案A：Pre-filter） ───
    ///
    /// 执行流程：
    /// 1. 标量搜索（BTree索引），获取候选集
    /// 2. 向量搜索（IVF索引，传入候选集过滤）
    /// 3. 回表验证Top-K候选的标量条件
    /// 4. 全局排序，返回最终Top-K结果
    pub async fn search_hybrid_prefilter(
        &self,
        request: HybridSearchRequest,
    ) -> Result<HybridSearchResult> {
        // 步骤1：标量搜索（获取候选集）
        let scalar_result = self.search_scalar(SearchScalarIndexRequest {
            table_uuid: request.table_uuid,
            snapshot_id: request.snapshot_id,
            index_name: request.scalar_index_name,
            query: request.scalar_query,
            pruned_files: request.pruned_files,
        }).await?;
        
        if scalar_result.addresses.is_empty() {
            return Ok(HybridSearchResult {
                snapshot_id: request.snapshot_id,
                candidates: Vec::new(),
            });
        }
        
        // 步骤2：向量搜索（在候选集中搜索）
        let vector_candidates = self.search_vector_prefiltered(
            &request.vector_query.query,
            request.vector_query.limit,
            &scalar_result.addresses,
            request.vector_query.score_order,
            request.vector_query.parameters,
        ).await?;
        
        if vector_candidates.is_empty() {
            return Ok(HybridSearchResult {
                snapshot_id: request.snapshot_id,
                candidates: Vec::new(),
            });
        }
        
        // 步骤3：回表验证Top-K候选的标量条件
        let verified_candidates = self.verify_scalar_conditions(
            vector_candidates,
            &request.scalar_query.expressions,
        ).await?;
        
        // 步骤4：全局排序
        let mut sorted = verified_candidates;
        sorted.sort_by_key(|c| c.distance);
        
        Ok(HybridSearchResult {
            snapshot_id: request.snapshot_id,
            candidates: sorted,
        })
    }
    
    /// ── 辅助方法：回表验证标量条件 ─────────────────────────
    ///
    /// 并行回表读取标量列值，验证候选是否满足标量条件。
    async fn verify_scalar_conditions(
        &self,
        candidates: Vec<IndexCandidate>,
        expressions: &[ScalarExpression],
    ) -> Result<Vec<IndexCandidate>> {
        if expressions.is_empty() {
            return Ok(candidates); // 无标量条件，直接返回
        }
        
        let mut verified = Vec::new();
        
        // 并行回表验证所有候选
        for candidate in candidates {
            // 使用现有的RowAddress回表机制
            let row = self.source.read_row(&candidate.row_address).await?;
            
            // 验证标量条件
            if self.evaluate_row(&row, expressions)? {
                verified.push(candidate);
            }
        }
        
        Ok(verified)
    }
    
    /// ── 辅助方法：并行回表验证（优化版） ───────────────────
    ///
    /// 使用Rayon并行处理，提升回表验证性能。
    #[cfg(feature = "rayon")]
    async fn verify_scalar_conditions_parallel(
        &self,
        candidates: Vec<IndexCandidate>,
        expressions: &[ScalarExpression],
    ) -> Result<Vec<IndexCandidate>> {
        if expressions.is_empty() {
            return Ok(candidates);
        }
        
        use rayon::prelude::*;
        
        let verified: Vec<_> = candidates
            .into_par_iter()
            .filter(|candidate| {
                let row = self.source.read_row(&candidate.row_address).await.unwrap();
                self.evaluate_row(&row, expressions).unwrap()
            })
            .collect();
        
        Ok(verified)
    }
}
```

### 3.4 IVF插件实现

**文件**：`iceberg-index-plugins/src/ivf.rs`

```rust
// 覆盖 RuntimeIndex::search_vector_prefiltered 方法
impl VectorIndex for IVFIndex {
    async fn search_vector_prefiltered(
        &self,
        query: &[f32],
        limit: usize,
        allowed_row_ids: &BTreeSet<RowAddress>,
        score_order: ScoreOrder,
        parameters: serde_json::Value,
    ) -> Result<Vec<IndexCandidate>> {
        let params: IVFSearchParams = serde_json::from_value(parameters)?;
        let nprobe = params.nprobe.unwrap_or(DEFAULT_NPROBE);
        
        // 1. 确定nprobe个最近中心
        let centers = self.find_nearest_centers(query, nprobe)?;
        
        // 2. 在候选集中收集向量候选
        let mut candidates = Vec::new();
        
        for center in &centers {
            // 扩展到多个倒排列表（重复查询）
            for inverted_list in &self.inverted_lists[center.idx] {
                if let Some(rows) = self.get_rows_from_list(&inverted_list) {
                    // ── 关键优化：只保留在allowed_row_ids中的行 ───
                    let filtered = rows.iter().filter(|row_id| {
                        let addr = RowAddress::new(self.file_path.clone(), *row_id);
                        allowed_row_ids.contains(&addr)
                    }).cloned().collect::<Vec<_>>();
                    
                    // 计算距离
                    for row_id in filtered {
                        let distance = self.compute_distance(query, row_id);
                        candidates.push(IndexCandidate {
                            row_address: RowAddress::new(self.file_path.clone(), row_id),
                            distance,
                            is_overfetch: false,
                        });
                    }
                }
            }
        }
        
        // 3. 过滤dead-row
        let live_rows = self.get_live_row_positions(&self.file_path)?;
        candidates.retain(|c| live_rows.contains(&c.row_address.row_position));
        
        // 4. 取Top-K（如果候选数量远小于limit）
        let k = if candidates.len() > limit {
            limit
        } else {
            candidates.len()
        };
        
        // 5. 按距离排序
        candidates.sort_by(|a, b| a.distance.partial_cmp(&b.distance).unwrap());
        
        // 6. 返回Top-K
        Ok(candidates.into_iter().take(k).collect())
    }
}
```

---

## 四、优化点设计

### 4.1 并行回表优化

使用Rayon并行处理回表验证，充分利用多核CPU：

```rust
#[cfg(feature = "rayon")]
async fn verify_scalar_conditions_parallel(
    &self,
    candidates: Vec<IndexCandidate>,
    expressions: &[ScalarExpression],
) -> Result<Vec<IndexCandidate>> {
    if expressions.is_empty() {
        return Ok(candidates);
    }
    
    use rayon::prelude::*;
    
    let verified: Vec<_> = candidates
        .into_par_iter() // 并行处理
        .filter(|candidate| {
            let row = self.source.read_row(&candidate.row_address).await.unwrap();
            self.evaluate_row(&row, expressions).unwrap()
        })
        .collect();
    
    Ok(verified)
}
```

**性能提升**：
- 假设回表耗时 10ms，候选数 100
- 单线程：100 * 10ms = 1000ms
- 8线程并行：100 / 8 * 10ms = 125ms（8倍加速）

### 4.2 RowAddress存储优化

**问题**：IVF倒排列表存储简单的row_id，与RowAddress转换开销大

**方案**：在IVF倒排列表中直接存储RowAddress

```rust
// 原始存储（简单但转换开销大）
pub struct InvertedList {
    pub row_id: u64,  // 需要转换成RowAddress
}

// 优化存储（直接存储RowAddress）
pub struct InvertedList {
    pub row_address: RowAddress,  // 直接存储，无转换
}

// 查找优化
pub fn find_candidates(&self, allowed_row_ids: &BTreeSet<RowAddress>) -> Vec<IndexCandidate> {
    self.inverted_lists
        .iter()
        .filter(|list| allowed_row_ids.contains(&list.row_address))
        .map(|list| IndexCandidate {
            row_address: list.row_address.clone(),
            distance: self.compute_distance(query, list.row_address),
            is_overfetch: false,
        })
        .collect()
}
```

### 4.3 Top-K候选集大小控制

避免标量搜索返回过多候选导致IVF搜索效率下降：

```rust
// 限制标量搜索返回的候选数量
let scalar_result = self.search_scalar(...).await?;
let max_scalar_candidates = 10000; // 可配置的上限
let scalar_candidates: Vec<_> = scalar_result.addresses
    .into_iter()
    .take(max_scalar_candidates)
    .collect();

if scalar_candidates.len() < scalar_result.addresses.len() {
    tracing::warn!("标量搜索候选集被裁剪到上限（{}），可能影响结果准确性", max_scalar_candidates);
}
```

---

## 五、实施步骤

### Phase 1：基础接口扩展（2-3天）

- [ ] 扩展 `VectorSearchRequest`，新增 `allowed_row_ids` 字段
- [ ] 扩展 `RuntimeIndex` trait，新增 `search_vector_prefiltered` 方法（默认实现）
- [ ] 扩展 `IndexSearchCoordinator`，新增 `search_hybrid_prefilter` 方法
- [ ] 编写单元测试：验证接口设计正确性

### Phase 2：标量搜索集成（2天）

- [ ] 在 `search_hybrid_prefilter` 中调用 `search_scalar`
- [ ] 验证标量搜索返回 `Vec<RowAddress>` 可作为IVF搜索的候选集
- [ ] 测试标量搜索 + IVF搜索的基础流程
- [ ] 编写集成测试：标量条件 = 1 的场景

### Phase 3：IVF搜索优化（3-4天）

- [ ] 实现IVF的 `search_vector_prefiltered` 覆盖方法
- [ ] 优化倒排列表过滤逻辑（避免构造完整候选集）
- [ ] 添加性能测试：对比有无候选集过滤的性能差异
- [ ] 测试不同nprobe下的性能

### Phase 4：回表验证（2天）

- [ ] 实现并行回表验证 `verify_scalar_conditions_parallel`
- [ ] 验证 RowAddress 精确读取路径
- [ ] 测试回表性能（Top-K候选的回表开销）
- [ ] 优化回表并行度（限制max 16 threads）

### Phase 5：端到端集成（2天）

- [ ] FDW层路由决策：识别混合查询并调用HybridSearch
- [ ] 完整测试：标量索引 + 向量索引并存场景
- [ ] 测试边界情况：标量候选为空、标量候选过少等
- [ ] 性能基准测试：验证Pre-filter加速效果

### Phase 6：文档和测试（2天）

- [ ] 编写集成测试用例（5-10个场景）
- [ ] 更新 `capabilities.md`，标记为 `Implemented`
- [ ] 编写使用文档（SQL示例、性能调优）
- [ ] 更新设计文档，补充案例

**总工时**：13-17天

---

## 六、性能预期

### 6.1 执行时间分解

```
场景：WHERE category = 'electronics' AND vector ~ q

数据规模：1000万行
索引：
  - BTree(category): 覆盖1000行（选择率0.01%）
  - IVF(vector): 1024个cluster，nprobe=10

执行时间分解：
  ┌─────────────────────────────────────────────────────┐
  │ 标量搜索（BTree）                                    │
  │   ├─ Coverage规划：    ~5ms                         │
  │   ├─ 加载segment：     ~10ms                        │
  │   ├─ 前缀定位+过滤：   ~15ms                        │
  │   └─ 合计：           ~30ms                         │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 向量搜索（IVF + 预过滤）                              │
  │   ├─ nprobe搜索中心：  ~10ms                        │
  │   ├─ 候选集过滤：      ~60ms（1024中心 × 倒排列表）   │
  │   ├─ Top-K排序：       ~5ms                         │
  │   └─ 合计：           ~75ms                         │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 回表验证（并行，10个候选）                            │
  │   ├─ 并行读取：       ~15ms                         │
  │   ├─ 标量条件验证：    ~10ms                         │
  │   └─ 合计：           ~25ms                         │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 全局排序 + 返回                                        │
  │   └─ 合计：       ~5ms                             │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 总计：           ~135ms                             │
  └─────────────────────────────────────────────────────┘
```

### 6.2 性能对比

```
纯向量搜索（无标量优化）：
  ┌─────────────────────────────────────────────────────┐
  │ IVF搜索（全表）：           ~500ms                    │
  │   ├─ nprobe搜索中心：      ~10ms                    │
  │   ├─ 候选集搜索：          ~480ms（100万行）         │
  │   ├─ Top-K排序：           ~10ms                    │
  │   └─ 合计：                ~500ms                    │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 总计：            ~500ms                             │
  └─────────────────────────────────────────────────────┘

Pre-filter方案：
  ┌─────────────────────────────────────────────────────┐
  │ 总计：           ~135ms                             │
  └─────────────────────────────────────────────────────┘

加速比：500ms / 135ms ≈ 3.7倍
```

### 6.3 回表成本分析

```
场景分析：

场景1：Top-K = 10，标量候选 = 1000
  纯向量搜索：0次回表
  Pre-filter：10次回表
  回表成本：10 * 0.1ms = 1ms
  占比：1ms / 135ms ≈ 0.7%

场景2：Top-K = 100，标量候选 = 100000
  纯向量搜索：0次回表
  Pre-filter：100次回表
  回表成本：100 * 0.1ms = 10ms
  占比：10ms / 200ms ≈ 5%

场景3：Top-K = 1000，标量候选 = 1000000
  纯向量搜索：0次回表
  Pre-filter：1000次回表
  回表成本：1000 * 0.1ms = 100ms
  占比：100ms / 350ms ≈ 28.5%

结论：在正常场景（Top-K ≤ 100）下，回表成本占比 < 10%
```

---

## 七、关键风险和应对

### 7.1 风险1：标量搜索结果过大

**问题描述**：
标量索引选择率低，返回大量候选（如100万行），导致IVF搜索效率下降。

**场景示例**：
```sql
-- 场景：标量列user_id选择率低
WHERE user_id = 123 AND vector ~ q
-- 假设表1000万行，user_id=123只有1行
-- 但BTree(user_id)返回100万行（错误的）
```

**应对策略**：

1. **限制候选集大小**：
   ```rust
   // 在search_hybrid_prefilter中
   let max_scalar_candidates = 10000; // 可配置
   let scalar_candidates: Vec<_> = scalar_result.addresses
       .into_iter()
       .take(max_scalar_candidates)
       .collect();
   
   if scalar_candidates.len() < scalar_result.addresses.len() {
       tracing::warn!("标量搜索候选集被裁剪到上限（{}），可能影响结果准确性", max_scalar_candidates);
   }
   ```

2. **动态回退到方案B**：
   ```rust
   // 当候选集过大时，回退到方案B（纯向量搜索 + 回表验证）
   if scalar_candidates.len() > 10000 {
       tracing::info!("标量候选集过大（{}），回退到方案B", scalar_candidates.len());
       return self.search_hybrid_postfilter(request).await?;
   }
   ```

3. **优化标量索引构建**：
   - 添加标量列的选择率统计
   - 在FDW路由时预估选择率
   - 选择率低时直接跳过标量搜索

### 7.2 风险2：RowAddress不连续

**问题描述**：
IVF索引中的row_id不连续，难以高效过滤候选。

**场景示例**：
```rust
// IVF倒排列表存储的row_id
[100, 150, 1000, 2000, 10000, ...]

// 标量搜索返回的RowAddress
[RowAddress{file_path: "data/file1.parquet", row_position: 150},
 RowAddress{file_path: "data/file1.parquet", row_position: 2000}, ...]

// 转换开销：需要逐一匹配
```

**应对策略**：

1. **直接存储RowAddress**：
   ```rust
   // 优化IVF倒排列表存储
   pub struct InvertedList {
       pub row_address: RowAddress,  // 直接存储，无转换
   }
   
   // 查找优化
   pub fn find_candidates(&self, allowed_row_ids: &BTreeSet<RowAddress>) -> Vec<IndexCandidate> {
       self.inverted_lists
           .iter()
           .filter(|list| allowed_row_ids.contains(&list.row_address))
           .map(|list| IndexCandidate {
               row_address: list.row_address.clone(),
               distance: self.compute_distance(query, list.row_address),
               is_overfetch: false,
           })
           .collect()
   }
   ```

2. **使用Hash Map优化查找**：
   ```rust
   // 预处理allowed_row_ids为HashSet
   let allowed_set: HashSet<&RowAddress> = allowed_row_ids.iter().collect();
   
   // 在IVF搜索时快速查找
   candidates.retain(|c| allowed_set.contains(&c.row_address));
   ```

### 7.3 风险3：回表性能瓶颈

**问题描述**：
并行回表过多，反而增加延迟。

**场景示例**：
```rust
// 标量候选100万行，Top-K=1000
// 并行回表1000次，但内存带宽可能成为瓶颈
```

**应对策略**：

1. **限制并行度**：
   ```rust
   const MAX_PARALLELISM: usize = 16;
   
   let parallelism = (candidates.len() as usize).min(MAX_PARALLELISM);
   
   let verified: Vec<_> = if parallelism > 1 {
       candidates.par_chunks(parallelism)
           .into_par_iter()
           .flat_map(|chunk| verify_scalar_conditions_for_chunk(chunk, expressions))
           .collect()
   } else {
       candidates.into_iter()
           .filter(|c| verify_single_candidate(c, expressions))
           .collect()
   };
   ```

2. **增加回表缓存**：
   ```rust
   // 缓存热点候选的标量列值
   let mut cache = LruCache::new(1000);
   
   async fn verify_candidate_cached(
       &self,
       candidate: &IndexCandidate,
       expressions: &[ScalarExpression],
   ) -> Result<bool> {
       let cache_key = candidate.row_address.clone();
       if let Some(cached) = cache.get(&cache_key) {
           return Ok(cached);
       }
       
       let row = self.source.read_row(&candidate.row_address).await?;
       let result = self.evaluate_row(&row, expressions)?;
       
       cache.put(cache_key, result);
       Ok(result)
   }
   ```

3. **预取优化**：
   ```rust
   // 使用tokio::task::spawn并发读取多个文件
   use tokio::task::spawn;
   
   let mut handles = Vec::new();
   for batch in candidates.chunks(100) {
       let handle = spawn(async move {
           verify_scalar_conditions_for_batch(batch, expressions)
       });
       handles.push(handle);
   }
   
   // 等待所有批次完成
   let results: Vec<_> = handles.into_iter()
       .map(|h| h.await.unwrap())
       .collect();
   ```

---

## 八、测试策略

### 8.1 单元测试

**标量搜索集成测试**：
```rust
#[tokio::test]
async fn test_hybrid_prefilter_basic() {
    // 创建测试表和数据
    // 建立标量索引和BTree索引
    // 执行混合查询
    // 验证结果正确性
}
```

**IVF预过滤测试**：
```rust
#[tokio::test]
async fn test_ivf_prefilter_optimization() {
    // 测试候选集过滤是否生效
    // 对比有无候选集过滤的性能差异
}
```

**回表验证测试**：
```rust
#[tokio::test]
async fn test_scalar_condition_verification() {
    // 验证回表是否正确读取标量列值
    // 验证标量条件过滤是否正确
}
```

### 8.2 集成测试

**场景1：简单混合查询**
```sql
-- 表结构
CREATE TABLE t (id INT, category TEXT, vector VECTOR(128));

-- 索引
CREATE INDEX idx_category ON t (category) USING BTREE;
CREATE INDEX idx_vector ON t (vector) USING IVF_FLAT;

-- 查询
SELECT * FROM t
WHERE category = 'electronics'
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

**场景2：多标量条件混合查询**
```sql
SELECT * FROM t
WHERE category = 'electronics'
  AND price > 100
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

**场景3：高选择率标量条件**
```sql
-- 标量条件选择率高，但仍然受益
SELECT * FROM t
WHERE user_id = 123
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

**场景4：标量候选集过大**
```sql
-- 标量条件选择率低，回退到方案B
SELECT * FROM t
WHERE year = 2024
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

### 8.3 性能测试

**基准测试**：
```bash
# 使用BenchComplete跑性能测试
python scripts/bench_complete.py \
  --query "WHERE category = 'electronics' AND vector IS CLOSEST TO q LIMIT 10" \
  --iterations 100
```

**对比测试**：
- 纯向量搜索 vs Pre-filter方案
- 不同标量选择率下的性能
- 不同Top-K值下的性能

---

## 九、后续扩展

### 9.1 方案B（Post-filter）扩展

当标量索引存在但选择率低时，自动回退到方案B：

```rust
async fn search_hybrid_auto(
    &self,
    request: HybridSearchRequest,
) -> Result<HybridSearchResult> {
    // 1. 标量搜索
    let scalar_result = self.search_scalar(...).await?;
    let scalar_selection_rate = scalar_result.addresses.len() as f64 / total_rows;
    
    // 2. 决策：选择率低 → 方案B，否则 → 方案A
    if scalar_selection_rate < 0.01 {
        tracing::info!("标量选择率低（{:.2}%），使用方案B（Post-filter）", scalar_selection_rate * 100);
        return self.search_hybrid_postfilter(request).await?;
    } else {
        tracing::info!("标量选择率较高（{:.2}%），使用方案A（Pre-filter）", scalar_selection_rate * 100);
        return self.search_hybrid_prefilter(request).await?;
    }
}
```

### 9.2 多标量条件优化

支持多个标量条件的组合搜索：

```rust
ScalarSearchRequest {
    expressions: vec![
        ScalarExpression { field_id: 1, op: Eq, value: ScalarValue::Int64(1) },      // category = 1
        ScalarExpression { field_id: 2, op: Gt, value: ScalarValue::Int64(100) },   // price > 100
        ScalarExpression { field_id: 3, op: Le, value: ScalarValue::Int64(1000) },  // price <= 1000
    ],
}
```

**优化点**：
- 在标量搜索时并行过滤多个条件
- 多列mask优化（类似多列BTree）

### 9.3 成本估算与规划器集成

```rust
/// 成本估算
pub struct HybridSearchCost {
    pub scalar_search_cost: f64,
    pub vector_search_cost: f64,
    pub backfill_cost: f64,
    pub total_cost: f64,
}

impl IndexSearchCoordinator {
    fn estimate_hybrid_cost(
        &self,
        scalar_selection_rate: f64,
        total_rows: usize,
        k: usize,
    ) -> HybridSearchCost {
        // 标量搜索成本
        let scalar_search_cost = 1.0 / scalar_selection_rate * 1e-3; // 与选择率成反比
        
        // 向量搜索成本（取决于候选集大小）
        let vector_search_cost = (scalar_selection_rate * total_rows) as f64 * 1e-4;
        
        // 回表成本
        let backfill_cost = k as f64 * 0.1; // 每行0.1ms
        
        HybridSearchCost {
            scalar_search_cost,
            vector_search_cost,
            backfill_cost,
            total_cost: scalar_search_cost + vector_search_cost + backfill_cost,
        }
    }
}
```

---

## 十、总结

### 10.1 方案优势

1. **加速效果显著**：预过滤可减少100-1000倍的向量搜索量，3-10倍加速
2. **复用现有基础设施**：标量搜索、IVF搜索、回表机制均已实现
3. **回表成本低**：仅Top-K候选回表，占比 < 10%
4. **架构清晰**：Pre-filter → 向量搜索 → 回表验证，流程直观

### 10.2 关键设计点

1. **接口设计**：`VectorSearchRequest` 新增 `allowed_row_ids` 字段
2. **搜索流程**：标量搜索 → 向量搜索（预过滤）→ 回表验证 → 全局排序
3. **性能优化**：并行回表、RowAddress直接存储、候选集大小控制
4. **容错机制**：候选集过大时回退到方案B

### 10.3 实施优先级

**高优先级**（核心路径）：
1. Phase 1：基础接口扩展
2. Phase 2：标量搜索集成
3. Phase 3：IVF搜索优化
4. Phase 5：端到端集成

**中优先级**（性能提升）：
5. Phase 4：回表验证优化
6. Phase 6：文档和测试

**低优先级**（高级优化）：
- 方案B自动回退
- 成本估算与规划器集成

---

## 附录

### A. 参考文档

- [多列BTree索引设计方案.md](../getting-started/multi-column-btree.md) - 标量多列索引设计
- [能力与限制矩阵](../reference/capabilities.md) - 当前支持能力
- [心智模型](../concepts/mental-model.md) - 核心概念

### B. 相关Issue

- 向量+标量混合查询需求
- ANN-aware scalar filter
- 多候选择优 (#38)

### C. 测试用例

详见[测试策略](#八、测试策略)

### D. 性能基准

详见[性能预期](#六、性能预期)
