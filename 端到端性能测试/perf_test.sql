-- SIFT1M 向量索引性能测试
-- 必须用字面量向量（子查询会导致优化器不走索引）

\timing on

-- 128维随机向量
\set qv '\'[0.12,0.45,0.78,0.23,0.56,0.89,0.34,0.67,0.91,0.15,0.48,0.72,0.36,0.59,0.83,0.27,0.51,0.94,0.18,0.42,0.75,0.29,0.63,0.86,0.31,0.54,0.97,0.21,0.44,0.77,0.33,0.66,0.88,0.25,0.58,0.92,0.16,0.49,0.73,0.37,0.61,0.84,0.28,0.52,0.95,0.19,0.43,0.76,0.32,0.55,0.98,0.22,0.46,0.79,0.35,0.68,0.81,0.24,0.57,0.93,0.17,0.41,0.74,0.38,0.62,0.85,0.26,0.53,0.96,0.13,0.47,0.71,0.39,0.64,0.87,0.11,0.52,0.85,0.29,0.63,0.86,0.11,0.44,0.77,0.23,0.56,0.88,0.32,0.65,0.98,0.21,0.54,0.87,0.31,0.64,0.97,0.18,0.51,0.84,0.27,0.61,0.94,0.16,0.49,0.82,0.25,0.58,0.91,0.14,0.47,0.79,0.33,0.66,0.89,0.22,0.55,0.78,0.19,0.52,0.95,0.28,0.51,0.74,0.37,0.69,0.92,0.15,0.48]''

-- ============================================================
-- 1. 索引状态
-- ============================================================
\echo === 索引状态 ===
SELECT index_name, index_type, index_status
FROM iceberg_catalog.table_indexes
WHERE namespace='sift_ns' AND table_name='sift1m';

-- ============================================================
-- 2. 全表扫描（关闭向量索引）× 3 轮
-- ============================================================
\echo === 全表扫描 Round 1 ===
SET enable_vectorsearch = off;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

\echo === 全表扫描 Round 2 ===
SET enable_vectorsearch = off;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

\echo === 全表扫描 Round 3 ===
SET enable_vectorsearch = off;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

-- ============================================================
-- 3. 索引扫描 × 3 轮
-- ============================================================
\echo === 索引扫描 Round 1 ===
SET enable_vectorsearch = on;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

\echo === 索引扫描 Round 2 ===
SET enable_vectorsearch = on;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

\echo === 索引扫描 Round 3 ===
SET enable_vectorsearch = on;
SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 10;

-- ============================================================
-- 4. 索引扫描 Top-100 × 3 轮
-- ============================================================
\echo === 索引 Top-100 Round 1 ===
SET enable_vectorsearch = on;
SELECT count(*) FROM (SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 100) sub;

\echo === 索引 Top-100 Round 2 ===
SET enable_vectorsearch = on;
SELECT count(*) FROM (SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 100) sub;

\echo === 索引 Top-100 Round 3 ===
SET enable_vectorsearch = on;
SELECT count(*) FROM (SELECT id FROM sift_ns.sift1m ORDER BY vec <-> :qv::vector LIMIT 100) sub;
