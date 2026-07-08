#!/bin/bash
# ============================================================
# openGauss Iceberg — 端到端测试入口
#
# 用法:
#   bash run_test.sh [--schedule <file>] [--case <name>] [--stop-on-failure] [--keep-temp]
#
# 首次运行会自动 clone DataInfra-devtest 测试仓库。
# 需要先完成编译（setup.sh 已跑通）。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 加载配置 ----
if [ -f "$SCRIPT_DIR/config.env" ]; then
    source "$SCRIPT_DIR/config.env"
fi

export ICEBERG_OG_ROOT="${ICEBERG_OG_ROOT:-$HOME/iceberg-og}"
export BINARYLIBS_DIR="${BINARYLIBS_DIR:-$ICEBERG_OG_ROOT/binarylibs}"
export BUILD_MODE="${BUILD_MODE:-debug}"

# ---- 派生路径 ----
OPENGAUSS_REPO="$ICEBERG_OG_ROOT/openGauss-server-datainfra"
GAUSSHOME="$OPENGAUSS_REPO/mppdb_temp_install"
DEVTEST_REPO="$ICEBERG_OG_ROOT/DataInfra-devtest"
DEVTEST_RUNNER="$DEVTEST_REPO/test/run_all.sh"

GCC_HOME="$BINARYLIBS_DIR/buildtools/gcc10.3/gcc"
GCTOOLS="$BINARYLIBS_DIR/buildtools/gcc10.3"
PYTHON_HOME="$BINARYLIBS_DIR/kernel/platform/python3.7"
SSL_HOME="$BINARYLIBS_DIR/kernel/dependency/openssl/comm"
OG_SHIM="$ICEBERG_OG_ROOT/tmp/og-python-bin"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# ---- 检查编译产物 ----
if [ ! -x "$GAUSSHOME/bin/gaussdb" ]; then
    echo -e "${RED}[ERROR]${NC} gaussdb 不存在: $GAUSSHOME/bin/gaussdb"
    echo "  请先运行: bash setup.sh"
    exit 1
fi

if [ ! -x "$GAUSSHOME/bin/gsql" ]; then
    echo -e "${RED}[ERROR]${NC} gsql 不存在: $GAUSSHOME/bin/gsql"
    echo "  请先运行: bash setup.sh"
    exit 1
fi

# ---- Clone DataInfra-devtest（首次） ----
if [ ! -x "$DEVTEST_RUNNER" ]; then
    echo -e "${YELLOW}[INFO]${NC} DataInfra-devtest 不存在，正在 clone..."
    DEVTEST_URL="https://github.com/DataInfraLab/DataInfra-devtest.git"
    git clone --depth 1 -b main "$DEVTEST_URL" "$DEVTEST_REPO" 2>&1 | tail -1
    if [ ! -x "$DEVTEST_RUNNER" ]; then
        echo -e "${RED}[ERROR]${NC} clone DataInfra-devtest 失败"
        echo "  手动 clone: git clone $DEVTEST_URL $DEVTEST_REPO"
        exit 1
    fi
fi

# ---- 设置测试环境 ----
export DATA_INFRA_ROOT="$ICEBERG_OG_ROOT"
export GAUSSHOME
export ICEBERG_OG_ROOT
export BINARYLIBS_DIR

# Python shim
mkdir -p "$OG_SHIM"
ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python" 2>/dev/null
ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python3" 2>/dev/null

# 环境变量
export PATH="$OG_SHIM:$GCC_HOME/bin:$GAUSSHOME/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"
export CC="$GCC_HOME/bin/gcc"
export CXX="$GCC_HOME/bin/g++"

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  openGauss Iceberg — 端到端测试${NC}"
echo -e "${GREEN}  GAUSSHOME: $GAUSSHOME${NC}"
echo -e "${GREEN}  gaussdb:   $($GAUSSHOME/bin/gsql --version 2>&1 | head -1)${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# ---- 运行测试 ----
exec "$DEVTEST_RUNNER" "$@"
