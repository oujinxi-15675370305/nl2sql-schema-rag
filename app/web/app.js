const $ = (sel) => document.querySelector(sel);

const stateEl = $("#state");
const metaEl = $("#meta");
const tableWrap = $("#table-wrap");
const sqlEl = $("#sql");
const btnRun = $("#btn-run");
const answerEl = $("#answer");
const attemptsEl = $("#attempts");
const rewriteEl = $("#rewrite");

// M3：会话 id 由服务端下发；带上它才能接上文（"那上个月呢"）
let sessionId = "";

const SAMPLES = [
  "总GMV是多少",
  "各地区的GMV",
  "各品类的销售额",
  "今年各月份GMV",
  "各商品销量排名",
  "退款率是多少",
  "华东地区的客单价",
];

const INTENT_LABEL = {
  chat: "闲聊",
  clarify: "需要补充信息",
  out_of_scope: "超出数据范围",
  unsafe: "越权 / 写操作",
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

async function run(question) {
  const q = (question ?? $("#q").value).trim();
  if (!q) {
    setState("请输入问题", "error");
    return;
  }
  $("#q").value = q;
  btnRun.disabled = true;
  setState("生成中…", "loading");
  metaEl.textContent = "";
  sqlEl.textContent = "—";
  rewriteEl.textContent = "";
  tableWrap.innerHTML = "";
  answerEl.innerHTML = "";
  attemptsEl.innerHTML = "";
  clearTags();

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sessionId ? { question: q, session_id: sessionId } : { question: q }),
    });
    const data = await res.json();

    if (!res.ok) {
      setState(`请求失败：${data.error || res.status}`, "error");
      return;
    }

    // 服务端可能新开一个会话，后续请求带上它
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
    metaEl.textContent = `${data.rows.length} 行 · ${data.elapsed_ms} ms${tries > 1 ? ` · 第 ${tries} 次尝试成功` : ""}`;
    setState("完成", "ok");
  } catch (err) {
    setState(`请求失败：${err.message}`, "error");
  } finally {
    btnRun.disabled = false;
  }
}

btnRun.addEventListener("click", () => run());

// M3：清空会话上下文，下一句当新问题（否则连点两个示例会互相继承槽位）
$("#btn-new").addEventListener("click", () => {
  sessionId = "";
  rewriteEl.textContent = "";
  answerEl.innerHTML = "";
  attemptsEl.innerHTML = "";
  sqlEl.textContent = "—";
  tableWrap.innerHTML = "";
  metaEl.textContent = "";
  clearTags();
  setState("已开启新会话", "info");
});

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
    setState(
      `评测 ${s.total} 条：执行一致率 ${(s.exec_rate * 100).toFixed(1)}%（${s.exec_match}/${s.total}），平均 ${s.avg_ms} ms`,
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
