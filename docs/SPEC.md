# SPEC：范围与验收标准

这份文件写清楚项目做什么、不做什么，以及每个模块的验收标准。代码和这里对不上时，先改这份文件，再改实现。

## 0. 一句话

给数据仓库做 NL2SQL：先把问题缩到「指标 / 表 / 列」，再生成 SQL，校验、执行；错了能带着报错自己改（默认最多 2 次）；不是问数的问题不硬编 SQL，答案要能追到口径。

## 1. 目标（做完这几件就算完成）

| 编号 | 目标 | 可验证的方式 |
|---|---|---|
| G1 | 一条命令起服务，能问能答 | `make seed && make run`，页面返回表格 |
| G2 | 检索能把 schema 缩到 ≤ 4 表 / ≤ 12 列 | 单测断言 context 规模 |
| G3 | 生成的 SQL 一定只读、表白名单、可执行 | 校验器单测 + 执行单测 |
| G4 | SQL 执行失败能带错误重试，并记录每次尝试 | 注入失败用例的测试 |
| G5 | 多轮追问能接上（"那上个月呢"） | 会话测试 |
| G6 | 非问数问题不硬编 SQL（闲聊/超纲/需澄清） | 路由测试 |
| G7 | 敏感列按角色过滤 | 权限测试 |
| G8 | 评测 ≥ 50 条，执行一致率 ≥ 85%，进 CI | `python -m app.evals` |

## 2. 非目标（明确不做，防止越做越大）

- 不做知识图谱、不做多模态文档解析；
- 不做多数据源（只 SQLite 演示库 + 可替换的执行器接口）；
- 不做图表看板、不做完整账号体系（权限用"角色 → 可见列"配置演示）；
- 不套 Agent 框架（LangGraph 之类），卖点是确定性校验；
- 不做模型训练/微调。

## 3. 模块与验收标准

> 每条 AC 都要在 `tests/` 里有对应测试，测试注释里写 `# AC-x`。

### M1 SQL 自我修复（P0）

- **AC-1.1**：生成 → 校验 → 执行，失败时把错误信息回传给生成器，最多重试 `max_repair` 次。
- **AC-1.2**：重试成功后，返回结果与"一次就对"完全一致（同 SQL 同数据）。
- **AC-1.3**：用完重试次数仍失败，`ok=False` 且 `error` 含最后一次错误，`attempts` 记录全部尝试。
- **AC-1.4**：修复路径不得绕过校验器（修复后的 SQL 仍要过 `validate`）。
- **AC-1.5**：关闭重试（`max_repair=0`）时，行为与旧版一致（只尝试一次）。
- **AC-1.6**：单条 SQL 有执行预算（默认 5 秒），超时中断并记为失败；超时不重试同一句（重跑只会再烧满一次预算）。

### M2 意图路由（P0）

- **AC-2.1**：五类意图——`ask`（问数）/ `chat`（闲聊）/ `clarify`（缺信息）/ `out_of_scope`（超纲）/ `unsafe`（越权或写操作）。
- **AC-2.2**：只有 `ask` 会生成 SQL；其余四类返回固定话术，`sql` 为空。
- **AC-2.3**：缺信息（如"看下业绩"没说指标/时间）返回 `clarify`，并给出候选澄清项。判定用 `extract_keywords` 的 metric 槽位，不另抄一份指标词表。
- **AC-2.4**：含写操作词（删除/更新/插入/导出到文件）判为 `unsafe`，不生成 SQL。只拦祈使用法："被删除的订单有多少"这类定语不算写操作。
- **AC-2.5**：路由是规则优先（可解释）；关闭 LLM 时路由仍可用。低置信度交给 LLM 兜底**不在 M2 范围**（`Route.confidence` 留了接口，没用）。

### M3 多轮追问（P1）

- **AC-3.1**：会话保存最近 N 轮（默认 5）的"问题 + 解析出的槽位（指标/维度/筛选/时间）"。
- **AC-3.2**：省略主语/时间时，从上文继承缺失槽位；显式给出的槽位覆盖上文（说"总/整体"且没有"各/按"时，视为显式要求不分组）。只补槽位、不带新主语的短句（"上个月""按品类看下""华南"）同样继承；句子里有新主语（"上个月工资发了多少"）则不猜，走澄清。
- **AC-3.3**：用户说"换成华南""那上个月呢"能正确改写。
- **AC-3.4**：新问题与上文无关（换了指标且无指代）时不继承，开新话题。
- **AC-3.5**：会话有上限，超限淘汰最旧；不落盘（进程内即可）。
- **AC-3.6**：顺序必须是 `session → 槽位继承 → classify → 生成`；`classify(question, catalog, slots)` 只接收槽位、不自己再抽一遍——先路由会把"那上个月呢"判成 clarify，多轮直接断。

### M4 权限与脱敏（P1）

- **AC-4.1**：角色配置为"可见列白名单"；不可见列不得出现在生成的 SQL、结果表、错误信息里。
- **AC-4.2**：请求了无权列时，返回 `unsafe` + 说明，不静默改列。
- **AC-4.3**：脱敏列（如手机号）返回掩码，且掩码在服务端完成，不依赖前端。
- **AC-4.4**：越权尝试写审计日志（内存环形缓冲即可）。

> 实现说明（2026-11-16）：角色策略 = **隐藏列 + 掩码列**；敏感列默认隐藏，必须显式配 `masked` 才可见——等价于最小权限白名单，新增敏感列"不配置就不可见"。列白名单落在 `validate`（`denied=`），`denied` 非空时禁 `select *` / `表.*`；掩码列只允许裸列投影（别名/函数/拼接在校验阶段拒，结果行另有手机号值模式兜底）。错误话术不带列名，`attempts[].sql` 打码；非 ask 不再返回 `context`。

### M5 评测与 CI（P0）

- **AC-5.1**：用例 ≥ 50 条，覆盖 5 个指标 × 4 个维度 × 3 类时间过滤。
- **AC-5.2**：主指标为执行结果一致率，阈值 ≥ 85%；低于阈值退出码 1。
- **AC-5.3**：输出"失败分类"（检索未命中 / 生成语法错 / 字段不存在 / 结果不一致）。
- **AC-5.4**：CI 跑单测 + 评测，任一失败即红。

## 4. 契约（公开接口，改动需改本文件）

```python
# app/agent.py
ask(question: str, *, catalog=None, retriever=None, generator=None,
    use_llm: bool = True, max_repair: int = 2, session=None,
    role: str = "analyst") -> SQLPlan
# M3 已加：session=None（app/session.py 的 Session）
# M4 已加：role（analyst / viewer；未知角色抛 ValueError，HTTP 层 400）

# app/router.py（M3：槽位由调用方算好传进来，classify 不再自己抽取 —— AC-3.6）
classify(question: str, catalog: Catalog, slots: dict[str, list[str]],
         denied_terms: Iterable[str] = ()) -> Route
# M4 已加：denied_terms = 当前角色不可见列的列名/同义词，命中即判 unsafe（AC-4.2）

# app/domain.py
SQLPlan: question, sql, ok, error, rows, columns, elapsed_ms,
         context, keywords, attempts, engine, intent, reply, clarify_options,
         session_id, rewritten_question, inherited_slots, role, masked_columns
# M3 已加：session_id / rewritten_question（补全继承槽位后的问句）/ inherited_slots
# M4 已加：role / masked_columns（服务端结果行上被掩码的列）
```

```http
POST /api/ask
  { "question": "...", "use_llm": true, "session_id": "可选，不带就新开一个",
    "role": "analyst|viewer，默认 analyst" }
  → { sql, ok, error, columns, rows, masked_columns, elapsed_ms, intent, reply,
      clarify_options, engine, attempts, context, keywords,
      session_id, rewritten_question, inherited_slots, role }
  # engine: rule | llm | custom，用来确认这次真的走了模型
  # 把返回的 session_id 带回来就能接上文；不带就是单轮，行为与 M2 一致
  # 非 ask 时 context 为 null（不检索、不泄漏 schema）
  # 未知 role 返回 400
GET /api/schema?role=analyst|viewer → 按角色裁剪后的表/列/指标
GET /api/eval   → 评测摘要
GET /api/audit  → { total, entries }：越权尝试（内存环形缓冲）
GET /api/health → { ok, tables, metrics, roles }
```

## 5. 质量门槛（不过不许提交）

1. `python -m unittest discover -s tests` 全绿；
2. `python -m app.evals` 执行一致率 ≥ 85%（CI 目前按 90% 卡）；
3. 新增模块必须有 ≥ 3 条带 AC 编号的测试；
4. 不许引入第三方运行时依赖（保持 `dependencies = []`）；
5. 每条 AC 至少要有一条"反向用例"（错误输入被正确拒绝）。

## 6. 提交规范

- 一个模块一个 commit，消息格式：`feat(M2): 意图路由（AC-2.1~2.4）`；
- 修 bug：`fix(M1): 重试次数用完没有回传最后错误（AC-1.3）`；
- 改契约：单独的 `docs(SPEC): ...` 提交，和代码分开。

## 7. 模块状态

| 模块 | 状态 | 备注 |
|---|---|---|
| 基线（M0：检索/生成/校验/执行/评测/页面） | ✅ 已完成 | 20 条评测 100% |
| M1 SQL 自我修复 | ✅ 已完成 | 9 条测试（含 error 清理 / engine / 超时回归）；`scripts/demo_repair.py` 可复现 |
| M2 意图路由 | ✅ 已完成 | 13 条测试；槽位判澄清；非 ask 不生成 SQL，返回话术；前端已接 |
| M1/M2 审核修复 | ✅ 已完成 | 2026-11-13 |
| M3 多轮追问 | ✅ 已完成 | 35 条测试（AC-3.1~3.6，含标签覆盖/客户维度回环）；顺序 `session → 槽位继承 → classify → 生成`，classify 只收槽位；会话进程内、默认 5 轮 |
| M4 权限与脱敏 | ✅ 已完成 | 29 条权限测试 + 7 条 HTTP 契约；顺序 `role → 裁剪 catalog → 槽位 → classify → 检索/生成`；列白名单（含禁 `*`）、裸投影掩码 + 值兜底、审计；2026-11-16 审核补了两条绕过路径 |
| M5 评测扩容 + CI | ⬜ 待做 | 22 → 50 条 |

## 8. 开发约定

- 改代码前先确认它属于哪一节；超出「非目标」的先停下来。
- 每条 AC 都要有对应测试，测试里标注 AC 编号。
- 改完更新第 7 节的模块状态，跑一遍评测门槛再提交。
