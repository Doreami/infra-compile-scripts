# 2026-07-30 测试归档

## 文件说明

| 文件                       | 用途                                          |
| ------------------------ | ------------------------------------------- |
| `性能测试报告.md`              | 完整测试报告（中文），含 SIFT/GIST/DEEP/Synth 四表测试结果    |
| `bench_complete.py`      | **完整测试脚本**（可复用），含 IVF+FullScan+DOP+Recall   |
| `bench_comprehensive.py` | 早期测试脚本（部分 bug 已修，建议用 bench_complete.py）     |
| `setup_fixed.py`         | 数据导入脚本（含 `vector_dim` 修复 + `--vec-type` 参数） |
| `gen_synth.py`           | 合成数据生成脚本（小写 m 命名修复）                         |

## 关键修复

1. **`list<float>` → `vector(N)` 类型映射**：schema JSON 需加 `"vector_dim": N` 字段
2. **表名大小写**：统一用小写避免双引号问题
3. **DOP 上限**：代码中 DOP 最大为 8

## 测试数据位置（服务器）

| 数据集   | 表名                       | 索引                        |
| ----- | ------------------------ | ------------------------- |
| SIFT  | `sift_ns.sift1m`         | `idx_ivf_pq_vec` (active) |
| GIST  | `gist_ns.gist1m`         | `idx_ivf_pq_vec` (active) |
| DEEP  | `deep_ns.deep1b`         | `idx_ivf_pq_vec` (active) |
| Synth | `synth_ns.synth2048_10m` | `idx_ivf_pq_vec` (active) |

## fbin 文件

- SIFT: `/data/xl/测试文件/sift_base.fvecs`
- GIST: `/data/xl/测试文件/gist-960-euclidean.hdf5`
- DEEP: `/data/xl/big-ann-benchmarks/data/deep1b/base.1B.fbin`
- Synth: `/data/xl/测试文件/synth2048_10M_base.fbin` (77GB, seed=42)
