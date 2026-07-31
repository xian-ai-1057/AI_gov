"""覆核決定儲存的正/反例。

重點在拒絕面：review_id 會被拿去組檔名，放行不合法的值等於開放任意路徑寫入。
"""

from __future__ import annotations

import json

import pytest

from govcheck.web import decisions as store

_ID = "a" * 32
_OTHER = "b" * 32


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_DECISION_DIR", str(tmp_path / "decisions"))
    return tmp_path


def test_save_then_load_round_trip():
    store.save(_ID, {"1-1": "accept", "4-2": "reject"}, subject="測試案")
    loaded = store.load(_ID)
    assert loaded["decisions"] == {"1-1": "accept", "4-2": "reject"}
    assert loaded["subject"] == "測試案"
    assert loaded["review_id"] == _ID
    assert loaded["updated_at"]


def test_save_overwrites_rather_than_merges():
    """整批覆寫：前端持有完整狀態，合併語意會讓「改回待確認」無法表達。"""
    store.save(_ID, {"1-1": "accept", "1-2": "reject"})
    store.save(_ID, {"1-1": "pending"})
    assert store.load(_ID)["decisions"] == {"1-1": "pending"}


def test_load_missing_returns_none():
    assert store.load(_ID) is None


def test_reviews_are_isolated_from_each_other():
    store.save(_ID, {"1-1": "accept"})
    store.save(_OTHER, {"1-1": "reject"})
    assert store.load(_ID)["decisions"]["1-1"] == "accept"
    assert store.load(_OTHER)["decisions"]["1-1"] == "reject"


def test_corrupt_file_reads_as_missing_not_crash():
    """手動編壞的檔案不該讓報告頁整頁掛掉。"""
    store.save(_ID, {"1-1": "accept"})
    (store.decision_dir() / f"{_ID}.json").write_text("{ not json", encoding="utf-8")
    assert store.load(_ID) is None


def test_no_temp_files_left_behind():
    store.save(_ID, {"1-1": "accept"})
    assert [p.name for p in store.decision_dir().iterdir()] == [f"{_ID}.json"]


# ── 拒絕面：review_id 是路徑穿越的唯一防線 ───────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "..",
        "a" * 31,          # 太短
        "a" * 33,          # 太長
        "A" * 32,          # 大寫不在 uuid4().hex 的值域內
        "g" * 32,          # 非 hex
        "a" * 30 + "/x",
        "",
    ],
)
def test_invalid_review_id_is_rejected(bad_id):
    with pytest.raises(store.DecisionError):
        store.save(bad_id, {"1-1": "accept"})
    with pytest.raises(store.DecisionError):
        store.load(bad_id)


def test_path_traversal_writes_nothing():
    with pytest.raises(store.DecisionError):
        store.save("../escape", {"1-1": "accept"})
    assert not store.decision_dir().exists()


@pytest.mark.parametrize("bad_value", ["approved", "", None, 1, "ACCEPT"])
def test_unknown_decision_value_is_rejected(bad_value):
    with pytest.raises(store.DecisionError):
        store.save(_ID, {"1-1": bad_value})


def test_non_dict_decisions_rejected():
    with pytest.raises(store.DecisionError):
        store.save(_ID, [("1-1", "accept")])


def test_empty_item_id_rejected():
    with pytest.raises(store.DecisionError):
        store.save(_ID, {"  ": "accept"})


def test_rejected_input_does_not_touch_existing_file():
    store.save(_ID, {"1-1": "accept"})
    with pytest.raises(store.DecisionError):
        store.save(_ID, {"1-2": "bogus"})
    assert store.load(_ID)["decisions"] == {"1-1": "accept"}


# ── 稽核與隱私 ──────────────────────────────────────────────────────────


def test_stored_file_contains_no_evidence_text():
    """落檔只有項次與決定；佐證原文、檔案內容一律不進這裡。"""
    store.save(_ID, {"1-1": "accept"}, subject="測試案", note="已與提案單位確認")
    raw = json.loads((store.decision_dir() / f"{_ID}.json").read_text(encoding="utf-8"))
    assert set(raw) == {
        "schema_version", "review_id", "subject", "note", "updated_at", "decisions",
    }
