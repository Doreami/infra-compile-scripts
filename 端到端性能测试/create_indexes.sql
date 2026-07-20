SELECT iceberg_catalog.create_index('gist_ns', 'gist1m', 'idx_ivf_pq_vec', '["vec"]'::jsonb, 'ivf_pq', 'ivf', '{"vector_column":"vec","num_clusters":1024,"sample_rate":100000}'::jsonb);
SELECT iceberg_catalog.create_index('sift_ns', 'sift1m', 'idx_ivf_pq_vec', '["vec"]'::jsonb, 'ivf_pq', 'ivf', '{"vector_column":"vec","num_clusters":1024,"sample_rate":100000}'::jsonb);
