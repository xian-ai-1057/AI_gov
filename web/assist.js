/* AI 治理提案小幫手 — 提案單位端 AI 協填（vanilla JS，獨立於審查員端 app.js）。
   兩步驟：上傳佐證並勾選表單 → 填寫建議書（欄位 → 建議 → 出處 → 信心）。
   後端：POST /api/assist/suggest。不代填 Excel、不匯出 Excel；「已抄入」只存在本機瀏覽器。
   資料全程地端，無外部請求。 */

(() => {
  "use strict";

  // ── 常數 ───────────────────────────────────────────────────────
  const FORMS = [
    { id: "f01", tag: "F1", name: "F01 系統資訊表", note: "系統基本資訊與必填欄位" },
    { id: "f02", tag: "F2", name: "F02 風險評鑑", note: "固有風險題項（是／否）" },
    { id: "f03", tag: "F3", name: "F03 上線檢核表", note: "檢核項目的佐證說明" },
  ];
  const FORM_LABEL = Object.fromEntries(FORMS.map((form) => [form.id, form.name.split(" ")[0]]));
  const EVIDENCE = [
    { tag: "簽", name: "簽呈 / 核准文件", note: "系統名稱、單位、負責人、上線時程" },
    { tag: "簡", name: "簡報 / 提案說明", note: "用途、資料來源、模型與供應商" },
    { tag: "規", name: "規格書 / 測試報告", note: "效能、安全、監控與人工介入" },
    { tag: "信", name: "往來信件 / 會議紀錄", note: "決議與責任分工" },
  ];
  const MODE = {
    llm: { cls: "llm", label: "語意協填", note: "已由模型依佐證產出建議並驗證出處" },
    retrieval_only: { cls: "retrieval_only", label: "僅檢索候選", note: "語意判讀未啟用：只列出候選出處，建議欄留空" },
    unavailable: { cls: "unavailable", label: "無法產生", note: "欄位目錄或佐證不可用，無建議可列" },
  };
  const SEV = {
    error: { fg: "var(--err-fg)", bg: "var(--err-bg)", icon: "!", label: "錯誤" },
    warn: { fg: "var(--warn-fg)", bg: "var(--warn-bg)", icon: "?", label: "提醒" },
    info: { fg: "var(--info-fg)", bg: "var(--info-bg)", icon: "i", label: "紀錄" },
  };
  const UNCOVERED_TEXT = "—（文件未涵蓋，請自行填寫）";
  const STORAGE_PREFIX = "govcheck.assist.adopted.";

  // ── 狀態 ───────────────────────────────────────────────────────
  const state = {
    step: 1,
    files: [],                       // 真實 File 物件
    forms: { f01: true, f02: true, f03: true },
    useLlm: true,                    // 送 enable_llm；/api/readiness 說未就緒時預設關
    readiness: null,                 // /api/readiness 的 assist 區塊（只用來提示）
    result: null,                    // /api/assist/suggest 回應
    tab: null,                       // 目前表單分頁（f01/f02/f03）
    openCite: {},                    // rowKey -> 出處是否展開
    openNotice: {},                  // notice id -> 是否展開
    adopted: {},                     // rowKey -> 已抄入（同步到 localStorage）
    busy: false,
    error: null,
  };

  const app = document.getElementById("app");
  const fileInput = document.getElementById("file-input");
  const toastEl = document.getElementById("toast");

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
    if (n.endsWith(".pptx") || n.endsWith(".ppt")) return "📙";
    if (n.endsWith(".msg") || n.endsWith(".eml")) return "✉️";
    if (n.endsWith(".zip")) return "🗜️";
    return "📗";
  }

  const rowKey = (row) => `${row.form}:${row.field_id}`;
  const selectedForms = () => FORMS.map((form) => form.id).filter((id) => state.forms[id]);

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toastEl.hidden = true; }, 1900);
  }

  // 「已抄入」只是提案者自己的進度記號，存本機瀏覽器即可；任何儲存失敗都不影響頁面。
  function storageKey() {
    return state.result ? STORAGE_PREFIX + state.result.assist_id : null;
  }
  function loadAdopted() {
    const key = storageKey();
    if (!key) return {};
    try {
      const raw = window.localStorage.getItem(key);
      const parsed = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) { return {}; }
  }
  function saveAdopted() {
    const key = storageKey();
    if (!key) return;
    try { window.localStorage.setItem(key, JSON.stringify(state.adopted)); } catch (_) { /* 私密模式等 */ }
  }

  async function postForm(url, formData) {
    const res = await fetch(url, { method: "POST", body: formData });
    if (!res.ok) throw new Error(await responseDetail(res));
    return res.json();
  }

  async function responseDetail(res) {
    let detail = `伺服器錯誤（${res.status}）`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* 非 JSON 錯誤頁，沿用狀態碼 */ }
    return detail;
  }

  // ── 流程 ───────────────────────────────────────────────────────
  async function loadReadiness() {
    try {
      const res = await fetch("/api/readiness");
      if (!res.ok) return;
      const body = await res.json();
      state.readiness = body && body.assist ? body.assist : null;
      if (state.readiness && state.readiness.ready === false) state.useLlm = false;
    } catch (_) { state.readiness = null; }
    render();
  }

  function onFilesChosen(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    state.files = files;
    state.error = null;
    render();
  }

  function reset() {
    state.step = 1;
    state.files = [];
    state.result = null;
    state.tab = null;
    state.openCite = {};
    state.openNotice = {};
    state.adopted = {};
    state.busy = false;
    state.error = null;
    fileInput.value = "";
    render();
  }

  async function suggest() {
    if (state.busy) return;
    if (!state.files.length) { state.error = "請先選擇至少一份佐證文件。"; render(); return; }
    const forms = selectedForms();
    if (!forms.length) { state.error = "請至少勾選一份要協填的表單。"; render(); return; }
    state.error = null;
    state.busy = true;
    render();
    const fd = new FormData();
    state.files.forEach((file) => fd.append("files", file, file.name));
    fd.append("forms", forms.join(","));
    fd.append("enable_llm", state.useLlm ? "true" : "false");
    try {
      const data = await postForm("/api/assist/suggest", fd);
      applyResult(data);
    } catch (error) {
      state.error = error.message;
    }
    state.busy = false;
    render();
  }

  function applyResult(data) {
    state.result = data;
    state.openCite = {};
    state.openNotice = {};
    const present = (data.forms || []).filter((id) => FORM_LABEL[id]);
    state.tab = present[0] || null;
    state.adopted = loadAdopted();
    state.step = 2;
  }

  function downloadMarkdown() {
    if (!state.result || !state.result.markdown) return;
    const blob = new Blob([state.result.markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `填寫建議書_${String(state.result.assist_id || "assist").replace(/[\\/]/g, "_")}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }

  // ── 渲染：頂部 ─────────────────────────────────────────────────
  function renderHeader() {
    return `<div class="gv-sticky">
      <div class="gv-banner">
        <span class="dot">i</span>
        <div class="txt"><strong>AI 建議僅供參考，官方表單仍由您親自填寫、由單位主管核可</strong>
          <span>　·　最終責任在提案單位　·　資料全程地端</span></div>
      </div>
      <div class="gv-header">
        <div class="brand">
          <span class="logo">✍️</span>
          <div><div class="title">AI 治理提案小幫手</div>
            <div class="sub">提案單位端 · AI 協填</div></div>
        </div>
        <div class="pills">
          <a class="pill" href="/">審查員端</a>
          <div class="pill"><span class="dot"></span>地端執行 · 資料不外送</div>
        </div>
      </div>
    </div>`;
  }

  function renderError() {
    return state.error ? `<div class="gv-error" role="alert">⚠ ${esc(state.error)}</div>` : "";
  }

  // ── 渲染：Step 1 上傳佐證 + 勾選表單 ──────────────────────────
  function renderStep1() {
    const loaded = state.files.length > 0;
    let body;
    if (!loaded) {
      body = `<div class="gv-upload-grid">
        <button class="gv-drop" id="dropzone" data-action="pick">
          <span class="ic">⤓</span>
          <div><div class="big">拖入提案的佐證文件</div>
            <div class="small">簽呈、簡報、規格書、信件（可多檔）· 或點此選擇檔案</div></div>
          <span class="cta">選擇檔案</span>
        </button>
        <div class="gv-aside"><h3>可提供的佐證</h3>
          ${EVIDENCE.map((item) => `<div class="item"><span class="tag">${esc(item.tag)}</span>
            <div><div class="name">${esc(item.name)}</div><div class="note">${esc(item.note)}</div></div></div>`).join("")}
        </div></div>`;
    } else {
      const rows = state.files.map((file) => `<div class="gv-row">
        <span class="ic">${fileIcon(file.name)}</span>
        <div class="meta"><div class="fname">${esc(file.name)}</div></div>
        <span class="fsize">${fmtSize(file.size)}</span></div>`).join("");
      const checks = FORMS.map((form) => `<label class="${state.forms[form.id] ? "on" : ""}">
        <input type="checkbox" data-action="form" data-form="${form.id}" ${state.forms[form.id] ? "checked" : ""}>
        ${esc(form.name)}</label>`).join("");
      const ready = state.readiness;
      const llmNote = ready && ready.ready === false
        ? `<span class="k">語意協填未就緒（${esc(readinessReason(ready))}），未勾選時只列候選出處</span>` : "";
      const llmToggle = `<label class="${state.useLlm ? "on" : ""}">
        <input type="checkbox" data-action="llm" ${state.useLlm ? "checked" : ""}>使用語意協填（地端模型）</label>`;
      body = `<div class="gv-filelist">
        <div class="head"><span>已選擇 ${state.files.length} 個檔案</span>
          <button class="btn-small" data-action="reset">重新選擇</button></div>
        ${rows}</div>
        <div class="gv-assist-forms"><span class="k">要協填的表單</span>${checks}</div>
        <div class="gv-assist-forms">${llmToggle}${llmNote}</div>
        <div class="row-end"><button class="btn-primary" data-action="suggest"
          ${selectedForms().length ? "" : "disabled"}>產生填寫建議書　→</button></div>`;
    }
    return `<div>
      <h1 class="gv-h1">上傳佐證文件</h1>
      <p class="gv-lead">上傳您手上的簽呈、簡報、規格書或信件，系統會依 F01 / F02 / F03 的欄位逐一找出可引用的內容，
        產出一份「填寫建議書」。建議只供參考，官方表單仍需由您親自填寫。</p>
      ${renderError()}${body}</div>`;
  }

  function readinessReason(ready) {
    if (!ready.fields_available) return "欄位目錄缺檔";
    if (typeof ready.prompt === "string" && ready.prompt.startsWith("unavailable:")) return "Prompt 未部署";
    return "模型端點未設定或未啟用";
  }

  // ── 渲染：Step 2 填寫建議書 ────────────────────────────────────
  function renderNoticeCard(f) {
    const s = SEV[f.severity] || SEV.info;
    const open = !!state.openNotice[f.id];
    const body = open ? `<div class="fbody">
        <p class="fmsg">${esc(f.message)}</p>
        <div class="fcode-line"><code class="fcode">${esc(f.code)}</code></div></div>` : "";
    return `<div class="gv-finding${open ? " open" : ""}">
      <button class="fbtn" data-action="notice-toggle" data-id="${esc(f.id)}">
        <span class="fbadge" style="background:${s.bg};color:${s.fg};">${s.icon}</span>
        <span class="fsev" style="color:${s.fg};">${esc(s.label)}</span>
        <span class="ftitle">${esc(f.title)}</span>
        <span class="fcaret">⌄</span></button>${body}</div>`;
  }

  function renderNotices(r) {
    const notices = r.notices || [];
    if (!notices.length) return "";
    return `<div class="gv-notices gv-assist-notices">
      <div class="gv-notices-head">⚠ 協填未完整執行（以下狀況會讓部分欄位無建議，請自行填寫）</div>
      <div class="gv-findings">${notices.map(renderNoticeCard).join("")}</div>
    </div>`;
  }

  function formStats(r) {
    const stats = {};
    (r.forms || []).forEach((id) => { stats[id] = { covered: 0, total: 0 }; });
    let uncovered = 0;
    (r.suggestions || []).forEach((row) => {
      const s = stats[row.form] || (stats[row.form] = { covered: 0, total: 0 });
      s.total += 1;
      if (row.covered) s.covered += 1; else uncovered += 1;
    });
    return { stats, uncovered };
  }

  function renderBar(r) {
    const { stats, uncovered } = formStats(r);
    const mode = MODE[r.mode] || MODE.unavailable;
    const chips = Object.keys(stats).map((id) =>
      `<span class="gv-chip">${esc(FORM_LABEL[id] || id)} ${stats[id].covered}/${stats[id].total}</span>`);
    chips.push(`<span class="gv-chip" style="color:${SEV.warn.fg};background:${SEV.warn.bg};">未涵蓋 ${uncovered}</span>`);
    return `<div class="gv-assist-bar">
      <span class="gv-mode-badge ${mode.cls}" title="${esc(mode.note)}">${esc(mode.label)}</span>
      ${chips.join("")}
      <span class="gv-chip">${esc(r.assist_id || "")}</span>
    </div>`;
  }

  function renderTabs(r) {
    const forms = (r.forms || []).filter((id) => FORM_LABEL[id]);
    if (forms.length < 2) return "";
    return `<div class="gv-viewbar">
      ${forms.map((id) => `<button class="gv-tab${state.tab === id ? " active" : ""}" data-action="tab" data-form="${id}"
        aria-selected="${state.tab === id}">${esc(FORM_LABEL[id])}</button>`).join("")}
    </div>`;
  }

  function locatorText(c) {
    const head = c.file ? `文件 ${c.file}` : "文件";
    const tail = [
      c.sheet ? `工作表 ${c.sheet}` : null,
      c.cell ? `儲存格 ${c.cell}` : null,
      c.page == null ? null : `p.${c.page}`,
      c.section ? c.section : null,
    ].filter(Boolean).join(" · ");
    return tail ? `${head} · ${tail}` : head;
  }

  function renderCiteRow(row, key) {
    const cites = row.citations || [];
    const quotes = cites.length
      ? cites.map((c) => `<blockquote class="quote">${esc(c.quote || "（無引文）")}
          <span class="loc">${esc(locatorText(c))}</span></blockquote>`).join("")
      : `<div class="gv-mdetail-empty">此欄位沒有可對照的佐證片段。</div>`;
    const rationale = row.rationale ? `<div class="gv-src-note">${esc(row.rationale)}</div>` : "";
    return `<tr class="gv-assist-cite" data-key="${esc(key)}"><td colspan="5">
      ${rationale}${quotes}
      <div class="gv-cite-note">請自行核對原文；建議與出處皆為 AI 初判，正式填寫以您核對後的內容為準。</div>
    </td></tr>`;
  }

  function renderRow(row) {
    const key = rowKey(row);
    const adopted = !!state.adopted[key];
    const open = !!state.openCite[key];
    const covered = !!row.covered && row.suggestion != null && row.suggestion !== "";
    const suggestion = covered
      ? `<div class="sugg">${esc(row.suggestion)}</div>`
      : `<div class="none">${esc(UNCOVERED_TEXT)}</div>`;
    const cites = row.citations || [];
    const citeChip = cites.length
      ? `<button class="gv-chip${open ? " open" : ""}" data-action="cite" data-key="${esc(key)}" aria-expanded="${open}">
          出處 ${cites.length}${covered ? "" : "（候選）"}</button>`
      : `<span class="none">—</span>`;
    const conf = covered ? Math.round((Number(row.confidence) || 0) * 100) : null;
    const confHtml = conf == null ? `<span class="conf">—</span>`
      : `<span class="conf${conf >= 70 ? " hi" : ""}">${conf}%</span>`;
    const main = `<tr class="gv-assist-row${adopted ? " adopted" : ""}" data-key="${esc(key)}">
      <td><strong>${esc(row.label)}</strong><span class="cell">${esc(row.cell)}${row.kind === "yes_no" ? " · 是/否" : ""}</span></td>
      <td>${suggestion}</td>
      <td>${citeChip}</td>
      <td>${confHtml}</td>
      <td><input type="checkbox" class="adopt" data-action="adopt" data-key="${esc(key)}"
        aria-label="已抄入 ${esc(row.label)}" ${adopted ? "checked" : ""}></td>
    </tr>`;
    return open ? main + renderCiteRow(row, key) : main;
  }

  function renderTable(r) {
    const rows = (r.suggestions || []).filter((row) => !state.tab || row.form === state.tab);
    if (!rows.length) {
      return `<div class="gv-card"><div class="gv-assist-empty">此表單沒有可列的欄位（欄位目錄不可用或未選擇此表）。</div></div>`;
    }
    return `<div class="gv-card gv-assist-table"><table class="md-table">
      <thead><tr><th>欄位（儲存格）</th><th>建議內容</th><th>依據出處</th><th>信心</th><th>已抄入</th></tr></thead>
      <tbody>${rows.map(renderRow).join("")}</tbody></table></div>`;
  }

  function renderMeta(r) {
    const usage = r.usage || {};
    const documents = r.documents || [];
    const prompts = r.prompt_refs || [];
    return `<div class="gv-review-meta">
      <h2 class="gv-h2">執行資訊</h2>
      <div class="gv-meta-grid">
        <div><strong>模式</strong><span>${esc((MODE[r.mode] || MODE.unavailable).label)}</span></div>
        <div><strong>文件解析</strong><span>${documents.map((doc) => `${doc.document_id}:${doc.status}`).join("、") || "無"}</span></div>
        <div><strong>模型呼叫</strong><span>主要 ${usage.review_llm_primary_calls || 0}／Token 輸入 ${usage.input_tokens || 0}、輸出 ${usage.output_tokens || 0}</span></div>
        <div><strong>Prompt 版本</strong><span>${prompts.map((p) => `${p.prompt_id}@${p.prompt_version}`).join("、") || "未執行"}</span></div>
      </div>
    </div>`;
  }

  function renderResult() {
    const r = state.result;
    return `<div>
      <h1 class="gv-h1">填寫建議書<span class="light">（AI 協填草稿）</span></h1>
      <p class="gv-lead">每一列對應官方表單的一個欄位：建議內容旁附上佐證出處與信心。請逐列核對原文後再抄入表單，
        勾選「已抄入」可記錄您的進度（只存在本機瀏覽器）。</p>
      ${renderError()}
      ${renderNotices(r)}
      ${renderBar(r)}
      ${renderTabs(r)}
      ${renderTable(r)}
      ${renderMeta(r)}
      <div class="gv-persistence-note">AI 初判草稿 · 不代填 · 不匯出 Excel　·　官方表單請依建議書自行填寫並送單位主管核可。</div>
      <div class="gv-foot">
        <button class="btn-ghost" data-action="reset">↻　重新上傳</button>
        <div class="gv-foot-actions">
          <button class="btn-download" data-action="download">⬇ 下載建議書（Markdown）</button>
        </div>
      </div></div>`;
  }

  function renderOverlay() {
    if (!state.busy) return "";
    return `<div class="gv-overlay" role="status" aria-live="polite"><div class="box">
      <div class="spin"></div><div class="t">產生建議書中…</div>
      <div class="s">正在解析佐證並逐欄位檢索可引用內容；檔案處理後即刪除，不會保存。</div>
      </div></div>`;
  }

  // ── 主渲染 ─────────────────────────────────────────────────────
  function render() {
    const content = state.step === 2 && state.result ? renderResult() : renderStep1();
    app.setAttribute("aria-busy", state.busy ? "true" : "false");
    app.innerHTML = `${renderHeader()}<div class="gv-main">${content}</div>${renderOverlay()}`;
    bindDynamic();
  }

  function bindDynamic() {
    const dropzone = document.getElementById("dropzone");
    if (!dropzone) return;
    ["dragover", "dragenter"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach((eventName) => {
      dropzone.addEventListener(eventName, () => dropzone.classList.remove("dragover"));
    });
    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragover");
      if (event.dataTransfer && event.dataTransfer.files) onFilesChosen(event.dataTransfer.files);
    });
  }

  // ── 事件（委派）────────────────────────────────────────────────
  app.addEventListener("click", (e) => {
    if (state.busy) return;
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    if (action === "pick") { fileInput.value = ""; fileInput.click(); }
    else if (action === "reset") reset();
    else if (action === "suggest") suggest();
    else if (action === "download") downloadMarkdown();
    else if (action === "tab") { state.tab = el.dataset.form; render(); }
    else if (action === "cite") { const key = el.dataset.key; state.openCite[key] = !state.openCite[key]; render(); }
    else if (action === "notice-toggle") { const id = el.dataset.id; state.openNotice[id] = !state.openNotice[id]; render(); }
  });

  app.addEventListener("change", (e) => {
    if (state.busy) return;
    const el = e.target.closest("[data-action]");
    if (!el) return;
    if (el.dataset.action === "form") {
      state.forms[el.dataset.form] = el.checked;
      state.error = null;
      render();
    } else if (el.dataset.action === "llm") {
      state.useLlm = el.checked;
      render();
    } else if (el.dataset.action === "adopt") {
      const key = el.dataset.key;
      if (el.checked) state.adopted[key] = true; else delete state.adopted[key];
      saveAdopted();
      render();
    }
  });

  fileInput.addEventListener("change", (event) => {
    if (!state.busy) onFilesChosen(event.target.files);
  });

  render();
  loadReadiness();
})();
