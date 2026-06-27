#!/bin/bash
# ============================================================
# openGauss Iceberg — 单仓编译脚本
#
# 用法:
#   bash build.sh <target> [--release|--debug] [--force] [--pull]
#
# 目标:
#   opengauss  - 编译 openGauss 数据库（全量，耗时 30-60 分钟）
#   index      - cargo check iceberg-index（Rust 语法检查）
#   bridge     - 编译 iceberg-rust-bridge（依赖 iceberg-index）
#   fdw        - 编译 iceberg_fdw（依赖 openGauss）
#   catalog    - 编译 openGauss-Catalog（依赖 openGauss + bridge）
#   delta      - 编译 iceberg_delta（依赖 openGauss + catalog）
#
# --force: 全量重编（make clean / cargo clean / rm -rf build 后再编译）
# --pull:  编译前 git pull 目标仓库（及 Rust 依赖）最新代码
# 不加时: bridge/fdw/catalog/delta 走增量编译; opengauss 产物存在则跳过
#
# 示例:
#   bash build.sh fdw                    # 增量
#   bash build.sh bridge --release       # release 增量
#   bash build.sh catalog --force        # 全量重编
#   bash build.sh fdw --pull             # 拉最新代码 + 增量编译
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_REBUILD=false
PULL_BEFORE_BUILD=false
BUILD_MODE=""

# ---- 解析参数 ----
TARGET=""
for arg in "$@"; do
    case "$arg" in
        --force)     FORCE_REBUILD=true ;;
        --pull)      PULL_BEFORE_BUILD=true ;;
        --release)   BUILD_MODE=release ;;
        --debug)     BUILD_MODE=debug ;;
        -h|--help)
            sed -n '2,22p' "$0"
            exit 0
            ;;
        *)           TARGET="$arg" ;;
    esac
done

if [ -z "$TARGET" ]; then
    echo "[ERROR] 缺少目标，用法: bash build.sh <opengauss|index|bridge|fdw|catalog|delta>"
    exit 1
fi

# ---- 加载配置 ----
if [ -f "$SCRIPT_DIR/config.env" ]; then
    source "$SCRIPT_DIR/config.env"
fi

export ICEBERG_OG_ROOT="${ICEBERG_OG_ROOT:-$HOME/iceberg-og}"
export BINARYLIBS_DIR="${BINARYLIBS_DIR:-$ICEBERG_OG_ROOT/binarylibs}"
export BUILD_MODE="${BUILD_MODE:-debug}"
export RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://mirrors.tuna.tsinghua.edu.cn/rustup}"
export BUILD_JOBS="${BUILD_JOBS:-8}"

# ---- 派生路径 ----
OPENGAUSS_REPO="$ICEBERG_OG_ROOT/openGauss-server-datainfra"
ICEBERG_INDEX_REPO="$ICEBERG_OG_ROOT/iceberg-index"
ICEBERG_BRIDGE_REPO="$ICEBERG_OG_ROOT/iceberg-rust-bridge"
ICEBERG_FDW_REPO="$ICEBERG_OG_ROOT/iceberg_fdw"
ICEBERG_CATALOG_REPO="$ICEBERG_OG_ROOT/openGauss-Catalog"
ICEBERG_DELTA_REPO="$ICEBERG_OG_ROOT/iceberg_delta"
GAUSSHOME="$OPENGAUSS_REPO/mppdb_temp_install"

GCC_HOME="$BINARYLIBS_DIR/buildtools/gcc10.3/gcc"
GCTOOLS="$BINARYLIBS_DIR/buildtools/gcc10.3"
PYTHON_HOME="$BINARYLIBS_DIR/kernel/platform/python3.7"
SSL_HOME="$BINARYLIBS_DIR/kernel/dependency/openssl/comm"
BOOST_A="$BINARYLIBS_DIR/kernel/dependency/boost/comm/lib"
LOCAL_BOOST="$ICEBERG_OG_ROOT/local-boost-lib"

OG_SHIM="$HOME/tmp/og-python-bin"
BRIDGE_SO="$ICEBERG_BRIDGE_REPO/target/$([ "$BUILD_MODE" = "release" ] && echo release || echo debug)/libiceberg_rust_bridge.so"
BRIDGE_HEADER="$ICEBERG_BRIDGE_REPO/include/iceberg_bridge.h"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
step()  { echo -e "\n${GREEN}>>> $1${NC}"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }

# ============================================================
# 工具函数
# ============================================================

ensure_dir() { mkdir -p "$1"; }

check_file() {
    local desc="$1" path="$2"
    if [ ! -e "$path" ]; then
        error "$desc 不存在: $path"
    fi
}

# ============================================================
# 环境准备
# ============================================================

setup_python_shim() {
    ensure_dir "$OG_SHIM"
    ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python"
    ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python3"
}

setup_rust() {
    if ! command -v rustup >/dev/null 2>&1 && ! command -v ~/.cargo/bin/rustup >/dev/null 2>&1; then
        info "安装 rustup..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs > /tmp/rust-init.sh
        sed -i 's|RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://static.rust-lang.org/rustup}"|RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"|' /tmp/rust-init.sh
        export RUSTUP_DIST_SERVER="https://rsproxy.cn"
        bash /tmp/rust-init.sh -y --default-toolchain stable 2>&1 | tail -5
        source "$HOME/.cargo/env"
    fi

    if ! ~/.cargo/bin/rustc --version 2>/dev/null | grep -q "1.96"; then
        info "安装 Rust 1.96.0 toolchain..."
        export RUSTUP_DIST_SERVER="https://rsproxy.cn"
        export RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"
        ~/.cargo/bin/rustup toolchain install 1.96.0
        ~/.cargo/bin/rustup default 1.96.0
    fi

    mkdir -p ~/.cargo
    if ! grep -q "tuna-sparse" ~/.cargo/config.toml 2>/dev/null; then
        cat > ~/.cargo/config.toml << 'TOML'
[source.crates-io]
replace-with = "tuna-sparse"
[source.tuna-sparse]
registry = "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"
[net]
git-fetch-with-cli = true
retry = 3
TOML
    fi
    source "$HOME/.cargo/env"
    info "Rust: $(rustc --version)"
}

setup_boost_patch() {
    if [ ! -f "$LOCAL_BOOST/libboost_thread.so" ]; then
        info "生成本地 boost .so..."
        ensure_dir "$LOCAL_BOOST"
        cd "$LOCAL_BOOST"
        for lib in thread chrono system atomic; do
            gcc -shared -fPIC -o "libboost_${lib}.so" \
                -Wl,--whole-archive "${BOOST_A}/libboost_${lib}.a" \
                -Wl,--no-whole-archive 2>/dev/null
            ln -sf "libboost_${lib}.so" "libboost_${lib}_pic.so"
        done
    fi

    patch_boost() {
        local f=$1
        [ ! -f "$f" ] && return
        if [ -f "${f}.bak" ]; then
            cp "${f}.bak" "$f"
        else
            cp "$f" "${f}.bak"
        fi
        for lib in thread chrono system atomic; do
            sed -i "s|-lboost_${lib}\b|${LOCAL_BOOST}/libboost_${lib}.so|g" "$f"
            sed -i "s|-lboost_${lib}_pic\b|${LOCAL_BOOST}/libboost_${lib}_pic.so|g" "$f"
        done
    }

    patch_boost "$OPENGAUSS_REPO/src/gausskernel/Makefile"
    patch_boost "$OPENGAUSS_REPO/src/gausskernel/CMakeLists.txt"
}

check_binarylibs() {
    check_file "binarylibs (GCC10.3)" "$GCC_HOME/bin/gcc"
    check_file "binarylibs (Python3.7)" "$PYTHON_HOME/bin/python3.7"
    info "binarylibs: $(realpath $BINARYLIBS_DIR)"
}

install_bridge_to_gausshome() {
    if [ -d "$GAUSSHOME/lib/postgresql" ]; then
        info "安装 bridge .so 到 GAUSSHOME..."
        ensure_dir "$GAUSSHOME/lib/postgresql"
        cp "$BRIDGE_SO" "$GAUSSHOME/lib/postgresql/libiceberg_rust_bridge.so"
    fi
}

# ============================================================
# opengauss — 仅全量编译，产物存在且模式匹配则跳过
# ============================================================

build_opengauss() {
    step "编译 openGauss-server-datainfra ($BUILD_MODE 模式, 约 30-60 分钟)"

    check_binarylibs
    check_file "openGauss 源码" "$OPENGAUSS_REPO/build.sh"
    setup_python_shim
    setup_boost_patch

    local mode_file="$GAUSSHOME/bin/gaussdb.build_mode"
    if ! $FORCE_REBUILD && [ -f "$GAUSSHOME/bin/gaussdb" ] && [ -f "$mode_file" ] && [ "$(cat "$mode_file")" = "$BUILD_MODE" ]; then
        info "openGauss 产物已存在且模式匹配 (--force 强制重编)"
        "$GAUSSHOME/bin/gsql" --version 2>&1 || warn "gsql 验证失败"
        return 0
    fi
    if $FORCE_REBUILD; then
        info "--force: 强制全量重编"
    fi
    if $PULL_BEFORE_BUILD; then
        info "git pull openGauss-server-datainfra ($OPENGAUSS_BRANCH)"
        (cd "$OPENGAUSS_REPO" && git fetch origin && git checkout "$OPENGAUSS_BRANCH" && git pull origin "$OPENGAUSS_BRANCH") || warn "openGauss pull failed"
    fi

    source "$ICEBERG_OG_ROOT/opengauss.env" 2>/dev/null || true
    export PATH="$OG_SHIM:$GCC_HOME/bin:/usr/local/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

    cd "$OPENGAUSS_REPO"
    rm -rf tmp_build "$GAUSSHOME" 2>/dev/null || true

    info "Log: tail -f $HOME/og-build.log"
    sh build.sh -m "$BUILD_MODE" -3rd "$BINARYLIBS_DIR" 2>&1 | tee "$HOME/og-build.log"
    make -j1 2>&1 | tee -a "$HOME/og-build.log"

    [ -d "$GAUSSHOME/jre" ] && chmod -R u+w "$GAUSSHOME/jre" 2>/dev/null || true
    [ -d "$GAUSSHOME/python" ] && chmod -R u+w "$GAUSSHOME/python" 2>/dev/null || true

    make install -j1 2>&1 | tee -a "$HOME/og-build.log"
    echo "$BUILD_MODE" > "$mode_file"

    "$GAUSSHOME/bin/gsql" --version 2>&1 || error "openGauss 编译验证失败"
    info "openGauss 编译完成 — $(ls -lh $GAUSSHOME/bin/gaussdb | awk '{print $5}')"
}

# ============================================================
# index — 仅 Rust 语法检查
# ============================================================

build_index() {
    step "cargo check iceberg-index"

    setup_rust
    check_file "iceberg-index 源码" "$ICEBERG_INDEX_REPO/Cargo.toml"

    if $PULL_BEFORE_BUILD; then
        info "git pull iceberg-index ($ICEBERG_INDEX_BRANCH)"
        (cd "$ICEBERG_INDEX_REPO" && git fetch origin && git checkout "$ICEBERG_INDEX_BRANCH" && git pull origin "$ICEBERG_INDEX_BRANCH") || warn "iceberg-index pull failed"
    fi

    cd "$ICEBERG_INDEX_REPO"
    cargo check --workspace 2>&1
    info "iceberg-index 检查通过"
}

# ============================================================
# bridge — 增量: cargo build ;  --force: cargo clean + cargo build
# ============================================================

build_bridge() {
    step "编译 iceberg-rust-bridge ($BUILD_MODE 模式)"
    $FORCE_REBUILD && info "--force: cargo clean + 全量重编"

    setup_rust
    check_file "iceberg-index 源码" "$ICEBERG_INDEX_REPO/Cargo.toml"
    check_file "iceberg-rust-bridge 源码" "$ICEBERG_BRIDGE_REPO/Cargo.toml"

    if $PULL_BEFORE_BUILD; then
        info "git pull iceberg-index ($ICEBERG_INDEX_BRANCH)"
        (cd "$ICEBERG_INDEX_REPO" && git fetch origin && git checkout "$ICEBERG_INDEX_BRANCH" && git pull origin "$ICEBERG_INDEX_BRANCH") || warn "iceberg-index pull failed"
        info "git pull iceberg-rust-bridge ($ICEBERG_BRIDGE_BRANCH)"
        (cd "$ICEBERG_BRIDGE_REPO" && git fetch origin && git checkout "$ICEBERG_BRIDGE_BRANCH" && git pull origin "$ICEBERG_BRIDGE_BRANCH") || warn "iceberg-rust-bridge pull failed"
    fi

    export LD_LIBRARY_PATH=
    source "$HOME/.cargo/env"

    info "cargo check iceberg-index (依赖检查)..."
    cd "$ICEBERG_INDEX_REPO"
    cargo check --workspace 2>&1

    cargo_flags=""
    [ "$BUILD_MODE" = "release" ] && cargo_flags="--release"

    cd "$ICEBERG_BRIDGE_REPO"
    if $FORCE_REBUILD; then
        cargo clean 2>&1
    fi
    cargo build $cargo_flags \
        --config "patch.\"${ICEBERG_INDEX_CARGO_URL}\".iceberg-index-abi.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-abi\"" \
        --config "patch.\"${ICEBERG_INDEX_CARGO_URL}\".iceberg-index-core.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-core\"" \
        --config "patch.\"${ICEBERG_INDEX_CARGO_URL}\".iceberg-index-iceberg.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-iceberg\"" \
        --config "patch.\"${ICEBERG_INDEX_CARGO_URL}\".iceberg-index-plugins.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-plugins\"" \
        --config "patch.\"${ICEBERG_INDEX_CARGO_URL}\".iceberg-index-runtime.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-runtime\"" \
        2>&1

    echo "$BUILD_MODE" > "${BRIDGE_SO}.build_mode"
    ls -lh "$BRIDGE_SO"

    install_bridge_to_gausshome
    info "bridge 编译完成"
}

# ============================================================
# fdw — 增量: make ;  --force: make clean + make
# ============================================================

build_fdw() {
    step "编译 iceberg_fdw"
    $FORCE_REBUILD && info "--force: make clean + 全量重编"

    check_file "GAUSSHOME (pg_config)" "$GAUSSHOME/bin/pg_config"
    check_file "iceberg_fdw 源码" "$ICEBERG_FDW_REPO/Makefile"

    if $PULL_BEFORE_BUILD; then
        info "git pull iceberg_fdw ($ICEBERG_FDW_BRANCH)"
        (cd "$ICEBERG_FDW_REPO" && git fetch origin && git checkout "$ICEBERG_FDW_BRANCH" && git pull origin "$ICEBERG_FDW_BRANCH") || warn "iceberg_fdw pull failed"
    fi

    setup_python_shim
    export PATH="$OG_SHIM:$GCC_HOME/bin:$GAUSSHOME/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

    ensure_dir "$GAUSSHOME/lib/postgresql/proc_srclib"
    ensure_dir "$GAUSSHOME/share/postgresql/extension"

    cd "$ICEBERG_FDW_REPO"
    if $FORCE_REBUILD; then
        make clean 2>/dev/null || true
    fi
    make PG_CONFIG="$GAUSSHOME/bin/pg_config" \
        OPENGAUSS_SRC_INCLUDE="$OPENGAUSS_REPO/src/include" 2>&1
    make install PG_CONFIG="$GAUSSHOME/bin/pg_config" 2>&1

    cp iceberg_fdw.so "$GAUSSHOME/lib/postgresql/iceberg_fdw.so"
    cp iceberg_fdw.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_fdw.so"
    cp iceberg_fdw.control "$GAUSSHOME/share/postgresql/extension/"
    cp iceberg_fdw--0.1.0.sql "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_fdw.so.build_mode"

    info "iceberg_fdw 编译完成"
}

# ============================================================
# catalog — 增量: make ;  --force: make clean + make
# ============================================================

build_catalog() {
    step "编译 openGauss-Catalog"
    $FORCE_REBUILD && info "--force: make clean + 全量重编"

    check_file "GAUSSHOME (pg_config)" "$GAUSSHOME/bin/pg_config"
    check_file "bridge .so" "$BRIDGE_SO"
    check_file "bridge header" "$BRIDGE_HEADER"
    check_file "openGauss-Catalog 源码" "$ICEBERG_CATALOG_REPO/Makefile"

    if $PULL_BEFORE_BUILD; then
        info "git pull openGauss-Catalog ($ICEBERG_CATALOG_BRANCH)"
        (cd "$ICEBERG_CATALOG_REPO" && git fetch origin && git checkout "$ICEBERG_CATALOG_BRANCH" && git pull origin "$ICEBERG_CATALOG_BRANCH") || warn "openGauss-Catalog pull failed"
    fi

    setup_python_shim
    export PATH="$OG_SHIM:$GCC_HOME/bin:$GAUSSHOME/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

    ensure_dir "$ICEBERG_CATALOG_REPO/deps"
    cp "$BRIDGE_SO" "$ICEBERG_CATALOG_REPO/deps/libiceberg_rust_bridge.so"
    cp "$BRIDGE_HEADER" "$ICEBERG_CATALOG_REPO/deps/"

    ensure_dir "$GAUSSHOME/lib/postgresql/proc_srclib"
    ensure_dir "$GAUSSHOME/share/postgresql/extension"

    cd "$ICEBERG_CATALOG_REPO"
    if $FORCE_REBUILD; then
        make clean 2>/dev/null || true
    fi
    make PG_CONFIG="$GAUSSHOME/bin/pg_config" GAUSS_SRC="$OPENGAUSS_REPO" 2>&1

    cp iceberg_catalog.so "$GAUSSHOME/lib/postgresql/iceberg_catalog.so"
    cp iceberg_catalog.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_catalog.so"
    cp iceberg_catalog.control "$GAUSSHOME/share/postgresql/extension/"
    cp iceberg_catalog--1.0.0.sql "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_catalog.so.build_mode"

    info "iceberg_catalog 编译完成"
}

# ============================================================
# delta — 增量: cmake --build ;  --force: rm -rf build + cmake configure + build
# ============================================================

build_delta() {
    step "编译 iceberg_delta"

    check_file "GAUSSHOME (pg_config)" "$GAUSSHOME/bin/pg_config"
    check_file "catalog header" "$ICEBERG_CATALOG_REPO/src/include/iceberg_catalog.h"
    check_file "iceberg_delta 源码" "$ICEBERG_DELTA_REPO/CMakeLists.txt"

    if $PULL_BEFORE_BUILD; then
        info "git pull iceberg_delta ($ICEBERG_DELTA_BRANCH)"
        (cd "$ICEBERG_DELTA_REPO" && git fetch origin && git checkout "$ICEBERG_DELTA_BRANCH" && git pull origin "$ICEBERG_DELTA_BRANCH") || warn "iceberg_delta pull failed"
    fi

    DELTA_BUILD="$ICEBERG_DELTA_REPO/tmp_build_gcc10"
    local configure_needed=false

    if $FORCE_REBUILD; then
        info "--force: 清理构建目录 + 全量重编"
        rm -rf "$DELTA_BUILD"
        configure_needed=true
    elif [ ! -d "$DELTA_BUILD" ] || [ ! -f "$DELTA_BUILD/CMakeCache.txt" ]; then
        info "构建目录不存在，需要 cmake configure..."
        configure_needed=true
    fi

    # cmake 需要系统 libstdc++（避免 GCC ABI 冲突）
    export CC="$GCC_HOME/bin/gcc" CXX="$GCC_HOME/bin/g++"
    export PATH="$GAUSSHOME/bin:$GCC_HOME/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="/usr/lib64:/lib64:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib"

    if $configure_needed; then
        ensure_dir "$DELTA_BUILD"
        cd "$DELTA_BUILD"

        cmake_build_type="Debug"
        [ "$BUILD_MODE" = "release" ] && cmake_build_type="Release"

        cmake "$ICEBERG_DELTA_REPO" \
            -DCMAKE_BUILD_TYPE="$cmake_build_type" \
            -DGAUSS_SRC="$OPENGAUSS_REPO" \
            -DICEBERG_CATALOG_INCLUDE="$ICEBERG_CATALOG_REPO/src/include" 2>&1
        cmake --build . --parallel "$BUILD_JOBS" 2>&1
    else
        cd "$DELTA_BUILD"
        info "cmake --build (增量)..."
        cmake --build . --parallel "$BUILD_JOBS" 2>&1
    fi

    ensure_dir "$GAUSSHOME/lib/postgresql/proc_srclib"
    ensure_dir "$GAUSSHOME/share/postgresql/extension"

    cp iceberg_delta.so "$GAUSSHOME/lib/postgresql/iceberg_delta.so"
    cp iceberg_delta.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_delta.so"
    cp "$ICEBERG_DELTA_REPO/iceberg_delta.control" "$GAUSSHOME/share/postgresql/extension/"
    cp "$ICEBERG_DELTA_REPO/iceberg_delta--1.0.0.sql" "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_delta.so.build_mode"

    info "iceberg_delta 编译完成"
}

# ============================================================
# 依赖关系校验
# ============================================================

check_dep() {
    case "$1" in
        opengauss)
            check_file "binarylibs (GCC10.3)" "$GCC_HOME/bin/gcc"
            ;;
        bridge|fdw|catalog|delta)
            check_file "openGauss (GAUSSHOME)" "$GAUSSHOME/bin/pg_config"
            ;;
    esac

    case "$1" in
        catalog|delta)
            check_file "bridge .so" "$BRIDGE_SO"
            ;;
    esac

    case "$1" in
        delta)
            check_file "catalog header" "$ICEBERG_CATALOG_REPO/src/include/iceberg_catalog.h"
            ;;
    esac
}

# ============================================================
# 入口
# ============================================================

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  openGauss Iceberg — 单仓编译${NC}"
echo -e "${GREEN}  目标: ${TARGET}  |  模式: ${BUILD_MODE}${NC}"
$FORCE_REBUILD && echo -e "${GREEN}  --force: 全量重编${NC}"
$PULL_BEFORE_BUILD && echo -e "${GREEN}  --pull: 编译前拉取最新代码${NC}"
echo -e "${GREEN}============================================${NC}"

if [ "$TARGET" != "opengauss" ] && [ "$TARGET" != "index" ]; then
    check_dep "$TARGET"
fi

case "$TARGET" in
    opengauss) build_opengauss ;;
    index)     build_index ;;
    bridge)    build_bridge ;;
    fdw)       build_fdw ;;
    catalog)   build_catalog ;;
    delta)     build_delta ;;
    *)
        error "未知目标: $TARGET\n  可用目标: opengauss | index | bridge | fdw | catalog | delta"
        ;;
esac

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  ${TARGET} — 编译完成${NC}"
echo -e "${GREEN}============================================${NC}"
