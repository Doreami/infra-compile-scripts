#!/bin/bash
# MoReVec 向量+标量混合查询火焰图采集（性能瓶颈观察）
# 用法: bash flame_morevec.sh <outdir> [filter] [gaussdb_pid]
# 功能: 取 MoRe query 向量 → perf 采样期间连续跑查询 → 生成 flame.svg + report.txt
#      gaussdb_pid 缺省时按「命令行含 -p 37000」自动找共享实例（排除 devtest 临时实例）。
# 注意: perf 需 LD_LIBRARY_PATH 为空（opengauss 旧 lib 会报 Access to performance monitoring limited）
set -e
OUTDIR=${1:?Usage: $0 <outdir> [filter] [gaussdb_pid]}
FILTER="${2:-total_votes >= 743.0}"
GPD_PID="${3:-}"
gsql=~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql
mkdir -p "$OUTDIR"

# 找 gaussdb PID：优先参数；否则 grep 命令行含 -p 37000 的 gaussdb 主进程
if [ -z "$GPD_PID" ]; then
  GPD_PID=$(ps -ef | grep "[g]aussdb .*-p 37000" | awk '{print $2}' | head -1)
fi
[ -z "$GPD_PID" ] && { echo "ERROR: 未找到 gaussdb(-p 37000) PID，可用第三个参数显式传"; exit 1; }
echo "gaussdb pid=$GPD_PID"

# 取一个 MoRe query 向量（filter_id=1 的查询文件）
cd ~/infra-compile-scripts/端到端性能测试/测试文件/MoReVec_small
QV=$(python3 -c "
import h5py, numpy as np
q = np.asarray(h5py.File('queries/queries_flex_reviews_sim_0_1.hdf5','r')['test'][0], dtype=np.float32)
print('[' + ','.join(repr(float(x)) for x in q) + ']')
")

cat > "$OUTDIR/q.sql" <<EOF
SET enable_vectorsearch = on;
SELECT id FROM more_ns.reviews WHERE $FILTER ORDER BY vec <-> '$QV'::vector LIMIT 10;
EOF

echo "filter=[$FILTER], 预热..."
$gsql -d postgres -p 37000 -f "$OUTDIR/q.sql" > /dev/null 2>&1 || true

# perf 采样（99Hz 带调用栈）：默认 -a 全系统；传了 PID 则只采该进程。
# 必须清 LD_LIBRARY_PATH。
unset LD_LIBRARY_PATH
export LD_LIBRARY_PATH=
PERF_TARGET="-a"
[ -n "$GPD_PID" ] && PERF_TARGET="-p $GPD_PID"
echo "采集 perf $PERF_TARGET 25s..."
perf record $PERF_TARGET -F 99 -g -o "$OUTDIR/perf.data" -- sleep 25 &
PFPID=$!
sleep 2
for i in $(seq 1 12); do
  $gsql -d postgres -p 37000 -f "$OUTDIR/q.sql" > /dev/null 2>&1
done
wait $PFPID 2>/dev/null || true

echo "生成火焰图..."
perf script -i "$OUTDIR/perf.data" | ~/FlameGraph/stackcollapse-perf.pl > "$OUTDIR/perf.folded" 2>/dev/null
~/FlameGraph/flamegraph.pl --title "MoReVec reviews: WHERE $FILTER LIMIT 10" --width 1600 \
  "$OUTDIR/perf.folded" > "$OUTDIR/flame.svg" 2>/dev/null
perf report -i "$OUTDIR/perf.data" --stdio --no-children 2>/dev/null | head -40 > "$OUTDIR/report.txt" || true

echo "完成: $OUTDIR/flame.svg / $OUTDIR/report.txt"
echo "提示: perf 采样的是 CPU，若 gaussdb 线程占比极低 => 瓶颈是等待(I/O/锁)，不是 CPU。"
