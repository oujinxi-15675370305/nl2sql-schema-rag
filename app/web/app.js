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

function setState(text, kind = "") {
  stateEl.className = "state " + kind;
  stateEl.textContent = text;
}

function renderTags(el, items, kind) {
  if (!items || !items.length) {
    el.innerHTML = `<span class="muted">${kind} —</span>`;
    return;
  }
  el.innerHTML = items
    .map((it) => `<span class="tag">${escapeHtml(it.key)}${it.table ? " · " + escapeHtml(it.table) : ""}</span>`)
    .join("");
}

function clearTags() {
  CONTEXT_ROWS.forEach(([sel, label]) => {
    $(sel).innerHTML = `<span class="muted">${label} —</span>`;
  });
}

function renderTable(columns, rows) {
  if (!rows || !rows.length) {
    tableWrap.innerHTML = `<div class="empty">没有数据</div>`;
    return;
  }
  const head = `<thead><tr>${columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const body = rows
    .map(
      (r) =>
        `<tr>${r
          .map((v) => `<td>${v === null ? "NULL" : typeof v === "number" ? Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : escapeHtml(v)}</td>`)
          .join("")}</tr>`
    )
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
  clearTags();
}

// ask 与 demo 端点返回同一份结构，渲染只写这一套
function renderResult(data) {
  // 服务端可能新开一个会话，后续请求带上它（演示接口不建会话，回空就不动）
  if (data.session_id) sessionId = data.session_id;
  // M3：告诉用户这句被补了什么，不然"那上个月呢"的结果看着像凭空来的
  if (data.inherited_slots && data.inherited_slots.length && data.rewritten_question) {
    rewriteEl.textContent = `承接上文 → ${data.rewritten_question}`;
  }

  // M2：非 ask 不生成 SQL，页面显示意图话术而不是"失败"
  if (data.intent && data.intent !== "ask") {
    renderReply(data, INTENT_SAMPLES);
    setState(`意图：${INTENT_LABEL[data.intent] || data.intent}`, "info");
    return;
  }

  renderAttempts(data.attempts);

  if (!data.ok) {
    setState(`失败：${data.error || "未知错误"}`, "error");
    sqlEl.textContent = data.sql || "—";
    return;
  }

  const ctx = data.context || {};
  renderTags($("#ctx-metrics"), ctx.metrics, "指标");
  renderTags($("#ctx-tables"), ctx.tables, "表");
  renderTags($("#ctx-columns"), ctx.columns, "列");
  renderTable(data.columns, data.rows);
  sqlEl.textContent = data.sql;
  const tries = (data.attempts || []).length;
  // M4：服务端掩码过的列在这里点出来，避免把 138****0047 当成真实数据
  const masked = (data.masked_columns || []).length
    ? ` · 已掩码：${data.masked_columns.join(", ")}`
    : "";
  const engine = ENGINE_LABEL[data.engine] || data.engine || "未知来源";
  metaEl.textContent =
    `${engine} · ${data.rows.length} 行 · ${data.elapsed_ms} ms${masked}${tries > 1 ? ` · 第 ${tries} 次尝试成功` : ""}`;
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
    // 页面上的「跑评测」走服务端 run() 默认引擎 = rule：离线、2ms，是 CI 回归基线。
    // 模型成绩需要 CLI：python -m app.evals --engine llm（约 1 分钟），不要看混。
    const engineLabel = s.engine === "llm" ? "模型" : "离线规则基线";
    setState(
      `评测（${engineLabel}）${s.total} 条（问数 ${asks}）：执行一致率 ${(s.exec_rate * 100).toFixed(1)}%（${s.exec_match}/${asks}）` +
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
