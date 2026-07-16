-- ============================================================
-- SIFT1M 性能测试 — 建表 + 索引 + 查询
-- 依赖: convert_sift.py 已将 Parquet 写入 warehouse
-- ============================================================

CREATE EXTENSION IF NOT EXISTS iceberg_fdw;
CREATE EXTENSION IF NOT EXISTS iceberg_catalog;

\timing on

-- ============================================================
-- 1. 建命名空间和表
-- ============================================================
SELECT iceberg_catalog.create_namespace('sift_ns', '{}'::jsonb) IS NOT NULL AS ns_created;

SELECT iceberg_catalog.create_table(
    'sift_ns', 'sift1m',
    '{"type":"struct","fields":[
      {"id":1,"name":"id","type":"long","required":true},
      {"id":2,"name":"vec","type":{"type":"list","element-id":3,"element":"float","element-required":true},"required":false}
    ]}'::jsonb
);

-- 确认数据可读（数据量取决于导入的 Parquet 文件数）
SELECT count(*) AS total_rows FROM sift_ns.sift1m;

-- ============================================================
-- 2. 无索引基准测试（全表扫描）
-- ============================================================
\echo === B1: No Index — Full Table Scan ===

-- 取第 0 号查询向量
\set q0_vec '(1.0,2.0,3.0,...)'  -- 占位，实际运行时替换

EXPLAIN (ANALYZE, TIMING OFF)
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> (SELECT vec FROM sift_queries WHERE qid = 0)::vector
LIMIT 10;

-- ============================================================
-- 3. 创建 HNSW 索引（M=16, efConstruction=200）
-- ============================================================
\echo === B2: HNSW M=16 efConstruction=200 ===

-- 记录开始时间，构建索引
SELECT iceberg_catalog.create_index(
    'sift_ns', 'sift1m', 'idx_hnsw_m16_ef200',
    '["vec"]'::jsonb, 'hnsw', 'hnsw',
    '{"M":16,"efConstruction":200,"distance":"l2"}'::jsonb
) AS hnsw_m16_created;

-- 验证索引
SELECT index_name, index_type, implementation, index_status
FROM iceberg_catalog.table_indexes
WHERE namespace = 'sift_ns' AND table_name = 'sift1m';

-- 索引扫描查询
EXPLAIN (ANALYZE, TIMING OFF)
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> (SELECT vec FROM sift_queries WHERE qid = 0)::vector
LIMIT 10;

-- ============================================================
-- 4. 创建 HNSW 索引（M=32, efConstruction=500）
-- ============================================================
\echo === B3: HNSW M=32 efConstruction=500 ===

-- 先删除上一个索引
SELECT iceberg_catalog.drop_index('sift_ns', 'sift1m', 'idx_hnsw_m16_ef200');

SELECT iceberg_catalog.create_index(
    'sift_ns', 'sift1m', 'idx_hnsw_m32_ef500',
    '["vec"]'::jsonb, 'hnsw', 'hnsw',
    '{"M":32,"efConstruction":500,"distance":"l2"}'::jsonb
) AS hnsw_m32_created;

-- 索引扫描查询
EXPLAIN (ANALYZE, TIMING OFF)
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> (SELECT vec FROM sift_queries WHERE qid = 0)::vector
LIMIT 10;

-- ============================================================
-- 5. 创建 HNSW 索引（M=64, efConstruction=500）
-- ============================================================
\echo === B4: HNSW M=64 efConstruction=500 ===

SELECT iceberg_catalog.drop_index('sift_ns', 'sift1m', 'idx_hnsw_m32_ef500');

SELECT iceberg_catalog.create_index(
    'sift_ns', 'sift1m', 'idx_hnsw_m64_ef500',
    '["vec"]'::jsonb, 'hnsw', 'hnsw',
    '{"M":64,"efConstruction":500,"distance":"l2"}'::jsonb
) AS hnsw_m64_created;

-- 索引扫描查询
EXPLAIN (ANALYZE, TIMING OFF)
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> (SELECT vec FROM sift_queries WHERE qid = 0)::vector
LIMIT 10;

-- ============================================================
-- 6. 索引大小
-- ============================================================
\echo === Index Size ===
SELECT index_name, index_type
FROM iceberg_catalog.table_indexes
WHERE namespace = 'sift_ns' AND table_name = 'sift1m';

-- 清理
SELECT iceberg_catalog.drop_index('sift_ns', 'sift1m', 'idx_hnsw_m64_ef500');
