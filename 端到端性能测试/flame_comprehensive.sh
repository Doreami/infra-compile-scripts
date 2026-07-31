#!/bin/bash
# Comprehensive flamegraph capture for all 4 datasets
# Usage: bash flame_comprehensive.sh <dataset> [--skip-fullscan]
set -e

DS=${1:?"Usage: $0 <sift|gist|deep|synth>"}
source ~/iceberg-og/opengauss.env
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
OUTDIR=~/flamegraphs_${DS}
mkdir -p $OUTDIR

# Dataset config
case $DS in
  sift)
    NS=sift_ns; TBL=sift1m; DIM=128
    QV=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM ${NS}.${TBL} WHERE id=1;" 2>/dev/null)
    KS="10 100 1000 10000"
    ;;
  gist)
    NS=gist_ns; TBL=gist1m; DIM=960
    QV=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM ${NS}.${TBL} WHERE id=1;" 2>/dev/null)
    KS="10 100 1000 10000"
    ;;
  deep)
    NS=deep_ns; TBL=deep1b; DIM=96
    QV=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM ${NS}.${TBL} WHERE id=1;" 2>/dev/null)
    KS="10 100 1000 10000"
    ;;
  synth)
    NS=synth_ns; TBL=synth2048_10M; DIM=2048
    QV=$($gsql -d postgres -p 37000 -t -A -c "SELECT vec FROM ${NS}.\"${TBL}\" WHERE id=1;" 2>/dev/null)
    KS="10 100 1000 10000"
    ;;
  *) echo "Unknown dataset: $DS"; exit 1 ;;
esac

echo "=== Flamegraph capture: ${DS} (${DIM}-dim) ==="
echo "Output: $OUTDIR"

capture() {
  local label=$1 setup=$2 k=$3
  local PF=$OUTDIR/perf_${label}.data
  local SVG=$OUTDIR/flame_${label}.svg
  echo "  [${label}]"

  PID=$(pgrep -x gaussdb)
  if [ -z "$PID" ]; then
    echo "    ERROR: gaussdb not running"
    return 1
  fi

  # Warmup
  $gsql -d postgres -p 37000 -c "${setup} SELECT id FROM ${NS}.${TBL} ORDER BY vec <-> '${QV}'::vector LIMIT ${k};" > /dev/null 2>&1

  # Capture
  perf record -F 99 -g -p $PID -o $PF -- sleep 999 &
  local PFPID=$!
  sleep 1.5

  for i in 1 2 3; do
    $gsql -d postgres -p 37000 -c "${setup} SELECT id FROM ${NS}.${TBL} ORDER BY vec <-> '${QV}'::vector LIMIT ${k};" > /dev/null 2>&1
    echo "    round $i done"
  done

  kill -TERM $PFPID 2>/dev/null
  wait $PFPID 2>/dev/null

  # Generate SVG
  if [ -f $PF ]; then
    perf script -i $PF 2>/dev/null | \
      ~/FlameGraph/stackcollapse-perf.pl 2>/dev/null | \
      ~/FlameGraph/flamegraph.pl --title "${label}" --width 1200 --colors hot > $SVG 2>/dev/null
    echo "    SVG: $(ls -lh $SVG | awk '{print $5}')"
  else
    echo "    ERROR: perf data not created"
  fi
}

# ── IVF flamegraphs for each K ──
for K in $KS; do
  capture "${DS}_ivf_k${K}" \
    "SET enable_vectorsearch=on; SET try_vector_engine_strategy=force;" \
    $K
done

# ── FullScan flamegraph for K=10 ──
capture "${DS}_fullscan_k10" \
  "SET enable_vectorsearch=off; SET enable_indexscan=off; SET enable_bitmapscan=off;" \
  10

echo "=== ${DS} flamegraphs done ==="
ls -lh $OUTDIR/*.svg
