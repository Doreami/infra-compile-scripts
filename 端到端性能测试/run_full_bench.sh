#!/bin/bash
# Full pipeline: import → index → benchmark → flamegraph
# Usage: nohup bash run_full_bench.sh > bench_$(date +%Y-%m-%d).log 2>&1 &
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGDIR="$SCRIPT_DIR/$(date +%Y-%m-%d)"
mkdir -p "$LOGDIR"

GSQL="gsql -d postgres -p 37000"
WARM_QUERY="SELECT 1"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── 1. Sync scripts ──
log "=== Sync infra-compile-scripts ==="
cd ~/infra-compile-scripts
git fetch origin && git reset --hard origin/main
log "  commit: $(git log --oneline -1)"

# ── 2. Check SIFT index, rebuild if needed ──
log "=== SIFT IVF-PQ index ==="
HAS_IDX=$($GSQL -t -A -c "SELECT count(*) FROM iceberg_catalog.table_indexes WHERE namespace='sift_ns' AND table_name='sift1m';" 2>/dev/null || echo 0)
if [ "$HAS_IDX" = "0" ]; then
    TS=$(date +%s)
    log "  building IVF-PQ index..."
    $GSQL -c "SELECT iceberg_catalog.create_index('sift_ns','sift1m','idx_ivf_pq_vec','[\"vec\"]'::jsonb,'ivf_pq','ivf','{\"vector_column\":\"vec\",\"num_clusters\":1024,\"sample_rate\":100000}'::jsonb);"
    ELAPSED=$(($(date +%s) - TS))
    log "  SIFT index built in ${ELAPSED}s"
else
    IDX_STATUS=$($GSQL -t -A -c "SELECT index_status FROM iceberg_catalog.table_indexes WHERE namespace='sift_ns' AND table_name='sift1m';")
    log "  SIFT index: $IDX_STATUS"
fi

# ── 3. Clean + Import GIST partitioned ──
log "=== GIST Import ==="
$GSQL -c "DROP FOREIGN TABLE IF EXISTS gist_ns_part.gist1m_part;" 2>/dev/null || true
$GSQL -c "SELECT iceberg_catalog.drop_table('gist_ns_part','gist1m_part');" 2>/dev/null || true
$GSQL -c "DELETE FROM iceberg_catalog.tables_internal WHERE namespace='gist_ns_part';" 2>/dev/null || true
rm -rf /data/xl/warehouse/gist_ns_part 2>/dev/null || true

TS=$(date +%s)
python3 "$SCRIPT_DIR/setup_fixed.py" --input ~/测试文件/gist-960-euclidean.hdf5 --partition-buckets 16
ELAPSED=$(($(date +%s) - TS))
log "  GIST import done in ${ELAPSED}s"

# ── 4. GIST IVF-PQ index ──
log "=== GIST IVF-PQ index ==="
TS=$(date +%s)
$GSQL -c "SELECT iceberg_catalog.create_index('gist_ns_part','gist1m_part','idx_ivf_pq_vec','[\"vec\"]'::jsonb,'ivf_pq','ivf','{\"vector_column\":\"vec\",\"num_clusters\":1024,\"sample_rate\":100000}'::jsonb);"
ELAPSED=$(($(date +%s) - TS))
log "  GIST index built in ${ELAPSED}s"

# ── 5. Run parallel benchmark ──
log "=== Parallel Benchmark ==="
python3 "$SCRIPT_DIR/bench_parallel.py" --all 2>&1 | tee "$LOGDIR/bench_parallel.log"

# ── 6. Run flamegraph (selected scenarios) ──
log "=== Flamegraph ==="
python3 "$SCRIPT_DIR/run_flamegraph.py" --dataset sift --namespace sift_ns --table sift1m --scenarios "fullscan_k10,ivf_k10,btree_point" 2>&1 | tee "$LOGDIR/flamegraph_sift.log"
python3 "$SCRIPT_DIR/run_flamegraph.py" --dataset gist --namespace gist_ns_part --table gist1m_part --scenarios "fullscan_k10,ivf_k10,btree_point" 2>&1 | tee "$LOGDIR/flamegraph_gist.log"

# ── 7. Collect version info ──
log "=== Version Info ==="
{
echo "## 版本信息"
echo ""
echo "| 组件 | Commit |"
echo "|------|--------|"
for d in ~/iceberg-og/openGauss-server-datainfra ~/iceberg-og/iceberg-rust-bridge ~/iceberg-og/iceberg-index ~/iceberg-og/iceberg-rust-cache ~/iceberg-og/iceberg-rust-datainfra ~/iceberg-og/openGauss-Catalog ~/iceberg-og/iceberg_fdw; do
    if [ -d "$d" ]; then
        name=$(basename "$d")
        commit=$(cd "$d" && git log --oneline -1 2>/dev/null || echo "N/A")
        echo "| $name | \`$commit\` |"
    fi
done
} > "$LOGDIR/version_info.md"

log "=== ALL DONE ==="
log "Output: $LOGDIR/"
ls -la "$LOGDIR/"
