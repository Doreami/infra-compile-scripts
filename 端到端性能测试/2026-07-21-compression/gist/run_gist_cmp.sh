#!/bin/bash
# GIST1M 压缩对比测试 — 一键执行脚本
# 用法: bash run_gist_cmp.sh
# 前提: gaussdb 在 37000 端口运行, setup_fixed.py 已更新, bench_parallel.py 就绪

set -e
source ~/iceberg-og/opengauss.env 2>/dev/null
SCRIPT_DIR=~/infra-compile-scripts/端到端性能测试
cd "$SCRIPT_DIR"

echo "============================================"
echo " GIST1M 压缩对比测试: zstd vs uncompressed"
echo " 开始时间: $(date)"
echo "============================================"

# ── Step 1: 检查 zstd 表状态 ──
echo ""
echo "=== Step 1: 检查现有表 ==="
ZSTD_EXISTS=$(gsql -d postgres -p 37000 -t -A -c \
  "SELECT count(*) FROM iceberg_catalog.table_indexes WHERE namespace='gist_ns' AND table_name='gist1m_zstd';" 2>/dev/null || echo "0")
NONE_EXISTS=$(gsql -d postgres -p 37000 -t -A -c \
  "SELECT count(*) FROM iceberg_catalog.table_indexes WHERE namespace='gist_ns' AND table_name='gist1m_none';" 2>/dev/null || echo "0")
echo "gist1m_zstd index: $ZSTD_EXISTS"
echo "gist1m_none index: $NONE_EXISTS"

# ── Step 2: 导入 zstd 表 (如果失败或被 kill) ──
ROW_COUNT=$(gsql -d postgres -p 37000 -t -A -c \
  "SELECT count(*) FROM gist_ns.gist1m_zstd;" 2>/dev/null || echo "0")
if [ "$ROW_COUNT" != "1000000" ]; then
    echo ""
    echo "=== Step 2: 导入 gist1m_zstd ==="
    python3 -u setup_fixed.py \
        --input ~/测试文件/gist-960-euclidean.hdf5 \
        --namespace gist_ns --table gist1m_zstd \
        --compression zstd
else
    echo "gist1m_zstd 已存在 ($ROW_COUNT 行), 跳过导入"
fi

# ── Step 3: 导入无压缩表 (如果不存在) ──
ROW_COUNT=$(gsql -d postgres -p 37000 -t -A -c \
  "SELECT count(*) FROM gist_ns.gist1m_none;" 2>/dev/null || echo "0")
if [ "$ROW_COUNT" != "1000000" ]; then
    echo ""
    echo "=== Step 3: 导入 gist1m_none ==="
    python3 -u setup_fixed.py \
        --input ~/测试文件/gist-960-euclidean.hdf5 \
        --namespace gist_ns --table gist1m_none
else
    echo "gist1m_none 已存在 ($ROW_COUNT 行), 跳过导入"
fi

# ── Step 4: 建索引 ──
for tbl in gist1m_zstd gist1m_none; do
    IDX_STATUS=$(gsql -d postgres -p 37000 -t -A -c \
      "SELECT index_status FROM iceberg_catalog.table_indexes WHERE namespace='gist_ns' AND table_name='$tbl';" 2>/dev/null || echo "")
    if [ "$IDX_STATUS" != "active" ]; then
        echo ""
        echo "=== Step 4: 建索引 $tbl (约 63min) ==="
        nohup gsql -d postgres -p 37000 -c \
          "SELECT iceberg_catalog.create_index('gist_ns', '$tbl', 'idx_ivf_pq_vec', '[\"vec\"]'::jsonb, 'ivf_pq', 'ivf', '{\"vector_column\":\"vec\",\"num_clusters\":256,\"sample_rate\":100000}'::jsonb);" \
          > ~/build_idx_gist_${tbl}.log 2>&1 &
        echo "  已启动后台建索引 (PID=$!)"
        echo "  等待完成..."
        wait $!
        echo "  索引 $tbl 完成"
    else
        echo "索引 $tbl: active (跳过)"
    fi
done

# ── Step 5: Benchmark ──
echo ""
echo "=== Step 5: 性能测试 ==="
echo "$(date): 开始 bench_zstd ..."
python3 -u bench_parallel.py \
    --serial --dataset gist --namespace gist_ns --table gist1m_zstd \
    --dop 1 --rounds 5 --skip-fullscan \
    2>&1 | tee ~/gist_bench_zstd.log

echo "$(date): 开始 bench_none ..."
python3 -u bench_parallel.py \
    --serial --dataset gist --namespace gist_ns --table gist1m_none \
    --dop 1 --rounds 5 --skip-fullscan \
    2>&1 | tee ~/gist_bench_none.log

# ── Step 6: 火焰图 ──
echo ""
echo "=== Step 6: 火焰图采集 ==="
for tbl in gist1m_zstd gist1m_none; do
    if [ "$tbl" = "gist1m_zstd" ]; then cmp="zstd"; else cmp="none"; fi
    echo "$(date): flame_$cmp ..."
    python3 -u run_flamegraph.py \
        --dataset gist --namespace gist_ns --table $tbl \
        --serial --scenarios ivf_k10,ivf_k100,fullscan_k10 \
        2>&1 | tee ~/gist_flame_$cmp.log
done

# ── Step 7: 统计信息 ──
echo ""
echo "=== Step 7: 存储统计 ==="
python3 -c "
import os, glob
for tbl, label in [('gist1m_zstd','zstd'), ('gist1m_none','none')]:
    files = glob.glob(f'/data/xl/warehouse/gist_ns/{tbl}/data/*.parquet')
    if not files:
        files = glob.glob(f'/data/xl/warehouse/gist_ns/{tbl}/data/**/*.parquet', recursive=True)
    sz = sum(os.path.getsize(f) for f in files)
    print(f'{label}: {len(files)} files, {sz/(1024**3):.1f}GB')
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(files[0])
    col = pf.metadata.row_group(0).column(1)  # vec column
    print(f'  vec codec={col.compression}')
"

echo ""
echo "============================================"
echo " GIST 测试完成: $(date)"
echo " 结果: ~/gist_bench_zstd.log, ~/gist_bench_none.log"
echo " 火焰图: 端到端性能测试/YYYY-MM-DD/flamegraphs/"
echo "============================================"
