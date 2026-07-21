-- ============================================================
-- Iceberg 索引功能端到端测试
-- 依赖: iceberg_fdw + iceberg_catalog 已安装，表已创建
-- ============================================================

CREATE EXTENSION IF NOT EXISTS iceberg_fdw;
CREATE EXTENSION IF NOT EXISTS iceberg_catalog;

-- ============================================================
-- 1. 准备测试数据
-- ============================================================
SELECT iceberg_catalog.create_namespace('idx_ns', '{}'::jsonb) IS NOT NULL AS ns_created;

SELECT iceberg_catalog.create_table(
    'idx_ns', 'idx_test',
    '{"type":"struct","fields":[
      {"id":1,"name":"id","type":"long","required":true},
      {"id":2,"name":"name","type":"string","required":false},
      {"id":3,"name":"score","type":"double","required":false},
      {"id":4,"name":"embedding","type":{"type":"list","element-id":5,"element":"float","element-required":true},"required":false}
    ]}'::jsonb
);

-- 确认表已创建
SELECT count(*) = 1 AS table_created
FROM iceberg_catalog.tables_internal
WHERE namespace = 'idx_ns' AND table_name = 'idx_test';

-- ============================================================
-- 2. create_index — 同步创建 BTree 索引
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'idx_test', 'idx_btree_id',
    '["id"]'::jsonb, 'btree'
) AS btree_created;

-- 验证 table_indexes 记录
SELECT index_name, index_type, index_status
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test'
ORDER BY index_name;

-- ============================================================
-- 3. create_index — 创建 HNSW 向量索引（同步）
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'idx_test', 'idx_hnsw_emb',
    '["embedding"]'::jsonb, 'hnsw',
    ''::text,
    '{"distance":"cosine","M":16,"efConstruction":200}'::jsonb
) AS hnsw_created;

-- 验证多索引记录
SELECT count(*) = 2 AS two_indexes
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test';

-- ============================================================
-- 4. create_index — 异步创建（p_is_async = true）
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'idx_test', 'idx_async',
    '["score"]'::jsonb, 'btree',
    ''::text, NULL::jsonb, true
) AS async_created;

-- 异步创建的索引状态应该是 building
SELECT index_name, index_status
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test'
  AND index_name = 'idx_async';

-- ============================================================
-- 5. build_index — 对异步索引执行构建
-- ============================================================
SELECT iceberg_catalog.build_index(
    'idx_ns', 'idx_test', 'idx_async'
) AS async_built;

-- 构建后状态变为 active
SELECT index_name, index_status
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test'
  AND index_name = 'idx_async';

-- ============================================================
-- 6. 索引总数确认
-- ============================================================
SELECT count(*) AS total_indexes
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test';

-- ============================================================
-- 7. drop_index — 删除单个索引
-- ============================================================
SELECT iceberg_catalog.drop_index(
    'idx_ns', 'idx_test', 'idx_async'
) AS dropped;

-- 确认已删除
SELECT count(*) = 2 AS remaining_indexes
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test';

-- ============================================================
-- 8. 错误处理 — 缺少必填参数
-- ============================================================
SELECT iceberg_catalog.create_index(
    '', 'idx_test', 'idx_empty_ns',
    '["id"]'::jsonb, 'btree'
);

-- ============================================================
-- 9. 错误处理 — 无效索引类型
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'idx_test', 'idx_bad_type',
    '["id"]'::jsonb, 'invalid_type'
);

-- ============================================================
-- 10. 错误处理 — 不存在的表
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'no_such_table', 'idx_no_table',
    '["id"]'::jsonb, 'btree'
);

-- ============================================================
-- 11. 错误处理 — 已存在的索引名
-- ============================================================
SELECT iceberg_catalog.create_index(
    'idx_ns', 'idx_test', 'idx_btree_id',
    '["id"]'::jsonb, 'btree'
);

-- ============================================================
-- 12. 错误处理 — 删除不存在的索引
-- ============================================================
SELECT iceberg_catalog.drop_index(
    'idx_ns', 'idx_test', 'idx_not_exist'
);

-- ============================================================
-- 13. 清理
-- ============================================================
SELECT iceberg_catalog.drop_index('idx_ns', 'idx_test', 'idx_btree_id') AS cleanup1;
SELECT iceberg_catalog.drop_index('idx_ns', 'idx_test', 'idx_hnsw_emb') AS cleanup2;
SELECT count(*) = 0 AS all_cleaned
FROM iceberg_catalog.table_indexes
WHERE namespace = 'idx_ns' AND table_name = 'idx_test';
