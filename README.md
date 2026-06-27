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
git lfs pull
# minio opengauss三方仓需要 git lfs pull

```

# 3. Github配置token

操作路径：

[创建token链接](https://github.com/settings/tokens)

点击`Generate new token` -> `Generate new token (classic)` 来到创建token页面

起一个名字，选择到期时间，勾选repo全选，点击创建

会得到一个token，记得保存下来

# 4. 修改config.env

```shell
GITHUB_USER="你的GitHub用户名"          # 创建这个 token 的 GitHub 账号
GITHUB_TOKEN="XXX" # token
```

# 5. 执行一键搭建脚本

```shell
# 途中ssh有可能会被断开
# 首次运行 → 全量编译
# 再次运行 → 检测已有产物自动跳过
sh setup.sh

# 特殊参数：
--force # 全量重编
--skip-update # 不拉代码，只检查产物是否存在
--debug            # 全链路 debug（默认）
--release          # 全链路 release

# 说明
# opengauss仓仅支持全量编译 (通过检查二进制来判断是否要重编)
# 支持增量的只有 bridge（cargo 自带）、fdw/catalog（make 只重编变动的 .o）、delta（cmake 同理）
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

# 8. 存储接minIO

`TODO`

```shell

```
