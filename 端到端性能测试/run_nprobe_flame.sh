#!/bin/bash
# nprobe=1 vs 32 火焰图对比
set -uo pipefail

source ~/iceberg-og/opengauss.env
export PATH="$GAUSSHOME/bin:$PATH"

GSQL="$GAUSSHOME/bin/gsql -d postgres -p 37000"
FLAMEGRAPH_DIR=~/FlameGraph
STACK="$FLAMEGRAPH_DIR/stackcollapse-perf.pl"
FLAME="$FLAMEGRAPH_DIR/flamegraph.pl"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TODAY=$(date +%Y-%m-%d)
OUT_DIR="$SCRIPT_DIR/$TODAY/flamegraphs"
PERF_DIR="$OUT_DIR/perf_data"
mkdir -p "$PERF_DIR"

GAUSSDB_PID=$(pgrep -f 'gaussdb.*ogdata.*37000' | head -1)
QV='[16,14,9,5,9,5,14,9,6,40,128,37,11,0,0,0,25,103,103,12,0,0,0,0,15,45,61,0,0,0,0,0,128,29,3,0,0,0,5,28,128,117,128,33,0,0,0,20,12,27,128,128,2,0,1,3,44,24,50,50,1,0,0,2,128,11,4,0,0,0,0,53,128,7,3,2,0,0,19,128,7,1,17,48,9,24,94,45,8,8,21,42,11,16,24,8,31,39,96,0,0,0,0,6,81,43,53,1,0,0,5,65,28,30,27,4,0,4,31,22,2,3,39,12,0,2,13,10]'

log() { echo "[$(date +%H:%M:%S)] $*"; }

for nprobe in 1 32; do
    log "=== nprobe=$nprobe ==="

    # set nprobe
    $GSQL -c "ALTER FOREIGN TABLE sift_ns.sift1m OPTIONS (DROP nprobe);" > /dev/null 2>&1
    $GSQL -c "ALTER FOREIGN TABLE sift_ns.sift1m OPTIONS (ADD nprobe '$nprobe');" > /dev/null 2>&1

    # warmup
    log "  预热..."
    echo "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force; SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '$QV'::vector LIMIT 10;" | $GSQL > /dev/null 2>&1

    # perf
    perf_data="$PERF_DIR/perf_ivf_k10_nprobe${nprobe}_${TODAY}.data"
    log "  perf record..."
    perf record -F 99 -g -p "$GAUSSDB_PID" -o "$perf_data" -- sleep 99999 &
    PERF_PID=$!
    sleep 1.5

    log "  执行 5 轮..."
    for i in $(seq 5); do
        echo "SET enable_vectorsearch = on; SET try_vector_engine_strategy = force; SELECT id FROM sift_ns.sift1m ORDER BY vec <-> '$QV'::vector LIMIT 10;" | $GSQL > /dev/null 2>&1
    done

    sleep 0.5
    kill -INT "$PERF_PID" 2>/dev/null || true
    wait "$PERF_PID" 2>/dev/null || true

    samples=$(perf report -i "$perf_data" --stdio 2>/dev/null | grep -c '^\s\+[0-9]' || echo 0)
    log "  samples: $samples"

    if [ "$samples" -gt 0 ]; then
        tmp="/tmp/flame_nprobe${nprobe}_$$.txt"
        perf script -i "$perf_data" > "$tmp" 2>/dev/null
        svg="$OUT_DIR/flame_ivf_k10_nprobe${nprobe}_${TODAY}.svg"
        "$STACK" < "$tmp" | "$FLAME" --title "SIFT1M IVF K=10 nprobe=$nprobe ($TODAY)" --width 1200 --colors hot > "$svg" 2>/dev/null
        rm -f "$tmp"
        echo "  SVG: $(du -h "$svg" | cut -f1)"
    fi
done

# cleanup
$GSQL -c "ALTER FOREIGN TABLE sift_ns.sift1m OPTIONS (DROP nprobe);" > /dev/null 2>&1
log "Done."
