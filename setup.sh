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
step "1. 检查 binarylibs"

if [ ! -d "$BINARYLIBS_DIR/buildtools/gcc10.3" ]; then
    # 尝试从脚本所在目录的 tar.gz 自动解压
    TARBALL=$(ls "$SCRIPT_DIR"/openGauss-third_party_binarylibs_*.tar.gz 2>/dev/null | head -1)
    if [ -n "$TARBALL" ]; then
        echo "发现 binarylibs 压缩包: $TARBALL"
        echo "正在解压到 $ICEBERG_OG_ROOT ..."
        mkdir -p "$ICEBERG_OG_ROOT"
        cd "$ICEBERG_OG_ROOT"
        tar xzf "$TARBALL" 2>&1 | tail -1
        # 解压后目录名可能是 openGauss-third_party_binarylibs_*，重命名为 binarylibs
        EXTRACTED=$(ls -d openGauss-third_party_binarylibs_* 2>/dev/null | head -1)
        if [ -n "$EXTRACTED" ] && [ ! -d binarylibs ]; then
            mv "$EXTRACTED" binarylibs
        fi
        echo "解压完成"
    fi
fi

if [ ! -d "$BINARYLIBS_DIR/buildtools/gcc10.3" ]; then
    echo ""
    error "binarylibs 不存在或缺少 gcc10.3 工具链: $BINARYLIBS_DIR
请下载 openGauss third_party binarylibs 并解压到此目录，或将 tar.gz 放在脚本同级目录。
下载地址: https://opengauss.org/zh/download/"
fi
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
    local dir=$1 branch=$2 repo=$3
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
    echo "  [CLONE] $repo ($branch)"
    if git clone -b "$branch" "https://github.com/DataInfraLab/${repo}.git" "$ICEBERG_OG_ROOT/$dir" 2>&1; then
        :
    else
        error "git clone 失败。请检查:
  1. GITHUB_USER/GITHUB_TOKEN 是否正确（config.env）
  2. GITHUB_USER 是 GitHub 用户名，不是服务器用户名
  3. Token 是否有 public_repo 权限
  或手动 clone: git clone https://github.com/DataInfraLab/${repo}.git $ICEBERG_OG_ROOT/$dir"
    fi
}

setup_git_auth

REPOS=(
    "openGauss-server-datainfra:datainfra_dev:openGauss-server-datainfra"
    "iceberg-index:main:iceberg-index"
    "iceberg-rust-bridge:main:iceberg-rust-bridge"
    "iceberg_fdw:main:iceberg_fdw"
    "openGauss-Catalog:main:openGauss-Catalog"
    "iceberg_delta:master:iceberg_delta"
)
for entry in "${REPOS[@]}"; do
    IFS=':' read -r d b r <<< "$entry"
    clone_repo "$d" "$b" "$r"
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
    cargo check --workspace 2>&1 | tail -3

    cargo_flags=""
    [ "$BUILD_MODE" = "release" ] && cargo_flags="--release"
    cd "$ICEBERG_BRIDGE_REPO"
    cargo build $cargo_flags \
    --config "patch.\"https://github.com/DataInfraLab/iceberg-index.git\".iceberg-index-abi.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-abi\"" \
    --config "patch.\"https://github.com/DataInfraLab/iceberg-index.git\".iceberg-index-core.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-core\"" \
    --config "patch.\"https://github.com/DataInfraLab/iceberg-index.git\".iceberg-index-iceberg.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-iceberg\"" \
    --config "patch.\"https://github.com/DataInfraLab/iceberg-index.git\".iceberg-index-plugins.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-plugins\"" \
    --config "patch.\"https://github.com/DataInfraLab/iceberg-index.git\".iceberg-index-runtime.path=\"${ICEBERG_INDEX_REPO}/crates/iceberg-index-runtime\"" \
    2>&1 | tail -3

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
echo "Installing bridge..."
cp "$BRIDGE_SO" "$GAUSSHOME/lib/postgresql/libiceberg_rust_bridge.so"
echo "bridge OK"

# 8b. iceberg_fdw
if skip_or_rebuild "iceberg_fdw" "$GAUSSHOME/lib/postgresql/iceberg_fdw.so" "iceberg_fdw" "openGauss-server-datainfra"; then
    echo "building iceberg_fdw..."
    cd "$ICEBERG_FDW_REPO"
    make clean 2>/dev/null || true
    make PG_CONFIG="$GAUSSHOME/bin/pg_config" OPENGAUSS_SRC_INCLUDE="$OPENGAUSS_REPO/src/include" 2>&1 | tail -5
    make install PG_CONFIG="$GAUSSHOME/bin/pg_config" 2>&1 | tail -3
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
    make clean 2>/dev/null || true
    make PG_CONFIG="$GAUSSHOME/bin/pg_config" GAUSS_SRC="$OPENGAUSS_REPO" 2>&1 | tail -5
    cp iceberg_catalog.so "$GAUSSHOME/lib/postgresql/iceberg_catalog.so"
    cp iceberg_catalog.so "$GAUSSHOME/lib/postgresql/proc_srclib/iceberg_catalog.so"
    cp iceberg_catalog.control "$GAUSSHOME/share/postgresql/extension/"
    cp iceberg_catalog--1.0.0.sql "$GAUSSHOME/share/postgresql/extension/"
    echo "$BUILD_MODE" > "$GAUSSHOME/lib/postgresql/iceberg_catalog.so.build_mode"
fi
echo "iceberg_catalog OK"

# 8d. iceberg_delta (cmake)
if skip_or_rebuild "iceberg_delta" "$GAUSSHOME/lib/postgresql/iceberg_delta.so" "iceberg_delta" "openGauss-server-datainfra" "openGauss-Catalog"; then
    echo "building iceberg_delta..."
    DELTA_BUILD="$ICEBERG_DELTA_REPO/tmp_build_gcc10"
    rm -rf "$DELTA_BUILD"
    mkdir -p "$DELTA_BUILD"
    cd "$DELTA_BUILD"

    # cmake 需要系统 libstdc++（避免 GCC ABI 冲突）
    export CC="$GCC_HOME/bin/gcc" CXX="$GCC_HOME/bin/g++"
    export PATH="$GAUSSHOME/bin:$GCC_HOME/bin:/usr/bin:/bin"
    export LD_LIBRARY_PATH="/usr/lib64:/lib64:$GCC_HOME/lib64:$GCTOOLS/isl/lib:$GCTOOLS/mpc/lib:$GCTOOLS/mpfr/lib:$GCTOOLS/gmp/lib:$GAUSSHOME/lib"

    cmake_build_type="Debug"
    [ "$BUILD_MODE" = "release" ] && cmake_build_type="Release"
    cmake "$ICEBERG_DELTA_REPO" \
        -DCMAKE_BUILD_TYPE="$cmake_build_type" \
        -DGAUSS_SRC="$OPENGAUSS_REPO" \
        -DICEBERG_CATALOG_INCLUDE="$ICEBERG_CATALOG_REPO/src/include" 2>&1 | tail -3
    cmake --build . --parallel "$BUILD_JOBS" 2>&1 | tail -5
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
