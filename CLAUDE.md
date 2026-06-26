# AI 治理審查小幫手 — Project Guide

> 本檔案在專案 root，會自動載入到 Claude 的 context。
> **狀態：專案重建中（2026-06-26 起）**。舊版完整內容已封存於 `_Archive/`，架構待重新規劃。

---

## 專案總覽

台新銀行內部 AI 治理審查工具。治理人員上傳提案單位提交的
**F01 系統資訊表 / F02 風險評鑑 / F03 上線檢核表**（+佐證文件），
系統依 **R01–R07 治理辦法**與 F03 檢核表自動做「初步審查」，第一時間產出**結構化審查報告**，指出：
(1) 缺少文件/欄位、(2) 是否符合規範、(3) 哪些部分需要補足。

**定位**：AI 產出「初判草稿」加速人工審閱，**最終判定權在治理人員與三遵**。
報告全程標示「AI 初判，需人工覆核」。

**主要交付**：內部單機 Streamlit 網頁（上傳 → 報告），資料全程不外送雲端。

---

## 技術棧（意向）

Python 3.12、Streamlit、Pydantic v2、openpyxl/pdfplumber/python-docx、
OpenAI SDK（指向內部/地端端點）、sentence-transformers(bge-m3)、Milvus(pymilvus)、pytest。
套件用最新版、**uv** 管理。

> 本機開發可改用 ollama（gemma + bge-m3，`localhost:11434`）做真實端到端測試，免內部端點。

---

## 目錄結構

```
AI治理/
├── CLAUDE.md            ← 你正在讀的檔案
├── .gitignore
├── data/
│   └── original/        ← R01–R07 治理辦法 PDF + F01/F02/F03 等官方模板（唯讀來源，禁寫入、不入庫）
└── _Archive/            ← 舊版（Phase 0–4）完整封存，僅本機留存、不入庫；重建時可參考
```

`data/original/` 是重建期間**唯一的事實來源**，內容：
R01–R07 七份治理辦法 PDF、附件一 F01（xlsx）、附件二 F02（xlsm）、附件三 F03（xlsx）、附件四/五（pdf）。

---

## 行為守則

### 必須做
- ✅ 規則式檢查（缺件/必填/高風險條件式）與 LLM 判讀**分離**。
- ✅ LLM 相關測試一律 **mock**，不打真端點。
- ✅ 動 `data/original/` 前先確認：**唯讀來源，禁止寫入或更動**。

### 不能做
- ❌ 寫入 `data/original/`（唯讀來源）。
- ❌ 把提交者資料或規範內容送往**外部雲端服務**（僅地端/內部端點）。
- ❌ 跳過測試直接收工。

---

## 重建備註

- 舊版採 SDD + Agent Teams，程式碼、specs、tests 皆在 `_Archive/`；新架構重新規劃，可酌情參考舊實作。
- 舊環境設定範例與金鑰在 `_Archive/.env` / `_Archive/.env.example`，需要時複製回 root。
- 重建依賴與 lockfile 重新建立（`_Archive/pyproject.toml`、`_Archive/uv.lock` 為舊版參考）。
