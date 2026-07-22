#!/bin/bash
# Full A/B benchmark: fxhash vs sparse-row-take, GIST K=1/10/100/1000/10000, SELECT *
set -e
source ~/iceberg-og/opengauss.env
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
TABLE=gist_ns.gist1m_none
LOG=~/bench_full_$(date +%Y%m%d_%H%M).log

echo "=== Full A/B Benchmark $(date) ===" | tee $LOG
echo "Table: $TABLE, SELECT *" | tee -a $LOG

# Get query vector
QVEC=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM $TABLE WHERE id=1;" 2>/dev/null)

deploy_and_restart() {
    local so=$1
    cp $so ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so
    cp ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so ~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/lib/postgresql/
    kill -9 $(pgrep gaussdb) 2>/dev/null; sleep 1
    rm -f ~/ogdata/postmaster.pid.lock
    source ~/iceberg-og/opengauss.env
    ~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gaussdb -D ~/ogdata -p 37000 &
    sleep 5
    pgrep -x gaussdb > /dev/null && echo "  gaussdb started" || echo "  FAILED to start"
}

run_bench() {
    local branch=$1
    echo "" | tee -a $LOG
    echo "=== $branch ===" | tee -a $LOG
    for K in 1 10 100 1000 10000; do
        cat > /tmp/q.sql << EOF
SET enable_vectorsearch=on;
SET try_vector_engine_strategy=force;
SELECT * FROM ${TABLE} ORDER BY vec <-> '${QVEC}'::vector LIMIT ${K};
EOF
        # warmup
        $gsql -d postgres -p 37000 -f /tmp/q.sql > /dev/null 2>&1
        local total=0
        for i in 1 2 3; do
            local s=$(date +%s%3N)
            $gsql -d postgres -p 37000 -f /tmp/q.sql > /dev/null 2>&1
            local e=$(date +%s%3N)
            local d=$((e - s))
            total=$((total + d))
            echo "  ${branch} K=${K} run${i}: ${d}ms" | tee -a $LOG
        done
        echo "  ${branch} K=${K} avg: $((total / 3))ms" | tee -a $LOG
    done
}

# Round 1: fxhash baseline
echo "Deploying fxhash..." | tee -a $LOG
deploy_and_restart ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so.fxhash
run_bench "fxhash"

# Round 2: sparse-row-take
echo "Deploying sparse..." | tee -a $LOG
deploy_and_restart ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so.sparse
run_bench "sparse"

# Restore fxhash
cp ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so.fxhash ~/iceberg-og/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so

echo "" | tee -a $LOG
echo "=== Done $(date) ===" | tee -a $LOG
cat $LOG
