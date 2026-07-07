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

  // 只有 F03 檢核項 finding 的 location 會長這樣（見 f03.py F03ChecklistItem.loc）：
  // 「項次 1-1」或「項次 1-1（管理議題）」。F01/F02/DOC/CROSS/CLASSIFY 一律不會命中。
  const ITEM_LOC_RE = /^項次\s*(\d+-\d+)(?:（([^）]*)）)?/;

  // 兩張 F03 彙整表（LLM_TABLE / RAG_TABLE）的儲存格前導符號 → 狀態種類。
  const CELL_MARKER_KIND = [["✅", "ok"], ["🟡", "warn"], ["❓", "undet"], ["⚠️", "err"]];

  // 彙整參考分頁：順序即 defaultRefTab 的優先序（原始彙整 → 法規對應 → 通過紀錄 → 全部）。
  const REF_TAB_ORDER = ["aggregate", "regref", "passlog", "all"];
  const REF_TAB_LABELS = { aggregate: "原始彙整", regref: "法規對應", passlog: "通過紀錄", all: "全部" };

  // 非項次 info finding 的分桶依據（見 f03_evidence.py / f03_rag.py / f02_reg_refs.py 產出代碼）。
  const AGGREGATE_CODES = new Set(["F03.LLM_TABLE", "F03.LLM_SUMMARY", "F03.RAG_TABLE", "F03.RAG_SUMMARY"]);
  function isRegRefCode(code) { return code === "F02.REG_REF_SUMMARY" || code === "F02.REG_REF_NOTE"; }

  // 整段 AI 判讀降級訊號（端點連不上而略過，或連續失敗而中止）：非合規判讀結果，
  // 代表「這次 LLM/RAG 判讀未完整跑」。須在主視圖顯著提示，不可埋進單一項次列或收合的彙整參考。
  // 只有在 LLM/RAG 原本應執行時才會由 engine/checks 產生（刻意停用不會出現），故出現即為真降級。
  const NOTICE_CODES = new Set(["F03.LLM_SKIPPED", "RAG.SKIPPED", "F03.LLM_ERROR", "F03.RAG_ERROR"]);

  // 總覽表「主因」欄位的問題欄 → 顯示字樣；CAUSE_CODE_MAP 讓「無彙整表」時仍能由個別
  // finding 代碼推出主因（見 buildMatrix 的 no-LLM fallback 說明）。
  const CAUSE_LABELS = {
    golive: "上線階段佐證不足",
    prop: "提案階段佐證不足",
    diff: "提案與上線差異",
    rag: "法規符合性缺口",
  };
  const CAUSE_CODE_MAP = {
    "F03.EVIDENCE_MISSING_PROPOSAL": "prop",
    "F03.LLM_VAGUE_PROPOSAL": "prop",
    "F03.EVIDENCE_MISSING_GOLIVE": "golive",
    "F03.LLM_VAGUE_GOLIVE": "golive",
    "F03.LLM_DIFF": "diff",
    "F03.RAG_GAP": "rag",
  };
  // 規則式「佐證空白」finding → 狀態格後備（no-LLM 模式下整列不再全「—」）；
  // 僅在該格尚無彙整表資料（kind === "none"）時套用，表格判讀永遠優先。
  // 值取自 CAUSE_CODE_MAP（同兩碼對應的佐證欄與主因欄一致），集中管理避免雙表漂移。
  const RULE_CELL_FALLBACK = {
    "F03.EVIDENCE_MISSING_PROPOSAL": CAUSE_CODE_MAP["F03.EVIDENCE_MISSING_PROPOSAL"],
    "F03.EVIDENCE_MISSING_GOLIVE": CAUSE_CODE_MAP["F03.EVIDENCE_MISSING_GOLIVE"],
  };

  // ── 狀態 ───────────────────────────────────────────────────────
  const state = {
    step: 1,
    files: [],          // 真實 File 物件
    classify: null,     // /api/classify 回應 { kinds, files:[...] }
    kinds: {},          // index -> 使用者選定 kind（覆寫預設）
    review: null,       // /api/review 回應
    filter: "info",     // 【彙整參考】「全部」後備分頁用的舊版嚴重度濾鏡（保留原行為）
    open: {},           // finding id -> bool（扁平清單 / 彙整卡共用的展開狀態）
    rowOpen: {},        // 檢核項狀態總覽表：itemId -> 是否展開
    matrixFilter: "attention", // 總覽表濾鏡："attention"（只看有問題）| "all"（全部）
    refOpen: false,     // 【彙整參考】卡片是否展開
    refTab: null,       // 【彙整參考】目前分頁 key（aggregate/regref/passlog/all）
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

  // 從 lines[i] 開始嘗試解析一個 GFM 表格（| 開頭列 + 分隔列 + 資料列）；不是表格開頭回 null。
  // 回傳 next = 表格結束後下一行索引，供呼叫端（renderMessage）續掃剩餘內容。
  function parseGfmTableAt(lines, i) {
    const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
    // 僅把「以 | 開頭」的列視為表格列（我們的彙整表必有前導 |，且儲存格已去除 |），
    // 避免含管線符號的一般說明文字被誤判為表格。
    const isRow = (l) => l.trimStart().startsWith("|");
    const splitRow = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    if (!(i + 1 < lines.length && isRow(lines[i]) && isSep(lines[i + 1]))) return null;
    const header = splitRow(lines[i]);
    let j = i + 2;
    const rows = [];
    while (j < lines.length && isRow(lines[j]) && !isSep(lines[j])) { rows.push(splitRow(lines[j])); j++; }
    return { header, rows, next: j };
  }

  // 給 buildMatrix 用：F03.LLM_TABLE / F03.RAG_TABLE 的 message 恆「以表格開頭、無其餘文字」，
  // 故直接從第 0 行找表格即可；非表格（或 message 為空）回 null。
  function parseGfmTable(msg) {
    if (!msg) return null;
    const t = parseGfmTableAt(String(msg).split("\n"), 0);
    return t ? { header: t.header, rows: t.rows } : null;
  }

  // 極簡 GFM 表格渲染（給 F03 LLM 佐證彙整表的多行訊息）；非表格純文字保留換行。
  function renderMessage(msg) {
    if (!msg) return "";
    const lines = String(msg).split("\n");
    let html = "";
    let buf = [];
    const flush = () => {
      if (buf.length) { html += `<p class="fmsg">${buf.map(esc).join("<br>")}</p>`; buf = []; }
    };
    let i = 0;
    while (i < lines.length) {
      const table = parseGfmTableAt(lines, i);
      if (table) {
        flush();
        html += "<table class=\"md-table\"><thead><tr>" +
          table.header.map((h) => `<th>${esc(h)}</th>`).join("") + "</tr></thead><tbody>" +
          table.rows.map((r) => "<tr>" + r.map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
        i = table.next;
      } else if (lines[i].trim() === "") {
        flush(); i++;
      } else {
        buf.push(lines[i]); i++;
      }
    }
    flush();
    return html;
  }

  // ── 純函式：F03 項次定位 / 排序 / 嚴重度 / 摘要 / 儲存格判讀 ──────────

  // 從 finding.location 解析「項次 1-1」「項次 1-1（管理議題）」→ {itemId, topic}；不符回 null。
  function parseItemLoc(location) {
    if (!location) return null;
    const m = ITEM_LOC_RE.exec(location);
    if (!m) return null;
    return { itemId: m[1], topic: m[2] || null };
  }

  // 項次自然排序：依「-」拆段後逐段比數值（"1-1" < "1-2" < "2-1" < "10-1"）。
  function naturalItemCompare(a, b) {
    const pa = String(a).split("-").map(Number);
    const pb = String(b).split("-").map(Number);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
      const da = pa[i] || 0;
      const db = pb[i] || 0;
      if (da !== db) return da - db;
    }
    return 0;
  }

  // 一組 finding 中最高（數字最小）的嚴重度；空陣列回 null。
  function maxSeverity(findings) {
    if (!findings.length) return null;
    let best = findings[0].severity;
    findings.forEach((f) => { if (SEV_ORDER[f.severity] < SEV_ORDER[best]) best = f.severity; });
    return best;
  }

  // 彙整表儲存格文字 → {kind, note}：kind 由「前導符號」決定，note 為符號後剩餘文字（trim）。
  // 空白或「—」→ none；不含已知符號的文字（理論上不會發生）也回 none，並把全文塞進 note。
  function cellStatus(cellText) {
    const text = (cellText == null ? "" : String(cellText)).trim();
    if (!text || text === "—") return { kind: "none", note: "" };
    for (const [marker, kind] of CELL_MARKER_KIND) {
      if (text.startsWith(marker)) return { kind, note: text.slice(marker.length).trim() };
    }
    return { kind: "none", note: text };
  }

  // 彙整參考預設分頁：依 REF_TAB_ORDER 找第一個非空桶；理論上 all 恆非空，僅作保底。
  function defaultRefTab(reference) {
    for (const key of REF_TAB_ORDER) { if (reference[key] && reference[key].length) return key; }
    return "all";
  }

  // ── 檢核項狀態總覽表：把 findings 分流成「項次列 / 其他待處理 / 彙整參考」──

  function newMatrixRow(itemId, topic) {
    return {
      itemId,
      topic: topic || null,
      findings: [],
      cells: {
        prop: { kind: "none", note: "" },
        golive: { kind: "none", note: "" },
        diff: { kind: "none", note: "" },
        rag: { kind: "none", note: "" },
      },
      refs: "",
    };
  }

  // 摘要欄：優先序為「兩段差異摘要 → 上線佐證草率原因 → 法規缺口 → 首個附掛 finding 標題」。
  // 後備取 title 而非 message：no-LLM 模式下 message 是整句規則說明文，塞進摘要欄變雜訊；
  // title 本來就是精煉字樣（如「上線階段佐證空白」）。
  // 注意：須在 applyRuleCellFallback 之前呼叫，讓儲存格 note 只反映彙整表判讀結果。
  function rowGist(row) {
    if (row.cells.diff.note) return row.cells.diff.note;
    if (row.cells.golive.note) return row.cells.golive.note;
    if (row.cells.rag.kind === "warn" && row.cells.rag.note) return row.cells.rag.note;
    if (row.findings.length) return row.findings[0].title;
    return "—";
  }

  // 規則式「佐證空白」finding 補進狀態格（僅填 kind === "none" 的格，表格資料永遠優先）。
  function applyRuleCellFallback(row) {
    row.findings.forEach((f) => {
      const col = RULE_CELL_FALLBACK[f.code];
      if (col && row.cells[col].kind === "none") row.cells[col] = { kind: "warn", note: "空白" };
    });
  }

  // 這一列「問題出在哪一欄」的集合：優先看彙整表儲存格狀態，再補上個別 finding 代碼
  // （沒有彙整表時，個別 finding 代碼是唯一線索，讓 no-LLM fallback 也能算出主因）。
  function rowCauses(row) {
    const causes = new Set();
    if (row.cells.golive.kind === "warn") causes.add("golive");
    if (row.cells.prop.kind === "warn") causes.add("prop");
    if (row.cells.diff.kind === "warn" || row.cells.diff.kind === "err") causes.add("diff");
    if (row.cells.rag.kind === "warn") causes.add("rag");
    row.findings.forEach((f) => { const c = CAUSE_CODE_MAP[f.code]; if (c) causes.add(c); });
    return causes;
  }

  function computePrimaryCause(attentionRows) {
    const tally = { golive: 0, prop: 0, diff: 0, rag: 0 };
    attentionRows.forEach((row) => { row.causes.forEach((c) => { tally[c] += 1; }); });
    let best = null;
    let bestN = 0;
    ["golive", "prop", "diff", "rag"].forEach((k) => { if (tally[k] > bestN) { best = k; bestN = tally[k]; } });
    return best ? CAUSE_LABELS[best] : "—";
  }

  // 單一入口：一次掃過所有 findings，產出總覽表列、其他待處理、彙整參考四桶、統計。
  function buildMatrix(report) {
    const rowMap = new Map();
    const others = [];
    const notices = [];
    const reference = { aggregate: [], regref: [], passlog: [], all: [] };

    report.findings.forEach((f) => {
      // 降級訊號先攔下：它可能帶著某一項次的 location（如 LLM 中止取 chunk[0].loc），
      // 若不先攔，會被下面的 item-location 分流埋進那一列的收合明細裡。
      if (NOTICE_CODES.has(f.code)) { notices.push(f); return; }
      const loc = parseItemLoc(f.location);
      if (loc) {
        // 項次-定位的 finding 一律進 Map，不分 severity：即使是 info 等級的
        // 「上線階段佐證空白」，也正是治理人員要親自確認的項目；這樣一來，
        // 完全沒有 LLM/RAG 彙整表時，總覽表仍有內容可看（不會整表空白）。
        let row = rowMap.get(loc.itemId);
        if (!row) { row = newMatrixRow(loc.itemId, loc.topic); rowMap.set(loc.itemId, row); }
        if (loc.topic && !row.topic) row.topic = loc.topic;
        row.findings.push(f);
      } else if (f.severity !== "info") {
        others.push(f);
      } else if (AGGREGATE_CODES.has(f.code)) {
        reference.aggregate.push(f);
      } else if (isRegRefCode(f.code)) {
        reference.regref.push(f);
      } else {
        reference.passlog.push(f);
      }
    });
    reference.all = [...report.findings].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);

    const llmTableFinding = reference.aggregate.find((f) => f.code === "F03.LLM_TABLE");
    const ragTableFinding = reference.aggregate.find((f) => f.code === "F03.RAG_TABLE");
    const llmTable = llmTableFinding ? parseGfmTable(llmTableFinding.message) : null;
    const ragTable = ragTableFinding ? parseGfmTable(ragTableFinding.message) : null;
    const noTables = !llmTable && !ragTable;

    if (llmTable) {
      llmTable.rows.forEach((cells) => {
        const [itemId, topic, diffCell, propCell, golCell] = cells;
        if (!itemId) return;
        let row = rowMap.get(itemId);
        if (!row) { row = newMatrixRow(itemId, topic); rowMap.set(itemId, row); }
        if (topic && topic !== "—") row.topic = topic; // 表格 topic 優先
        row.cells.diff = cellStatus(diffCell);
        row.cells.prop = cellStatus(propCell);
        row.cells.golive = cellStatus(golCell);
      });
    }
    if (ragTable) {
      ragTable.rows.forEach((cells) => {
        const [itemId, topic, verdictCell, refsCell] = cells;
        if (!itemId) return;
        let row = rowMap.get(itemId);
        if (!row) { row = newMatrixRow(itemId, topic); rowMap.set(itemId, row); }
        if (topic && topic !== "—") row.topic = topic;
        row.cells.rag = cellStatus(verdictCell);
        row.refs = refsCell && refsCell !== "—" ? refsCell : "";
      });
    }

    const rows = [...rowMap.values()];
    rows.forEach((row) => {
      // 順序有意為之：先算摘要（此時儲存格 note 只含彙整表判讀結果），再補規則式狀態格
      // （避免後備填入的「空白」note 蓋掉更精煉的 finding 標題摘要），最後才統計注意/嚴重度。
      row.gist = rowGist(row);
      applyRuleCellFallback(row);
      const cellKeys = ["prop", "golive", "diff", "rag"];
      // warn（有問題）、err（⚠️ 判讀失敗）、undet（❓ 無法判定）三種儲存格都代表「需人工確認」：
      // err/undet 是「AI 判不了，請人看」，同樣不能在預設「只看有問題」濾鏡下被藏掉。
      const attnKinds = { warn: 1, err: 1, undet: 1 };
      const anyCellAttn = cellKeys.some((k) => attnKinds[row.cells[k].kind]);
      const findingSev = maxSeverity(row.findings);
      const anyFindingWarnErr = findingSev === "error" || findingSev === "warn";
      // 有需注意的儲存格／有 warn+ 的附掛 finding／或彙整表完全缺席時任何附掛 finding 都算數
      // （no-LLM fallback：規則式的佐證空白 finding 是唯一線索，不能被漏掉）。
      row.hasAttention = anyCellAttn || anyFindingWarnErr || (noTables && row.findings.length > 0);
      // 嚴重度（紅底＋自動展開）只由真正 error 級的附掛 finding 決定；⚠️/❓ 在後端皆為 INFO
      // （判讀失敗/無法判定 ≠ 合規違規），不可染成紅色錯誤、搶走治理人員對真違規的 triage 注意力。
      row.rowSeverity = findingSev === "error" ? "error" : (row.hasAttention ? "warn" : "info");
      row.causes = rowCauses(row);
    });
    rows.sort((a, b) => naturalItemCompare(a.itemId, b.itemId));

    const attentionRows = rows.filter((r) => r.hasAttention);
    const stats = {
      attention: attentionRows.length,
      total: rows.length,
      primaryCause: computePrimaryCause(attentionRows),
    };
    return { rows, others, notices, reference, stats };
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
    // 預設停在【紀錄】（彙整表所在，供「彙整參考→全部」後備分頁沿用）；
    // 若無任何紀錄項，退回【全部】，避免落地畫面空白而藏住錯誤。
    state.filter = data.info_count > 0 ? "info" : "all";

    const m = buildMatrix(data);
    const rowOpen = {};
    m.rows.forEach((row) => { if (row.rowSeverity === "error") rowOpen[row.itemId] = true; });
    state.rowOpen = rowOpen;
    state.matrixFilter = m.stats.attention > 0 ? "attention" : "all";
    state.refOpen = false;
    state.refTab = defaultRefTab(m.reference);

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
    state.rowOpen = {};
    state.matrixFilter = "attention";
    state.refOpen = false;
    state.refTab = null;
    state.reviewing = false;  // 防禦性清空：正常路徑 applyReport/錯誤處理已先清，這裡保持完整
    state.progress = null;
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

  // ── 渲染：finding 卡片共用（扁平清單 / 彙整參考 / 其他待處理 共用）────

  // 期望/實際比較區塊；無 expected/actual 時回空字串。
  function findingCompareHtml(f) {
    const hasCompare = f.expected != null || f.actual != null;
    if (!hasCompare) return "";
    const s = SEV[f.severity];
    return `<div class="gv-compare">
      <div class="col expected"><div class="k">期望</div><div class="v">${esc(f.expected || "—")}</div></div>
      <div class="col" style="background:${s.bg};border-color:${s.border};"><div class="k">實際</div>
        <div class="v" style="color:${s.fg};">${esc(f.actual || "—")}</div></div></div>`;
  }

  // finding 展開內容：期望/實際比較 + renderMessage + 代碼列（扁平卡與總覽表明細列共用）。
  function findingBodyHtml(f) {
    return `<div class="fbody">${findingCompareHtml(f)}${renderMessage(f.message)}
      <div class="fcode-line"><span class="fcode">${esc(f.code)}</span>
        <span class="fhint">· 需人工覆核</span></div></div>`;
  }

  // 單一可展開/收合的 finding 卡片（扁平清單 / 彙整參考 / 其他待處理 共用）。
  function renderFindingCard(f) {
    const s = SEV[f.severity];
    const open = !!state.open[f.id];
    const loc = f.location ? `<span class="floc">${esc(f.location)}</span>` : "";
    const bodyHtml = open ? findingBodyHtml(f) : "";
    return `<div class="gv-finding ${f.severity === "error" ? "err" : ""}${open ? " open" : ""}">
      <button class="fbtn" data-action="toggle" data-id="${esc(f.id)}">
        <span class="fbadge" style="background:${s.bg};color:${s.fg};">${s.icon}</span>
        <span class="fsev" style="color:${s.fg};">${esc(s.label)}</span>
        <span class="ftitle">${esc(f.title)}</span>${loc}
        <span class="fcaret">⌄</span></button>${bodyHtml}</div>`;
  }

  // ── 渲染：Step 3 報告 ──────────────────────────────────────────

  // Hero：徽章＋標題＋受審對象＋結論條 + tiles（固有風險 / 錯誤 / 待確認項 / 無異常項）。
  function renderSummary(r, m) {
    const passed = r.passed;
    const barColor = passed ? "var(--ok)" : "var(--err-fg)";
    const { attention, total, primaryCause } = m.stats;
    const clean = total - attention;

    const tileHtml = (n, label, colors, outline) => {
      const out = outline && n > 0 ? `outline-color:${colors.fg};` : "";
      return `<div class="gv-tile${outline && n > 0 ? " big" : ""}" style="background:${colors.bg};color:${colors.fg};border-color:${colors.border};${out}">
        <div class="n">${n}</div><div class="l">${esc(label)}</div></div>`;
    };
    const okColors = { bg: "var(--ok-bg)", fg: "var(--ok)", border: "var(--ok-bg)" };

    // 固有風險分級/分數 tile（來源：F02 第一頁 AI系統固有風險分級評估表）；無 F02 則不渲染。
    const riskTile = (() => {
      if (!r.risk_grade) return "";
      const c = riskColors(r.risk_grade);
      const pct = r.risk_score != null ? ` · ${r.risk_score.toFixed(0)}%` : "";
      return `<div class="gv-tile" style="background:${c.bg};color:${c.fg};border-color:${c.border};min-width:120px;">
        <div class="n">${esc(r.risk_grade)}</div><div class="l">固有風險${esc(pct)}</div></div>`;
    })();

    // 結論條文字與配色須反映「所有需處理的事」，不只 F03 檢核項：非項次錯誤（缺件/F01必填/
    // 跨表不一致）進 m.others、AI 判讀降級進 m.notices；任一非空都代表待處理，不可顯示綠色「無需確認」
    // （否則會與同一卡片內紅色「✕ 待處理」徽章及非零錯誤 tile 自相矛盾）。
    const otherCount = m.others.length;
    const needsAction = attention > 0 || r.error_count > 0 || otherCount > 0 || m.notices.length > 0;
    const parts = [`${r.error_count} 錯誤`];
    if (attention > 0) parts.push(`${attention}/${total} 檢核項需人工確認`);
    if (otherCount > 0) parts.push(`另 ${otherCount} 項其他待處理`);
    if (m.notices.length > 0) parts.push("AI 判讀未完整執行");
    if (attention > 0) parts.push(`主因：${primaryCause}`);
    const conclusion = needsAction ? `「${parts.join(" · ")}」` : "「0 錯誤 · 檢核項無需人工確認」";

    return `<div class="gv-summary" style="--bar:${barColor};">
      <div class="top">
        <div class="lead">
          <span class="badge" style="background:${passed ? "var(--ok-bg)" : "var(--err-bg)"};color:${passed ? "var(--ok)" : "var(--err-fg)"};">${passed ? "✓" : "✕"}</span>
          <div><div class="title" style="color:${passed ? "var(--ok)" : "var(--err-fg)"};">${passed ? "規則通過" : "待處理"}</div>
            <div class="subline">受審：${esc(r.subject || "未標示")}　·　${esc(r.form_type)}</div>
            <div class="gv-hero-conclusion ${needsAction ? "attn" : "ok"}">${esc(conclusion)}</div>
          </div>
        </div>
        <div class="gv-tiles">
          ${riskTile}
          ${tileHtml(r.error_count, "錯誤", SEV.error, true)}
          ${tileHtml(attention, "待確認項", SEV.warn, true)}
          ${tileHtml(clean, "無異常項", okColors, false)}
        </div>
      </div>
      <div class="foot"><span class="note spacer">⚠ 本報告為 AI 初判，每項皆需人工覆核</span></div>
    </div>`;
  }

  // AI 判讀降級提示：端點連不上而略過、或連續失敗而中止時，在主視圖顯著提示「這次判讀未完整跑」。
  // 非合規違規（琥珀資訊帶，非紅色錯誤）；只在存在時渲染，避免正常情況出現空帶。
  function renderNotices(m) {
    if (!m.notices.length) return "";
    const items = m.notices.map((f) =>
      `<li><strong>${esc(f.title)}</strong>${f.message ? "：" + esc(f.message) : ""}</li>`).join("");
    return `<div class="gv-notices">
      <div class="gv-notices-head">⚠ AI 判讀未完整執行（規則檢查不受影響，請一併人工覆核）</div>
      <ul>${items}</ul>
    </div>`;
  }

  // 其他待處理：非項次的 error/warn finding，沿用扁平卡片；只在存在時渲染整節。
  function renderOthers(m) {
    if (!m.others.length) return "";
    const sorted = [...m.others].sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);
    return `<div class="gv-others">
      <h2 class="gv-h2">其他待處理</h2>
      <div class="gv-findings">${sorted.map(renderFindingCard).join("")}</div>
    </div>`;
  }

  // 狀態格 note 截短（僅總覽表狀態格使用；完整文字在展開明細與摘要欄，不損失資訊）：
  // 1) 去「上線階段/提案階段」前綴 — 欄名已標明階段，重複只佔寬度；
  // 2) 取第一個標點（，。；、：）前的片段 — 兼去尾標點，避免截在句中殘留逗號；
  // 3) 仍超過 8 字 → 硬截 8 字加「…」；放得下則不加。
  // 例：「上線階段沒有提供任何佐證」→「沒有提供任何佐證」；「佐證為空白，無法判斷」→「佐證為空白」。
  const CELL_NOTE_MAX = 8;
  function shortCellNote(note) {
    let s = String(note == null ? "" : note).trim();
    s = s.replace(/^(上線階段|提案階段)/, "");
    const m = /[，。；、：]/.exec(s);
    if (m) s = s.slice(0, m.index);
    s = s.trim();
    return s.length > CELL_NOTE_MAX ? s.slice(0, CELL_NOTE_MAX) + "…" : s;
  }

  // 狀態格 → 小色塊 label（依 kind 決定顏色/字樣；warn 依欄位不同截短或改固定字樣）。
  function matrixCellHtml(colKey, cell) {
    if (cell.kind === "ok") return `<span class="st-ok">✓ ${esc(cell.note || "充分")}</span>`;
    if (cell.kind === "undet") return `<span class="st-undet">？無法判定</span>`;
    if (cell.kind === "err") return `<span class="st-err">✕ 判讀失敗</span>`;
    if (cell.kind === "warn") {
      if (colKey === "diff") return `<span class="st-warn">▲ 有差異</span>`;
      const short = shortCellNote(cell.note) || "待確認";
      return `<span class="st-warn">▲ ${esc(short)}</span>`;
    }
    return `<span class="st-none">—</span>`;
  }

  // 展開列裡單一附掛 finding 的完整明細（沿用 findingBodyHtml，加一行嚴重度＋標題）。
  function matrixDetailFindingHtml(f) {
    const s = SEV[f.severity];
    return `<div class="gv-mdetail-finding">
      <div class="gv-mdetail-head" style="color:${s.fg};">${esc(s.label)} · ${esc(f.title)}</div>
      ${findingBodyHtml(f)}
    </div>`;
  }

  function renderMatrixDetailRow(row) {
    const inner = row.findings.length
      ? row.findings.map(matrixDetailFindingHtml).join('<div class="gv-mdetail-sep"></div>')
      : `<div class="gv-mdetail-empty">此項次無個別 finding；狀態來自彙整表判讀。</div>`;
    const refsLine = row.refs ? `<div class="gv-mdetail-refs">條文引用：${esc(row.refs)}</div>` : "";
    return `<tr class="gv-matrix-detail"><td colspan="8">${inner}${refsLine}</td></tr>`;
  }

  function renderMatrixRow(row) {
    const open = !!state.rowOpen[row.itemId];
    const rowClass = row.rowSeverity === "error" ? " row-err" : (row.hasAttention ? " row-attn" : "");
    const topic = row.topic ? esc(row.topic) : "—";
    const gist = esc(row.gist || "—");
    const mainRow = `<tr class="gv-mrow${rowClass}${open ? " open" : ""}" data-action="row-toggle" data-key="${esc(row.itemId)}">
      <td>${esc(row.itemId)}</td>
      <td>${topic}</td>
      <td>${matrixCellHtml("prop", row.cells.prop)}</td>
      <td>${matrixCellHtml("golive", row.cells.golive)}</td>
      <td>${matrixCellHtml("diff", row.cells.diff)}</td>
      <td>${matrixCellHtml("rag", row.cells.rag)}</td>
      <td class="gv-mgist" title="${gist}">${gist}</td>
      <td class="gv-mcaret"><span class="fcaret">⌄</span></td>
    </tr>`;
    return mainRow + (open ? renderMatrixDetailRow(row) : "");
  }

  // ① 檢核項狀態總覽：整份報告的中心視覺 — 一眼看出哪些檢核項有問題待確認。
  function renderMatrix(m) {
    const { rows, stats } = m;
    if (!rows.length) {
      return `<div class="gv-matrix">
        <div class="gv-matrix-head"><h2 class="gv-h2">① 檢核項狀態總覽</h2></div>
        <div class="gv-matrix-empty">規則檢查無需人工確認項目</div>
      </div>`;
    }
    const shown = state.matrixFilter === "all" ? rows : rows.filter((r) => r.hasAttention);
    const pillDefs = [
      { key: "attention", label: `只看有問題 ${stats.attention}` },
      { key: "all", label: `全部 ${stats.total}` },
    ];
    const pills = pillDefs.map((p) =>
      `<button class="gv-filter${state.matrixFilter === p.key ? " active" : ""}" data-action="matrix-filter" data-mf="${p.key}">${esc(p.label)}</button>`
    ).join("");

    return `<div class="gv-matrix">
      <div class="gv-matrix-head">
        <h2 class="gv-h2">① 檢核項狀態總覽</h2>
        <div class="gv-matrix-filters">${pills}</div>
      </div>
      <div class="gv-matrix-card">
        <table>
          <thead><tr>
            <th>項次</th><th>管理議題</th><th>提案佐證</th><th>上線佐證</th>
            <th>兩段差異</th><th>法規符合</th><th>摘要</th><th></th>
          </tr></thead>
          <tbody>${shown.map(renderMatrixRow).join("")}</tbody>
        </table>
      </div>
    </div>`;
  }

  // 【彙整參考】「全部」後備分頁：逐字保留舊版扁平清單行為（state.filter 嚴重度濾鏡 + 舊卡片）。
  function renderAllFallback(r) {
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

    return `<div class="gv-filters">${filters}</div>
      <div class="gv-findings">${shown.map(renderFindingCard).join("")}</div>`;
  }

  // ② 彙整參考：預設收合的教師卡；展開後依分頁顯示彙整表原文 / 法規對應 / 通過紀錄 / 全部後備。
  function renderReference(r, m) {
    const ref = m.reference;
    const counts = {
      aggregate: ref.aggregate.length,
      regref: ref.regref.length,
      passlog: ref.passlog.length,
      all: ref.all.length,
    };
    const teaserSummary = `原始彙整 ${counts.aggregate}　法規對應 ${counts.regref}　通過紀錄 ${counts.passlog}`;
    const teaser = `<button class="gv-ref-teaser" data-action="ref-toggle">
      <span class="badge">②</span>
      <div class="body"><div class="title">彙整參考</div><div class="sum">${esc(teaserSummary)}</div></div>
      <span class="fcaret${state.refOpen ? " open" : ""}">⌄</span>
    </button>`;

    if (!state.refOpen) {
      return `<div class="gv-reference">${teaser}</div>`;
    }

    const tabs = REF_TAB_ORDER.map((key) => ({ key, label: `${REF_TAB_LABELS[key]} ${counts[key]}` }));
    const tabPills = tabs.map((t) =>
      `<button class="gv-filter${state.refTab === t.key ? " active" : ""}" data-action="ref-tab" data-tab="${t.key}">${esc(t.label)}</button>`
    ).join("");

    let body;
    if (state.refTab === "all") {
      body = renderAllFallback(r);
    } else {
      const list = ref[state.refTab] || [];
      body = `<div class="gv-findings">${list.length ? list.map(renderFindingCard).join("") : '<div class="gv-matrix-empty">尚無項目</div>'}</div>`;
    }

    return `<div class="gv-reference">${teaser}
      <div class="gv-ref-tabs">${tabPills}</div>
      <div class="gv-ref-body">${body}</div>
    </div>`;
  }

  function renderStep3() {
    const r = state.review;
    const m = buildMatrix(r);
    return `<div>
      ${renderSummary(r, m)}
      ${renderError()}
      ${renderNotices(m)}
      ${renderOthers(m)}
      ${renderMatrix(m)}
      ${renderReference(r, m)}
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
    else if (action === "row-toggle") { const key = el.dataset.key; state.rowOpen[key] = !state.rowOpen[key]; render(); }
    else if (action === "matrix-filter") { state.matrixFilter = el.dataset.mf; render(); }
    else if (action === "ref-toggle") { state.refOpen = !state.refOpen; render(); }
    else if (action === "ref-tab") { state.refTab = el.dataset.tab; state.refOpen = true; render(); }
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
