"""佐證解析轉接層（FileProcess HTTP）測試：全程用 fake_fileprocess，不打真服務。"""

from __future__ import annotations

import io
import zipfile

import requests

from govcheck.models import DocumentParseStatus
from govcheck.parsers.supporting import (
    PARSER_SOURCE_REF,
    ParserSettings,
    TsParserAdapter,
    fileprocess_readiness,
    load_parser_settings,
    parser_source_ref,
)


def _settings(*formats: str, **overrides) -> ParserSettings:
    values = {
        "enabled_formats": set(formats),
        "max_file_bytes": 1024 * 1024,
        "max_pages": 20,
    }
    values.update(overrides)
    return ParserSettings(**values)


# ── 上傳安全預檢（不發請求） ──────────────────────────────────────────────

def test_disabled_format_is_unsupported_without_request(tmp_path, fake_fileprocess):
    source = tmp_path / "mail.msg"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic")

    artifact, usage = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.UNSUPPORTED
    assert artifact.warnings == ["format_disabled"]
    assert fake_fileprocess.calls == []
    assert usage.parser_remote_calls == 0


def test_oversized_file_is_unsupported_without_request(tmp_path, fake_fileprocess):
    source = tmp_path / "big.txt"
    source.write_bytes(b"A" * 2048)

    artifact, _ = TsParserAdapter(_settings(".txt", max_file_bytes=1024)).parse(source)

    assert artifact.status is DocumentParseStatus.UNSUPPORTED
    assert artifact.warnings == ["file_too_large"]
    assert fake_fileprocess.calls == []


def test_invalid_pdf_signature_is_unreadable_without_request(tmp_path, fake_fileprocess):
    source = tmp_path / "evidence.pdf"
    source.write_bytes(b"not-a-pdf")

    artifact, _ = TsParserAdapter(_settings(".pdf")).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert fake_fileprocess.calls == []


def test_invalid_ole_signature_for_msg_is_unreadable(tmp_path, fake_fileprocess):
    source = tmp_path / "mail.msg"
    source.write_bytes(b"plain text pretending to be msg")

    artifact, _ = TsParserAdapter(_settings(".msg")).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert fake_fileprocess.calls == []


def test_invalid_zip_signature_for_zip_is_unreadable(tmp_path, fake_fileprocess):
    source = tmp_path / "bundle.zip"
    source.write_bytes(b"not a zip")

    artifact, _ = TsParserAdapter(_settings(".zip")).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert fake_fileprocess.calls == []


def test_zip_bomb_limit_returns_unsupported(tmp_path, fake_fileprocess):
    source = tmp_path / "large.docx"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * 50_000)
    source.write_bytes(buffer.getvalue())

    artifact, _ = TsParserAdapter(_settings(
        ".docx", max_zip_uncompressed_bytes=100, max_zip_ratio=2
    )).parse(source)

    assert artifact.status is DocumentParseStatus.UNSUPPORTED
    assert artifact.warnings[0] in {"zip_uncompressed_limit_exceeded", "zip_ratio_limit_exceeded"}
    assert fake_fileprocess.calls == []


# ── 成功路徑：200 → PARSED ─────────────────────────────────────────────────

def test_text_source_parses_to_stable_artifact(tmp_path, fake_fileprocess):
    source = tmp_path / "機密名稱.txt"
    source.write_text("公平性測試已完成，結果符合門檻。", encoding="utf-8")

    artifact, usage = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.PARSED
    assert artifact.parser_name == "fileprocess"
    assert artifact.parser_source_commit == "fileprocess@localhost"
    assert artifact.source_units[0].content == "公平性測試已完成，結果符合門檻。"
    assert artifact.source_units[0].locator_type == "page"
    assert artifact.source_units[0].locator_value == "1"
    assert artifact.source_units[0].unit_id == f"{artifact.document_id}:1"
    assert artifact.filename_hash != source.name
    assert usage.parser_remote_calls == 1
    assert usage.parser_vlm_calls == 0 and usage.parser_ocr_calls == 0


def test_request_sends_opaque_filename_not_original(tmp_path, fake_fileprocess):
    source = tmp_path / "客服語音機器人_簽呈.txt"
    source.write_text("內容", encoding="utf-8")

    artifact, _ = TsParserAdapter(_settings(".txt")).parse(source)

    (call,) = fake_fileprocess.calls
    assert call["url"] == "http://localhost:3322/api/parse"
    assert call["filename"] == f"{artifact.document_id}.txt"
    assert "客服" not in call["filename"]
    assert call["metadata"] == {}
    assert call["size"] == len("內容".encode())
    assert call["timeout"] == 120.0


def test_attachment_chunks_get_attachment_locator(tmp_path, fake_fileprocess):
    source = tmp_path / "mail.msg"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic")
    fake_fileprocess.chunks = [
        {"content": "信件本文", "page": 1, "filename": "x.msg", "from_file": "x.msg"},
        {"content": "附件第 2 頁", "page": 2, "filename": "規格書.pdf", "from_file": "x.msg",
         "is_attachment": True},
        # is_attachment 缺席 → 以 from_file != filename 推得
        {"content": "附件工作表", "page": 1, "filename": "測試.xlsx", "from_file": "x.msg"},
    ]

    artifact, _ = TsParserAdapter(_settings(".msg")).parse(source)

    assert artifact.status is DocumentParseStatus.PARSED
    body, pdf, xlsx = artifact.source_units
    assert (body.locator_type, body.locator_value, body.attachment_path) == ("page", "1", None)
    assert (pdf.locator_type, pdf.locator_value, pdf.attachment_path) == (
        "attachment", "2", "規格書.pdf")
    assert (xlsx.locator_type, xlsx.attachment_path) == ("attachment", "測試.xlsx")
    assert [unit.unit_id for unit in artifact.source_units] == [
        f"{artifact.document_id}:{i}" for i in (1, 2, 3)
    ]


def test_all_empty_chunks_is_unreadable(tmp_path, fake_fileprocess):
    source = tmp_path / "blank.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    fake_fileprocess.chunks = [
        {"content": "", "page": 1, "filename": "a.pdf", "from_file": "a.pdf"},
        {"content": "   \n", "page": 2, "filename": "a.pdf", "from_file": "a.pdf"},
    ]

    artifact, _ = TsParserAdapter(_settings(".pdf")).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert artifact.warnings == ["empty_parser_output"]


def test_chunk_count_over_max_pages_is_unreadable(tmp_path, fake_fileprocess):
    source = tmp_path / "long.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    fake_fileprocess.chunks = [
        {"content": f"第 {i} 頁", "page": i, "filename": "a.pdf", "from_file": "a.pdf"}
        for i in range(1, 6)
    ]

    artifact, _ = TsParserAdapter(_settings(".pdf", max_pages=3)).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert artifact.warnings == ["page_limit_exceeded"]
    assert artifact.source_units == []


# ── 失敗對映：狀態碼／例外 → DocumentParseStatus，絕不 raise ─────────────

def test_415_maps_to_unsupported(tmp_path, fake_fileprocess):
    source = tmp_path / "weird.txt"
    source.write_text("x", encoding="utf-8")
    fake_fileprocess.status = 415

    artifact, usage = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.UNSUPPORTED
    assert artifact.warnings == ["parser_not_supported"]
    assert usage.parser_remote_calls == 1


def test_422_and_400_map_to_unreadable_rejected(tmp_path, fake_fileprocess):
    source = tmp_path / "broken.txt"
    source.write_text("x", encoding="utf-8")
    for status in (400, 413, 422):
        fake_fileprocess.status = status
        artifact, _ = TsParserAdapter(_settings(".txt")).parse(source)
        assert artifact.status is DocumentParseStatus.UNREADABLE, status
        assert artifact.warnings == ["fileprocess_rejected"], status


def test_5xx_maps_to_parser_unavailable(tmp_path, fake_fileprocess):
    source = tmp_path / "x.txt"
    source.write_text("x", encoding="utf-8")
    fake_fileprocess.status = 500

    artifact, _ = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.PARSER_UNAVAILABLE
    assert artifact.warnings == ["fileprocess_unavailable"]


def test_connection_error_maps_to_parser_unavailable(tmp_path, fake_fileprocess):
    source = tmp_path / "x.txt"
    source.write_text("x", encoding="utf-8")
    fake_fileprocess.raise_exc = requests.ConnectionError("refused")

    artifact, usage = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.PARSER_UNAVAILABLE
    assert artifact.warnings == ["fileprocess_unavailable"]
    # 請求已嘗試送出，計數要反映；只是對方不可用
    assert usage.parser_remote_calls == 1


def test_timeout_maps_to_parser_unavailable(tmp_path, fake_fileprocess):
    source = tmp_path / "x.txt"
    source.write_text("x", encoding="utf-8")
    fake_fileprocess.raise_exc = requests.Timeout("slow")

    artifact, _ = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.PARSER_UNAVAILABLE


def test_contract_mismatch_is_unreadable(tmp_path, fake_fileprocess):
    source = tmp_path / "x.txt"
    source.write_text("x", encoding="utf-8")
    fake_fileprocess.chunks = [{"page": 1}]  # 缺 content

    artifact, _ = TsParserAdapter(_settings(".txt")).parse(source)

    assert artifact.status is DocumentParseStatus.UNREADABLE
    assert artifact.warnings == ["fileprocess_contract_mismatch"]


def test_unapproved_host_sends_nothing(tmp_path, fake_fileprocess):
    """fail closed：目的地不在核准清單，佐證內容不得出本機。"""
    source = tmp_path / "secret.txt"
    source.write_text("機密", encoding="utf-8")

    artifact, usage = TsParserAdapter(_settings(
        ".txt", fileprocess_url="https://parser.example.com"
    )).parse(source)

    assert artifact.status is DocumentParseStatus.PARSER_UNAVAILABLE
    assert artifact.warnings == ["fileprocess_host_not_approved"]
    assert fake_fileprocess.calls == []
    assert usage.parser_remote_calls == 0


def test_missing_url_sends_nothing(tmp_path, fake_fileprocess):
    source = tmp_path / "secret.txt"
    source.write_text("機密", encoding="utf-8")

    artifact, _ = TsParserAdapter(_settings(".txt", fileprocess_url=None)).parse(source)

    assert artifact.status is DocumentParseStatus.PARSER_UNAVAILABLE
    assert fake_fileprocess.calls == []


def test_duplicate_document_is_counted_as_cache_hit(tmp_path, fake_fileprocess):
    source = tmp_path / "same.txt"
    source.write_text("same", encoding="utf-8")

    artifacts, usage = TsParserAdapter(_settings(".txt")).parse_many([source, source])

    assert len(artifacts) == 1
    assert usage.cache_hits == 1
    assert usage.parser_remote_calls == 1
    assert len(fake_fileprocess.calls) == 1


# ── readiness ───────────────────────────────────────────────────────────────

def test_readiness_reachable(fake_fileprocess):
    result = fileprocess_readiness(_settings(".txt"))

    assert result == {"backend": "fileprocess", "url_host_approved": True, "reachable": True}
    assert fake_fileprocess.health_calls == ["http://localhost:3322/health"]


def test_readiness_unreachable_never_raises(fake_fileprocess):
    fake_fileprocess.health_exc = requests.ConnectionError("refused")

    result = fileprocess_readiness(_settings(".txt"))

    assert result == {"backend": "fileprocess", "url_host_approved": True, "reachable": False}


def test_readiness_unapproved_host_does_not_probe(fake_fileprocess):
    result = fileprocess_readiness(_settings(".txt", fileprocess_url="http://evil.example"))

    assert result == {"backend": "fileprocess", "url_host_approved": False, "reachable": False}
    assert fake_fileprocess.health_calls == []


# ── 設定讀取韌性與可重現性 ───────────────────────────────────────────────────

def test_blank_env_values_fall_back_to_defaults(monkeypatch):
    """.env 慣例會留下 `KEY=` 空值；不可因此丟 ValueError 拖垮整份審查。"""
    for key in ("GOVCHECK_PARSER_FILEPROCESS_TIMEOUT", "GOVCHECK_PARSER_MAX_FILE_BYTES",
                "GOVCHECK_PARSER_MAX_PAGES", "GOVCHECK_PARSER_FILEPROCESS_URL"):
        monkeypatch.setenv(key, "")

    settings = load_parser_settings()

    assert settings.fileprocess_timeout == 120.0
    assert settings.max_pages == 200
    assert settings.fileprocess_url == "http://localhost:3322"


def test_env_overrides_fileprocess_url_and_timeout(monkeypatch):
    monkeypatch.setenv("GOVCHECK_PARSER_FILEPROCESS_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("GOVCHECK_PARSER_FILEPROCESS_TIMEOUT", "5")

    settings = load_parser_settings()

    assert settings.fileprocess_url == "http://127.0.0.1:9999/"
    assert settings.fileprocess_timeout == 5.0
    assert settings.fileprocess_host_is_approved()


def test_legacy_env_keys_are_silently_ignored(monkeypatch):
    for key in ("GOVCHECK_PARSER_WORD_STRATEGY", "GOVCHECK_PARSER_MODEL_HOST",
                "GOVCHECK_PARSER_OCR_ENABLED", "GOVCHECK_PARSER_VLM_ENABLED",
                "GOVCHECK_PARSER_TEMP_ROOT", "GOVCHECK_PARSER_TIMEOUT"):
        monkeypatch.setenv(key, "garbage")

    settings = load_parser_settings()

    assert not hasattr(settings, "word_strategy")
    assert settings.fileprocess_url == "http://localhost:3322"


def test_garbage_env_value_falls_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv("GOVCHECK_PARSER_MAX_PAGES", "not-a-number")
    assert load_parser_settings().max_pages == 200


def test_default_config_enables_new_formats():
    formats = load_parser_settings().normalized_formats()
    assert {".pptx", ".msg", ".zip"} <= formats


def test_config_hash_is_stable_for_equal_settings():
    """parser_config_hash 是稽核用的設定指紋：同設定必須算出同一個值。"""
    a = ParserSettings(
        enabled_formats={".pdf", ".txt", ".docx"},
        approved_model_hosts={"localhost", "127.0.0.1"},
    )
    b = ParserSettings(
        enabled_formats={".docx", ".pdf", ".txt"},      # 同內容、不同插入順序
        approved_model_hosts={"127.0.0.1", "localhost"},
    )
    assert a.safe_hash() == b.safe_hash()


def test_safe_hash_has_no_plaintext_url():
    settings = ParserSettings(fileprocess_url="http://internal-parser.bank.local:3322")
    digest = settings.safe_hash()
    assert "internal-parser" not in digest and "3322" not in digest
    assert digest != ParserSettings(fileprocess_url="http://localhost:3322").safe_hash()


def test_parser_source_ref_only_exposes_host():
    assert PARSER_SOURCE_REF == "fileprocess@localhost"
    assert parser_source_ref("http://10.0.0.5:3322/base") == "fileprocess@10.0.0.5"
    assert parser_source_ref(None) == "fileprocess@unconfigured"
