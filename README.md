# 1. 创建用户

```shell
useradd test_user
passwd test_user
# gauss@123
```

# 2. clone仓库

```sh
git clone git@github.com:Doreami/infra-compile-scripts.git
cd infra-compile-scripts
```

# 3. Github配置token

操作路径：

[创建token链接](https://github.com/settings/tokens)

点击`Generate new token` -> `Generate new token (classic)` 来到创建token页面

起一个名字，选择到期时间，勾选repo全选，点击创建

会得到一个token，记得保存下来

# 4. 修改 config.env

```shell
# GitHub 认证（必填）
GITHUB_USER="你的GitHub用户名"
GITHUB_TOKEN="ghp_xxxx"

# 如需用 fork 仓：
# 全部 fork → 只改 GITHUB_ORG
GITHUB_ORG="your_username"
# 单个 fork → 覆盖对应 _REPO_URL
# ICEBERG_FDW_REPO_URL="https://github.com/your_fork/iceberg_fdw.git"
```

其他可配变量（一般无需修改）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BUILD_MODE` | `debug` | 编译模式: debug / release |
| `BUILD_JOBS` | `8` | 编译并发数 |
| `*_BRANCH` (9 个) | 各仓默认分支 | `--pull` 时使用的目标分支 |
| `RUSTUP_DIST_SERVER` | `rsproxy.cn` | Rust 工具链下载镜像 |

# 5. 执行一键搭建脚本

```shell
# 途中ssh有可能会被断开
# 首次运行 → 全量编译（clone 仓库 + 编译全家桶）
# 再次运行 → 检测已有产物自动跳过
bash setup.sh

# 可选参数：
--force             # 全量重编（make clean / cargo clean 后编译）
--skip-update       # 不拉代码，不 clone 不存在的仓库，仅编译
--debug             # 全链路 debug（默认）
--release           # 全链路 release

# 说明
# opengauss 仓仅支持全量编译 (通过检查二进制来判断是否要重编)
# 其他仓不加 --force 走增量编译（cargo build / make / cmake --build 自带增量检测）
# 加 --force 会先 clean 再全量编译
# 注意: setup.sh 不支持 --help，查看帮助用 build.sh --help
┌────────────────────────────────┬────────┬──────┐
│                                │ 拉代码  │ 编译  │
├────────────────────────────────┼────────┼──────┤
│ setup.sh                       │ ✅     │ 增量 │
├────────────────────────────────┼────────┼──────┤
│ setup.sh --force               │ ✅     │ 全量 │
├────────────────────────────────┼────────┼──────┤
│ setup.sh --skip-update         │ ❌     │ 增量 │
├────────────────────────────────┼────────┼──────┤
│ setup.sh --force --skip-update │ ❌     │ 全量 │
└────────────────────────────────┴────────┴──────┘
```

### 5b. 单独编译某个仓（无需 setup.sh，可直接使用）

首次搭建也可跳过 `setup.sh`，用 `build.sh` 逐个编译（需提前 clone 好仓库和 binarylibs）：

```shell
bash build.sh <目标> [--release|--debug] [--force] [--pull]
bash build.sh --help    # 查看完整帮助

# 目标:
#   opengauss  - openGauss 数据库（30-60 分钟，全量；产物存在则跳过）
#   bridge     - iceberg-rust-bridge（依赖 iceberg-index）
#   fdw        - iceberg_fdw（依赖 openGauss）
#   catalog    - openGauss-Catalog（依赖 openGauss + bridge）
#   delta      - iceberg_delta（依赖 openGauss + catalog）
#   index      - cargo check iceberg-index（仅 Rust 语法检查）

# 示例:
bash build.sh fdw                  # 增量编译
bash build.sh bridge --release     # release 模式
bash build.sh catalog --force      # make clean + 全量重编
bash build.sh fdw --pull           # 拉最新代码 + 增量编译
```

`bridge`/`fdw`/`catalog`/`delta` 不加 `--force` 走增量编译（构建系统自带增量检测）。  
加 `--force` 会先 `make clean` / `cargo clean` / `rm -rf build` 再全量编译。  
加 `--pull` 会在编译前 `git pull` 目标仓最新代码（bridge 会同时 pull iceberg-index）。

依赖链：`opengauss` → `bridge` → `catalog` → `delta`，`fdw` 只需 `opengauss`。  
脚本会自动检查前置依赖，缺失时提示先编译上游仓。

# 6. init && start db

```shell
# 0. 首先需要配置环境变量
export ICEBERG_WAREHOUSE=file://$HOME/warehouse

# 服务器上可能会存在多个opengauss，不建议端口使用默认值5432
# 1. 初始化数据目录（仅首次）
gs_initdb -D ~/ogdata --nodename=primary

# 2. 启动
gaussdb -D ~/ogdata -p YOUR_PORT --single_node &

# 3. 连接
gsql -d postgres -p YOUR_PORT

# 4. 首次需修改密码
# 执行select 1会提示你修改密码语句
select 1;
ALTER ROLE "user" PASSWORD 'gauss@123';
```

# 7. opengauss + iceberg使用示例

```sql
-- 1. 创建extension
CREATE EXTENSION iceberg_fdw;
CREATE EXTENSION iceberg_catalog;
CREATE EXTENSION iceberg_delta;

-- 2. 创建icberg namespace
SELECT iceberg_catalog.create_namespace('iceberg_ns', '{}'::jsonb) IS NOT NULL AS namespace_created;

-- 3. 建表
SELECT iceberg_catalog.create_table(
    'iceberg_ns',
    'test',
    '{"type":"struct","fields":['
    '{"id":1,"name":"id","type":"long","required":true},'
    '{"id":2,"name":"data","type":"string","required":false}'
    ']}'::jsonb
);

-- 查询表结构
SELECT field_name, field_type
FROM iceberg_catalog.table_schemas
WHERE table_uuid = (
    SELECT table_uuid FROM iceberg_catalog.tables_internal
    WHERE namespace = 'iceberg_ns' AND table_name = 'test'
)
ORDER BY field_position;

-- 查询metadata_location 
SELECT metadata_location IS NOT NULL AS metadata_location_set
FROM iceberg_catalog.tables_internal
WHERE namespace = 'iceberg_ns' AND table_name = 'test';

-- 查询
SELECT * FROM iceberg_ns.test;

-- explain
EXPLAIN (VERBOSE, COSTS OFF)
SELECT * FROM iceberg_ns.test;
```

# 8. 存储接 minIO（规划中）

> 计划支持将 Iceberg 表数据存储在 minIO 对象存储上，与本地文件系统 warehouse 并存。

```shell
# 示例环境变量
export ICEBERG_WAREHOUSE=s3://bucket-name/
export AWS_ENDPOINT=http://localhost:9000
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
```

# 9. 端到端测试

编译完成后运行回归测试：

```shell
# 首次会自动 clone DataInfra-devtest 测试仓库
bash run_test.sh

# 指定测试用例
bash run_test.sh --case 01_basic_opengauss

# 失败即停
bash run_test.sh --stop-on-failure
```

---

> **遇到问题？** 查看 `项目说明.md`，包含已知问题（boost ABI、cmake GLIBCXX 等）及解决方案。

