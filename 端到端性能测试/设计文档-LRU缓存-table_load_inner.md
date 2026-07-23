# 设计文档：bridge 层 TableMetadata LRU 缓存

> 2026-07-24, iceberg-rust-bridge, commit `2804a3e`

## 问题

每次 IVFPQ 向量查询，GaussDB 通过 bridge 至少发起两次独立的 C ABI 调用：

```
plan_table_scan  ──→ table_load_inner() ──→ StaticTable::from_metadata_file()
                                               ├─ TableMetadata::read_from()  ← 读 metadata.json
                                               └─ .into_table()               ← no-op

table_scan_open  ──→ table_load_inner() ──→ StaticTable::from_metadata_file()  ← 同一文件再读一遍
```

`table_load_inner` 的核心开销是 `StaticTable::from_metadata_file()`：

1. **磁盘 I/O**：`TableMetadata::read_from(file_io, metadata_location)` 从存储读取完整的 metadata JSON 文件
2. **JSON 反序列化**：将 JSON 解析为 `TableMetadata` 结构体（包含 schemas、partition_specs、snapshots、properties 等）
3. **Table 构造**：`Table::builder().build()` 纯内存操作，开销可忽略

同一张表的同一个 snapshot，metadata.json 内容完全不变。每次查询都重新读取和解析是冗余的。

## 方案

在 `table_load_inner` 前增加进程级 LRU 缓存，缓存已加载的 `TableMetadataRef`（`Arc<TableMetadata>`），以 `metadata_location` 为键。

### 缓存位置

```
GaussDB (C)
  │
  └─ iceberg_rust_bridge (Rust .so)
       │
       ├─ METADATA_CACHE ─── 进程级全局 LRU ← 本次新增
       │   └─ LruCache<String, TableMetadataRef> (容量 64)
       │
       ├─ table_load_inner()
       │   ├─ 快路径: cache.get(metadata_location) → 命中则直接构建 Table
       │   └─ 慢路径: StaticTable::from_metadata_file() → 读文件 + 反序列化
       │
       └─ iceberg-index (C ABI)
           └─ IndexEngine::metadata_cache ─── 已有，缓存 IndexedTableView
```

与本层（bridge）和下层（iceberg-index）的缓存关系：

| 层级                             | 缓存内容                       | 键                   | 说明                                   |
| ------------------------------ | -------------------------- | ------------------- | ------------------------------------ |
| **bridge** `METADATA_CACHE`    | `Arc<TableMetadata>`       | `metadata_location` | **本次新增**，缓存 metadata.json 解析结果       |
| iceberg-index `metadata_cache` | `Arc<IndexedTableView>`    | `metadata_location` | 已有，缓存 Puffin registry + Coordinators |
| iceberg-index `table_for()`    | `Arc<IcebergIndexedTable>` | `TableId`           | 已有，HashMap 缓存 catalog 表 handle       |

三者独立，缓存不同层级的对象。bridge 缓存的是最底层的 TableMetadata（所有操作的基础）；iceberg-index 缓存的是索引搜索所需的 IndexedTableView（含 registry、coordinator）。

### 线程安全设计

Bridge 采用 caller-thread 执行模型：每个 OS 线程拥有独立的 tokio runtime。`METADATA_CACHE` 是 `static LazyLock<Mutex<LruCache>>`，跨线程共享：

- `LazyLock`：首次访问时惰性初始化，确保所有线程看到同一个实例
- `Mutex`：`lru::LruCache::get()` 需要 `&mut self`（更新 LRU 访问序），所以需要独占锁。临界区极短（HashMap 查询 + Arc clone），锁竞争可忽略
- 缓存值 `Arc<TableMetadata>`：允许并发读
- 缓存键 `String`：`metadata_location` 在同一个 snapshot 下不变

`IcebergBridgeTable` 仍保持线程亲和设计（`owner_thread: ThreadId`），因为每次 `table_load_inner` 返回时都新建 `IcebergBridgeTable`，设置当前线程 ID。

### Mutex 毒化处理

Rust 的 `std::sync::Mutex` 有一种保护机制：当线程在持有锁期间 panic 了，锁会被标记为"中毒"（poisoned）。这是为了提示后续使用者——锁保护的数据可能处于不一致的中间状态，上一任持有者没来得及完成操作就崩溃了。

此后任何线程调用 `lock()` 都会返回 `Err(PoisonError)` 而非 `Ok(guard)`。调用者可以选择：
- 传播这个错误（保守，放弃使用可能损坏的数据）
- 调用 `into_inner()` 强行恢复（激进，信任自己能处理残留状态）

本缓存的策略是混合的：读路径保守（毒化时跳过缓存，重新从存储加载），写路径激进（`into_inner()` + `clear()` 清空再写入，因为新加载的数据一定是正确的）。

```
快路径（读缓存）:
  lock() → Err(poisoned) → 跳过缓存，fall through 到慢路径
                            （不做入栈：用旧数据不如重新加载）

慢路径（加载）:
  StaticTable::from_metadata_file() → 重新从存储加载，确保拿到正确数据

写缓存:
  lock() → Ok → cache.put()                         正常写入
  lock() → Err(poisoned) → into_inner() → clear()  清掉旧数据
                          → put()                   写入新鲜数据，锁恢复健康
```

## 实现

### 数据结构

```rust
const METADATA_CACHE_CAPACITY: usize = 64;

static METADATA_CACHE: LazyLock<Mutex<LruCache<String, TableMetadataRef>>> =
    LazyLock::new(|| {
        Mutex::new(LruCache::new(
            NonZeroUsize::new(METADATA_CACHE_CAPACITY).unwrap(),
        ))
    });
```

### 核心逻辑

```rust
pub(crate) fn table_load_inner(
    storage: &IcebergBridgeStorage,
    metadata_location: &str,
    table_ident: iceberg::TableIdent,
) -> Result<Box<IcebergBridgeTable>, TableLoadError> {
    // ── 快路径：检查缓存 ──
    if let Ok(mut cache) = METADATA_CACHE.lock() {
        if let Some(metadata) = cache.get(metadata_location) {
            // 命中：跳过 I/O，直接用缓存好的 metadata 构建 Table
            let table = Table::builder()
                .metadata(metadata.clone())
                .metadata_location(metadata_location)
                .identifier(table_ident)
                .file_io(storage.file_io.clone())
                .runtime(with_bridge_runtime(Runtime::new))
                .readonly(true)
                .build()?;
            return Ok(IcebergBridgeTable::new(table));
        }
    }

    // ── 慢路径：从存储加载 ──
    let table = bridge_block_on(StaticTable::from_metadata_file(
        metadata_location, table_ident, storage.file_io.clone(),
    ))?.into_table();

    // ── 写回缓存（含毒化恢复） ──
    match METADATA_CACHE.lock() {
        Ok(mut cache) => {
            cache.put(metadata_location.to_string(), table.metadata_ref());
        }
        Err(poisoned) => {
            let mut cache = poisoned.into_inner();
            cache.clear();
            cache.put(metadata_location.to_string(), table.metadata_ref());
        }
    }

    Ok(IcebergBridgeTable::new(table))
}
```

## 容量与内存

| 参数    | 值                                     |
| ----- |:-------------------------------------:|
| 缓存容量  | 64 条                                  |
| 单条目大小 | 5-200 KB（取决于 snapshot 数量和 schema 复杂度） |
| 总内存占用 | 0.3-13 MB                             |
| 淘汰策略  | LRU，无 TTL（metadata.json 内容不可变）        |

64 条覆盖常规生产环境的活跃表集合。对于 GIST/SIFT 测试场景（1-2 张表），永远不会触发淘汰。

## 不变性保证

缓存正确性依赖以下不变性：

1. **metadata_location 唯一性**：同一 snapshot 的 metadata 文件路径不可变，内容不可变（metadata.json 写入后不再修改）
2. **Table 构造幂等性**：给定相同的 `TableMetadataRef` + `file_io` + `table_ident`，`Table::builder().build()` 产生等价的结果
3. **FileIO 一致性**：`storage.file_io` 是从同一个进程级 storage handle clone 的，读取语义一致

不需要 TTL 或 snapshot 版本校验，因为 `metadata_location` 已经锁定了具体的 metadata 文件版本。

## 验证（2026-07-24）

测试条件：SIFT1M (128维) / GIST1M (960维), fixed, uncompressed, IVF nc=256, 本地 FS, openEuler 24.03

### strace：metadata.json 打开次数

| 版本                   | metadata.json openat 次数 |
| -------------------- |:-----------------------:|
| 旧 .so（无缓存）           | 5                       |
| 新 .so（bridge LRU 缓存） | 3                       |

每次 IVF 查询减少 **40%** 的 metadata.json 读取。剩余 3 次来自 iceberg-index 层。

### 墙钟时间

| 场景            | 旧 .so (warm) | 新 .so (warm) | 差异    |
| ------------- |:------------:|:------------:|:-----:|
| SIFT IVF K=10 | 232ms        | 229ms        | -1.3% |
| GIST IVF K=10 | 1,263ms      | 1,261ms      | -0.2% |

本地盘 metadata.json ~10KB，I/O 占比 <0.5%，收益在噪声范围内。远程存储（S3）场景下单次 metadata 读取可达 10-50ms，减少 2 次可节省 20-100ms。

## 已知限制

### iceberg-index 层缓存未生效

`IndexEngine` 已声明 `metadata_cache: Mutex<LruCache<String, Arc<IndexedTableView>>>`，但当前服务端代码将所有 metadata-location 操作重构到了 `MetadataIndexService`，该服务的 `load_view()` 方法**绕过了 `IndexEngine` 的缓存**，每次都直接调用 `load_metadata_location_table()` → `TableMetadata::read_from()` 读取 metadata.json。

```rust
// MetadataIndexService::load_view — 无缓存，每次都会读 metadata.json
async fn load_view(&self, ...) -> Result<IndexedTableView> {
    let table = load_metadata_location_table(config).await?;  // ← 每次都读文件
    IndexedTableView::from_table(table, Some(self.loader.clone())).await
}
```

这就是 warm 查询中剩余 3 次 metadata.json openat 的来源——bridge 层缓存已消除 bridge 侧的冗余读取，但 iceberg-index 层各自独立加载。

**建议后续优化**（issue #159）：

1. **方案 A**：在 `MetadataIndexService` 中增加 `metadata_cache` 和 `get_or_load_view()`，从 `IndexEngine` 删除已死的缓存字段。
2. **方案 B**：在 `load_metadata_location_table()` 中增加进程级 LRU 缓存，所有 iceberg-index 调用者自动受益。
3. **方案 C**：bridge 和 iceberg-index 共享缓存 —— 但两层缓存对象类型不同（`TableMetadataRef` vs `IndexedTableView`），且依赖方向为 bridge → iceberg-index（单向），共享需要额外公共 crate 或 C ABI 传递缓存句柄，性价比低。

## 文件变更

| 文件                                    | 变更                   |
| ------------------------------------- | -------------------- |
| `Cargo.toml`                          | +1 行（`lru = "0.12"`） |
| `src/services/managed_table/table.rs` | +56 行                |
