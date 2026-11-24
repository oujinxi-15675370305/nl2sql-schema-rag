const $ = (sel) => document.querySelector(sel);

const stateEl = $("#state");
const metaEl = $("#meta");
const tableWrap = $("#table-wrap");
const sqlEl = $("#sql");
const btnRun = $("#btn-run");
const btnDemo = $("#btn-demo");
const answerEl = $("#answer");
const attemptsEl = $("#attempts");
const rewriteEl = $("#rewrite");
const roleEl = $("#role");
const llmLastEl = $("#llm-last");
const llmLastCardEl = $("#llm-last-card");
const engineBadgeEl = $("#engine-badge");
const emptyStateEl = $("#empty-state");
const sqlBlockEl = $("#sql-block");
const analysisEl = $("#analysis");
const modeEl = $("#mode-analysis");
const conclusionEl = $("#conclusion-toggle");

// M3：会话 id 由服务端下发；带上它才能接上文（"那上个月呢"）
let sessionId = "";
// M4：角色按请求传，服务端按角色裁剪 catalog；换角色等于换权限，顺手开新会话
let role = roleEl ? roleEl.value : "analyst";

const SAMPLES = [
  "总GMV是多少",
  "各地区的GMV",
  "各品类的销售额",
  "今年各月份GMV",
  "各商品销量排名",
  "退款率是多少",
  "华东地区的客单价",
  "各地区的客户数",
  "各客户的GMV和手机号",
];

const INTENT_LABEL = {
  chat: "闲聊",
  clarify: "需要补充信息",
  out_of_scope: "超出数据范围",
  unsafe: "越权 / 写操作",
};

// 结果里的 engine 是服务端下发的：llm=模型、rule=规则 fallback、custom=脚本化演示。
// 不标出来，页面只能靠耗时猜（模型 ~0.8s，规则 ~2ms），这正是最容易被误读的地方。
const ENGINE_LABEL = {
  llm: "模型生成",
  rule: "规则 fallback",
  custom: "固定 SQL 演示",
  analysis: "分析模式",
};
const ENGINE_CLASS = {
  llm: "engine-llm",
  rule: "engine-rule",
  custom: "engine-custom",
  analysis: "engine-analysis",
};
// 分析步骤的表格只渲染前 20 行；综合器最多看 50 行（见 app/analysis.py PREVIEW_ROWS）
const STEP_PREVIEW_ROWS = 20;

const INTENT_SAMPLES = ["上个月的GMV", "各地区的订单数"];

const CONTEXT_ROWS = [
  ["#ctx-metrics", "指标"],
  ["#ctx-tables", "表"],
  ["#ctx-columns", "列"],
];

function escapeHtml(text) {
  const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(text ?? "").replace(/[&<>"']/g, (c) => map[c]);
}

function setEngineBadge(engine) {
  if (!engineBadgeEl) return;
  if (!engine) {
    engineBadgeEl.hidden = true;
    return;
  }
  engineBadgeEl.textContent = ENGINE_LABEL[engine] || engine;
  engineBadgeEl.className = "engine-badge " + (ENGINE_CLASS[engine] || "");
  engineBadgeEl.hidden = false;
}

// SQL 高亮的输入是模型生成的文本，必须先按 token 切分再逐段转义，不能先转义
// （转义会把引号变成 &#39;，字符串 token 就匹配不到了）。
const SQL_TOKEN = /('[^']*')|(--[^\n]*)|(\b(?:SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|CROSS|ON|AND|OR|NOT|IN|IS|NULL|AS|GROUP\s+BY|ORDER\s+BY|LIMIT|OFFSET|HAVING|WITH|OVER|PARTITION\s+BY|CASE|WHEN|THEN|ELSE|END|DISTINCT|COUNT|SUM|AVG|MIN|MAX|ROUND|COALESCE|CAST|DATE|SUBSTR|STRFTIME|UNION|ALL|DESC|ASC|BETWEEN|LIKE)\b)/gi;

function highlightSql(sql) {
  if (!sql) return "";
  SQL_TOKEN.lastIndex = 0;
  let out = "";
  let last = 0;
  let match;
  while ((match = SQL_TOKEN.exec(sql)) !== null) {
    out += escapeHtml(sql.slice(last, match.index));
    const cls = match[1] ? "sql-str" : match[2] ? "sql-comment" : "sql-kw";
    out += `<span class="${cls}">${escapeHtml(match[0])}</span>`;
    last = match.index + match[0].length;
  }
  out += escapeHtml(sql.slice(last));
  return out;
}

function setState(text, kind = "") {
  stateEl.className = "state " + kind;
  stateEl.textContent = text;
}

function renderTags(el, items, kind) {
  const label = `<span class="tag-label">${kind}</span>`;
  if (!items || !items.length) {
    el.innerHTML = `${label}<span class="muted">—</span>`;
    return;
  }
  el.innerHTML = label + items
    .map((it) => `<span class="tag">${escapeHtml(it.key)}${it.table ? " · " + escapeHtml(it.table) : ""}</span>`)
    .join("");
}

function clearTags() {
  CONTEXT_ROWS.forEach(([sel, label]) => {
    $(sel).innerHTML = `<span class="tag-label">${label}</span><span class="muted">—</span>`;
  });
}

const COLUMN_LABELS = {
  quarter: "季度",
  month: "月份",
  gmv: "销售额",
  order_count: "订单数",
  refund_rate: "退款率",
  customer_count: "客户数",
  region_name: "地区",
  customer_name: "客户名",
  brand: "品牌",
  category: "品类",
  mom: "环比",
};

function columnLabel(name) {
  return COLUMN_LABELS[String(name).toLowerCase()] || name;
}

function tableHtml(columns, rows) {
  if (!rows || !rows.length) return `<div class="empty">没有数据</div>`;
  const numeric = columns.map((_, index) =>
    rows.every((row) => row[index] === null || typeof row[index] === "number")
  );
  const head = `<thead><tr>${columns
    .map((c, index) => `<th${numeric[index] ? ' class="num"' : ""}>${escapeHtml(columnLabel(c))}</th>`)
    .join("")}</tr></thead>`;
  const body = rows
    .map((r) => {
      const cells = r.map((v) => {
        if (v === null) return `<td class="null">NULL</td>`;
        if (typeof v === "number") {
          const text = Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
          return `<td class="num">${text}</td>`;
        }
        return `<td>${escapeHtml(v)}</td>`;
      });
      return `<tr>${cells.join("")}</tr>`;
    })
    .join("");
  return `<table>${head}<tbody>${body}</tbody></table>`;
}

function renderTable(columns, rows) {
  tableWrap.innerHTML = tableHtml(columns, rows);
}

// 非问数意图：显示话术；澄清时列出要补的信息，其它意图给两个可直接点的示例
function renderReply(data, samples) {
  const parts = [];
  if (data.clarify_options && data.clarify_options.length) {
    // reply 本身就是这些选项拼起来的，这里只留一句引导，避免重复
    parts.push(`<p class="reply">还差点信息，补充下面任意一项再问：</p>`);
    parts.push(`<ul class="options">${data.clarify_options.map((o) => `<li>${escapeHtml(o)}</li>`).join("")}</ul>`);
  } else {
    if (data.reply) parts.push(`<p class="reply">${escapeHtml(data.reply)}</p>`);
    if (samples && samples.length) {
      parts.push(`<div class="choices">${samples.map((s) => `<button class="chip choice">${escapeHtml(s)}</button>`).join("")}</div>`);
    }
  }
  answerEl.innerHTML = parts.join("");
  answerEl.querySelectorAll(".choice").forEach((btn) => {
    btn.addEventListener("click", () => run(btn.textContent));
  });
}

// 修复过程：尝试 >= 2 次才显示，压成一行一次，便于截图/录屏
function renderAttempts(attempts) {
  if (!attempts || attempts.length < 2) {
    attemptsEl.innerHTML = "";
    return;
  }
  const rows = attempts.map((a) => {
    const detail = a.ok ? "执行成功" : (a.error || "").split("\n")[0].slice(0, 120);
    return `<div class="attempt ${a.ok ? "ok" : "err"}">第 ${a.n} 次 ${a.ok ? "✓" : "✗"} ${escapeHtml(detail)}</div>`;
  });
  attemptsEl.innerHTML =
    `<div class="attempts-title">自我修复：共尝试 ${attempts.length} 次</div>` + rows.join("");
}

// 结论里的 [s1] 是步骤引用：先整体转义，再把引用包成小标签（转义不会动方括号）
function formatAnswer(text) {
  return escapeHtml(text)
    .replace(/\n/g, "<br>")
    .replace(/\[s(\d+)\]/gi, '<span class="cite">[s$1]</span>');
}

function renderStep(step) {
  const skipped = !!step.skipped;
  const state = step.ok ? "ok" : skipped ? "skip" : "err";
  const label = step.ok ? "成功" : skipped ? "跳过" : "失败";
  const cardClass = step.ok ? "" : skipped ? " skipped" : " err";
  const purpose = [
    step.purpose,
    (step.depends_on || []).length ? `依赖 ${step.depends_on.join("/")}` : "",
  ].filter(Boolean).join(" · ");
  const error = step.error ? `<div class="step-error">${escapeHtml(step.error)}</div>` : "";
  const warning = step.warning ? `<div class="step-warning">口径警告：${escapeHtml(step.warning)}</div>` : "";
  const sql = step.sql ? `<div class="step-sql">${highlightSql(step.sql)}</div>` : "";
  const rows = (step.rows || []).slice(0, STEP_PREVIEW_ROWS);
  const table = (step.ok && rows.length)
    ? `<div class="step-table">${tableHtml(step.columns || [], rows)}</div>`
    : "";
  const foot = [];
  if (step.engine) foot.push(`来源 ${ENGINE_LABEL[step.engine] || step.engine}`);
  if (step.row_count) {
    const preview = rows.length < step.row_count ? ` · 预览 ${rows.length} 行` : "";
    foot.push(`共 ${step.row_count} 行${preview}`);
  }
  if (step.elapsed_ms) foot.push(`${step.elapsed_ms} ms`);
  return `<div class="step-card${cardClass}">
    <div class="step-head">
      <div>
        <div class="step-title"><span class="step-id">[${escapeHtml(step.id)}]</span> ${escapeHtml(step.question)}</div>
        ${purpose ? `<p class="step-purpose">${escapeHtml(purpose)}</p>` : ""}
      </div>
      <span class="step-badge ${state}">${label}</span>
    </div>
    ${error}${warning}${sql}${table}
    ${foot.length ? `<div class="step-foot">${foot.map((x) => `<span>${escapeHtml(x)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function renderAnalysis(data) {
  if (sqlBlockEl) sqlBlockEl.hidden = true;
  tableWrap.innerHTML = "";
  answerEl.innerHTML = "";
  setEngineBadge("analysis");
  if (data.analysis_model && engineBadgeEl) {
    // 分析模型单独标出来：SQL 生成仍走 LLM_MODEL，两者可能不是一个模型
    engineBadgeEl.textContent = `分析模式 · ${data.analysis_model}`;
  }
  const steps = data.steps || [];
  const okCount = steps.filter((s) => s.ok).length;
  const parts = [];
  if (data.analysis_warning) {
    parts.push(`<div class="analysis-note">${escapeHtml(data.analysis_warning)}</div>`);
  }
  if (data.analysis_error) {
    parts.push(`<div class="analysis-note">${escapeHtml(data.analysis_error)}</div>`);
  }
  if (data.answer) {
    parts.push(`<div class="analysis-answer"><h3>结论</h3>` +
      `<div class="analysis-text">${formatAnswer(data.answer)}</div></div>`);
  }
  if (steps.length) {
    parts.push(`<div class="steps-head"><h3>执行步骤</h3>` +
      `<span>${okCount}/${steps.length} 步成功</span></div>`);
    parts.push(steps.map(renderStep).join(""));
  }
  analysisEl.innerHTML = parts.join("");
  analysisEl.hidden = !parts.length;
  const usage = data.usage && (data.usage.prompt_tokens || data.usage.completion_tokens)
    ? ` · token ${data.usage.prompt_tokens || 0}+${data.usage.completion_tokens || 0}`
    : "";
  metaEl.textContent = `${steps.length} 步 · ${okCount} 步成功 · ${data.elapsed_ms} ms${usage}`;
  if (!steps.length) {
    setState(data.analysis_error || "没有可执行步骤", "error");
  } else if (!data.answer) {
    // 步骤结果还在，但综合失败/没给结论：不能显示“完成”
    setState("综合未完成，步骤结果仍可查看", "error");
  } else if (okCount === steps.length) {
    setState("完成", "ok");
  } else if (okCount > 0) {
    setState(`部分步骤失败：${steps.length - okCount}/${steps.length}`, "error");
  } else {
    setState("没有可用结果", "error");
  }
}

function clearResult() {
  metaEl.textContent = "";
  sqlEl.textContent = "—";
  rewriteEl.textContent = "";
  tableWrap.innerHTML = "";
  answerEl.innerHTML = "";
  attemptsEl.innerHTML = "";
  if (analysisEl) {
    analysisEl.innerHTML = "";
    analysisEl.hidden = true;
  }
  setEngineBadge(null);
  if (emptyStateEl) emptyStateEl.hidden = true;
  if (sqlBlockEl) sqlBlockEl.hidden = false;
  clearTags();
}

// 上次模型评测：读只读快照（GET /api/eval/llm/last），没有就隐藏整张卡片。
// 页面任何点击都不会现场调模型；更新快照用 python -m scripts.record_llm_eval。
async function loadLastLlmEval() {
  if (!llmLastEl || !llmLastCardEl) return;
  try {
    const res = await fetch("/api/eval/llm/last");
    const data = await res.json();
    const s = data.summary || {};
    if (!data.available || !s.ask_total) {
      llmLastCardEl.hidden = true;
      return;
    }
    const hard = s.hard_match_min === s.hard_match_max
      ? `${s.hard_match_max}/${s.hard_total}`
      : `${s.hard_match_min}–${s.hard_match_max}/${s.hard_total}`;
    const date = (data.generated_at || "").slice(0, 10);
    llmLastEl.innerHTML =
      `<span class="llm-model"><span class="llm-dot"></span>${escapeHtml(data.model || "模型")}` +
      `<span class="llm-date">${escapeHtml(date)}</span></span>` +
      `<span class="llm-metrics">` +
      `<span>基础 <b>${s.exec_match}/${s.ask_total}</b></span>` +
      `<span>多轮 <b>${s.multi_turn_match}/${s.multi_turn_total}</b></span>` +
      `<span>hard <b>${hard}</b></span>` +
      `<span>回退 <b>${s.fallbacks ?? 0}</b></span>` +
      `</span>`;
    llmLastEl.hidden = false;
    llmLastCardEl.hidden = false;
  } catch (e) {
    llmLastCardEl.hidden = true;
  }
}

// ask 与 demo 端点返回同一份结构，渲染只写这一套
function renderResult(data) {
  if (emptyStateEl) emptyStateEl.hidden = true;
  // 服务端可能新开一个会话，后续请求带上它（演示接口不建会话，回空就不动）
  if (data.session_id) sessionId = data.session_id;
  // M3：告诉用户这句被补了什么，不然"那上个月呢"的结果看着像凭空来的
  if (data.inherited_slots && data.inherited_slots.length && data.rewritten_question) {
    rewriteEl.textContent = `承接上文 → ${data.rewritten_question}`;
  }

  // Step 2：分析模式有独立结构（结论 + 步骤）；Planner 失败会回退 mode=single
  if (data.mode === "analysis") {
    if (data.intent && data.intent !== "ask") {
      // 澄清/越权在分析模式下仍然只回话术，不硬跑步骤
      renderReply(data, INTENT_SAMPLES);
      setEngineBadge("analysis");
      if (sqlBlockEl) sqlBlockEl.hidden = true;
      setState(`意图：${INTENT_LABEL[data.intent] || data.intent}`, "info");
      return;
    }
    renderAnalysis(data);
    return;
  }
  if (data.analysis_error) {
    // 规划失败回退单 SQL：明确告诉用户这一轮没跑分析，而不是悄悄少几步
    analysisEl.hidden = false;
    analysisEl.innerHTML =
      `<div class="analysis-note">分析模式未执行：${escapeHtml(data.analysis_error)}</div>`;
  }

  // M2：非 ask 不生成 SQL，页面显示意图话术而不是"失败"
  if (data.intent && data.intent !== "ask") {
    setEngineBadge(null);
    if (sqlBlockEl) sqlBlockEl.hidden = true;  // 没有 SQL 就不摆一个空的 SQL 框
    renderReply(data, INTENT_SAMPLES);
    setState(`意图：${INTENT_LABEL[data.intent] || data.intent}`, "info");
    return;
  }

  setEngineBadge(data.engine);
  if (sqlBlockEl) sqlBlockEl.hidden = false;
  renderAttempts(data.attempts);

  if (!data.ok) {
    setState(`失败：${data.error || "未知错误"}`, "error");
    sqlEl.innerHTML = data.sql ? highlightSql(data.sql) : "—";
    return;
  }

  // Step 3：单 SQL 自动结论（只有显式开启且有 answer 时才显示）
  if (data.answer) {
    answerEl.innerHTML = `<div class="analysis-answer"><h3>结论</h3>` +
      `<div class="analysis-text">${formatAnswer(data.answer)}</div></div>`;
  } else if (data.conclusion_error) {
    answerEl.innerHTML =
      `<div class="analysis-note">结论未生成：${escapeHtml(data.conclusion_error)}</div>`;
  }

  const ctx = data.context || {};
  renderTags($("#ctx-metrics"), ctx.metrics, "指标");
  renderTags($("#ctx-tables"), ctx.tables, "表");
  renderTags($("#ctx-columns"), ctx.columns, "列");
  renderTable(data.columns, data.rows);
  sqlEl.innerHTML = highlightSql(data.sql);
  const tries = (data.attempts || []).length;
  // M4：服务端掩码过的列在这里点出来，避免把 138****0047 当成真实数据
  const masked = (data.masked_columns || []).length
    ? ` · 已掩码：${data.masked_columns.join(", ")}`
    : "";
  const cUsage = data.conclusion_usage
    && (data.conclusion_usage.prompt_tokens || data.conclusion_usage.completion_tokens)
    ? ` · 结论 token ${data.conclusion_usage.prompt_tokens || 0}+${data.conclusion_usage.completion_tokens || 0}`
    : "";
  metaEl.textContent =
    `${data.rows.length} 行 · ${data.elapsed_ms} ms${masked}${tries > 1 ? ` · 第 ${tries} 次尝试成功` : ""}${cUsage}`;
  setState("完成", "ok");
}

async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || String(res.status));
  return data;
}

async function run(question) {
  const q = (question ?? $("#q").value).trim();
  if (!q) {
    setState("请输入问题", "error");
    return;
  }
  $("#q").value = q;
  btnRun.disabled = true;
  setState("生成中…", "loading");
  clearResult();

  try {
    const data = await postJson("/api/ask", {
      question: q,
      role,
      ...(sessionId ? { session_id: sessionId } : {}),
      ...(modeEl && modeEl.checked ? { mode: "analysis" } : {}),
      ...(conclusionEl && conclusionEl.checked ? { conclusion: true } : {}),
    });
    renderResult(data);
  } catch (err) {
    setState(`请求失败：${err.message}`, "error");
  } finally {
    btnRun.disabled = false;
  }
}

// M6 演示：没配 LLM key 时规则生成器不会写错列，页面上看不到修复过程；
// 这里用服务端的脚本化生成器复现"写错列名 → 带报错改对"，不写会话、不参与评测。
// 注意：两条 SQL 是写死的（engine=custom），既不是规则生成，也不是模型生成。
async function runDemoRepair() {
  btnDemo.disabled = true;
  sessionId = "";  // 演示不写会话，也不该让下一句继承演示前的旧上下文
  setState("演示：固定 SQL，不调模型…", "loading");
  clearResult();
  try {
    const data = await postJson("/api/demo/repair", { role });
    $("#q").value = data.question || "";
    renderResult(data);
  } catch (err) {
    setState(`请求失败：${err.message}`, "error");
  } finally {
    btnDemo.disabled = false;
  }
}

function resetView(stateText) {
  sessionId = "";
  clearResult();
  if (emptyStateEl) emptyStateEl.hidden = false;
  if (stateText) setState(stateText, "info");
}

function syncRole() {
  role = roleEl.value;
  $("#link-schema").href = `/api/schema?role=${encodeURIComponent(role)}`;
}

// 分析模式已有综合结论；两者不叠加，开启分析时禁用自动结论
function syncModeOptions() {
  if (!modeEl || !conclusionEl) return;
  const analysis = modeEl.checked;
  conclusionEl.disabled = analysis;
  if (analysis) conclusionEl.checked = false;
}

modeEl.addEventListener("change", syncModeOptions);
syncModeOptions();

btnRun.addEventListener("click", () => run());

// M6：演示自我修复（服务端脚本化生成器，离线可复现）
btnDemo.addEventListener("click", () => runDemoRepair());

// M3：清空会话上下文，下一句当新问题（否则连点两个示例会互相继承槽位）
$("#btn-new").addEventListener("click", () => resetView("已开启新会话"));

// M4：换角色 = 换可见列，顺手清掉上文，避免拿 analyst 的上下文去看 viewer 的结果
roleEl.addEventListener("change", () => {
  syncRole();
  resetView(`角色：${roleEl.options[roleEl.selectedIndex].text}`);
});

syncRole();
loadLastLlmEval();

$("#q").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run();
});
$("#btn-copy").addEventListener("click", async () => {
  const sql = sqlEl.textContent;
  if (!sql || sql === "—") return;
  await navigator.clipboard.writeText(sql);
  $("#btn-copy").textContent = "已复制";
  setTimeout(() => ($("#btn-copy").textContent = "复制"), 1200);
});
$("#btn-eval").addEventListener("click", async () => {
  setState("评测中…", "loading");
  try {
    const res = await fetch("/api/eval");
    const s = await res.json();
    const asks = s.ask_total ?? s.total;
    // 「跑离线基线」= 服务端 run() 默认 engine=rule：离线、2ms，是 CI 回归基线。
    // 这里不调模型；模型成绩看 artifact（loadLastLlmEval）或 CLI。
    const engineLabel = s.engine === "llm" ? "模型评测" : "离线规则基线";
    setState(
      `${engineLabel}（engine=${s.engine}）${s.total} 条（问数 ${asks}）：执行一致率 ${(s.exec_rate * 100).toFixed(1)}%（${s.exec_match}/${asks}）` +
        ` · 意图 ${(s.intent_rate * 100).toFixed(1)}% · 多轮 ${(s.multi_turn_rate * 100).toFixed(1)}%` +
        `（${s.multi_turn_match}/${s.multi_turn_total}）· 平均 ${s.avg_ms} ms`,
      "ok"
    );
  } catch (e) {
    setState(`评测失败：${e.message}`, "error");
  }
});

// 示例按钮由这里渲染，模板里只留容器
SAMPLES.forEach((s) => {
  const btn = document.createElement("button");
  btn.className = "chip";
  btn.textContent = s;
  btn.addEventListener("click", () => run(s));
  document.querySelector(".samples").appendChild(btn);
});
