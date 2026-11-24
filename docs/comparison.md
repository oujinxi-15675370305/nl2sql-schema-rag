# 同类 NL2SQL 项目对比（2026-11-23）

> 直接比"准确率"不成立：题库、数据库、难度和口径都不同。这份文档把能验证的东西分开列：
> 定位、规模、复杂问题能力、评测方式，以及作者自报的数字。星数取自 GitHub API（2026-11-23），
> 只代表关注度，不代表质量。

## 1. 先看赛道：大框架 vs 小项目

| 项目 | 定位 | 活跃度 / 规模 | 复杂问题与 Agent | 评测 | 对本项目的参考价值 |
|---|---|---|---|---|---|
| [DB-GPT](https://github.com/eosphoros-ai/DB-GPT) | Agentic data assistant：SQL + Python + 沙箱 + RAG + 报告 | 20.3k★，MIT，2026-11 仍在日更 | 规划、分步执行、工具调用、sandbox、AWEL、skills | 未在 README 报统一 EX | 复杂问题的"目标形态"，但整体是数据 Agent 平台，照搬成本极高 |
| [WrenAI](https://github.com/Canner/WrenAI) | GenBI：语义层（MDL）+ 受治理的 text-to-SQL + dashboard | 18.4k★，活跃 | 面向 Claude Code/Cursor/MCP 等 agent 提供语义层与执行原语 | 未报统一 EX，强调 governance | 语义层/指标定义可评审，是"正确而不是自信地错"的方向 |
| [Chat2DB](https://github.com/OtterMind/Chat2DB) | AI 数据库客户端 + SQL 工作台 | 28.4k★，活跃 | 生成/解释/优化 SQL、本地 agent 预览 | 无统一 benchmark | 产品化客户端，不是同一类产品 |
| [SuperSonic](https://github.com/tencentmusic/supersonic) | Chat BI + Headless BI（语义层） | 5.2k★，活跃 | 规则 + LLM 语义解析、纠错、多轮、三级权限 | 未报统一 EX | 语义解析/纠错的工程化参考，Java 技术栈 |
| [Vanna](https://github.com/vanna-ai/vanna) | train-and-retrieve 的 Python text-to-SQL 库 | 23.8k★，**2026-03 已归档** | 检索 DDL/docs/Q→SQL 对，agentic retrieval | 无统一 benchmark | 早期最流行的"两行接模型"路径；仓库已只读 |
| [Dataherald](https://github.com/Dataherald/dataherald) | API-first NL→SQL 编排 | 3.7k★，**最后 push 2024-07** | golden SQL + schema 指令 | 无 | 参考架构，已停更 |
| [nl2sql-agent](https://github.com/Howard-Qin-art/nl2sql-agent) | 中文只读查询 Agent（LangGraph） | 3★，新项目 | schema linking + N 候选投票 + 自愈 + MCP | 自建 60 + DuSQL 100 + BIRD mini 30/55，严格/宽松双口径 | **和本项目体量最接近，最值得逐项对照** |

大框架的共同点：都在往"数据 Agent"走（规划、多个工具、沙箱、报告/看板、语义层/治理），
但代价是依赖多、部署重、集成面大。它们不是这个 5 表 demo 的替代品，硬接会把项目变成另一个产品的壳。

## 2. 同体量对照：nl2sql-agent vs 本项目

| 维度 | nl2sql-agent | 本项目 |
|---|---|---|
| 数据规模 | 9 张表 / 约 1.5 万行 | 6 张表 / 800 单 / 1608 明细 |
| Schema 检索 | BM25 + 字面值命中 + 外键反推 + 向量 + RRF + LLM 精排 | 指标/表/列三路 + 关键词/哈希稠密 + RRF，最多 4 表 / 12 列 |
| SQL 生成 | N=3 候选 + 结果集指纹投票 | 单条 SQL，失败带错误重试（最多 2 次） |
| 复杂 SQL | 支持窗口函数/CTE，但作者自报仍是短板 | 2026-11-22 起放行只读 `WITH ... SELECT`，支持 CTE + 窗口函数 |
| 安全护栏 | sqlglot AST 白名单 + 只读连接 + 超时 | 正则校验 + 表白名单 + 列白名单 + 只读连接 + 5s 超时 |
| 权限/脱敏 | 未见角色级列掩码 | 角色裁剪 catalog、隐藏/掩码列、审计（本项目更强） |
| 回答形态 | SQL + 结果 + 中文结论 + trace | SQL + 结果表；可选分析模式（`mode=analysis`）给步骤 + 引用 `[sN]` 的结论；可选自动结论（`conclusion=true`，1–3 句、只描述结果） |
| 记忆 | 会话 4 轮 + 长期偏好（文件/Redis） | 会话 5 轮、槽位继承（进程内存） |
| 接口 | FastAPI + CLI + Streamlit + MCP | HTTP + 原生 JS 页面 |
| 测试 | 141 个（不碰模型） | 288 个（不碰模型，含 HTTP 契约、分析/holdout/自动结论/A-B 覆盖率） |
| 运行时依赖 | LangGraph、sqlglot、FastAPI、Streamlit 等 | **零运行时依赖（标准库）** |
| 评测 | 自建 60 + DuSQL 100 + BIRD mini 30/55 | 自建 57+21 + hard 10 + complex 13 + **BIRD Mini-Dev fresh holdout 50 题** |
| 作者自报准确率 | qwen-plus：自建 79.6/98.0、DuSQL 48.0/68.0；qwen2.5:7b：自建 63.3/93.9、BIRD mini 40.0/76.7（严格/宽松） | deepseek-chat：自建 50/50、多轮 21/21、hard 8–9/10、complex 最新 3 次 12/11/12（C9 偶发；不当硬门槛）；**BIRD Mini-Dev fresh 50 题官方 EX 36/50（72%）** |

两个项目不能直接比分数：本项目的 50 条是围绕 6 个指标 × 6 个维度手写的，难度明显低于 DuSQL/BIRD；
`nl2sql-agent` 的 60 条也自建，但它另外接了公开集，且公开集分数明显低于自建集（自建 79.6 → DuSQL 48.0）。
本项目现在也有了 BIRD Mini-Dev fresh 50 题：官方 EX 36/50（72%），换 schema 后与自建 complex 12/12 有差距，这是正常的。
但两边样本量、prompt、strict/loose 定义不同，**不能把 36/50 和 30/55 当同一张榜**。

## 3. 用真实问题探测本项目的边界

2026-11-23 用 `deepseek-chat` 实测：

| 问题 | 实际结果 | 结论 |
|---|---|---|
| 华东地区有哪些商品品类，各品类有哪些具体商品，销售额如何？ | 1 条 SQL、24 行（category / product_name / gmv） | 自然语言"复合"，但仍是一条 GROUP BY，能答 |
| 对比华东和华南的 GMV 和退款率，哪个更好？ | 1 条 SQL、2 行、两个指标 | 单条 SQL 能表达多指标对比，能答 |
| 华东销售额最高的三个品类，每个品类再列出销售额前两名商品 | 修复前：24 行全量、无分组 Top-N；修复后：CTE + `ROW_NUMBER()`、6 行 | **模型会写**，之前是校验器不允许 `WITH` 卡死的 |
| 为什么上个月 GMV 下降了？ | 返回上个月一行 GMV，没有对比/归因 | 单条 SQL 只能取数，**回答不了"为什么"** |

这张表比分数更能说明问题：**瓶颈有两层**——
1. 我们自己加的约束（只允许 `SELECT` 开头、固定 LIMIT）会把模型能写的复杂 SQL 卡掉；
2. 真正的"分析型问题"（为什么、归因、多步对比）需要多条 SQL + 综合结论，单条 SQL 架构表达不了。

## 4. 结论与路线

本项目的定位不应该是"又一个通用 NL2SQL 框架"，而是：
**一个零依赖、护栏/权限清楚、可复现评测的中文问数 demo；复杂分析作为可选增强，不做成大而全。**

已经在做/刚做完：

- [x] 规则 fallback / LLM 主线拆分，hard 用例证明规则覆盖外的差距；
- [x] 维度清单（有哪些X）不再误澄清；
- [x] 允许只读 `WITH ... SELECT`，支持 CTE + 窗口函数（分组 Top-N 已实测 6 行正确）；
- [x] 页面浅色工作台、结果来源徽标、模型快照只读展示；
- [x] 复杂问法评测集（13 条，独立 `--complex`，规则 0/12 vs 模型最新 3 次 12/11/12（C9 偶发；不当硬门槛），不进 CI；分组 GMV 口径限制见 README）。

路线与下一步（按性价比）：

1. **复杂问法评测集（已完成）**：13 条 CTE/窗口/多指标/占比/复购/多维度 + 1 条归因题；
   规则 0/12、`deepseek-chat` 最新 3 次 12/11/12（C9 偶发；不当硬门槛），单独 `--complex`，不进 CI。分组 GMV 沿用整单口径、不可相加。
2. **自动结论（已完成）**：SQL 执行后多一次 LLM 调用，把结果写成 1–3 句中文结论；
   prompt 明确“只根据结果陈述，不编原因”；默认关闭，显式 `conclusion=true` 才启用。
3. **分析模式（1–2 天，可选）**：显式 `mode=analysis`；LLM 先拆成 2–4 个子问题，
   每个复用现有 `ask()` 的检索/校验/执行/权限链路，最后用一次 LLM 综合结论；
   UI 展示步骤和每步 SQL。仍守只读、权限、超时；不做无限自治 agent。
4. **公开集（BIRD 已做，Spider/DuSQL 未接）**：独立 `benchmarks/`、fresh 50 题、关自动 LIMIT、
   官方 EX；不进 CI。已跑 BIRD Mini-Dev SQLite fresh 50 题官方 EX 36/50；没有真跑过就不写分数。

不建议：为了"像 DB-GPT"而接一堆 Agent/RAG/沙箱依赖；也不建议在页面点击里跑模型评测。
这个项目的优势是"小、透明、可复现"，不是"功能多"。

## 5. 数据来源

- GitHub API：仓库元数据（stars / archived / pushed_at），2026-11-23。
- 各项目 README：
  [DB-GPT](https://github.com/eosphoros-ai/DB-GPT)、
  [WrenAI](https://github.com/Canner/WrenAI)、
  [Chat2DB](https://github.com/OtterMind/Chat2DB)、
  [SuperSonic](https://github.com/tencentmusic/supersonic)、
  [Vanna](https://github.com/vanna-ai/vanna)、
  [Dataherald](https://github.com/Dataherald/dataherald)、
  [nl2sql-agent](https://github.com/Howard-Qin-art/nl2sql-agent)。
- 本项目数字：`eval/` 与 `eval/llm_result.json`，均为本机真实运行；公开集：BIRD Mini-Dev SQLite
  fresh 50 题官方 EX 36/50（`benchmarks/results/bird_fresh_base.json`），非官方 leaderboard。
