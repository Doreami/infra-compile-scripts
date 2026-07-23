#!/bin/bash
set -e
source ~/iceberg-og/opengauss.env
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
SODIR=~/iceberg-og/iceberg-rust-bridge/target/release
DEPLOY=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so
OUTDIR=~/infra-compile-scripts/端到端性能测试/性能优化追踪
mkdir -p $OUTDIR/perf_data

QG=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM gist_ns.gist1m_none WHERE id=1;" 2>/dev/null)
QS=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM sift_ns.sift1m_none WHERE id=1;" 2>/dev/null)

deploy() {
  local so=$1
  cp $so $SODIR/libiceberg_rust_bridge.so
  cp $SODIR/libiceberg_rust_bridge.so $DEPLOY
  kill -9 $(pgrep gaussdb) 2>/dev/null; sleep 1; rm -f ~/ogdata/postmaster.pid.lock
  source ~/iceberg-og/opengauss.env
  ~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gaussdb -D ~/ogdata -p 37000 &
  sleep 5
}

capture() {
  local label=$1 tbl=$2 qvec=$3 setup=$4 k=$5
  local PF=$OUTDIR/perf_data/perf_${label}.data
  local SVG=$OUTDIR/flame_${label}.svg
  echo "=== ${label} ==="
  PID=$(pgrep -x gaussdb)
  $gsql -d postgres -p 37000 -c "${setup} SELECT id FROM ${tbl} ORDER BY vec <-> '${qvec}'::vector LIMIT ${k};" > /dev/null 2>&1
  sleep 1
  perf record -F 99 -g -p $PID -o $PF -- sleep 999 &
  local PFPID=$!
  sleep 1.5
  for i in 1 2 3; do
    $gsql -d postgres -p 37000 -c "${setup} SELECT id FROM ${tbl} ORDER BY vec <-> '${qvec}'::vector LIMIT ${k};" > /dev/null 2>&1
    echo "  round $i"
  done
  kill -TERM $PFPID 2>/dev/null
  wait $PFPID 2>/dev/null
  perf script -i $PF 2>/dev/null | ~/FlameGraph/stackcollapse-perf.pl 2>/dev/null | ~/FlameGraph/flamegraph.pl --title "${label}" --width 1200 --colors hot > $SVG 2>/dev/null
  echo "  SVG: $(ls -lh $SVG | cut -d' ' -f5)"
}

# 1. GIST fxhash IVF K=10
deploy $SODIR/libiceberg_rust_bridge.so.fxhash
capture "gist_fxhash_ivf_k10" "gist_ns.gist1m_none" "$QG" "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;" 10

# 2. GIST sparse IVF K=10
deploy $SODIR/libiceberg_rust_bridge.so.sparse
capture "gist_sparse_ivf_k10" "gist_ns.gist1m_none" "$QG" "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;" 10

# 3. GIST FullScan K=10
capture "gist_fullscan_k10" "gist_ns.gist1m_none" "$QG" "SET enable_vectorsearch=off; SET enable_indexscan=off; SET enable_bitmapscan=off;" 10

# 4. SIFT sparse IVF K=10
capture "sift_sparse_ivf_k10" "sift_ns.sift1m_none" "$QS" "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;" 10

# 5. SIFT FullScan K=10
capture "sift_fullscan_k10" "sift_ns.sift1m_none" "$QS" "SET enable_vectorsearch=off; SET enable_indexscan=off; SET enable_bitmapscan=off;" 10

# Restore fxhash
cp $SODIR/libiceberg_rust_bridge.so.fxhash $SODIR/libiceberg_rust_bridge.so
echo "=== ALL DONE ==="
