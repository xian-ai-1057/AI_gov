"""RegulationStore 測試：真 Milvus Lite 於 tmp_path，無網路（p3-01 spec §4.5）。

涵蓋 AC：
- AC-9：store round-trip（recreate→insert→write_meta→open→search+lookup）
    (a) search 取回已插入的 id/text
    (b) lookup prefix 粗篩 + 依 order 升冪 + 不含「六」不含「五」（prefix '五/(二)'）
    (c) lookup 回傳 dict 含六欄（id/reg_code/section_path/title/text/order）
- AC-10：sidecar 缺失 → raise；model 不符 → raise；dim 不符 → raise
- build_regulation_index seam 函式命名（AC-17 前提）
- AC-17：--dry-run 不呼叫 embed / store、輸出到 stdout
"""

from __future__ import annotations

import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from govcheck.rag.models import IndexMeta, RegulationChunk
from govcheck.rag.store import RegulationStore, StoreError


# ── Fixture：可重用的 chunk 集合 ──────────────────────────────────────────────

@pytest.fixture()
def sample_chunks() -> list[RegulationChunk]:
    """四個測試用 chunk：五/(二)/1、五/(二)/2、六、五。

    AC-9(b)：lookup('R0X','五/(二)') → 恰回前兩筆，不含六、不含五。
    """
    return [
        RegulationChunk(
            reg_code="R0X",
            reg_title="Test Reg",
            section_path="五/(二)/1",
            title="評鑑程序",
            text="【R0X Test Reg|五、節>(二)程序>1.】\n第一項內容。",
            chunk_seq=0,
            order=0,
        ),
        RegulationChunk(
            reg_code="R0X",
            reg_title="Test Reg",
            section_path="五/(二)/2",
            title="評鑑程序",
            text="【R0X Test Reg|五、節>(二)程序>2.】\n第二項內容。",
            chunk_seq=0,
            order=1,
        ),
        RegulationChunk(
            reg_code="R0X",
            reg_title="Test Reg",
            section_path="六",
            title="第六節",
            text="【R0X Test Reg|六、第六節】\n六節引言。",
            chunk_seq=0,
            order=2,
        ),
        RegulationChunk(
            reg_code="R0X",
            reg_title="Test Reg",
            section_path="五",
            title="第五節",
            text="【R0X Test Reg|五、第五節】\n五節引言。",
            chunk_seq=0,
            order=3,
        ),
    ]


@pytest.fixture()
def dim8_vectors(sample_chunks) -> list[list[float]]:
    """對應 sample_chunks 的假向量（dim=8）。"""
    return [[float(i + 1)] + [0.0] * 7 for i in range(len(sample_chunks))]


@pytest.fixture()
def sample_meta() -> IndexMeta:
    return IndexMeta(
        schema_version=1,
        built_at="2026-01-01T00:00:00+08:00",
        embedding_model="bge-m3",
        embedding_dim=8,
    )


@pytest.fixture()
def store_with_data(tmp_path, sample_chunks, dim8_vectors, sample_meta):
    """已 recreate + insert + write_meta 的 RegulationStore（dim=8）。

    此 fixture 完成後 store 仍處於 recreate 後的狀態（client 仍開啟）；
    呼叫端可再呼叫 open() 測試讀路徑。
    """
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    store = RegulationStore(
        uri=db,
        embedding_model="bge-m3",
        embedding_dim=8,
        meta_path=str(meta_path),
    )
    store.recreate(dim=8)
    store.insert(sample_chunks, dim8_vectors)
    store.write_meta(sample_meta)
    yield store
    store.close()


# ── AC-9(a)：search round-trip ────────────────────────────────────────────────


def test_search_returns_inserted_ids(tmp_path, sample_chunks, dim8_vectors, sample_meta):
    """AC-9(a)：recreate→insert→write_meta→open→search 能取回已插入的 id/text。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store.recreate(dim=8)
    store.insert(sample_chunks, dim8_vectors)
    store.write_meta(sample_meta)

    # Reopen with model/dim check
    store2 = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store2.open()

    # Query with vector closest to first chunk's vector [1,0,0,0,0,0,0,0]
    query = [1.0] + [0.0] * 7
    hits = store2.search(query, top_k=4)
    store2.close()

    assert len(hits) >= 1
    # 最相近的應為第一個 chunk
    returned_ids = {h["id"] for h in hits}
    assert sample_chunks[0].chunk_id in returned_ids

    # hit 應含 id/text
    first_hit = hits[0]
    assert "id" in first_hit
    assert "text" in first_hit
    assert "score" in first_hit


def test_search_returns_score(tmp_path, sample_chunks, dim8_vectors, sample_meta):
    """AC-9(a)：search hits 含 score 欄位（COSINE distance）。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store.recreate(dim=8)
    store.insert(sample_chunks, dim8_vectors)
    store.write_meta(sample_meta)
    store2 = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store2.open()
    hits = store2.search([1.0] + [0.0] * 7, top_k=2)
    store2.close()

    for hit in hits:
        assert isinstance(hit["score"], float)


# ── AC-9(b)：lookup prefix 粗篩 + order 升冪 ─────────────────────────────────


def test_lookup_prefix_returns_two_sections(store_with_data, sample_chunks):
    """AC-9(b)：lookup('R0X','五/(二)') 恰回兩筆，不含「六」，不含「五」。"""
    res = store_with_data.lookup("R0X", "五/(二)")

    assert len(res) == 2, f"Expected 2 rows, got {len(res)}: {[r['section_path'] for r in res]}"

    paths = {r["section_path"] for r in res}
    assert "五/(二)/1" in paths
    assert "五/(二)/2" in paths
    assert "六" not in paths, "lookup('R0X','五/(二)') 不應命中「六」"
    assert "五" not in paths, "lookup('R0X','五/(二)') 不應命中「五」（非 prefix+/ 展開）"


def test_lookup_order_ascending(store_with_data):
    """AC-9(b)：lookup 結果依 order 升冪排序。"""
    res = store_with_data.lookup("R0X", "五/(二)")
    orders = [r["order"] for r in res]
    assert orders == sorted(orders), f"order 未升冪：{orders}"


def test_lookup_row_dict_has_all_six_fields(store_with_data):
    """AC-9(b)：lookup 回傳 row dict 含六欄：id/reg_code/section_path/title/text/order。"""
    res = store_with_data.lookup("R0X", "五/(二)")
    assert len(res) > 0
    required_fields = {"id", "reg_code", "section_path", "title", "text", "order"}
    for row in res:
        missing = required_fields - row.keys()
        assert not missing, f"row dict 缺欄位：{missing}"


def test_lookup_empty_prefix_returns_all_chunks(store_with_data, sample_chunks):
    """AC-9(b)：lookup('R0X','') prefix='' → 回傳該 reg_code 全部 chunk。"""
    res = store_with_data.lookup("R0X", "")
    assert len(res) == len(sample_chunks)


def test_lookup_nonexistent_reg_returns_empty(store_with_data):
    """AC-9(b)：不存在的 reg_code → 回傳空 list。"""
    res = store_with_data.lookup("R99", "")
    assert res == []


def test_lookup_prefix_does_not_match_longer_sibling(store_with_data):
    """AC-9(b)：prefix='五/(二)' 不誤命中 '五/(二十)'（分段 '/' 比對）。

    本 fixture 無 '五/(二十)' chunk，但此測試確認 '五' chunk 也不在結果中。
    """
    res = store_with_data.lookup("R0X", "五/(二)")
    paths = [r["section_path"] for r in res]
    # '五' 不以 '五/(二)/' 開頭，也不等於 '五/(二)'
    assert "五" not in paths


# ── AC-9(c)：write_meta 原子寫 ────────────────────────────────────────────────


def test_write_meta_creates_json_file(tmp_path, sample_meta):
    """AC-9：write_meta 成功建立 index_meta.json（原子寫）。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    store = RegulationStore(uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path))
    store.recreate(dim=8)
    store.write_meta(sample_meta)
    store.close()

    assert meta_path.exists(), "index_meta.json 應由 write_meta 建立"
    with meta_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["embedding_model"] == "bge-m3"
    assert data["embedding_dim"] == 8
    assert data["schema_version"] == 1

    # 原子寫：不留 .tmp 殘檔
    assert not meta_path.with_name(meta_path.name + ".tmp").exists()


# ── AC-10：open 核對 meta，不符即 raise ──────────────────────────────────────


def test_open_raises_when_sidecar_missing(tmp_path):
    """AC-10：缺 sidecar → StoreError。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "missing_meta.json"

    store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    # 沒有 recreate，也沒有 write_meta
    with pytest.raises(StoreError, match="sidecar not found"):
        store.open()


def test_open_raises_on_model_mismatch(tmp_path, sample_chunks, dim8_vectors, sample_meta):
    """AC-10：sidecar embedding_model 與傳入 config 不符 → StoreError。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    # 建索引（model='bge-m3'）
    store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store.recreate(dim=8)
    store.insert(sample_chunks, dim8_vectors)
    store.write_meta(sample_meta)
    store.close()

    # 以不同 model 嘗試 open
    wrong_store = RegulationStore(
        uri=db, embedding_model="wrong-model", embedding_dim=8, meta_path=str(meta_path)
    )
    with pytest.raises(StoreError, match="embedding_model mismatch"):
        wrong_store.open()


def test_open_raises_on_dim_mismatch(tmp_path, sample_chunks, dim8_vectors, sample_meta):
    """AC-10：sidecar embedding_dim 與傳入 config 不符 → StoreError。"""
    db = str(tmp_path / "gov.db")
    meta_path = tmp_path / "index_meta.json"

    store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=8, meta_path=str(meta_path)
    )
    store.recreate(dim=8)
    store.insert(sample_chunks, dim8_vectors)
    store.write_meta(sample_meta)
    store.close()

    wrong_store = RegulationStore(
        uri=db, embedding_model="bge-m3", embedding_dim=999, meta_path=str(meta_path)
    )
    with pytest.raises(StoreError, match="embedding_dim mismatch"):
        wrong_store.open()


# ── AC-17：build script seam 命名 + --dry-run 不 embed 不寫庫 ────────────────


def test_seam_function_names_exist():
    """AC-17 前提：build script 匯出 Lead 鎖定的五個 seam 函式。"""
    import scripts.build_regulation_index as bri

    assert callable(bri.build_index), "build_index 必須存在（spec §4.6 seam ①）"
    assert callable(bri.print_chunk_tree), "print_chunk_tree 必須存在（spec §4.6 seam ③）"
    assert callable(bri.iter_regulation_chunks), "iter_regulation_chunks 必須存在（spec §4.6 seam）"
    assert callable(bri.build_retrieval_map), "build_retrieval_map 必須存在（spec §4.6 seam ②）"
    assert callable(bri.run_eval), "run_eval 必須存在（spec §4.6 seam ④）"
    assert callable(bri.main), "main 必須存在（CLI 入口）"


def test_dry_run_prints_to_stdout_and_does_not_embed():
    """AC-17：print_chunk_tree() 印 chunk 樹到 stdout；不呼叫 EmbeddingClient；不建 Milvus。"""
    import scripts.build_regulation_index as bri

    fake_chunks = [
        RegulationChunk(
            reg_code="R01",
            reg_title="TestReg",
            section_path="第一條",
            title="T1",
            text="【R01 TestReg|第一條】\nSome article text.",
            chunk_seq=0,
            order=0,
        ),
    ]

    captured = io.StringIO()

    with (
        patch.object(bri, "iter_regulation_chunks", return_value=iter(fake_chunks)),
        patch.object(bri, "EmbeddingClient") as patched_ec,
        patch.object(bri, "RegulationStore") as patched_rs,
    ):
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            bri.print_chunk_tree(cfg={})
        finally:
            sys.stdout = old_stdout

        # embed / store 建構函式不應被呼叫
        patched_ec.assert_not_called()
        patched_rs.assert_not_called()

    output = captured.getvalue()
    assert "R01" in output, "stdout 應含 reg_code"
    assert "第一條" in output, "stdout 應含 section_path"


def test_build_index_calls_store_and_embed(tmp_path):
    """AC-17：build_index() 呼叫 store.recreate / insert / write_meta 與 embedding_client.embed。"""
    import scripts.build_regulation_index as bri

    fake_chunks = [
        RegulationChunk(
            reg_code="R01",
            reg_title="TestReg",
            section_path="第一條",
            title="T1",
            text="【R01 TestReg|第一條】\nSome text.",
            chunk_seq=0,
            order=0,
        ),
        RegulationChunk(
            reg_code="R01",
            reg_title="TestReg",
            section_path="第二條",
            title="T2",
            text="【R01 TestReg|第二條】\nMore text.",
            chunk_seq=0,
            order=1,
        ),
    ]

    fake_store = MagicMock()
    fake_emb = MagicMock()
    fake_emb.embed.return_value = [[0.1] * 8, [0.2] * 8]

    cfg = {
        "embedding_dim": 8,
        "embedding_model": "bge-m3",
        "milvus_uri": str(tmp_path / "gov.db"),
    }

    with patch.object(bri, "iter_regulation_chunks", return_value=iter(fake_chunks)):
        bri.build_index(cfg=cfg, store=fake_store, embedding_client=fake_emb)

    fake_store.recreate.assert_called_once_with(8)
    fake_store.insert.assert_called()
    fake_store.write_meta.assert_called_once()
    fake_emb.embed.assert_called()


# ── 回歸測試：gitignore 閘門使用檔路徑，而非父目錄路徑 ────────────────────────


def test_check_gitignore_matches_file_path_not_parent_dir():
    """回歸：_check_gitignore 應傳入輸出檔路徑（data/rag/retrieval_map.json），
    而非其父目錄（data/rag）。

    .gitignore 的 pattern 是 `data/rag/`（僅目錄形式）：
    - `git check-ignore data/rag`（裸目錄、目錄不在磁碟）→ exit 1（未匹配）←修正前的 bug
    - `git check-ignore data/rag/retrieval_map.json`    → exit 0（被忽略）←修正後正確行為

    此測試驗證：
    1. 對預設輸出路徑 data/rag/retrieval_map.json，_check_gitignore 回 True（被忽略，閘門放行）。
    2. 對不存在於 .gitignore 的路徑，_check_gitignore 回 False（等同修正前 bug：傳父目錄 → 不匹配）。
    """
    import scripts.build_regulation_index as bri

    # 正確行為（修正後）：檔案路徑被 .gitignore 的 data/rag/ pattern 匹配 → True
    assert bri._check_gitignore("data/rag/retrieval_map.json") is True, (
        "data/rag/retrieval_map.json 應被 .gitignore（pattern: data/rag/）匹配，"
        "_check_gitignore 應回 True（閘門放行）。"
    )

    # Bug 復現（修正前的行為）：裸目錄路徑不在磁碟且無對應 .gitignore pattern 時 → False。
    # 注意：若 data/rag/ 目錄已存在於磁碟，git check-ignore "data/rag"（無 /）仍可能因
    # git 向上遍歷而回 True。為使斷言環境無關，改用永遠不存在的 dummy 路徑，確保 exit 1。
    assert bri._check_gitignore("data/rag_nonexistent_for_regression_test") is False, (
        "不在 .gitignore 的裸目錄路徑應回 False。"
        "此斷言確認修正前的 bug 行為（傳父目錄而非檔路徑→ 不匹配）的迴歸測試意義："
        "閘門必須傳完整檔路徑（data/rag/retrieval_map.json），而非父目錄。"
    )


def test_build_retrieval_map_gitignore_gate_checks_file_path_not_parent(monkeypatch):
    """回歸（呼叫端層級）：build_retrieval_map 走 use_default 分支時，
    gitignore 閘門應把「實際輸出檔路徑」交給 _check_gitignore，而非其父目錄。

    這是真正守住 bug 的測試：helper 本身沒錯，錯在呼叫端傳了 output_path.parent。
    若有人把第 315 行改回 str(output_path.parent)，spy 會收到 "data/rag" →
    斷言失敗 → 紅燈。原本所有測試都顯式注入 output_path（繞過 use_default），
    所以才沒抓到；本測試明確走 output_path=None 分支。

    做法：monkeypatch _check_gitignore → spy（記錄引數、回 False）。
    回 False 使閘門立刻 raise 中止，不觸發任何 store/embedding/寫檔下游動作，
    因此不需 data/original、不需網路、不實際寫檔。
    """
    import scripts.build_regulation_index as bri

    received: list[str] = []

    def spy_check_gitignore(path_str: str) -> bool:
        received.append(path_str)
        return False  # 模擬「未被忽略」→ 閘門立即 raise，阻斷下游 build

    monkeypatch.setattr(bri, "_check_gitignore", spy_check_gitignore)

    cfg = {"mapping_path": "data/rag/retrieval_map.json"}

    # (a) 閘門回 False → 應 raise 中止（不進入任何下游 build/寫檔）
    with pytest.raises(RuntimeError, match="check-ignore"):
        bri.build_retrieval_map(cfg=cfg, output_path=None)

    # (b) spy 收到的必須是「輸出檔路徑」（以 data/rag/retrieval_map.json 結尾的絕對路徑），
    # 而非父目錄（data/rag 或含 ROOT 前綴的父目錄路徑）。
    # output_path 現已錨定 ROOT 成絕對路徑，因此用 endswith 比對尾段即可。
    # 若有人把呼叫端改回傳 output_path.parent，received[0] 會以 "data/rag" 結尾 → 紅燈。
    assert len(received) == 1, f"spy 應被呼叫恰一次，實收：{received}"
    assert received[0].endswith("data/rag/retrieval_map.json"), (
        "gitignore 閘門應檢查實際輸出檔路徑（以 data/rag/retrieval_map.json 結尾），"
        f"而非父目錄；spy 實收：{received[0]}。"
        " 若收到以 'data/rag' 結尾（無檔名），代表呼叫端被改回傳 output_path.parent（bug 復發）。"
    )


def test_dry_run_does_not_write_tracked_files(tmp_path):
    """AC-17：print_chunk_tree 不寫出任何新檔案（驗 tmp_path 無新 .json/.db 等）。"""
    import scripts.build_regulation_index as bri

    fake_chunks = [
        RegulationChunk(
            reg_code="R03",
            reg_title="TestReg2",
            section_path="一",
            title="T",
            text="【R03 TestReg2|一、T】\nBody.",
            chunk_seq=0,
            order=0,
        ),
    ]

    before_files = set(tmp_path.rglob("*"))

    captured = io.StringIO()
    with patch.object(bri, "iter_regulation_chunks", return_value=iter(fake_chunks)):
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            bri.print_chunk_tree(cfg={})
        finally:
            sys.stdout = old_stdout

    after_files = set(tmp_path.rglob("*"))
    new_files = after_files - before_files
    assert not new_files, f"print_chunk_tree 不應寫出任何新檔案，但發現：{new_files}"
