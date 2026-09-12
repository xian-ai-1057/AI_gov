"""欄位目錄載入：合成 fixture、缺檔／壞檔／版本不符一律回 None、env 覆寫路徑。"""

from __future__ import annotations

import json
from pathlib import Path

from govcheck.assist.fields import FieldSpec, load_form_fields, query_text

FIXTURE = Path(__file__).parent / "fixtures" / "form_fields_minimal.json"


def test_fixture_loads_with_expected_shape():
    fields = load_form_fields(FIXTURE)

    assert fields is not None
    assert [field.form for field in fields] == ["f01", "f01", "f02", "f02", "f02", "f03"]
    assert all(isinstance(field, FieldSpec) for field in fields)
    by_id = {field.field_id: field for field in fields}
    assert by_id["test_a"].cell == "A4"
    assert by_id["test_a"].kind == "text"
    assert by_id["test_a"].required is True
    assert by_id["test_b"].required is False
    assert by_id["Q-01"].kind == "yes_no"
    assert by_id["Q-01"].group is None
    members = [field for field in fields if field.group == "G-01"]
    assert len(members) == 2
    assert {field.group_kind for field in members} == {"single_choice"}
    assert by_id["1-1"].cell == "I5"


def test_query_text_joins_label_description_and_terms():
    fields = load_form_fields(FIXTURE)
    assert fields is not None
    field = next(field for field in fields if field.field_id == "test_a")

    text = query_text(field)

    assert "測試欄位A" in text
    assert "提出本案的單位名稱" in text
    assert "甲方單位" in text


def test_default_path_uses_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom_fields.json"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(target))

    fields = load_form_fields()

    assert fields is not None
    assert len(fields) == 6


def test_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GOVCHECK_FORM_FIELDS_PATH", str(tmp_path / "absent.json"))
    assert load_form_fields() is None
    assert load_form_fields(tmp_path / "absent.json") is None


def test_unreadable_json_returns_none(tmp_path):
    target = tmp_path / "bad.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_form_fields(target) is None


def test_schema_mismatch_returns_none(tmp_path):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    target = tmp_path / "old.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_form_fields(target) is None


def test_unknown_key_or_bad_enum_returns_none(tmp_path):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["fields"][0]["unexpected"] = "x"
    target = tmp_path / "extra.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_form_fields(target) is None

    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["fields"][0]["kind"] = "number"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_form_fields(target) is None


def test_duplicate_field_id_within_form_returns_none(tmp_path):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["fields"].append(dict(raw["fields"][0]))
    target = tmp_path / "dup.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_form_fields(target) is None


def test_empty_or_non_object_root_returns_none(tmp_path):
    target = tmp_path / "list.json"
    target.write_text("[]", encoding="utf-8")
    assert load_form_fields(target) is None
    target.write_text(json.dumps({"schema_version": 1, "fields": []}), encoding="utf-8")
    assert load_form_fields(target) is None
