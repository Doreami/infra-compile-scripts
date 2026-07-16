#!/bin/bash
# ============================================================
# 火焰图采集脚本 — 全扫 vs IVF 索引扫 性能剖析
# 用法: bash run_flamegraph.sh
# 前置条件:
#   1. perf 已安装 (dnf install perf)
#   2. FlameGraph 脚本在 ~/FlameGraph/
#   3. gaussdb -D ~/ogdata -p 37000 --single_node 已启动
#   4. sift_ns.sift1m 有 1M 行数据 + idx_ivf_vec 索引
#
# 输出: flamegraphs/YYYY-MM-DD/flame_<场景>.svg
# ============================================================
set -uo pipefail

# ── 配置 ──────────────────────────────────────────────────
source ~/iceberg-og/opengauss.env
export PATH="$GAUSSHOME/bin:$PATH"

GSQL="$GAUSSHOME/bin/gsql -d postgres -p 37000"
FLAMEGRAPH_DIR=~/FlameGraph
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TODAY=$(date +%Y-%m-%d)
OUTPUT_DIR="$SCRIPT_DIR/$TODAY/flamegraphs"
PERF_DATA_DIR="$OUTPUT_DIR/perf_data"

# 清理旧数据（同一天重复执行会覆盖）
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR" "$PERF_DATA_DIR"

STACKCOLLAPSE="$FLAMEGRAPH_DIR/stackcollapse-perf.pl"
FLAMEGRAPH="$FLAMEGRAPH_DIR/flamegraph.pl"

# 查询向量：从 id=500000 取 128 维 float 向量
QUERY_VEC='[16,14,9,5,9,5,14,9,6,40,128,37,11,0,0,0,25,103,103,12,0,0,0,0,15,45,61,0,0,0,0,0,128,29,3,0,0,0,5,28,128,117,128,33,0,0,0,20,12,27,128,128,2,0,1,3,44,24,50,50,1,0,0,2,128,11,4,0,0,0,0,53,128,7,3,2,0,0,19,128,7,1,17,48,9,24,94,45,8,8,21,42,11,16,24,8,31,39,96,0,0,0,0,6,81,43,53,1,0,0,5,65,28,30,27,4,0,4,31,22,2,3,39,12,0,2,13,10]'

# 元数据
GAUSSDB_PID=$(pgrep -f 'gaussdb.*ogdata.*37000' | head -1)
COMMIT_SHA=$(cd ~/iceberg-og 2>/dev/null && git log --oneline -1 2>/dev/null | cut -d' ' -f1 || echo "unknown")
KERNEL_VER=$(uname -r)

# ── 颜色 ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "[$(date +%H:%M:%S)] $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*"; exit 1; }

# ── 环境检查 ──────────────────────────────────────────────
log "============================================"
log "  火焰图采集"
log "  Date: $TODAY"
log "============================================"
log "GAUSSHOME: $GAUSSHOME"
log "gaussdb PID: $GAUSSDB_PID"
log "Kernel: $KERNEL_VER"
log "Commit: $COMMIT_SHA"
log "Output: $OUTPUT_DIR"
log ""

which perf >/dev/null 2>&1 || err "perf 未安装！请执行: sudo dnf install -y perf"
[ -f "$STACKCOLLAPSE" ] || err "$STACKCOLLAPSE 不存在"
[ -f "$FLAMEGRAPH" ] || err "$FLAMEGRAPH 不存在"
[ -n "$GAUSSDB_PID" ] || err "gaussdb 进程未找到！"

# ── 核心采集函数 ──────────────────────────────────────────
# 用法: run_perf <场景名> <SQL> <重复次数> <采样设计>
#   采样设计说明:
#     - 长查询 (>1s): 重复 1 次即可，99Hz × 2.4s ≈ 240 samples
#     - 短查询 (<1s): 循环重复，保证 >200 samples
run_perf() {
    local name="$1"
    local sql="$2"
    local repeat="${3:-1}"
    local desc="${4:-}"

    local perf_data="$PERF_DATA_DIR/perf_${name}_${TODAY}.data"
    local svg_out="$OUTPUT_DIR/flame_${name}_${TODAY}.svg"

    log ">>> $name (重复 ${repeat} 次, ${desc})"

    # 预热
    log "  预热..."
    echo "$sql" | $GSQL > /dev/null 2>&1 || warn "预热失败"

    # 启动 perf record
    log "  启动 perf record (PID=$GAUSSDB_PID)..."
    perf record -F 99 -g -p "$GAUSSDB_PID" -o "$perf_data" -- sleep 99999 &
# 注意: 要求编译时保留帧指针，否则调用栈会错乱
#   bridge: RUSTFLAGS="-C force-frame-pointers=yes"
#   gaussdb: CFLAGS="-fno-omit-frame-pointer"
    local PERF_PID=$!
    sleep 1.5

    # 执行查询
    log "  执行..."
    local q_start q_end elapsed
    q_start=$(date +%s%3N)
    for ((i=0; i<repeat; i++)); do
        echo "$sql" | $GSQL > /dev/null 2>&1
    done
    q_end=$(date +%s%3N)
    elapsed=$((q_end - q_start))
    log "  耗时: ${elapsed}ms"

    # 停止 perf
    sleep 0.5
    kill -INT "$PERF_PID" 2>/dev/null || true
    wait "$PERF_PID" 2>/dev/null || true

    # 验证 perf 数据有效
    local samples
    samples=$(perf report -i "$perf_data" --stdio 2>/dev/null | grep -c '^\s\+[0-9]' || echo 0)
    if [ "$samples" -eq 0 ]; then
        warn "perf 数据为空，跳过火焰图生成"
        rm -f "$perf_data"
        return
    fi

    # 生成火焰图
    log "  生成火焰图 ($samples samples)..."
    local tmp_script="/tmp/flame_script_${name}_$$.txt"
    perf script -i "$perf_data" 2>/dev/null > "$tmp_script"
    if [ -s "$tmp_script" ]; then
        "$STACKCOLLAPSE" < "$tmp_script" 2>/dev/null \
            | "$FLAMEGRAPH" \
                --title "SIFT1M $name ($TODAY)" \
                --width 1200 \
                --colors hot \
                > "$svg_out" 2>/dev/null
        rm -f "$tmp_script"

        if [ -s "$svg_out" ]; then
            ok "火焰图: $(basename "$svg_out") ($(du -h "$svg_out" | cut -f1), ${samples} samples, ${elapsed}ms)"
        else
            warn "火焰图生成失败"
        fi
    else
        warn "perf script 输出为空，跳过"
        rm -f "$tmp_script"
    fi
}

# ── SQL 模板 ──────────────────────────────────────────────
SQL_FULLSCAN="
SET enable_indexscan = off;
SET enable_bitmapscan = off;
SET enable_vectorsearch = off;
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> '$QUERY_VEC'::vector
LIMIT __K__;
"

SQL_IVF="
SET enable_vectorsearch = on;
SET try_vector_engine_strategy = force;
SELECT id FROM sift_ns.sift1m
ORDER BY vec <-> '$QUERY_VEC'::vector
LIMIT __K__;
"

SQL_BTREE="SELECT * FROM sift_ns.sift1m WHERE id = 500000;"

# ============================================================
# 采集场景（采样目标: 每个场景 >200 samples）
#   - 全扫 ~2.4s × 99Hz ≈ 240 samples → 1 轮足够
#   - IVF K=10 ~0.8s × 99Hz ≈ 80 samples   → 需要 3 轮
#   - IVF K=10000 ~1.7s × 99Hz ≈ 170 samples → 2 轮
#   - btree ~0.18s × 99Hz ≈ 18 samples     → 需要 50 轮
# ============================================================

# 全扫 (~2.4s/次, ~240 samples) — 2 轮确保 >400
run_perf "fullscan_k10"   "${SQL_FULLSCAN//__K__/10}"    2  "~2.4s×2"
run_perf "fullscan_k100"  "${SQL_FULLSCAN//__K__/100}"   2  "~2.4s×2"
run_perf "fullscan_k1000" "${SQL_FULLSCAN//__K__/1000}"  2  "~2.4s×2"

# IVF K=10 (~0.8s/次) — 5 轮累积 ~4s
run_perf "ivf_k10"        "${SQL_IVF//__K__/10}"         5  "~0.8s×5"
# IVF K=100 (~1.5s/次) — 3 轮累积 ~4.5s
run_perf "ivf_k100"       "${SQL_IVF//__K__/100}"        3  "~1.5s×3"
# IVF K=10000 (~1.7s/次) — 2 轮累积 ~3.5s
run_perf "ivf_k10000"     "${SQL_IVF//__K__/10000}"      2  "~1.7s×2"

# Btree (~0.18s/次) — 60 轮累积 ~10s
run_perf "btree_point"    "$SQL_BTREE"                   60 "~0.18s×60"

# ============================================================
# 汇总
# ============================================================
log ""
log "============================================"
log "  采集完成！"
log "============================================"
log ""

# 生成元信息
cat > "$OUTPUT_DIR/README.txt" <<EOF
火焰图采集结果
==============
Date: $TODAY
Run: $TODAY
gaussdb PID: $GAUSSDB_PID
Kernel: $KERNEL_VER
Commit: $COMMIT_SHA
Query Vector: id=500000 (SIFT1M)

场景:
  fullscan_k10     — 全表扫描, LIMIT 10
  fullscan_k100    — 全表扫描, LIMIT 100
  fullscan_k1000   — 全表扫描, LIMIT 1000
  ivf_k10          — IVF 索引扫描, K=10 (num_clusters=1024)
  ivf_k100         — IVF 索引扫描, K=100
  ivf_k10000       — IVF 索引扫描, K=10000
  btree_point      — Btree 点查 WHERE id=500000
EOF

log "SVG 火焰图:"
ls -lh "$OUTPUT_DIR"/flame_*.svg 2>/dev/null | awk '{print "  " $NF " (" $5 ")"}'
log ""
log "原始 perf 数据: $PERF_DATA_DIR"
log "元信息: $OUTPUT_DIR/README.txt"
log ""
log "在浏览器中打开 SVG 即可交互式查看（支持搜索/缩放/点击）"
