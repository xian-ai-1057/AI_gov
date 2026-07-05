# Milvus Lite → Milvus Server 遷移手冊

本文檔說明如何將本地開發的 govcheck RAG 索引從 **Milvus Lite**（單機嵌入式）遷移至 **Milvus Server**（獨立服務），
適用於多程序部署、併發審查、集中管理等生產場景。

---

## 1. 何時需要遷移

**Milvus Lite 適用場景**：
- 單機開發與測試；小規模批次（<100份送件包/天）；無並行審查需求；資料全程地端不外送。

**遷移至 Milvus Server 的觸發條件**：
- **多程序部署**：多個 govcheck 審查實例共享同一份法規索引，避免重複構建。
- **併發審查**：需要支持數個審查任務同時檢索，Lite 的單進程限制無法滿足。
- **集中管理**：將法規索引集中在專用伺服器上，便於索引版本控制、備份、監控；減少端機器負擔。

---

## 2. 設定切換

Milvus Lite 與 Server 具有相同 Python 客戶端介面（`pymilvus.MilvusClient`），程式碼零改動。
切換只需環境變數配置：

```bash
# Milvus Lite（預設；開發/單機測試）
export GOVCHECK_RAG_MILVUS_URI="./milvus.db"

# Milvus Server（生產部署）
export GOVCHECK_RAG_MILVUS_URI="http://<server-host>:19530"
export GOVCHECK_RAG_MILVUS_TOKEN="<optional-token>"  # 若 Server 啟用認證
```

在初始化 `MilvusClient` 時，SDK 自動根據 URI 判斷連線方式（本地檔或遠端服務），無需修改應用層程式碼。

---

## 3. 資料遷移

**重要原則**：真值來源是 R01–R07 PDF 檔案與 build script；**無需匯出/複製索引檔**，直接在新環境重跑建置。

**遷移步驟**：

1. 在**可存取 `data/original/` 與內部 `/embeddings` 端點的機器**上，執行建置指令指向 Server URI：
   ```bash
   export GOVCHECK_RAG_MILVUS_URI="http://<server-host>:19530"
   uv run python scripts/build_regulation_index.py
   ```

2. 建置過程自動連線至遠端 Server，逐塊寫入法規 chunks、embeddings、canonical 映射。
3. 語料規模約 200–400 chunks，總耗時通常 1–3 分鐘（取決於網路與端點響應）。

遷移完成後，所有 govcheck 實例（開發/生產）配置同一個 Server URI，即可共享索引。

---

## 4. Air-gapped 前提

生產環境若無外網與 GitHub，遷移前確認以下依賴可得性：

- **pymilvus 與 gRPC 相依套件**：wheel 檔案須在內網倉庫（如 nexus/artifactory）可得，或預先離線快取。
- **PDF 檔案進入 prod 機器的流程**：R01–R07 PDF 檔須通過安全管道進入 build 機器的 `data/original/`，建置完成後可刪除原檔。
- **內部 /embeddings 端點**：build 機器須能連線至內部 embedding 服務（OpenAI 相容端點），並提供認證憑證（GOVCHECK_EMBEDDING_API_KEY）。

確認上述三項後，遷移可在完全隔離環境中進行，Milvus Server 與法規索引均不需外網。

---

## 5. 為何不用 milvus-backup

Milvus 官方提供 `milvus-backup` 工具用於 Lite ↔ Server 遷移，但本專案選擇重建而非備份遷移，理由包括：

- **工具鏈相容性**：backup 工具版本與 pymilvus 版本耦合緊密，升級時易出現不相容。
- **檔案搬運稽核**：直接搬移索引檔案難以追溯資料來源與構建過程；重建保持完整稽核軌跡（build script → log）。
- **簡化運維**：無需額外學習/維護 backup 工具；build script 已是單一事實源，重跑即還原完整索引。
- **驗證便利**：重建過程會自動執行品質檢驗（如 `--eval`），確保遷移後索引可用；備份遷移無此驗證。

---

## 6. 遷移檢核清單

完成遷移前，逐項檢視以下清單：

- [ ] **環境變數**：Server 機器與 build 機器均已設定 `GOVCHECK_RAG_MILVUS_URI` 與 `GOVCHECK_RAG_MILVUS_TOKEN`（若需）。
- [ ] **.gitignore**：確認 `data/milvus/`、`data/rag/` 都在 `.gitignore`，且不在 staged 清單；舊 Lite db 檔（`./milvus.db`）清理完畢。
- [ ] **索引中繼資料對齐**：build script 產生的 `index_meta.json` 或版本標記已驗證，Server 端索引表名稱與元資訊一致。
- [ ] **檢索品質驗證**：在新 Server 上執行 `uv run python scripts/build_regulation_index.py --eval`，確保 recall@k（常用 k=5）達預期水準（>0.8）。
- [ ] **應用層驗證**：本地或 staging 環境啟動 govcheck（Streamlit 或 Web），上傳一份真實送件包，驗證 F03 RAG 判讀能夠觸發並回傳相關條文。
- [ ] **清理與確認**：刪除舊本地索引檔（若有 `./milvus.db`），確認後續 `data/milvus/`、`data/rag/` 目錄為空。

---

## 後續維護

遷移完成後，以下操作推薦：

1. **定期備份** Server 數據（由 Milvus 或容器編排工具負責，不由 govcheck 管理）。
2. **索引版本管理**：當 R01–R07 治理辦法更新時，重跑 build script，Server 會覆蓋舊索引；可在 build script 中加版本標記便於追溯。
3. **監控與告警**：監控 Server 可用性、查詢延遲、容量；與維運平台整合告警機制。
