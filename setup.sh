#!/bin/bash
# ============================================================
# openGauss Iceberg 联合编译 — 一键环境搭建脚本
#
# 适用于 openEuler 22.03 / 24.03
# 使用说明: README.md
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_REBUILD=false
SKIP_UPDATE=false
for arg in "$@"; do
    case "$arg" in
        --force) FORCE_REBUILD=true ;;
        --skip-update) SKIP_UPDATE=true ;;
        --release) BUILD_MODE=release ;;
        --debug) BUILD_MODE=debug ;;
    esac
done

# 追踪哪些仓库有代码更新
declare -A REPO_UPDATED

skip_or_rebuild() {
    local label="$1" check_file="$2"
    shift 2
    # 检查编译模式是否匹配（无标记文件视为不匹配，触发重编）
    local mode_file="${check_file}.build_mode"
    if [ ! -f "$mode_file" ] || [ "$(cat "$mode_file")" != "$BUILD_MODE" ]; then
        echo "  [REBUILD] $label (编译模式: ${BUILD_MODE})"
        return 0
    fi
    if [ -f "$check_file" ] && ! $FORCE_REBUILD; then
        for repo in "$@"; do
            if [[ "${REPO_UPDATED[$repo]:-}" == "1" ]]; then
                echo "  [REBUILD] $label ($repo 代码已更新)"
                return 0
            fi
        done
        echo "  [SKIP] $label 已存在 (--force 强制重编)"
        return 1
    fi
    return 0
}

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
ICEBERG_RUST_DATAINFRA_REPO="$ICEBERG_OG_ROOT/iceberg-rust-datainfra"
ICEBERG_ARROW_DEPS_REPO="$ICEBERG_OG_ROOT/iceberg-arrow-deps"
ICEBERG_RUST_CACHE_REPO="$ICEBERG_OG_ROOT/iceberg-rust-cache"
ARROW_HOME="${ARROW_HOME:-$ICEBERG_OG_ROOT/arrow_install}"
# build.sh 硬编码安装到 mppdb_temp_install，不做额外处理
GAUSSHOME="$OPENGAUSS_REPO/mppdb_temp_install"

GCC_HOME="$BINARYLIBS_DIR/buildtools/gcc10.3/gcc"
GCTOOLS="$BINARYLIBS_DIR/buildtools/gcc10.3"
PYTHON_HOME="$BINARYLIBS_DIR/kernel/platform/python3.7"
SSL_HOME="$BINARYLIBS_DIR/kernel/dependency/openssl/comm"
BOOST_A="$BINARYLIBS_DIR/kernel/dependency/boost/comm/lib"
LOCAL_BOOST="$ICEBERG_OG_ROOT/local-boost-lib"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

step()  { echo -e "\n${GREEN}>>> $1${NC}"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ============================================================
# 0. 环境检查
# ============================================================
step "0. 检查系统环境"
echo "OS: $(cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2)"
echo "CPU: $(nproc) cores"
echo "Memory: $(free -h | awk '/Mem/{print $2}')"

for tool in gcc g++ cmake git curl; do
    if ! command -v $tool >/dev/null 2>&1; then
        error "缺少工具: $tool，请 sudo dnf install -y $tool"
    fi
done
echo "所有基础工具已安装"

# 系统依赖检查
MISSING_PKGS=()
for pkg in libedit-devel libxml2-devel lz4-devel numactl-devel \
    unixODBC-devel java-1.8.0-openjdk-devel libaio-devel flex bison \
    ncurses-devel glibc-devel patch readline-devel openblas-devel dkms; do
    rpm -q "$pkg" >/dev/null 2>&1 || MISSING_PKGS+=("$pkg")
done
if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    warn "缺少 ${#MISSING_PKGS[@]} 个依赖: ${MISSING_PKGS[*]}"
    echo "运行: sudo dnf install -y ${MISSING_PKGS[*]}"
    read -p "是否继续? (y/n) " -r; [[ $REPLY =~ ^[Yy]$ ]] || exit 0
fi

# ============================================================
# 1. 准备 binarylibs
# ============================================================
step "1. 准备 binarylibs"

if [ ! -d "$BINARYLIBS_DIR/buildtools/gcc10.3" ]; then
    # 根据 OS 版本拼装 binarylibs 文件名
    OS_VERSION=$(grep -oP 'VERSION_ID="?\K[0-9]+\.[0-9]+' /etc/os-release 2>/dev/null || echo "")
    case "$OS_VERSION" in
        24.03) BINARYLIBS_SUFFIX="openEuler_2403_x86_64" ;;
        22.03) BINARYLIBS_SUFFIX="openEuler_2203_x86_64" ;;
        *)     BINARYLIBS_SUFFIX="openEuler_x86_64" ;;
    esac
    TARBALL_NAME="openGauss-third_party_binarylibs_${BINARYLIBS_SUFFIX}.tar.gz"
    TARBALL="$SCRIPT_DIR/$TARBALL_NAME"
    DOWNLOADED=false

    # 兜底 URL（优先级：config.env 自定义 > 自动拼接）
    if [ -z "$BINARYLIBS_DOWNLOAD_URL" ]; then
        BINARYLIBS_DOWNLOAD_URL="https://opengauss.obs.cn-south-1.myhuaweicloud.com/latest/binarylibs/gcc10.3/$TARBALL_NAME"
    fi

    # 1) 检查脚本所在目录是否有 tar.gz（手动下载或已存在）
    if [ -f "$TARBALL" ]; then
        echo "发现本地 binarylibs 压缩包: $TARBALL"
    else
        # 也检查旧格式文件名（兼容）
        OLD_TARBALL=$(ls "$SCRIPT_DIR"/openGauss-third_party_binarylibs_*.tar.gz 2>/dev/null | head -1) || true
        if [ -n "$OLD_TARBALL" ] && [ "$OLD_TARBALL" != "$TARBALL" ]; then
            echo "发现已有 binarylibs: $OLD_TARBALL（使用中）"
            TARBALL="$OLD_TARBALL"
        fi
    fi

    if [ ! -f "$TARBALL" ]; then
        # 2) 自动从华为云 OBS 下载（国内速度快，~800MB）
        echo "OS: openEuler ${OS_VERSION:-unknown} → ${TARBALL_NAME}"
        echo "URL: $BINARYLIBS_DOWNLOAD_URL"

        if command -v wget >/dev/null 2>&1; then
            wget -c "$BINARYLIBS_DOWNLOAD_URL" -O "$TARBALL" 2>&1
        elif command -v curl >/dev/null 2>&1; then
            curl -C - -L -o "$TARBALL" "$BINARYLIBS_DOWNLOAD_URL"
        else
            error "缺少 wget 或 curl，无法下载 binarylibs。请手动下载后放到: $SCRIPT_DIR/"
        fi

        [ -f "$TARBALL" ] || error "binarylibs 下载失败。请检查网络后重试"
        DOWNLOADED=true
    fi

    echo "正在解压到 $ICEBERG_OG_ROOT ..."
    mkdir -p "$ICEBERG_OG_ROOT"
    cd "$ICEBERG_OG_ROOT"
    tar xzf "$TARBALL" 2>&1 | tail -1
    EXTRACTED=$(ls -d openGauss-third_party_binarylibs_* 2>/dev/null | head -1) || true
    if [ -n "$EXTRACTED" ] && [ ! -d binarylibs ]; then
        mv "$EXTRACTED" binarylibs
    fi
    echo "解压完成"

    # 若为自动下载，可选择删除 tar.gz 以节省磁盘
    if $DOWNLOADED; then
        echo "下载的 tar.gz 保留在: $TARBALL (如需节省磁盘可手动删除)"
    fi
fi

check_binarylibs() {
    [ -d "$BINARYLIBS_DIR/buildtools/gcc10.3" ] || \
        error "binarylibs 不存在或缺少 gcc10.3 工具链: $BINARYLIBS_DIR"
}
check_binarylibs
echo "binarylibs OK: $(realpath $BINARYLIBS_DIR)"

# ============================================================
# 2. 克隆仓库
# ============================================================
step "2. 同步代码仓库"

mkdir -p "$ICEBERG_OG_ROOT"
cd "$ICEBERG_OG_ROOT"

# GitHub 认证配置
setup_git_auth() {
    if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_USER:-}" ]; then
        git config --global credential.helper store
        echo "https://${GITHUB_USER}:${GITHUB_TOKEN}@github.com" > ~/.git-credentials
        chmod 600 ~/.git-credentials
        echo "GitHub 认证已配置"
    fi
}

clone_repo() {
    local dir=$1 branch=$2 url=$3
    if [ -d "$ICEBERG_OG_ROOT/$dir/.git" ]; then
        if $SKIP_UPDATE; then
            echo "  [SKIP] $dir"
        else
            echo "  [UPDATE] $dir ($branch)"
            local before=$(cd "$ICEBERG_OG_ROOT/$dir" && git rev-parse HEAD 2>/dev/null)
            (cd "$ICEBERG_OG_ROOT/$dir" && git fetch origin && git checkout "$branch" && git pull origin "$branch" 2>&1) || \
                { warn "$dir 更新失败，使用本地版本继续"; return; }
            local after=$(cd "$ICEBERG_OG_ROOT/$dir" && git rev-parse HEAD 2>/dev/null)
            if [ "$before" != "$after" ]; then
                REPO_UPDATED["$dir"]=1
                echo "  [UPDATED] $dir: ${before:0:7} → ${after:0:7}"
            fi
        fi
        return
    fi
    echo "  [CLONE] $dir ($branch)"
    if git clone -b "$branch" "$url" "$ICEBERG_OG_ROOT/$dir" 2>&1; then
        :
    else
        error "git clone 失败。请检查:
  1. GITHUB_USER/GITHUB_TOKEN 是否正确（config.env）
  2. GITHUB_USER 是 GitHub 用户名，不是服务器用户名
  3. Token 是否有 public_repo 权限
  或手动 clone: git clone $url $ICEBERG_OG_ROOT/$dir"
    fi
}

setup_git_auth

REPOS=(
    "openGauss-server-datainfra:$OPENGAUSS_BRANCH:$OPENGAUSS_REPO_URL"
    "iceberg-index:$ICEBERG_INDEX_BRANCH:$ICEBERG_INDEX_REPO_URL"
    "iceberg-rust-bridge:$ICEBERG_BRIDGE_BRANCH:$ICEBERG_BRIDGE_REPO_URL"
    "iceberg_fdw:$ICEBERG_FDW_BRANCH:$ICEBERG_FDW_REPO_URL"
    "openGauss-Catalog:$ICEBERG_CATALOG_BRANCH:$ICEBERG_CATALOG_REPO_URL"
    "iceberg_delta:$ICEBERG_DELTA_BRANCH:$ICEBERG_DELTA_REPO_URL"
    "iceberg-rust-datainfra:$ICEBERG_RUST_DATAINFRA_BRANCH:$ICEBERG_RUST_DATAINFRA_REPO_URL"
    "iceberg-arrow-deps:$ICEBERG_ARROW_DEPS_BRANCH:$ICEBERG_ARROW_DEPS_REPO_URL"
    "iceberg-rust-cache:$ICEBERG_RUST_CACHE_BRANCH:$ICEBERG_RUST_CACHE_REPO_URL"
)
for entry in "${REPOS[@]}"; do
    IFS=':' read -r d b u <<< "$entry"
    clone_repo "$d" "$b" "$u"
done
echo "代码同步完成"

# ============================================================
# 3. 创建配置文件
# ============================================================
step "3. 创建 openGauss 编译配置"

cat > "$ICEBERG_OG_ROOT/opengauss.env" << EOF
#!/bin/bash
export GAUSSHOME="\${GAUSSHOME:-$GAUSSHOME}"
echo "opengauss.env loaded: GAUSSHOME=\${GAUSSHOME}"
EOF

# 本地配置（不影响 git）
LOCAL_ENV="$OPENGAUSS_REPO/iceberg-opengauss-build/local.env"
if [ -f "$LOCAL_ENV.example" ]; then
    cp "$LOCAL_ENV.example" "$LOCAL_ENV"
fi

echo "配置文件已创建: opengauss.env"

# ============================================================
# 4. 安装 Rust
# ============================================================
step "4. 安装 Rust 1.96.0"

# rustup 本体（首次安装）
if ! command -v rustup >/dev/null 2>&1 && ! command -v ~/.cargo/bin/rustup >/dev/null 2>&1; then
    echo "安装 rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs > /tmp/rust-init.sh
    # 修改 rustup-init 下载地址为国内镜像
    sed -i 's|RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://static.rust-lang.org/rustup}"|RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"|' /tmp/rust-init.sh
    export RUSTUP_DIST_SERVER="https://rsproxy.cn"
    bash /tmp/rust-init.sh -y --default-toolchain stable 2>&1
    source "$HOME/.cargo/env"
fi

# 安装/更新 1.96.0 toolchain（rsproxy 镜像，速度快）
if ~/.cargo/bin/rustc --version 2>/dev/null | grep -q "1.96"; then
    echo "Rust 1.96.0 已安装: $(~/.cargo/bin/rustc --version)"
else
    echo "安装 Rust 1.96.0 toolchain (rsproxy)..."
    export RUSTUP_DIST_SERVER="https://rsproxy.cn"
    export RUSTUP_UPDATE_ROOT="https://rsproxy.cn/rustup"
    ~/.cargo/bin/rustup toolchain install 1.96.0
    ~/.cargo/bin/rustup default 1.96.0
    source "$HOME/.cargo/env"
fi

# Cargo 国内镜像（tuna-sparse，下载 crate 依赖快）
mkdir -p ~/.cargo
cat > ~/.cargo/config.toml << 'TOML'
[source.crates-io]
replace-with = "tuna-sparse"
[source.tuna-sparse]
registry = "sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/"
[net]
git-fetch-with-cli = true
retry = 3
TOML
source "$HOME/.cargo/env"
echo "Rust: $(rustc --version)"

# ============================================================
# 5. 平台兼容处理（openEuler 24.03 需要）
# ============================================================
step "5. 平台兼容性处理"

echo "系统 GCC: $(gcc --version | head -1)"
echo "编译 GCC: $($GCC_HOME/bin/gcc --version | head -1)"

# 5a. 从 binarylibs .a 生成 .so（解决 boost ABI 不兼容）
if [ ! -f "$LOCAL_BOOST/libboost_thread.so" ]; then
    echo "生成本地 boost .so 文件..."
    mkdir -p "$LOCAL_BOOST"
    cd "$LOCAL_BOOST"
    for lib in thread chrono system atomic; do
        gcc -shared -fPIC -o "libboost_${lib}.so" \
            -Wl,--whole-archive "${BOOST_A}/libboost_${lib}.a" \
            -Wl,--no-whole-archive 2>/dev/null
        ln -sf "libboost_${lib}.so" "libboost_${lib}_pic.so"
    done
    echo "本地 boost .so 已生成: $LOCAL_BOOST"
fi

# 5b. Patch Makefile + CMakeLists.txt（用本地 boost 代替系统 boost）
echo "Patching boost 链接路径..."

patch_boost() {
    local f=$1
    [ ! -f "$f" ] && return
    cp "$f" "${f}.bak"
    for lib in thread chrono system atomic; do
        sed -i "s|-lboost_${lib}\b|${LOCAL_BOOST}/libboost_${lib}.so|g" "$f"
        sed -i "s|-lboost_${lib}_pic\b|${LOCAL_BOOST}/libboost_${lib}_pic.so|g" "$f"
    done
}

patch_boost "$OPENGAUSS_REPO/src/gausskernel/Makefile"
patch_boost "$OPENGAUSS_REPO/src/gausskernel/CMakeLists.txt"
echo "Patch 完成"

# 5c. 修复 onnxruntime 符号链接（2403 binarylibs 默认指向 1.16.3，但 onnx_wrapper 需要 1.22.0）
ONNX_LIB="$BINARYLIBS_DIR/kernel/dependency/onnxruntime"
if [ -f "$ONNX_LIB/comm/lib/libonnx_wrapper.so" ] && [ -f "$ONNX_LIB/comm/lib/libonnxruntime.so.1.22.0" ]; then
    echo "修正 onnxruntime 符号链接 → 1.22.0..."
    for subdir in comm llt; do
        ln -sfn libonnxruntime.so.1.22.0 "$ONNX_LIB/$subdir/lib/libonnxruntime.so.1"
        ln -sfn libonnxruntime.so.1 "$ONNX_LIB/$subdir/lib/libonnxruntime.so"
    done
    echo "onnxruntime 链接已修正"
fi


# ============================================================
# 6. 编译 openGauss
# ============================================================
step "6. 编译 openGauss ($BUILD_MODE 模式, 约 30-60 分钟)"

# Python shim
OG_SHIM="$HOME/tmp/og-python-bin"
mkdir -p "$OG_SHIM"
ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python"
ln -sfn "$PYTHON_HOME/bin/python3.7" "$OG_SHIM/python3"

source "$ICEBERG_OG_ROOT/opengauss.env"
export PATH="$OG_SHIM:$GCC_HOME/bin:/usr/local/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

if skip_or_rebuild "openGauss" "$GAUSSHOME/bin/gaussdb" "openGauss-server-datainfra"; then
    cd "$OPENGAUSS_REPO"
    rm -rf tmp_build "$GAUSSHOME" 2>/dev/null || true

    echo "Log: $(date) | tail -f $HOME/og-build.log"
    export CFLAGS="-fno-omit-frame-pointer"
    export CXXFLAGS="-fno-omit-frame-pointer"
    sh build.sh -m "$BUILD_MODE" -3rd "$BINARYLIBS_DIR" 2>&1 | tee "$HOME/og-build.log"

    make -j1 2>&1 | tee -a "$HOME/og-build.log"

    [ -d "$GAUSSHOME/jre" ] && chmod -R u+w "$GAUSSHOME/jre" 2>/dev/null || true
    [ -d "$GAUSSHOME/python" ] && chmod -R u+w "$GAUSSHOME/python" 2>/dev/null || true

    make install -j1 2>&1 | tee -a "$HOME/og-build.log"
    echo "$BUILD_MODE" > "$GAUSSHOME/bin/gaussdb.build_mode"
fi

"$GAUSSHOME/bin/gsql" --version 2>&1 || error "openGauss 验证失败"
test -x "$GAUSSHOME/bin/gaussdb" && echo "gaussdb: $(ls -lh $GAUSSHOME/bin/gaussdb | awk '{print $5}')"
echo "openGauss 编译完成"

# ============================================================
# 7. 编译 Rust 组件
# ============================================================
step "7. 编译 iceberg-rust-bridge"

BRIDGE_SO="$ICEBERG_BRIDGE_REPO/target/$([ "$BUILD_MODE" = "release" ] && echo release || echo debug)/libiceberg_rust_bridge.so"
if skip_or_rebuild "iceberg-rust-bridge" "$BRIDGE_SO" "iceberg-rust-bridge" "iceberg-index"; then
    export LD_LIBRARY_PATH=   # Rust 不能用 GCC10 的 libstdc++
    source "$HOME/.cargo/env"

    cd "$ICEBERG_INDEX_REPO"
    cargo check --workspace 2>&1

    cargo_flags=""
    [ "$BUILD_MODE" = "release" ] && cargo_flags="--release"
    cd "$ICEBERG_BRIDGE_REPO"
    if $FORCE_REBUILD; then
        cargo clean 2>&1
    fi
    export RUSTFLAGS="-C force-frame-pointers=yes"  # perf -g 火焰图需要帧指针
    cargo build $cargo_flags 2>&1

    ls -lh "$BRIDGE_SO"
    echo "$BUILD_MODE" > "${BRIDGE_SO}.build_mode"
fi
echo "Rust bridge 编译完成"

# ============================================================
# 8. 编译扩展 (FDW / Catalog / Delta)
# ============================================================
step "8. 编译扩展组件"

# 扩展编译环境（GAUSSHOME 已在步骤 6 设置）
export PATH="$OG_SHIM:$GCC_HOME/bin:$GAUSSHOME/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

mkdir -p "$GAUSSHOME/lib/postgresql/proc_srclib" "$GAUSSHOME/share/postgresql/extension"

# 8a. Install bridge .so (always, lightweight)
export ICEBERG_RUST_BRIDGE_HOME="${ICEBERG_RUST_BRIDGE_HOME:-$ICEBERG_BRIDGE_REPO}"
echo "Installing bridge..."
cp "$BRIDGE_SO" "$GAUSSHOME/lib/postgresql/libiceberg_rust_bridge.so"
echo "bridge OK"

# 8b. iceberg_fdw
if skip_or_rebuild "iceberg_fdw" "$GAUSSHOME/lib/postgresql/iceberg_fdw.so" "iceberg_fdw" "openGauss-server-datainfra"; then
    echo "building iceberg_fdw..."
    cd "$ICEBERG_FDW_REPO"
    if $FORCE_REBUILD; then
        make clean 2>/dev/null || true
    fi
    make PG_CONFIG="$GAUSSHOME/bin/pg_config" OPENGAUSS_SRC_INCLUDE="$OPENGAUSS_REPO/src/include" 2>&1
    make install PG_CONFIG="$GAUSSHOME/bin/pg_config" 2>&1
    cp iceberg_fdw.so "$GAUSSHOME/lib/postgresql/iceberg_fdw.so"
    cp iceberg_fdw.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_fdw.so"
    cp iceberg_fdw.control "$GAUSSHOME/share/postgresql/extension/"
    cp iceberg_fdw--0.1.0.sql "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_fdw.so.build_mode"
fi
echo "iceberg_fdw OK"

# 8c. openGauss-Catalog
if skip_or_rebuild "iceberg_catalog" "$GAUSSHOME/lib/postgresql/iceberg_catalog.so" "openGauss-Catalog" "iceberg-rust-bridge" "openGauss-server-datainfra"; then
    echo "building openGauss-Catalog..."
    mkdir -p "$ICEBERG_CATALOG_REPO/deps"
    cp "$BRIDGE_SO" "$ICEBERG_CATALOG_REPO/deps/libiceberg_rust_bridge.so"
    cp "$ICEBERG_BRIDGE_REPO/include/iceberg_bridge.h" "$ICEBERG_CATALOG_REPO/deps/"
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
fi
echo "iceberg_catalog OK"

# 8d. iceberg_delta (cmake) — 需要 Apache Arrow，缺失时自动构建
if skip_or_rebuild "iceberg_delta" "$GAUSSHOME/lib/postgresql/iceberg_delta.so" "iceberg_delta" "openGauss-server-datainfra" "openGauss-Catalog"; then
    # 检查/构建 Arrow
    if [ ! -f "$ARROW_HOME/lib64/libarrow.so" ] && [ ! -f "$ARROW_HOME/lib/libarrow.so" ] && \
       ! ldconfig -p 2>/dev/null | grep -q libarrow && \
       [ ! -f /usr/lib64/libarrow.so ] && [ ! -f /usr/lib/libarrow.so ]; then
        if [ -f "$ICEBERG_ARROW_DEPS_REPO/build_arrow.sh" ]; then
            echo "Apache Arrow C++ 未安装，自动构建..."
            echo "  ARROW_HOME=$ARROW_HOME"
            # cmake 需系统 libstdc++，不能用 GCC10 的
            env CC="$GCC_HOME/bin/gcc" CXX="$GCC_HOME/bin/g++" \
                LD_LIBRARY_PATH="/usr/lib64:/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib" \
                bash "$ICEBERG_ARROW_DEPS_REPO/build_arrow.sh"
            [ -f "$ARROW_HOME/lib64/libarrow.so" ] || [ -f "$ARROW_HOME/lib/libarrow.so" ] || \
                error "Arrow 构建完成但未找到 libarrow.so"
        else
            warn "跳过 iceberg_delta：iceberg-arrow-deps 仓库未克隆且 Arrow 未安装"
            echo "  (不影响其他组件运行)"
            return 0  # skip this if-block, continue to "iceberg_delta OK"
        fi
    fi
    echo "building iceberg_delta..."
    DELTA_BUILD="$ICEBERG_DELTA_REPO/tmp_build_gcc10"
    configure_needed=false

    if $FORCE_REBUILD; then
        rm -rf "$DELTA_BUILD"
        configure_needed=true
    elif [ ! -d "$DELTA_BUILD" ] || [ ! -f "$DELTA_BUILD/CMakeCache.txt" ]; then
        configure_needed=true
    fi

    # cmake 需要系统 libstdc++（避免 GCC ABI 冲突）
    export CC="$GCC_HOME/bin/gcc" CXX="$GCC_HOME/bin/g++"
    export PATH="$GAUSSHOME/bin:$GCC_HOME/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="/usr/lib64:/lib64:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib"

    if $configure_needed; then
        mkdir -p "$DELTA_BUILD"
        cd "$DELTA_BUILD"

        cmake_build_type="Debug"
        [ "$BUILD_MODE" = "release" ] && cmake_build_type="Release"
        cmake "$ICEBERG_DELTA_REPO" \
            -DCMAKE_BUILD_TYPE="$cmake_build_type" \
            -DGAUSS_SRC="$OPENGAUSS_REPO" \
            -DICEBERG_CATALOG_INCLUDE="$ICEBERG_CATALOG_REPO/src/include" \
            -DARROW_HOME="$ARROW_HOME" \
            -DICEBERG_RUST_BRIDGE_HOME="$ICEBERG_BRIDGE_REPO" 2>&1
        cmake --build . --parallel "$BUILD_JOBS" 2>&1
    else
        cd "$DELTA_BUILD"
        cmake --build . --parallel "$BUILD_JOBS" 2>&1
    fi
    cp iceberg_delta.so "$GAUSSHOME/lib/postgresql/iceberg_delta.so"
    cp iceberg_delta.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_delta.so"
    cp "$ICEBERG_DELTA_REPO/iceberg_delta.control" "$GAUSSHOME/share/postgresql/extension/"
    cp "$ICEBERG_DELTA_REPO/iceberg_delta--1.0.0.sql" "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_delta.so.build_mode"
fi
echo "iceberg_delta OK"

# ============================================================
# 9. 验证
# ============================================================
step "9. 验证安装"

export LD_LIBRARY_PATH="$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib:$GAUSSHOME/lib/postgresql:$PYTHON_HOME/lib:$SSL_HOME/lib:/usr/lib64:/lib64"

echo ""
echo "============================================"
echo "  openGauss Iceberg 联调环境"
echo "============================================"
echo ""
echo " openGauss: $("$GAUSSHOME/bin/gsql" --version 2>&1)"
echo " gaussdb:   $(ls -lh $GAUSSHOME/bin/gaussdb | awk '{print $5}')"
echo ""
echo " 组件产物:"
for so in libiceberg_rust_bridge iceberg_fdw iceberg_catalog iceberg_delta; do
    f="$GAUSSHOME/lib/postgresql/${so}.so"
    if [ -f "$f" ]; then
        printf "   %-30s %s\n" "${so}.so" "$(ls -lh $f | awk '{print $5}')"
    else
        printf "   %-30s MISSING!\n" "${so}.so"
    fi
done
echo ""
echo " GAUSSHOME: $GAUSSHOME"
echo "============================================"
echo "  搭建完成！"
echo "============================================"

# ============================================================
# 10. 写入环境变量到 ~/.bashrc
# ============================================================
step "10. 配置终端环境变量"

ENV_MARKER="# >>> openGauss Iceberg env (auto-generated by setup.sh) <<<"
ENV_BLOCK=$(cat << EOF
$ENV_MARKER
export GAUSSHOME="$GAUSSHOME"
export PATH="\$GAUSSHOME/bin:\$PATH"
export LD_LIBRARY_PATH="\$GAUSSHOME/lib:\$GAUSSHOME/lib/postgresql:\$LD_LIBRARY_PATH"
# >>> end openGauss Iceberg env <<<
EOF
)

if grep -q "$ENV_MARKER" ~/.bashrc 2>/dev/null; then
    echo "~/.bashrc 已包含环境变量，跳过"
else
    echo "" >> ~/.bashrc
    echo "$ENV_BLOCK" >> ~/.bashrc
    echo "环境变量已写入 ~/.bashrc，重新登录或执行 source ~/.bashrc 生效"
fi
