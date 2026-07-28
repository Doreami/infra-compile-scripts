# 进程级 Metadata/Registry 缓存架构设计（v4）

> issue #89：caller-runtime 下 metadata/Registry 复用、single-flight

---

## 1. 问题

```
match → load_view() → metadata I/O + Registry I/O
plan  → load_view() → metadata I/O + Registry I/O   ← 与上一行相同
scan  → load_view() → metadata I/O + Registry I/O   ← 与上一行相同
```

同一 metadata_location 在同一 query 内被重复加载 3 次。DOP=N 时放大 N 倍。

## 2. 方案

桥接层增加一个进程级缓存。只此一级。

为什么一级够了：

| 维度 | 数据 |
|---|---|
| L2 Mutex 查找开销 | ~100 ns |
| 三阶段各查一次 | ~300 ns |
| 端到端查询时延 (MinIO) | ~4,000,000 ns |
| 缓存锁开销占比 | **<0.01%** |

没必要再加 TLS 线程级缓存省这 300ns。

```
优化前: match/plan/scan → 各 1 次远端 I/O = 3 次
优化后: match/plan/scan → 1 次远端 I/O + 3 次 Mutex 查找 ≈ 1 次远端 I/O
```

---

## 3. 架构

```
openGauss worker thread
  → iceberg_fdw
    → C ABI 入口
      → SnapshotCacheKey::from_request(storage, ns, table, metadata_location)
      → SNAPSHOT_CACHE.get_or_load(key, || service.resolve_metadata_snapshot(...))
            │
            ├─ hit  → ~100ns, 直接返回
            └─ miss → single-flight
                        → 第一个 caller: service.resolve_metadata_snapshot()
                                           → metadata JSON GET
                                           → Registry Puffin GET
                        → 其余 caller: 等待 mutex, 共享结果
      → service.search_vector_by_metadata(...)
```

---

## 4. 不变量

```
L2 不持有 Runtime / Table / View / Future
single-flight: 同 key 并发只触发 1 次远端加载
error 不缓存, 后续可重试
```

---

## 5. 数据结构

### 5.1 缓存键

```rust
/// 一个 key 对应一个 (storage_scope, metadata_file)。
/// Phase 1: target_snapshot = current snapshot。
pub struct SnapshotCacheKey {
    pub scope_hash: [u8; 16],
    pub table_namespace: Vec<String>,
    pub table_name: String,
    pub metadata_location: String,
}
```

### 5.2 StorageScope

```rust
/// 确定性派生, 不依赖 handle 实例 ID。
pub struct StorageScope {
    pub endpoint: String,
    pub warehouse: String,
    pub tenant: String,
    pub credential_hash: [u8; 16],   // SHA-256 前 16B, 不存原文
}
```

### 5.3 缓存值

```rust
/// 纯数据, 不含 Runtime/Table/View。
pub struct ResolvedMetadataSnapshot {
    pub table_namespace: Vec<String>,
    pub table_name: String,
    pub metadata_location: String,
    pub table_uuid: String,
    pub snapshot_id: i64,
    pub table_metadata: Arc<TableMetadata>,
    pub registry: Option<Arc<SnapshotIndexRegistry>>,
    pub approximate_bytes: usize,
}
```

### 5.4 缓存

```rust
pub struct ResolvedMetadataSnapshotCache {
    entries: Mutex<LruCache<SnapshotCacheKey, Arc<ResolvedMetadataSnapshot>>>,
    loading_slots: ParkingMutex<HashMap<Key, Arc<TokioMutex<()>>>>,
    max_entries: usize,           // 128
    max_bytes: usize,             // 128 MiB
    max_entry_bytes: usize,       // max_bytes / 4, 超限 bypass
    current_bytes: AtomicU64,
    slot_count: AtomicU64,
    counters: Counters,
}
```

### 5.5 计数器

```
cache_hit, cache_miss, singleflight_wait, load_total, load_error, evict_total, bypass_total
```

---

## 6. 两阶段 API (index-abi)

```rust
impl MetadataIndexService {
    /// Phase 1: 读 metadata JSON + 解析 Registry, 返回纯数据
    pub async fn resolve_metadata_snapshot(
        &self, table_namespace, table_name, metadata_location, file_io_config
    ) -> Result<ResolvedMetadataSnapshot>;

    /// Phase 2: 在当前 Runtime 创建 IndexedTableView
    pub async fn bind_resolved_snapshot(
        &self, snapshot, file_io
    ) -> Result<IndexedTableView>;
}
```

---

## 7. Single-flight 语义

| 场景 | 行为 |
|---|---|
| leader 正常 | 结果写入缓存, 通知等待者 |
| leader cancel | RAII 清理 slot, 等待者重新竞选 |
| leader error | 不缓存, 等待者重新竞选 |
| oversized entry | bypass 直接返回, 不缓存 |
| slot 满 | 降级, 调用方直接加载 |

---

## 8. 调用链路

```
search_vector_by_metadata(storage, request)
  │
  ├─ scope_hash = valid_storage(storage).scope.scope_hash()
  ├─ key = SnapshotCacheKey { scope_hash, ns, table, metadata_location }
  │
  ├─ SNAPSHOT_CACHE.get_or_load(key, || {
  │     service.resolve_metadata_snapshot(ns, table, metadata_location, config)
  │   })
  │   ├─ 命中: ~100ns
  │   └─ 未命中 + single-flight: 1 次远端加载
  │
  └─ service.search_vector_by_metadata(&sdk_req)
```

---

## 9. 改动范围

| 文件 | 说明 |
|---|---|
| `iceberg-index-abi/src/snapshot_cache.rs` | 数据类型: `SnapshotCacheKey`, `ResolvedMetadataSnapshot`, `CountersSnapshot` |
| `iceberg-index-abi/src/metadata_ops.rs` | `MetadataIndexService` +2 public 方法 |
| `iceberg-index-abi/src/lib.rs` | 导出新类型 |
| `iceberg-rust-bridge/src/sdk/storage_scope.rs` | `StorageScope` |
| `iceberg-rust-bridge/src/sdk/storage.rs` | `IcebergBridgeStorage` + `scope` 字段 |
| `iceberg-rust-bridge/src/services/index/snapshot_cache.rs` | 缓存实现 + single-flight + 计数器 |
| `iceberg-rust-bridge/src/services/index/metadata_abi.rs` | 静态单例 + 三个读入口接入 |

**仅改 bridge 层 + index-abi 数据类型，不改 PluginContext、不改 IndexLoader、不改 index-core。**

---

## 10. 不涉及

- ❌ TLS 线程级缓存 — 一级就够了
- ❌ `PluginContext` / `LoadedSegmentKey` 改动 — 不改 index 内部 API
- ❌ `get_or_bind_table` — 无调用方
- ❌ `target_snapshot_id` — Phase 1 只支持 current snapshot

---

## 11. 版本历史

| 版本 | 变更 |
|---|---|
| v1 | 初版：两级缓存 |
| v2 | re-review：双层 Key、StorageScope、IndexLoader 隔离、bind 零 I/O、state machine |
| v3 | 精简：砍 PluginContext/LoadedSegmentKey、合并双层 Key、简化 L1 |
| v4 | **单级**：砍掉 TLS L1，只保留进程级缓存。300ns 锁开销在 4ms 查询中占比 <0.01% |
