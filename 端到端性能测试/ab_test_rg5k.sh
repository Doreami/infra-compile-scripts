#!/bin/bash
# A/B test: fxhash vs row-group-pruning on gist1m_rg5k (200 row groups)
set -e

SODIR=~/iceberg-og/iceberg-rust-bridge/target/release
BASELINE_SO=$SODIR/libiceberg_rust_bridge.so.fxhash
PRUNING_SO=$SODIR/libiceberg_rust_bridge.so
DEPLOY_TO=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so
LOG=~/ab_test_rg5k.log

echo "=== A/B Test: Row Group Pruning (gist1m_rg5k, 200 row groups) ===" | tee $LOG
echo "Started: $(date)" | tee -a $LOG

gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
DB="-d postgres -p 37000"

# Use a fixed query vector (first row's vector)
QVEC=$($gsql $DB -t -A -c "SELECT vec::text FROM gist_ns.gist1m_rg5k WHERE id=1;" 2>/dev/null | head -1)

test_round() {
    local label="$1"
    echo "" | tee -a $LOG
    echo "--- $label ---" | tee -a $LOG

    # Stop
    kill %1 2>/dev/null; sleep 1

    # Deploy and start
    cp $SODIR/libiceberg_rust_bridge.so $DEPLOY_TO
    ~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gaussdb -D ~/ogdata -p 37000 &
    sleep 3

    # Warm up
    $gsql $DB -c "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force; SELECT count(*) FROM gist_ns.gist1m_rg5k;" > /dev/null 2>&1
    $gsql $DB -c "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force; SELECT * FROM gist_ns.gist1m_rg5k ORDER BY vec_l2_distance(vec, '$QVEC') LIMIT 10;" > /dev/null 2>&1

    # 3 timed runs
    for i in 1 2 3; do
        start=$(date +%s%3N)
        $gsql $DB -c "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force; SELECT * FROM gist_ns.gist1m_rg5k ORDER BY vec_l2_distance(vec, '$QVEC') LIMIT 10;" > /dev/null 2>&1
        end=$(date +%s%3N)
        elapsed=$((end - start))
        echo "  $label run$i: ${elapsed}ms" | tee -a $LOG
    done
}

# Round 1: fxhash baseline
cp $BASELINE_SO $SODIR/libiceberg_rust_bridge.so
test_round "fxhash_baseline"

# Round 2: row-group-pruning
cp $PRUNING_SO $SODIR/libiceberg_rust_bridge.so
test_round "rg_pruning"

# Cleanup
kill %1 2>/dev/null
cp $BASELINE_SO $SODIR/libiceberg_rust_bridge.so

echo "" | tee -a $LOG
echo "=== Done: $(date) ===" | tee -a $LOG
cat $LOG
