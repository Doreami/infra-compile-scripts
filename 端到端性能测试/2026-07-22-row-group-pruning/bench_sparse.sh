#!/bin/bash
# Sparse row-take benchmark: GIST multi-K + SIFT
set -e
source ~/iceberg-og/opengauss.env
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
DB="-d postgres -p 37000"
SETUP="SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;"
LOG=~/bench_sparse.log

echo "=== Sparse Row-Take Benchmark $(date) ===" | tee $LOG

# ── Helpers ──
run_bench() {
    local label=$1 ns=$2 tbl=$3 k=$4 rounds=${5:-3}
    local qvec=$($gsql $DB -t -A -c "SELECT vec FROM ${ns}.${tbl} WHERE id=1;" 2>/dev/null | tr -d " ")
    echo "" | tee -a $LOG
    echo "=== $label (K=$k, $rounds rounds) ===" | tee -a $LOG
    # warmup
    $gsql $DB -c "${SETUP} SELECT id FROM ${ns}.${tbl} ORDER BY vec <-> '$qvec'::vector LIMIT $k;" > /dev/null 2>&1
    echo "  warmup done" | tee -a $LOG
    local total=0
    for i in $(seq 1 $rounds); do
        local s=$(date +%s%3N)
        $gsql $DB -c "${SETUP} SELECT id FROM ${ns}.${tbl} ORDER BY vec <-> '$qvec'::vector LIMIT $k;" > /dev/null 2>&1
        local e=$(date +%s%3N)
        local d=$((e - s))
        total=$((total + d))
        echo "  run$i: ${d}ms" | tee -a $LOG
    done
    local avg=$((total / rounds))
    echo "  avg: ${avg}ms" | tee -a $LOG
}

# ── GIST ──
run_bench "gist1m_none_K1"  gist_ns gist1m_none 1  3
run_bench "gist1m_none_K10" gist_ns gist1m_none 10 3
run_bench "gist1m_none_K100" gist_ns gist1m_none 100 3
run_bench "gist1m_rg5k_K10" gist_ns gist1m_rg5k 10 3

# ── SIFT ──
run_bench "sift1m_none_K10" sift_ns sift1m_none 10 3

echo "" | tee -a $LOG
echo "=== Done $(date) ===" | tee -a $LOG
