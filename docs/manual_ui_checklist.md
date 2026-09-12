# 審查報告頁手動驗收清單

程式測試只驗證契約、狀態語意與安全降級。以下項目是**人眼才看得出來**的東西：
分層出場的節奏、視覺層級、收欄與 RWD。改動 `web/app.js`、`web/styles.css` 或報告頁資料契約後走一次。

## 前置

```sh
uv sync --extra dev
uv run ruff check src tests scripts
uv run pytest                                     # 須全綠
uv run uvicorn govcheck.web.api:app --port 8501
```

需要本機 ollama（gemma + bge-m3 @ `localhost:11434`）與 FileProcess（`localhost:3322`）。
送件包用 `data/samples/` 底下任一含 F03 的資料夾，流程階段選「上線」。

## 項目

1. **分層出場**　L1 pill 在 ~10 秒內轉綠、缺陷清單立刻可用（不是空白遮罩）；
   L2 約 1 分鐘、L3 約 5 分鐘陸續補列。**已讀過的列不重排** —— 這是最容易被改壞的一項。

2. **Hero 的結論**　標題是「初判：不可放行 · 需補件」而不是「規則通過」；
   `0 錯誤` 出現在「規則層」tile 裡，不是獨立 KPI。規則層無錯不等於可放行。

3. **缺陷清單（預設視圖）**　每類缺陷各一列；共通事實帶顯示「N/N 項未填上線階段佐證」；
   `照抄提案 0` 以 ok 色呈現。展開任一列看得到命中位置 chip 與座標（例如 `F03·檢核表 J14`）。

4. **狀態總覽表**　列首是短名（「模型監控機制」）而不是整段管理議題；
   同值欄收起時要列出被收起的欄；改覆核下拉後 hero 的待覆核／已覆核 tile 與進度條即時更新，
   該列底色從 `--row-attn` 回白底。

5. **補件通知**　點按鈕開 modal，文字確實由缺陷清單組出（含筆數、依據、位置、來源信心）；
   「複製全文」寫入剪貼簿並跳 toast。

6. **RWD**　縮到 760 px 寬：tiles 變 2×2、缺陷列收欄、總覽列隱藏覆核欄。

## 已自動化，不必手動走

| 原項目 | 現在由誰驗 |
|---|---|
| 上傳樣本送件包跑完整流程 | 上線校準的 shadow run（`scripts/calibration/`） |
| 覆核決定落檔、`audit.log` 不含檢核項原文 | shadow run 的跑後隱私掃描，比對每份案例的 F03 I/J 欄文字 |
| `GOVCHECK_LLM_ENABLED=0` 的降級路徑 | shadow run 的強制對照組 |
