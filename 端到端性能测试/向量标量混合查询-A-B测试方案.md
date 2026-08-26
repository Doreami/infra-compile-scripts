# 向量+标量混合查询 · 物化路径优化 A/B 测试方案

> 对应设计文档《向量标量混合查询加速-物化路径优化-详细设计.md》§7。
> 口径：**同一条 SQL，现状 build vs 优化 build 的端到端延迟对比**，按实测存活率分档。
> 状态：**场景 A（MoReVec 真实过滤负载）数据已就绪，两 build + 基准待执行**。代码改动：#26 列子集读、#27-29 标量先行、`setup_morevec.py`（MoReVec 导入）、引擎 `ICEBERG_SCALAR_FIRST_LOG` 埋点。

---

## 1. 数据（场景 A：MoReVec 真实过滤负载）

**已导入服务器**（`setup_morevec.py`，MoReVec_small 从 Google Drive 经本地代理下载后 scp）：

| 表 | 行数 | 维度 | 过滤列 | 过滤选择性档（selectivities） |
|---|---|---|---|---|
| `more_ns.movies` | 9,999 | 768 | `avgrating` (double) | 1.2% / 2.3% / 5.3% / 10.9% / 20% / 51% |
| `more_ns.reviews` | 247,286 | 768 | `total_votes` (double) | 1% / 2% / 5% / 10% / 20% / 50% |

- 两表均建了 `idx_ivf_pq_vec`（IVF_PQ，num_clusters movies=32 / reviews=512），状态 active。
- 过滤定义：`filters/{movies,reviews}_filters_0.hdf5`（filter 字符串 + selectivities）。
- 每 filter 有独立查询负载：`queries/queries_flex_{type}_sim_0_{filter_id}.hdf5`（1000 查询 `test` + 真近邻 GT `mids`）。filter_id 0 = No_filter。
- movies 仅 9999 行（够验证正确性，性能意义小）；**reviews 是主基准表**。
- 冒烟已验证：两表 `WHERE {filter} ORDER BY vec <-> q LIMIT K` 均命中 `Materialization: scalar-first`。

> 备选：合成 cat（SIFT/GIST + `cat=行号%10`，`setup_fixed.py --cat-column`）可作对照数据集。

---

## 2. Build 切换（基线 vs 优化）

三个仓（iceberg-index / iceberg-rust-bridge / iceberg_fdw）的 #26-29 改动均为**未提交工作区改动**。
- **基线** = `git stash`（回到 HEAD 54d4877，改动前的现状代码）。
- **优化** = `git stash pop`（当前代码）。

```bash
# ── 基线 build ──
for r in iceberg-index iceberg-rust-bridge iceberg_fdw; do (cd ~/iceberg-og/$r && git stash); done
bash ~/infra-compile-scripts/build.sh bridge --release
bash ~/infra-compile-scripts/build.sh fdw --force --release    # fdw Makefile 无 header 依赖，必须 --force
# 重启共享 gaussdb 使新 .so 生效（见 §3）→ 跑基线基准（§4）

# ── 优化 build ──
for r in iceberg-index iceberg-rust-bridge iceberg_fdw; do (cd ~/iceberg-og/$r && git stash pop); done
bash ~/infra-compile-scripts/build.sh bridge --release
bash ~/infra-compile-scripts/build.sh fdw --force --release
# 重启 gaussdb → 跑优化基准
```

> **环境一致**：两 build 同机、同索引工件、同 DOP、同查询向量集合、同过滤 SQL。`git stash list` 确认 3 仓各有一条 stash，避免误 pop。

---

## 3. 重启共享 gaussdb（.so 换装后必做）

```bash
# 停
kill $(pgrep -u $USER -x gaussdb) 2>/dev/null; sleep 3
# 起（埋点：ICEBERG_SCALAR_FIRST_LOG=1 让引擎输出 fetch_k/survivors 到 server log）
ulimit -n 65536
ICEBERG_SCALAR_FIRST_LOG=1 nohup gaussdb -D ~/ogdata -p 37000 > ~/ogdata/server.log 2>&1 &
sleep 5
~/iceberg-og/openGauss-server-datainfra/mppdb_temp_install/bin/gsql -d postgres -p 37000 -c "SELECT 1;"
```

---

## 4. 过滤梯度基准

**查询向量**：MoRe 每 filter 的 query 负载 HDF5（`test` 数组，768d），取前 N 个（如 100）逐条跑。
**矩阵**：数据集 {movies, reviews} × K {1,10,100} × DOP {1,8} × 过滤档 {No_filter, 1%, 2%, 5%, 10%, 20%, 50%}。
**计时**：`EXPLAIN ANALYZE` 解析执行时间（同 bench_complete.py 的 `parse_ms`），每档 ≥3 轮取中位数，先 3 轮 warmup。

```sql
-- 每档 SQL（<t> = more_ns.reviews / more_ns.movies，<q> = 该 filter 的 query 向量文本，K 为档位）
SET query_dop = <DOP>; SET enable_vectorsearch = on;

-- 过滤档（reviews 用 total_votes，movies 用 avgrating）：
SELECT id FROM <t> ORDER BY vec <-> '<q>'::vector LIMIT K;                                 -- No_filter（确认无回归）
SELECT id FROM <t> WHERE total_votes >= 743.0  ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 1%
SELECT id FROM <t> WHERE total_votes >= 527.0  ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 2%
SELECT id FROM <t> WHERE total_votes >= 310.0  ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 5%
SELECT id FROM <t> WHERE total_votes >= 192.0  ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 10%
SELECT id FROM <t> WHERE total_votes >= 108.0  ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 20%
SELECT id FROM <t> WHERE total_votes >= 32.0   ORDER BY vec <-> '<q>'::vector LIMIT K;     -- 50%
```

**召回率**：MoRe query HDF5 自带真近邻 `mids`（每 filter 的 1000 查询）——用官方 GT 算 recall@K（基准 build 全表物化、优化 build 两阶段，两者 recall 应一致且达 IVF 预期）。

**埋点读取（实测存活率）**：
```bash
grep "\[scalar_first\]" ~/ogdata/server.log | tail -50
# 输出形如: [scalar_first] fetch_k=500 survivors=47 filter_cols=cat
# 按 存活率 = survivors/fetch_k 分组报告（不假设 cat 档位 = 存活率）
```

**预期形态**（设计 §7.4）：
- 低存活率档（~10%）加速最大（向量列读量降 ~10 倍）；高存活率档（~90%）接近现状或略慢（两阶段多一次窄读 + 求值开销）。
- **GIST 收益 > SIFT**（960d 向量列占宽比大）。
- 无过滤档（0%）与 ~90% 档不得劣化（无回归断言）。

---

## 5. 报告模板

| 数据集 | K | DOP | 存活率档 | 实测存活率 | 现状(ms) | 优化(ms) | 加速比 | 备注 |
|---|---|---|---|---|---|---|---|---|
| SIFT | 10 | 1 | 无过滤 | 100% | | | | 无回归断言 |
| SIFT | 10 | 1 | ~10% | | | | | 主价值档 |
| ... | | | | | | | | |
| GIST | 10 | 1 | ~10% | | | | | 应 > SIFT |

每档 ≥3 轮中位数；报告附：同索引工件、同 query 向量、DOP 与表分区形态（是否 _part）、埋点实测存活率。

---

## 6. 备注

- **IN 暂不可下推**（设计 §5.2/§7.2 注）：本方案过滤梯度用**范围谓词**（EQ/GE），cat 均匀 10 桶下与 IN 语义等效。
- **enable_vectorsearch** GUC 默认关，每条 SQL 前 `SET enable_vectorsearch = on`。
- **查询结果正确性**：每档先跑一次无 EXPLAIN 的普通查询核对返回行数符合存活率预期（顺带验证两阶段无假阳性/漏行）。
- 基线 build 时 EXPLAIN 的 `Materialization` 标注不存在（现状代码无此功能），只比较延迟，不比计划。
