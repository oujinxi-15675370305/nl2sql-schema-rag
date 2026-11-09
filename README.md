# nl2sql-schema-rag

用自然语言查数据仓库：先把问题映射到「指标 / 表 / 列」，再让模型在缩小的 schema 里写 SQL，最后校验、执行、给出结果。

## 为什么写这个

在宽表几十张、字段几百个的仓库上直接让大模型写 SQL，最常见的问题不是"不会写"，是**编字段**：它会把 `pay_amount` 写成 `pay_amt`，把 `order_date` 当成日期类型去 `DATE()`，或者把两张表的同名字段接错。

所以这个项目的重点不是"接个大模型"，而是**把 schema 缩到模型不会犯错的范围内**：

1. 先从问题里抽出业务词（指标、维度、筛选值、时间）；
2. 用三路检索把候选缩到 3 个指标、4 张表、12 个列；
3. 只把这部分 DDL 放进 prompt；
4. 生成后逐条校验字段是否存在、是不是只读、有没有行数上限。

## 它怎么跑

```
问题 → 关键词 → 三路检索（指标/表/列）→ 裁剪 DDL → 生成 SQL → 校验 → 执行 → 结果
```

- **三路检索**：指标、表、列分别建索引。列用关键词为主（列名要精确命中），指标和表稠密+关键词各占一半，最后 RRF 融合。
- **裁剪**：只渲染命中的表和列，通常 30 行以内；指标把口径描述一起带上。
- **校验**：只允许单条 SELECT、表白名单、禁 DDL/DML、执行时只读连接 + 限行。
- **兜底**：没配 `LLM_API_KEY` 时用规则生成（离线可跑），配了就走大模型，规则结果当兜底。

## 跑起来

```bash
git clone <repo> && cd nl2sql-schema-rag
python scripts/seed_demo_db.py     # 生成演示库 data/demo.sqlite（800 单 / 1608 明细）
python main.py                     # http://127.0.0.1:8000
```

没有依赖要装，标准库就能跑（3.10+）。

想跑评测：

```bash
python -m app.evals            # 打印每条用例
python -m app.evals --json     # 输出 JSON，CI 用
python -m unittest discover -s tests -v
```

## 现在的结果

20 条用例，**执行结果一致率 100%**，平均 2ms（规则模式，不含模型调用）。

注意这是**规则模式的成绩**，不能当"模型准确率"看：规则只覆盖了 4 个维度、5 个指标的组合，样本也少。接大模型后的真实表现要重测，见下面「还没做」。

## 配置

不配也能跑。要接大模型，复制 `.env.example` 改名 `.env`：

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_API_KEY` | 为空则走规则生成 | 空 |
| `LLM_BASE_URL` | OpenAI 兼容接口 | `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 模型名 | `deepseek-chat` |
| `PORT` | 服务端口 | 8000 |

数据库、向量库这些没有做成必填：演示库是 SQLite，检索是内存索引。真接 Qdrant/ES 时替换 `app/retrieval.py` 里的 `DenseIndex` 即可。

## 目录

```
app/
  catalog.json      演示 schema（4 表 + 5 指标）
  catalog.py        加载成领域对象
  domain.py         表/列/指标/命中/计划的定义
  retrieval.py      三路检索 + RRF
  agent.py          关键词、生成、校验、执行、ask()
  evals.py          评测
  server.py         HTTP 服务
  web/              问数页面（原生 JS，无构建）
eval/cases.json     20 条 NL→SQL 对照
scripts/seed_demo_db.py
tests/
docs/decisions.md   几个取舍的记录
```

## 已知问题

- **只支持单指标 + 单维度**。「各品牌客单价和销量对比」这种会退化成默认维度，代码里留了 FIXME。
- **规则生成覆盖窄**：时间只认上个月/本月/今年/去年/最近 30 天/具体月份，其它说法会忽略。
- **没有多轮**：每轮都当独立问题，"那上个月呢"这种接不上（TODO）。
- **列检索的稠密部分是近似**（字符 n-gram 哈希），换成真 embedding 后指标会变，现在的成绩不可直接对比。
- **评测集只有 20 条**，且是我自己写的，有过拟合的嫌疑；下一步要拿真实提问补到 50 条以上。

## 下一步

- [ ] SQL 执行报错后，把错误信息带回给模型重试一次
- [ ] 多轮追问与指代消解
- [ ] 列级权限（不同角色看不到的字段要过滤）
- [ ] 接 Qdrant + ES，把内存索引换掉

## License

MIT
