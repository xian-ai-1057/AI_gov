"""RegulationStore: pymilvus.MilvusClient 包裝（build-time RAG 索引）。

collection「regulation_chunks」七欄 schema（Lead 裁決定案）：
  id VARCHAR PK / reg_code VARCHAR / section_path VARCHAR /
  title VARCHAR / text VARCHAR / order INT64 / embedding FLOAT_VECTOR(dim)
AUTOINDEX + COSINE；Milvus Lite 本地 .db 檔；URI 換 http(s):// 即切 Server，程式零改動。

注意：
- 本模組為 build-time 工具，runtime 審查流程不 import 也不呼叫（D1 設計決策）。
- VARCHAR max_length 以寬裕餘量設定（text=8192, id=256 等，see AC note in spec §5）。
- api_key（embedding）與 milvus_token 一律由 env 提供，不入本模組邏輯。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pymilvus import DataType, MilvusClient

from govcheck.logging_setup import get_logger

log = get_logger("rag_store")


class StoreError(RuntimeError):
    """向量索引存取、schema 核對或 sidecar 讀寫失敗。"""


class RegulationStore:
    """Milvus 向量索引包裝（build-time）。

    URI = Milvus Lite .db 檔路徑；換 http(s):// 前綴即切 Milvus Server（程式零改動）。
    sidecar「index_meta.json」與 .db 同目錄；open() 核對 embedding_model/dim，不符即 raise。
    """

    COLLECTION = "regulation_chunks"

    def __init__(
        self,
        uri: str,
        *,
        embedding_model: str = "",
        embedding_dim: int = 1024,
        meta_path: str | Path | None = None,
    ) -> None:
        self._uri = uri
        self._expected_model = embedding_model
        self._expected_dim = embedding_dim
        self._client: MilvusClient | None = None

        # sidecar path：Lite .db 同目錄；Server URI 退回可設定；預設 data/milvus/
        if meta_path is not None:
            self._meta_path = Path(meta_path)
        elif uri.endswith(".db"):
            self._meta_path = Path(uri).parent / "index_meta.json"
        else:
            self._meta_path = Path("data/milvus/index_meta.json")

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> RegulationStore:
        """由 RAG config dict 建立 RegulationStore。"""
        from govcheck.rag.config import load_rag_config

        cfg = cfg or load_rag_config()
        return cls(
            uri=cfg["milvus_uri"],
            embedding_model=cfg.get("embedding_model") or "",
            embedding_dim=cfg.get("embedding_dim", 1024),
        )

    # ── Build-time：recreate / insert / write_meta ───────────────────────────

    def recreate(self, dim: int) -> None:
        """Drop-and-recreate collection（若已存在先刪），以 dim 建 FLOAT_VECTOR 欄。"""
        # 確保 .db 父目錄存在（clean-checkout 首次 build 時 data/milvus/ 可能尚未建立）
        if self._uri.endswith(".db"):
            Path(self._uri).parent.mkdir(parents=True, exist_ok=True)
        client = MilvusClient(uri=self._uri)
        if client.has_collection(self.COLLECTION):
            client.drop_collection(self.COLLECTION)
            log.debug("store dropped existing collection=%s", self.COLLECTION)

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        # Seven fields as per spec §4.5 + Lead decision (order INT64)
        # VARCHAR max_length: 寬裕餘量（spec §5 AC 不綁定具體值）
        schema.add_field("id", DataType.VARCHAR, max_length=256, is_primary=True)
        schema.add_field("reg_code", DataType.VARCHAR, max_length=32)
        schema.add_field("section_path", DataType.VARCHAR, max_length=512)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("order", DataType.INT64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)

        idx = client.prepare_index_params()
        idx.add_index("embedding", metric_type="COSINE", index_type="AUTOINDEX")

        client.create_collection(self.COLLECTION, schema=schema, index_params=idx)
        self._client = client
        log.info("store recreated collection=%s dim=%d", self.COLLECTION, dim)

    def insert(self, chunks: list, vectors: list[list[float]]) -> int:
        """RegulationChunk → Milvus row 投影並批次寫入。

        chunks 以 duck-type 接受任何有
        chunk_id / reg_code / section_path / title / text / order 的物件。
        回傳實際寫入筆數。
        """
        assert self._client is not None, "Call recreate() or open() first"
        rows = [
            {
                "id": chunk.chunk_id,
                "reg_code": chunk.reg_code,
                "section_path": chunk.section_path,
                "title": chunk.title,
                "text": chunk.text,
                "order": chunk.order,
                "embedding": vector,
            }
            for chunk, vector in zip(chunks, vectors)
        ]
        if rows:
            self._client.insert(self.COLLECTION, rows)
        log.debug("store insert n=%d", len(rows))
        return len(rows)

    def write_meta(self, meta) -> None:
        """原子寫 sidecar index_meta.json（temp + os.replace）。

        meta 須有 model_dump(mode='json') 方法（IndexMeta Pydantic model）。
        """
        data = meta.model_dump(mode="json")
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._meta_path.with_name(self._meta_path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._meta_path)
        except Exception:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise
        log.info(
            "store meta written path=%s model=%s dim=%d",
            self._meta_path,
            data.get("embedding_model", ""),
            data.get("embedding_dim", 0),
        )

    # ── Read-time：open / search / lookup ────────────────────────────────────

    def open(self) -> None:
        """載入既有索引並核對 sidecar meta（model/dim 不符即 raise StoreError）。

        AC-10：缺 sidecar → raise；embedding_model 或 embedding_dim 不符 → raise。
        """
        if not self._meta_path.exists():
            raise StoreError(f"sidecar not found: {self._meta_path}")

        try:
            with self._meta_path.open(encoding="utf-8") as fh:
                meta = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            raise StoreError(f"cannot read sidecar: {exc}") from exc

        stored_model = meta.get("embedding_model", "")
        stored_dim = meta.get("embedding_dim", -1)

        if stored_model != self._expected_model:
            raise StoreError(
                f"embedding_model mismatch: stored={stored_model!r},"
                f" expected={self._expected_model!r}"
            )
        if stored_dim != self._expected_dim:
            raise StoreError(
                f"embedding_dim mismatch: stored={stored_dim},"
                f" expected={self._expected_dim}"
            )

        self._client = MilvusClient(uri=self._uri)
        self._client.load_collection(self.COLLECTION)
        log.info("store opened uri=%s collection=%s", self._uri, self.COLLECTION)

    def search(self, vector: list[float], top_k: int) -> list[dict]:
        """向量相似度搜尋；回傳 list[dict] 含 id/reg_code/section_path/title/text/order/score。

        score = COSINE distance（1.0 最相似）。
        """
        assert self._client is not None, "Call recreate() or open() first"
        results = self._client.search(
            self.COLLECTION,
            data=[vector],
            anns_field="embedding",
            limit=top_k,
            output_fields=["id", "reg_code", "section_path", "title", "text", "order"],
        )
        hits = []
        for hit in results[0]:
            row = {"score": hit["distance"]}
            row.update(hit["entity"])
            hits.append(row)
        log.debug("store search top_k=%d hits=%d", top_k, len(hits))
        return hits

    def lookup(self, reg_code: str, section_path_prefix: str) -> list[dict]:
        """Scalar filter 查詢（不走向量），回傳符合 prefix 的 rows 依 order 升冪。

        prefix="" → 回傳該 reg_code 全部 chunks。
        分段比對：prefix="五/(二)" 命中 "五/(二)" 與 "五/(二)/1"，但不誤命中 "五/(二十)"。
        細篩（全形容錯、R03&R07 展開等 refs 邏輯）由 p3-02 rag/refs.py 負責，本方法不做。
        """
        assert self._client is not None, "Call recreate() or open() first"
        safe = reg_code.replace('"', "").replace("\\", "")
        rows = list(
            self._client.query(
                self.COLLECTION,
                filter=f'reg_code == "{safe}"',
                output_fields=[
                    "id", "reg_code", "section_path", "title", "text", "order"
                ],
            )
        )

        # Python-side prefix 粗篩（spec §4.5：分段比對）
        if section_path_prefix:
            rows = [
                r
                for r in rows
                if (
                    r["section_path"] == section_path_prefix
                    or r["section_path"].startswith(section_path_prefix + "/")
                )
            ]

        rows.sort(key=lambda r: r["order"])
        log.debug(
            "store lookup reg=%s prefix=%r n=%d", reg_code, section_path_prefix, len(rows)
        )
        return rows

    def close(self) -> None:
        """關閉 MilvusClient 連線。"""
        if self._client is not None:
            self._client.close()
            self._client = None
            log.debug("store closed")
