#!/bin/bash
# ============================================================
# 端到端索引性能验证脚本
# 用法: bash run_perf_test.sh
# ============================================================
set -euo pipefail

source ~/iceberg-og/opengauss.env
GSQL="gsql -d postgres -p 37000"
WAREHOUSE=~/warehouse
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT="$SCRIPT_DIR/perf_report_$(date +%Y%m%d_%H%M%S).txt"

NS="perf_ns"
TBL="vectors"
DIM=128
ROWS=100000

# ── 工具函数 ──────────────────────────────────────────────
log()  { echo "[$(date +%H:%M:%S)] $*" | tee -a "$REPORT"; }
gsql() { $GSQL -c "$1" 2>&1 | tee -a "$REPORT"; }

# ── Step 1: 环境检查 ──────────────────────────────────────
log "============================================"
log "  端到端索引性能验证"
log "============================================"
log ""
log "GAUSSHOME: $GAUSSHOME"
log "Warehouse: $WAREHOUSE"

# 建 SERVER
if $GSQL -t -c "SELECT 1 FROM pg_foreign_server WHERE srvname='iceberg_catalog_server'" 2>/dev/null | grep -q 1; then
    log "✓ iceberg_catalog_server 已存在"
else
    log "创建 iceberg_catalog_server..."
    $GSQL -c "CREATE SERVER iceberg_catalog_server FOREIGN DATA WRAPPER iceberg_fdw;" 2>&1 | tee -a "$REPORT"
fi

# ── Step 2: 准备数据 ──────────────────────────────────────
CSV=/tmp/perf_test.csv
if [ -f "$CSV" ]; then
    log "✓ 数据文件已存在: $CSV ($(wc -l < $CSV) 行)"
else
    log "生成 ${ROWS} 行 × ${DIM} 维测试数据..."
    python3 -c "
import random
N = $ROWS
dim = $DIM
with open('$CSV', 'w') as f:
    f.write('id,vec\n')
    for i in range(N):
        vec = [round(random.uniform(-1, 1), 6) for _ in range(dim)]
        vec_str = '[' + ','.join(map(str, vec)) + ']'
        f.write(f'{i+1},\"{vec_str}\"\n')
print(f'Generated {N} rows')
"
    log "✓ 数据生成完成"
fi

# ── Step 3: 导入数据 ──────────────────────────────────────
IMPORT_DIR="$HOME/infra-compile-scripts/端到端测试指南/导入数据"
log "导入数据到 $NS.$TBL ..."
python3 "$IMPORT_DIR/import_csv.py" \
    --op create \
    --csv "$CSV" \
    -n "$NS" -t "$TBL" \
    -c "id:long,vec:vector($DIM)" \
    -w "$WAREHOUSE" \
    --gsql "$GSQL" 2>&1 | tee -a "$REPORT"

# 验证
CNT=$($GSQL -t -c "SELECT count(*) FROM $NS.$TBL;" 2>/dev/null | head -1 | tr -d ' ')
log "✓ 表 $NS.$TBL 共 $CNT 行"

# ── Step 4: 全表扫描 baseline ─────────────────────────────
log ""
log "============================================"
log "  全表扫描 Baseline"
log "============================================"

QID=500
QVEC=$($GSQL -t -c "SELECT vec FROM $NS.$TBL WHERE id = $QID LIMIT 1;" 2>/dev/null | head -1 | tr -d ' ')

log "查询向量: id=$QID"
log ""

log "--- 无索引，全表扫描 ---"
gsql "SET enable_indexscan = off; SET enable_bitmapscan = off; EXPLAIN (ANALYZE) SELECT id FROM $NS.$TBL ORDER BY vec <-> (SELECT vec FROM $NS.$TBL WHERE id = $QID)::vector LIMIT 10;"

log "--- 全扫，验证结果 ---"
gsql "SET enable_indexscan = off; SELECT id FROM $NS.$TBL ORDER BY vec <-> (SELECT vec FROM $NS.$TBL WHERE id = $QID)::vector LIMIT 10;"

# ── Step 5: 创建索引 ──────────────────────────────────────
log ""
log "============================================"
log "  创建索引"
log "============================================"

# btree
log "--- btree 索引 (id) ---"
gsql "SELECT iceberg_catalog.create_index('$NS','$TBL','idx_btree_id','[\"id\"]'::jsonb,'btree','btree','{\"key_column\":\"id\"}'::jsonb);" 2>&1 | tee -a "$REPORT" || log "btree 索引创建失败（可能已存在）"

# IVF
log "--- IVF 向量索引 (vec, nlist=128) ---"
gsql "SELECT iceberg_catalog.create_index('$NS','$TBL','idx_ivf_vec','[\"vec\"]'::jsonb,'ivf_flat','ivf_flat','{\"nlist\":128}'::jsonb);" 2>&1 | tee -a "$REPORT" || log "IVF 索引创建失败（可能已存在）"

# ── Step 6: 索引扫描测试 ──────────────────────────────────
log ""
log "============================================"
log "  索引扫描性能"
log "============================================"

log "--- btree 点查 ---"
gsql "EXPLAIN (ANALYZE) SELECT * FROM $NS.$TBL WHERE id = 1000;"

log "--- btree 范围扫描 ---"
gsql "EXPLAIN (ANALYZE) SELECT * FROM $NS.$TBL WHERE id BETWEEN 1 AND 100;"

log "--- IVF 向量索引 ---"
gsql "EXPLAIN (ANALYZE) SELECT id FROM $NS.$TBL ORDER BY vec <-> (SELECT vec FROM $NS.$TBL WHERE id = $QID)::vector LIMIT 10;"

log "--- IVF 索引结果 ---"
gsql "SELECT id FROM $NS.$TBL ORDER BY vec <-> (SELECT vec FROM $NS.$TBL WHERE id = $QID)::vector LIMIT 10;"

# ── Step 7: EXPLAIN 验证索引是否被使用 ────────────────────
log ""
log "============================================"
log "  查询计划验证"
log "============================================"

log "--- btree 索引是否生效 ---"
gsql "SET enable_seqscan = off; EXPLAIN (COSTS OFF) SELECT * FROM $NS.$TBL WHERE id = 1000;"

log "--- IVF 索引是否生效 ---"
gsql "EXPLAIN (COSTS OFF) SELECT id FROM $NS.$TBL ORDER BY vec <-> (SELECT vec FROM $NS.$TBL WHERE id = $QID)::vector LIMIT 10;"

# ── 完成 ──────────────────────────────────────────────────
log ""
log "============================================"
log "  测试完成"
log "  报告: $REPORT"
log "============================================"
