火焰图采集结果
==============
Date: 2026-07-16
Run: 2026-07-16
gaussdb PID: 642771
Kernel: 6.6.0-28.0.0.34.oe2403.x86_64
Commit: unknown
Query Vector: id=500000 (SIFT1M)

场景:
  fullscan_k10     — 全表扫描, LIMIT 10
  fullscan_k100    — 全表扫描, LIMIT 100
  fullscan_k1000   — 全表扫描, LIMIT 1000
  ivf_k10          — IVF 索引扫描, K=10 (num_clusters=1024)
  ivf_k100         — IVF 索引扫描, K=100
  ivf_k10000       — IVF 索引扫描, K=10000
  btree_point      — Btree 点查 WHERE id=500000
