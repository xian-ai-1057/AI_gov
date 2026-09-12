/* AI 治理審查小幫手 — 前端（vanilla JS）。
   四步驟：上傳送件包 → 確認自動分類 → 審查報告 → 覆核與退件。狀態驅動整頁重繪。
   後端：POST /api/classify、POST /api/review/stream（失敗時退回 /api/review）。
   資料全程地端，無外部請求。 */

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
  const STEP_LABELS = ["上傳送件包", "確認分類", "審查報告", "覆核與退件"];
  const CHECKLIST = [
    { tag: "F1", name: "F01 系統資訊表", note: "系統基本資訊與必填欄位" },
    { tag: "F2", name: "F02 風險評鑑", note: "固有風險計分與分級" },
    { tag: "F3", name: "F03 上線檢核表", note: "上線前檢核項目" },
    { tag: "＋", name: "佐證文件", note: "模型卡、測試報告等（可多檔）" },
  ];

  // 只有 F03 檢核項 finding 的 location 會長這樣（見 f03.py F03ChecklistItem.loc）：
  // 「項次 1-1」或「項次 1-1（管理議題）」。F01/F02/DOC/CROSS 一律不會命中。
  const ITEM_LOC_RE = /^項次\s*(\d+-\d+)(?:（([^）]*)）)?/;

  // 彙整參考分頁：順序即 defaultRefTab 的優先序（判讀摘要 → 法規對應 → 通過紀錄 → 全部）。
  const REF_TAB_ORDER = ["aggregate", "regref", "passlog", "all"];
  const REF_TAB_LABELS = { aggregate: "判讀摘要", regref: "法規對應", passlog: "通過紀錄", all: "全部" };

  // 非項次 info finding 的分桶依據（見 f03_assessment.py / f02_reg_refs.py 產出代碼）。
  const AGGREGATE_CODES = new Set(["F03.ASSESSMENT_SUMMARY"]);
  function isRegRefCode(code) {
    return code === "F02.REG_REF_SUMMARY" || code === "F02.REG_REF_NOTE"
      || code === "REGULATION.MAP_UNAVAILABLE";
  }

  const DECISION_OPTIONS = [
    { value: "pending", label: "待確認", cls: "" },
    { value: "accept", label: "接受初判", cls: "d-accept" },
    { value: "reject", label: "不同意", cls: "d-reject" },
    { value: "na", label: "不適用", cls: "d-na" },
  ];
  const SOURCE_TAGS = {
    rule: { cls: "tag-rule", label: "規則" },
    rag: { cls: "tag-rag", label: "檢索" },
    llm: { cls: "tag-llm", label: "語意" },
  };

  // 總覽表「主因」欄位的問題欄 → 顯示字樣；CAUSE_CODE_MAP 讓「無彙整表」時仍能由個別
  // finding 代碼推出主因（見 buildMatrix 的 no-LLM fallback 說明）。
  const CAUSE_LABELS = {
    golive: "上線階段內容不足",
    prop: "提案階段內容不足",
    evidence: "佐證文件不足",
    diff: "佐證彼此衝突",
    reg: "法規符合性缺口",
  };
  const CAUSE_CODE_MAP = {
    "F03.EVIDENCE_MISSING_PROPOSAL": "prop",
    "F03.EVIDENCE_MISSING_GOLIVE": "golive",
    "F03.ASSESSMENT_INCOMPLETE": "prop",
    "F03.ASSESSMENT_VAGUE": "prop",
    "F03.ASSESSMENT_UNREASONABLE": "prop",
    "F03.EVIDENCE_NOT_SUPPORTED": "evidence",
    "F03.EVIDENCE_CONTRADICTED": "diff",
    "F03.EVIDENCE_SOURCE_CONFLICT": "diff",
    "F03.REGULATION_GAP": "reg",
    "F03.TEMPLATE_REF_MODIFIED": "reg",
  };
  // 流程階段：治理流程只有提案、上線兩段（見 models/evidence.WorkflowStage）。
  // 值必須與後端 enum 一致；下拉選單與「審查執行資訊」的中文字樣都由此表推出。
  const STAGE_LABELS = { proposal: "提案", golive: "上線" };
  const isGoliveStage = (stage) => stage === "golive";

  // 上線佐證相對提案佐證的差異（後端規則式比對，見 F03ChecklistItem.golive_diff_for_stage）。
  // 「相同」也要明講：照抄提案內容等於沒有交代上線後的實況，是治理人員要看見的事實。
  // 不含 missing：上線佐證空白已由規則式 F03.EVIDENCE_MISSING_GOLIVE 標成「空白」，不重複警示。
  const DIFF_CELL = {
    same: { kind: "skip", note: "同提案" },
    changed: { kind: "skip", note: "有差異" },
  };
  const DIFF_LABELS = {
    missing: "未填寫上線階段佐證說明。",
    same: "上線階段佐證與提案內容完全相同（未列出上線後的差異）。",
    changed: "上線階段佐證與提案內容不同，下方以標示標出上線內容中新增／改寫的段落。",
  };

  // 未進入語意判讀的檢核項（見 F03Checklist.roster）：狀態格與摘要的字樣。
  // 這些列刻意不算「需人工確認」（kind "skip" 不在 attnKinds 內）——它們是「本次沒判」，
  // 不是「判出問題」；但必須存在於表中，否則「全部 N」會少掉整批項次而無人察覺。
  const COVERAGE_CELL = {
    not_applicable: { kind: "skip", note: "不適用" },
    no_evidence: { kind: "skip", note: "未填" },
  };
  const COVERAGE_GIST = {
    not_applicable: "標示不適用，未送語意判讀",
    no_evidence: "佐證欄空白，未送語意判讀",
  };
  const COVERAGE_NOTE = {
    not_applicable: "提案單位標示此項「不適用」，故未送語意判讀。"
      + "不適用主張是否成立由治理人員認定，AI 未做任何判斷。",
    no_evidence: "佐證欄空白，無可判讀內容，故未送語意判讀；狀態非合規結論。",
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
    classify: null,     // /api/classify 回應
    kinds: {},          // index -> 使用者確認或修正後的 kind
    review: null,       // /api/review 回應
    role: "proposer",  // proposer | reviewer
    workflowStage: "proposal",
    filter: "info",     // 【彙整參考】「全部」後備分頁用的舊版嚴重度濾鏡（保留原行為）
    open: {},           // finding id -> bool（扁平清單 / 彙整卡共用的展開狀態）
    rowOpen: {},        // 檢核項狀態總覽表：itemId -> 是否展開
    view: "defects",   // defects | matrix
    openDefect: {},     // 缺陷清單：code -> 是否展開
    decisions: {},      // itemId -> pending | accept | reject | na
    showUniform: false,
    layers: [],         // 串流中的最新分層狀態
    partial: null,      // L1 中間報告；final 到達後清空
    refOpen: false,     // 【彙整參考】卡片是否展開
    refTab: null,       // 【彙整參考】目前分頁 key（aggregate/regref/passlog/all）
    classifying: false,
    reviewing: false,
    error: null,
  };

  const app = document.getElementById("app");
  const fileInput = document.getElementById("file-input");
  const mask = document.getElementById("mask");
  const toastEl = document.getElementById("toast");
  let decisionSaveTimer = null;
  let decisionRevision = 0;
  let decisionSaveChain = Promise.resolve();
  let noticeTriggerToken = null;
  let renderRevision = 0;

  // ── 工具 ───────────────────────────────────────────────────────
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
  }

  const FOCUS_DATA_KEYS = [
    "action", "item", "key", "code", "id", "view", "tab", "filter", "step", "index",
  ];
  const FOCUS_CLASS_KEYS = ["mobile", "mx-toggle", "mx-caret", "gv-tab", "gv-filter"];

  function captureFocusToken(element = document.activeElement) {
    const target = element && element.closest ? element.closest("[data-action]") : null;
    if (!target || !app.contains(target)) return null;
    const data = {};
    FOCUS_DATA_KEYS.forEach((key) => {
      if (target.dataset[key] != null) data[key] = target.dataset[key];
    });
    return {
      tag: target.tagName,
      data,
      classKey: FOCUS_CLASS_KEYS.find((key) => target.classList.contains(key)) || null,
      text: target.textContent.replace(/\s+/g, " ").trim().slice(0, 80),
    };
  }

  function restoreFocusToken(token) {
    if (!token) return;
    let candidates = [...app.querySelectorAll("[data-action]")].filter((target) =>
      target.tagName === token.tag && Object.entries(token.data)
        .every(([key, value]) => target.dataset[key] === value));
    if (token.classKey) {
      candidates = candidates.filter((target) => target.classList.contains(token.classKey));
    }
    const target = candidates.find((candidate) =>
      candidate.textContent.replace(/\s+/g, " ").trim().slice(0, 80) === token.text)
      || candidates[0];
    if (!target || target.disabled) return;
    try { target.focus({ preventScroll: true }); } catch (_) { target.focus(); }
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

  // 極簡 GFM 表格渲染；finding message 目前多為純文字，非表格內容保留換行。
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
      shortName: null,
      lifecycle: null,
      rowIndex: null,
      propChars: null,
      goliveChars: null,
      coverage: null,     // "assessable" | "not_applicable" | "no_evidence"（來自名冊）
      naReason: null,     // 不適用理由（提案單位填在佐證欄時）
      // 提案單位填寫的原文（來自名冊）：治理人員要拿它對照 AI 判讀，不是拿來重新判讀。
      description: null,      // D 檢查項目
      propText: null,         // I 提案階段佐證說明
      goliveText: null,       // J 上線階段佐證說明（提案階段為 null）
      goliveDiff: null,       // "missing" | "same" | "changed"（提案階段為 null）
      findings: [],
      cells: {
        prop: { kind: "none", note: "" },      // 提案階段內容品質
        golive: { kind: "none", note: "" },    // 上線階段內容品質
        evidence: { kind: "none", note: "" },  // 佐證文件是否支持敘述
        reg: { kind: "none", note: "" },       // 法規條文符合性
      },
      refs: "",
      assessment: null,
    };
  }

  // 摘要欄：優先序為「法規缺口 → 佐證問題 → 內容問題 → 首個附掛 finding 標題」。
  // 後備取 title 而非 message：message 是整句說明文，塞進摘要欄變雜訊；
  // title 本來就是精煉字樣（如「上線階段佐證空白」）。
  // 注意：須在 applyRuleCellFallback 之前呼叫，讓儲存格 note 只反映判讀結果。
  function rowGist(row) {
    if (row.cells.reg.kind === "warn" && row.cells.reg.note) return row.cells.reg.note;
    if (row.cells.evidence.kind === "warn" && row.cells.evidence.note) return row.cells.evidence.note;
    if (row.cells.golive.note) return row.cells.golive.note;
    if (row.cells.prop.note) return row.cells.prop.note;
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

  // 未送判讀的項次補灰底字樣。最後才套用，讓判讀結果與規則式警示都優先於「本次沒判」。
  function applyCoverageCells(row) {
    const cell = COVERAGE_CELL[row.coverage];
    if (!cell) return;
    ["prop", "golive", "evidence", "reg"].forEach((k) => {
      if (row.cells[k].kind === "none") row.cells[k] = { ...cell };
    });
  }

  // 上線階段兩欄都要有內容：語意判讀只會落在其中一欄（見 buildMatrix 的 stageColumn），
  // 另一欄若留空，治理人員會看到整欄「—」而以為表壞了。補的是**事實**（有沒有填、與提案差在哪），
  // 不是判讀結果，故僅填 kind === "none" 的格，且排在名冊灰字之後（見 buildMatrix 的呼叫順序）。
  function applyStageTextCells(row, stage) {
    if (!isGoliveStage(stage)) return;
    if (row.cells.prop.kind === "none" && row.propText) {
      row.cells.prop = { kind: "ok", note: "已填寫" };
    }
    const diff = DIFF_CELL[row.goliveDiff];
    if (row.cells.golive.kind === "none" && diff) row.cells.golive = { ...diff };
  }

  // 需求：內容、法規都通過，只是沒找到佐證文件時，佐證欄用通過色（字樣仍寫「未找到佐證」）。
  // 「沒附佐證」與「內容有問題」是兩件事，混用同一個琥珀警示會讓治理人員每列都得點開才知道沒事。
  // 只在「其他欄位都乾淨」時放行；後端 finding 與 decision_status 不動，明細仍看得到原因。
  const EVIDENCE_RELAXABLE_NOTE = "未找到佐證";

  function relaxEvidenceCell(row) {
    const ev = row.cells.evidence;
    if (ev.kind !== "warn" || ev.note !== EVIDENCE_RELAXABLE_NOTE) return;
    const othersClean = ["prop", "golive", "reg"]
      .every((k) => row.cells[k].kind === "ok" || row.cells[k].kind === "none");
    if (!othersClean) return;
    if (row.findings.some((f) => f.severity === "error")) return;
    row.cells.evidence = { kind: "okinfo", note: EVIDENCE_RELAXABLE_NOTE };
    row.evidenceRelaxed = true;
  }

  // 這一列「問題出在哪一欄」的集合：先看狀態格，再補上個別 finding 代碼
  // （關閉語意判讀時，規則式 finding 代碼是唯一線索）。
  const CAUSE_KEYS = ["golive", "prop", "evidence", "diff", "reg"];

  // 佐證欄放行時，對應的 warn finding 也要一併排除，否則整列仍被 finding 拉回「有問題」。
  const isRelaxedEvidenceFinding = (row, f) =>
    row.evidenceRelaxed && f.code === "F03.EVIDENCE_NOT_SUPPORTED";

  function rowCauses(row) {
    const causes = new Set();
    if (row.cells.golive.kind === "warn") causes.add("golive");
    if (row.cells.prop.kind === "warn") causes.add("prop");
    if (row.cells.evidence.kind === "warn") causes.add("evidence");
    if (row.cells.reg.kind === "warn") causes.add("reg");
    row.findings.forEach((f) => {
      if (isRelaxedEvidenceFinding(row, f)) return;
      const c = CAUSE_CODE_MAP[f.code];
      if (c) causes.add(c);
    });
    return causes;
  }

  function computePrimaryCause(attentionRows) {
    const tally = Object.fromEntries(CAUSE_KEYS.map((k) => [k, 0]));
    attentionRows.forEach((row) => { row.causes.forEach((c) => { tally[c] += 1; }); });
    let best = null;
    let bestN = 0;
    CAUSE_KEYS.forEach((k) => { if (tally[k] > bestN) { best = k; bestN = tally[k]; } });
    return best ? CAUSE_LABELS[best] : "—";
  }

  // 單一入口：一次掃過所有 findings，產出總覽表列、其他待處理、彙整參考四桶、統計。
  function buildMatrix(report) {
    const rowMap = new Map();
    const others = [];
    // 降級訊號由後端 notices 契約決定；前端不維護第二份代碼表，避免新增 notice 時漏顯示。
    const notices = Array.isArray(report.notices) ? [...report.notices] : [];
    const noticeCodes = new Set(notices.map((finding) => finding.code));
    const reference = { aggregate: [], regref: [], passlog: [], all: [] };

    // 先鋪名冊：總覽表是「檢核表的視圖」，每一個檢核項都該有一列。只靠 findings/assessment
    // 長列的話，標示不適用或佐證全空的項次會整列消失，連「全部 N」都看不到（v1 舊回應無此
    // 欄位，rowMap 留空即退回原本的長列方式）。
    (report.checklist_roster || []).forEach((entry) => {
      const row = newMatrixRow(entry.item_id, entry.topic);
      row.coverage = entry.coverage;
      row.naReason = entry.reason || null;
      row.description = entry.description || null;
      row.shortName = entry.short_name || null;
      row.lifecycle = entry.lifecycle || null;
      row.rowIndex = entry.row_index || null;
      row.propChars = entry.evidence_proposal_chars == null ? null : entry.evidence_proposal_chars;
      row.goliveChars = entry.evidence_golive_chars == null ? null : entry.evidence_golive_chars;
      row.propText = entry.evidence_proposal_text || null;
      row.goliveText = entry.evidence_golive_text || null;
      row.goliveDiff = entry.golive_diff || null;
      rowMap.set(entry.item_id, row);
    });

    report.findings.forEach((f) => {
      // 降級訊號先攔下：它可能帶著某一項次的 location（如 LLM 中止取 chunk[0].loc），
      // 若不先攔，會被下面的 item-location 分流埋進那一列的收合明細裡。
      if (noticeCodes.has(f.code)) return;
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
    reference.all = report.findings.filter((finding) => !noticeCodes.has(finding.code))
      .sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);

    const assessments = report.item_assessments || [];
    // 關閉語意判讀（或判讀整段失敗）時，規則式 finding 是總覽表唯一的內容來源。
    const noAssessments = assessments.length === 0;

    assessments.forEach((assessment) => {
      let row = rowMap.get(assessment.item_id);
      if (!row) {
        row = newMatrixRow(assessment.item_id, assessment.profile);
        rowMap.set(assessment.item_id, row);
      }
      row.assessment = assessment;
      // assessed === false 代表模型從未判讀此項（端點失敗／回應缺漏／Prompt 未就緒）。
      // 這種列的 evidence_status 只是佔位預設值，不可畫成「未找到佐證」的警示。
      if (assessment.assessed === false) {
        const unknown = { kind: "undet", note: "未判讀" };
        row.cells[report.workflow_stage === "proposal" ? "prop" : "golive"] = unknown;
        row.cells.evidence = unknown;
        row.cells.reg = unknown;
        return;
      }
      const contentBad = assessment.completeness === "incomplete" ||
        ["vague", "unreasonable"].includes(assessment.reasonableness);
      const contentUnknown = assessment.completeness === "undetermined" ||
        assessment.reasonableness === "undetermined";
      const contentCell = contentBad
        ? { kind: "warn", note: "內容需補強" }
        : contentUnknown
          ? { kind: "undet", note: "無法判定" }
          : { kind: "ok", note: "內容完整" };
      const stageColumn = report.workflow_stage === "proposal" ? "prop" : "golive";
      row.cells[stageColumn] = contentCell;

      if (assessment.retrieval_complete === false) {
        row.cells.evidence = { kind: "undet", note: "檢索未完整" };
      } else if (assessment.evidence_status === "supported") {
        row.cells.evidence = { kind: "ok", note: "有佐證" };
      } else if (assessment.evidence_status === "unreadable") {
        row.cells.evidence = { kind: "undet", note: "文件無法解析" };
      } else if (["contradicted", "conflicting_sources"].includes(assessment.conflict_status)) {
        // 佐證衝突比「找不到佐證」更值得看，故蓋過 evidence_status 的字樣。
        row.cells.evidence = { kind: "warn", note: "佐證有衝突" };
      } else {
        row.cells.evidence = {
          kind: "warn",
          note: assessment.evidence_status === "partial" ? "部分佐證" : "未找到佐證",
        };
      }

      if (assessment.regulation_gap === "covered") {
        row.cells.reg = { kind: "ok", note: "已涵蓋" };
      } else if (assessment.regulation_gap === "gap") {
        row.cells.reg = { kind: "warn", note: "條文缺口" };
      } else {
        row.cells.reg = { kind: "undet", note: "無條文對照" };
      }
      // 引用欄同時放法規條號與佐證出處：治理人員要的是「依據在哪」，兩者都是依據。
      const citationRefs = (assessment.citations || []).map((citation) =>
        `${citation.document_id} ${citation.locator_type}:${citation.locator_value}`);
      row.refs = [...(assessment.regulation_refs || []), ...citationRefs].join("、");
    });

    const rows = [...rowMap.values()];
    rows.forEach((row) => {
      // 順序有意為之：先算摘要（此時儲存格 note 只含彙整表判讀結果），再補規則式狀態格
      // （避免後備填入的「空白」note 蓋掉更精煉的 finding 標題摘要），最後才統計注意/嚴重度。
      row.gist = rowGist(row);
      applyRuleCellFallback(row);
      applyCoverageCells(row);
      // 排在 applyCoverageCells 之後：不適用／未填佐證的列已整列標成灰字，
      // 不可再被「已填寫」的綠勾蓋掉（那會讓未判讀的列看起來像通過）。
      applyStageTextCells(row, report.workflow_stage);
      // 佐證放行必須排在所有狀態格都填完之後：它的前提正是「其他欄位都乾淨」。
      relaxEvidenceCell(row);
      if (row.gist === "—" && COVERAGE_GIST[row.coverage]) row.gist = COVERAGE_GIST[row.coverage];
      const cellKeys = ["prop", "golive", "evidence", "reg"];
      // warn（有問題）、err（⚠️ 判讀失敗）、undet（❓ 無法判定）三種儲存格都代表「需人工確認」：
      // err/undet 是「AI 判不了，請人看」，同樣不能在預設「只看有問題」濾鏡下被藏掉。
      const attnKinds = { warn: 1, err: 1, undet: 1 };
      const anyCellAttn = cellKeys.some((k) => attnKinds[row.cells[k].kind]);
      const countedFindings = row.findings.filter((f) => !isRelaxedEvidenceFinding(row, f));
      const findingSev = maxSeverity(countedFindings);
      const anyFindingWarnErr = findingSev === "error" || findingSev === "warn";
      // 有需注意的儲存格／有 warn+ 的附掛 finding／或完全沒有判讀結果時任何附掛 finding 都算數
      // （關閉判讀時，規則式的佐證空白 finding 是唯一線索，不能被漏掉）。
      row.hasAttention = anyCellAttn || anyFindingWarnErr
        || (noAssessments && countedFindings.length > 0);
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
    return { rows, others, notices, reference, stats, stage: report.workflow_stage };
  }

  async function postForm(url, formData) {
    const res = await fetch(url, { method: "POST", body: formData });
    if (!res.ok) {
      throw new Error(await responseDetail(res));
    }
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

  function reviewFormData() {
    const fd = new FormData();
    state.files.forEach((file) => fd.append("files", file, file.name));
    const confirmed = state.classify.files.map((file) =>
      state.kinds[file.index] != null ? state.kinds[file.index] : file.kind);
    fd.append("kinds", JSON.stringify(confirmed));
    fd.append("workflow_stage", state.workflowStage);
    return fd;
  }

  function updateLayer(event) {
    const current = state.layers.length ? [...state.layers] : [
      { id: "L1", label: "規則檢查", state: "todo", elapsed_ms: null },
      { id: "L2", label: "法規／佐證檢索", state: "todo", elapsed_ms: null },
      { id: "L3", label: "敘述充分性判讀", state: "todo", elapsed_ms: null },
    ];
    const index = current.findIndex((layer) => layer.id === event.id);
    const prior = index >= 0 ? current[index] : { id: event.id, label: event.id };
    const next = {
      ...prior,
      ...event,
      label: event.label || prior.label,
      elapsed_ms: event.elapsed_ms == null ? prior.elapsed_ms : event.elapsed_ms,
    };
    if (index >= 0) current[index] = next;
    else current.push(next);
    state.layers = current;
  }

  async function postReviewStream(formData, onEvent) {
    const res = await fetch("/api/review/stream", { method: "POST", body: formData });
    if (!res.ok) throw new Error(await responseDetail(res));
    if (!res.body || !res.body.getReader) throw new Error("瀏覽器不支援串流回應。");

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let finalSeen = false;

    const consume = (frame) => {
      const payload = frame.split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!payload) return;
      const event = JSON.parse(payload);
      if (event.kind === "error") {
        const error = new Error(event.detail || "串流審查失敗。");
        error.reviewFailure = true;
        throw error;
      }
      if (event.kind === "final") finalSeen = true;
      onEvent(event);
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      frames.forEach(consume);
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!finalSeen) throw new Error("串流提前結束，未收到完整報告。");
  }

  function reachableStep() {
    if (state.review) return 4;
    if (state.classify) return 2;
    return 1;
  }

  function invalidateReport() {
    clearTimeout(decisionSaveTimer);
    state.review = null;
    state.partial = null;
    state.layers = [];
    state.open = {};
    state.rowOpen = {};
    state.openDefect = {};
    state.decisions = {};
    decisionRevision = 0;
    state.view = "defects";
    state.showUniform = false;
    state.refOpen = false;
    state.refTab = null;
  }

  function currentClassifications() {
    const seen = new Set();
    return state.classify.files.map((file) => {
      const kind = state.kinds[file.index] ?? file.kind;
      let flag = null;
      if (kind === "ignore") {
        flag = file.flag === "unknown" ? "unknown" : "ignored";
      } else if (["f01", "f02", "f03"].includes(kind)) {
        if (seen.has(kind)) flag = "dup";
        else seen.add(kind);
      }
      return { file, kind, flag };
    });
  }

  // ── 動作 ───────────────────────────────────────────────────────
  function onFilesChosen(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    invalidateReport();
    state.files = files;
    state.classify = null;
    state.kinds = {};
    state.error = null;
    render();
  }

  async function goClassify() {
    if (!state.files.length || state.classifying || state.reviewing) return;
    invalidateReport();
    state.error = null;
    state.classifying = true;
    render();
    const fd = new FormData();
    state.files.forEach((file) => fd.append("files", file, file.name));
    try {
      const data = await postForm("/api/classify", fd);
      state.classify = data;
      state.kinds = {};
      data.files.forEach((file) => { state.kinds[file.index] = file.kind; });
      state.step = 2;
    } catch (error) {
      state.error = error.message;
    }
    state.classifying = false;
    render();
  }

  async function startReview() {
    if (!state.classify || state.classifying || state.reviewing) return;
    invalidateReport();
    state.error = null;
    state.reviewing = true;
    state.step = 3;
    state.layers = [
      { id: "L1", label: "規則檢查", state: "running", elapsed_ms: null },
      { id: "L2", label: "法規／佐證檢索", state: "todo", elapsed_ms: null },
      { id: "L3", label: "敘述充分性判讀", state: "todo", elapsed_ms: null },
    ];
    render();
    let partialReceived = false;
    let finalReceived = false;
    try {
      await postReviewStream(reviewFormData(), (event) => {
        if (event.kind === "layer") {
          updateLayer(event);
          render();
        } else if (event.kind === "partial" && event.report) {
          partialReceived = true;
          applyReport(event.report, true);
        } else if (event.kind === "final" && event.report) {
          finalReceived = true;
          applyReport(event.report, false);
        }
      });
    } catch (error) {
      // 已收到 partial 就代表這次審查已建立 review_id，也可能已執行昂貴的 LLM。
      // 此時保留畫面與覆核決定，絕不重送成另一筆審查；只有第一幀前的傳輸不相容才 fallback。
      if (finalReceived) return;
      if (partialReceived || error.reviewFailure) {
        state.error = partialReceived
          ? `後續串流中斷，已保留目前結果與覆核決定：${error.message}`
          : error.message;
        state.reviewing = false;
        render();
        return;
      }
      try {
        applyReport(await postForm("/api/review", reviewFormData()), false);
      } catch (fallbackError) {
        state.error = fallbackError.message;
        state.reviewing = false;
        render();
      }
    }
  }

  // 套用 L1 中間報告或最終報告。中間報告先可用；final 只補資料，不清掉使用者已下的覆核決定。
  function applyReport(data, partial = false) {
    const sameReview = !!(state.review && state.review.review_id &&
      state.review.review_id === data.review_id);
    state.review = data;
    state.partial = partial ? data : null;
    if ((data.layers || []).length) state.layers = data.layers;

    if (!sameReview) {
      const open = {};
      (data.findings || []).forEach((f) => { if (f.severity === "error") open[f.id] = true; });
      state.open = open;
      state.openDefect = {};
      state.view = "defects";
      state.showUniform = false;
    }
    const m = buildMatrix(data);
    if (!sameReview) {
      // 預設停在【紀錄】（彙整表所在，供「彙整參考→全部」後備分頁沿用）；
      // 後續 partial/final 不可把使用者已選的分頁重設掉。
      state.filter = data.info_count > 0 ? "info" : "all";
      const rowOpen = {};
      m.rows.forEach((row) => { if (row.rowSeverity === "error") rowOpen[row.itemId] = true; });
      state.rowOpen = rowOpen;
      state.refOpen = false;
      state.refTab = defaultRefTab(m.reference);
    }

    const decisions = sameReview ? { ...state.decisions } : {};
    (data.checklist_roster || []).forEach((entry) => {
      if (!DECISION_OPTIONS.some((option) => option.value === decisions[entry.item_id])) {
        decisions[entry.item_id] = "pending";
      }
    });
    state.decisions = decisions;

    state.reviewing = partial;
    if (!sameReview) state.step = 3;
    render();
    if (!partial && data.review_id) loadStoredDecisions(data.review_id);
  }

  async function loadStoredDecisions(reviewId) {
    const startedAtRevision = decisionRevision;
    try {
      const res = await fetch(`/api/review/${encodeURIComponent(reviewId)}/decisions`);
      if (!res.ok) return;
      const payload = await res.json();
      if (!state.review || state.review.review_id !== reviewId
          || decisionRevision !== startedAtRevision) return;
      const stored = payload.decisions || {};
      let changed = false;
      Object.keys(state.decisions).forEach((itemId) => {
        if (DECISION_OPTIONS.some((option) => option.value === stored[itemId])) {
          if (state.decisions[itemId] !== stored[itemId]) {
            state.decisions[itemId] = stored[itemId];
            changed = true;
          }
        }
      });
      if (changed) render();
    } catch (_) { /* 決定讀取失敗不影響報告本體 */ }
  }

  function scheduleDecisionSave() {
    clearTimeout(decisionSaveTimer);
    if (!state.review || !state.review.review_id) return;
    const reviewId = state.review.review_id;
    const payload = {
      decisions: { ...state.decisions },
      subject: state.review.subject || null,
      note: null,
    };
    decisionSaveTimer = setTimeout(() => {
      // POST 必須序列化；若使用者快速連改，同一 review 的舊回應不能晚到並覆蓋新決定。
      decisionSaveChain = decisionSaveChain.then(() => saveDecisions(reviewId, payload));
    }, 250);
  }

  async function saveDecisions(reviewId, payload) {
    try {
      const res = await fetch(`/api/review/${encodeURIComponent(reviewId)}/decisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await responseDetail(res));
    } catch (error) {
      if (state.review && state.review.review_id === reviewId) {
        toast(`覆核決定尚未儲存：${error.message}`);
      }
    }
  }

  function reset() {
    clearTimeout(decisionSaveTimer);
    state.step = 1;
    state.files = [];
    state.classify = null;
    state.kinds = {};
    invalidateReport();
    state.filter = "info";  // 與初始預設一致（落地分頁在 applyReport 依紀錄數再決定）
    state.classifying = false;
    state.reviewing = false;  // 防禦性清空：正常路徑 applyReport/錯誤處理已先清，這裡保持完整
    state.error = null;
    fileInput.value = "";
    render();
  }

  function gotoStep(n) {
    if (n > reachableStep()) return;
    if (state.review && n === 3) state.view = "defects";
    if (state.review && n === 4) state.view = "matrix";
    state.step = n;
    render();
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
      const sep = n < STEP_LABELS.length ? `<span class="gv-step-sep">›</span>` : "";
      return `<button class="gv-step ${cls}${can ? "" : " disabled"}" data-action="goto" data-step="${n}"
        ${n === cur ? 'aria-current="step"' : ""} ${can ? "" : 'disabled aria-disabled="true"'}>
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
            <div class="sub">批次上傳自動分類 · 規則檢查與語意判讀</div></div>
        </div>
        <div class="pills">
          <a class="pill" href="/assist">AI 協填（提案單位）</a>
          <div class="pill"><span class="dot"></span>地端執行 · 資料不外送</div>
        </div>
      </div>
      <div class="gv-steps">${steps}</div>
    </div>`;
  }

  function renderError() {
    return state.error ? `<div class="gv-error" role="alert">⚠ ${esc(state.error)}</div>` : "";
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
            <div class="small">支援 F01 / F02 / F03 與佐證（可多檔）· 或點此選擇檔案</div></div>
          <span class="cta">選擇檔案</span>
        </button>
        <div class="gv-aside"><h3>送件包應包含</h3>
          ${CHECKLIST.map((item) => `<div class="item"><span class="tag">${esc(item.tag)}</span>
            <div><div class="name">${esc(item.name)}</div><div class="note">${esc(item.note)}</div></div></div>`).join("")}
        </div></div>`;
    } else {
      const rows = state.files.map((file) => `<div class="gv-row">
        <span class="ic">${fileIcon(file.name)}</span>
        <div class="meta"><div class="fname">${esc(file.name)}</div></div>
        <span class="fsize">${fmtSize(file.size)}</span></div>`).join("");
      body = `<div class="gv-filelist">
        <div class="head"><span>已選擇 ${state.files.length} 個檔案</span>
          <button class="btn-small" data-action="reset">重新選擇</button></div>
        ${rows}</div>
        <div class="row-end"><button class="btn-primary" data-action="classify">下一步：確認分類　→</button></div>`;
    }
    return `<div>
      <h1 class="gv-h1">上傳送件包</h1>
      <p class="gv-lead">一次拖入提案單位提交的所有檔案，系統會自動辨識 F01 / F02 / F03 與佐證文件。審查前仍可逐檔確認或修正。</p>
      ${renderError()}${body}</div>`;
  }

  // ── 渲染：Step 2 分類 ──────────────────────────────────────────
  function renderStep2() {
    const classification = state.classify;
    const current = currentClassifications();
    const hasReviewableFile = current.some(({ kind }) => kind !== "ignore");
    const counts = { f01: 0, f02: 0, f03: 0, supporting: 0, ignore: 0 };
    current.forEach(({ kind }) => {
      counts[kind] = (counts[kind] || 0) + 1;
    });
    const flagged = current.filter(({ flag }) => flag).length;
    const chip = (label, fg, bg, border) =>
      `<span class="gv-chip" style="color:${fg};background:${bg};border-color:${border};">${esc(label)}</span>`;
    const chips = [
      chip(`共 ${classification.files.length} 個檔案`, "var(--text)", "var(--surface)", "var(--border)"),
      chip(`表單 ${counts.f01 + counts.f02 + counts.f03} 份`, "var(--accent)", "var(--accent-soft)", "var(--accent-soft)"),
      chip(`佐證 ${counts.supporting} 份`, "var(--muted)", "var(--bg)", "var(--border)"),
    ];
    if (flagged) {
      chips.push(chip(`需確認 ${flagged} 項`, "var(--warn-fg)", "var(--warn-bg)", "var(--warn-border)"));
    }

    const optionHtml = (selected) => classification.kinds.map((option) =>
      `<option value="${esc(option.value)}"${option.value === selected ? " selected" : ""}>${esc(option.label)}</option>`).join("");
    const rows = current.map(({ file, kind, flag }) => {
      const flagLabel = flag === "dup" ? "重複" : flag === "unknown" ? "無法辨識" :
        flag === "ignored" ? "已忽略" : "";
      const flagHtml = flagLabel ? `<span class="gv-flag">${flagLabel}</span>` : "";
      return `<div class="gv-classify-row${flag ? " flagged" : ""}">
        <div class="line">
          <span class="ic">${fileIcon(file.filename)}</span>
          <div class="body"><div class="top"><span class="fname">${esc(file.filename)}</span>${flagHtml}</div>
            <div class="reason">${esc(file.reason)}</div></div>
          <select class="gv-select" data-action="set-kind" data-index="${file.index}"
            aria-label="檔案 ${esc(file.filename)} 的分類">${optionHtml(kind)}</select>
        </div></div>`;
    }).join("");

    const reviewOptions = `<div class="gv-review-options">
      <label>使用角色
        <select class="gv-select" data-action="set-role">
          <option value="proposer"${state.role === "proposer" ? " selected" : ""}>提案者</option>
          <option value="reviewer"${state.role === "reviewer" ? " selected" : ""}>審查者</option>
        </select>
      </label>
      <label>流程階段
        <select class="gv-select" data-action="set-stage">
          ${Object.entries(STAGE_LABELS).map(([value, label]) =>
            `<option value="${value}"${state.workflowStage === value ? " selected" : ""}>${label}</option>`
          ).join("")}
        </select>
      </label>
    </div>`;

    return `<div>
      <h1 class="gv-h1">自動分類<span class="light">（可修正）</span></h1>
      <p class="gv-lead">Excel 依工作表名稱判定，其他檔案列為佐證。請逐一確認；標記為「重複」或「無法辨識」的項目尤其需要人工判斷。</p>
      ${renderError()}
      <div class="gv-chips">${chips.join("")}</div>
      ${reviewOptions}
      <div class="gv-filelist">${rows}</div>
      <div class="row-split">
        <button class="btn-ghost" data-action="goto" data-step="1">←　返回上傳</button>
        <button class="btn-primary" data-action="start-review"
          ${hasReviewableFile ? "" : 'disabled aria-disabled="true"'}>開始審查　→</button>
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
      <div class="fcode-line"><span class="fcode">${esc(f.code)}</span></div></div>`;
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

  const reviewedCount = () => Object.values(state.decisions)
    .filter((value) => value !== "pending").length;

  function renderLayers() {
    const layers = state.layers.length ? state.layers : ((state.review && state.review.layers) || []);
    if (!layers.length) return "";
    const stateClass = { done: "done", running: "run", todo: "", skipped: "skipped" };
    const stateLabel = { done: "已完成", running: "進行中", todo: "待執行", skipped: "已略過" };
    return `<div class="gv-layers" aria-label="審查分層進度">
      ${layers.map((layer) => {
        const elapsed = layer.elapsed_ms == null ? "" : ` · ${(layer.elapsed_ms / 1000).toFixed(layer.elapsed_ms < 10000 ? 1 : 0)} 秒`;
        return `<div class="gv-layer ${stateClass[layer.state] || ""}"><span class="d"></span>
          ${esc(layer.id)} ${esc(layer.label)} · ${esc(stateLabel[layer.state] || layer.state)}${esc(elapsed)}</div>`;
      }).join("")}
      <button class="rerun" data-action="rerun-l3" title="送件檔已刪除，需重新上傳原檔"
        ${state.reviewing ? " disabled" : ""}>重跑語意判讀（需重送）</button>
    </div>`;
  }

  function fallbackVerdict(r, m) {
    const blocked = r.decision_status === "changes_required" || r.error_count > 0;
    const passed = r.decision_status === "approved";
    const label = blocked ? "初判：不可放行 · 需補件"
      : passed ? "初判：可進入覆核" : "初判：需人工覆核後放行";
    const because = m.stats.total
      ? `${m.stats.attention}/${m.stats.total} 個檢核項需人工確認；主因：${m.stats.primaryCause}。`
      : "本次結果仍須治理人員與三遵人工覆核。";
    return { code: blocked ? "blocked" : passed ? "passed" : "review", label, because };
  }

  function renderSummary(r, m) {
    const verdict = r.verdict || fallbackVerdict(r, m);
    const passed = verdict.code === "passed";
    const total = m.stats.total;
    const done = reviewedCount();
    const pending = Math.max(0, total - done);
    const risk = riskColors(r.risk_grade);
    const riskPct = r.risk_score == null ? "" : ` · ${r.risk_score.toFixed(0)}%`;
    const ruleDefects = (r.defects || []).filter((defect) => defect.layer === "L1" && defect.count);
    const ruleErrors = ruleDefects.filter((defect) => defect.severity === "error")
      .reduce((sum, defect) => sum + defect.count, 0);
    const l1 = (state.layers.length ? state.layers : (r.layers || []))
      .find((layer) => layer.id === "L1");
    const rulesTitle = l1 && l1.state !== "done" ? "規則層進行中"
      : ruleErrors ? `規則層 ${ruleErrors} 錯誤` : "規則層 ✓ 通過";
    const ruleNote = ruleDefects.length
      ? ruleDefects.slice(0, 3).map((defect) => `${defect.label} ${defect.count}`).join(" · ")
      : `${r.error_count || 0} 錯誤 · ${r.warn_count || 0} 提醒`;
    const defectCount = (r.defects || []).filter((defect) => !defect.trial)
      .reduce((sum, defect) => sum + (defect.count || 0), 0);

    return `<div class="gv-hero${passed ? " pass" : ""}">
      <div class="top">
        <div class="badge" aria-hidden="true">${passed ? "✓" : "▲"}</div>
        <div class="hero-copy">
          <div class="verdict">${esc(verdict.label)}</div>
          <div class="subline">受審：${esc(r.subject || "未標示")} · ${esc(r.form_type || "送件包")}</div>
          <div class="because">${esc(verdict.because)}</div>
        </div>
      </div>
      <div class="gv-tiles">
        <div class="gv-tile risk" style="background:${risk.bg};border-color:${risk.border};">
          <div class="n" style="color:${risk.fg};">${esc(r.risk_grade || "—")}</div>
          <div class="l" style="color:${risk.fg};">固有風險${esc(riskPct)}</div></div>
        <div class="gv-tile attn"><div class="n">${pending}</div><div class="l">待覆核</div></div>
        <div class="gv-tile"><div class="n">${done}</div><div class="l">已覆核</div></div>
        <div class="gv-tile rules${ruleErrors ? " error" : ""}"><div class="n">${esc(rulesTitle)}</div><div class="l">${esc(ruleNote)}</div></div>
      </div>
      <div class="foot">
        <button class="btn-primary" data-action="notice">產生補件通知${defectCount ? `（${defectCount} 項）` : ""}</button>
        <button class="btn-ghost" data-action="start-item-review">開始逐項覆核</button>
        <span class="note">⚠ 本報告為 AI 初判，最終判定權在治理人員與三遵</span>
      </div>
    </div>`;
  }

  function renderViewBar(m) {
    const total = m.stats.total;
    const done = reviewedCount();
    const pct = total ? Math.round(done / total * 100) : 0;
    return `<div class="gv-viewbar">
      <h2>審查結果</h2>
      <button class="gv-tab${state.view === "defects" ? " active" : ""}" data-action="view-tab"
        data-view="defects" role="tab" aria-selected="${state.view === "defects"}">缺陷清單 · 依類型</button>
      <button class="gv-tab${state.view === "matrix" ? " active" : ""}" data-action="view-tab"
        data-view="matrix" role="tab" aria-selected="${state.view === "matrix"}">檢核項總覽 · 依項次 ${total}</button>
      <div class="gv-progress"><span>${done}/${total} 已覆核</span>
        <span class="bar" aria-hidden="true"><i style="width:${pct}%"></i></span></div>
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

  function locatorText(locator) {
    if (!locator) return "—";
    const head = [locator.file, locator.sheet].filter(Boolean).join("·");
    const tail = [
      locator.cell,
      locator.page == null ? null : `p.${locator.page}`,
      locator.section,
    ].filter(Boolean).join(" ");
    return [head, tail].filter(Boolean).join(" ") || "—";
  }

  function renderCommonFacts(r) {
    const facts = r.common_facts || [];
    if (!facts.length) return "";
    return `<div class="gv-common">
      <span class="t">▲ ${facts.map((fact) => esc(fact.text)).join(" · ")}</span>
      <span class="s">${esc(facts[0].hint || "共通事實，不再逐列重複")}</span>
      <button class="a" data-action="notice">產生補件通知</button>
    </div>`;
  }

  function renderDefects(r) {
    const defects = r.defects || [];
    if (!defects.length) {
      return `<div class="gv-card"><div class="gv-matrix-empty">目前沒有可顯示的缺陷資料</div></div>`;
    }
    const severityMeta = {
      error: { cls: "st-err", mark: "！" },
      warn: { cls: "st-warn", mark: "▲" },
      info: { cls: "st-skip", mark: "i" },
      ok: { cls: "st-ok", mark: "✓" },
    };
    const rows = defects.map((defect) => {
      const open = !!state.openDefect[defect.code];
      const meta = severityMeta[defect.severity] || severityMeta.info;
      const source = SOURCE_TAGS[defect.source] || { cls: "tag-rule", label: defect.source || "來源" };
      const hits = (defect.hits || []).map((hit) => {
        const loc = locatorText(hit.locator);
        const title = loc === "—" ? "" : ` title="${esc(loc)}"`;
        return `<span class="hit"${title}><b>${esc(hit.key)}</b><span>${esc(hit.value)}</span></span>`;
      }).join("");
      const attention = defect.count > 0 && defect.severity !== "ok" ? " attn" : "";
      return `<div class="r r-def clickable${attention}${open ? " open" : ""}"
          role="button" tabindex="0" aria-expanded="${open}" data-action="defect-toggle" data-code="${esc(defect.code)}">
          <div><span class="${meta.cls}">${meta.mark} ${esc(defect.label)}</span></div>
          <div class="n-big">${defect.count}</div>
          <div class="c-basis cell-sub" title="${esc(defect.basis)}">${esc(defect.basis)}</div>
          <div class="c-loc"><span class="tag ${source.cls}">${esc(defect.layer)} ${esc(source.label)}</span>
            ${defect.trial ? '<span class="tag tag-trial">試辦</span>' : ""}</div>
          <div class="caret" aria-hidden="true">⌄</div>
        </div>
        ${open ? `<div class="detail plain">
          <h4>判定依據</h4><div>${esc(defect.basis)} · 信心：${esc(defect.confidence)}</div>
          ${hits ? `<h4>命中位置</h4><div class="hits">${hits}</div>` : ""}
          <h4>座標</h4><div class="loc">${esc(defect.locator_label || "—")}</div>
          <h4>說明</h4><div class="detail-note">${esc(defect.note || "—")}</div>
          ${(defect.codes || []).length ? `<div class="codes"><span>命中規則：</span>${defect.codes.map((code) => `<code>${esc(code)}</code>`).join("")}</div>` : ""}
        </div>` : ""}`;
    }).join("");

    const timing = (state.layers.length ? state.layers : (r.layers || [])).map((layer) =>
      `${layer.id} ${layer.state === "done" ? "已完成" : layer.state === "skipped" ? "已略過" : "進行中"}`)
      .join(" · ");
    return `<div class="gv-card">
      ${renderCommonFacts(r)}
      <div class="r r-def head"><div>缺陷類型</div><div class="n-big">筆數</div>
        <div class="c-basis">判定依據</div><div class="c-loc">來源</div><div></div></div>
      ${rows}
      <div class="gv-card-actions">
        <button class="btn-primary" data-action="copy-defects">複製缺失清單</button>
        <button class="btn-ghost" data-action="notice">產生補件通知</button>
        <span class="card-note">${esc(timing)}</span>
      </div>
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
    // 通過色但不加 ✓：字樣本身是「未找到佐證」，掛個勾會變成自相矛盾的「✓ 未找到佐證」。
    if (cell.kind === "okinfo") return `<span class="st-ok">${esc(cell.note)}</span>`;
    if (cell.kind === "undet") return `<span class="st-undet">？無法判定</span>`;
    // 灰底：本次未判讀，非合規結論也非警示，刻意與 ✓/▲/？ 拉開視覺層級。
    if (cell.kind === "skip") return `<span class="st-skip">${esc(cell.note || "未判讀")}</span>`;
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

  // 依換行與句末標點切句（不用 regex lookbehind，相容舊瀏覽器）；比對與標示都以句為單位。
  function splitSentences(text) {
    const out = [];
    let buf = "";
    for (const ch of String(text || "")) {
      buf += ch;
      if ("。；！？!?;\n".indexOf(ch) >= 0) {
        const done = buf.trim();
        if (done) out.push(done);
        buf = "";
      }
    }
    const tail = buf.trim();
    if (tail) out.push(tail);
    return out;
  }

  // 比對鍵：去掉所有空白，讓排版差異不被當成內容差異（與後端 golive_diff 同一套正規化）。
  const sentenceKey = (s) => s.split(/\s+/).join("");

  // 上線佐證中「提案沒寫過的句子」標成 <mark>；標示對象是原文，一律先 esc 再包標籤。
  function diffMarkHtml(goliveText, propText) {
    const seen = new Set(splitSentences(propText).map(sentenceKey));
    return splitSentences(goliveText)
      .map((s) => (seen.has(sentenceKey(s)) ? esc(s) : `<mark>${esc(s)}</mark>`))
      .join("");
  }

  function srcItemHtml(label, bodyHtml, noteHtml) {
    return `<div class="gv-src-item"><div class="gv-src-label">${esc(label)}</div>${
      noteHtml || ""}${bodyHtml ? `<div class="gv-src-text">${bodyHtml}</div>` : ""}</div>`;
  }

  // 展開列的「文件內容（參考）」：治理人員要拿原文對照 AI 判讀，不能只看結論。
  // 佐證文件內容是未信任的上傳內容，一律走 esc()。
  function sourceTextBlockHtml(row, stage) {
    const parts = [];
    if (row.description) parts.push(srcItemHtml("檢查項目", esc(row.description)));
    // 不適用列的提案佐證欄填的是「不適用理由」，上方「未送語意判讀」區塊已完整顯示，這裡不重複。
    if (row.propText && row.coverage !== "not_applicable") {
      parts.push(srcItemHtml("提案階段佐證說明", esc(row.propText)));
    }
    if (isGoliveStage(stage) && (row.goliveText || row.goliveDiff)) {
      const note = DIFF_LABELS[row.goliveDiff]
        ? `<div class="gv-src-note">${esc(DIFF_LABELS[row.goliveDiff])}</div>` : "";
      const body = !row.goliveText ? "" : row.goliveDiff === "changed"
        ? diffMarkHtml(row.goliveText, row.propText)
        : esc(row.goliveText);
      parts.push(srcItemHtml("上線階段佐證說明", body, note));
    }
    ((row.assessment && row.assessment.citations) || []).forEach((citation) => {
      parts.push(srcItemHtml(
        `佐證文件引用 · ${citation.document_id} ${citation.locator_type}:${citation.locator_value}`,
        esc(citation.quote || "（本次未取得原文片段）"),
      ));
    });
    if (!parts.length) return "";
    return `<div class="gv-mdetail-finding">
      <div class="gv-mdetail-head">文件內容（參考）</div>
      <div class="gv-src">${parts.join("")}</div>
    </div>`;
  }

  function decisionSelect(itemId, mobile = false) {
    const value = state.decisions[itemId] || "pending";
    const selected = DECISION_OPTIONS.find((option) => option.value === value) || DECISION_OPTIONS[0];
    return `<select class="dec ${selected.cls}${mobile ? " mobile" : ""}" data-action="decide"
      data-item="${esc(itemId)}" aria-label="項次 ${esc(itemId)} 的覆核決定">
      ${DECISION_OPTIONS.map((option) => `<option value="${option.value}"${option.value === value ? " selected" : ""}>${option.label}</option>`).join("")}
    </select>`;
  }

  function regulationCell(row) {
    if (["none", "skip"].includes(row.cells.reg.kind)) {
      return '<span class="st-skip">本次未判讀</span>';
    }
    return matrixCellHtml("reg", row.cells.reg);
  }

  function renderMatrixDetailRow(row, stage) {
    const rubric = ((state.review && state.review.rubrics) || {})[row.itemId];
    const lines = (rubric && rubric.lines) || [];
    const rubricHtml = lines.length ? `<h4>審查員檢核句<span class="tag tag-trial rubric-tag">L3 試辦</span></h4>
      <div class="rubric">${lines.map((line) => {
        const met = line.met === true || line.ok === true;
        return `<div><span class="${met ? "y" : "n"}">${met ? "✓" : "✗"}</span>
          <span>${esc(line.text || line.t)}${met ? "" : ' <span class="unmet">· 未提及</span>'}</span></div>`;
      }).join("")}</div>` : "";
    const quote = rubric && rubric.quote;
    const quoteLocator = rubric && (typeof rubric.quote_locator === "string"
      ? rubric.quote_locator : locatorText(rubric.quote_locator));
    const quoteHtml = quote ? `<h4>引用原文</h4><div class="quote">${esc(quote)}</div>
      ${quoteLocator && quoteLocator !== "—" ? `<div class="loc quote-loc">${esc(quoteLocator)}</div>` : ""}` : "";
    const codes = [...new Set([
      ...((rubric && rubric.codes) || []),
      ...(row.findings || []).map((finding) => finding.code),
    ])];
    const codesHtml = codes.length ? `<div class="codes"><span>命中規則：</span>
      ${codes.map((code) => `<code>${esc(code)}</code>`).join("")}</div>` : "";
    const coverage = COVERAGE_NOTE[row.coverage];
    const judgement = coverage || row.gist || "本項未發現可彙整的個別缺陷，仍須人工核對原文。";
    const source = quote ? "" : sourceTextBlockHtml(row, stage);
    return `<div class="detail">
      <h4>判讀（同因已合併）</h4><div>${esc(judgement)}${row.naReason ? `；提案單位理由：${esc(row.naReason)}` : ""}</div>
      ${rubricHtml}${quoteHtml}${source}${codesHtml}
      <div class="c-dec mobile-decision"><h4>覆核決定</h4>${decisionSelect(row.itemId, true)}</div>
    </div>`;
  }

  function renderMatrixRow(row, stage) {
    const open = !!state.rowOpen[row.itemId];
    const decision = state.decisions[row.itemId] || "pending";
    const context = [row.itemId, row.lifecycle, row.topic].filter(Boolean);
    const stageChars = isGoliveStage(stage) ? row.goliveChars : row.propChars;
    if (stageChars != null && row.findings.some((finding) =>
      finding.code === "F03.EVIDENCE_TOO_SHORT")) context.push(`佐證僅 ${stageChars} 字`);
    if (row.gist && row.gist !== "—") context.push(row.gist);
    const name = row.shortName || row.description || row.topic || `檢核項 ${row.itemId}`;
    return `<div class="r r-mx${decision === "pending" ? " attn" : ""}${open ? " open" : ""}">
      <button class="mx-toggle" data-action="row-toggle" data-key="${esc(row.itemId)}" aria-expanded="${open}">
        <span class="cell-title">${esc(name)}</span><span class="cell-sub">${esc(context.join(" · "))}</span>
      </button>
      <div>${regulationCell(row)}</div>
      <div class="c-dec">${decisionSelect(row.itemId)}</div>
      <button class="mx-caret" data-action="row-toggle" data-key="${esc(row.itemId)}"
        aria-label="${open ? "收合" : "展開"}項次 ${esc(row.itemId)}" aria-expanded="${open}"><span class="caret">⌄</span></button>
    </div>${open ? renderMatrixDetailRow(row, stage) : ""}`;
  }

  function renderMatrix(r, m) {
    const { rows, stage } = m;
    if (!rows.length) {
      return `<div class="gv-card"><div class="gv-matrix-empty">本次送件沒有 F03 檢核項</div></div>`;
    }
    const uniform = r.uniform_columns || [];
    return `<div class="gv-card">
      ${renderCommonFacts(r)}
      ${uniform.length ? `<div class="gv-uniform"><span>${uniform.length} 欄全表同值，已收起：</span>
        ${uniform.map((column) => `<code>${esc(column)}</code>`).join("")}
        <button class="btn-ghost uniform-toggle" data-action="uniform">${state.showUniform ? "收起" : "仍要展開"}</button>
      </div>${state.showUniform ? `<div class="detail plain uniform-detail">${uniform.map((column) => `<div>${esc(column)}</div>`).join("")}</div>` : ""}` : ""}
      <div class="r r-mx head"><div>檢核項</div><div>法規符合</div><div class="c-dec">覆核</div><div></div></div>
      ${rows.map((row) => renderMatrixRow(row, stage)).join("")}
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
    const teaserSummary = `判讀摘要 ${counts.aggregate}　法規對應 ${counts.regref}　通過紀錄 ${counts.passlog}`;
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

  function noticeText(r = state.review) {
    if (!r) return "";
    const sourceLabel = (source) => (SOURCE_TAGS[source] || { label: source || "來源不明" }).label;
    const defects = (r.defects || []).filter((defect) => defect.count > 0);
    const verdict = r.verdict || { label: "AI 初判，待人工覆核" };
    const lines = [
      "【AI 治理審查 · 補件通知（初判草稿）】",
      `受審案件：${r.subject || "未標示"}`,
      `初判結果：${verdict.label}（AI 初判，最終判定權在治理人員與三遵）`,
      "",
    ];
    defects.forEach((defect, index) => {
      const unit = (defect.hits || []).some((hit) => hit.item_id) ? "項" : "處";
      lines.push(`${index + 1}. ${defect.label}（${defect.count} ${unit}）— ${defect.basis}`);
      if ((defect.hits || []).length) {
        const hitText = defect.hits.map((hit) => {
          const locator = locatorText(hit.locator);
          return `${hit.key}（${hit.value}${locator === "—" ? "" : `；${locator}`}）`;
        }).join("、");
        lines.push(`   位置：${hitText}`);
      } else if (defect.locator_label && defect.locator_label !== "—") {
        lines.push(`   位置：${defect.locator_label}`);
      }
      lines.push(`   來源：${defect.layer} ${sourceLabel(defect.source)} · 信心 ${defect.confidence}${defect.trial ? " · 試辦" : ""}`);
      lines.push("");
    });

    const unmet = [];
    Object.entries(r.rubrics || {}).forEach(([itemId, rubric]) => {
      const missing = (rubric.lines || []).filter((line) => !(line.met === true || line.ok === true))
        .map((line) => line.text || line.t).filter(Boolean);
      if (missing.length) unmet.push(`${itemId}：${missing.join("、")}`);
    });
    if (unmet.length) {
      lines.push("審查員檢核句尚未回應（L3 試辦）：");
      unmet.forEach((line) => lines.push(`- ${line}`));
      lines.push("");
    }
    lines.push(defects.length
      ? "請就上列項目補充後重新送件；說明請具體交代做法、負責單位、執行頻率與異常處理方式。"
      : "本次未彙整出機械性缺陷，仍請治理人員與三遵完成實質內容覆核。");
    return lines.join("\n");
  }

  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toastEl.hidden = true; }, 1900);
  }

  async function copyText(text) {
    try {
      let copied = false;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try {
          await navigator.clipboard.writeText(text);
          copied = true;
        } catch (_) { /* 權限被拒時再退回地端 execCommand */ }
      }
      if (!copied) {
        let field = null;
        try {
          field = document.createElement("textarea");
          field.value = text;
          field.setAttribute("readonly", "");
          field.style.position = "fixed";
          field.style.opacity = "0";
          document.body.appendChild(field);
          field.select();
          copied = document.execCommand("copy");
        } finally {
          if (field && field.parentNode) field.parentNode.removeChild(field);
        }
      }
      if (!copied) throw new Error("copy unavailable");
      toast("已複製到剪貼簿");
    } catch (_) {
      toast("複製失敗，請在通知視窗中手動選取");
    }
  }

  function openNotice(trigger) {
    if (!mask || !state.review) return;
    noticeTriggerToken = captureFocusToken(trigger || document.activeElement);
    mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="notice-title">
      <header><h3 id="notice-title">補件通知（草稿）</h3>
        <div class="sub">由目前缺陷清單即時彙整 · 送出前請自行增修</div></header>
      <textarea id="notice-text" aria-label="補件通知草稿">${esc(noticeText())}</textarea>
      <footer>
        <button class="btn-primary" data-modal-action="copy-notice">複製全文</button>
        <button class="btn-ghost" data-modal-action="close">關閉</button>
        <span class="note">此文字僅為草稿，不代表最終判定</span>
      </footer>
    </div>`;
    mask.hidden = false;
    requestAnimationFrame(() => document.getElementById("notice-text")?.focus());
  }

  function closeNotice() {
    if (!mask) return;
    mask.hidden = true;
    mask.innerHTML = "";
    const token = noticeTriggerToken;
    noticeTriggerToken = null;
    requestAnimationFrame(() => restoreFocusToken(token));
  }

  function renderReport() {
    const r = state.review;
    const m = buildMatrix(r);
    return `<div>
      ${renderLayers()}
      ${renderSummary(r, m)}
      ${renderError()}
      ${renderNotices(m)}
      ${renderViewBar(m)}
      ${state.view === "defects" ? renderDefects(r) : renderMatrix(r, m)}
      ${renderOthers(m)}
      ${renderReviewerMeta(r)}
      ${renderReference(r, m)}
      <div class="gv-persistence-note">覆核決定僅供稽核軌跡與準確率統計；重新整理不會還原報告，重跑需重新上傳原檔。</div>
      <div class="gv-foot">
        <button class="btn-ghost" data-action="reset">↻　審查新的送件包</button>
        <div class="gv-foot-actions">
          <button class="btn-download" data-action="download">⬇ 下載 Markdown</button>
          <button class="btn-ghost" data-action="copy-defects">複製缺失清單</button>
          <button class="btn-primary" data-action="notice">產生補件通知</button>
        </div>
      </div></div>`;
  }

  function renderReviewLoading() {
    return `${renderLayers()}${renderError()}<div class="gv-card review-wait" role="status" aria-live="polite">
      <div class="review-wait-icon">⌁</div><div><strong>規則檢查進行中</strong>
      <p>第一層完成後，缺陷清單會立即出現；法規檢索與語意判讀會繼續在背景補齊。</p></div>
    </div>`;
  }

  function renderReviewerMeta(r) {
    if (state.role !== "reviewer") return "";
    const usage = r.usage_summary || {};
    const meta = r.review_run_meta || {};
    const documents = r.document_statuses || [];
    const prompts = meta.prompt_refs || [];
    return `<div class="gv-review-meta">
      <h2 class="gv-h2">審查執行資訊</h2>
      <div class="gv-meta-grid">
        <div><strong>判定</strong><span>${esc(r.decision_status || "—")}</span></div>
        <div><strong>流程階段</strong><span>${esc(STAGE_LABELS[r.workflow_stage] || r.workflow_stage || "—")}</span></div>
        <div><strong>模型呼叫</strong><span>主要 ${usage.review_llm_primary_calls || 0}／升級 ${usage.review_llm_escalation_calls || 0}</span></div>
        <div><strong>Token</strong><span>輸入 ${usage.input_tokens || 0}／輸出 ${usage.output_tokens || 0}</span></div>
        <div><strong>文件解析</strong><span>${documents.map((doc) => `${doc.document_id}:${doc.status}`).join("、") || "無佐證文件"}</span></div>
        <div><strong>Prompt 版本</strong><span>${prompts.map((prompt) => `${prompt.prompt_id}@${prompt.prompt_version}`).join("、") || "未執行"}</span></div>
      </div>
    </div>`;
  }

  function renderOverlay() {
    if (!state.classifying) return "";
    return `<div class="gv-overlay" role="status" aria-live="polite"><div class="box">
      <div class="spin"></div><div class="t">分類中…</div>
      <div class="s">正在讀取 Excel 工作表名稱；應用不會保存檔案，請求暫存會在處理後清除。</div>
      </div></div>`;
  }

  // ── 主渲染 ─────────────────────────────────────────────────────
  function render() {
    const focusToken = captureFocusToken();
    const thisRender = ++renderRevision;
    let content;
    if (state.step === 1) content = renderStep1();
    else if (state.step === 2) content = renderStep2();
    else content = state.review ? renderReport() : renderReviewLoading();
    const busy = state.classifying || (state.reviewing && !state.review);
    app.setAttribute("aria-busy", busy ? "true" : "false");
    app.innerHTML = `${renderHeader()}<div class="gv-main">${content}</div>${renderOverlay()}`;
    bindDynamic();
    if (focusToken) {
      requestAnimationFrame(() => {
        if (thisRender === renderRevision) restoreFocusToken(focusToken);
      });
    }
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
      if (event.dataTransfer && event.dataTransfer.files) {
        onFilesChosen(event.dataTransfer.files);
      }
    });
  }

  // ── 事件（委派）────────────────────────────────────────────────
  app.addEventListener("click", (e) => {
    if (state.classifying) return;
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    if (state.reviewing && ["pick", "reset", "classify", "start-review", "goto"].includes(action)) {
      toast("審查仍在補齊後續層，請稍候完成後再離開。");
      return;
    }
    if (action === "pick") {
      fileInput.value = "";
      fileInput.click();
    }
    else if (action === "reset") reset();
    else if (action === "classify") goClassify();
    else if (action === "start-review") startReview();
    else if (action === "goto") gotoStep(Number(el.dataset.step));
    else if (action === "toggle") { const id = el.dataset.id; state.open[id] = !state.open[id]; render(); }
    else if (action === "filter") { state.filter = el.dataset.filter; render(); }
    else if (action === "download") downloadMarkdown();
    else if (action === "row-toggle") { const key = el.dataset.key; state.rowOpen[key] = !state.rowOpen[key]; render(); }
    else if (action === "defect-toggle") {
      const code = el.dataset.code;
      state.openDefect[code] = !state.openDefect[code];
      render();
    }
    else if (action === "view-tab") {
      state.view = el.dataset.view;
      state.step = state.view === "matrix" ? 4 : 3;
      render();
    }
    else if (action === "uniform") { state.showUniform = !state.showUniform; render(); }
    else if (action === "notice") openNotice(el);
    else if (action === "copy-defects") copyText(noticeText());
    else if (action === "start-item-review") {
      state.view = "matrix";
      state.step = 4;
      render();
      requestAnimationFrame(() => {
        const target = [...document.querySelectorAll('[data-action="decide"]')]
          .find((select) => select.offsetParent !== null);
        if (target) target.focus();
      });
    }
    else if (action === "rerun-l3") {
      toast("送件檔審查後即刪除；只重跑語意判讀需重新送審原檔。");
    }
    else if (action === "ref-toggle") { state.refOpen = !state.refOpen; render(); }
    else if (action === "ref-tab") { state.refTab = el.dataset.tab; state.refOpen = true; render(); }
  });

  app.addEventListener("keydown", (e) => {
    const row = e.target.closest('[data-action="defect-toggle"]');
    if (!row || !["Enter", " "].includes(e.key)) return;
    e.preventDefault();
    row.click();
  });

  app.addEventListener("change", (e) => {
    if (state.classifying) return;
    const el = e.target.closest("[data-action]");
    if (!el) return;
    if (el.dataset.action === "decide") {
      const itemId = el.dataset.item;
      if (!DECISION_OPTIONS.some((option) => option.value === el.value)) return;
      state.decisions[itemId] = el.value;
      decisionRevision += 1;
      scheduleDecisionSave();
      render();
      return;
    }
    if (state.reviewing) return;
    if (el.dataset.action === "set-kind") {
      const index = Number(el.dataset.index);
      if (state.kinds[index] !== el.value) invalidateReport();
      state.kinds[index] = el.value;
      state.error = null;
    }
    else if (el.dataset.action === "set-role") state.role = el.value;
    else if (el.dataset.action === "set-stage") {
      if (state.workflowStage !== el.value) invalidateReport();
      state.workflowStage = el.value;
      state.error = null;
    }
    else return;
    render();
  });

  if (mask) {
    mask.addEventListener("click", (e) => {
      const action = e.target.closest("[data-modal-action]")?.dataset.modalAction;
      if (action === "close" || e.target === mask) closeNotice();
      else if (action === "copy-notice") {
        const text = document.getElementById("notice-text")?.value || "";
        copyText(text);
      }
    });
  }

  document.addEventListener("keydown", (e) => {
    if (!mask || mask.hidden) return;
    if (e.key === "Escape") {
      e.preventDefault();
      closeNotice();
      return;
    }
    if (e.key !== "Tab") return;
    const focusable = [...mask.querySelectorAll("button, textarea, select, [tabindex]:not([tabindex='-1'])")]
      .filter((node) => !node.disabled);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  fileInput.addEventListener("change", (event) => {
    if (!state.classifying && !state.reviewing) onFilesChosen(event.target.files);
  });

  render();
})();
