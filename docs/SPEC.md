# SPEC：范围与验收标准

这份文件写清楚项目做什么、不做什么，以及每个模块的验收标准。代码和这里对不上时，先改这份文件，再改实现。

## 0. 一句话

给数据仓库做 NL2SQL：先把问题缩到「指标 / 表 / 列」，再由**大模型**在缩小的 schema 里写 SQL，代码校验、执行；错了带着报错重试（默认最多 2 次）；不是问数的问题不硬编 SQL，答案要能追到口径。规则模板退到离线 fallback，只在没 key / 模型故障时兜底，同时是 CI 的确定性回归基线。主路径是 `检索 → LLM → 校验 → 执行 → 带错误重试`，规则不是主路径（见 `app/fallback.py`）。

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
- 不做模型训练/微调；
- 不在页面里跑模型评测（成本、延迟、非确定性），也不把公开 benchmark（BIRD/Spider）接进 CI；
  公开集要做就独立 `benchmarks/`、手动跑（见 README）。

## 3. 模块与验收标准

> 每条 AC 都要在 `tests/` 里有对应测试，测试注释里写 `# AC-x`。

### M1 SQL 自我修复（P0）

- **AC-1.1**：生成 → 校验 → 执行，失败时把错误信息回传给生成器，最多重试 `max_repair` 次。
- **AC-1.2**：重试成功后，返回结果与"一次就对"完全一致（同 SQL 同数据）。
- **AC-1.3**：用完重试次数仍失败，`ok=False` 且 `error` 含最后一次错误，`attempts` 记录全部尝试。
- **AC-1.4**：修复路径不得绕过校验器（修复后的 SQL 仍要过 `validate`）。
- **AC-1.5**：关闭重试（`max_repair=0`）时，行为与旧版一致（只尝试一次）。
- **AC-1.6**：单条 SQL 有执行预算（默认 5 秒），超时中断并记为失败；超时不重试同一句（重跑只会再烧满一次预算）。
- **AC-1.7**：校验器放行只读的 `WITH ... SELECT`（CTE + 窗口函数）；CTE 名不能当成未知表，
  DML 关键字仍拒绝。用于"每个品类销售额前两名商品"这类分组 Top-N；普通 `SELECT` 不受影响。
- **AC-1.8**：生成后做筛选形状检查：上文筛选值必须保留；单值筛选且没有维度时，不得把筛选列
  放进 SELECT（"改成华南"只换条件，不按地区分组）。违反时当作一次 validate 失败、把话术作为
  `feedback` 重写，不执行错误 SQL；多值筛选（"华东和华南对比"）允许按筛选列分组。

### M2 意图路由（P0）

- **AC-2.1**：五类意图——`ask`（问数）/ `chat`（闲聊）/ `clarify`（缺信息）/ `out_of_scope`（超纲）/ `unsafe`（越权或写操作）。
- **AC-2.2**：只有 `ask` 会生成 SQL；其余四类返回固定话术，`sql` 为空。
- **AC-2.3**：缺信息（如"看下业绩"没说指标/时间）返回 `clarify`，并给出候选澄清项。判定用 `extract_keywords` 的 metric 槽位，不另抄一份指标词表。
  例外：只问「有哪些 / 列出」某个维度、且已识别到维度时（"华东地区有哪些商品类别"），问题本身不需要指标，
  按 `ask` 交生成器写 `SELECT DISTINCT`，不返回 clarify。
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

- **AC-5.1**：用例 ≥ 50 条（现 57：50 条问数 + 7 条意图/越权）；6 个指标、6 个维度都至少出现一次，指标×维度组合 ≥ 31/36
  （customer 维度按客户分组语义特殊，只留 1 条连通性用例，不铺满 6 条）；时间过滤覆盖 ≥ 5 类
  （现 6 类：上个月 / 本月 / 今年 / 去年 / 最近 30 天 / 指定月份）；非问数意图 chat / clarify /
  out_of_scope / unsafe 各至少 1 条（含 1 条 viewer 越权），否则"意图命中率"没有意义。
- **AC-5.2**：主指标为执行结果一致率；SPEC 门槛 ≥ 85%，CI 按 ≥ 90% 卡，低于阈值退出码 1。支持
  `--engine rule|llm`：rule 是 CI 默认（离线、确定性）；llm 需要 `LLM_API_KEY`，逐条记录 `engine`，
  静默回退（engine=rule）次数 > 0 直接退出码 1，避免"假 LLM 成绩"。
- **AC-5.3**：输出失败分类：`routed`（路由拦截）/ `permission`（权限）/ `validate`（校验）/
  `field`（字段不存在）/ `syntax`（生成语法错）/ `timeout`（执行超时）/ `retrieval`（检索未命中）/
  `result`（结果不一致）/ `intent`（意图不符）。每条用例带 `failure`，`summary.failure_counts` 与
  `summary.multi_turn_failure_counts` 分别汇总；分类先看 `attempts[].stage`，只有执行阶段再按错误码
  区分 `field` / `syntax`。
- **AC-5.4**：CI 跑单测 + 评测（单轮 / 意图 / 多轮三个门槛），任一失败即红；`timeout-minutes` 兜底，
  评测 JSON 作为 artifact 上传，失败时能直接看哪条挂了。
- **AC-5.5**：多轮评测独立文件 `eval/multi_turn.json`（≥ 3 个会话 × ≥ 3 轮，现 7 × 3 = 21，含客户维度回环与
  "改成华南"改写），用同一个 `Session` 顺序跑、逐轮比执行结果；指标为 `multi_turn_rate`，
  **不并入**单轮 `exec_rate`（否则历史数字不可比）。
- **AC-5.6**：规则覆盖外的问法独立成组 `eval/hard_cases.json`（≥ 10 条：IN 多值筛选 / 季度 / HAVING /
  任意 Top-N / 占比 / 复购 / 状态分布 / 滚动 7 天），`--hard` 单独跑并输出
  `hard_total / hard_match / hard_rate / hard_engine_counts`，**不并入**基础单轮与多轮；
  期望 SQL 手写、必须过校验与执行，且用例不能被路由提前判成 clarify/unsafe（否则测不到生成）。
  该组只作"规则 vs 模型"的证据，**不作 CI 门槛**（CI 仍只跑 `--engine rule` 的基础集）。
- **AC-5.7**：页面「跑离线基线」只跑 `engine=rule` 的基础集，不在页面现场调模型；模型成绩来自只读快照
  `eval/llm_result.json`（由 `scripts/record_llm_eval.py` 生成：剥掉 `details`，静默回退或 hard 期望 SQL
  报错时不落盘），`GET /api/eval/llm/last` 读它——有快照返回 `available=true` + 模型/日期/汇总，
  缺失或损坏返回 `available=false`（页面隐藏）。接口、页面加载与测试都**不得触发模型调用**。
- **AC-5.8**：复杂问法独立成组 `eval/complex_cases.json`（10–15 条：CTE/窗口、分组 Top-N、多指标、
  占比、复购、多维度，另含 1 条归因题），`--complex` 单独跑并输出
  `complex_total / complex_supported / complex_match / complex_rate / complex_engine_counts /
  complex_failure_counts`，**不并入**基础单轮、多轮与 hard；期望 SQL 手写、必须过校验与执行，
  且已支持题不能被路由提前判成 clarify/unsafe。`expected_unsupported` 的归因题只记录实际响应，
  不算通过也不算 SQL 失败；该组不作 CI 门槛（CI 仍只跑基础集）。
  口径限制：按品类/商品/品牌拆分的分组 GMV 沿用整单 `pay_amount`，不可跨组相加（见 README）。

### M6 交付物：Docker + 截图 + GIF（P1）

- **AC-6.1**：`docker compose up --build` 一条命令起服务：容器内生成演示库、暴露 8000 端口、
  `HEALTHCHECK` 通过；挂载空 `/app/data` 时 entrypoint 能补 seed。
- **AC-6.2**：README 带 3 张页面截图（角色掩码 / 越权拦截 / 自我修复时间线）+ 1 个多轮 GIF
  （时间继承 → "改成华南"）；素材由 `scripts/shots.py` 固定脚本生成，页面改动后可重跑，不手工对时间点。
- **AC-6.3**：没配 LLM key 时页面上也能看到自我修复时间线：`/api/demo/repair` 用脚本化生成器
  复现"第一次写错列名 → 带报错改对"（响应结构与 `/api/ask` 一致，`attempts` 里能看到 execute 阶段失败），
  **只做演示**：不写会话、不参与评测、不影响生成器。

### M7 分析模式（P1，可选，默认关闭）

- **AC-7.1**：`ask(..., mode="single" | "analysis")` 默认 `single`，快路径响应结构不变；
  `POST /api/ask` 增加 `mode`（未知值 400）。analysis 响应在原有字段外增加
  `mode / steps / answer / evidence`，step 含 `question / purpose / sql / columns / rows /
  row_count / ok / skipped / error / engine / elapsed_ms`。
- **AC-7.2**：Planner 只接收改写后的问句（不带 schema），输出 2–4 个自包含子问题的 JSON；
  坏 JSON/空步骤重试 1 次，仍失败回退 `single` 并带 `analysis_error`；步数上限 4，超出截断。
- **AC-7.3**：Executor 每步复用 `ask(mode="single")`，同 role、不共享 session；澄清/越权/
  预算不足标 `skipped` 不硬跑；失败步骤保留 sql/error，不隐藏；SQL 仍走校验/权限/只读/5s 超时。
  聚合层级与子问题不一致的步骤（典型：复购题的 `HAVING` 落在地区分组、返回地区全部客户）
  判 `ok=false`，不参与证据，避免综合器引用错数。
- **AC-7.4**：Synthesizer 不给 schema、不允许写 SQL；结论 1–3 段且必须引用成功的 `[sN]`；
  无证据时不调模型、直接说“当前结果不足以判断”；失败步骤由代码补进“局限”；结论只做长度/
  空白/引用检查，不做 SQL 校验。
- **AC-7.5**：默认关闭、页面有开关；模型先用 `deepseek-chat`，`ANALYSIS_MODEL` 只影响
  Planner/Synthesizer；可单独配 `ANALYSIS_BASE_URL` / `ANALYSIS_API_KEY` 接千问等另一家
  OpenAI 兼容服务（留空复用 LLM 的地址/key）；A/B 报告结论质量、步骤失败率、耗时与 token；
  不换默认模型。测试不碰真实模型；基础 50/50、多轮 21/21、hard/complex 数字不得回退。
  complex 以 prompt v3.1 去示例后的 **11/12** 为干净基线；v3 的 12/12 含 4 条与题面重叠的示例，不作为回退基准。
  A/B 旧 8/12、10/12 是单次人工抽样且无原始产物，只作历史参考；prompt v3.1 已复核重跑并落盘
  `eval/analysis_ab.json`（chat 26/26 步、5.98s、coverage 0.71；flash 25/27 步、16.85s、coverage 0.63），
  单轮 coverage 不作为稳定质量门槛。

### M8 自动结论（Step 3，P1，默认关闭）

- **AC-8.1**：`ask(..., conclusion=False)` 默认关闭；`POST /api/ask` 增加 `conclusion`
  （默认 false）。单 SQL 成功后响应增加 `answer / conclusion / conclusion_error /
  conclusion_usage`；不开启时 `answer=""`、`conclusion=false`。
- **AC-8.2**：只对 `intent=ask`、`ok=true`、`engine=llm` 且有行的单 SQL 调用模型；
  规则 fallback、空结果、SQL 失败都不调用。结论模型失败只写 `conclusion_error`，
  不得把 SQL 的 `ok/rows/sql` 改成失败。
- **AC-8.3**：结论 prompt 只给 columns/rows（最多 50 行，截断要标注），不给 schema、不允许写 SQL；
  输出 1–3 句中文，只描述结果、不解释原因；空/NULL 直接说空；长度/空白检查后才展示。
- **AC-8.4**：页面「自动结论」开关默认关闭；分析模式开启时禁用/不叠加；测试全部 mock，
  不调真实模型；base 50/50、多轮 21/21、hard/complex 数字不得回退。

## 4. 契约（公开接口，改动需改本文件）

```python
# app/agent.py
ask(question: str, *, catalog=None, retriever=None, generator=None,
    use_llm: bool = True, max_repair: int = 2, session=None,
    role: str = "analyst", mode: str = "single", conclusion: bool = False) -> SQLPlan
# M3 已加：session=None（app/session.py 的 Session）
# M4 已加：role（analyst / viewer；未知角色抛 ValueError，HTTP 层 400）

# app/router.py（M3：槽位由调用方算好传进来，classify 不再自己抽取 —— AC-3.6）
classify(question: str, catalog: Catalog, slots: dict[str, list[str]],
         denied_terms: Iterable[str] = ()) -> Route
# M4 已加：denied_terms = 当前角色不可见列的列名/同义词，命中即判 unsafe（AC-4.2）

# app/domain.py
SQLPlan: question, sql, ok, error, failure_stage, rows, columns, elapsed_ms,
         context, keywords, attempts, engine, intent, reply, clarify_options,
         session_id, rewritten_question, inherited_slots, role, masked_columns
# M3 已加：session_id / rewritten_question（补全继承槽位后的问句）/ inherited_slots
# M4 已加：role / masked_columns（服务端结果行上被掩码的列）
# M5 已加：failure_stage（最后一次失败发生在 validate/execute/timeout，ok=True 时为空）；
#          attempts 每条带 stage（validate / execute / timeout / ok），失败分类不再猜 error 前缀
```

```http
POST /api/ask
  { "question": "...", "use_llm": true, "session_id": "可选，不带就新开一个",
    "role": "analyst|viewer，默认 analyst", "mode": "single|analysis，默认 single",
    "conclusion": false }
  → { sql, ok, error, failure_stage, columns, rows, masked_columns, elapsed_ms, intent,
      reply, answer, conclusion, conclusion_error?, conclusion_usage?, clarify_options,
      engine, attempts, context, keywords, session_id, rewritten_question, inherited_slots, role }
  # engine: rule | llm | custom，用来确认这次真的走了模型
  # 把返回的 session_id 带回来就能接上文；不带就是单轮，行为与 M2 一致
  # 非 ask 时 context 为 null（不检索、不泄漏 schema）
  # 未知 role 返回 400
POST /api/demo/repair
  { "role": "analyst|viewer，可选，默认 analyst" }
  → 与 /api/ask 同结构；固定演示"第一次写错列名 → 带报错改对"（AC-6.3）
  # 只给页面「演示修复」和截图脚本用：不写会话（session_id 为空）、不参与评测
GET /api/schema?role=analyst|viewer → 按角色裁剪后的表/列/指标
GET /api/eval   → 评测摘要：total / ask_total / exec_rate / intent_rate / multi_turn_rate /
                  failure_counts …（details 与 multi_turn_details 不下发；engine=rule）
GET /api/eval/llm/last → 最近一次模型评测的只读快照：
                  { available, generated_at, model, engine, summary }
                  # available=false 表示没有或损坏的 eval/llm_result.json，页面隐藏这一块
                  # 只读文件，不调模型、不扫题库
GET /api/audit  → { total, entries }：越权尝试（内存环形缓冲）
GET /api/health → { ok, tables, metrics, roles }
```

## 5. 质量门槛（不过不许提交）

1. `python -m unittest discover -s tests` 全绿；
2. `python -m app.evals` 单轮执行一致率 ≥ 85%（CI 卡 90%）；意图命中率与多轮一致率 ≥ 90%
   （`--min-intent` / `--min-multi` 默认 0.9，CI 显式传参）；
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
| M1 SQL 自我修复 | ✅ 已完成 | 9 条测试（含 error 清理 / engine / 超时回归）；`scripts/demo_repair.py` 可复现；2026-11-22 放行只读 CTE/窗口函数（AC-1.7）、加筛选形状检查（AC-1.8） |
| M2 意图路由 | ✅ 已完成 | 13 条测试；槽位判澄清；非 ask 不生成 SQL，返回话术；前端已接；2026-11-22 补：维度清单（有哪些X）不澄清，交模型写 `SELECT DISTINCT`，有筛选时澄清项也补维度 |
| M1/M2 审核修复 | ✅ 已完成 | 2026-11-13 |
| M3 多轮追问 | ✅ 已完成 | 35 条测试（AC-3.1~3.6，含标签覆盖/客户维度回环）；顺序 `session → 槽位继承 → classify → 生成`，classify 只收槽位；会话进程内、默认 5 轮 |
| M4 权限与脱敏 | ✅ 已完成 | 29 条权限测试 + 7 条 HTTP 契约；顺序 `role → 裁剪 catalog → 槽位 → classify → 检索/生成`；列白名单（含禁 `*`）、裸投影掩码 + 值兜底、审计；2026-11-16 审核补了两条绕过路径 |
| M5 评测与 CI | ✅ 已完成 | 57 条单轮（50 问数 + 7 意图/越权；6 指标 × 6 维度、31/36 组合、6 类时间）+ 7 会话 21 轮多轮评测；失败分类（routed/permission/validate/field/syntax/timeout/retrieval/result/intent）汇总；CI 三门槛 90% + artifact；扩容时修了退款率二次舍入；`--engine llm` 实测 deepseek-chat 单轮 50/50、多轮 21/21、回退 0（2026-11-20）；规则覆盖外 10 条（AC-5.6）规则 0/10、模型 8–9/10（多次采样，最近快照 8–9/10） |
| M6 Docker + 截图/GIF | ✅ 已完成 | `docker compose up --build` 起 8000（容器内 seed、healthcheck、空 volume 补 seed）；`scripts/shots.py` 固定脚本产出 3 图 + 1 GIF；离线演示修复走 `/api/demo/repair`（脚本化生成器，不写会话、不参与评测）；180 个单测 |
| LLM 主线化 / 规则 fallback（2026-11-20） | ✅ 已完成 | 规则 SQL 移到 `app/fallback.py`（`OfflineFallback`，离线降级 + CI 基线），`agent.py` 只留 LLM/校验/执行/重试；新增 `eval/hard_cases.json` 10 条与 `--hard` runner（单独统计、不进 CI）；README/SPEC/decisions 改成模型主线；166 测试全绿，CI 仍只跑 `--engine rule` |
| 页面离线基线 / 模型快照（2026-11-21） | ✅ 已完成 | 按钮改「跑离线基线」；`scripts/record_llm_eval.py` 生成 `eval/llm_result.json`（只读快照），`GET /api/eval/llm/last` 有则显示「上次模型评测」、无/损坏则隐藏；接口与测试均不调模型；BIRD/Spider/DuSQL 仅方案评估、不接；180 单测 |
| 复杂问法评测（2026-11-22） | ✅ 已完成 | 13 条（12 可判定 + 1 归因未支持），`--complex` 单独统计、不进 CI；规则 0/12、`deepseek-chat` **11/12**（prompt v3.1 去示例后 3 次一致，只剩 #13）、回退 0；分组 GMV 口径见 README |
| 分析模式 MVP（2026-11-23） | ✅ 已完成 | `mode=analysis` 默认关闭；Planner 2–4 步 → 每步复用 `ask` → Synthesizer 引用 `[sN]`、失败/口径警告补“局限”；250 单测；每步重试默认 1、总预算从 Planner 计时；复购错误 HAVING 判步骤不可用；prompt v3.1 复杂单 SQL 11/12，holdout 3/3×3；A/B 旧 8/12、10/12 仅历史，v3.1 复核重跑 raw：chat 26/26 步/5.98s/coverage 0.71，flash 25/27 步/16.85s/coverage 0.63（`eval/analysis_ab.json`），默认不换模型 |
| 自动结论 Step 3（2026-11-23） | ✅ 已完成 | `conclusion=true` 显式开启、默认关闭；只对 LLM 成功单 SQL 多调一次模型；只描述结果、不解释原因；规则/空结果/失败不调；页面开关默认关；实测 1.2–1.7s；250 单测 |

## 8. 开发约定

- 改代码前先确认它属于哪一节；超出「非目标」的先停下来。
- 每条 AC 都要有对应测试，测试里标注 AC 编号。
- 改完更新第 7 节的模块状态，跑一遍评测门槛再提交。
