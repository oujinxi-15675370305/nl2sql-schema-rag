const $ = (sel) => document.querySelector(sel);

const stateEl = $("#state");
const metaEl = $("#meta");
const tableWrap = $("#table-wrap");
const sqlEl = $("#sql");
const btnRun = $("#btn-run");

const SAMPLES = [
  "总GMV是多少",
  "各地区的GMV",
  "各品类的销售额",
  "今年各月份GMV",
  "各商品销量排名",
  "退款率是多少",
  "华东地区的客单价",
];

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
    .map((it) => `<span class="tag">${it.key}${it.table ? " · " + it.table : ""}</span>`)
    .join("");
}

function renderTable(columns, rows) {
  if (!rows || !rows.length) {
    tableWrap.innerHTML = `<div class="empty">没有数据</div>`;
    return;
  }
  const head = `<thead><tr>${columns.map((c) => `<th>${c}</th>`).join("")}</tr></thead>`;
  const body = rows
    .map(
      (r) =>
        `<tr>${r
          .map((v) => `<td>${v === null ? "NULL" : typeof v === "number" ? Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : v}</td>`)
          .join("")}</tr>`
    )
    .join("");
  tableWrap.innerHTML = `<table>${head}<tbody>${body}</tbody></table>`;
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
  tableWrap.innerHTML = "";

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      setState(`失败：${data.error || res.status}`, "error");
      sqlEl.textContent = data.sql || "—";
      return;
    }
    const ctx = data.context || {};
    renderTags($("#ctx-metrics"), ctx.metrics, "指标");
    renderTags($("#ctx-tables"), ctx.tables, "表");
    renderTags($("#ctx-columns"), ctx.columns, "列");
    renderTable(data.columns, data.rows);
    sqlEl.textContent = data.sql;
    metaEl.textContent = `${data.rows.length} 行 · ${data.elapsed_ms} ms`;
    setState("完成", "ok");
  } catch (err) {
    setState(`请求失败：${err.message}`, "error");
  } finally {
    btnRun.disabled = false;
  }
}

btnRun.addEventListener("click", () => run());
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
