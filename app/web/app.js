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
};
const ENGINE_CLASS = { llm: "engine-llm", rule: "engine-rule", custom: "engine-custom" };

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

function renderTable(columns, rows) {
  if (!rows || !rows.length) {
    tableWrap.innerHTML = `<div class="empty">没有数据</div>`;
    return;
  }
  const numeric = columns.map((_, index) =>
    rows.every((row) => row[index] === null || typeof row[index] === "number")
  );
  const head = `<thead><tr>${columns
    .map((c, index) => `<th${numeric[index] ? ' class="num"' : ""}>${escapeHtml(c)}</th>`)
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
  tableWrap.innerHTML = `<table>${head}<tbody>${body}</tbody></table>`;
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

function clearResult() {
  metaEl.textContent = "";
  sqlEl.textContent = "—";
  rewriteEl.textContent = "";
  tableWrap.innerHTML = "";
  answerEl.innerHTML = "";
  attemptsEl.innerHTML = "";
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
  metaEl.textContent =
    `${data.rows.length} 行 · ${data.elapsed_ms} ms${masked}${tries > 1 ? ` · 第 ${tries} 次尝试成功` : ""}`;
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
