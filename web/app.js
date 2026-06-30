/* AI 治理審查小幫手 — 前端（vanilla JS，移植自 Claude Design .dc.html 的 DCLogic）。
   三步驟：上傳送件包 → 確認自動分類 → 審查報告。狀態驅動整頁重繪。
   後端：POST /api/classify、POST /api/review。資料全程地端，無外部請求。 */

(() => {
  "use strict";

  // ── 常數（對應設計的 SEV / 步驟標籤 / 送件包清單）──────────────────
  const SEV = {
    error: { fg: "var(--err-fg)", bg: "var(--err-bg)", border: "var(--err-border)", icon: "!", label: "錯誤" },
    warn:  { fg: "var(--warn-fg)", bg: "var(--warn-bg)", border: "var(--warn-border)", icon: "?", label: "提醒" },
    info:  { fg: "var(--info-fg)", bg: "var(--info-bg)", border: "var(--info-border)", icon: "i", label: "紀錄" },
  };
  const SEV_ORDER = { error: 0, warn: 1, info: 2 };
  // 固有風險分級 → tile 配色（含邊框 token，與其他 tile 一致）：
  // 高/中沿用 error/warn severity 色、低用通過(ok)綠，未知分級退中性色（不誤判為低風險）。
  function riskColors(grade) {
    if (grade === "高") return SEV.error;
    if (grade === "中") return SEV.warn;
    if (grade === "低") return { bg: "var(--ok-bg)", fg: "var(--ok)", border: "var(--ok-bg)" };
    return SEV.info;
  }
  const STEP_LABELS = ["上傳送件包", "確認分類", "審查報告"];
  const CHECKLIST = [
    { tag: "F1", name: "F01 系統資訊表", note: "系統基本資訊與必填欄位" },
    { tag: "F2", name: "F02 風險評鑑", note: "固有風險計分與分級" },
    { tag: "F3", name: "F03 上線檢核表", note: "上線前檢核項目" },
    { tag: "＋", name: "佐證文件", note: "模型卡、測試報告等（非 Excel）" },
  ];

  // ── 狀態 ───────────────────────────────────────────────────────
  const state = {
    step: 1,
    files: [],          // 真實 File 物件
    classify: null,     // /api/classify 回應 { kinds, files:[...] }
    kinds: {},          // index -> 使用者選定 kind（覆寫預設）
    review: null,       // /api/review 回應
    filter: "info",     // 預設停在【紀錄】分頁（彙整表所在）
    open: {},           // finding id -> bool
    reviewing: false,
    progress: null,     // { label, pct, detail } — SSE 串流的階段進度
    error: null,
  };

  // 進度條階段權重：把後端 (stage, done/total) 事件換算成 0–100% 的單調進度。
  // LLM 判讀是總等待時間大宗，分到最大的尾段；未啟用 LLM 時規則完成後直接由 done 收 100%。
  const PROGRESS_BANDS = {
    upload: [0, 10],
    parse:  [10, 35],
    rules:  [35, 70],
    llm:    [70, 98],
  };

  function progressPct(ev) {
    const band = PROGRESS_BANDS[ev.stage];
    if (!band) return null;
    const total = ev.total > 0 ? ev.total : 1;
    const frac = Math.max(0, Math.min(1, (ev.done || 0) / total));
    return Math.round(band[0] + (band[1] - band[0]) * frac);
  }

  const app = document.getElementById("app");
  const fileInput = document.getElementById("file-input");

  // ── 工具 ───────────────────────────────────────────────────────
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
  }
  function fileIcon(name) {
    const n = name.toLowerCase();
    if (n.endsWith(".pdf")) return "📕";
    if (n.endsWith(".docx") || n.endsWith(".doc")) return "📘";
    return "📗";
  }

  // 極簡 GFM 表格渲染（給 F03 LLM 佐證彙整表的多行訊息）；非表格純文字保留換行。
  function renderMessage(msg) {
    if (!msg) return "";
    const lines = String(msg).split("\n");
    const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
    // 僅把「以 | 開頭」的列視為表格列（我們的彙整表必有前導 |，且儲存格已去除 |），
    // 避免含管線符號的一般說明文字被誤判為表格。
    const isRow = (l) => l.trimStart().startsWith("|");
    const splitRow = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    let html = "";
    let buf = [];
    const flush = () => {
      if (buf.length) { html += `<p class="fmsg">${buf.map(esc).join("<br>")}</p>`; buf = []; }
    };
    let i = 0;
    while (i < lines.length) {
      if (i + 1 < lines.length && isRow(lines[i]) && isSep(lines[i + 1])) {
        flush();
        const header = splitRow(lines[i]);
        i += 2;
        const rows = [];
        while (i < lines.length && isRow(lines[i]) && !isSep(lines[i])) { rows.push(splitRow(lines[i])); i++; }
        html += "<table class=\"md-table\"><thead><tr>" +
          header.map((h) => `<th>${esc(h)}</th>`).join("") + "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + r.map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
      } else if (lines[i].trim() === "") {
        flush(); i++;
      } else {
        buf.push(lines[i]); i++;
      }
    }
    flush();
    return html;
  }

  async function postForm(url, formData) {
    const res = await fetch(url, { method: "POST", body: formData });
    if (!res.ok) {
      let detail = `伺服器錯誤（${res.status}）`;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function reachableStep() {
    if (state.review) return 3;
    if (state.classify) return 2;
    return 1;
  }

  // ── 動作 ───────────────────────────────────────────────────────
  function onFilesChosen(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    state.files = files;
    state.classify = null;
    state.kinds = {};
    state.error = null;
    render();
  }

  async function goClassify() {
    if (!state.files.length) return;
    state.error = null;
    const fd = new FormData();
    state.files.forEach((f) => fd.append("files", f, f.name));
    try {
      const data = await postForm("/api/classify", fd);
      state.classify = data;
      state.kinds = {};
      data.files.forEach((f) => { state.kinds[f.index] = f.kind; });
      state.step = 2;
    } catch (e) {
      state.error = e.message;
    }
    render();
  }

  async function startReview() {
    state.error = null;
    state.reviewing = true;
    state.progress = null;
    render();
    const fd = new FormData();
    state.files.forEach((f) => fd.append("files", f, f.name));
    // 依 classify.files 順序（與 state.files 上傳順序一致）逐檔取確認後的 kind，
    // 一律以伺服器指派的 f.index 為鍵，與 goClassify / set-kind 的寫入鍵保持一致。
    const confirmed = state.classify.files.map((f) =>
      state.kinds[f.index] != null ? state.kinds[f.index] : f.kind);
    fd.append("kinds", JSON.stringify(confirmed));
    try {
      // SSE 串流：邊審查邊推進度，最後一筆事件帶完整報告。
      const res = await fetch("/api/review/stream", { method: "POST", body: fd });
      if (!res.ok || !res.body) {
        let detail = `伺服器錯誤（${res.status}）`;
        try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      await consumeReviewStream(res.body);  // 收到 done 事件時套用報告並跳第 3 步
    } catch (e) {
      state.error = e.message;
      state.reviewing = false;
      state.progress = null;
      render();
    }
  }

  // 讀 text/event-stream：逐 frame 解析進度事件，更新進度條；done 套報告、error 拋出。
  async function consumeReviewStream(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let maxPct = 0;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
          if (ev.stage === "error") throw new Error(ev.message || "審查失敗。");
          if (ev.stage === "done") { applyReport(ev.report); return; }
          const pct = progressPct(ev);
          // 僅在百分比實際前進時更新＋重繪（單調遞增、永不回退），避免同 pct 的冗餘整頁重繪。
          if (pct != null && pct > maxPct) {
            maxPct = pct;
            const detail = ev.total ? `${ev.done}/${ev.total}` : "";
            state.progress = { label: ev.label || "審查中", pct: maxPct, detail };
            render();
          }
        }
      }
      throw new Error("審查串流中斷，未取得完整報告。");
    } finally {
      reader.cancel().catch(() => { /* 已關閉/已取消 → 忽略 */ });
    }
  }

  // 套用最終報告 → 跳第 3 步（單次/串流共用；錯誤項預設展開對應設計 componentDidMount）。
  function applyReport(data) {
    state.review = data;
    const open = {};
    data.findings.forEach((f) => { if (f.severity === "error") open[f.id] = true; });
    state.open = open;
    // 預設停在【紀錄】（彙整表所在）；但若無任何紀錄項，退回【全部】，避免落地畫面空白而藏住錯誤。
    state.filter = data.info_count > 0 ? "info" : "all";
    state.reviewing = false;
    state.progress = null;
    state.step = 3;
    render();
  }

  function reset() {
    state.step = 1;
    state.files = [];
    state.classify = null;
    state.kinds = {};
    state.review = null;
    state.filter = "info";  // 與初始預設一致（落地分頁在 startReview 依紀錄數再決定）
    state.open = {};
    state.error = null;
    fileInput.value = "";
    render();
  }

  function gotoStep(n) {
    if (n <= reachableStep()) { state.step = n; render(); }
  }

  function downloadMarkdown() {
    if (!state.review) return;
    const subject = (state.review.subject || "未標示").replace(/[\\/]/g, "_");
    const blob = new Blob([state.review.markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `送件包_審查報告_${subject}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }

  // ── 渲染：頂部（橫幅 / 頁首 / 步驟器）────────────────────────────
  function renderHeader() {
    const cur = state.step;
    const reach = reachableStep();
    const steps = STEP_LABELS.map((label, i) => {
      const n = i + 1;
      const cls = n === cur ? "cur" : n < cur ? "done" : "todo";
      const can = n <= reach;
      const num = n < cur ? "✓" : String(n);
      const sep = n < 3 ? `<span class="gv-step-sep">›</span>` : "";
      return `<button class="gv-step ${cls}${can ? "" : " disabled"}" data-action="goto" data-step="${n}">
        <span class="num">${num}</span><span>${esc(label)}</span></button>${sep}`;
    }).join("");

    return `<div class="gv-sticky">
      <div class="gv-banner">
        <span class="dot">i</span>
        <div class="txt"><strong>AI 初判草稿，需治理人員與三遵人工覆核</strong>
          <span>　·　最終判定權不在 AI　·　資料全程地端，不外送雲端</span></div>
      </div>
      <div class="gv-header">
        <div class="brand">
          <span class="logo">🛡️</span>
          <div><div class="title">AI 治理審查小幫手</div>
            <div class="sub">批次上傳自動分類 · 送件包初步審查</div></div>
        </div>
        <div class="pill"><span class="dot"></span>地端執行 · 資料不外送</div>
      </div>
      <div class="gv-steps">${steps}</div>
    </div>`;
  }

  function renderError() {
    return state.error ? `<div class="gv-error">⚠ ${esc(state.error)}</div>` : "";
  }

  // ── 渲染：Step 1 上傳 ──────────────────────────────────────────
  function renderStep1() {
    const loaded = state.files.length > 0;
    let body;
    if (!loaded) {
      body = `<div class="gv-upload-grid">
        <button class="gv-drop" id="dropzone" data-action="pick">
          <span class="ic">⤓</span>
          <div><div class="big">拖入送件包所有檔案</div>
            <div class="small">支援 F01/F02/F03 與佐證（可多檔）· 或點此選擇檔案</div></div>
          <span class="cta">選擇檔案</span>
        </button>
        <div class="gv-aside"><h3>送件包應包含</h3>
          ${CHECKLIST.map((c) => `<div class="item"><span class="tag">${esc(c.tag)}</span>
            <div><div class="name">${esc(c.name)}</div><div class="note">${esc(c.note)}</div></div></div>`).join("")}
        </div></div>`;
    } else {
      const rows = state.files.map((f) => `<div class="gv-row">
        <span class="ic">${fileIcon(f.name)}</span>
        <div class="meta"><div class="fname">${esc(f.name)}</div></div>
        <span class="fsize">${fmtSize(f.size)}</span></div>`).join("");
      body = `<div class="gv-filelist">
        <div class="head"><span>已選擇 ${state.files.length} 個檔案</span>
          <button class="btn-small" data-action="reset">重新選擇</button></div>
        ${rows}</div>
        <div class="row-end"><button class="btn-primary" data-action="classify">下一步：確認分類　→</button></div>`;
    }
    return `<div>
      <h1 class="gv-h1">上傳送件包</h1>
      <p class="gv-lead">一次拖入提案單位提交的所有檔案，系統會自動辨識 F01 / F02 / F03 與佐證文件。你可以在審查前確認或修正每個檔案的判定。</p>
      ${renderError()}${body}</div>`;
  }

  // ── 渲染：Step 2 分類 ──────────────────────────────────────────
  function renderStep2() {
    const cf = state.classify;
    const opts = cf.kinds;
    const counts = { f01: 0, f02: 0, f03: 0, supporting: 0, ignore: 0 };
    cf.files.forEach((f) => { const k = state.kinds[f.index] ?? f.kind; counts[k] = (counts[k] || 0) + 1; });
    const flagged = cf.files.filter((f) => f.flag).length;

    const chip = (label, fg, bg, border) =>
      `<span class="gv-chip" style="color:${fg};background:${bg};border-color:${border};">${esc(label)}</span>`;
    const chips = [
      chip(`共 ${cf.files.length} 個檔案`, "var(--text)", "var(--surface)", "var(--border)"),
      chip(`表單 ${counts.f01 + counts.f02 + counts.f03} 份`, "var(--accent)", "var(--accent-soft)", "var(--accent-soft)"),
      chip(`佐證 ${counts.supporting} 份`, "var(--muted)", "var(--bg)", "var(--border)"),
    ];
    if (flagged) chips.push(chip(`需確認 ${flagged} 項`, "var(--warn-fg)", "var(--warn-bg)", "var(--warn-border)"));

    const optionHtml = (selected) =>
      opts.map((o) => `<option value="${esc(o.value)}"${o.value === selected ? " selected" : ""}>${esc(o.label)}</option>`).join("");

    const rows = cf.files.map((f) => {
      const kind = state.kinds[f.index] ?? f.kind;
      const flag = f.flag === "dup" ? "重複" : f.flag === "unknown" ? "無法辨識" : "";
      const flagHtml = flag ? `<span class="gv-flag">${flag}</span>` : "";
      return `<div class="gv-classify-row${f.flag ? " flagged" : ""}">
        <div class="line">
          <span class="ic">${fileIcon(f.filename)}</span>
          <div class="body"><div class="top"><span class="fname">${esc(f.filename)}</span>${flagHtml}</div>
            <div class="reason">${esc(f.reason)}</div></div>
          <select class="gv-select" data-action="set-kind" data-index="${f.index}">${optionHtml(kind)}</select>
        </div></div>`;
    }).join("");

    return `<div>
      <h1 class="gv-h1">① 自動分類<span class="light">（可修正）</span></h1>
      <p class="gv-lead">分類依工作表名稱判定，屬確定性規則。請逐一確認；標記為「重複」或「無法辨識」的項目特別需要你的判斷。</p>
      ${renderError()}
      <div class="gv-chips">${chips.join("")}</div>
      <div class="gv-filelist">${rows}</div>
      <div class="row-split">
        <button class="btn-ghost" data-action="goto" data-step="1">←　返回上傳</button>
        <button class="btn-primary" data-action="start-review">② 開始審查　→</button>
      </div></div>`;
  }

  // ── 渲染：Step 3 報告 ──────────────────────────────────────────
  function renderStep3() {
    const r = state.review;
    const passed = r.passed;
    const barColor = passed ? "var(--ok)" : "var(--err-fg)";

    const tile = (n, label, sevKey, big) => {
      const s = SEV[sevKey];
      const outline = big && n > 0 ? `outline-color:${s.fg};` : "";
      return `<div class="gv-tile${big && n > 0 ? " big" : ""}" style="background:${s.bg};color:${s.fg};border-color:${s.border};${outline}">
        <div class="n">${n}</div><div class="l">${esc(label)}</div></div>`;
    };

    // 固有風險分級/分數 tile（來源：F02 第一頁 AI系統固有風險分級評估表）；無 F02 則不渲染。
    const riskTile = (() => {
      if (!r.risk_grade) return "";
      const c = riskColors(r.risk_grade);
      const pct = r.risk_score != null ? ` · ${r.risk_score.toFixed(0)}%` : "";
      return `<div class="gv-tile" style="background:${c.bg};color:${c.fg};border-color:${c.border};min-width:120px;">
        <div class="n">${esc(r.risk_grade)}</div><div class="l">固有風險${esc(pct)}</div></div>`;
    })();

    const filterDefs = [
      { key: "info", label: `紀錄 ${r.info_count}` },
      { key: "error", label: `錯誤 ${r.error_count}` },
      { key: "warn", label: `提醒 ${r.warn_count}` },
      { key: "all", label: `全部 ${r.findings.length}` },
    ];
    const filters = filterDefs.map((f) =>
      `<button class="gv-filter${state.filter === f.key ? " active" : ""}" data-action="filter" data-filter="${f.key}">${esc(f.label)}</button>`).join("");

    const sorted = [...r.findings].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);
    const shown = state.filter === "all" ? sorted : sorted.filter((f) => f.severity === state.filter);

    const cards = shown.map((f) => {
      const s = SEV[f.severity];
      const open = !!state.open[f.id];
      const hasCompare = f.expected != null || f.actual != null;
      const loc = f.location ? `<span class="floc">${esc(f.location)}</span>` : "";
      let bodyHtml = "";
      if (open) {
        let compare = "";
        if (hasCompare) {
          compare = `<div class="gv-compare">
            <div class="col expected"><div class="k">期望</div><div class="v">${esc(f.expected || "—")}</div></div>
            <div class="col" style="background:${s.bg};border-color:${s.border};"><div class="k">實際</div>
              <div class="v" style="color:${s.fg};">${esc(f.actual || "—")}</div></div></div>`;
        }
        bodyHtml = `<div class="fbody">${compare}${renderMessage(f.message)}
          <div class="fcode-line"><span class="fcode">${esc(f.code)}</span>
            <span class="fhint">· 需人工覆核</span></div></div>`;
      }
      return `<div class="gv-finding ${f.severity === "error" ? "err" : ""}${open ? " open" : ""}">
        <button class="fbtn" data-action="toggle" data-id="${esc(f.id)}">
          <span class="fbadge" style="background:${s.bg};color:${s.fg};">${s.icon}</span>
          <span class="fsev" style="color:${s.fg};">${esc(s.label)}</span>
          <span class="ftitle">${esc(f.title)}</span>${loc}
          <span class="fcaret">⌄</span></button>${bodyHtml}</div>`;
    }).join("");

    return `<div>
      <div class="gv-summary" style="--bar:${barColor};">
        <div class="top">
          <div class="lead">
            <span class="badge" style="background:${passed ? "var(--ok-bg)" : "var(--err-bg)"};color:${passed ? "var(--ok)" : "var(--err-fg)"};">${passed ? "✓" : "✕"}</span>
            <div><div class="title" style="color:${passed ? "var(--ok)" : "var(--err-fg)"};">${passed ? "規則通過" : "待處理"}</div>
              <div class="subline">受審：${esc(r.subject || "未標示")}　·　${esc(r.form_type)}</div></div>
          </div>
          <div class="gv-tiles">
            ${riskTile}
            ${tile(r.error_count, "錯誤", "error", true)}
            ${tile(r.warn_count, "提醒", "warn", false)}
            ${tile(r.info_count, "通過紀錄", "info", false)}
          </div>
        </div>
        <div class="foot"><span class="note spacer">⚠ 本報告為 AI 初判，每項皆需人工覆核</span></div>
      </div>
      ${renderError()}
      <div class="gv-filters">${filters}</div>
      <div class="gv-findings">${cards}</div>
      <div class="row-split" style="padding-top:22px;border-top:1px solid var(--border);">
        <button class="btn-ghost" data-action="reset">↻　審查新的送件包</button>
        <button class="btn-download" data-action="download">⬇ 下載 Markdown 報告</button>
      </div></div>`;
  }

  function renderOverlay() {
    if (!state.reviewing) return "";
    const p = state.progress;
    // 尚未收到首個事件 → 退回 spinner 文案，避免進度條空白閃爍。
    if (!p) {
      return `<div class="gv-overlay"><div class="box">
        <div class="spin"></div><div class="t">審查中…</div>
        <div class="s">缺件 · F01 必填 · F02 規則 · 跨表一致性</div></div></div>`;
    }
    return `<div class="gv-overlay"><div class="box">
      <div class="t">審查中…${esc(p.label)}</div>
      <div class="bar"><i style="width:${p.pct}%"></i></div>
      <div class="s">${p.pct}%${p.detail ? " · " + esc(p.detail) : ""}</div></div></div>`;
  }

  // ── 主渲染 ─────────────────────────────────────────────────────
  function render() {
    let content = "";
    if (state.step === 1) content = renderStep1();
    else if (state.step === 2) content = renderStep2();
    else if (state.step === 3) content = renderStep3();
    app.innerHTML = `${renderHeader()}<div class="gv-main">${content}</div>${renderOverlay()}`;
    bindDynamic();
  }

  // ── 事件（委派）────────────────────────────────────────────────
  function bindDynamic() {
    const dz = document.getElementById("dropzone");
    if (dz) {
      ["dragover", "dragenter"].forEach((ev) =>
        dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); }));
      ["dragleave", "dragend"].forEach((ev) =>
        dz.addEventListener(ev, () => dz.classList.remove("dragover")));
      dz.addEventListener("drop", (e) => {
        e.preventDefault(); dz.classList.remove("dragover");
        if (e.dataTransfer && e.dataTransfer.files) onFilesChosen(e.dataTransfer.files);
      });
    }
  }

  app.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    if (action === "pick") fileInput.click();
    else if (action === "reset") reset();
    else if (action === "classify") goClassify();
    else if (action === "start-review") startReview();
    else if (action === "goto") gotoStep(Number(el.dataset.step));
    else if (action === "toggle") { const id = el.dataset.id; state.open[id] = !state.open[id]; render(); }
    else if (action === "filter") { state.filter = el.dataset.filter; render(); }
    else if (action === "download") downloadMarkdown();
  });

  app.addEventListener("change", (e) => {
    const el = e.target.closest("[data-action='set-kind']");
    if (!el) return;
    state.kinds[Number(el.dataset.index)] = el.value;
    render();
  });

  fileInput.addEventListener("change", (e) => onFilesChosen(e.target.files));

  render();
})();
