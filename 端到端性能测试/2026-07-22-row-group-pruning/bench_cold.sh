#!/bin/bash
set -e
source ~/iceberg-og/opengauss.env
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
TABLE=gist_ns.gist1m_none
SODIR=~/iceberg-og/iceberg-rust-bridge/target/release
DEPLOY=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so

QVEC=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM $TABLE WHERE id=1;" 2>/dev/null)

cat > /tmp/q_cold.sql << EOF
SET enable_vectorsearch=on;
SET try_vector_engine_strategy=force;
SELECT id FROM ${TABLE} ORDER BY vec <-> '${QVEC}'::vector LIMIT 10;
EOF

deploy() {
    local so=$1
    cp $so $SODIR/libiceberg_rust_bridge.so
    cp $SODIR/libiceberg_rust_bridge.so $DEPLOY
    kill -9 $(pgrep gaussdb) 2>/dev/null; sleep 1
    rm -f ~/ogdata/postmaster.pid.lock
    source ~/iceberg-og/opengauss.env
    ~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gaussdb -D ~/ogdata -p 37000 &
    sleep 5
}

test_cold() {
    local label=$1
    echo "=== $label K=10 cold-cache 3 rounds ==="
    for i in 1 2 3; do
        sudo tee /proc/sys/vm/drop_caches <<< "3" > /dev/null
        sleep 1
        local s=$(date +%s%3N)
        $gsql -d postgres -p 37000 -f /tmp/q_cold.sql > /dev/null 2>&1
        local e=$(date +%s%3N)
        echo "  ${label} run${i}: $((e - s))ms"
    done
}

echo "=== Cold-cache A/B Test $(date) ==="

echo "Deploying fxhash..."
deploy $SODIR/libiceberg_rust_bridge.so.fxhash
test_cold "fxhash"

echo "Deploying sparse..."
deploy $SODIR/libiceberg_rust_bridge.so.sparse
test_cold "sparse"

cp $SODIR/libiceberg_rust_bridge.so.fxhash $SODIR/libiceberg_rust_bridge.so
echo "=== Done $(date) ==="
