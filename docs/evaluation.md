# 评测记录

这里放评测口径、用例和结果。README 只保留汇总；细节以本文件为准。

环境：`deepseek-chat`，`temperature=0`，2026-11-25。  
基础评测由 `python -m app.evals` 驱动；hard / complex / BIRD 都是手动评测，不作为 CI 门槛。

## 1. 汇总

| 评测集 | 规则 fallback | LLM |
|---|---|---|
| 基础 50 条问数 | 50/50 | 50/50 |
| 意图 57 条（含 7 条非问数） | 57/57 | 57/57 |
| 多轮 21 轮 | 21/21 | 21/21 |
| 规则覆盖外 10 条（hard） | 0/10 | 8–9/10（多次采样） |
| 复杂问法 12 条可判定（complex） | 0/12 | 12/12；最新 3 次 12/12、11/12、12/12 |
| BIRD Mini-Dev fresh 50 题 | — | 官方 EX 36/50（72%） |

- 基础 / 多轮：静默回退 0，平均约 0.7–0.8s/条。
- 单测：288 条，覆盖检索、校验、修复、路由、多轮、权限、评测、分析模式、自动结论、fallback 和 HTTP 契约。
- hard / complex 的模型数字是多次采样中的结果，不作为稳定基线；complex 的 C9（分组 Top-N）偶发失败。

## 2. 用例分组

### 基础 57 条

`eval/cases.json`：

- 50 条问数：6 个指标 × 6 个维度中的 31/36 组合，另含地区/品类筛选、双筛选、同义说法、Top N；
- 7 条意图 / 越权：chat ×2、超纲 ×2、clarify ×1、写操作 unsafe ×1、viewer 越权 ×1；
- 6 类时间：上个月、本月、今年、去年、最近 30 天、指定月份。

主指标是「两边都执行、比对结果集」，SQL 字面匹配只作参考。

### 多轮 21 轮

`eval/multi_turn.json`：7 个会话 × 3 轮，覆盖时间继承、维度覆盖、筛选切换、「总共」不分组、新话题不继承、澄清轮不污染上下文、客户维度回环和「改成华南」改写。

### 规则覆盖外 10 条

`eval/hard_cases.json`。这组用来量规则模板的边界：

| # | 问题 | 规则模板的限制 |
|---|---|---|
| 1 | 销量最好的三个商品 | 固定 `LIMIT 10/20`，不认「三个」 |
| 2 | 华东和华南的GMV对比 | 多值筛选被拼成 `= 华东 AND = 华南`，结果为空 |
| 3 | 按季度看GMV | 时间维度只有月/年，没有季度推导 |
| 4 | 退款率最高的品类 | 只会分组 + `LIMIT 10`，不会取 Top-1 |
| 5 | 客单价超过7000的品牌 | 没有聚合后过滤 `HAVING` |
| 6 | 上季度GMV | 时间词只有固定几类，没有自然季度 |
| 7 | 各订单状态的订单数 | 指标默认焊死 `status='paid'`，状态分布被压成一行 |
| 8 | 最近7天每天的订单数 | 没有滚动 7 天，也不会按天分组 |
| 9 | 各地区销量占全国比例 | 不会窗口/子查询算占比 |
| 10 | 复购客户数 | 只会数去重客户，不会先按客户聚合再 `HAVING` |

模型常见失败：

- 「退款率最高的品类」被「排名 → LIMIT 10」带偏成 10 行；
- 「上季度GMV」写成最近三个月，而不是上一个日历季度；
- 「复购客户数」偶发把 `GROUP BY` 写在外层。

### 复杂问法 13 条

`eval/complex_cases.json`：12 条可判定 + 1 条归因题标记未支持。

覆盖：

- CTE / 窗口函数；
- 分组 Top-N；
- 多指标对比；
- 占比 / 份额；
- 复购 / 阈值客户；
- 多维度聚合；
- 月度环比。

归因题没有 `expected_sql`，只记录系统实际响应，不计通过也不计 SQL 失败。当前它仍会被路由成问数并返回一条局部数据，这是单 SQL 路径的已知边界。

口径说明：`gmv` 是整单 `SUM(orders.pay_amount)`。按品类/品牌拆分时 join `order_items` 会按明细行重复计入，所以分组 GMV 不可跨组相加，也不等于分摊销售额；占比题使用可加的销量口径。

### 分析模式 A/B

`eval/analysis_ab.json` 保存 12 条 complex 的原始答案和步骤 SQL（不含 key）。

清理后两轮（删 Planner 契约死代码、执行约束改走 `ask(hint=...)`）：

| 指标 | `deepseek-chat` | `deepseek-flash` |
|---|---|---|
| 步骤成功 | 26/26、25/26 | 24/25、25/26 |
| 平均耗时 | 5.76s、5.93s | 17.5s、17.0s |
| 自动覆盖率（弱信号） | 0.94、0.92 | 0.92、0.75 |

份额题 C4 / C11 两轮都是 2/2 步、coverage 1.00。默认仍使用 `deepseek-chat`。

### BIRD Mini-Dev

`benchmarks/bird_sqlite.py` 是独立 harness，不并入 `app.evals`，也不进 CI。

- 数据：BIRD Mini-Dev SQLite；
- 样本：fresh 50 题，seed `20261128`，排除之前跑过的 125 题；
- 指标：BIRD 官方 EX，`set(predicted_res) == set(ground_truth_res)`；
- 结果：**36/50（72%）**，strict 35/50，0 题 SQL 执行失败，gold 失败 0；
- 平均 0.86s/题。

同一批题上「数据库描述 + 执行重试」是 31/50，低于 DDL-only 的 36/50，所以这两个能力默认关闭，保留为实验开关。

结果文件：[benchmarks/results/bird_fresh_base.json](../benchmarks/results/bird_fresh_base.json)。  
这不是官方 leaderboard：50 题样本、单次采样、prompt 和 strict/loose 定义都不等同于官方评测。

## 3. 复现

```bash
# 基础 / 多轮（离线规则）
python -m app.evals

# 基础 / 多轮（真实模型）
python -m app.evals --engine llm

# hard / complex
python -m app.evals --hard --engine llm
python -m app.evals --complex --engine llm

# 记录页面读的模型快照
python -m scripts.record_llm_eval

# 分析模式 A/B
python -m scripts.analysis_ab --models deepseek-chat,deepseek-flash --out eval/analysis_ab.json

# BIRD（先按 benchmarks/README.md 下载数据）
python -m benchmarks.bird_sqlite --limit 50 --seed 20261201
```

## 4. 怎么读这些数字

- 基础 / 多轮是回归指标，数字稳定；hard / complex / BIRD 是能力边界，样本小，不适合当硬门槛。
- 模型在 `temperature=0` 下仍可能有服务端采样波动；单次结果不要当稳定上下限。
- 评测集由项目自己编写，存在过拟合风险；BIRD 是外部集，但只跑了 50 题。
- 规则 fallback 的分数低是设计使然：它只负责离线兜底和 CI，不追求复杂问法覆盖。
