# 几个取舍

写下来是为了两周后自己还能想起为什么这么定。

## 2026-11-03 向量用哈希近似，不装 Qdrant

演示要能 clone 下来就跑，装 Qdrant 就把门槛抬高了。哈希 n-gram 排得不如真 embedding，但在这个只有 4 表 5 指标的 demo 上够用——检索错的时候主要是关键词没命中，不是向量不行。

真上生产要换，接口留好了：`DenseIndex` 只有 `search()` 一个方法。

## 2026-11-03 列检索以关键词为主

一开始三路都用向量，结果 `pay_amount` 这种列名经常被语义相近的 `cost_amount` 挤掉。列名是标识符，不是自然语言，关键词命中权重更该高。改成 BM25 为主、向量补充后，列命中明显稳了。

## 2026-11-06 校验放在生成之后、执行之前

顺序是：生成 → 校验 → 执行。没有在 prompt 里死磕"不要写 DROP"，而是生成完用代码检查。提示词约束不住的东西交给代码，这是这个项目的基本态度。

## 2026-11-06 评测用执行结果比对，不比 SQL 字面

同一个问题可以写出好几种等价 SQL（别名不同、JOIN 顺序不同、子查询 vs JOIN）。比字面会把正确结果判错。所以主指标是两边都执行、比对结果集；SQL 字面匹配只当参考，现在基本是 0%，正常。

## 2026-11-13 意图路由用槽位判，不另抄指标词表

M2 第一版在 `router.py` 里又写了一份指标正则，和 `slots.METRIC_HINTS`、`catalog.json` 的 synonyms 三份词表对不上，把"查一下华东的订单量"判成了"需要澄清"。改成复用 `extract_keywords` 的 metric 槽位：抽不到指标才澄清，词表只留在 `app/slots.py` 一处。

## 2026-11-13 写操作只拦祈使句

"被删除的订单有多少""更新时间分布"都是读查询，按关键词一把拦会误伤。现在 `drop/delete/导出/下载` 这类无歧义词无条件拦，`删除/更新/修改` 只在祈使语境拦（前面有"把/将"，或后面不是"的/时间/状态"这类定语）。

## 2026-11-13 .env 自己读，不引 python-dotenv

README 让人复制 `.env`，但代码从来没读过，配了 key 也白配。加了二十行标准库解析，已存在的环境变量优先（留给 CI 覆盖）。不为了读一个文件引依赖。

## 2026-11-14 多轮先继承槽位，再路由；classify 只收槽位

M3 的坑在顺序。"那上个月呢"字面上没有指标，M2 的 AC-2.3 会判成"需要澄清"。所以 `ask()` 定成 `session → 槽位继承 → classify → 生成`，并且 `classify(question, catalog, slots)` 的槽位由调用方传进来，它不再自己调 `extract_keywords`：重抽会把继承结果覆盖掉，等于没继承。

文本类判定（闲聊/写操作/超纲）仍然看原始问题，不看继承来的槽位——继承来的指标不能把"今天天气"救成问数。

继承规则：

- 指标：同指标，或者带指代（那/呢/换成…）才继承；换了指标又没指代算新话题（AC-3.4），免得"退款率是多少"莫名带上上个月的月份。
- 维度：显式覆盖，否则继承；"总/整体"且没有"各/按"时视为显式要求不分组。
- 筛选：按类型覆盖（"换成华南"只换地区，品类筛选还在），不是整体替换。
- 时间：显式覆盖，否则继承。

改写后的问句会补一句"（承接上文：GMV、各地区）"，作为生成器的输入：规则生成器和 LLM 看到同一个自包含问题，不用各自再猜上文。改写用的中文标签必须落在 `app/slots.py` 的词表里，`tests/test_session.py` 有回环测试钉住这一点。

## 2026-11-14 多轮只继承"纯槽位"的补充问法

"上个月""按品类看下""华南"这种只补槽位、不带新主语的短句，按 AC-3.2 该继承上文指标。但无条件继承会把"上个月工资发了多少"也接成 GMV——那类"系统自己编指标"的错误，M2 刚用澄清堵住，不能在 M3 又漏回来。

所以判据很窄：去掉槽位词、时间词和语气/改写词后如果什么都不剩（`slots.leftover()`），才当成补充问法；还剩新名词就交给路由澄清。同时判指代前先剥掉时间词，"这个月退款率是多少"里的"这"就不再被当成承接上文。

顺带把"改成华南"从写操作里放出来：只有"把/将 … 改成"才算改数据。改的时候发现正则回溯到单字"删"会绕过"删除的"这层豁免（"已删除的订单"被判 unsafe），lookahead 里补了"除"。

## 2026-11-16 权限：role 按请求传，不进会话；槽位与角色无关

M4 的顺序是 role → 裁剪 catalog → 会话槽位 → classify → 检索/生成。role 不写进 `Session`：同一个会话可以换角色（页面换角色时会顺手清空上文）。槽位只描述业务维度/指标，从不出现列名，所以 analyst 开的头、viewer 接着说不会泄漏隐藏列——`tests/test_permissions.py::RoleSessionTest` 钉住了这条。

## 2026-11-16 列权限用"隐藏列 + 敏感列默认隐藏"，不枚举可见列

AC-4.1 写的是"可见列白名单"。实现上没有把每个角色的可见列一条条列出来——那样等于在 `permissions.py` 里再维护一份 catalog，加列就忘。改成：敏感列默认隐藏，角色要显式把它配进 `masked` 才可见（此时结果行掩码）；普通列两个角色都可见。可验收的部分（不可见列不进 SQL / 结果 / 错误 / prompt）由测试钉住，新增敏感列"不配置就不可见"。

## 2026-11-16 掩码在结果行上做，错误话术不带列名

掩码不写进 SQL（不写 `substr(phone,1,3)||'****'`），否则列名还在、口径也变了；`ask()` 在 `execute()` 之后按结果列名掩码，`masked_columns` 回传给前端标出来。

同理，列白名单命中时错误话术统一是"引用了无权访问的列"：原来的 `no such column: xxx` 会把隐藏列名回传给调用方、也喂回模型，错误本身就成了泄漏通道。`attempts[].sql` 和审计里的列名一律打码成 `***`。

## 2026-11-16 手机号只在明确点名时才进 SQL

加 `customer_phone` 是为了演示掩码，但不想让"各客户的 GMV"顺手把手机号带出来。规则生成器检查问题里是否出现敏感列的列名/同义词，出现了才 SELECT；viewer 的 catalog 里没有该列，所以同一条问题会走"请求了无权列"→ unsafe，而不是悄悄少一列（不静默改列，AC-4.2）。

## 2026-11-16 列权限的两个绕过口子：`select *` 和别名/表达式

列白名单只按列名匹配，`select *` 不含列名——viewer 用它能把 `customer_phone` 整列带出来。掩码按结果列名做，`customer_phone AS p` 或 `substr(customer_phone,1,11)` 一改名/包一层就绕过。

两条都在 `validate` 阶段堵：`denied` 非空时禁 `select *` / `表.*`（`count(*)` 放行）；`masked` 列只允许裸列投影，命中 `AS`、函数包裹、`||`/算术都拒。结果行上再加一层"11 位手机号"的值模式兜底，防止极端的别名写法漏过去。代价是规则更保守——但这是"按列名"做权限/掩码的必然边界，不如把它写死在校验器里。

顺带修了一个跨模块回归：M4 往 catalog 加 `customer_count` 和 `customer` 维度时，`session.py` 的标签表没跟着加，多轮改写回退后 `GROUP BY` 丢了（"各客户的订单数" → "那上个月呢" 变成总数）。现在有一条"标签必须覆盖 catalog 且能回环"的测试，以后加指标/维度会直接红。

## 2026-11-18 评测扩到 50 条，期望 SQL 手写

M5 的起点是 22 条里只有 10/36 个指标×维度组合、customer 维度 0 条。现在补到 50 条：30 个非 customer 组合全覆盖，customer 维度只留 1 条（按客户分组时 customer_count 每行都是 1，铺满 6 条只会稀释信号）；时间从 3 类补到 6 类（去年、最近 30 天、指定月份）。

`expected_sql` 按口径手写，不复制生成器输出；写完逐条跑一遍，只有两边执行结果一致才算过。扩容时抓到两个真问题：

- 退款率的 catalog 口径是 4 位小数，但 `rule_based_sql` 统一在指标外层再套 `ROUND(..., 2)`，0.125 被二次舍入成 0.13；改成按指标配置精度（`METRIC_DECIMALS`），不再一刀切。
- 原来"期望 SQL 执行失败"和"两边都为空"共用 `bool(expected_rows or plan.ok)`，期望 SQL 不合法时可能蒙混过关；现在期望 SQL 自身出错直接判失败。

## 2026-11-18 失败分类用 stage，不猜 error 前缀

M5 先给 `ask()` 的每次 attempt 加 `stage`（validate / execute / timeout / ok），`SQLPlan` 加 `failure_stage`（成功时清空，避免再出现"成功却带失败信息"的自相矛盾）。分类优先级：权限 > 路由 > 超时 > 校验 > 执行（字段/语法）> 检索 > 结果。只有 `field` 和 `syntax` 需要看 SQLite 报错原文，其余靠 stage 就能分。

"权限"排在"路由"前面：viewer 要手机号时路由结论是 unsafe，但失败原因应该归到权限，否则权限问题全被统计成"路由拦截"。

## 2026-11-18 单轮与多轮分开统计，阈值 85/90 写清

M4 的 P0 是"单轮 22 条全绿、多轮算错"，而单轮评测完全看不见。所以加独立的 `eval/multi_turn.json`：5 个会话 × 3 轮（含一轮澄清不记入上下文），同一个 `Session` 顺序跑，逐轮比执行结果；summary 里 `multi_turn_rate` 单独一栏，不并进 `exec_rate`——混算后历史数字不可比，也看不出"多轮坏了"。

阈值统一成：SPEC 门槛 85，CI（以及 `--min-intent` / `--min-multi`）按 90 卡。之前 SPEC 写 85、CI 写 90 却都不解释，现在两边都写明，`--threshold` 的 help 里也写了。

## 2026-11-18 评测分两本账：问数看执行、意图看命中

原来的 50 条用例全是问数，`intent_rate` 实际是"`plan.intent == 'ask'` 的比例"，恒等于 100%，CI 的 `--min-intent` 门禁等于没有。补了 7 条非问数用例（chat ×2 / 超纲 ×2 / clarify ×1 / 写操作 unsafe ×1 / viewer 越权 ×1）并给 `cases.json` 加 `expected_intent` / `role`：

- 执行一致率只在问数用例上算（`ask_total` 做分母），否则会被意图用例稀释；
- 意图命中率在所有用例上算，才是真的路由指标；
- 非问数用例同时要求 `plan.sql == ""`，挡住"该拦却生成 SQL"的回归。

多轮另补 m6（客户维度回环）和 m7（"改成华南"改写）：M4 的 P0 就是客户标签漏更导致多轮丢 GROUP BY，单轮用例看不见；m7 则把 M2/M3 修过的改写说法钉住。客户维度用"各客户的GMV排名"（Top 10）而不是"各客户的订单数"，因为后者 119 个分组会撞上生成器的 `LIMIT 20`，边界并列时结果集不稳定，评测会 flaky。

退款率的口径注释原来写"退款订单数 / 已支付订单数"，SQL 分母其实是全部订单（按订单当前状态统计）；这次统一成注释口径，SQL 只保留 `ROUND(..., 4)` 的外层修正。

## 2026-11-19 Docker：镜像里 seed，entrypoint 兜底

镜像用 `python:3.11-slim`，构建时跑一次 seed，容器起来就是可用的演示库；如果用户把空目录挂到 `/app/data`（空 bind mount 不会触发 Docker 的 volume copy-up），entrypoint 检测到 `data/demo.sqlite` 不存在就补一次。`HEALTHCHECK` 打 `/api/health`，compose 起来后是 `Up (healthy)`。

没上 gunicorn/nginx：`ThreadingHTTPServer` 对这个 demo 足够，引 WSGI 服务器只会多一层配置。compose 也没有 TLS、认证、持久卷——README 的「已知问题」里写明这是单机 demo。

两个安全细节：端口只绑 `127.0.0.1:8000`，不把无鉴权的 demo / 审计接口暴露到局域网；`.env` 被 `.dockerignore` 排除（README 让用户复制它配 LLM，`COPY . .` 会把密钥烤进镜像层），LLM 变量由 Compose 用宿主 `.env` 插值、运行时注入。

顺带踩到两个 Windows 相关的坑：`/bin/sh` 入口脚本必须带 shebang 且是 LF（`.gitattributes` 钉 `*.sh eol=lf`）；PowerShell 的 `Invoke-WebRequest` 发中文 JSON 不按 UTF-8 编码，验证容器接口要用 Python 或浏览器，别拿它当接口测试。

## 2026-11-19 截图用固定脚本，不引 Playwright

页面素材要能重跑：`scripts/shots.py` 自己起服务、起无头 Chrome，用 Chrome DevTools Protocol（标准库实现的 WebSocket 客户端）点按钮、等状态、截图；GIF 用 Pillow 把按时间点抓的帧合成（只是开发工具，`dependencies` 仍是空）。

没引 Playwright/Selenium：为一个 3 图 + 1 GIF 的开发脚本加浏览器运行时和几 MB 依赖，和"clone 下来就能跑"的取向相反；CDP 的 WebSocket 协议本身不复杂，脚本里一百行左右就够。代价是机器上得装 Chrome/Edge（README 写清楚）。

## 2026-11-19 离线演示修复：单独端点，不碰生成器

规则生成器不会写错列名，所以离线页面永远只有一次尝试——截图里看不到"修复时间线"。没去改生成器造假，也没把演示 SQL 塞进主流程，而是加了 `/api/demo/repair`：用 `ScriptedGenerator` 固定两条 SQL（第一次 `GROUP BY` 错列 → `no such column`，第二次改对），响应结构与 `/api/ask` 完全一致，前端复用同一套渲染；不写会话、不参与评测。

这样"演示"和"真实能力"的边界是清楚的：README 的已知问题里写明它只证明时间线与重试链路，真实修复仍然要配 LLM。

## 2026-11-20 模型跑起来后的第一课：68% → 100% 靠补上下文，不是换模型

第一次用 `deepseek-chat` 跑 50 条问数只有 **34/50（68%）**。失败全不是语法问题：
`GROUP BY o.region_name`（列不存在）、`o.order_id = c.customer_id`（乱连表）、漏 `status='paid'`、
多选了 customer_id、把 `SUM(item_amount)` 当 GMV、把 `'now'` 拿去 `substr`。

根因是给模型看的上下文是残的：`SchemaContext.ddl()` 只渲染检索命中的列，JOIN 键被裁掉了；
prompt 里也没有表关系、指标过滤口径、日期写法。规则生成器不读 DDL，所以单测和规则评测都发现不了。

修法（同一模型，只改上下文和 prompt）：

- DDL 强制带上主外键 / status / order_date / 指标 `requires` 点名的列；
- `catalog.json` 增加 `relations`，prompt 里列出"只能按这些连表"；
- 指标 / 维度 / 筛选用到的表若被 top-4 挤掉，就用它替换得分最低的无关表（如"家电品类成交额"必须带 orders）；
- prompt 写死口径（哪些指标过滤 paid、退款率不过滤）、日期写法、只选要求的列、排名才 LIMIT 10、GMV 只能用 `pay_amount`；
- 加一个 few-shot 例子。

结果：50/50 单轮、21/21 多轮、静默回退 0、平均约 0.8s。**模型没换**——这个 schema 上 `deepseek-chat` 足够，
先补上下文再谈换更强的模型；`reasoner`/`qwen-max` 可以作为下一步 A/B，但不是当前瓶颈。

评测也改成双模式：`--engine rule` 是 CI 默认（离线、确定性），`--engine llm` 手动跑；
llm 模式逐条记录 `engine`，静默回退次数 >0 直接判失败，避免把规则成绩当成模型成绩。

## 2026-11-21 LLM 是主线，规则是 fallback：用 10 条 hard cases 把差距写成数字

Reviewer 打开仓库最先看到的事实，曾经是 `app/agent.py` 里整套 `rule_based_sql` / `METRIC_SQL` /
JOIN 拼接模板，评测默认 `--engine rule`，CI 也只跑规则。于是很容易得出"这是模板引擎，模型可有可无"。
真实情况正相反：主路径一直是模型，规则只是没 key 时的降级。

做了三件事：

1. 规则 SQL 整体搬到 `app/fallback.py`，`RuleGenerator` 改名 `OfflineFallback`，文件头写明
   "离线回退 + CI 确定性基线，不是主路径"；`app/agent.py` 只留 `LLMGenerator` / `ScriptedGenerator` /
   `build_context` / `validate` / `execute` / `ask`。行为不变：159 条测试全绿、规则 50/50、多轮 21/21。
2. 新增 `eval/hard_cases.json`：10 条规则拼不出来的问法（IN 多值、季度、HAVING、任意 Top-N、占比、
   复购、状态分布、滚动 7 天）。`--hard` 单独统计 `hard_*`，不并入基础 50 条。实测规则 **0/10**、
   `deepseek-chat` **7–8/10**（当时连跑 4 次：8 / 7 / 8 / 8；后续 prompt 改动后最近 4 次为 7–8，见 README 与快照）。
3. README 改成"检索 → 模型写 SQL → 校验 → 执行 → 失败重试"在前，规则 fallback 单独一节写定位。

为什么保留规则（而不是删掉）：

- CI 不配 key，需要离线、确定性的回归网，挡住检索/校验/权限/多轮这些与模型无关的退化；
- 没 key / 模型超时失败时服务不能空转，`engine=rule` 让调用方看得出来；
- 它是 hard 用例的对照组。没有这组基线，"模型必要"只是断言；有了它，差距是命令能复现的数字。

hard 组不进 CI 门槛：规则分数天然低，设成门禁只会逼人删规则或改期望。模型失败的几条也照实留在
README（Top-1 被 prompt 的 LIMIT 10 带偏、上季度写成最近三个月、复购偶发把 GROUP BY 写在外层），
没有回头去改 `expected_sql`——那会把"证据"变成"凑数"。

## 2026-11-21 页面只跑离线基线，模型成绩读只读快照

页面上的「跑评测」曾经容易被读成"模型评测"：点一次显示 2–3ms，其实是 `engine=rule` 的离线基线。
这一版把它正名为「跑离线基线」，并给页面接一个只读快照：

- `scripts/record_llm_eval.py` 跑真实模型（基础 57 条 + 多轮 21 轮 + hard × 4），剥掉明细，
  只写 `eval/llm_result.json`：模型、日期、50/50、21/21、hard 范围、静默回退；
- `GET /api/eval/llm/last` 只读这个文件；页面有快照就显示「上次模型评测」，没有就隐藏；
- 不在页面点一次跑一次模型：70 次生成请求、约 1 分钟、还有采样波动，挂按钮上既慢又不可重复。

公开 benchmark（BIRD/Spider）只做了方案评估，现在不接：hard 组已经证明模型不可替代，公开集是
外部有效性加分项；真要接是独立 `benchmarks/`、抽样 100–200、关自动 LIMIT、严格/宽松 EX、打乱题目，
1–2 天且不进 CI。仓库里不会出现没真跑过的公开集分数。

## 2026-11-22 “有哪些商品类别”不该先澄清：维度清单放行

用户问“华东地区门店有哪些商品类别”，路由回了 clarify，澄清项只剩指标/时间。原因是 AC-2.3 用“抽不到指标”
判澄清，但这个问题本来就不需要指标——它是列维度值（`SELECT DISTINCT`），不是聚合。把“没有指标”等同于
“信息不够”是规则路由的盲区。

改了三处：

- `router.py`：识别到维度 + “有哪些 / 列出”的清单问法 → `ask`（无指标也放行）；有筛选但没维度时，
  澄清项也补“按什么维度看”，不再只给指标/时间。
- `agent._required_tables`：没有指标时也带 `orders`，否则“地区 → 明细 → 商品”这条连表链会断在 context 里。
- prompt：加“只问有哪些维度时用 `SELECT DISTINCT`，不臆造指标”，并加一个华东品类的示例；
  “门店”这类 schema 里没有的词不编表，用已有维度近似。

结果：同一句真实模型返回 `SELECT DISTINCT p.category ... WHERE r.region_name = '华东'`，4 个品类、
`engine=llm`。规则 fallback 仍只覆盖指标模板，维度清单没 key 时会按指标兜底、可能答非所问，
`engine=rule` 会标出来——这是 fallback 的已知边界，不是模型能力。

## 2026-11-22 放行 CTE：模型能写复杂 SQL，先别让校验器卡死

对比同类项目时做了一次实测："华东销售额最高的三个品类，每个品类再列出销售额前两名商品"。
给模型加一条"分组 Top-N 用 `ROW_NUMBER()`"的提示后，它写出了带两层 CTE + 窗口函数的 SQL——
但我们的 `validate` 只允许 `SELECT` 开头，直接以"只允许 SELECT"拒掉；即使放行，`FROM ranked`
也会被表白名单当成未知表。

这是典型的"规则把模型能出的结果卡死"：约束的本意是只读安全，不是禁止复杂查询。改法：

- 允许 `SELECT` 或 `WITH ... SELECT`（CTE）；`WITH` 里必须出现 `SELECT`，DML 关键字仍在 `FORBIDDEN` 里拒；
- 从 `from` / `join` 提取的表名里减去 `with` / `,` 后面的 CTE 名，避免把 CTE 当物理表；
- prompt 增加分组 Top-N 规则：先取 Top-N 分组，再用 `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` 筛 `rn <= N`，不要全局 LIMIT。

验证：同一句真实模型返回 6 行（4 个品类里取销售额前 3 的品类 × 前 2 个商品），
基础 50/50、多轮 21/21、hard 单次 8/10，180 个测试全绿。
边界：这仍然是一条 SQL；"为什么下降/归因/多步对比"需要分析模式（多条 SQL + 综合结论），不在这次范围。

## 2026-11-22 多轮筛选列：用 feedback 让模型改，不改期望

加完 CTE / 分组 Top-N 规则后，多轮评测从 21/21 掉到 20/21：模型在"改成华南""那华南呢"里多 SELECT 了
`region_name`，"那总共多少呢"还丢过筛选。不是模型不会写，而是它把筛选列当成"顺带展示"；而
`multi_turn.json` 的期望是"只换筛选、不按筛选列分组"。

没有改期望，也没有在代码里裁列，而是加了一层生成后的筛选形状检查（`_shape_feedback`）：

- 上文筛选值必须出现在 SQL 里，丢了就让模型补回 WHERE；
- 单值筛选且没有维度时，筛选列不能出现在最后一个 SELECT 的投影里；
- 多值筛选（"华东和华南对比"）允许按筛选列分组，不能误判；
- 违反时当作一次 `validate` 失败，把话术作为 `feedback` 让模型重写，不执行错误 SQL。

结果：多轮 21/21（连跑 3 次）、基础 50/50、hard 7–8/10，180 个测试全绿。
这条检查只约束"筛选语义"，不替模型写 SQL；模型仍然可以自己决定分组、指标和窗口函数。

## 2026-11-22 complex 评测：分组 GMV 沿用整单口径，不可相加

Step 1 的 13 条 complex 用例里有 5 条（#1/#6/#9/#10/#12）按品类/商品/品牌拆 GMV。`gmv` 的 catalog 定义是整单 `SUM(orders.pay_amount)`，而 `order_items` 一单多行，join 后 `SUM(o.pay_amount)` 会把整单金额按明细行重复计入：全国 paid GMV 3,362,703.71，按品类相加是 7,877,829.48（约 2.34×），品类排名和品牌 Top-2 商品都会变。

没有把 expected 改成 `SUM(order_items.item_amount)`：prompt 和口径里已经写死“GMV 只能用 `pay_amount`”，直接换会让基础集与模型契约一起漂。complex 沿用基础集写法，保证与既有 50 条可比；代价是分组 GMV 不可跨组相加，也不能当“真实分摊销售额”用。要可加口径，应新增独立指标（如 `line_amount = SUM(order_items.item_amount)`）并重录基础集，那是单独任务。README 与 `eval/complex_cases.json` 都写了这条限制，Step 2 的分析结论不能把分组 GMV 当可加事实。

## 2026-11-23 分析模式 MVP：有界 plan-and-execute，默认关闭

Step 2 没有引入 LangGraph/ReAct：新增 `app/analysis.py`，Planner 只看改写后的问句、拆 2–4 个自包含子问题；Executor 每步复用 `agent.ask(mode="single")`，同 role、不共享 session，校验/权限/只读/5s 超时全部沿用；Synthesizer 只看步骤结果，不给 schema、不能再写 SQL，结论必须引用 `[sN]`。

三个刻意的边界：

1. **默认关闭**：页面开关默认关，HTTP 不传 `mode` 就是快路径，响应结构不变。
2. **失败不隐藏**：澄清/越权/预算不足标 `skipped`；Planner 失败回退单 SQL 并带 `analysis_error`；Synthesizer 没有成功步骤时不调模型，直接说“当前结果不足以判断”；模型漏写的失败步骤由代码补进“局限”。
3. **先测量再换模型**：Planner/Synthesizer 用 `ANALYSIS_MODEL`（默认同 `LLM_MODEL`）。3 条 complex 题 A/B：`deepseek-chat` 平均 4.6s、2 条完整；`deepseek-reasoner` 平均 11.8s、3 条完整；两者步骤都 6/6、无假因果。reasoner 的 completion token 约为 chat 的 4.4 倍，样本只有 3 条，**不换默认模型**，下一步扩到 complex 全集再决策。

A/B 第一轮抓到两个真问题，按“先测量”改的是提示词/输入，不是期望：

- 综合器原来只看 20 行预览：reasoner 在分组 Top-N 题上漏看 24 行里的目标商品，算错。现在执行层保留 50 行给综合器、UI 仍只渲染 20 行，prompt 明确“截断后的行不能用于排名/汇总推断”。
- Planner 把“退款率（退款金额/订单金额或退款订单数/订单数）”这种公式写进子问题，SQL 生成选了金额口径。现在 Planner prompt 明确“不要写 SQL、公式或指标口径”，由执行器按 catalog 定义生成。

已知没解决：Planner 仍可能拆得不好（chat 在月度环比题上把“去年12月”拆成独立步骤，却没生成 LAG 环比，结论只能说“缺计算步骤”）；步骤 `ok=true` 只代表 SQL 跑通，不代表业务口径对；50 行之外仍可能漏数据。

## 还没想清楚

- 指标口径冲突怎么办：同一个"客单价"，不同部门算法不一样。现在是单一 metric 定义，真做要加"口径版本"。
- 模型生成的 SQL 很慢时（全表扫）：现在用 progress handler 卡 5 秒硬超时，不做代价预估、也不提前拦。要不要在生成后先 `EXPLAIN QUERY PLAN` 估一下，还没想清楚。
