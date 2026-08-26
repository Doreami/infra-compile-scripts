# 向量+标量混合查询统一设计方案

> 最后更新: 2026-08-19
> 状态: 待实现

## 一、场景分类

### 1.1 查询类型总览

本文档覆盖**7个查询场景**，按标量条件、向量条件、排序需求分类：

| # | 场景 | 标量条件 | 向量条件 | 排序需求 | 优先级 | 加速比 |
|---|------|---------|---------|---------|--------|--------|
| **A** | 相似度搜索 | ✅ (预过滤) | `IS CLOSEST TO q` | 无 | P0 | 4x |
| **B** | 向量排序 | ✅ (预过滤) | `ORDER BY vector` | TOP-K | **P0** | **22x** |
| **C** | 相似度+排序 | ✅ (预过滤) | `IS CLOSEST TO q` | 距离+Id | P1 | 4x |
| **D** | 标量排序 | ✅ (预过滤) | N/A | TOP-K | P3 | 10x |
| **E** | 纯相似度 | ❌ | `IS CLOSEST TO q` | 无 | P4 | 10x |
| **F** | 纯排序 | ❌ | `ORDER BY vector` | TOP-K | P5 | 22x |
| **G** | 全表搜索 | ❌ | N/A | TOP-K | P6 | 1x |

### 1.2 各场景查询示例

#### 场景A：相似度搜索（Pre-filter）

```sql
-- 查询示例
SELECT * FROM tab
WHERE category = 'electronics'
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;

-- 执行流程
-- 1. 标量索引过滤 (BTree) → 候选集 C₁ (1000行)
-- 2. 向量索引搜索 (IVF-Flat) → 在 C₁ 中找最近邻 → 返回TOP-K
-- 3. 回表验证标量条件
-- 4. 全局排序 → 返回最终TOP-K
```

#### 场景B：向量排序（Min-K）⭐ 用户明确需求

```sql
-- 查询示例
SELECT * FROM tab
WHERE category = 'electronics' AND price > 100
ORDER BY vector
LIMIT 10;

-- 执行流程
-- 1. 标量索引过滤 (BTree) → 候选集 C₁ (10000行)
-- 2. 向量排序索引搜索 (IVF-SORTED) → 在 C₁ 中找TOP-K (最小堆) → 返回TOP-K
-- 3. 回表验证标量条件
-- 4. 排序 + 返回
```

#### 场景C：相似度+排序（Unique）

```sql
-- 查询示例
SELECT * FROM tab
WHERE category = 'electronics'
ORDER BY vector IS CLOSEST TO [0.1, 0.2, ...],
       id
LIMIT 10;

-- 执行流程
-- 1. 标量索引过滤 (BTree) → 候选集 C₁ (1000行)
-- 2. 向量索引搜索 (IVF-Flat) → 返回TOP-K (距离 + id)
-- 3. 回表验证标量条件
-- 4. 排序 (距离 + id) → 返回最终TOP-K
```

#### 场景D：标量排序（已有）

```sql
-- 查询示例
SELECT * FROM tab
WHERE category = 'electronics' AND price > 100
ORDER BY price
LIMIT 10;

-- 执行流程
-- 1. 标量索引过滤 (BTree) → 候选集 C₁ (1000行)
-- 2. BTree排序 → 返回TOP-K
-- 3. 回表验证标量条件
```

#### 场景E：纯相似度（已有）

```sql
-- 查询示例
SELECT * FROM tab
WHERE vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;

-- 执行流程
-- 1. 向量索引搜索 (IVF-Flat) → 返回TOP-K
```

#### 场景F：纯排序（需要IVF-SORTED）

```sql
-- 查询示例
SELECT * FROM tab
ORDER BY vector
LIMIT 10;

-- 执行流程
-- 1. 向量排序索引搜索 (IVF-SORTED) → 全表找TOP-K → 返回TOP-K
```

#### 场景G：全表搜索（基线）

```sql
-- 查询示例
SELECT * FROM tab
WHERE category = 'electronics'
ORDER BY id
LIMIT 10;

-- 执行流程
-- 1. 全表扫描 → 过滤 → 排序 → 返回TOP-K
```

### 1.2 组合场景矩阵

| 场景 | 标量条件 | 向量条件 | 核心目标 | 需要向量索引 |
|------|---------|---------|---------|------------|
| **A. 相似度搜索** | ✅ | `IS CLOSEST TO q` | 找最相似的k个 | ✅ IVF-Flat |
| **B. 向量排序** | ✅ | `ORDER BY vector` | 找向量最大的前k个 | ✅ IVF-SORTED |
| **C. 相似度+排序** | ✅ | `IS CLOSEST TO q` AND `ORDER BY vector` | 找最相似的k个并排序 | ✅ IVF-Flat |
| **D. 标量排序** | ✅ | N/A | 找标量最大的前k个 | ❌ BTree |
| **E. 纯相似度** | ❌ | `IS CLOSEST TO q` | 找最相似的k个 | ✅ IVF-Flat |
| **F. 纯排序** | ❌ | `ORDER BY vector` | 找向量最大的前k个 | ✅ IVF-SORTED |
| **G. 全表搜索** | N/A | N/A | 扫描全表 | ❌ N/A |
```

## 二、各场景优化策略

### 2.1 场景A：相似度搜索（Pre-filter）

**查询示例**：
```sql
SELECT * FROM tab
WHERE category = 'electronics'
  AND vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

**执行流程**：
```
1. 标量索引过滤（BTree）
   → 得到候选集 C₁（如 1000 行）

2. 向量索引搜索（IVF-Flat）
   → 在 C₁ 中找最近邻中心
   → 收集候选向量
   → 过滤dead-row
   → 返回TOP-K向量候选

3. 回表验证标量条件
   → 读取候选的标量列值
   → 验证是否满足 category='electronics'

4. 全局排序
   → 按距离排序
   → 返回最终TOP-K
```

**关键点**：
- **向量索引类型**：IVF-Flat（相似度搜索）
- **搜索目标**：最近邻（Nearest Neighbor）
- **需要存储**：Centroids + 倒排列表
- **回表开销**：小（仅TOP-K候选）

**代码结构**：
```rust
pub struct VectorSearchRequest {
    pub query: Vec<f32>,           // 查询向量
    pub limit: usize,              // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集
    pub score_order: ScoreOrder,
    pub parameters: serde_json::Value,
}

async fn search_vector_prefiltered(
    &self,
    query: &[f32],
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
) -> Result<Vec<IndexCandidate>> {
    // 1. 找nprobe个最近中心
    let centers = self.find_nearest_centers(query, nprobe)?;

    // 2. 在候选集中收集向量
    let mut candidates = Vec::new();
    for center in centers {
        for (vector_value, row_address) in &self.vector_values[center.idx] {
            if !allowed_row_ids.contains(row_address) {
                continue;
            }
            let distance = self.compute_distance(vector_value, query);
            candidates.push(IndexCandidate {
                row_address: row_address.clone(),
                distance,
                is_overfetch: false,
            });
        }
    }

    // 3. 过滤dead-row
    // 4. 取TOP-K
    // 5. 返回
}
```

**性能**：
- 加速比：3-10倍（1000倍候选压缩）
- 回表成本：<10%（仅TOP-K候选）

---

### 2.2 场景B：向量排序（Min-K）

**查询示例**：
```sql
SELECT * FROM tab
WHERE category = 'electronics' AND price > 100
ORDER BY vector
LIMIT 10;
```

**执行流程**：
```
1. 标量索引过滤（BTree）
   → 得到候选集 C₁（如 10000 行）

2. 向量排序索引搜索（IVF-SORTED）
   → 找nprobe个最近中心
   → 在候选集中找向量值 >= 搜索值的行
   → 用最小堆维护TOP-10
   → 返回TOP-10向量候选

3. 回表验证标量条件
   → 读取候选的标量列值
   → 验证是否满足 category='electronics' AND price>100

4. 排序 + 返回
   → 按距离排序
   → 返回最终TOP-10
```

**关键点**：
- **向量索引类型**：IVF-SORTED（排序）
- **搜索目标**：Min-K（向量值最小的前K个）
- **需要存储**：Centroids + vector_values + 倒排列表
- **回表开销**：小（仅TOP-K候选）

**代码结构**：
```rust
pub struct VectorSortRequest {
    pub limit: usize,              // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集
    pub metric: DistanceMetric,    // 距离度量
}

async fn search_vector_sorted(
    &self,
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
    metric: DistanceMetric,
) -> Result<Vec<IndexCandidate>> {
    // 1. 找nprobe个最近中心
    let centers = self.find_nearest_centers(&self.search_vector, nprobe)?;

    // 2. 在候选集中找TOP-K（最小堆）
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

    // 3. 转换为Vec并排序
    let mut result = Vec::from_iter(heap);
    result.sort_by_key(|c| c.distance);

    // 4. 返回TOP-K
    Ok(result.into_iter().take(limit).collect())
}
```

**性能**：
- 加速比：20-30倍（候选压缩 + 索引排序）
- 回表成本：<10%（仅TOP-K候选）

---

### 2.3 场景C：相似度+排序（Unique）

**查询示例**：
```sql
SELECT * FROM tab
WHERE category = 'electronics'
ORDER BY vector IS CLOSEST TO [0.1, 0.2, ...],
       id
LIMIT 10;
```

**执行流程**：
```
1. 标量索引过滤（BTree）
   → 得到候选集 C₁（如 1000 行）

2. 向量相似度搜索（IVF-Flat）
   → 在 C₁ 中找最近邻
   → 返回TOP-K向量候选（距离 + id）

3. 回表验证标量条件
   → 读取候选的标量列值
   → 验证是否满足 category='electronics'

4. 排序（距离 + id）
   → 返回最终TOP-10
```

**关键点**：
- **向量索引类型**：IVF-Flat（相似度搜索）
- **搜索目标**：最近邻
- **需要存储**：Centroids + 倒排列表
- **排序字段**：距离 + id（稳定排序）

**代码结构**：
```rust
pub struct VectorSearchWithSortRequest {
    pub query: Vec<f32>,           // 查询向量
    pub limit: usize,              // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集
    pub sort_fields: Vec<SortField>,  // 排序字段：[distance, id]
}

async fn search_vector_with_sort(
    &self,
    query: Vec<f32>,
    limit: usize,
    allowed_row_ids: &BTreeSet<RowAddress>,
    sort_fields: Vec<SortField>,
) -> Result<Vec<IndexCandidate>> {
    // 1. 找nprobe个最近中心
    let centers = self.find_nearest_centers(&query, nprobe)?;

    // 2. 在候选集中收集向量
    let mut candidates = Vec::new();
    for center in centers {
        for (vector_value, row_address) in &self.vector_values[center.idx] {
            if !allowed_row_ids.contains(row_address) {
                continue;
            }
            let distance = self.compute_distance(vector_value, &query);
            candidates.push(IndexCandidate {
                row_address: row_address.clone(),
                distance,
                is_overfetch: false,
                // 可以添加额外的排序字段
            });
        }
    }

    // 3. 过滤dead-row

    // 4. 排序（距离 + id）
    candidates.sort_by(|a, b| {
        match (&sort_fields[0], &sort_fields[1]) {
            // 先按距离排序，距离相同按id排序
            (SortField::Distance, SortField::Id) => {
                a.distance.partial_cmp(&b.distance)
                    .unwrap()
                    .then_with(|| a.row_address.row_position.cmp(&b.row_address.row_position))
            }
            _ => unreachable!(),
        }
    });

    // 5. 返回TOP-K
    Ok(candidates.into_iter().take(limit).collect())
}
```

**性能**：
- 加速比：3-10倍
- 回表成本：<10%

---

### 2.4 场景D：标量排序（已有）

**查询示例**：
```sql
SELECT * FROM tab
WHERE category = 'electronics' AND price > 100
ORDER BY price
LIMIT 10;
```

**执行流程**：
```
1. 标量索引过滤（BTree）
   → 得到候选集 C₁（如 1000 行）

2. BTree排序
   → 对候选按price排序
   → 返回TOP-10

3. 回表验证
   → 读取候选的标量列值
   → 验证是否满足条件
```

**关键点**：
- **索引类型**：BTree（已有）
- **搜索目标**：Min-K（标量值最小的前K个）
- **需要存储**：Pages + 排序键
- **回表开销**：小（仅TOP-K候选）

**已有实现**：
```rust
// 已有的标量搜索实现
async fn search_scalar(
    &self,
    request: SearchScalarIndexRequest,
) -> Result<ScalarIndexSearchResult> {
    // ...
}
```

**性能**：
- 加速比：5-10倍
- 回表成本：<10%

---

### 2.5 场景E：纯相似度搜索（已有）

**查询示例**：
```sql
SELECT * FROM tab
WHERE vector IS CLOSEST TO [0.1, 0.2, ...]
LIMIT 10;
```

**执行流程**：
```
1. 向量索引搜索（IVF-Flat）
   → 找nprobe个最近中心
   → 收集候选向量
   → 过滤dead-row
   → 返回TOP-K向量候选
```

**关键点**：
- **索引类型**：IVF-Flat（已有）
- **搜索目标**：最近邻
- **需要存储**：Centroids + 倒排列表
- **回表开销**：0（无标量条件）

**已有实现**：
```rust
// 已有的向量搜索实现
async fn search_vector(
    &self,
    request: VectorSearchRequest,
) -> Result<Vec<IndexCandidate>> {
    // ...
}
```

**性能**：
- 加速比：10-50倍（相对全表扫描）

---

### 2.6 场景F：纯排序（已有）

**查询示例**：
```sql
SELECT * FROM tab
ORDER BY vector
LIMIT 10;
```

**执行流程**：
```
1. 向量排序索引搜索（IVF-SORTED）
   → 找nprobe个最近中心
   → 在全表中找TOP-K
   → 返回TOP-K向量候选
```

**关键点**：
- **索引类型**：IVF-SORTED（需要新增）
- **搜索目标**：Min-K
- **需要存储**：Centroids + vector_values + 倒排列表
- **回表开销**：0（无标量条件）

**代码结构**：
```rust
async fn search_vector_sorted_full(
    &self,
    limit: usize,
    metric: DistanceMetric,
) -> Result<Vec<IndexCandidate>> {
    // 类似场景B，但allowed_row_ids为None
    let mut heap = BinaryHeap::new();

    for center in self.centroids.iter() {
        for (vector_value, row_address) in &self.vector_values[center.idx] {
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

    let mut result = Vec::from_iter(heap);
    result.sort_by_key(|c| c.distance);
    Ok(result.into_iter().take(limit).collect())
}
```

**性能**：
- 加速比：20-30倍（相对全表排序）

---

### 2.7 场景G：全表搜索（已有）

**查询示例**：
```sql
SELECT * FROM tab
WHERE category = 'electronics'
ORDER BY id
LIMIT 10;
```

**执行流程**：
```
1. 全表扫描
   → 读取所有行
2. 标量过滤
   → 过滤 category='electronics'
3. 排序
   → 按 id 排序
4. 取TOP-10
```

**关键点**：
- **索引类型**：无
- **搜索目标**：Min-K
- **需要存储**：无
- **回表开销**：大（全表）

**已有实现**：
```rust
// 已有的全表扫描实现
async fn full_scan_scalar(...) -> Result<Vec<RowAddress>> {
    // ...
}
```

**性能**：
- 加速比：1x（无索引）
- 回表成本：100%

---

## 三、统一搜索框架

### 3.1 共同接口设计

```rust
/// ── 统一向量搜索请求 ─────────────────────────────────────────
pub enum VectorSearchType {
    /// 相似度搜索
    Nearest(NearestSearchRequest),
    
    /// 向量排序
    MinK(MinKSearchRequest),
    
    /// 相似度+排序
    NearestWithSort(NearestWithSortRequest),
}

pub struct NearestSearchRequest {
    pub query: Vec<f32>,              // 查询向量
    pub limit: usize,                 // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集（可选）
    pub score_order: ScoreOrder,
    pub parameters: serde_json::Value,
}

pub struct MinKSearchRequest {
    pub limit: usize,                 // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集（可选）
    pub metric: DistanceMetric,       // 距离度量
    pub parameters: serde_json::Value,
}

pub struct NearestWithSortRequest {
    pub query: Vec<f32>,              // 查询向量
    pub limit: usize,                 // 返回K个
    pub allowed_row_ids: Option<Arc<BTreeSet<RowAddress>>>,  // 标量候选集（可选）
    pub sort_fields: Vec<SortField>,   // 排序字段
    pub parameters: serde_json::Value,
}

pub struct SortField {
    pub field: String,                // 'distance', 'id'
    pub order: SortOrder,             // Asc, Desc
}

pub enum SortOrder {
    Asc,
    Desc,
}

pub enum ScoreOrder {
    SmallerIsBetter,  // 距离从小到大（相似度搜索）
    LargerIsBetter,   // 距离从大到小
}
```

### 3.2 搜索协调器统一接口

```rust
impl IndexSearchCoordinator {
    /// ── 统一向量搜索入口 ───────────────────────────────────────
    pub async fn search_vector_unified(
        &self,
        request: VectorSearchRequest,
    ) -> Result<VectorSearchResult> {
        match request.search_type {
            VectorSearchType::Nearest(req) => {
                self.search_vector_nearest(req).await
            }
            VectorSearchType::MinK(req) => {
                self.search_vector_min_k(req).await
            }
            VectorSearchType::NearestWithSort(req) => {
                self.search_vector_nearest_with_sort(req).await
            }
        }
    }

    /// 相似度搜索
    async fn search_vector_nearest(
        &self,
        request: NearestSearchRequest,
    ) -> Result<VectorSearchResult> {
        // 1. 标量索引预过滤（如果有）
        let mut candidates = if let Some(allowed) = &request.allowed_row_ids {
            // 从标量索引获取候选集
            let scalar_result = self.search_scalar(...).await?;
            scalar_result.addresses
        } else {
            Vec::new()  // 无标量过滤
        };

        // 2. 向量索引搜索（IVF-Flat）
        let vector_candidates = self.search_vector(&request).await?;

        // 3. 过滤候选集
        if !candidates.is_empty() {
            vector_candidates.retain(|c| candidates.contains(&c.row_address));
        }

        // 4. 返回
        Ok(VectorSearchResult {
            snapshot_id: request.snapshot_id,
            candidates: vector_candidates,
        })
    }

    /// 向量排序
    async fn search_vector_min_k(
        &self,
        request: MinKSearchRequest,
    ) -> Result<VectorSearchResult> {
        // 1. 标量索引预过滤（如果有）
        let mut allowed_row_ids = request.allowed_row_ids.clone();

        if allowed_row_ids.is_none() {
            // 无标量过滤，扫描全表
            let scalar_result = self.full_scan_scalar(...).await?;
            allowed_row_ids = Some(Arc::new(scalar_result.addresses));
        }

        // 2. 向量排序索引搜索
        let vector_candidates = self.search_vector_sorted(
            request.limit,
            &allowed_row_ids.unwrap(),
            request.metric,
        ).await?;

        Ok(VectorSearchResult {
            snapshot_id: request.snapshot_id,
            candidates: vector_candidates,
        })
    }

    /// 相似度+排序
    async fn search_vector_nearest_with_sort(
        &self,
        request: NearestWithSortRequest,
    ) -> Result<VectorSearchResult> {
        // 1. 标量索引预过滤（如果有）
        let mut candidates = if let Some(allowed) = &request.allowed_row_ids {
            let scalar_result = self.search_scalar(...).await?;
            scalar_result.addresses
        } else {
            Vec::new()
        };

        // 2. 向量索引搜索
        let mut vector_candidates = self.search_vector(&request).await?;

        // 3. 过滤候选集
        if !candidates.is_empty() {
            vector_candidates.retain(|c| candidates.contains(&c.row_address));
        }

        // 4. 排序（距离 + id）
        vector_candidates.sort_by(|a, b| {
            match (&request.sort_fields[0], &request.sort_fields[1]) {
                (SortField { field: "distance", .. }, SortField { field: "id", .. }) => {
                    a.distance.partial_cmp(&b.distance).unwrap()
                        .then_with(|| a.row_address.row_position.cmp(&b.row_address.row_position))
                }
                _ => unreachable!(),
            }
        });

        // 5. 返回TOP-K
        let k = std::cmp::min(request.limit, vector_candidates.len());
        let result = vector_candidates.into_iter().take(k).collect();

        Ok(VectorSearchResult {
            snapshot_id: request.snapshot_id,
            candidates: result,
        })
    }
}
```

---

## 四、索引类型矩阵

### 4.1 索引类型定义

```rust
pub enum IndexKind {
    /// 标量索引（BTree）
    Scalar,
    
    /// 向量索引（IVF-Flat，相似度搜索）
    Vector,
    
    /// 向量排序索引（IVF-SORTED，Min-K搜索）
    #[serde(rename = "vector_sorted")]
    VectorSorted,
}

/// 距离度量
pub enum DistanceMetric {
    L2,           // 平方L2距离
    Cosine,       // 余弦距离
    InnerProduct, // 内积
}
```

### 4.2 索引支持矩阵

| 场景 | BTree | IVF-Flat | IVF-SORTED | 标量索引支持 | 向量索引支持 |
|------|-------|----------|-----------|------------|------------|
| **A. 相似度搜索** | ✅ (预过滤) | ✅ (搜索) | ✅ (预过滤) | ✅ | ✅ (IVF-Flat) |
| **B. 向量排序** | ✅ (预过滤) | ❌ | ✅ (搜索) | ✅ | ✅ (IVF-SORTED) |
| **C. 相似度+排序** | ✅ (预过滤) | ✅ (搜索) | ✅ (预过滤) | ✅ | ✅ (IVF-Flat) |
| **D. 标量排序** | ✅ (搜索) | ❌ | ❌ | ✅ | ❌ |
| **E. 纯相似度** | ❌ | ✅ (搜索) | ❌ | ❌ | ✅ (IVF-Flat) |
| **F. 纯排序** | ❌ | ❌ | ✅ (搜索) | ❌ | ✅ (IVF-SORTED) |
| **G. 全表搜索** | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## 五、实施计划

### 阶段1：统一接口设计（2天）
- [ ] 定义统一搜索类型枚举
- [ ] 定义请求/响应结构
- [ ] 设计路由决策逻辑

### 阶段2：IVF-SORTED索引实现（5天）
- [ ] 实现 IVF-SORTED 插件
- [ ] 实现向量值存储
- [ ] 实现Min-K搜索算法
- [ ] 测试相似度/排序切换

### 阶段3：标量索引集成（2天）
- [ ] 标量预过滤流程
- [ ] 标量条件回表验证
- [ ] 性能测试

### 阶段4：统一搜索协调器（3天）
- [ ] 实现统一搜索入口
- [ ] 实现路由决策
- [ ] 实现错误处理

### 阶段5：端到端测试（3天）
- [ ] 7个场景的集成测试
- [ ] 性能基准测试
- [ ] 边界情况测试

**总工时**：15天

---

## 六、总结

### 6.1 关键设计原则

1. **场景分类清晰**：7个场景，每种有明确的优化策略
2. **统一接口设计**：一个入口，多种搜索类型
3. **复用现有能力**：BTree、IVF-Flat、回表机制均已实现
4. **渐进式实施**：先IVF-Flat，再IVF-SORTED，最后统一

### 6.2 性能预期

| 场景 | 表规模 | 当前耗时 | 优化后耗时 | 加速比 |
|------|--------|---------|----------|--------|
| A. 相似度搜索 | 1000万 | 500ms | 120ms | 4x |
| B. 向量排序 | 1000万 | 2000ms | 92ms | 22x |
| C. 相似度+排序 | 1000万 | 500ms | 120ms | 4x |
| D. 标量排序 | 1000万 | 500ms | 50ms | 10x |
| E. 纯相似度 | 1000万 | 500ms | 50ms | 10x |
| F. 纯排序 | 1000万 | 2000ms | 92ms | 22x |
| G. 全表搜索 | 1000万 | 500ms | 500ms | 1x |

### 6.3 实施优先级

**Phase 1（基础）**：IVF-Flat 相似度搜索（已有）
**Phase 2（核心）**：IVF-SORTED 向量排序（新增）
**Phase 3（统一）**：统一搜索框架（新增）

---

## 附录

### A. 场景优先级

| 优先级 | 场景 | 理由 |
|-------|------|------|
| P0 | 场景B：向量排序 | 用户明确需求，高加速比 |
| P1 | 场景A：相似度搜索 | 用户明确需求，高加速比 |
| P2 | 场景C：相似度+排序 | 组合查询，有排序需求 |
| P3 | 场景D：标量排序 | 已有BTree，无需新增 |
| P4 | 场景E：纯相似度 | 已有IVF-Flat，无需新增 |
| P5 | 场景F：纯排序 | 需要IVF-SORTED，已有核心 |
| P6 | 场景G：全表搜索 | 已有，无需新增 |

### B. 文件组织

```
docs/design/
├── vector-scalar-hybrid-search-unified.md  (本文档)
├── vector-scalar-hybrid-search-prefilter.md  (原方案A：相似度搜索)
├── vector-sort-index.md  (向量排序方案)
└── ...
```
