# 向量排序索引设计方案

> 最后更新: 2026-08-19
> 状态: 待实现

## 一、场景定义

### 1.1 核心场景

```sql
SELECT * FROM tab
WHERE a = 1 AND b = 2
ORDER BY vector_c
LIMIT 5;
```

**关键要素**：
- **标量过滤**：`WHERE a = 1 AND b = 2`（利用标量BTree索引）
- **向量排序**：`ORDER BY vector_c`（利用向量索引）
- **结果限制**：`LIMIT 5`（TOP-K查询）

### 1.2 与相似度搜索的区别

| 维度 | 相似度搜索（原方案A） | 向量排序（新方案） |
|------|-------------------|------------------|
| 查询语义 | `WHERE vector IS CLOSEST TO q` | `ORDER BY vector_c` |
| 索引目标 | 找最相似的k个向量 | 找向量最大的前k个 |
| 查询向量 | 需要查询向量 `q` | 只需要阈值/比较值 |
| 结果排序 | 按距离从小到大 | 按向量值大小 |
| IVF作用 | nprobe最近中心 | 最近中心 + 候选集 |

### 1.3 优化目标

```
当前行为（慢）：
  1. 全表扫描1000万行
  2. 过滤 a=1, b=2 → 1000行
  3. 对1000行按vector_c排序 → O(1000 log 1000)
  4. 取TOP-5
  总耗时：~2秒

优化后（快）：
  1. 标量索引过滤 → 1000行
  2. IVF索引排序 → O(1000 log k)
  3. 取TOP-5
  总耗时：~50ms

加速比：40倍
```

---

## 二、核心流程

### 2.1 端到端流程图

```
┌─────────────────────────────────────────────────────────────┐
│  FDW层                                                         │
│    └─ 收集查询谓词                                            │
│        - WHERE a=1, b=2                                      │
│        - ORDER BY vector_c                                   │
│        - LIMIT 5                                             │
│    └─ 路由决策                                                │
│        - 检测到标量索引：idx_a (BTree)                        │
│        - 检测到向量排序索引：idx_vector (IVF-SORTED)          │
│        - 决定走HybridSearchRequest                           │
│    └─ 调用引擎                                                │
│        └─ IndexSearchCoordinator::search_vector_sorted      │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  标量搜索（BTree索引）                                         │
│    ├─ Coverage规划                                            │
│    ├─ 前缀定位 + 多列mask过滤                                  │
│    └─ 返回 Vec<RowAddress>（保守超集，如1000行）              │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  向量排序搜索（IVF-SORTED索引）                                │
│    ├─ 加载IVF segment                                        │
│    ├─ 确定nprobe个最近中心                                    │
│    ├─ 在候选集中找向量值 >= 查询值的行（最小堆）              │
│    └─ 维护TOP-K，返回最小堆                                  │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  回表验证（并行读取标量列值）                                  │
│    ├─ 读取每个候选的 a, b 列值                                │
│    ├─ 验证是否满足 a=1, b=2                                  │
│    └─ 返回验证通过的候选                                      │
└─────────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────────┐
│  排序 + 返回                                                   │
│    ├─ 按向量距离排序（最小堆已排好）                           │
│    └─ 返回TOP-5结果                                          │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 详细步骤

#### 步骤1：标量搜索

```rust
let scalar_result = self.search_scalar(SearchScalarIndexRequest {
    table_uuid: request.table_uuid,
    snapshot_id: request.snapshot_id,
    index_name: request.scalar_index_name,  // idx_a (BTree)
    query: ScalarSearchRequest {
        expressions: vec![
            ScalarExpression { field_id: 1, op: Eq, value: ScalarValue::Int64(1) },
            ScalarExpression { field_id: 2, op: Eq, value: ScalarValue::Int64(2) },
        ],
        limit: None,
    },
    pruned_files: request.pruned_files,
}).await?;

// scalar_result.addresses = Vec<RowAddress>
// 例如：[RowAddress{file_path: "data/file1.parquet", row_position: 100},
//       RowAddress{file_path: "data/file1.parquet", row_position: 250}, ...]
```

#### 步骤2：向量排序搜索

```rust
let vector_result = self.search_vector_sorted(
    &request.vector_sort_request,  // limit=5
    &scalar_result.addresses,       // 候选集
).await?;

// vector_result.candidates = Vec<IndexCandidate>
// 每个候选包含：
// - row_address: RowAddress
// - distance: f64（到查询向量的距离，用于排序）
// - is_overfetch: bool
```

#### 步骤3：回表验证

```rust
let verified_candidates = self.verify_scalar_conditions(
    vector_result.candidates,
    &request.scalar_query.expressions,  // [a=1, b=2]
).await?;

// verified_candidates 过滤掉不满足 a=1, b=2 的候选
```

#### 步骤4：排序 + 返回

```rust
let mut sorted = verified_candidates;
sorted.sort_by_key(|c| c.distance);

// 返回TOP-5
return sorted.into_iter().take(5).collect();
```

---

## 三、接口设计

### 3.1 扩展 IndexKind

**文件**：`iceberg-index-core/src/model.rs`

```rust
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum IndexKind {
    Scalar,
    Vector,
    #[serde(rename = "vector_sorted")]
    VectorSorted,  // ── 新增：向量排序索引 ─────────────────────────
}
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
    
    // ── 新增：向量排序搜索 ───────────────────────────────────
    /// 向量排序搜索接口
    ///
    /// # 参数
    /// - `limit`: 返回结果数量限制（TOP-K）
    /// - `allowed_row_ids`: 允许搜索的行地址集合（标量索引结果）
    /// - `metric`: 距离度量（L2, Cosine, Inner Product）
    ///
    /// # 返回
    /// - 返回TOP-K向量候选（按距离从小到大排序）
    async fn search_vector_sorted(
        &self,
        limit: usize,
        allowed_row_ids: &BTreeSet<RowAddress>,
        metric: DistanceMetric,
    ) -> Result<Vec<IndexCandidate>> {
        // 默认实现：全表搜索 + 过滤（降级方案）
        let all_candidates = self.search_vector(&VectorSearchRequest {
            query: vec![],  // 向量排序不需要查询向量
            limit: limit * 2, // 多取一些
            score_order: ScoreOrder::SmallerIsBetter,
            parameters: serde_json::json!({
                "metric": match metric {
                    DistanceMetric::L2 => "l2",
                    DistanceMetric::Cosine => "cosine",
                    DistanceMetric::InnerProduct => "inner_product",
                }
            }),
            allowed_row_ids: Some(Arc::new(allowed_row_ids.clone())),
        }).await?;
        
        // 手动过滤不在 allowed_row_ids 中的候选
        let filtered = all_candidates
            .into_iter()
            .filter(|c| allowed_row_ids.contains(&c.row_address))
            .collect();
        
        // 取TOP-K
        let k = if filtered.len() > limit {
            limit
        } else {
            filtered.len()
        };
        
        filtered.into_iter().take(k).collect()
    }
}
```

### 3.3 搜索协调器新增接口

**文件**：`iceberg-index-runtime/src/search.rs`

```rust
/// ── 新增：向量排序搜索请求 ───────────────────────────────────
pub struct VectorSortRequest {
    pub table_uuid: String,
    pub snapshot_id: SnapshotId,
    pub index_name: String,
    pub limit: usize,
    pub scalar_query: ScalarSearchRequest,
    pub pruned_files: Option<BTreeSet<String>>,
}

/// ── 新增：向量排序搜索结果 ───────────────────────────────────
pub struct VectorSortResult {
    pub snapshot_id: SnapshotId,
    pub candidates: Vec<IndexCandidate>,
}

impl IndexSearchCoordinator {
    /// ── 新增：执行向量排序搜索 ───────────────────────────────────
    /// 
    /// 执行流程：
    /// 1. 标量搜索（BTree索引），获取候选集
    /// 2. 向量排序搜索（IVF-SORTED索引），在候选集中找TOP-K
    /// 3. 回表验证候选的标量条件
    /// 4. 排序，返回最终TOP-K结果
    pub async fn search_vector_sorted(
        &self,
        request: VectorSortRequest,
    ) -> Result<VectorSortResult> {
        // 步骤1：标量搜索（获取候选集）
        let scalar_result = self.search_scalar(SearchScalarIndexRequest {
            table_uuid: request.table_uuid,
            snapshot_id: request.snapshot_id,
            index_name: request.index_name,
            query: request.scalar_query,
            pruned_files: request.pruned_files,
        }).await?;
        
        if scalar_result.addresses.is_empty() {
            return Ok(VectorSortResult {
                snapshot_id: request.snapshot_id,
                candidates: Vec::new(),
            });
        }
        
        // 步骤2：向量排序搜索
        let metric = DistanceMetric::L2; // 可配置
        let vector_candidates = self.search_vector_sorted(
            request.limit,
            &scalar_result.addresses,
            metric,
        ).await?;
        
        if vector_candidates.is_empty() {
            return Ok(VectorSortResult {
                snapshot_id: request.snapshot_id,
                candidates: Vec::new(),
            });
        }
        
        // 步骤3：回表验证
        let verified_candidates = self.verify_scalar_conditions(
            vector_candidates,
            &request.scalar_query.expressions,
        ).await?;
        
        // 步骤4：排序 + 返回
        let mut sorted = verified_candidates;
        sorted.sort_by_key(|c| c.distance);
        
        Ok(VectorSortResult {
            snapshot_id: request.snapshot_id,
            candidates: sorted,
        })
    }
}
```

### 3.4 IVF-SORTED插件实现

**文件**：`iceberg-index-plugins/src/ivf_sorted.rs`

```rust
#[async_trait]
impl VectorIndex for IVFSortedIndex {
    async fn search_vector_sorted(
        &self,
        limit: usize,
        allowed_row_ids: &BTreeSet<RowAddress>,
        metric: DistanceMetric,
    ) -> Result<Vec<IndexCandidate>> {
        // 1. 确定nprobe
        let nprobe = self.nprobe;
        
        // 2. 确定搜索方向（按距离升序还是降序）
        let search_asc = match metric {
            DistanceMetric::L2 | DistanceMetric::InnerProduct => true,  // L2从小到大，内积从小到大
            DistanceMetric::Cosine => false, // 余弦从小到大（距离小=相似度高）
        };
        
        // 3. 找nprobe个最近中心
        let mut centers = vec![];
        for _ in 0..nprobe {
            let center = self.find_nearest_center(&self.search_vector, &mut centers)?;
            centers.push(center);
        }
        
        // 4. 在候选集中找TOP-K（最小堆）
        let mut min_heap = BinaryHeap::new();
        
        for center in &centers {
            // 4.1 获取该中心的倒排列表
            if let Some(inverted_list) = &self.inverted_lists[center.idx] {
                // 4.2 遍历倒排列表中的所有行
                for (vector_value, row_address) in &self.vector_values[center.idx] {
                    // 4.3 只保留在allowed_row_ids中的候选
                    if !allowed_row_ids.contains(row_address) {
                        continue;
                    }
                    
                    // 4.4 计算距离
                    let distance = self.compute_distance(vector_value, metric);
                    
                    // 4.5 根据搜索方向决定是否加入堆
                    // 如果search_asc=true，距离小优先（正常TOP-K）
                    // 如果search_asc=false，距离大优先（需要最大堆）
                    if search_asc {
                        if min_heap.len() < limit || distance < min_heap.peek().unwrap().distance {
                            min_heap.push(IndexCandidate {
                                row_address: row_address.clone(),
                                distance,
                                is_overfetch: false,
                            });
                            
                            if min_heap.len() > limit {
                                min_heap.pop(); // 保持TOP-K
                            }
                        }
                    } else {
                        // 搜索最大值：使用最大堆
                        if min_heap.len() < limit || distance > min_heap.peek().unwrap().distance {
                            min_heap.push(IndexCandidate {
                                row_address: row_address.clone(),
                                distance,
                                is_overfetch: false,
                            });
                            
                            if min_heap.len() > limit {
                                min_heap.pop(); // 保持TOP-K
                            }
                        }
                    }
                }
            }
        }
        
        // 5. 转换为Vec并按距离排序（从小到大）
        let mut result = Vec::from_iter(min_heap);
        result.sort_by_key(|c| c.distance);
        
        // 6. 返回TOP-K
        Ok(result.into_iter().take(limit).collect())
    }
}
```

### 3.5 IVF-SORTED索引存储格式

```rust
// IVF-SORTED segment结构
pub struct IVFSortedSegment {
    // 基础字段（与IVF-Flat相同）
    pub file_path: String,
    pub indexed_rows: u64,
    pub num_clusters: usize,
    pub nprobe: usize,
    
    // ── 新增：向量值存储 ───────────────────────────────────────
    /// 每个cluster的向量值列表
    /// 结构：Vec<Vec<(Vec<f32>, RowAddress)>>
    /// 每个元素是 (vector_value, row_address)
    pub vector_values: Vec<Vec<(Vec<f32>, RowAddress)>>,
    
    /// 倒排列表
    pub inverted_lists: Vec<Vec<u64>>, // 存储row_id
}
```

**存储示例**：
```
num_clusters = 1024

Centroids: [c0, c1, c2, ..., c1023]

每个cluster的vector_values[i]:
  - vector_values[0] = [(v0, addr0), (v1, addr1), (v2, addr2), ...]
  - vector_values[1] = [(v10, addr10), (v11, addr11), (v12, addr12), ...]
  - ...

inverted_lists[i]:
  - inverted_lists[0] = [0, 5, 10, ...]  // 对应vector_values[0]
  - inverted_lists[1] = [1, 6, 11, ...]  // 对应vector_values[1]
  - ...
```

### 3.6 注册到插件系统

**文件**：`iceberg-index-plugins/src/lib.rs`

```rust
pub const IVF_SORTED_IMPLEMENTATION: &str = "huawei.gauss-infra.ivf-sorted-v1";

pub fn register_builtin_plugins() {
    // ... 原有注册
    
    // ── 新增：注册向量排序索引 ───────────────────────────────────
    registry.register("huawei.gauss-infra.ivf-sorted-v1", {
        Box::new(IVFSortedIndex::default())
    });
}
```

---

## 四、搜索算法详解

### 4.1 IVF-SORTED搜索核心逻辑

```rust
pub fn search_vector_sorted_core(
    &self,
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
    metric: DistanceMetric,
) -> Vec<IndexCandidate> {
    use std::collections::BinaryHeap;
    
    // 1. 确定nprobe
    let nprobe = self.nprobe;
    
    // 2. 确定搜索方向
    let search_asc = match metric {
        DistanceMetric::L2 => true,
        DistanceMetric::Cosine => true,
        DistanceMetric::InnerProduct => true,
    };
    
    // 3. 初始化最小堆（存距离更小的，保持TOP-K）
    let mut heap = BinaryHeap::with_capacity(limit);
    
    // 4. 找nprobe个最近中心
    let mut centers = self.find_nearest_centers(self.search_vector, nprobe)?;
    
    // 5. 遍历每个中心
    for center in centers {
        // 5.1 获取倒排列表
        let inverted_list = &self.inverted_lists[center.idx];
        let vector_values = &self.vector_values[center.idx];
        
        // 5.2 遍历倒排列表
        for (i, row_id) in inverted_list.iter().enumerate() {
            // 5.3 只保留在allowed_row_ids中的候选
            let row_address = RowAddress::new(self.file_path.clone(), *row_id);
            if !allowed_row_ids.contains(&row_address) {
                continue;
            }
            
            // 5.4 计算距离
            let vector_value = &vector_values[i].0;
            let distance = self.compute_distance(vector_value, metric);
            
            // 5.5 维护TOP-K（最小堆）
            if heap.len() < limit || distance < heap.peek().unwrap().distance {
                heap.push(IndexCandidate {
                    row_address,
                    distance,
                    is_overfetch: false,
                });
                
                if heap.len() > limit {
                    heap.pop(); // 保持TOP-K
                }
            }
        }
    }
    
    // 6. 转换并排序
    let mut result = Vec::from_iter(heap);
    result.sort_by_key(|c| c.distance);
    
    // 7. 返回TOP-K
    result.into_iter().take(limit).collect()
}
```

### 4.2 距离计算

```rust
impl IVFSortedIndex {
    fn compute_distance(&self, vector_value: &[f32], metric: DistanceMetric) -> f64 {
        match metric {
            DistanceMetric::L2 => {
                // 平方L2距离
                let mut sum = 0.0;
                for (a, b) in self.search_vector.iter().zip(vector_value.iter()) {
                    let diff = a - b;
                    sum += diff * diff;
                }
                sum
            }
            
            DistanceMetric::Cosine => {
                // 余弦距离 = 1 - 余弦相似度
                let dot_product = self
                    .search_vector
                    .iter()
                    .zip(vector_value.iter())
                    .map(|(a, b)| a * b)
                    .sum::<f32>();
                
                let norm_a = self.search_vector.iter().map(|x| x * x).sum::<f32>().sqrt();
                let norm_b = vector_value.iter().map(|x| x * x).sum::<f32>().sqrt();
                
                if norm_a == 0.0 || norm_b == 0.0 {
                    return 2.0; // 最坏情况
                }
                
                let cosine_sim = dot_product / (norm_a * norm_b);
                1.0 - cosine_sim
            }
            
            DistanceMetric::InnerProduct => {
                // 内积（L2归一化后等价于余弦）
                self.search_vector
                    .iter()
                    .zip(vector_value.iter())
                    .map(|(a, b)| a * b)
                    .sum::<f32>() as f64
            }
        }
    }
}
```

### 4.3 最近中心查找

```rust
impl IVFSortedIndex {
    fn find_nearest_centers(
        &self,
        query: &[f32],
        k: usize,
    ) -> Result<Vec<IVFCenter>> {
        // 1. 计算query到所有centroids的距离
        let mut distances: Vec<(usize, f64)> = self
            .centroids
            .iter()
            .enumerate()
            .map(|(i, centroid)| {
                (i, self.compute_centroid_distance(centroid, query))
            })
            .collect();
        
        // 2. 找最小的k个
        distances.sort_by_key(|(_, dist)| *dist);
        
        // 3. 返回k个中心
        Ok(distances.into_iter().take(k).map(|(i, _)| IVFCenter { idx: i }).collect())
    }
    
    fn compute_centroid_distance(&self, centroid: &[f32], query: &[f32]) -> f64 {
        // 简化版：与search_vector使用相同距离计算
        let mut sum = 0.0;
        for (a, b) in centroid.iter().zip(query.iter()) {
            let diff = a - b;
            sum += diff * diff;
        }
        sum
    }
}
```

---

## 五、实施步骤

### Phase 1：基础接口扩展（2天）

- [ ] 扩展 `IndexKind`，新增 `VectorSorted` 变体
- [ ] 扩展 `RuntimeIndex` trait，新增 `search_vector_sorted` 方法
- [ ] 扩展 `IndexSearchCoordinator`，新增 `search_vector_sorted` 方法
- [ ] 定义 `DistanceMetric` 枚举（L2, Cosine, Inner Product）
- [ ] 编写单元测试：验证接口设计正确性

### Phase 2：IVF-SORTED索引存储（3天）

- [ ] 设计 `IVFSortedSegment` 结构
- [ ] 实现 `vector_values` 存储逻辑（每个cluster存储向量值+行地址）
- [ ] 修改 `IVF-Flat` build流程，增加向量值存储
- [ ] 修改 `IVF-Flat` 查询流程，支持向量值访问
- [ ] 编写存储格式测试

### Phase 3：IVF-SORTED插件实现（3天）

- [ ] 实现 `IVFSortedIndex::search_vector_sorted` 方法
- [ ] 实现最近中心查找算法
- [ ] 实现TOP-K最小堆维护逻辑
- [ ] 实现距离计算（L2/Cosine/Inner Product）
- [ ] 编写单元测试：验证搜索正确性

### Phase 4：标量搜索集成（2天）

- [ ] 在 `search_vector_sorted` 中调用 `search_scalar`
- [ ] 验证标量搜索返回 `Vec<RowAddress>` 可作为候选集
- [ ] 测试标量条件过滤流程

### Phase 5：回表验证（2天）

- [ ] 实现并行回表验证 `verify_scalar_conditions_parallel`
- [ ] 验证 RowAddress 精确读取路径
- [ ] 测试回表性能

### Phase 6：端到端集成（2天）

- [ ] FDW层路由决策：识别向量排序查询
- [ ] 完整测试：标量索引 + IVF-SORTED向量索引并存场景
- [ ] 性能基准测试：验证加速效果

### Phase 7：文档和测试（1天）

- [ ] 编写集成测试用例（5个场景）
- [ ] 更新 `capabilities.md`，标记为 `Implemented`
- [ ] 编写使用文档（SQL示例、性能调优）

**总工时**：15天

---

## 六、性能预期

### 6.1 执行时间分解

```
场景：WHERE a=1, b=2 ORDER BY vector_c LIMIT 5

数据规模：1000万行
索引：
  - BTree(a, b): 覆盖1000行（选择率0.01%）
  - IVF-SORTED(vector): 1024个cluster，nprobe=10

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
  │ IVF-SORTED搜索（向量排序）                            │
  │   ├─ 找nprobe个最近中心：  ~10ms                    │
  │   ├─ 倒排列表遍历：        ~30ms（1000行）           │
  │   ├─ 最小堆维护：          ~5ms                     │
  │   └─ 合计：               ~45ms                     │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 回表验证（并行，5个候选）                              │
  │   ├─ 并行读取：       ~10ms                         │
  │   ├─ 标量条件验证：    ~5ms                          │
  │   └─ 合计：           ~15ms                         │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 排序 + 返回                                            │
  │   └─ 合计：       ~2ms                             │
  └─────────────────────────────────────────────────────┘
                        ↓
  ┌─────────────────────────────────────────────────────┐
  │ 总计：           ~92ms                             │
  └─────────────────────────────────────────────────────┘
```

### 6.2 性能对比

```
当前（全表扫描+排序）：
  ┌─────────────────────────────────────────────────────┐
  │ 全表扫描：          1000万行                         │
  │ 标量过滤：          1000万行 × 2条件 = 2000万次比较 │
  │ 排序：              1000行 × log(1000) ≈ 10,000次比较│
  │ 取TOP-5：           5次读表                         │
  │ 总耗时：            ~2000ms                         │
  └─────────────────────────────────────────────────────┘

IVF-SORTED方案：
  ┌─────────────────────────────────────────────────────┐
  │ 标量索引过滤：      30ms                             │
  │ IVF排序：           45ms                             │
  │ 回表验证：          15ms                             │
  │ 排序返回：          2ms                              │
  │ 总耗时：            ~92ms                            │
  └─────────────────────────────────────────────────────┘

加速比：2000ms / 92ms ≈ 22倍
```

### 6.3 不同数据规模下的性能

| 表规模 | 标量候选数 | 当前耗时 | IVF-SORTED耗时 | 加速比 |
|--------|----------|---------|--------------|--------|
| 1000万 | 1000 | 2000ms | 92ms | 22x |
| 1亿 | 10000 | 20000ms | 200ms | 100x |
| 10亿 | 100000 | 200000ms | 800ms | 250x |

### 6.4 不同k值的性能

| K值 | 当前耗时 | IVF-SORTED耗时 | 加速比 |
|-----|---------|--------------|--------|
| 5 | 2000ms | 92ms | 22x |
| 10 | 3000ms | 120ms | 25x |
| 100 | 5000ms | 200ms | 25x |
| 1000 | 8000ms | 300ms | 27x |

**结论**：K值越大，加速比越显著（因为排序开销占比越小）

---

## 七、关键风险和应对

### 7.1 风险1：向量值存储占用空间大

**问题描述**：
每个cluster的vector_values存储完整的向量值，占用空间：

```
存储估算：
  1024个cluster
  每个cluster平均1000行
  每行向量128维，float32
  字节数 = 1024 * 1000 * 128 * 4 = 512MB

对比IVF-Flat：
  只存储centroids（1024 * 128 * 4 = 512KB）
  每个row_id 8字节
  总计 = 512KB + 1000万 * 8字节 = 80MB

差距：640倍！
```

**应对策略**：

1. **按需加载**：
   ```rust
   // 只在搜索时加载需要的vector_values
   pub async fn load_cluster_vector_values(&self, cluster_idx: usize) -> Result<Vec<(Vec<f32>, RowAddress)>> {
       // 从puffin文件按需加载
       self.vector_values_cache.entry(cluster_idx).or_insert_with(|| {
           self.load_vector_values_from_puffin(cluster_idx)
       })
   }
   ```

2. **压缩存储**：
   ```rust
   // 使用F16或INT8量化
   pub struct QuantizedVector {
       pub values: Vec<i16>,  // 量化后的值（范围 -32768 到 32767）
   }
   
   // 存储空间减少50%
   512MB → 256MB
   ```

3. **分区存储**：
   ```rust
   // 按partition存储vector_values
   // 查询时只加载相关partition的vector_values
   ```

4. **权衡**：
   - 向量值存储空间换取搜索速度
   - 可通过配置选择是否存储向量值

### 7.2 风险2：最近中心查找性能

**问题描述**：
需要计算query到1024个centroids的距离，可能成为瓶颈：

```rust
// 当前实现
let mut distances = self
    .centroids
    .iter()
    .enumerate()
    .map(|(i, centroid)| (i, self.compute_distance(centroid, query)))
    .collect();
```

**应对策略**：

1. **提前终止**：
   ```rust
   fn find_nearest_centers_with_prune(
       &self,
       query: &[f32],
       k: usize,
   ) -> Result<Vec<IVFCenter>> {
       use std::collections::BinaryHeap;
       
       let mut heap = BinaryHeap::new();  // 存距离大的
       let mut visited = HashSet::new();
       
       // 使用BFS，按距离递增搜索
       let mut queue = VecDeque::new();
       queue.push_back(self.centroids.iter().enumerate());
       
       while let Some((i, centroid)) = queue.pop_front() {
           if visited.contains(&i) { continue; }
           visited.insert(i);
           
           let dist = self.compute_distance(centroid, query);
           
           if heap.len() < k || dist > heap.peek().unwrap().distance {
               heap.push(dist);
               if heap.len() > k {
                   heap.pop();
               }
           }
           
           // 找到k个最近的后提前终止
           if heap.len() == k {
               break;
           }
       }
       
       // ...
   }
   ```

2. **近似算法**：
   ```rust
   // 随机采样100个centroids，找最近的10个
   // 然后从结果中找最近的k个
   ```

### 7.3 风险3：倒排列表与vector_values同步

**问题描述**：
inverted_lists 和 vector_values 必须严格同步，否则会导致数据不一致：

```rust
// 错误示例
inverted_lists[0] = [0, 5, 10]        // 3个row
vector_values[0] = [(v0, addr0), (v5, addr5)]  // 只有2个，不一致！
```

**应对策略**：

1. **构建时验证**：
   ```rust
   impl IVFSortedIndex {
       fn build_with_validation(&self) -> Result<()> {
           for i in 0..self.num_clusters {
               let list_len = self.inverted_lists[i].len();
               let values_len = self.vector_values[i].len();
               
               if list_len != values_len {
                   return Err(Error::InvalidSegment(format!(
                       "Inverted list and vector values mismatch at cluster {}: {} vs {}",
                       i, list_len, values_len
                   )));
               }
           }
           
           Ok(())
       }
   }
   ```

2. **存储时校验**：
   ```rust
   // 加载segment时验证
   pub async fn load_segment(&self, metadata: &IndexSegmentMetadata) -> Result<IVFSortedSegment> {
       let segment = self.load_from_puffin(metadata)?;
       
       // 验证
       segment.build_with_validation()?;
       
       Ok(segment)
   }
   ```

3. **单元测试**：
   ```rust
   #[test]
   fn test_sync_validation() {
       let mut segment = create_test_segment();
       
       // 故意破坏同步
       segment.vector_values[0].pop();
       
       // 应该抛出错误
       assert!(segment.build_with_validation().is_err());
   }
   ```

---

## 八、测试策略

### 8.1 单元测试

**存储格式测试**：
```rust
#[test]
fn test_ivf_sorted_storage() {
    let segment = create_test_segment();
    
    // 验证vector_values长度与inverted_lists一致
    assert_eq!(
        segment.vector_values.len(),
        segment.inverted_lists.len()
    );
    
    for i in 0..segment.num_clusters {
        assert_eq!(
            segment.vector_values[i].len(),
            segment.inverted_lists[i].len()
        );
    }
}
```

**搜索算法测试**：
```rust
#[test]
fn test_vector_sorted_search() {
    let index = create_test_index();
    
    // 设置标量候选集
    let allowed = vec![
        RowAddress::new("data/file1.parquet".to_string(), 10),
        RowAddress::new("data/file1.parquet".to_string(), 20),
        RowAddress::new("data/file1.parquet".to_string(), 30),
    ];
    
    // 搜索TOP-5
    let result = index.search_vector_sorted(5, &allowed, DistanceMetric::L2).unwrap();
    
    // 验证结果
    assert_eq!(result.len(), 5);
    result.sort_by_key(|c| c.distance);
    
    // 验证距离单调递增
    for i in 1..result.len() {
        assert!(result[i].distance >= result[i-1].distance);
    }
}
```

**距离计算测试**：
```rust
#[test]
fn test_distance_computation() {
    let index = create_test_index();
    
    let vec1 = vec![1.0, 2.0, 3.0];
    let vec2 = vec![4.0, 5.0, 6.0];
    
    // L2距离
    let l2 = index.compute_distance(&vec1, &vec2, DistanceMetric::L2);
    assert_eq!(l2, 27.0); // (3^2 + 3^2 + 3^2) = 27
    
    // Cosine距离
    let cosine = index.compute_distance(&vec1, &vec2, DistanceMetric::Cosine);
    // cos = (1*4+2*5+3*6)/(sqrt(1+4+9)*sqrt(16+25+36)) = 32 / (sqrt14*sqrt77)
    // cosine距离 = 1 - 0.78 = 0.22
    
    // 内积
    let inner = index.compute_distance(&vec1, &vec2, DistanceMetric::InnerProduct);
    assert_eq!(inner, 32.0);
}
```

### 8.2 集成测试

**场景1：简单向量排序**
```sql
CREATE TABLE t (id INT, a INT, b INT, vector VECTOR(3));

INSERT INTO t VALUES
  (1, 1, 2, [1.0, 2.0, 3.0]),
  (2, 1, 2, [10.0, 20.0, 30.0]),
  (3, 1, 2, [5.0, 6.0, 7.0]),
  (4, 1, 2, [100.0, 200.0, 300.0]),
  (5, 1, 2, [0.0, 0.0, 0.0]);

-- 建索引
CREATE INDEX idx_a ON t (a) USING BTREE;
CREATE INDEX idx_vector ON t (vector) USING IVF_SORTED;

-- 查询
SELECT * FROM t
WHERE a = 1 AND b = 2
ORDER BY vector
LIMIT 2;

-- 预期结果：[0.0, 0.0, 0.0] (id=5) 和 [1.0, 2.0, 3.0] (id=1)
-- 排序后：id=5, id=1
```

**场景2：多标量条件**
```sql
SELECT * FROM t
WHERE a = 1 AND b = 2 AND c > 10
ORDER BY vector
LIMIT 3;
```

**场景3：向量值过小/过大**
```sql
-- 测试边界情况
SELECT * FROM t
WHERE a = 1
ORDER BY vector
LIMIT 5;
```

**场景4：标量候选为空**
```sql
SELECT * FROM t
WHERE a = 999  -- 不存在
ORDER BY vector
LIMIT 5;
-- 预期：返回空
```

### 8.3 性能测试

**基准测试**：
```bash
python scripts/bench_complete.py \
  --query "SELECT * FROM t WHERE a=1 AND b=2 ORDER BY vector LIMIT 5" \
  --iterations 100
```

**对比测试**：
- 纯标量索引 + 全表排序 vs IVF-SORTED
- 不同表规模下的性能
- 不同k值下的性能

---

## 九、与原方案A（相似度搜索）的对比

### 9.1 场景对比

| 维度 | 原方案A（相似度搜索） | 新方案（向量排序） |
|------|-------------------|------------------|
| 查询语义 | `WHERE vector IS CLOSEST TO q` | `ORDER BY vector_c` |
| 查询向量 | 需要查询向量 `q` | 不需要（只用距离比较） |
| 索引目标 | 找最相似的k个 | 找向量最大的前k个 |
| 索引类型 | IVF-Flat（相似度） | IVF-SORTED（排序） |
| 标量作用 | 预过滤候选集 | 同上 |
| 向量作用 | 寻找最近邻 | 寻找TOP-K |

### 9.2 代码结构对比

**原方案A（相似度搜索）**：
```rust
// 扩展 VectorSearchRequest
pub struct VectorSearchRequest {
    pub query: Vec<f32>,  // 查询向量
    pub limit: usize,
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集
    // ...
}

// IVF搜索
async fn search_vector_prefiltered(
    &self,
    query: &[f32],  // 查询向量
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
    score_order: ScoreOrder,
    parameters: serde_json::Value,
) -> Result<Vec<IndexCandidate>> {
    // 找nprobe个最近中心
    let centers = self.find_nearest_centers(query, nprobe)?;
    
    // 在候选中收集向量
    for center in centers {
        for row_id in self.get_rows(center) {
            let distance = self.compute_distance(query, row_id);
            if allowed_row_ids.contains(&row_id) {
                candidates.push((row_id, distance));
            }
        }
    }
    
    // 返回TOP-K
    Ok(candidates.into_iter().take(limit).collect())
}
```

**新方案（向量排序）**：
```rust
// 扩展 RuntimeIndex trait
async fn search_vector_sorted(
    &self,
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
    metric: DistanceMetric,
) -> Result<Vec<IndexCandidate>> {
    // 不需要查询向量
    // 找nprobe个最近中心
    let centers = self.find_nearest_centers(self.search_vector, nprobe)?;
    
    // 在候选中找向量值 >= 查询值的行
    let mut heap = BinaryHeap::new();
    
    for center in centers {
        for (vector_value, row_address) in &self.vector_values[center.idx] {
            if !allowed_row_ids.contains(row_address) {
                continue;
            }
            
            let distance = self.compute_distance(vector_value, metric);
            
            if heap.len() < limit || distance < heap.peek().unwrap().distance {
                heap.push(IndexCandidate {
                    row_address: row_address.clone(),
                    distance,
                    is_overfetch: false,
                });
                
                if heap.len() > limit {
                    heap.pop();
                }
            }
        }
    }
    
    // 返回TOP-K
    let mut result = Vec::from_iter(heap);
    result.sort_by_key(|c| c.distance);
    Ok(result.into_iter().take(limit).collect())
}
```

### 9.3 共同点

1. **都使用IVF索引结构**：Centroids + 倒排列表
2. **都支持标量预过滤**：BTree索引过滤候选集
3. **都使用最小堆维护TOP-K**：保持性能
4. **都支持回表验证**：确保精确性

### 9.4 差异点

| 维度 | 原方案A | 新方案 |
|------|---------|--------|
| 查询输入 | 查询向量 `q` | 不需要查询向量 |
| 搜索目标 | 找最近的k个 | 找向量值最大的前k个 |
| 候选收集 | 找最近邻中心 + 倒排列表 | 找最近邻中心 + vector_values |
| 距离计算 | 使用查询向量 `q` | 使用向量值与搜索向量的距离 |
| 存储需求 | 不需要存储向量值 | 需要存储每个row的向量值 |

---

## 十、总结

### 10.1 方案优势

1. **加速效果显著**：22倍加速（1000万行场景）
2. **复用IVF索引**：基于现有IVF-Flat结构扩展
3. **支持多种距离度量**：L2、Cosine、Inner Product
4. **与标量索引完美集成**：复用标量多列索引能力
5. **支持任意K值**：TOP-5、TOP-10、TOP-100等都高效

### 10.2 关键设计点

1. **IVF-SORTED索引**：在IVF-Flat基础上增加vector_values存储
2. **最近中心查找**：nprobe个最近中心 + 倒排列表遍历
3. **TOP-K最小堆**：动态维护，保证性能
4. **向量值存储**：按cluster组织，节省空间

### 10.3 实施优先级

**高优先级**（核心路径）：
1. Phase 1：基础接口扩展
2. Phase 2：IVF-SORTED索引存储
3. Phase 3：IVF-SORTED插件实现
4. Phase 6：端到端集成

**中优先级**（性能提升）：
5. Phase 4：标量搜索集成
6. Phase 5：回表验证优化
7. Phase 7：文档和测试

**低优先级**（优化）：
- 向量值压缩存储
- 最近中心查找优化

### 10.4 性能预期

| 表规模 | 当前耗时 | IVF-SORTED耗时 | 加速比 |
|--------|---------|--------------|--------|
| 1000万 | 2000ms | 92ms | 22x |
| 1亿 | 20000ms | 200ms | 100x |
| 10亿 | 200000ms | 800ms | 250x |

---

## 附录

### A. 参考文档

- [多列BTree索引设计方案.md](../端到端性能测试/多列BTree索引设计方案.md) - 标量多列索引设计
- [能力与限制矩阵](../code/iceberg-index/docs/reference/capabilities.md) - 当前支持能力
- [心智模型](../code/iceberg-index/docs/concepts/mental-model.md) - 核心概念

### B. 相关Issue

- 向量排序索引需求
- ORDER BY vector 优化

### C. 测试用例

详见[测试策略](#八、测试策略)

### D. 性能基准

详见[性能预期](#六、性能预期)
