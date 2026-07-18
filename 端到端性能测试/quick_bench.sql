-- Quick performance verification for fixed-type tables

-- GIST1M FullScan K=10
SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;
EXPLAIN (ANALYZE) SELECT id FROM gist_ns.gist1m ORDER BY vec <-> (SELECT vec FROM gist_ns.gist1m WHERE id = 1) LIMIT 10;

-- GIST1M IVF K=10
SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;
EXPLAIN (ANALYZE) SELECT id FROM gist_ns.gist1m ORDER BY vec <-> (SELECT vec FROM gist_ns.gist1m WHERE id = 1) LIMIT 10;

-- SIFT1M FullScan K=10
SET enable_indexscan = off; SET enable_bitmapscan = off; SET enable_vectorsearch = off;
EXPLAIN (ANALYZE) SELECT id FROM sift_ns.sift1m ORDER BY vec <-> (SELECT vec FROM sift_ns.sift1m WHERE id = 1) LIMIT 10;

-- SIFT1M IVF K=10
SET enable_vectorsearch = on; SET try_vector_engine_strategy = force;
EXPLAIN (ANALYZE) SELECT id FROM sift_ns.sift1m ORDER BY vec <-> (SELECT vec FROM sift_ns.sift1m WHERE id = 1) LIMIT 10;
