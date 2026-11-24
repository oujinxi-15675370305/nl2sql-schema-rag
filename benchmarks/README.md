# 公开集评测（Step 4）

这里放**外部有效性**评测，和 `eval/` 的自建题分开：

- 不并入基础 50 / 多轮 / hard / complex；
- 不进 CI；
- 不用于调 prompt；
- 不在没真跑过时写分数。

目前只接 **BIRD Mini-Dev（SQLite）**。它换的是 schema、表名、JOIN 路径和问法，用来回答“离开 demo 的 6 张表还剩多少”。

## 下载数据

BIRD Mini-Dev 原始包约 800MB（CC BY-SA 4.0），不提交到仓库：

```bash
mkdir -p benchmarks/.data
curl -L -o benchmarks/.data/minidev.zip \
  https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip
unzip benchmarks/.data/minidev.zip -d benchmarks/.data/minidev
```

脚本会递归找 `mini_dev_sqlite.json` 和 `dev_databases/`，不依赖固定解压层级。

## 跑 fresh holdout

```bash
python -m benchmarks.bird_sqlite --limit 50 --seed 20261127 \
  --exclude benchmarks/results/bird_mini_dev.json \
  --exclude benchmarks/results/bird_dev.json \
  --out benchmarks/results/bird_holdout.json
```

`--exclude` 排除已跑过的结果文件，保证 holdout 没在调参时看过。不要在同一批题上调完 prompt 再报“提升”。

默认输出包含：

- summary：数据集、模型、样本量、seed、official/strict/loose、描述开关、重试次数、错误数、平均延迟、时间；
- cases：每题的 `source_index`、db_id、question、模型 SQL、gold SQL、official/strict/loose、重试记录、错误。

## 指标定义

- **official**：BIRD 官方 EX，`set(predicted_res) == set(ground_truth_res)`；
- **strict**：列数相同，行多重集完全一致（忽略行顺序；浮点 round 6 位）；
- **loose**：列数相同，行数相同，忽略列顺序后的值多重集一致；
- 金标 SQL 执行失败单独计数 `gold_errors`，不算通过。

这不是 BIRD 官方 leaderboard 的复现：样本量、prompt、模型、是否使用 evidence、SQLite 方言都会影响结果。写 README 时必须同时写样本量和日期，不能只写一个百分比。

## 已跑结果

| 样本 | 配置 | official | strict | 说明 |
|---|---|---|---|---|
| 初始 50（seed 20261125） | 旧 harness，DDL only | — | 27/50 | 历史，口径不可直接比 |
| dev 25（seed 20261126，排除前 50） | 描述开，重试 1 | 13/25 | 11/25 | 调参用 |
| dev 25（同题） | 描述关，重试 1 | 14/25 | 14/25 | 描述没有净收益 |
| **fresh holdout 50（seed 20261127，排除前 75）** | **描述开，重试 1** | **24/50** | 23/50 | **最终干净数字** |

结论：1+2+3（官方 EX、描述/值样例、执行重试）**没有带来可复现的提升**；描述在 dev 上反而少 1 题，重试只救回少数执行错误。4/5 没有直接上 holdout。

## 只读与重试

- 数据库以 `mode=ro` 打开，并设置 `PRAGMA query_only=ON`、`temp_store=MEMORY`；
- 只接受 `SELECT` / `WITH` 开头；
- 30s 执行超时；
- 执行错误默认重试 1 次（`--retry-errors 0` 可关）；重试只修列名/语法/JOIN，不换查询语义。

## 描述/值样例

BIRD 的 `database_description/*.csv` 会拼进 DDL 的列注释（描述 + value description，截断）。
可用 `--no-descriptions` 关闭；dev A/B 显示当前没有净收益，所以不要把“带描述”写成必然更好。

## 边界

- 只做 SQLite；MySQL/PostgreSQL 方言不覆盖；
- 只做单条 SQL；分析模式/自动结论不在这组；
- 50 题是抽样，不是全量 500；
- 公开集分数低不代表 demo 坏，只说明换 schema 后泛化不足；分数高也不代表生产可用。
